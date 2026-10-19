from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import sessionmaker

from llm_d_lens.benchmark_rag import sources
from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationConflictError,
    PublicationError,
    ReleaseManifest,
    SourceApproval,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.sources import (
    BENCHMARK_ORIGIN,
    LLM_D_ORIGIN,
    PRISM_ORIGIN,
    SourceArchive,
    allowed_paths,
    collect_git,
    collect_snapshot,
    input_format,
)
from llm_d_lens.db.dao.rag_item import RagItemDaoError
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.models.rag_item import RagItemRow
from llm_d_lens.infrastructure.process import run_sync
from llm_d_lens.infrastructure.storage.paths import storage_path


def _approval(origin: str = LLM_D_ORIGIN) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="test-reviewer",
        approval_reference="test-only approval",
        license_reference="test-only license review",
        parser_version="test-parser-v1",
        allowed_paths=allowed_paths(origin),
    )


@pytest.fixture
def service(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RagPublicationService:
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "lens-data"))
    dao = RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))
    service = RagPublicationService(dao=dao)
    service.register_source(LLM_D_ORIGIN, _approval())
    return service


def _inputs(
    service: RagPublicationService,
    revision: str,
    documents: dict[str, bytes],
    *,
    parse_paths: set[str] | None = None,
    origin: str = LLM_D_ORIGIN,
) -> tuple[PinnedInventory, tuple[ParsedFile, ...]]:
    files = []
    parsed = []
    for path, data in sorted(documents.items()):
        digest = service.archive.put(data)
        file = InventoryFile(path=path, sha256=digest, format="markdown")
        files.append(file)
        if parse_paths is None or path in parse_paths:
            key = f"{path}#passage"
            passage = RagItem(
                item_id=item_id(origin, key, revision),
                kind="passage",
                logical_key=key,
                origin_id=origin,
                source_revision=revision,
                content_sha256=digest,
                access_classification="verified_public",
                source_path=path,
                format="markdown",
                parent_id=item_id(origin, f"file:{path}", revision),
                passage_text=data.decode(),
                heading_path=["Test"],
                source_pointer={"heading": "Test", "start": 0, "end": len(data.decode())},
            )
            parsed.append(ParsedFile(path=path, items=(passage,)))
    return PinnedInventory(origin_id=origin, revision=revision, complete=True, files=tuple(files)), tuple(parsed)


def _stage(service: RagPublicationService, inputs: tuple[PinnedInventory, tuple[ParsedFile, ...]]) -> RagItem:
    inventory, parsed = inputs
    return service.stage(
        inventory, parsed, expected_counts={"source": len(inventory.files), "passage": len(inventory.files)}
    )


def _publish(service: RagPublicationService, revision: str, documents: dict[str, bytes]) -> RagItem:
    manifest = _stage(service, _inputs(service, revision, documents))
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    return manifest


def _texts(service: RagPublicationService) -> set[str | None]:
    return {item.passage_text for item in service.dao.list_published(LLM_D_ORIGIN, kind="passage")}


def _row_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(RagItemRow)).scalar_one()


def test_staging_is_invisible_and_activation_uses_exact_manifest(service: RagPublicationService):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old alpha", "guides/b.md": b"old beta"})
    new_inputs = _inputs(service, "b" * 40, {"guides/a.md": b"new alpha", "guides/c.md": b"new gamma"})
    new = _stage(service, new_inputs)
    assert _texts(service) == {"old alpha", "old beta"}
    assert service.dao.count_published(LLM_D_ORIGIN, kind="passage") == 2
    assert service.dao.head(LLM_D_ORIGIN).manifest == old
    result = service.activate(LLM_D_ORIGIN, new.item_id)
    assert result.changed and result.generation == 2
    assert _texts(service) == {"new alpha", "new gamma"}
    assert service.dao.count_published(LLM_D_ORIGIN) == 4
    assert service.dao.get(old.item_id) == old
    assert service.archive.read(hashlib.sha256(b"old beta").hexdigest()) == b"old beta"
    tombstones = [
        item for item in service.dao.list_version(LLM_D_ORIGIN, "b" * 40) if item.publication_state == "tombstone"
    ]
    assert {item.source_path for item in tombstones} == {"guides/a.md", "guides/b.md"}
    assert all(item.item_id not in new.manifest_item_ids for item in tombstones)


