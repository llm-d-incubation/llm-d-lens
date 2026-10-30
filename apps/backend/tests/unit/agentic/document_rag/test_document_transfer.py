"""Portable document restores must never restore stale publication authority."""

import copy
import hashlib
import json
import stat
import zipfile
from collections import Counter
from contextlib import contextmanager
from unittest.mock import Mock
from urllib.parse import unquote

import httpx
import pytest
from pydantic import ValidationError

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord
from llm_d_lens.agentic.document_rag.document_repository import (
    MANAGEMENT_INDEX,
    DocumentRepository,
    OpenSearchDocumentRepository,
    external_member_reference,
)
from llm_d_lens.agentic.document_rag.document_transfer import DocumentBackupService
from llm_d_lens.agentic.document_rag.publication import RagPublicationService
from llm_d_lens.agentic.document_rag.publication_contracts import (
    InventoryFile,
    ManifestFile,
    ParsedFile,
    PinnedInventory,
    PublicationError,
    RegistryState,
    ReleaseManifest,
    SourceApproval,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.agentic.document_rag.repository_corpus import parse_markdown
from llm_d_lens.agentic.document_rag.runtime import (
    RuntimePolicy,
    RuntimeUpdate,
    load_runtime_policy,
    update_runtime_policy,
)
from llm_d_lens.agentic.document_rag.sources import BENCHMARK_ORIGIN, PRISM_ORIGIN, SourceArchive
from llm_d_lens.agentic.public_benchmark_record.contracts import PublicBenchmarkRecord
from llm_d_lens.db.dao.public_benchmark_record import PublicBenchmarkRecordDao


class Repository:
    def __init__(self, records=(), references=()):
        self.items = {item.item_id: item for item in records}
        self.references = {value["item_id"]: value for value in references}
        self.locked = False
        self.writes = []
        self.refreshes = 0

    @contextmanager
    def publication_lock(self):
        assert not self.locked
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def snapshot(self, *, max_records, max_bytes):
        assert self.locked
        assert 0 < max_records <= 100_000
        assert max_bytes > 0
        return tuple(self.items.values())

    def get(self, key):
        assert self.locked
        return self.items.get(key)

    def _create_immutable(self, item, *, refresh=True):
        assert self.locked
        assert item.source_role != "registry"
        assert item.item_id not in self.items
        self.items[item.item_id] = item
        self.writes.append(item.item_id)

    def read_record(self, key):
        assert self.locked
        assert key.startswith("external:")
        reference = self.references.get(key.removeprefix("external:"))
        return (reference, 0, 1) if reference is not None else None

    def save_external_reference(self, value, *, namespace="external", refresh=True):
        assert self.locked
        self.references[value["item_id"]] = value

    def refresh_management_index(self):
        assert self.locked
        self.refreshes += 1


def projected_repository(snapshot):
    documents = tuple(item for item in snapshot if item.kind in ("source", "passage"))
    references = tuple(external_member_reference(item) for item in snapshot if item.kind not in ("source", "passage"))
    return Repository(documents, references)


@pytest.fixture
def document_case(monkeypatch, tmp_path):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    archive = SourceArchive()
    data = b"# Public metrics\n\nTime to first token.\n"
    digest = archive.put(data)
    approval = SourceApproval(
        verified_public=True,
        approved_by="reviewer",
        approval_reference="review:1",
        license_reference="Apache-2.0",
        parser_version="test-v1",
        allowed_paths=("docs/**/*.md",),
    )
    origin = BENCHMARK_ORIGIN
    state = RegistryState(approval=approval)
    registry = DocumentRecord(
        item_id=registry_id(origin),
        kind="source",
        source_role="registry",
        origin_id=origin,
        logical_key="@registry",
        source_revision="registry-v1",
        content_sha256=content_digest(approval.model_dump(mode="json")),
        access_classification="verified_public",
        source_path="@registry",
        format="json",
        source_details=state.model_dump(mode="json"),
    )
    file = InventoryFile(path="docs/metrics.md", sha256=digest, format="markdown")
    inventory = PinnedInventory(origin_id=origin, revision="a" * 40, complete=True, files=(file,))
    key = "file:docs/metrics.md"
    source = DocumentRecord(
        item_id=item_id(origin, key, inventory.revision),
        kind="source",
        source_role="file",
        origin_id=origin,
        logical_key=key,
        source_revision=inventory.revision,
        content_sha256=digest,
        parent_id=registry.item_id,
        access_classification="verified_public",
        source_path=file.path,
        format=file.format,
        source_details={"parser_version": approval.parser_version},
    )
    passages = parse_markdown(file, inventory, data)
    members = (source, *passages)
    metadata = ReleaseManifest(
        inventory=inventory,
        parser_version=approval.parser_version,
        base_manifest_id=None,
        base_generation=0,
        files=(
            ManifestFile(
                file=file,
                file_item_id=source.item_id,
                member_ids=tuple(sorted(item.item_id for item in members)),
            ),
        ),
        counts=dict(Counter(item.kind for item in members)),
        removed_item_ids=(),
    )
    digest = content_digest(metadata.model_dump(mode="json"))
    key = f"@manifest:{digest}"
    manifest = DocumentRecord(
        item_id=item_id(origin, key, inventory.revision),
        kind="source",
        source_role="manifest",
        origin_id=origin,
        logical_key=key,
        source_revision=inventory.revision,
        content_sha256=digest,
        parent_id=registry.item_id,
        access_classification="verified_public",
        source_path=f"@manifests/{digest}",
        format="json",
        source_details=metadata.model_dump(mode="json"),
        manifest_item_ids=list(metadata.member_ids),
    )
    registry = registry.model_copy(update={"published_manifest_id": manifest.item_id})
    return archive, (registry, *members, manifest)


@pytest.fixture
def mixed_document_case(document_case):
    archive, records = document_case
    source = records[1]
    common = {
        "origin_id": source.origin_id,
        "source_revision": source.source_revision,
        "content_sha256": source.content_sha256,
        "access_classification": "verified_public",
        "source_path": source.source_path,
        "format": source.format,
        "source_pointer": {"table": 1},
    }
    key = f"{source.source_path}#experiment"
    experiment = PublicBenchmarkRecord(
        **common,
        item_id=item_id(source.origin_id, key, source.source_revision),
        kind="experiment",
        logical_key=key,
        parent_id=source.item_id,
        model="sql-only-model",
        hardware="sql-only-hardware",
        configuration={"private": "sql-only-config"},
    )
    key = f"{source.source_path}#measurement"
    measurement = PublicBenchmarkRecord(
        **common,
        item_id=item_id(source.origin_id, key, source.source_revision),
        kind="measurement",
        logical_key=key,
        parent_id=experiment.item_id,
        experiment_id=experiment.item_id,
        metric="ttft",
        value="123.45",
        original_value="123.45",
        unit="ms",
        original_unit="ms",
        evidence_type="published_table",
    )
    members = (*records[1:-1], experiment, measurement)
    original = ReleaseManifest.model_validate(records[-1].source_details)
    details = original.model_copy(
        update={
            "files": (
                original.files[0].model_copy(update={"member_ids": tuple(sorted(item.item_id for item in members))}),
            ),
            "counts": dict(Counter(item.kind for item in members)),
        }
    ).model_dump(mode="json")
    digest = content_digest(details)
    key = f"@manifest:{digest}"
    manifest = records[-1].model_copy(
        update={
            "item_id": item_id(source.origin_id, key, source.source_revision),
            "logical_key": key,
            "content_sha256": digest,
            "source_details": details,
            "manifest_item_ids": sorted(item.item_id for item in members),
            "source_path": f"@manifests/{digest}",
        }
    )
    registry = records[0].model_copy(update={"published_manifest_id": manifest.item_id})
    return archive, (registry, *members, manifest)


@pytest.fixture
def prism_sql_records(document_case):
    _, records = document_case
    approval = RegistryState.model_validate(records[0].source_details).approval.model_copy(
        update={"allowed_paths": ("archives/archived_drive_data.json",)},
    )
    registry = records[0].model_copy(
        update={
            "item_id": registry_id(PRISM_ORIGIN),
            "origin_id": PRISM_ORIGIN,
            "published_manifest_id": None,
            "content_sha256": content_digest(approval.model_dump(mode="json")),
            "source_details": RegistryState(approval=approval).model_dump(mode="json"),
        }
    )
    key, revision = "file:archives/archived_drive_data.json", "2026-01-01"
    source = DocumentRecord(
        item_id=item_id(PRISM_ORIGIN, key, revision),
        kind="source",
        source_role="file",
        origin_id=PRISM_ORIGIN,
        logical_key=key,
        source_revision=revision,
        content_sha256="f" * 64,
        parent_id=registry.item_id,
        access_classification="verified_public",
        source_path="archives/archived_drive_data.json",
        format="json",
        source_details={"parser_version": approval.parser_version},
    )
    key = f"{source.source_path}#prism-experiment"
    experiment = PublicBenchmarkRecord(
        item_id=item_id(PRISM_ORIGIN, key, revision),
        kind="experiment",
        origin_id=PRISM_ORIGIN,
        logical_key=key,
        source_revision=revision,
        content_sha256=source.content_sha256,
        parent_id=source.item_id,
        access_classification="verified_public",
        source_path=source.source_path,
        format="json",
        source_pointer={"pointer": "/experiment"},
        model="prism-sql-only-model",
    )
    return registry, source, experiment


def rewrite(path, transform):
    with zipfile.ZipFile(path) as bundle:
        entries = [(entry, bundle.read(entry)) for entry in bundle.infolist()]
    entries = transform(entries)
    with zipfile.ZipFile(path, "w") as bundle:
        for entry, data in entries:
            bundle.writestr(entry, data)


def test_backup_verify_restore_is_sql_free_and_requires_explicit_reapproval(document_case, tmp_path, monkeypatch):
    archive, records = document_case
    rollout = update_runtime_policy(
        RuntimeUpdate(expected_generation=0, agentic_mode="enabled", agentic_user_ids=("approved-reader",))
    )
    assert load_runtime_policy() == rollout
    output = tmp_path / "documents.zip"
    metadata = DocumentBackupService(Repository(records), archive).backup(output)
    assert DocumentBackupService.verify(output) == metadata
    with zipfile.ZipFile(output) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json",
            "records.jsonl",
            "external_records.jsonl",
            f"sources/{records[1].content_sha256}",
        }
    target = Repository()
    expected = archive.read(records[1].content_sha256)
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "recovered"))
    destination_archive = Mock(wraps=SourceArchive())
    service = DocumentBackupService(target, destination_archive)
    result = service.restore(output)
    assert load_runtime_policy() == RuntimePolicy()
    assert load_runtime_policy().agentic_mode == "disabled"
    assert result.inserted == len(records) - 1
    assert result.updated_sources == 0
    assert records[0].item_id not in target.items
    assert {item.item_id for item in records[1:]} == target.items.keys()
    assert service.restore(output).inserted == 0
    assert target.refreshes == 1
    destination_archive.read.assert_called()
    assert destination_archive.put.call_args.args[0] == expected
    assert destination_archive.read(records[1].content_sha256) == expected
    inventory = ReleaseManifest.model_validate(records[-1].source_details).inventory
    assert parse_markdown(inventory.files[0], inventory, expected) == tuple(
        item for item in records if item.kind == "passage"
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_stale_backup_retains_current_revocation_approval_and_head(document_case, tmp_path, enabled):
    archive, records = document_case
    output = tmp_path / "stale.zip"
    DocumentBackupService(Repository(records), archive).backup(output)
    state = RegistryState.model_validate(records[0].source_details)
    changed_approval = state.approval.model_copy(update={"approval_reference": "new-review"})
    current_state = RegistryState(approval=changed_approval, enabled=enabled, generation=9)
    current = records[0].model_copy(
        update={
            "published_manifest_id": None,
            "source_details": current_state.model_dump(mode="json"),
            "content_sha256": content_digest(changed_approval.model_dump(mode="json")),
        }
    )
    target = Repository((current,))
    DocumentBackupService(target, archive).restore(output)
    assert target.items[current.item_id] == current
    assert current.item_id not in target.writes


def test_immutable_conflict_preflight_precedes_all_writes(document_case, tmp_path):
    archive, records = document_case
    path = tmp_path / "conflict.zip"
    DocumentBackupService(Repository(records), archive).backup(path)
    conflicting = records[1].model_copy(update={"format": "json"})
    target = Repository((conflicting,))
    target_archive = Mock()
    with pytest.raises(PublicationError, match="conflicting immutable"):
        DocumentBackupService(target, target_archive).restore(path)
    assert not target.writes
    target_archive.put.assert_not_called()


def test_interrupted_import_is_inert_and_retryable(document_case, tmp_path):
    archive, records = document_case
    path = tmp_path / "retry.zip"
    DocumentBackupService(Repository(records), archive).backup(path)
    target = Repository()
    create = target._create_immutable
    attempts = 0

    def interrupted(item, *, refresh=True):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise PublicationError("interrupted OpenSearch write")
        create(item, refresh=refresh)

    target._create_immutable = interrupted
    service = DocumentBackupService(target, archive)
    with pytest.raises(PublicationError, match="interrupted"):
        service.restore(path)
    assert len(target.items) == 1
    assert records[0].item_id not in target.items
    target._create_immutable = create
    assert service.restore(path).inserted == len(records) - 2
    assert records[0].item_id not in target.items


def test_source_archive_corruption_prevents_backup(document_case, tmp_path):
    archive, records = document_case
    source = archive._root(records[1].content_sha256) / "source"
    source.write_bytes(b"corrupted")
    output = tmp_path / "corrupt-source.zip"
    with pytest.raises((PublicationError, ValueError)):
        DocumentBackupService(Repository(records), archive).backup(output)
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["records", "blob", "manifest", "missing", "extra", "duplicate", "symlink"])
def test_tampered_archives_are_rejected_before_mutation(document_case, tmp_path, mutation):
    archive, records = document_case
    path = tmp_path / f"{mutation}.zip"
    DocumentBackupService(Repository(records), archive).backup(path)

    def mutate(entries):
        if mutation == "records":
            return [(entry, b"{}\n" if entry.filename == "records.jsonl" else data) for entry, data in entries]
        if mutation == "blob":
            return [(entry, b"tampered" if entry.filename.startswith("sources/") else data) for entry, data in entries]
        if mutation == "manifest":
            return [(entry, b"{}" if entry.filename == "manifest.json" else data) for entry, data in entries]
        if mutation == "missing":
            return [(entry, data) for entry, data in entries if not entry.filename.startswith("sources/")]
        if mutation == "extra":
            return [*entries, (zipfile.ZipInfo("../escape"), b"bad")]
        if mutation == "duplicate":
            return [*entries, entries[0]]
        entry = copy.copy(entries[-1][0])
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        return [*entries[:-1], (entry, entries[-1][1])]

    if mutation == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate"):
            rewrite(path, mutate)
    else:
        rewrite(path, mutate)
    target, destination = Repository(), Mock()
    with pytest.raises(PublicationError):
        DocumentBackupService(target, destination).restore(path)
    assert not target.writes
    destination.put.assert_not_called()


