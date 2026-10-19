from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from llm_d_lens.benchmark_rag.admin import main
from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.prism import PARSER_VERSION
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationConflictError,
    PublicationError,
    SourceApproval,
    item_id,
)
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN, PRISM_ORIGIN, allowed_paths
from llm_d_lens.benchmark_rag.transfer import RagBackupService
from llm_d_lens.db.dao.rag_transfer import RagTransferDao


@dataclass
class Site:
    engine: Engine
    dao: RagTransferDao
    root: Path
    schema: str
    url: str

    def select(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LENS_DATA_DIR", str(self.root))

    def args(self) -> list[str]:
        return ["--schema", self.schema, "--data-dir", str(self.root)]


@pytest.fixture
def sites(postgres_url: str, tmp_path: Path) -> Iterator[Callable[..., Site]]:
    admin = create_engine(postgres_url)
    created: list[Site] = []

    def factory(name: str, *, initialize: bool = True) -> Site:
        schema = f"rag_transfer_{uuid4().hex}"
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = create_engine(admin.url.update_query_dict({"options": f"-csearch_path={schema}"}))
        root = tmp_path / name
        root.mkdir()
        site = Site(
            engine, RagTransferDao(sessionmaker(bind=engine, expire_on_commit=False)), root, schema, postgres_url
        )
        created.append(site)
        if initialize:
            site.dao.prepare(initialize=True)
        return site

    try:
        yield factory
    finally:
        for site in created:
            site.engine.dispose()
            with admin.begin() as connection:
                connection.execute(DropSchema(site.schema, cascade=True))
        admin.dispose()


def _approval() -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="portable acceptance",
        approval_reference="isolated test",
        license_reference="test-only approval",
        parser_version="portable-test-v1",
        allowed_paths=allowed_paths(LLM_D_ORIGIN),
    )


def _publish(
    site: Site,
    revision: str,
    data: bytes,
    *,
    passage: str | None = None,
    approval: SourceApproval | None = None,
) -> RagItem:
    service = RagPublicationService(dao=site.dao)
    service.register_source(LLM_D_ORIGIN, approval if approval is not None else _approval())
    path = "guides/test.md"
    digest = service.archive.put(data)
    key = f"{path}#passage"
    item = RagItem(
        item_id=item_id(LLM_D_ORIGIN, key, revision),
        logical_key=key,
        kind="passage",
        origin_id=LLM_D_ORIGIN,
        source_revision=revision,
        content_sha256=digest,
        access_classification="verified_public",
        source_path=path,
        format="markdown",
        parent_id=item_id(LLM_D_ORIGIN, f"file:{path}", revision),
        passage_text=passage if passage is not None else data.decode(),
        heading_path=["Test"],
        source_pointer={"heading": "Test"},
    )
    manifest = service.stage(
        PinnedInventory(
            origin_id=LLM_D_ORIGIN,
            revision=revision,
            complete=True,
            files=(InventoryFile(path=path, sha256=digest, format="markdown"),),
        ),
        (ParsedFile(path=path, items=(item,)),),
        expected_counts={"source": 1, "passage": 1},
    )
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    return manifest


def test_standalone_initialization_is_idempotent_and_rag_only(sites):
    site = sites("standalone")
    site.dao.prepare()
    assert inspect(site.engine).get_table_names() == ["rag_items"]


