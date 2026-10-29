"""Portable document evidence backups; restoration never grants publication authority.

Registry snapshots are provenance, not restore instructions. Existing approvals,
revocations and heads remain untouched; an empty target requires explicit source
registration and publication before rebuilding its retrieval index.
Shared manifests retain their original identities using separately checksummed
external membership proofs, never structured fact bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from llm_d_lens.benchmark_rag.contracts import ItemKind, RagItem
from llm_d_lens.benchmark_rag.document_migration import _validated_snapshot
from llm_d_lens.benchmark_rag.document_repository import (
    OpenSearchDocumentRepository,
    external_member_reference,
    validate_document_member_map,
    validate_external_reference,
)
from llm_d_lens.benchmark_rag.publication_contracts import (
    PublicationError,
    PublicationModel,
    RegistryState,
    ReleaseManifest,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN, SourceArchive, input_format, validate_approval
from llm_d_lens.benchmark_rag.transfer_contracts import RestoreResult
from llm_d_lens.db.dao.rag_publication import RagPublicationDao

MAX_BACKUP_BYTES = 1024 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_MEMBERS = MAX_RECORDS + 3
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class DocumentBackupManifest(PublicationModel):
    contract: Literal["benchmark-rag.documents.backup.v1"] = "benchmark-rag.documents.backup.v1"
    records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0, le=MAX_RECORDS, strict=True)
    counts: dict[ItemKind, Annotated[int, Field(ge=0, strict=True)]]
    blobs: dict[str, Annotated[int, Field(ge=0, strict=True)]]
    external_records_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    external_count: int = Field(default=0, ge=0, le=MAX_RECORDS, strict=True)

    @property
    def backup_id(self) -> str:
        payload = self.model_dump(mode="json")
        if self.external_records_sha256 is None and self.external_count == 0:
            del payload["external_records_sha256"]
            del payload["external_count"]
        return content_digest(payload)


def _required_references(records: tuple[RagItem, ...]) -> set[str]:
    documents = {item.item_id for item in records}
    required = set()
    for item in records:
        if item.source_role == "manifest":
            metadata = ReleaseManifest.model_validate(item.source_details)
            required.update((*metadata.member_ids, *metadata.removed_item_ids))
    return required - documents


def _validate_records(
    records: tuple[RagItem, ...],
    references: dict[str, dict[str, Any]],
) -> None:
    if any(item.origin_id == PRISM_ORIGIN for item in records) or any(
        reference.get("origin_id") == PRISM_ORIGIN for reference in references.values()
    ):
        raise PublicationError("Prism is SQL-owned and cannot enter document backup or restore")
    if any(item.kind not in ("source", "passage") for item in records):
        raise PublicationError("document backup contains structured fact bodies instead of external membership proofs")
    items = {item.item_id: item for item in records}
    if len(items) != len(records) or len(records) + len(references) > MAX_RECORDS:
        raise PublicationError("document backup must contain bounded unique records")
    if references.keys() != _required_references(records):
        raise PublicationError("document backup requires complete external membership proofs")
    for key, reference in references.items():
        validate_external_reference(key, reference)
    states = {}
    for item in records:
        if item.item_id != item_id(item.origin_id, item.logical_key, item.source_revision):
            raise PublicationError("document backup immutable identity is invalid")
        if item.source_role == "registry":
            state = RegistryState.model_validate(item.source_details)
            validate_approval(item.origin_id, state.approval)
            if (
                item.item_id != registry_id(item.origin_id)
                or item.parent_id is not None
                or item.content_sha256 != content_digest(state.approval.model_dump(mode="json"))
            ):
                raise PublicationError("document backup approval identity is invalid")
            states[item.origin_id] = state
    for item in records:
        if item.origin_id not in states:
            raise PublicationError("document backup is missing source provenance")
        approval = states[item.origin_id].approval
        if item.source_role != "manifest":
            RagPublicationDao._validate_targets(item, items.get)
        elif item.parent_id != registry_id(item.origin_id):
            raise PublicationError("document manifest source registry is invalid")
        if item.source_role == "file" and (
            item.parent_id != registry_id(item.origin_id)
            or input_format(item.origin_id, item.source_path, approval) != item.format
        ):
            raise PublicationError("document backup source file is not approved")
        if item.source_role == "manifest":
            metadata = RagPublicationDao._manifest(item, item.origin_id, approval)
            validate_document_member_map(
                item,
                metadata,
                approval,
                items,
                {key: references[key] for key in metadata.member_ids if key in references},
            )
            for key in (*metadata.removed_item_ids, metadata.base_manifest_id):
                if key is not None and (
                    (key not in items and key not in references)
                    or (items[key].origin_id if key in items else references[key]["origin_id"]) != item.origin_id
                ):
                    raise PublicationError("document backup is missing retained release history")
            if metadata.base_manifest_id is not None and (
                metadata.base_manifest_id not in items or items[metadata.base_manifest_id].source_role != "manifest"
            ):
                raise PublicationError("document backup base release is not a manifest")


class DocumentBackupService:
    """Back up metadata and content without SQL, aliases or active build pointers."""

    def __init__(
        self,
        repository: OpenSearchDocumentRepository,
        archive: SourceArchive | None = None,
    ) -> None:
        self.repository = repository
        self.archive = archive if archive is not None else SourceArchive()

    def backup(
        self,
        output: Path,
        *,
        max_bytes: int = MAX_BACKUP_BYTES,
        snapshot: tuple[RagItem, ...] | None = None,
    ) -> DocumentBackupManifest:
        """Export OpenSearch metadata or an explicitly supplied migration baseline.

        A migration caller supplies one consistent, complete snapshot, validated
        in memory before projecting structured bodies into membership proofs.
        SQL-owned Prism records are excluded only after full baseline validation.
        This service never opens SQL or selects authority from environment flags;
        shadow migration remains distinct from operational cutover.
        """
        if max_bytes <= 0:
            raise PublicationError("backup size limit must be positive")
        with self.repository.publication_lock():
            if snapshot is None:
                records = self.repository.snapshot(max_records=MAX_RECORDS, max_bytes=max_bytes)
                references = {}
                required = _required_references(records)
                if len(records) + len(required) > MAX_RECORDS:
                    raise PublicationError("document backup contains too many records")
                reference_bytes = 0
                for key in sorted(required):
                    record = self.repository.read_record(f"external:{key}")
                    if record is None:
                        raise PublicationError("document backup requires complete external membership proofs")
                    reference_bytes += len(json.dumps(record[0], sort_keys=True, separators=(",", ":")).encode()) + 1
                    if reference_bytes > max_bytes:
                        raise PublicationError("document backup exceeds the uncompressed size limit")
                    references[key] = record[0]
            else:
                if len(snapshot) > MAX_RECORDS:
                    raise PublicationError("document backup contains too many records")
                validated = _validated_snapshot(snapshot)
                records = tuple(
                    item
                    for item in validated.values()
                    if item.kind in ("source", "passage") and item.origin_id != PRISM_ORIGIN
                )
                if validated and not records:
                    raise PublicationError("SQL baseline has no OpenSearch-owned document sources")
                references = {key: external_member_reference(validated[key]) for key in _required_references(records)}
            records = tuple(sorted(records, key=lambda item: item.item_id))
            _validate_records(records, references)
            chunks = []
            used = 0
            for item in records:
                chunk = json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode() + b"\n"
                used += len(chunk)
                if used > max_bytes:
                    raise PublicationError("document backup exceeds the uncompressed size limit")
                chunks.append(chunk)
            payload = b"".join(chunks)
            external_chunks = []
            for key in sorted(references):
                chunk = json.dumps(references[key], sort_keys=True, separators=(",", ":")).encode() + b"\n"
                used += len(chunk)
                if used > max_bytes:
                    raise PublicationError("document backup exceeds the uncompressed size limit")
                external_chunks.append(chunk)
            external_payload = b"".join(external_chunks)
            blobs = {}
            for digest in sorted({item.content_sha256 for item in records if item.source_role == "file"}):
                data = self.archive.read(digest)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise PublicationError("document source checksum mismatch")
                used += len(data)
                if used > max_bytes:
                    raise PublicationError("document backup exceeds the uncompressed size limit")
                blobs[digest] = data
            metadata = DocumentBackupManifest(
                records_sha256=hashlib.sha256(payload).hexdigest(),
                row_count=len(records),
                counts=dict(Counter(item.kind for item in records)),
                blobs={digest: len(data) for digest, data in blobs.items()},
                external_records_sha256=hashlib.sha256(external_payload).hexdigest(),
                external_count=len(references),
            )
            encoded = metadata.model_dump_json().encode()
            if len(encoded) > MAX_MANIFEST_BYTES or used + len(encoded) > max_bytes:
                raise PublicationError("document backup exceeds the uncompressed size limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=output.parent, prefix=".document-backup-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("manifest.json", encoded)
                bundle.writestr("records.jsonl", payload)
                bundle.writestr("external_records.jsonl", external_payload)
                for digest, data in blobs.items():
                    bundle.writestr(f"sources/{digest}", data)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temporary, output)
            descriptor = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata

    @staticmethod
    def _read(
        path: Path,
        max_bytes: int,
    ) -> tuple[DocumentBackupManifest, tuple[RagItem, ...], dict[str, bytes], dict[str, dict[str, Any]]]:
        if max_bytes <= 0:
            raise PublicationError("backup size limit must be positive")
        try:
            with zipfile.ZipFile(path) as bundle:
                entries = bundle.infolist()
                names = [entry.filename for entry in entries]
                if len(entries) > MAX_MEMBERS or len(names) != len(set(names)):
                    raise PublicationError("document backup has duplicate or too many members")
                if sum(entry.file_size for entry in entries) > max_bytes:
                    raise PublicationError("document backup exceeds the uncompressed size limit")
                if any(
                    entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or not (
                        entry.filename in ("manifest.json", "records.jsonl", "external_records.jsonl")
                        or re.fullmatch(r"sources/[0-9a-f]{64}", entry.filename)
                    )
                    for entry in entries
                ):
                    raise PublicationError("document backup has an unsupported or unsafe member")
                if "manifest.json" not in names or "records.jsonl" not in names:
                    raise PublicationError("document backup is missing its manifest or records")
                if bundle.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise PublicationError("document backup manifest is too large")
                metadata = DocumentBackupManifest.model_validate_json(bundle.read("manifest.json"))
                if metadata.external_records_sha256 is None and metadata.external_count:
                    raise PublicationError("document backup external membership checksum is missing")
                if any(
                    not re.fullmatch(r"[0-9a-f]{64}", digest) or type(size) is not int or size < 0
                    for digest, size in metadata.blobs.items()
                ):
                    raise PublicationError("document backup blob inventory is invalid")
                if set(names) != {
                    "manifest.json",
                    "records.jsonl",
                    *(("external_records.jsonl",) if metadata.external_records_sha256 is not None else ()),
                    *(f"sources/{digest}" for digest in metadata.blobs),
                }:
                    raise PublicationError("document backup member inventory differs from its manifest")
                payload = bundle.read("records.jsonl")
                if hashlib.sha256(payload).hexdigest() != metadata.records_sha256:
                    raise PublicationError("document backup record checksum mismatch")
                lines = payload.splitlines()
                if len(lines) > MAX_RECORDS:
                    raise PublicationError("document backup contains too many records")
                records = tuple(RagItem.model_validate_json(line) for line in lines)
                if (
                    len(records) != metadata.row_count
                    or dict(Counter(item.kind for item in records)) != metadata.counts
                ):
                    raise PublicationError("document backup record counts are invalid")
                references = {}
                if metadata.external_records_sha256 is not None:
                    external_payload = bundle.read("external_records.jsonl")
                    if hashlib.sha256(external_payload).hexdigest() != metadata.external_records_sha256:
                        raise PublicationError("document backup external membership checksum mismatch")
                    external_lines = external_payload.splitlines()
                    if (
                        len(external_lines) != metadata.external_count
                        or len(records) + len(external_lines) > MAX_RECORDS
                    ):
                        raise PublicationError("document backup external membership count is invalid")
                    for line in external_lines:
                        reference = json.loads(line)
                        if not isinstance(reference, dict) or not isinstance(reference.get("item_id"), str):
                            raise PublicationError("document backup external membership is invalid")
                        key = reference["item_id"]
                        if key in references:
                            raise PublicationError("document backup has duplicate external members")
                        references[key] = reference
                _validate_records(records, references)
                if {item.content_sha256 for item in records if item.source_role == "file"} != metadata.blobs.keys():
                    raise PublicationError("document backup evidence inventory is incomplete")
                blobs = {}
                for digest, size in metadata.blobs.items():
                    data = bundle.read(f"sources/{digest}")
                    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                        raise PublicationError("document backup source checksum mismatch")
                    blobs[digest] = data
                return metadata, records, blobs, references
        except zipfile.BadZipFile as error:
            raise PublicationError("invalid or corrupt document backup ZIP") from error
        except (ValidationError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PublicationError("invalid document backup metadata") from error

    @classmethod
    def verify(cls, path: Path, *, max_bytes: int = MAX_BACKUP_BYTES) -> DocumentBackupManifest:
        return cls._read(path, max_bytes)[0]

    def restore(self, path: Path, *, max_bytes: int = MAX_BACKUP_BYTES) -> RestoreResult:
        metadata, records, blobs, references = self._read(path, max_bytes)
        immutable = tuple(item for item in records if item.source_role != "registry")
        with self.repository.publication_lock():
            # Preflight every conflict before writing any evidence or metadata.
            for item in immutable:
                existing = self.repository.get(item.item_id)
                if existing is not None and existing != item:
                    raise PublicationError("conflicting immutable document metadata")
            existing_references = {}
            for key, reference in references.items():
                existing = self.repository.read_record(f"external:{key}")
                if existing is not None and existing[0] != reference:
                    raise PublicationError("conflicting immutable external membership")
                existing_references[key] = existing
            for digest, data in blobs.items():
                if self.archive.put(data) != digest or self.archive.read(digest) != data:
                    raise PublicationError("restored source archive identity differs")
            inserted = 0
            for key, reference in references.items():
                if existing_references[key] is None:
                    self.repository.save_external_reference(reference, refresh=False)
                    inserted += 1
            for item in immutable:
                if self.repository.get(item.item_id) is None:
                    self.repository._create_immutable(item, refresh=False)
                    inserted += 1
            if inserted:
                self.repository.refresh_management_index()
        return RestoreResult(
            backup_id=metadata.backup_id,
            inserted=inserted,
            updated_sources=0,
            total_rows=len(records) + len(references),
        )