@pytest.mark.parametrize("max_bytes", [0, -1, 1])
def test_size_bounds_cover_backup_and_restore(document_case, tmp_path, max_bytes):
    archive, records = document_case
    service = DocumentBackupService(Repository(records), archive)
    path = tmp_path / "bounded.zip"
    with pytest.raises(PublicationError, match="limit"):
        service.backup(path, max_bytes=max_bytes)
    assert not path.exists()
    service.backup(path)
    with pytest.raises(PublicationError, match="limit"):
        service.restore(path, max_bytes=max_bytes)


def test_output_is_immutable_and_scratch_is_cleaned(document_case, tmp_path):
    archive, records = document_case
    path = tmp_path / "existing.zip"
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        DocumentBackupService(Repository(records), archive).backup(path)
    assert path.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".document-backup-*"))


def test_corrupt_archive_and_unknown_contract_fail_closed(tmp_path):
    path = tmp_path / "bad.zip"
    path.write_bytes(b"not a ZIP")
    with pytest.raises(PublicationError, match="ZIP"):
        DocumentBackupService.verify(path)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"contract": "benchmark-rag.backup.v1"}))
        bundle.writestr("records.jsonl", b"")
    with pytest.raises(PublicationError, match="metadata"):
        DocumentBackupService.verify(path)