def test_migration_retains_history_and_supports_rollback(sites, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    old = _publish(source, "a" * 40, b"old")
    current = _publish(source, "b" * 40, b"new")
    original = source.dao.snapshot()
    bundle = tmp_path / "rag.zip"
    manifest = RagBackupService(source.dao).backup(bundle)
    assert RagBackupService.verify(bundle) == manifest
    target.select(monkeypatch)
    result = RagBackupService(target.dao).restore(bundle)
    assert result.inserted == len(original) and result.updated_sources == 1
    assert target.dao.head(LLM_D_ORIGIN).manifest == current
    assert [item for item in target.dao.snapshot() if item.source_role != "registry"] == [
        item for item in original if item.source_role != "registry"
    ]
    before = target.dao.snapshot()
    repeated = RagBackupService(target.dao).restore(bundle)
    assert repeated.inserted == repeated.updated_sources == 0
    assert target.dao.snapshot() == before
    head = target.dao.head(LLM_D_ORIGIN)
    RagPublicationService(dao=target.dao).rollback(LLM_D_ORIGIN, old.item_id, expected_generation=head.state.generation)
    assert target.dao.head(LLM_D_ORIGIN).manifest == old


def test_nonempty_merge_preserves_target_history_and_disabled_policy(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    incoming = _publish(source, "b" * 40, b"incoming")
    bundle = tmp_path / "rag.zip"
    RagBackupService(source.dao).backup(bundle)
    target.select(monkeypatch)
    local = _publish(target, "c" * 40, b"target-only")
    target.dao.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
    result = RagBackupService(target.dao).restore(bundle)
    assert result.inserted > 0 and result.updated_sources == 1
    head = target.dao.head(LLM_D_ORIGIN)
    assert head.manifest == incoming and not head.state.enabled and head.state.generation == 3
    assert target.dao.get(local.item_id) == local
    assert target.dao.list_published(LLM_D_ORIGIN) == []
    target.dao.set_enabled(LLM_D_ORIGIN, True, expected_generation=3)
    RagPublicationService(dao=target.dao).rollback(LLM_D_ORIGIN, local.item_id, expected_generation=4)
    assert target.dao.head(LLM_D_ORIGIN).manifest == local
    repeated = RagBackupService(target.dao).restore(bundle)
    assert repeated.updated_sources == 0
    assert target.dao.head(LLM_D_ORIGIN).manifest == local


def test_second_migration_retains_original_publication_history(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source, middle, target = sites("source"), sites("middle"), sites("target")
    source.select(monkeypatch)
    old = _publish(source, "a" * 40, b"old")
    _publish(source, "b" * 40, b"new")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    RagBackupService(source.dao).backup(first)
    middle.select(monkeypatch)
    RagBackupService(middle.dao).restore(first)
    RagBackupService(middle.dao).backup(second)
    target.select(monkeypatch)
    RagBackupService(target.dao).restore(second)
    state = target.dao.head(LLM_D_ORIGIN).state
    assert len(state.restores) == 2
    RagPublicationService(dao=target.dao).rollback(
        LLM_D_ORIGIN,
        old.item_id,
        expected_generation=state.generation,
    )
    assert target.dao.head(LLM_D_ORIGIN).manifest == old


def test_immutable_conflict_rolls_back_all_target_changes(sites, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    _publish(source, "a" * 40, b"same")
    _publish(source, "b" * 40, b"new")
    bundle = tmp_path / "rag.zip"
    RagBackupService(source.dao).backup(bundle)
    target.select(monkeypatch)
    _publish(target, "a" * 40, b"same", passage="conflicting parser payload")
    before = target.dao.snapshot()
    with pytest.raises(PublicationError, match="immutable"):
        RagBackupService(target.dao).restore(bundle)
    assert target.dao.snapshot() == before


def test_local_approval_is_preserved_and_backup_revocation_takes_effect(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    _publish(source, "b" * 40, b"incoming")
    source.dao.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
    bundle = tmp_path / "rag.zip"
    RagBackupService(source.dao).backup(bundle)
    target.select(monkeypatch)
    local_approval = _approval().model_copy(update={"approved_by": "target reviewer"})
    _publish(target, "a" * 40, b"local", approval=local_approval)
    RagBackupService(target.dao).restore(bundle)
    state = target.dao.head(LLM_D_ORIGIN).state
    assert state.approval == local_approval and not state.enabled


def test_pointer_failure_rolls_back_insertions_and_receipts(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    _publish(source, "b" * 40, b"incoming")
    bundle = tmp_path / "rag.zip"
    RagBackupService(source.dao).backup(bundle)
    target.select(monkeypatch)
    _publish(target, "a" * 40, b"local")
    before = target.dao.snapshot()

    def fail(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE rag_items SET") and "published_manifest_id=" in statement:
            raise RuntimeError("injected transfer pointer failure")

    event.listen(target.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            RagBackupService(target.dao).restore(bundle)
    finally:
        event.remove(target.engine, "before_cursor_execute", fail)
    assert target.dao.snapshot() == before


def test_readers_keep_old_release_until_restore_commits(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    incoming = _publish(source, "b" * 40, b"incoming")
    bundle = tmp_path / "rag.zip"
    RagBackupService(source.dao).backup(bundle)
    target.select(monkeypatch)
    local = _publish(target, "a" * 40, b"local")
    locked, release = Event(), Event()

    def pause(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE rag_items SET") and "published_manifest_id=" in statement:
            locked.set()
            assert release.wait(10)

    event.listen(target.engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(RagBackupService(target.dao).restore, bundle)
            try:
                assert locked.wait(10)
                assert target.dao.head(LLM_D_ORIGIN).manifest == local
                assert {item.passage_text for item in target.dao.list_published(LLM_D_ORIGIN, kind="passage")} == {
                    "local"
                }
            finally:
                release.set()
            future.result(timeout=10)
    finally:
        event.remove(target.engine, "before_cursor_execute", pause)
    assert target.dao.head(LLM_D_ORIGIN).manifest == incoming
    with pytest.raises(PublicationConflictError):
        RagPublicationService(dao=target.dao).activate(LLM_D_ORIGIN, local.item_id)


@pytest.mark.parametrize("corruption", ["records", "source", "missing", "unsafe", "duplicates"])
def test_corrupt_bundle_is_rejected_before_target_update(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corruption: str,
):
    source, target = sites("source"), sites("target")
    source.select(monkeypatch)
    _publish(source, "a" * 40, b"source")
    bundle = tmp_path / "good.zip"
    RagBackupService(source.dao).backup(bundle)
    with zipfile.ZipFile(bundle) as original:
        members = {name: original.read(name) for name in original.namelist()}
    first_source = next(name for name in members if name.startswith("sources/"))
    if corruption == "records":
        members["records.jsonl"] += b"\n"
    elif corruption == "source":
        members[first_source] = b"corrupt"
    elif corruption == "missing":
        del members[first_source]
    elif corruption == "unsafe":
        members["../outside"] = b"not allowed"
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)
        if corruption == "duplicates":
            with pytest.warns(UserWarning, match="Duplicate"):
                output.writestr("records.jsonl", members["records.jsonl"])
    target.select(monkeypatch)
    local = _publish(target, "c" * 40, b"local")
    before = target.dao.snapshot()
    with pytest.raises(PublicationError):
        RagBackupService(target.dao).restore(broken)
    assert target.dao.snapshot() == before
    assert target.dao.head(LLM_D_ORIGIN).manifest == local


def test_backup_is_exclusive_and_size_limits_fail_closed(sites, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source = sites("source")
    source.select(monkeypatch)
    _publish(source, "a" * 40, b"source")
    bundle = tmp_path / "rag.zip"
    service = RagBackupService(source.dao)
    service.backup(bundle)
    before = bundle.read_bytes()
    with pytest.raises(FileExistsError):
        service.backup(bundle)
    assert bundle.read_bytes() == before
    with pytest.raises(PublicationError, match="limit"):
        service.verify(bundle, max_bytes=10)
    with pytest.raises(PublicationError, match="limit"):
        service.backup(tmp_path / "oversize.zip", max_bytes=10)
    assert not list(tmp_path.glob(".rag-backup-*"))


def test_existing_schema_mismatch_is_not_silently_changed(sites):
    site = sites("schema")
    with site.engine.begin() as connection:
        connection.execute(text("ALTER TABLE rag_items ALTER COLUMN metric TYPE varchar(99)"))
    with pytest.raises(PublicationError, match="column"):
        site.dao.prepare(initialize=True)


def test_managed_lens_schema_cannot_be_bootstrapped_by_admin(sites):
    site = sites("managed", initialize=False)
    with site.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
    with pytest.raises(PublicationError, match="Alembic"):
        site.dao.prepare(initialize=True)
    assert inspect(site.engine).get_table_names() == ["alembic_version"]


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url-with-secret-test-marker",
        "postgresql+psycopg://user:secret-test-marker@localhost:not-a-port/database",
    ],
)
def test_cli_requires_explicit_database_and_never_echoes_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    url: str,
):
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    args = ["backup", "--data-dir", str(tmp_path), "--output", str(tmp_path / "rag.zip")]
    assert main(args) == 1
    assert "explicit database" in capsys.readouterr().err
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", url)
    assert main(args) == 1
    captured = capsys.readouterr()
    assert not captured.out and "secret-test-marker" not in captured.err


def test_cli_backup_verify_restore_into_new_machine(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    source, target = sites("source"), sites("target", initialize=False)
    source.select(monkeypatch)
    current = _publish(source, "a" * 40, b"source")
    bundle = tmp_path / "rag.zip"
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", source.url)
    assert main(["backup", *source.args(), "--output", str(bundle)]) == 0
    exported = json.loads(capsys.readouterr().out)
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL")
    assert main(["verify", "--bundle", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["backup_id"] == exported["backup_id"]
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", target.url)
    assert main(["restore", *target.args(), "--bundle", str(bundle), "--initialize"]) == 0
    assert json.loads(capsys.readouterr().out)["updated_sources"] == 1
    assert target.dao.head(LLM_D_ORIGIN).manifest == current
    with target.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM rag_items WHERE search_vector @@ plainto_tsquery('simple', 'source')")
            )
            == 1
        )


_REAL_SNAPSHOT = Path(__file__).resolve().parents[5] / "private/prism-public-2026-10-13"


@pytest.mark.skipif(not _REAL_SNAPSHOT.is_dir(), reason="local Git-ignored Prism snapshot required")
def test_real_snapshot_cli_rebuild_and_portable_migration(
    sites,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    source, target = sites("source", initialize=False), sites("target", initialize=False)
    source.select(monkeypatch)
    approval = _approval().model_copy(
        update={
            "parser_version": PARSER_VERSION,
            "allowed_paths": allowed_paths(PRISM_ORIGIN),
        }
    )
    approval_file = tmp_path / "approval.json"
    approval_file.write_text(approval.model_dump_json())
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", source.url)
    assert (
        main(
            [
                "rebuild",
                *source.args(),
                "--snapshot",
                str(_REAL_SNAPSHOT),
                "--approval",
                str(approval_file),
                "--initialize",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"source": 22, "experiment": 249, "measurement": 9060, "passage": 4}
    bundle = tmp_path / "prism-rag.zip"
    assert main(["backup", *source.args(), "--output", str(bundle)]) == 0
    exported = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", target.url)
    assert main(["restore", *target.args(), "--bundle", str(bundle), "--initialize"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["inserted"] == exported["row_count"] == 9337
    assert target.dao.count_published(PRISM_ORIGIN, kind="experiment") == 249
    assert target.dao.count_published(PRISM_ORIGIN, kind="measurement") == 9060
    assert main(["restore", *target.args(), "--bundle", str(bundle)]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["inserted"] == repeated["updated_sources"] == 0