def test_staged_and_published_retries_create_no_duplicate_rows(service: RagPublicationService, postgres_engine: Engine):
    inputs = _inputs(service, "a" * 40, {"guides/a.md": b"alpha"})
    first = _stage(service, inputs)
    count = _row_count(postgres_engine)
    assert _stage(service, inputs) == first
    assert _row_count(postgres_engine) == count
    initial = service.activate(LLM_D_ORIGIN, first.item_id)
    unchanged = _stage(service, _inputs(service, "a" * 40, {"guides/a.md": b"alpha"}, parse_paths=set()))
    assert unchanged == first
    result = service.activate(LLM_D_ORIGIN, unchanged.item_id)
    assert not result.changed and result.generation == initial.generation
    assert _row_count(postgres_engine) == count
    assert service.register_source(LLM_D_ORIGIN, _approval()).published_manifest_id == first.item_id


def test_new_commit_reuses_unchanged_file_versions(service: RagPublicationService, postgres_engine: Engine):
    old = _publish(service, "a" * 40, {"guides/a.md": b"alpha", "guides/b.md": b"beta"})
    old_metadata = ReleaseManifest.model_validate(old.source_details)
    before = _row_count(postgres_engine)
    updated = _stage(
        service,
        _inputs(
            service,
            "b" * 40,
            {"guides/a.md": b"alpha", "guides/b.md": b"beta"},
            parse_paths=set(),
        ),
    )
    assert updated.manifest_item_ids == old.manifest_item_ids
    assert ReleaseManifest.model_validate(updated.source_details).files == old_metadata.files
    assert _row_count(postgres_engine) == before + 1
    service.activate(LLM_D_ORIGIN, updated.item_id)
    assert {item.source_revision for item in service.dao.list_published(LLM_D_ORIGIN)} == {"a" * 40}
    assert service.dao.head(LLM_D_ORIGIN).manifest.source_revision == "b" * 40


@pytest.mark.parametrize("failure", ["parser", "missing-parser", "counts", "unapproved-path", "missing-blob"])
def test_prepare_failure_preserves_previous_release(
    service: RagPublicationService, postgres_engine: Engine, failure: str
):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    inventory, parsed = _inputs(service, "b" * 40, {"guides/b.md": b"new"})
    counts = {"source": 1, "passage": 1}
    if failure == "parser":
        parsed = (parsed[0].model_copy(update={"errors": ("partial table",)}),)
    elif failure == "missing-parser":
        parsed = ()
    elif failure == "counts":
        counts["passage"] = 2
    elif failure == "unapproved-path":
        inventory = inventory.model_copy(
            update={"files": (inventory.files[0].model_copy(update={"path": "private/leak.md"}),)}
        )
    else:
        inventory = inventory.model_copy(
            update={"files": (inventory.files[0].model_copy(update={"sha256": "0" * 64}),)}
        )
    before = _row_count(postgres_engine)
    with pytest.raises((PublicationError, FileNotFoundError)):
        service.stage(inventory, parsed, expected_counts=counts)
    assert _row_count(postgres_engine) == before
    assert service.dao.head(LLM_D_ORIGIN).manifest == old
    assert _texts(service) == {"old"}


def test_incomplete_or_duplicate_inventory_rejected_without_retirement(service: RagPublicationService):
    old = _publish(service, "a" * 40, {"guides/a.md": b"alpha", "guides/b.md": b"beta"})
    inventory, _ = _inputs(service, "b" * 40, {"guides/a.md": b"alpha"})
    with pytest.raises(ValidationError):
        PinnedInventory.model_validate(inventory.model_dump() | {"complete": False})
    with pytest.raises(ValidationError):
        PinnedInventory.model_validate(inventory.model_dump() | {"files": [inventory.files[0], inventory.files[0]]})
    assert service.dao.head(LLM_D_ORIGIN).manifest == old
    assert _texts(service) == {"alpha", "beta"}