@pytest.mark.parametrize("mutation", ["identity", "missing_registry", "counts", "blob_size", "contract"])
def test_rechecksummed_malformed_metadata_fails_closed(document_case, tmp_path, mutation):
    archive, records = document_case
    path = tmp_path / "malformed.zip"
    DocumentBackupService(Repository(records), archive).backup(path)

    def mutate(entries):
        contents = {entry.filename: data for entry, data in entries}
        manifest = json.loads(contents["manifest.json"])
        rows = [json.loads(line) for line in contents["records.jsonl"].splitlines()]
        if mutation == "identity":
            rows[0]["item_id"] = "0" * 64
        elif mutation == "missing_registry":
            rows = [row for row in rows if row["source_role"] != "registry"]
            manifest["row_count"] = len(rows)
            manifest["counts"] = dict(Counter(row["kind"] for row in rows))
        elif mutation == "counts":
            manifest["counts"]["passage"] += 1
        elif mutation == "blob_size":
            manifest["blobs"][next(iter(manifest["blobs"]))] += 1
        elif mutation == "contract":
            manifest["contract"] = "benchmark-rag.documents.backup.v99"
        payload = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        manifest["records_sha256"] = hashlib.sha256(payload).hexdigest()
        contents["records.jsonl"] = payload
        contents["manifest.json"] = json.dumps(manifest).encode()
        return [(entry, contents[entry.filename]) for entry, _ in entries]

    rewrite(path, mutate)
    with pytest.raises(PublicationError):
        DocumentBackupService.verify(path)


