"""Verified document shadowing and explicit promotion; never switches aliases or flags."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.document_repository import OpenSearchDocumentRepository, external_member_reference
from llm_d_lens.benchmark_rag.publication_contracts import (
    PublicationConflictError,
    PublicationError,
    RegistryState,
    SourceHead,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN, SourceArchive, validate_approval
from llm_d_lens.db.dao.rag_publication import RagPublicationDao


@dataclass(frozen=True)
class DocumentMigrationReport:
    snapshot_sha256: str
    item_count: int
    origins: tuple[str, ...]
    cutover: bool = False
    structured_item_ids: tuple[str, ...] = ()
    cutover_blocked_reason: str | None = None
    sql_only_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentPromotionReport:
    snapshot_sha256: str
    inserted_items: int
    inserted_references: int
    installed_sources: tuple[str, ...]
    preserved_sources: tuple[str, ...]
    source_generations: tuple[tuple[str, int], ...]
    cutover: bool = False
    sql_only_origins: tuple[str, ...] = ()


def _validated_snapshot(snapshot: tuple[RagItem, ...]) -> dict[str, RagItem]:
    items = {item.item_id: RagItem.model_validate(item.model_dump()) for item in snapshot}
    if len(items) != len(snapshot):
        raise PublicationError("snapshot contains duplicate immutable identities")
    registries = {
        item.origin_id: item for item in items.values() if item.kind == "source" and item.source_role == "registry"
    }
    for item in items.values():
        if item.item_id != item_id(item.origin_id, item.logical_key, item.source_revision):
            raise PublicationError("snapshot immutable identity is invalid")
        registry = registries.get(item.origin_id)
        if registry is None or registry.item_id != registry_id(item.origin_id):
            raise PublicationError("snapshot source approval is missing")
        state = RegistryState.model_validate(registry.source_details)
        validate_approval(item.origin_id, state.approval)
        if registry.content_sha256 != content_digest(state.approval.model_dump(mode="json")):
            raise PublicationError("snapshot approval hash is invalid")
        if item.kind == "source" and item.source_role == "manifest":
            metadata = RagPublicationDao._manifest(item, item.origin_id, state.approval)
            RagPublicationDao._validate_member_map(item, metadata, state.approval, items)
        RagPublicationDao._validate_targets(item, items.get)
    return items


def shadow_copy_documents(
    repository: OpenSearchDocumentRepository,
    snapshot: tuple[RagItem, ...],
) -> DocumentMigrationReport:
    """Verify a SQL snapshot and copy documents plus external identities inertly.

    Source/manifest identities, hashes and membership remain unchanged. Structured
    fact bodies remain SQL-only; only their immutable membership proof is copied.
    This operation never installs a live source head or switches retrieval aliases.
    """
    items = _validated_snapshot(snapshot)
    ordered = tuple(items[key].model_dump(mode="json") for key in sorted(items))
    digest = content_digest(ordered)
    documents = {
        key: item
        for key, item in items.items()
        if item.kind in ("source", "passage") and item.origin_id != PRISM_ORIGIN
    }
    structured_ids = tuple(
        sorted(key for key, item in items.items() if key not in documents and item.origin_id != PRISM_ORIGIN)
    )
    references = {key: external_member_reference(items[key]) for key in structured_ids}
    with repository.publication_lock():
        # Preflight conflicts before any writes. Retrying an interrupted copy is safe.
        for item in documents.values():
            record = repository._read_record(f"shadow:{digest}:{item.item_id}")
            if record is not None and record[0] != item:
                raise PublicationError("conflicting immutable shadow snapshot")
        for key, value in references.items():
            existing = repository.read_record(f"shadow:{digest}:external:{key}")
            if existing is not None and existing[0] != value:
                raise PublicationError("conflicting immutable shadow external membership")
        for item in documents.values():
            key = f"shadow:{digest}:{item.item_id}"
            repository._create_immutable(item, key=key, refresh=False)
            copied = repository._read_record(key)
            if copied is None or copied[0] != item:
                raise PublicationError("shadow snapshot roundtrip verification failed")
        for key, value in references.items():
            repository.save_external_reference(value, namespace=f"shadow:{digest}:external", refresh=False)
            copied = repository.read_record(f"shadow:{digest}:external:{key}")
            if copied is None or copied[0] != value:
                raise PublicationError("shadow external membership verification failed")
        repository.refresh_management_index()
    return DocumentMigrationReport(
        snapshot_sha256=digest,
        item_count=len(documents),
        origins=tuple(sorted(item.origin_id for item in documents.values() if item.source_role == "registry")),
        structured_item_ids=structured_ids,
        sql_only_origins=(PRISM_ORIGIN,) if any(item.origin_id == PRISM_ORIGIN for item in items.values()) else (),
    )


def promote_documents(
    repository: OpenSearchDocumentRepository,
    snapshot: tuple[RagItem, ...],
    *,
    verified_snapshot_sha256: str,
    expected_heads: Mapping[str, SourceHead | None],
    archive: SourceArchive,
    legacy_writers_quiesced: bool = False,
    fresh_snapshot: tuple[RagItem, ...] | None = None,
) -> DocumentPromotionReport:
    """Explicitly replay a verified shadow without changing aliases or the flag.

    Missing sources inherit the baseline's exact approval, enabled state and
    generation. Existing target registries are never overwritten: current
    approvals, releases and revocations take precedence over a stale baseline.
    Callers must capture expected target heads before starting; concurrent drift
    aborts before writes. Repeating an already installed identical baseline is safe.
    Before obtaining ``fresh_snapshot``, operators must quiesce legacy writers and
    keep them quiesced until this call finishes. This function never stops writers
    or opens SQL; explicit confirmation and a matching fresh export are mandatory.
    """
    if legacy_writers_quiesced is not True:
        raise PublicationError("promotion requires explicit confirmation that legacy writers remain quiesced")
    if fresh_snapshot is None:
        raise PublicationError(
            "promotion requires a fresh authoritative export taken after legacy writers were quiesced"
        )
    items = _validated_snapshot(snapshot)
    digest = content_digest(tuple(items[key].model_dump(mode="json") for key in sorted(items)))
    if digest != verified_snapshot_sha256:
        raise PublicationError("promotion baseline does not match the verified snapshot digest")
    fresh_items = _validated_snapshot(fresh_snapshot)
    fresh_digest = content_digest(tuple(fresh_items[key].model_dump(mode="json") for key in sorted(fresh_items)))
    if fresh_digest != digest:
        raise PublicationConflictError(
            "legacy source export changed after shadow verification; rebuild the frozen baseline"
        )
    documents = {
        key: item
        for key, item in items.items()
        if item.kind in ("source", "passage") and item.origin_id != PRISM_ORIGIN
    }
    references = {
        key: external_member_reference(item)
        for key, item in items.items()
        if key not in documents and item.origin_id != PRISM_ORIGIN
    }
    registries = {item.origin_id: item for item in documents.values() if item.source_role == "registry"}
    if set(expected_heads) != set(registries):
        raise PublicationError("promotion requires an explicit expected target head for every source")
    with repository.publication_lock():
        for key, item in documents.items():
            shadow = repository._read_record(f"shadow:{digest}:{key}")
            if shadow is None or shadow[0] != item:
                raise PublicationError("promotion requires a complete verified shadow snapshot")
        for key, value in references.items():
            shadow = repository.read_record(f"shadow:{digest}:external:{key}")
            if shadow is None or shadow[0] != value:
                raise PublicationError("promotion external membership shadow is missing or differs")
        for sha256 in {item.content_sha256 for item in documents.values() if item.source_role == "file"}:
            if hashlib.sha256(archive.read(sha256)).hexdigest() != sha256:
                raise PublicationError("promotion source archive hash differs from the baseline")
        current = {}
        for origin, baseline in registries.items():
            target = repository.head(origin) if repository.get(baseline.item_id) is not None else None
            expected = expected_heads[origin]
            if target is not None and target.registry == baseline and expected is None:
                current[origin] = target
                continue
            if (target.registry if target else None) != (expected.registry if expected else None):
                raise PublicationConflictError(
                    "source changed since promotion preflight; capture current heads and retry"
                )
            current[origin] = target
        for key, item in documents.items():
            if item.source_role != "registry":
                existing = repository.get(key)
                if existing is not None and existing != item:
                    raise PublicationError("conflicting immutable document metadata during promotion")
        for key, value in references.items():
            existing = repository.read_record(f"external:{key}")
            if existing is not None and existing[0] != value:
                raise PublicationError("conflicting immutable external membership during promotion")
        inserted_items = 0
        inserted_references = 0
        for item in documents.values():
            if item.source_role != "registry" and repository.get(item.item_id) is None:
                repository._create_immutable(item, refresh=False)
                inserted_items += 1
        for key, value in references.items():
            if repository.read_record(f"external:{key}") is None:
                repository.save_external_reference(value, refresh=False)
                inserted_references += 1
        installed = []
        preserved = []
        for origin, baseline in sorted(registries.items()):
            if current[origin] is None:
                repository._create_immutable(baseline, refresh=False)
                inserted_items += 1
                installed.append(origin)
            else:
                preserved.append(origin)
        repository.refresh_management_index()
        return DocumentPromotionReport(
            snapshot_sha256=digest,
            inserted_items=inserted_items,
            inserted_references=inserted_references,
            installed_sources=tuple(installed),
            preserved_sources=tuple(preserved),
            source_generations=tuple(
                (origin, repository.head(origin).state.generation) for origin in sorted(registries)
            ),
            sql_only_origins=(PRISM_ORIGIN,) if any(item.origin_id == PRISM_ORIGIN for item in items.values()) else (),
        )