def test_failure_during_activation_rolls_back_pointer_history_and_tombstones(
    service: RagPublicationService,
    postgres_engine: Engine,
):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    staged = _stage(service, _inputs(service, "b" * 40, {"guides/b.md": b"new"}))
    before = _row_count(postgres_engine)
    head = service.dao.head(LLM_D_ORIGIN)

    def fail_pointer_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE rag_items SET ") and "published_manifest_id=" in statement:
            raise RuntimeError("injected pointer failure")

    event.listen(postgres_engine, "before_cursor_execute", fail_pointer_update)
    try:
        with pytest.raises(RuntimeError, match="pointer failure"):
            service.activate(LLM_D_ORIGIN, staged.item_id)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail_pointer_update)
    assert _row_count(postgres_engine) == before
    assert service.dao.head(LLM_D_ORIGIN) == head
    assert service.dao.get(old.item_id) == old
    assert _texts(service) == {"old"}


def test_concurrent_activations_serialize_and_readers_never_see_staged_rows(
    service: RagPublicationService,
    monkeypatch: pytest.MonkeyPatch,
):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    left = _stage(service, _inputs(service, "b" * 40, {"guides/a.md": b"left"}))
    right = _stage(service, _inputs(service, "c" * 40, {"guides/a.md": b"right"}))
    start = Barrier(2)
    locked = Event()
    release = Event()
    original_verify = service._verify_manifest

    def pause_verification(metadata: ReleaseManifest) -> None:
        original_verify(metadata)
        locked.set()
        assert release.wait(10)

    monkeypatch.setattr(service, "_verify_manifest", pause_verification)

    def activate(manifest: RagItem):
        start.wait(timeout=10)
        try:
            return service.activate(LLM_D_ORIGIN, manifest.item_id)
        except PublicationConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(activate, manifest) for manifest in (left, right)]
        try:
            assert locked.wait(10)
            assert service.dao.head(LLM_D_ORIGIN).manifest == old
            assert _texts(service) == {"old"}
            assert service.dao.count_published(LLM_D_ORIGIN, kind="passage") == 1
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert results.count("conflict") == 1
    assert _texts(service) in ({"left"}, {"right"})
    assert service.dao.head(LLM_D_ORIGIN).state.generation == 2


def test_rollback_restores_exact_manifest_and_prevents_aba_activation(service: RagPublicationService):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    next_release = _stage(service, _inputs(service, "b" * 40, {"guides/b.md": b"new"}))
    stale = _stage(service, _inputs(service, "c" * 40, {"guides/c.md": b"stale"}))
    with pytest.raises(PublicationError, match="never published"):
        service.rollback(LLM_D_ORIGIN, next_release.item_id, expected_generation=1)
    service.activate(LLM_D_ORIGIN, next_release.item_id)
    with pytest.raises(PublicationConflictError):
        service.activate(LLM_D_ORIGIN, stale.item_id)
    result = service.rollback(LLM_D_ORIGIN, old.item_id, expected_generation=2)
    assert result.changed and result.generation == 3
    assert _texts(service) == {"old"}
    assert service.dao.get(next_release.item_id) == next_release
    assert service.archive.read(hashlib.sha256(b"new").hexdigest()) == b"new"
    with pytest.raises(PublicationConflictError):
        service.activate(LLM_D_ORIGIN, stale.item_id)
    with pytest.raises(PublicationConflictError):
        service.rollback(LLM_D_ORIGIN, next_release.item_id, expected_generation=1)