@pytest.mark.parametrize("kind", ["experiment", "measurement", "figure", "figure_link"])
@pytest.mark.parametrize("declare_kind", [False, True])
def test_mixed_legacy_bundles_never_import_structured_fact_bodies(document_case, tmp_path, kind, declare_kind):
    archive, records = document_case
    source = records[1]
    fields = {
        "experiment": {},
        "measurement": {
            "experiment_id": source.item_id,
            "metric": "ttft",
            "value": "1",
            "original_value": "1",
            "evidence_type": "raw_json",
        },
        "figure": {"coverage": "complete"},
        "figure_link": {"referenced_item_id": source.item_id},
    }
    key = f"{source.source_path}#legacy-{kind}"
    structured = PublicBenchmarkRecord(
        item_id=item_id(source.origin_id, key, source.source_revision),
        kind=kind,
        origin_id=source.origin_id,
        logical_key=key,
        source_revision=source.source_revision,
        content_sha256=source.content_sha256,
        parent_id=source.item_id,
        access_classification="verified_public",
        source_path=source.source_path,
        format=source.format,
        source_pointer={},
        **fields[kind],
    )
    path = tmp_path / "legacy-mixed.zip"
    service = DocumentBackupService(Repository(records), archive)
    service.backup(path)

    def append_legacy_body(entries):
        contents = {entry.filename: data for entry, data in entries}
        payload = contents["records.jsonl"] + structured.model_dump_json().encode() + b"\n"
        manifest = json.loads(contents["manifest.json"])
        manifest["records_sha256"] = hashlib.sha256(payload).hexdigest()
        manifest["row_count"] += 1
        if declare_kind:
            manifest["counts"][kind] = 1
        contents["records.jsonl"] = payload
        contents["manifest.json"] = json.dumps(manifest).encode()
        return [(entry, contents[entry.filename]) for entry, _ in entries]

    rewrite(path, append_legacy_body)
    target, target_archive = Repository(), Mock()
    with pytest.raises(PublicationError, match="invalid document backup metadata") as error:
        DocumentBackupService(target, target_archive).restore(path)
    assert isinstance(error.value.__cause__, ValidationError)
    assert any(issue["type"] == "literal_error" for issue in error.value.__cause__.errors())
    assert not target.writes
    target_archive.put.assert_not_called()


