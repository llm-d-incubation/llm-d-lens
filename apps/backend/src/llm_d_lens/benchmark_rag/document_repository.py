"""OpenSearch authority for public document publication metadata.

The management head authorizes metadata, never retrieval: only the retrieval
alias selects searchable builds. Writers and the alias writer must hold
``publication_lock`` throughout their complete operation, on the same thread.
The flock protocol supports one host with a shared persistent Lens data directory;
multi-host writers are deliberately unsupported. Locks never expire or steal.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from llm_d_lens.benchmark_rag.contracts import ItemKind, RagItem
from llm_d_lens.benchmark_rag.publication_contracts import (
    PublicationConflictError,
    PublicationError,
    PublicationEvent,
    PublicationResult,
    RegistryState,
    ReleaseManifest,
    SourceApproval,
    SourceHead,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.query_contracts import QueryFilters
from llm_d_lens.benchmark_rag.search_contracts import PublicSearchScope
from llm_d_lens.benchmark_rag.sources import (
    PRISM_ORIGIN,
    WEB_SOURCE_PATHS,
    allowed_paths,
    input_format,
    validate_approval,
)
from llm_d_lens.core.exceptions import ConflictError
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.dao.rag_query import QueryMatch, QuerySlice
from llm_d_lens.infrastructure.storage.paths import storage_path

MANAGEMENT_INDEX = "public-rag-management-v1"
METADATA_READ_BATCH_SIZE = 256
_locks = threading.local()
_MAPPING = {
    "dynamic": "strict",
    "_meta": {"contract": "public-rag-management.v1"},
    "properties": {
        "contract": {"type": "keyword"},
        "sha256": {"type": "keyword"},
        "item": {"type": "object", "enabled": False},
    },
}
_EXTERNAL_FIELDS = (
    "item_id",
    "kind",
    "origin_id",
    "logical_key",
    "source_revision",
    "content_sha256",
    "access_classification",
    "publication_state",
    "source_path",
    "format",
    "parent_id",
    "experiment_id",
    "referenced_item_id",
)


def external_member_reference(item: RagItem) -> dict[str, Any]:
    """Retain immutable membership proof, never a structured experiment body."""
    if item.kind in ("source", "passage"):
        raise PublicationError("document members must not become external references")
    payload = item.model_dump(mode="json")
    return {
        "contract": "public-rag.external-member.v1",
        **{field: payload[field] for field in _EXTERNAL_FIELDS},
        "payload_sha256": content_digest(payload),
    }


def validate_external_reference(key: str, value: dict[str, Any]) -> None:
    if (
        set(value) != {*_EXTERNAL_FIELDS, "contract", "payload_sha256"}
        or value.get("contract") != "public-rag.external-member.v1"
        or value.get("item_id") != key
        or value.get("kind") not in ("experiment", "measurement", "figure", "figure_link")
        or value.get("access_classification") != "verified_public"
        or value.get("publication_state") not in ("staged", "published", "retired")
        or not all(
            isinstance(value.get(field), str)
            for field in (
                "origin_id",
                "logical_key",
                "source_revision",
                "content_sha256",
                "source_path",
                "format",
            )
        )
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("payload_sha256"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("content_sha256"))) is None
        or key != item_id(value["origin_id"], value["logical_key"], value["source_revision"])
    ):
        raise PublicationError("external manifest member identity is invalid")
    if any(
        value[field] is not None and not isinstance(value[field], str)
        for field in ("parent_id", "experiment_id", "referenced_item_id")
    ):
        raise PublicationError("external manifest member dependency is invalid")


def validate_document_member_map(
    manifest: RagItem,
    metadata: ReleaseManifest,
    approval: SourceApproval,
    documents: dict[str, RagItem],
    references: dict[str, dict[str, Any]],
) -> None:
    """Validate unchanged mixed membership without loading external fact bodies."""
    members = {
        key: documents[key].model_dump(mode="json") if key in documents else references.get(key)
        for key in metadata.member_ids
    }
    if references and manifest.origin_id in WEB_SOURCE_PATHS:
        raise PublicationError("web members must remain context-only documents")
    if any(value is None for value in members.values()):
        raise PublicationError("manifest has missing document or external members")
    for key, reference in references.items():
        validate_external_reference(key, reference)
    values = {key: value for key, value in members.items() if value is not None}
    if dict(Counter(value["kind"] for value in values.values())) != metadata.counts:
        raise PublicationError("manifest kind counts are invalid")
    if len({value["logical_key"] for value in values.values()}) != len(values):
        raise PublicationError("a release cannot mix versions of one logical item")
    for entry in metadata.files:
        source = documents.get(entry.file_item_id)
        if (
            source is None
            or source.kind != "source"
            or source.source_role != "file"
            or source.parent_id != registry_id(manifest.origin_id)
            or source.source_details != {"parser_version": metadata.parser_version}
            or input_format(manifest.origin_id, entry.file.path, approval) != entry.file.format
        ):
            raise PublicationError("manifest file source is invalid")
        for key in entry.member_ids:
            value = values[key]
            if (
                value["origin_id"] != manifest.origin_id
                or value["source_path"] != entry.file.path
                or value["format"] != entry.file.format
                or value["content_sha256"] != entry.file.sha256
                or value["source_revision"] != source.source_revision
                or value["access_classification"] != "verified_public"
                or value["publication_state"] == "tombstone"
                or key != item_id(value["origin_id"], value["logical_key"], value["source_revision"])
            ):
                raise PublicationError("member does not match its pinned file")
            kind = value["kind"]
            if kind == "source":
                if key != source.item_id:
                    raise PublicationError("manifest contains an invalid source member")
                continue
            for field, expected_kind in (
                (
                    "parent_id",
                    "figure" if kind == "figure_link" else "experiment" if kind == "measurement" else "source",
                ),
                ("experiment_id", "experiment"),
                ("referenced_item_id", "experiment"),
            ):
                dependency = value.get(field)
                target = values.get(dependency) if dependency is not None else None
                if dependency is not None and (
                    target is None
                    or target["kind"] != expected_kind
                    or target["origin_id"] != manifest.origin_id
                    or target["source_revision"] != value["source_revision"]
                ):
                    raise PublicationError("manifest is not closed over its external dependencies")
            if kind in ("experiment", "passage", "figure") and value["parent_id"] != source.item_id:
                raise PublicationError("member must link to its exact pinned source file")
            if kind == "measurement" and value["parent_id"] != value["experiment_id"]:
                raise PublicationError("measurement must link to its experiment")
            if kind == "measurement" and value["experiment_id"] is None:
                raise PublicationError("measurement experiment reference is required")
            if kind == "figure_link" and (value["parent_id"] is None or value["referenced_item_id"] is None):
                raise PublicationError("figure link targets are required")


class OpenSearchDocumentRepository:
    """Synchronous DAO-shaped repository; no SQL sessions or fallback reads."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        lock_path: Path | None = None,
        writer_mode: str | None = None,
    ) -> None:
        url = httpx.URL(endpoint)
        if url.scheme not in ("http", "https") or not url.host or url.query or url.fragment:
            raise PublicationError("a valid OpenSearch HTTP endpoint is required")
        mode = writer_mode if writer_mode is not None else os.environ.get("LENS_PUBLIC_RAG_WRITER_MODE", "single-host")
        if mode != "single-host":
            raise PublicationError("document writers require single-host coordination; multi-host is unsupported")
        self.endpoint = endpoint.rstrip("/")
        self._owned_client = client is None
        self.client = client if client is not None else httpx.Client(timeout=30.0)
        self.headers = {"Authorization": f"ApiKey {api_key}"} if api_key else {}
        self.lock_path = (lock_path or storage_path("data", "public-rag", "publication.lock")).resolve()

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    @classmethod
    def from_environment(cls, *, client: httpx.Client | None = None) -> OpenSearchDocumentRepository:
        legacy_mode = os.environ.get("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY")
        if legacy_mode not in (None, "opensearch"):
            raise PublicationError(
                "PostgreSQL document authority has been removed; unset LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY"
            )
        endpoint = os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_URL")
        if not endpoint:
            raise PublicationError("public document metadata requires LENS_PUBLIC_RAG_OPENSEARCH_URL")
        return cls(
            endpoint,
            api_key=os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_API_KEY"),
            client=client,
        )

    @contextmanager
    def publication_lock(self) -> Iterator[None]:
        """Reentrant, cross-process single-host lock; never unlink its inode."""
        held = getattr(_locks, "held", None) if getattr(_locks, "pid", None) == os.getpid() else None
        if held is None:
            _locks.pid = os.getpid()
            held = _locks.held = {}
        key = str(self.lock_path)
        if key in held:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = handle
            try:
                yield
            finally:
                del held[key]
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, f"{self.endpoint}/{path}", headers=self.headers, **kwargs)
        except httpx.HTTPError as error:
            raise PublicationError("OpenSearch document metadata is unavailable") from error
        if response.status_code == 409:
            raise PublicationConflictError("OpenSearch metadata CAS conflict; reload the source head")
        if response.status_code >= 400:
            raise PublicationError(f"OpenSearch document metadata request failed ({response.status_code})")
        return response

    def prepare(self, *, initialize: bool = False) -> None:
        if initialize:
            try:
                self._request("PUT", MANAGEMENT_INDEX, json={"mappings": _MAPPING})
            except PublicationError:
                # Existing indexes must pass the same mapping check as new ones.
                self._check_mapping()
                return
        self._check_mapping()

    def _check_mapping(self) -> None:
        try:
            mapping = self._request("GET", f"{MANAGEMENT_INDEX}/_mapping").json()[MANAGEMENT_INDEX]["mappings"]
            if (
                mapping.get("_meta") != _MAPPING["_meta"]
                or mapping.get("dynamic") != "strict"
                or mapping.get("properties") != _MAPPING["properties"]
            ):
                raise PublicationError("incompatible document management index mapping")
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationError("invalid document management index mapping") from error

    def refresh_management_index(self) -> None:
        try:
            response = self._request("POST", f"{MANAGEMENT_INDEX}/_refresh").json()
            if response.get("_shards", {}).get("failed") != 0:
                raise ValueError("management index refresh failed")
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationError("document management index refresh is incomplete") from error

    @staticmethod
    def _envelope(item: RagItem) -> dict[str, Any]:
        payload = item.model_dump(mode="json")
        return {"contract": "public-rag-management.v1", "sha256": content_digest(payload), "item": payload}

    @staticmethod
    def _record_value(key: str, body: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        envelope = body["_source"]
        payload = envelope["item"]
        if (
            not isinstance(payload, dict)
            or envelope
            != {
                "contract": "public-rag-management.v1",
                "sha256": content_digest(payload),
                "item": payload,
            }
            or body["_id"] != key
            or type(body["_seq_no"]) is not int
            or body["_seq_no"] < 0
            or type(body["_primary_term"]) is not int
            or body["_primary_term"] < 1
        ):
            raise PublicationError("document metadata identity or payload hash is invalid")
        return payload, body["_seq_no"], body["_primary_term"]

    def read_record(self, key: str) -> tuple[dict[str, Any], int, int] | None:
        """Read a hash-verified management payload and its exact CAS version."""
        path = f"{MANAGEMENT_INDEX}/_doc/{quote(key, safe='')}"
        try:
            response = self.client.get(f"{self.endpoint}/{path}", params={"realtime": "true"}, headers=self.headers)
            body = response.json()
            if response.status_code == 404 and body.get("found") is False:
                return None
            if response.status_code != 200:
                raise PublicationError(f"OpenSearch metadata read failed ({response.status_code})")
            return self._record_value(key, body)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as error:
            raise PublicationError("invalid or unavailable OpenSearch document metadata") from error

    def _read_records(self, keys: Sequence[str]) -> dict[str, tuple[dict[str, Any], int, int] | None]:
        """Batch real-time reads without retaining authority across resolution calls."""
        ordered = tuple(dict.fromkeys(keys))
        records = {}
        try:
            for offset in range(0, len(ordered), METADATA_READ_BATCH_SIZE):
                batch = ordered[offset : offset + METADATA_READ_BATCH_SIZE]
                payload = self._request(
                    "POST", f"{MANAGEMENT_INDEX}/_mget", params={"realtime": "true"}, json={"ids": batch}
                ).json()
                docs = payload["docs"]
                if not isinstance(docs, list) or len(docs) != len(batch):
                    raise PublicationError("document metadata batch is incomplete")
                for key, body in zip(batch, docs, strict=True):
                    if (
                        not isinstance(body, dict)
                        or body.get("_id") != key
                        or body.get("_index") != MANAGEMENT_INDEX
                        or "error" in body
                        or type(body.get("found")) is not bool
                    ):
                        raise PublicationError("document metadata batch identity or result is invalid")
                    if not body["found"]:
                        if "_source" in body:
                            raise PublicationError("missing document metadata has an unexpected payload")
                        records[key] = None
                    else:
                        records[key] = self._record_value(key, body)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as error:
            raise PublicationError("invalid or unavailable OpenSearch document metadata batch") from error
        return records

    def create_record(self, key: str, value: dict[str, Any]) -> None:
        """Create a lifecycle record; existing keys are explicit conflicts."""
        if ":" not in key or key.startswith("shadow:"):
            raise PublicationError("lifecycle records require a reserved namespaced key")
        with self.publication_lock():
            self._request(
                "PUT",
                f"{MANAGEMENT_INDEX}/_create/{quote(key, safe='')}",
                params={"refresh": "wait_for"},
                json={"contract": "public-rag-management.v1", "sha256": content_digest(value), "item": value},
            )

    def replace_record(self, key: str, value: dict[str, Any], seq_no: int, primary_term: int) -> None:
        """Replace a lifecycle record using the caller's observed CAS version."""
        if ":" not in key or key.startswith("shadow:"):
            raise PublicationError("lifecycle records require a reserved namespaced key")
        with self.publication_lock():
            self._request(
                "PUT",
                f"{MANAGEMENT_INDEX}/_doc/{quote(key, safe='')}",
                params={"if_seq_no": seq_no, "if_primary_term": primary_term, "refresh": "wait_for"},
                json={"contract": "public-rag-management.v1", "sha256": content_digest(value), "item": value},
            )

    def _scan_record_keys(self) -> Iterator[str]:
        scroll_id: str | None = None
        try:
            response = self._request(
                "POST",
                f"{MANAGEMENT_INDEX}/_search",
                params={"scroll": "1m"},
                json={"size": 500, "sort": ["_doc"], "query": {"match_all": {}}, "_source": False},
            ).json()
            while True:
                if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
                    raise PublicationError("document metadata scan is incomplete")
                scroll_id = response.get("_scroll_id", scroll_id)
                hits = response["hits"]["hits"]
                if not hits:
                    return
                for hit in hits:
                    yield hit["_id"]
                if scroll_id is None:
                    raise PublicationError("document metadata scan has no continuation")
                response = self._request(
                    "POST",
                    "_search/scroll",
                    json={"scroll": "1m", "scroll_id": scroll_id},
                ).json()
        except PublicationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationError("invalid document metadata scan response") from error
        finally:
            if scroll_id is not None:
                self._request("DELETE", "_search/scroll", json={"scroll_id": [scroll_id]})

    def list_records(
        self,
        prefix: str,
        *,
        max_records: int = 100_000,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> tuple[dict[str, Any], ...]:
        """List real-time, hash-verified payloads in canonical key order."""
        if not prefix or max_records < 1 or max_bytes < 1:
            raise PublicationError("record listing requires a prefix and positive bounds")
        records: dict[str, dict[str, Any]] = {}
        size = 0
        with self.publication_lock(), closing(self._scan_record_keys()) as keys:
            for key in keys:
                if not key.startswith(prefix) or key in records:
                    continue
                record = self.read_record(key)
                if record is None:
                    raise PublicationError("document metadata changed during record listing")
                records[key] = record[0]
                size += len(json.dumps(record[0], separators=(",", ":"), ensure_ascii=True).encode())
                if len(records) > max_records or size > max_bytes:
                    raise PublicationError("document metadata record listing exceeds its bounds")
        return tuple(records[key] for key in sorted(records))

    @staticmethod
    def _record_item(key: str, payload: dict[str, Any]) -> RagItem:
        try:
            item = RagItem.model_validate(payload)
        except ValidationError as error:
            raise PublicationError("invalid document item metadata") from error
        if not key.startswith("shadow:") and item.item_id != key:
            raise PublicationError("document metadata key does not match its immutable identity")
        return item

    def _read_record(self, key: str) -> tuple[RagItem, int, int] | None:
        record = self.read_record(key)
        if record is None:
            return None
        payload, sequence, term = record
        return self._record_item(key, payload), sequence, term

    def snapshot(self, *, max_records: int = 100_000, max_bytes: int = 1_073_741_824) -> tuple[RagItem, ...]:
        """Bounded complete live metadata snapshot, excluding shadows/build state."""
        if max_records < 1 or max_bytes < 1:
            raise PublicationError("snapshot bounds must be positive")
        records: dict[str, RagItem] = {}
        size = 0
        with self.publication_lock(), closing(self._scan_record_keys()) as keys:
            for key in keys:
                if re.fullmatch(r"[0-9a-f]{64}", key) is None or key in records:
                    continue
                item = self.get(key)
                if item is None:
                    raise PublicationError("document metadata changed during snapshot")
                size += len(item.model_dump_json().encode())
                records[key] = item
                if len(records) > max_records or size > max_bytes:
                    raise PublicationError("document metadata snapshot exceeds its bounds")
        return tuple(records[key] for key in sorted(records))

    def get(self, item_id: str) -> RagItem | None:
        record = self._read_record(item_id)
        return record[0] if record is not None else None

    def _create_immutable(self, item: RagItem, *, key: str | None = None, refresh: bool = True) -> RagItem:
        if item.origin_id == PRISM_ORIGIN:
            raise PublicationError("Prism snapshots remain SQL-owned and cannot be imported into document authority")
        if item.kind not in ("source", "passage"):
            raise PublicationError(
                "document authority cannot store structured experiment or figure facts; keep them in SQL"
            )
        key = key or item.item_id
        existing = self._read_record(key)
        if existing is not None:
            if existing[0] != item:
                raise PublicationError("conflicting immutable document metadata")
            return existing[0]
        try:
            self._request(
                "PUT",
                f"{MANAGEMENT_INDEX}/_create/{quote(key, safe='')}",
                params={"refresh": "wait_for"} if refresh else {},
                json=self._envelope(item),
            )
        except PublicationConflictError:
            existing = self._read_record(key)
            if existing is None or existing[0] != item:
                raise
        return item

    def save_external_reference(
        self, value: dict[str, Any], *, namespace: str = "external", refresh: bool = True
    ) -> None:
        """Idempotently retain an immutable SQL-member identity, without facts."""
        key = value.get("item_id")
        validate_external_reference(key, value)
        if value["origin_id"] == PRISM_ORIGIN:
            raise PublicationError("Prism memberships remain SQL-owned")
        if namespace != "external" and re.fullmatch(r"shadow:[0-9a-f]{64}:external", namespace) is None:
            raise PublicationError("invalid external membership namespace")
        record_key = f"{namespace}:{key}"
        with self.publication_lock():
            existing = self.read_record(record_key)
            if existing is not None:
                if existing[0] != value:
                    raise PublicationError("conflicting immutable external membership")
                return
            try:
                self._request(
                    "PUT",
                    f"{MANAGEMENT_INDEX}/_create/{quote(record_key, safe='')}",
                    params={"refresh": "wait_for"} if refresh else {},
                    json={"contract": "public-rag-management.v1", "sha256": content_digest(value), "item": value},
                )
            except PublicationConflictError:
                existing = self.read_record(record_key)
                if existing is None or existing[0] != value:
                    raise

    def _registry(self, origin: str) -> tuple[RagItem, RegistryState, int, int]:
        allowed_paths(origin)
        record = self._read_record(registry_id(origin))
        if record is None:
            raise PublicationError("source is not registered")
        item, sequence, term = record
        state = RegistryState.model_validate(item.source_details)
        validate_approval(origin, state.approval)
        if (
            item.origin_id != origin
            or item.kind != "source"
            or item.source_role != "registry"
            or item.logical_key != "@registry"
            or item.source_revision != "registry-v1"
            or item.content_sha256 != content_digest(state.approval.model_dump(mode="json"))
        ):
            raise PublicationError("source approval identity or hash is invalid")
        return item, state, sequence, term

    def _update_registry(self, item: RagItem, sequence: int, term: int) -> None:
        self._request(
            "PUT",
            f"{MANAGEMENT_INDEX}/_doc/{item.item_id}",
            params={"if_seq_no": sequence, "if_primary_term": term, "refresh": "wait_for"},
            json=self._envelope(item),
        )

    def register(self, origin_id: str, approval: SourceApproval) -> RagItem:
        if origin_id == PRISM_ORIGIN:
            raise PublicationError("Prism source approval remains SQL-owned")
        validate_approval(origin_id, approval)
        with self.publication_lock():
            existing = self.get(registry_id(origin_id))
            if existing is not None:
                item, state, _, _ = self._registry(origin_id)
                if state.approval != approval:
                    raise PublicationError("source approval changes require an explicit compatibility review")
                return item
            return self._create_immutable(
                RagItem(
                    item_id=registry_id(origin_id),
                    kind="source",
                    source_role="registry",
                    origin_id=origin_id,
                    logical_key="@registry",
                    source_revision="registry-v1",
                    content_sha256=content_digest(approval.model_dump(mode="json")),
                    access_classification="verified_public",
                    source_path="@registry",
                    format="json",
                    source_details=RegistryState(approval=approval).model_dump(mode="json"),
                )
            )

    def head(self, origin_id: str) -> SourceHead:
        item, state, _, _ = self._registry(origin_id)
        manifest = self.get(item.published_manifest_id) if item.published_manifest_id else None
        if item.published_manifest_id is not None:
            if manifest is None:
                raise PublicationError("published manifest is missing")
            RagPublicationDao._manifest(manifest, origin_id, state.approval)
        return SourceHead(registry=item, state=state, manifest=manifest)

    def _members(
        self,
        manifest: RagItem,
        metadata: ReleaseManifest,
        approval: SourceApproval,
        supplied: Sequence[RagItem] = (),
        *,
        batch_reads: bool = False,
    ) -> dict[str, RagItem]:
        items = {item.item_id: item for item in supplied}
        references = {}
        if len(items) != len(supplied) or items.keys() - set(metadata.member_ids):
            raise PublicationError("staged items must be unique exact manifest members")
        pending = tuple(key for key in metadata.member_ids if key not in items)
        records = self._read_records(pending) if batch_reads else {}
        external = (
            self._read_records(tuple(f"external:{key}" for key in pending if records[key] is None))
            if batch_reads
            else {}
        )
        for key in metadata.member_ids:
            if key not in items:
                record = records[key] if batch_reads else None
                value = self._record_item(key, record[0]) if record is not None else None
                if not batch_reads:
                    value = self.get(key)
                if value is not None:
                    items[key] = value
                else:
                    reference = external[f"external:{key}"] if batch_reads else self.read_record(f"external:{key}")
                    if reference is not None:
                        references[key] = reference[0]
        registry = self.get(registry_id(manifest.origin_id))
        if registry is not None:
            items[registry.item_id] = registry
        if references:
            validate_document_member_map(manifest, metadata, approval, items, references)
        else:
            RagPublicationDao._validate_member_map(manifest, metadata, approval, items)
        return items

    def stage(self, manifest: RagItem, items: Sequence[RagItem]) -> RagItem:
        with self.publication_lock():
            head = self.head(manifest.origin_id)
            if not head.state.enabled:
                raise PublicationError("source is disabled")
            metadata = RagPublicationDao._manifest(manifest, manifest.origin_id, head.state.approval)
            if any(kind not in ("source", "passage") and count for kind, count in metadata.counts.items()):
                raise PublicationError(
                    "mixed document/structured releases require an explicit document split before cutover; "
                    "publish the existing shared manifest in SQL"
                )
            if (metadata.base_generation, metadata.base_manifest_id) != (
                head.state.generation,
                head.registry.published_manifest_id,
            ):
                raise PublicationConflictError("source advanced while preparing this release")
            self._members(manifest, metadata, head.state.approval, items)
            old = set(head.manifest.manifest_item_ids or ()) if head.manifest else set()
            if set(metadata.removed_item_ids) != old - set(metadata.member_ids):
                raise PublicationError("manifest removal set does not match the current release")
            # Partial failures leave only unreachable immutable staging, never a new head.
            for item in items:
                self._create_immutable(item)
            return self._create_immutable(manifest)

    def activate(
        self,
        origin_id: str,
        manifest_id: str,
        verify_sources: Callable[[ReleaseManifest], None],
        *,
        rollback_generation: int | None = None,
        expected_generation: int | None = None,
    ) -> PublicationResult:
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 0):
            raise PublicationError("explicit document activation requires a nonnegative target generation")
        if expected_generation is not None and rollback_generation is not None:
            raise PublicationError("document activation approval and rollback are distinct operations")
        with self.publication_lock():
            registry, state, sequence, term = self._registry(origin_id)
            if not state.enabled:
                raise PublicationError("source is disabled")
            if expected_generation is not None and expected_generation != state.generation:
                raise PublicationConflictError("source advanced before explicit document activation approval")
            manifest = self.get(manifest_id)
            if manifest is None:
                raise PublicationError("manifest does not exist")
            metadata = RagPublicationDao._manifest(manifest, origin_id, state.approval)
            previous = registry.published_manifest_id
            if rollback_generation is not None:
                if rollback_generation != state.generation:
                    raise PublicationConflictError("source advanced before rollback")
                if not state.was_published(manifest_id):
                    raise PublicationError("rollback target was never published")
            elif (
                expected_generation is None
                and previous != manifest_id
                and (metadata.base_generation != state.generation or metadata.base_manifest_id != previous)
            ):
                raise PublicationConflictError("staged release is stale; rebuild against the current head")
            self._members(manifest, metadata, state.approval)
            verify_sources(metadata)
            changed = previous != manifest_id
            generation = state.generation + int(changed)
            if changed:
                event = PublicationEvent(
                    operation="rollback" if rollback_generation is not None else "activate",
                    manifest_id=manifest_id,
                    previous_manifest_id=previous,
                    generation=generation,
                    at=datetime.now(UTC),
                    counts=metadata.counts,
                )
                updated = state.model_copy(update={"generation": generation, "history": (*state.history, event)})
                self._update_registry(
                    registry.model_copy(
                        update={
                            "published_manifest_id": manifest_id,
                            "source_details": updated.model_dump(mode="json"),
                        }
                    ),
                    sequence,
                    term,
                )
            return PublicationResult(
                origin_id=origin_id,
                manifest_id=manifest_id,
                previous_manifest_id=previous,
                generation=generation,
                counts=metadata.counts,
                changed=changed,
            )

    def set_enabled(self, origin_id: str, enabled: bool, *, expected_generation: int) -> None:
        with self.publication_lock():
            registry, state, sequence, term = self._registry(origin_id)
            if state.generation != expected_generation:
                raise PublicationConflictError("source advanced before access-policy change")
            if state.enabled == enabled:
                return
            event = PublicationEvent(
                operation="enable" if enabled else "disable",
                manifest_id=registry.published_manifest_id,
                previous_manifest_id=registry.published_manifest_id,
                generation=state.generation + 1,
                at=datetime.now(UTC),
                counts={},
            )
            updated = state.model_copy(
                update={
                    "enabled": enabled,
                    "generation": event.generation,
                    "history": (*state.history, event),
                }
            )
            self._update_registry(
                registry.model_copy(update={"source_details": updated.model_dump(mode="json")}), sequence, term
            )

    def public_search_scopes(self, origins: tuple[str, ...]) -> tuple[PublicSearchScope, ...]:
        scopes = []
        for origin in sorted(set(origins)):
            allowed_paths(origin)
            if self.get(registry_id(origin)) is None:
                continue
            head = self.head(origin)
            if head.state.enabled and head.manifest is not None:
                scopes.append(
                    PublicSearchScope(
                        origin_id=origin,
                        manifest_id=head.manifest.item_id,
                        generation=head.state.generation,
                    )
                )
        return tuple(scopes)

    def list_published(self, origin_id: str, *, kind: ItemKind | None = "passage") -> list[RagItem]:
        head = self.head(origin_id)
        if not head.state.enabled or head.manifest is None:
            return []
        metadata = RagPublicationDao._manifest(head.manifest, origin_id, head.state.approval)
        members = self._members(head.manifest, metadata, head.state.approval)
        return sorted(
            (
                members[key]
                for key in metadata.member_ids
                if key in members and (kind is None or members[key].kind == kind)
            ),
            key=lambda item: item.item_id,
        )

    def resolve_public_passages(
        self,
        origins: tuple[str, ...],
        item_ids: tuple[str, ...],
    ) -> tuple[QueryMatch, ...]:
        if not origins or not item_ids or len(item_ids) > 200:
            raise PublicationError("passage resolution requires bounded origins and evidence IDs")
        matches = []
        wanted = set(item_ids)
        for origin in sorted(set(origins)):
            head = self.head(origin)
            if not head.state.enabled or head.manifest is None:
                continue
            metadata = RagPublicationDao._manifest(head.manifest, origin, head.state.approval)
            members = self._members(head.manifest, metadata, head.state.approval, batch_reads=True)
            files = {entry.file.path: entry.file_item_id for entry in metadata.files}
            for key in sorted(wanted.intersection(metadata.member_ids)):
                item = members.get(key)
                if item is not None and item.kind == "passage":
                    matches.append(
                        QueryMatch(
                            item=item,
                            experiment=None,
                            source=members[files[item.source_path]],
                            manifest_id=head.manifest.item_id,
                            generation=head.state.generation,
                        )
                    )
        resolved = tuple(matches)
        self.assert_current(resolved)
        return resolved

    def assert_current(self, matches: tuple[QueryMatch, ...]) -> None:
        heads = {}
        for match in matches:
            if match.item.origin_id not in heads:
                heads[match.item.origin_id] = self.head(match.item.origin_id)
            head = heads[match.item.origin_id]
            if (
                not head.state.enabled
                or head.manifest is None
                or (head.manifest.item_id, head.state.generation) != (match.manifest_id, match.generation)
                or match.item.item_id not in (head.manifest.manifest_item_ids or ())
            ):
                raise ConflictError("RAG publication or source access changed during the query; retry")

    def search(self, filters: QueryFilters, *, intent: str, text_query: str, limit: int | None) -> QuerySlice:
        """Definition compatibility adapter, pinned to the lexical retrieval alias."""
        from llm_d_lens.benchmark_rag.indexing import passage_document, passage_projection_matches
        from llm_d_lens.benchmark_rag.opensearch_evidence_search import (
            ACTIVE_ALIAS,
            _mapping_build,
            source_filter,
            technical_identifiers,
        )
        from llm_d_lens.benchmark_rag.runtime import load_runtime_policy
        from llm_d_lens.benchmark_rag.search_contracts import PublicSearchHit

        if intent != "definition" or limit is None or not 1 <= limit <= 50:
            raise PublicationError("document retrieval supports bounded definitions only")
        if not load_runtime_policy().search_enabled:
            raise PublicationError("Public document search is administratively disabled")
        if not text_query:
            return QuerySlice(matches=(), total=0)
        document_filters = {"origins", "source_revision", "source_path", "normalized"}
        if filters.model_dump(exclude_defaults=True).keys() - document_filters:
            return QuerySlice(matches=(), total=0)
        with self.publication_lock():
            pending = self.read_record("activation:lexical")
            if pending is not None and pending[0].get("status") != "settled":
                raise PublicationError("uncertain alias publication requires administrative recovery")
            try:
                alias = self._request("GET", f"{ACTIVE_ALIAS}/_mapping").json()
                if len(alias) != 1:
                    raise PublicationError("public alias must resolve to exactly one physical index")
                index, mapping = next(iter(alias.items()))
                build = _mapping_build(index, mapping)
                if build.retrieval_mode != "opensearch_lexical":
                    raise PublicationError("document definitions require a lexical alias")
                history = self.read_record("build:" + build.build_id)
                if (
                    history is None
                    or history[0].get("status") != "active"
                    or history[0].get("physical_index") != index
                    or history[0].get("build") != build.model_dump(mode="json")
                ):
                    raise PublicationError("public alias points to an unverified document build")
                scopes = self.public_search_scopes(filters.origins)
                expected = tuple(scope for scope in build.scopes if scope.origin_id in filters.origins)
                if scopes != expected:
                    raise ConflictError("document source access changed; rebuild the public alias")
                if not scopes:
                    return QuerySlice(matches=(), total=0)
                clauses = source_filter(scopes)["bool"]["filter"]
                for field in ("source_path", "source_revision"):
                    value = getattr(filters, field)
                    if value is not None:
                        clauses.append({"term": {field: value}})
                payload = self._request(
                    "POST",
                    f"{index}/_search",
                    json={
                        "size": limit,
                        "track_total_hits": True,
                        "query": {
                            "bool": {
                                "filter": clauses,
                                "minimum_should_match": 1,
                                "should": [
                                    {
                                        "multi_match": {
                                            "query": text_query,
                                            "fields": ["title^3", "heading_path^2", "text", "text.english", "text.cjk"],
                                        }
                                    },
                                    {
                                        "terms": {
                                            "identifiers": list(technical_identifiers(text_query))[:32],
                                            "boost": 2,
                                        }
                                    },
                                ],
                            }
                        },
                        "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
                    },
                ).json()
                if payload["timed_out"] or payload["_shards"]["failed"]:
                    raise PublicationError("document definition search is incomplete")
                hits = tuple(
                    PublicSearchHit(document=hit["_source"], score=hit["_score"]) for hit in payload["hits"]["hits"]
                )
                if len(hits) > limit:
                    raise PublicationError("document search exceeded its bound")
                resolved = (
                    self.resolve_public_passages(
                        filters.origins,
                        tuple(hit.document.evidence_id for hit in hits),
                    )
                    if hits
                    else ()
                )
                members = {match.item.item_id: match for match in resolved}
                scope_map = {scope.origin_id: scope for scope in scopes}
                matches = []
                for hit in hits:
                    match = members.get(hit.document.evidence_id)
                    scope = scope_map.get(hit.document.origin_id)
                    if match is None or scope is None:
                        raise PublicationError("document projection differs from its authorized source")
                    expected_document = passage_document(match.item, scope)
                    if not passage_projection_matches(hit.document, expected_document, expected_document):
                        raise PublicationError("document projection differs from its authorized source")
                    matches.append(match)
                self.assert_current(tuple(matches))
                if self._request("GET", f"{ACTIVE_ALIAS}/_mapping").json() != alias:
                    raise ConflictError("document retrieval alias changed during the query; retry")
                return QuerySlice(matches=tuple(matches), total=payload["hits"]["total"]["value"])
            except PublicationError:
                raise
            except (KeyError, TypeError, ValueError, ValidationError) as error:
                raise PublicationError("invalid document definition search response") from error


def configured_document_repository(*, client: httpx.Client | None = None) -> OpenSearchDocumentRepository:
    """Public documents have one authority; failures never fall back to SQL."""
    return OpenSearchDocumentRepository.from_environment(client=client)


DocumentRepository = OpenSearchDocumentRepository