def test_source_revocation_hides_members_and_invalidates_staged_releases(service: RagPublicationService):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    staged = _stage(service, _inputs(service, "b" * 40, {"guides/b.md": b"new"}))
    service.dao.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
    assert service.dao.count_published(LLM_D_ORIGIN) == 0
    assert service.dao.list_published(LLM_D_ORIGIN) == []
    with pytest.raises(PublicationError, match="disabled"):
        service.activate(LLM_D_ORIGIN, staged.item_id)
    with pytest.raises(PublicationError, match="disabled"):
        _stage(service, _inputs(service, "b" * 40, {"guides/b.md": b"new"}))
    service.dao.set_enabled(LLM_D_ORIGIN, True, expected_generation=2)
    assert service.dao.head(LLM_D_ORIGIN).manifest == old
    assert _texts(service) == {"old"}
    with pytest.raises(PublicationConflictError):
        service.activate(LLM_D_ORIGIN, staged.item_id)


def test_corrupt_bytes_block_activation_and_rollback(service: RagPublicationService):
    old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
    new = _publish(service, "b" * 40, {"guides/b.md": b"new"})
    digest = hashlib.sha256(b"old").hexdigest()
    storage_path("data", "artifacts", "benchmark-rag", digest, "source").write_bytes(b"corrupt")
    with pytest.raises(PublicationError, match="hash verification"):
        service.rollback(LLM_D_ORIGIN, old.item_id, expected_generation=2)
    assert service.dao.head(LLM_D_ORIGIN).manifest == new
    with pytest.raises(PublicationError, match="immutable"):
        service.archive.put(b"old")
    assert storage_path("data", "artifacts", "benchmark-rag", digest, "source").read_bytes() == b"corrupt"
    staged = _stage(service, _inputs(service, "c" * 40, {"guides/c.md": b"pending"}))
    digest = hashlib.sha256(b"pending").hexdigest()
    storage_path("data", "artifacts", "benchmark-rag", digest, "source").write_bytes(b"corrupt")
    with pytest.raises(PublicationError, match="hash verification"):
        service.activate(LLM_D_ORIGIN, staged.item_id)
    assert service.dao.head(LLM_D_ORIGIN).manifest == new
    assert _texts(service) == {"new"}


def test_manifest_cannot_promote_unrelated_staged_or_wrong_source_items(service: RagPublicationService):
    inventory, parsed = _inputs(service, "a" * 40, {"guides/a.md": b"alpha"})
    bad = parsed[0].items[0].model_copy(update={"origin_id": BENCHMARK_ORIGIN})
    with pytest.raises(PublicationError, match="source identity"):
        service.stage(
            inventory, (ParsedFile(path="guides/a.md", items=(bad,)),), expected_counts={"source": 1, "passage": 1}
        )
    staged = _stage(service, (inventory, parsed))
    forged = staged.model_copy(update={"manifest_item_ids": [registry_id(LLM_D_ORIGIN)]})
    with pytest.raises(PublicationError, match="membership"):
        service.dao.stage(forged, ())
    assert service.dao.count_published(LLM_D_ORIGIN) == 0


def test_stale_file_dependency_forces_reparse_and_failure_is_atomic(
    service: RagPublicationService, postgres_engine: Engine
):
    inventory, parsed = _inputs(service, "a" * 40, {"guides/a.md": b"alpha"})
    file = inventory.files[0]
    figure_key = f"{file.path}#figure"
    figure = RagItem(
        item_id=item_id(LLM_D_ORIGIN, figure_key, inventory.revision),
        logical_key=figure_key,
        kind="figure",
        origin_id=LLM_D_ORIGIN,
        source_revision=inventory.revision,
        content_sha256=file.sha256,
        access_classification="verified_public",
        source_path=file.path,
        format="markdown",
        coverage="unknown",
        source_pointer={"heading": "Test"},
        parent_id=item_id(LLM_D_ORIGIN, f"file:{file.path}", inventory.revision),
    )
    bad = parsed[0].items[0].model_copy(update={"parent_id": figure.item_id})
    before = _row_count(postgres_engine)
    with pytest.raises((PublicationError, RagItemDaoError)):
        service.stage(
            inventory,
            (ParsedFile(path=file.path, items=(figure, bad)),),
            expected_counts={"source": 1, "passage": 1, "figure": 1},
        )
    assert _row_count(postgres_engine) == before
    assert service.dao.count_published(LLM_D_ORIGIN) == 0