def test_manifest_without_external_proofs_rejects_inconsistent_kind_counts(document_case, tmp_path):
    archive, records = document_case
    details = (
        ReleaseManifest.model_validate(records[-1].source_details)
        .model_copy(
            update={"counts": {"source": 1, "measurement": 1}},
        )
        .model_dump(mode="json")
    )
    digest = content_digest(details)
    key = f"@manifest:{digest}"
    mixed_manifest = records[-1].model_copy(
        update={
            "source_details": details,
            "content_sha256": digest,
            "logical_key": key,
            "item_id": item_id(records[-1].origin_id, key, records[-1].source_revision),
            "source_path": f"@manifests/{digest}",
        }
    )
    registry = records[0].model_copy(update={"published_manifest_id": mixed_manifest.item_id})
    snapshot = (registry, *records[1:-1], mixed_manifest)
    with pytest.raises(PublicationError, match="counts"):
        DocumentBackupService(Repository(snapshot), archive).backup(tmp_path / "mixed-manifest.zip")


def test_mixed_baseline_projects_external_proofs_and_preserves_original_manifest(mixed_document_case, tmp_path):
    archive, snapshot = mixed_document_case
    path = tmp_path / "mixed-baseline.zip"
    manifest = DocumentBackupService(projected_repository(snapshot), archive).backup(path)
    assert manifest.external_count == 2
    assert manifest.row_count == len(snapshot) - 2
    with zipfile.ZipFile(path) as bundle:
        records = tuple(DocumentRecord.model_validate_json(line) for line in bundle.read("records.jsonl").splitlines())
        references = tuple(json.loads(line) for line in bundle.read("external_records.jsonl").splitlines())
        uncompressed = b"".join(bundle.read(entry) for entry in bundle.infolist())
    assert all(item.kind in ("source", "passage") for item in records)
    assert next(item for item in records if item.source_role == "manifest") == snapshot[-1]
    assert next(item for item in records if item.source_role == "registry") == snapshot[0]
    assert all(
        not {"model", "hardware", "configuration", "workload", "value", "source_pointer"}.intersection(reference)
        for reference in references
    )
    assert all(secret not in uncompressed for secret in (b"sql-only-model", b"sql-only-config", b"123.45"))
    projected = Repository(records, references)
    assert DocumentBackupService(projected, archive).backup(tmp_path / "projected.zip") == manifest
    target = Repository()
    result = DocumentBackupService(target, archive).restore(path)
    assert result.inserted == len(snapshot) - 1
    assert snapshot[0].item_id not in target.items
    assert target.items[snapshot[-1].item_id] == snapshot[-1]
    assert target.references == {reference["item_id"]: reference for reference in references}


def test_prism_only_baseline_and_legacy_prism_restore_fail_clearly(document_case, prism_sql_records, tmp_path):
    archive, records = document_case
    path = tmp_path / "prism-baseline.zip"
    with pytest.raises(ValidationError, match="kind"):
        DocumentBackupService(Repository(), archive).backup(path, snapshot=prism_sql_records)
    assert not path.exists()
    DocumentBackupService(Repository(records), archive).backup(path)

    def legacy_prism(entries):
        contents = {entry.filename: data for entry, data in entries}
        payload = prism_sql_records[0].model_dump_json().encode() + b"\n"
        metadata = json.loads(contents["manifest.json"])
        metadata["records_sha256"] = hashlib.sha256(payload).hexdigest()
        metadata["row_count"] = 1
        metadata["counts"] = {"source": 1}
        contents["records.jsonl"] = payload
        contents["manifest.json"] = json.dumps(metadata).encode()
        return [(entry, contents[entry.filename]) for entry, _ in entries]

    rewrite(path, legacy_prism)
    target, target_archive = Repository(), Mock()
    with pytest.raises(PublicationError, match="Prism is SQL-owned"):
        DocumentBackupService(target, target_archive).restore(path)
    assert not target.writes
    target_archive.put.assert_not_called()


def test_external_proof_conflict_preflight_precedes_archive_or_record_writes(mixed_document_case, tmp_path):
    archive, snapshot = mixed_document_case
    path = tmp_path / "conflict-proofs.zip"
    DocumentBackupService(projected_repository(snapshot), archive).backup(path)
    structured = next(item for item in snapshot if item.kind == "experiment")
    reference = external_member_reference(structured)
    reference["payload_sha256"] = "0" * 64
    target = Repository(references=(reference,))
    destination_archive = Mock()
    with pytest.raises(PublicationError, match="conflicting immutable external"):
        DocumentBackupService(target, destination_archive).restore(path)
    assert not target.writes
    destination_archive.put.assert_not_called()


@pytest.mark.parametrize("mutation", ["checksum", "missing", "duplicate", "private", "dependency", "kind", "legacy"])
def test_invalid_external_proofs_fail_before_any_restore_writes(mixed_document_case, tmp_path, mutation):
    archive, snapshot = mixed_document_case
    path = tmp_path / "malformed-external.zip"
    DocumentBackupService(projected_repository(snapshot), archive).backup(path)

    def mutate(entries):
        contents = {entry.filename: data for entry, data in entries}
        metadata = json.loads(contents["manifest.json"])
        references = [json.loads(line) for line in contents["external_records.jsonl"].splitlines()]
        if mutation == "legacy":
            del contents["external_records.jsonl"]
            del metadata["external_records_sha256"]
            del metadata["external_count"]
        else:
            if mutation in ("checksum", "private"):
                references[0]["configuration"] = {"never": "copy"}
            elif mutation == "missing":
                references.pop()
            elif mutation == "duplicate":
                references.append(references[0])
            elif mutation == "dependency":
                references[0]["parent_id"] = "0" * 64
            elif mutation == "kind":
                references[0]["kind"] = "passage"
            payload = b"".join(json.dumps(reference).encode() + b"\n" for reference in references)
            contents["external_records.jsonl"] = payload
            if mutation != "checksum":
                metadata["external_records_sha256"] = hashlib.sha256(payload).hexdigest()
                metadata["external_count"] = len(references)
        contents["manifest.json"] = json.dumps(metadata).encode()
        return [(entry, contents[entry.filename]) for entry, _ in entries if entry.filename in contents]

    rewrite(path, mutate)
    target, destination_archive = Repository(), Mock()
    with pytest.raises(PublicationError):
        DocumentBackupService(target, destination_archive).restore(path)
    assert not target.writes
    assert not target.references
    destination_archive.put.assert_not_called()