def test_membership_filter_applies_before_full_text_ranking_and_counts(
    service: RagPublicationService, postgres_engine: Engine
):
    _publish(service, "a" * 40, {"guides/a.md": b"published benchmark definition"})
    _stage(service, _inputs(service, "b" * 40, {"guides/b.md": b"unpublished secretphrase definition"}))
    with sessionmaker(bind=postgres_engine)() as session:
        statement = service.dao._published_members(LLM_D_ORIGIN).where(
            RagItemRow.search_vector.op("@@")(func.plainto_tsquery("simple", "secretphrase"))
        )
        assert (
            session.scalars(
                statement.order_by(
                    func.ts_rank(
                        RagItemRow.search_vector,
                        func.plainto_tsquery("simple", "secretphrase"),
                    ).desc()
                )
            ).all()
            == []
        )
        assert session.scalar(select(func.count()).select_from(statement.subquery())) == 0


def test_unknown_sources_and_approval_expansion_fail_closed(service: RagPublicationService):
    with pytest.raises(PublicationError, match="allowlist"):
        service.register_source("https://example.invalid/unreviewed", _approval())
    with pytest.raises(PublicationError, match="allowlist"):
        service.register_source(LLM_D_ORIGIN, _approval().model_copy(update={"allowed_paths": ("**/*",)}))
    with pytest.raises(PublicationError, match="compatibility"):
        service.register_source(LLM_D_ORIGIN, _approval().model_copy(update={"parser_version": "unreviewed-v2"}))
    assert service.dao.count_published(BENCHMARK_ORIGIN) == 0


def test_postgres_required_for_publication_not_a_sqlite_fallback():
    with pytest.raises(PublicationError, match="PostgreSQL"):
        RagPublicationDao().register(LLM_D_ORIGIN, _approval())


def test_concurrent_staging_and_repeated_activation_are_idempotent(
    service: RagPublicationService,
    postgres_engine: Engine,
):
    inputs = _inputs(service, "a" * 40, {"guides/a.md": b"alpha"})
    start = Barrier(2)

    def stage(_: int) -> RagItem:
        start.wait(timeout=10)
        return _stage(service, inputs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        manifests = list(executor.map(stage, range(2)))
    assert manifests[0] == manifests[1]
    before = _row_count(postgres_engine)
    start = Barrier(2)

    def activate(_: int):
        start.wait(timeout=10)
        return service.activate(LLM_D_ORIGIN, manifests[0].item_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, range(2)))
    assert sorted(result.changed for result in results) == [False, True]
    assert _row_count(postgres_engine) == before
    assert len(service.dao.head(LLM_D_ORIGIN).state.history) == 1


def test_row_state_cannot_bypass_manifest_membership(service: RagPublicationService):
    inventory, parsed = _inputs(service, "a" * 40, {"guides/a.md": b"published"})
    manifest = _stage(service, (inventory, parsed))
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    key = "guides/a.md#not-a-member"
    rogue = (
        parsed[0]
        .items[0]
        .model_copy(
            update={
                "item_id": item_id(LLM_D_ORIGIN, key, inventory.revision),
                "logical_key": key,
                "publication_state": "published",
                "passage_text": "unpublished extra",
            }
        )
    )
    service.dao.save(rogue)
    assert service.dao.get(rogue.item_id) == rogue
    assert _texts(service) == {"published"}
    assert service.dao.count_published(LLM_D_ORIGIN, kind="passage") == 1