def test_legacy_document_only_bundle_remains_readable(document_case, tmp_path):
    archive, snapshot = document_case
    path = tmp_path / "legacy-document-only.zip"
    DocumentBackupService(Repository(snapshot), archive).backup(path)

    def legacy(entries):
        result = []
        for entry, data in entries:
            if entry.filename == "external_records.jsonl":
                continue
            if entry.filename == "manifest.json":
                metadata = json.loads(data)
                del metadata["external_records_sha256"]
                del metadata["external_count"]
                data = json.dumps(metadata).encode()
            result.append((entry, data))
        return result

    rewrite(path, legacy)
    assert DocumentBackupService.verify(path).external_count == 0
    with zipfile.ZipFile(path) as bundle:
        assert DocumentBackupService.verify(path).backup_id == content_digest(json.loads(bundle.read("manifest.json")))
    target = Repository()
    assert DocumentBackupService(target, archive).restore(path).inserted == len(snapshot) - 1
    assert snapshot[0].item_id not in target.items


def test_restore_real_repository_uses_only_document_create_requests(document_case, tmp_path):
    archive, records = document_case
    path = tmp_path / "http.zip"
    DocumentBackupService(Repository(records), archive).backup(path)
    stored = {}
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and f"/{MANAGEMENT_INDEX}/_doc/" in request.url.path:
            key = request.url.path.rsplit("/", 1)[1]
            if key not in stored:
                return httpx.Response(404, json={"found": False})
            return httpx.Response(
                200,
                json={
                    "_id": key,
                    "_source": stored[key],
                    "_seq_no": 0,
                    "_primary_term": 1,
                },
            )
        if request.method == "PUT" and f"/{MANAGEMENT_INDEX}/_create/" in request.url.path:
            key = request.url.path.rsplit("/", 1)[1]
            stored[key] = json.loads(request.content)
            return httpx.Response(201, json={"result": "created"})
        if request.method == "POST" and request.url.path == f"/{MANAGEMENT_INDEX}/_refresh":
            return httpx.Response(200, json={"_shards": {"failed": 0}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        repository = OpenSearchDocumentRepository(
            "http://opensearch.test",
            client=client,
            lock_path=tmp_path / "publication.lock",
        )
        result = DocumentBackupService(repository, archive).restore(path)
    assert result.inserted == len(records) - 1
    assert records[0].item_id not in stored
    assert calls.count(("POST", f"/{MANAGEMENT_INDEX}/_refresh")) == 1
    assert not any("_alias" in path or "_search" in path or "build" in path for _, path in calls)


def test_backup_real_repository_uses_bounded_snapshot_without_lifecycle_or_shadow(document_case, tmp_path):
    archive, records = document_case
    stored = {item.item_id: OpenSearchDocumentRepository._envelope(item) for item in records}
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == f"/{MANAGEMENT_INDEX}/_search":
            return httpx.Response(
                200,
                json={
                    "_scroll_id": "snapshot",
                    "hits": {
                        "hits": [
                            *({"_id": key} for key in stored),
                            {"_id": "shadow:baseline:immutable"},
                            {"_id": "search-active:lexical"},
                        ]
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/_search/scroll":
            return httpx.Response(200, json={"_scroll_id": "snapshot", "hits": {"hits": []}})
        if request.method == "DELETE" and request.url.path == "/_search/scroll":
            return httpx.Response(200, json={"succeeded": True})
        if request.method == "GET" and f"/{MANAGEMENT_INDEX}/_doc/" in request.url.path:
            key = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "_id": key,
                    "_source": stored[key],
                    "_seq_no": 0,
                    "_primary_term": 1,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    path = tmp_path / "real-snapshot.zip"
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        repository = OpenSearchDocumentRepository(
            "http://opensearch.test",
            client=client,
            lock_path=tmp_path / "publication.lock",
        )
        manifest = DocumentBackupService(repository, archive).backup(path)
    assert manifest.row_count == len(records)
    assert DocumentBackupService.verify(path) == manifest
    assert ("DELETE", "/_search/scroll") in calls
    assert not any("shadow:" in path or "search-active:" in path for _, path in calls)


@pytest.mark.parametrize("fixture_name", ["document_case", "mixed_document_case"])
def test_real_repository_backup_restore_republish_is_sql_free_and_preserves_revocation(
    fixture_name, request, tmp_path, monkeypatch
):
    archive, baseline = request.getfixturevalue(fixture_name)
    stored, requests = {}, []
    sql = Mock(side_effect=AssertionError("document recovery must not open SQL"))
    monkeypatch.setattr(PublicBenchmarkRecordDao, "_transaction", sql)

    def transport(request):
        requests.append(request)
        if request.url.path == f"/{MANAGEMENT_INDEX}/_search":
            return httpx.Response(
                200,
                json={
                    "_scroll_id": "recovery",
                    "hits": {"hits": [{"_id": key} for key in stored]},
                },
            )
        if request.url.path == "/_search/scroll":
            return httpx.Response(200, json={"_scroll_id": "recovery", "hits": {"hits": []}})
        if request.method == "POST" and request.url.path == f"/{MANAGEMENT_INDEX}/_refresh":
            return httpx.Response(200, json={"_shards": {"failed": 0}})
        key = unquote(request.url.path.rsplit("/", 1)[1])
        if request.method == "GET" and f"/{MANAGEMENT_INDEX}/_doc/" in request.url.path:
            return (
                httpx.Response(200, json=stored[key])
                if key in stored
                else httpx.Response(
                    404,
                    json={"found": False},
                )
            )
        if request.method == "PUT" and f"/{MANAGEMENT_INDEX}/_create/" in request.url.path:
            if key in stored:
                return httpx.Response(409, json={"error": "conflict"})
            sequence = 0
        elif request.method == "PUT" and f"/{MANAGEMENT_INDEX}/_doc/" in request.url.path:
            if (
                key not in stored
                or request.url.params["if_seq_no"] != str(stored[key]["_seq_no"])
                or request.url.params["if_primary_term"] != "1"
            ):
                return httpx.Response(409, json={"error": "conflict"})
            sequence = stored[key]["_seq_no"] + 1
        else:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        stored[key] = {
            "_id": key,
            "_source": json.loads(request.content),
            "_seq_no": sequence,
            "_primary_term": 1,
        }
        return httpx.Response(201, json={"result": "created"})

    origin = BENCHMARK_ORIGIN
    approval = RegistryState.model_validate(baseline[0].source_details).approval
    metadata = ReleaseManifest.model_validate(baseline[-1].source_details)
    file = metadata.inventory.files[0]
    data = archive.read(file.sha256)
    parsed = (ParsedFile(path=file.path, items=parse_markdown(file, metadata.inventory, data)),)
    path = tmp_path / "operator-backup.zip"
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        repository = DocumentRepository(
            "http://opensearch.test",
            client=client,
            lock_path=tmp_path / "publication.lock",
        )
        publication = RagPublicationService(dao=repository, archive=archive)
        publication.register_source(origin, approval)
        if fixture_name == "mixed_document_case":
            for item in baseline:
                if item.kind in ("source", "passage") and item.source_role != "registry":
                    repository._create_immutable(item)
                elif item.kind not in ("source", "passage"):
                    repository.save_external_reference(external_member_reference(item))
            manifest = baseline[-1]
        else:
            manifest = publication.stage(metadata.inventory, parsed, expected_counts=metadata.counts)
        publication.activate(origin, manifest.item_id)
        repository.create_record("search-active:lexical", {"build_id": "old-build"})
        backup = DocumentBackupService(repository, archive).backup(path)

        stored.clear()
        monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "recovery-data"))
        restored_archive = SourceArchive()
        restore = DocumentBackupService(repository, restored_archive)
        assert restore.restore(path).inserted == backup.row_count + backup.external_count - 1
        assert repository.get(registry_id(origin)) is None
        assert repository.read_record("search-active:lexical") is None
        assert repository.public_search_scopes((origin,)) == ()
        with pytest.raises(PublicationError, match="not registered"):
            publication.activate(origin, manifest.item_id)

        # Explicit current approval is required before the restored bytes can publish.
        publication = RagPublicationService(dao=repository, archive=restored_archive)
        publication.register_source(origin, approval)
        rebuilt = (
            repository.get(manifest.item_id)
            if backup.external_count
            else publication.stage(
                metadata.inventory,
                parsed,
                expected_counts=metadata.counts,
            )
        )
        publication.activate(origin, rebuilt.item_id)
        scopes = repository.public_search_scopes((origin,))
        assert len(scopes) == 1 and scopes[0].manifest_id == rebuilt.item_id
        assert tuple(repository.list_published(origin)) == parsed[0].items
        assert restored_archive.read(file.sha256) == data

        repository.set_enabled(origin, False, expected_generation=repository.head(origin).state.generation)
        revoked = repository.head(origin).registry
        assert restore.restore(path).inserted == 0
        assert repository.head(origin).registry == revoked
        assert repository.public_search_scopes((origin,)) == ()
        assert repository.read_record("search-active:lexical") is None
    sql.assert_not_called()
    assert not any("_alias" in request.url.path for request in requests)