def test_complete_empty_inventory_retires_all_members_and_remains_reversible(
    service: RagPublicationService,
    postgres_engine: Engine,
):
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        old = _publish(service, "a" * 40, {"guides/a.md": b"old"})
        empty = _stage(service, _inputs(service, "b" * 40, {}))
        service.activate(LLM_D_ORIGIN, empty.item_id)
        assert service.dao.list_published(LLM_D_ORIGIN) == []
        assert service.dao.count_published(LLM_D_ORIGIN) == 0
        assert service.dao.get(old.item_id) == old
        service.rollback(LLM_D_ORIGIN, old.item_id, expected_generation=2)
        assert _texts(service) == {"old"}
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert any(statement.lstrip().upper().startswith("INSERT") for statement in statements)
    assert not any("agentic_benchmark_records" in statement.lower() for statement in statements)


def test_cross_file_links_cannot_survive_removal_of_their_target(
    service: RagPublicationService,
    postgres_engine: Engine,
):
    inventory, _ = _inputs(service, "a" * 40, {"guides/a.md": b"experiment", "guides/b.md": b"figure"})
    first, second = inventory.files
    common = {
        "origin_id": LLM_D_ORIGIN,
        "source_revision": inventory.revision,
        "access_classification": "verified_public",
        "format": "markdown",
        "source_pointer": {"heading": "Test"},
    }
    experiment = RagItem.model_validate(
        common
        | {
            "kind": "experiment",
            "item_id": item_id(LLM_D_ORIGIN, f"{first.path}#experiment", inventory.revision),
            "logical_key": f"{first.path}#experiment",
            "source_path": first.path,
            "content_sha256": first.sha256,
            "parent_id": item_id(LLM_D_ORIGIN, f"file:{first.path}", inventory.revision),
        }
    )
    figure = RagItem.model_validate(
        common
        | {
            "kind": "figure",
            "item_id": item_id(LLM_D_ORIGIN, f"{second.path}#figure", inventory.revision),
            "logical_key": f"{second.path}#figure",
            "source_path": second.path,
            "content_sha256": second.sha256,
            "parent_id": item_id(LLM_D_ORIGIN, f"file:{second.path}", inventory.revision),
            "coverage": "unknown",
        }
    )
    link = RagItem.model_validate(
        common
        | {
            "kind": "figure_link",
            "item_id": item_id(LLM_D_ORIGIN, f"{second.path}#link", inventory.revision),
            "logical_key": f"{second.path}#link",
            "source_path": second.path,
            "content_sha256": second.sha256,
            "parent_id": figure.item_id,
            "referenced_item_id": experiment.item_id,
        }
    )
    old = service.stage(
        inventory,
        (ParsedFile(path=first.path, items=(experiment,)), ParsedFile(path=second.path, items=(figure, link))),
        expected_counts={"source": 2, "experiment": 1, "figure": 1, "figure_link": 1},
    )
    service.activate(LLM_D_ORIGIN, old.item_id)
    new_inventory, _ = _inputs(service, "b" * 40, {second.path: b"figure"}, parse_paths=set())
    before = _row_count(postgres_engine)
    with pytest.raises(PublicationError, match="closed"):
        service.stage(new_inventory, (), expected_counts={"source": 1, "figure": 1, "figure_link": 1})
    assert _row_count(postgres_engine) == before
    assert service.dao.head(LLM_D_ORIGIN).manifest == old


@pytest.mark.parametrize(
    ("origin", "path", "expected"),
    [
        (LLM_D_ORIGIN, "guides/direct.md", "markdown"),
        (LLM_D_ORIGIN, "guides/demo/benchmark-results/direct.png", "png"),
        (LLM_D_ORIGIN, "guides/benchmark-results/nested/data.json", "json"),
        (LLM_D_ORIGIN, "guides/demo/benchmark-results/nested/data.json", "json"),
        (LLM_D_ORIGIN, "guides/config.yaml", None),
        (LLM_D_ORIGIN, "guides/not-a-result.json", None),
        (BENCHMARK_ORIGIN, "docs/direct.md", "markdown"),
        (BENCHMARK_ORIGIN, "benchmark-report/README.md", "markdown"),
        (BENCHMARK_ORIGIN, "benchmark-report/llmd_benchmark_report/br_v0_2_json_schema.json", None),
    ],
)
def test_only_reviewed_paths_and_formats_are_collectable(origin: str, path: str, expected: str | None):
    assert input_format(origin, path) == expected


def test_archive_concurrent_same_bytes_never_overwrite_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    archive = SourceArchive()
    with ThreadPoolExecutor(max_workers=2) as executor:
        digests = list(executor.map(archive.put, [b"same pinned bytes"] * 2))
    assert digests[0] == digests[1]
    assert archive.read(digests[0]) == b"same pinned bytes"
    assert not list(storage_path("data", "artifacts", "benchmark-rag", digests[0]).glob(".source-*"))


def _git(repo: Path, *arguments: str) -> str:
    result = run_sync(["git", "-C", str(repo), *arguments], timeout=10)
    assert result.ok, result.stderr
    return result.stdout.strip()


def test_git_inventory_uses_complete_pinned_tree_not_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", LLM_D_ORIGIN)
    (repo / "guides").mkdir()
    (repo / "guides" / "a.md").write_bytes(b"pinned")
    png = repo / "guides" / "demo" / "benchmark-results" / "plot.png"
    png.parent.mkdir(parents=True)
    png_bytes = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x80"
    png.write_bytes(png_bytes)
    (repo / "excluded.md").write_bytes(b"not approved")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "guides" / "a.md").write_bytes(b"dirty worktree")
    (repo / "guides" / "new.md").write_bytes(b"uncommitted")
    archive = SourceArchive()
    inventory = collect_git(LLM_D_ORIGIN, repo, commit, _approval(), archive)
    assert [file.path for file in inventory.files] == ["guides/a.md", "guides/demo/benchmark-results/plot.png"]
    assert archive.read(inventory.files[0].sha256) == b"pinned"
    assert archive.read(inventory.files[1].sha256) == png_bytes
    original_git = sources._git
    with monkeypatch.context() as patch:

        def truncated_git(repo: Path, *arguments: str) -> str:
            result = original_git(repo, *arguments)
            return result.rstrip("\x00") if arguments[0] == "ls-tree" else result

        patch.setattr(sources, "_git", truncated_git)
        with pytest.raises(PublicationError, match="truncated"):
            collect_git(LLM_D_ORIGIN, repo, commit, _approval(), archive)
    (repo / "guides" / "link.md").symlink_to("../excluded.md")
    _git(repo, "add", "guides/link.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "symlink fixture")
    with pytest.raises(PublicationError, match="symlink"):
        collect_git(LLM_D_ORIGIN, repo, _git(repo, "rev-parse", "HEAD"), _approval(), archive)
    _git(repo, "remote", "set-url", "origin", BENCHMARK_ORIGIN)
    with pytest.raises(PublicationError, match="origin"):
        collect_git(LLM_D_ORIGIN, repo, commit, _approval(), archive)


def test_snapshot_inventory_cannot_treat_missing_file_as_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "snapshot"
    root.mkdir()
    report = root / "historical-merged-deduplicated.json"
    report.write_bytes(b"[]")
    hashes = {"historical-merged-deduplicated.json": hashlib.sha256(b"[]").hexdigest()}
    archive = SourceArchive()
    approval = _approval(PRISM_ORIGIN)
    assert collect_snapshot(root, "2026-10-13", hashes, approval, archive).files[0].sha256 == hashes[report.name]
    report.unlink()
    with pytest.raises(PublicationError, match="incomplete"):
        collect_snapshot(root, "2026-10-13", hashes, approval, archive)
    report.write_bytes(b"changed")
    with pytest.raises(PublicationError, match="hash"):
        collect_snapshot(root, "2026-10-13", hashes, approval, archive)
