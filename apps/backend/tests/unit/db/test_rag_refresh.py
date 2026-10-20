from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.admin import main
from llm_d_lens.benchmark_rag.prism import PARSER_VERSION as PRISM_PARSER
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    PinnedInventory,
    PublicationConflictError,
    PublicationError,
    SourceApproval,
)
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.refresh import RagRefreshService, RefreshPlan
from llm_d_lens.benchmark_rag.repository_corpus import PARSER_VERSION
from llm_d_lens.benchmark_rag.service import PublicRagQueryService
from llm_d_lens.benchmark_rag.sources import (
    BENCHMARK_ORIGIN,
    PRISM_ORIGIN,
    allowed_paths,
    collect_git,
)
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.dao.rag_query import RagQueryDao


@pytest.fixture
def publication(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    return RagPublicationService(RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False)))


def _approval(origin: str = BENCHMARK_ORIGIN) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated refresh test",
        approval_reference="test only, not production approval",
        license_reference="fixture",
        parser_version=PRISM_PARSER if origin == PRISM_ORIGIN else PARSER_VERSION,
        allowed_paths=allowed_paths(origin),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path, publication: RagPublicationService) -> tuple[Path, RefreshPlan]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", BENCHMARK_ORIGIN)
    (repo / "docs").mkdir()
    (repo / "docs/report.md").write_text("# Report\nBenchmark workload definition.\n")
    return repo, _commit_plan(repo, publication)


def _commit_plan(repo: Path, publication: RagPublicationService) -> RefreshPlan:
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    revision = _git(repo, "rev-parse", "HEAD")
    approval = _approval()
    publication.register_source(BENCHMARK_ORIGIN, approval)
    head = publication.dao.head(BENCHMARK_ORIGIN)
    inventory = collect_git(BENCHMARK_ORIGIN, repo, revision, approval, publication.archive)
    return RefreshPlan(
        review_reference="reviewed fixture commit and counts",
        approval=approval,
        inventory=inventory,
        expected_counts={"source": len(inventory.files), "passage": len(inventory.files)},
        base_generation=head.state.generation,
        base_manifest_id=head.registry.published_manifest_id,
        triggers=("manual", "commit", "periodic"),
        allow_empty=not inventory.files,
    )


def _snapshot(tmp_path: Path, publication: RagPublicationService, revision: str, value: int = 11):
    root = tmp_path / revision
    root.mkdir()
    record = {
        "id": "historical",
        "model_name": "fixture-model",
        "timestamp": "20250923-201033",
        "metadata": {"hardware": "reported-hardware"},
        "workload": {},
        "source_info": {"file_identifier": "original.json"},
        "metrics": {"throughput": value},
    }
    documents = {
        "archives/archived_drive_data.json": json.dumps([record]),
        "archives/llm-d-benchmarks.json": "[]",
        "historical-merged-deduplicated.json": json.dumps([record]),
        "results/public-index.json": '{"items": [], "nextPageToken": null}',
        "results/promoted-index.json": '{"items": [], "nextPageToken": null}',
        "normalize-and-merge.jq": "# fixture",
    }
    files = []
    for path, text in sorted(documents.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        files.append(
            InventoryFile(
                path=path,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                format="jq" if path.endswith(".jq") else "json",
            )
        )
    approval = _approval(PRISM_ORIGIN)
    publication.register_source(PRISM_ORIGIN, approval)
    head = publication.dao.head(PRISM_ORIGIN)
    plan = RefreshPlan(
        review_reference="reviewed complete offline snapshot",
        approval=approval,
        inventory=PinnedInventory(origin_id=PRISM_ORIGIN, revision=revision, complete=True, files=tuple(files)),
        expected_counts={"source": 6, "experiment": 1, "measurement": 1},
        base_generation=head.state.generation,
        base_manifest_id=head.registry.published_manifest_id,
    )
    return root, plan


@pytest.mark.parametrize("trigger", ["manual", "commit", "periodic"])
def test_shared_triggers_and_replay(publication, tmp_path: Path, trigger: str):
    repo, plan = _repository(tmp_path, publication)
    kwargs = {"event_revision": plan.inventory.revision} if trigger == "commit" else {}
    service = RagRefreshService(publication)
    result = service.run(plan, repo, trigger=trigger, **kwargs)
    assert result.status == "published"
    assert result.plan_sha256 == plan.digest
    assert result.file_count == 1
    assert result.publication.counts == {"source": 1, "passage": 1}
    assert result.finished_at >= result.started_at
    repeated = service.run(plan, repo, trigger=trigger, **kwargs)
    assert repeated.status == "unchanged"
    assert repeated.publication.generation == result.publication.generation


def test_prism_new_snapshot_exact_answer_old_citation_and_revocation(publication, tmp_path: Path):
    runner = RagRefreshService(publication)
    root, plan = _snapshot(tmp_path, publication, "2026-10-13")
    first = runner.run(plan, root)
    old = publication.dao.list_published(PRISM_ORIGIN, kind="measurement")[0]
    newer, update = _snapshot(tmp_path, publication, "2026-10-20", value=23)
    result = runner.run(update, newer)
    assert result.publication.generation == first.publication.generation + 1
    current = publication.dao.list_published(PRISM_ORIGIN, kind="measurement")
    assert len(current) == 1 and current[0].original_value == 23
    assert publication.dao.get(old.item_id) == old
    assert b'"throughput": 11' in publication.archive.read(old.content_sha256)
    query = PublicRagQueryService(RagQueryDao(publication.dao._get_session_factory()), publication.archive)
    reader = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding(role="reader", scope_type=ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    request = QueryRequest(
        question="Reported throughput",
        intent="numeric",
        filters=QueryFilters(origins=(PRISM_ORIGIN,), metric="throughput", record_id="historical"),
    )
    answer = query.query(request, reader)
    assert len(answer.evidence) == 1 and answer.evidence[0].value == 23
    assert answer.evidence[0].citation.source_revision == "2026-10-20"
    publication.dao.set_enabled(PRISM_ORIGIN, False, expected_generation=result.publication.generation)
    assert not query.query(request, reader).evidence
    with pytest.raises(PublicationError, match="disabled"):
        runner.run(update, newer)


@pytest.mark.parametrize("fault", ["pagination", "missing", "hash", "count", "measurement-count", "parser"])
def test_prism_refresh_failure_preserves_head(publication, tmp_path: Path, fault: str):
    runner = RagRefreshService(publication)
    original, first = _snapshot(tmp_path, publication, "2026-10-13")
    runner.run(first, original)
    root, plan = _snapshot(tmp_path, publication, "2026-10-20")
    if fault == "count":
        plan = RefreshPlan.model_validate({**plan.model_dump(), "expected_counts": {"source": 6, "experiment": 2}})
    elif fault == "measurement-count":
        plan = RefreshPlan.model_validate(
            {
                **plan.model_dump(),
                "expected_counts": {"source": 6, "experiment": 1, "measurement": 2},
            }
        )
    elif fault == "missing":
        (root / "results/public-index.json").unlink()
    elif fault == "hash":
        (root / "normalize-and-merge.jq").write_text("# unreviewed")
    else:
        path = "results/public-index.json" if fault == "pagination" else "archives/archived_drive_data.json"
        target = root / path
        target.write_text('{"items": [], "nextPageToken": "more"}' if fault == "pagination" else "{")
        files = tuple(
            file.model_copy(update={"sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            if file.path == path
            else file
            for file in plan.inventory.files
        )
        plan = RefreshPlan.model_validate(
            {
                **plan.model_dump(),
                "inventory": {**plan.inventory.model_dump(), "files": files},
            }
        )
    before = publication.dao.head(PRISM_ORIGIN)
    with pytest.raises(PublicationError):
        runner.run(plan, root)
    assert publication.dao.head(PRISM_ORIGIN) == before


def test_plan_cannot_undo_rollback_or_policy_aba(publication, tmp_path: Path):
    runner = RagRefreshService(publication)
    repo, first = _repository(tmp_path, publication)
    old = runner.run(first, repo)
    (repo / "docs/report.md").write_text("# Changed\nNew workload definition.\n")
    plan = _commit_plan(repo, publication)
    new = runner.run(plan, repo)
    publication.rollback(BENCHMARK_ORIGIN, old.publication.manifest_id, expected_generation=new.publication.generation)
    with pytest.raises(PublicationConflictError, match="stale"):
        runner.run(plan, repo, trigger="periodic")
    head = publication.dao.head(BENCHMARK_ORIGIN)
    publication.dao.set_enabled(BENCHMARK_ORIGIN, False, expected_generation=head.state.generation)
    publication.dao.set_enabled(BENCHMARK_ORIGIN, True, expected_generation=head.state.generation + 1)
    with pytest.raises(PublicationConflictError, match="stale"):
        runner.run(first, repo)


def test_refresh_rejects_unreviewed_inventory_and_changed_approval(publication, tmp_path: Path):
    repo, plan = _repository(tmp_path, publication)
    runner = RagRefreshService(publication)
    runner.run(plan, repo)
    before = publication.dao.head(BENCHMARK_ORIGIN)
    (repo / "docs/report.md").write_text("# Changed\nAnother report.\n")
    update = _commit_plan(repo, publication)
    wrong_file = update.inventory.files[0].model_copy(update={"sha256": "0" * 64})
    wrong = RefreshPlan.model_validate(
        {
            **update.model_dump(),
            "inventory": {**update.inventory.model_dump(), "files": (wrong_file,)},
        }
    )
    with pytest.raises(PublicationError, match="inventory"):
        runner.run(wrong, repo)
    wrong = RefreshPlan.model_validate(
        {
            **update.model_dump(),
            "approval": {**update.approval.model_dump(), "approved_by": "unreviewed replacement"},
        }
    )
    with pytest.raises(PublicationError, match="approval differs"):
        runner.run(wrong, repo)
    assert publication.dao.head(BENCHMARK_ORIGIN) == before


def test_complete_empty_inventory_needs_explicit_review(publication, tmp_path: Path):
    repo, plan = _repository(tmp_path, publication)
    runner = RagRefreshService(publication)
    first = runner.run(plan, repo)
    old_file = publication.dao.list_published(BENCHMARK_ORIGIN, kind="source")[0]
    (repo / "docs/report.md").unlink()
    empty = _commit_plan(repo, publication)
    with pytest.raises(ValidationError, match="allow_empty"):
        RefreshPlan.model_validate({**empty.model_dump(), "allow_empty": False})
    result = runner.run(empty, repo)
    assert result.publication.counts == {}
    assert result.publication.generation == first.publication.generation + 1
    assert not publication.dao.list_published(BENCHMARK_ORIGIN)
    assert b"Benchmark workload" in publication.archive.read(old_file.content_sha256)


def test_concurrent_change_after_collection_cannot_publish(publication, tmp_path: Path, monkeypatch):
    repo, plan = _repository(tmp_path, publication)
    ready, resume = Event(), Event()
    original = publication.stage

    def blocked(*args, **kwargs):
        staged = original(*args, **kwargs)
        ready.set()
        assert resume.wait(20)
        return staged

    monkeypatch.setattr(publication, "stage", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(RagRefreshService(publication).run, plan, repo)
        try:
            assert ready.wait(20)
            publication.dao.set_enabled(BENCHMARK_ORIGIN, False, expected_generation=0)
        finally:
            resume.set()
        with pytest.raises(PublicationConflictError):
            future.result(timeout=20)
    assert publication.dao.head(BENCHMARK_ORIGIN).manifest is None


def test_policy_aba_during_noop_activation_conflicts(publication, tmp_path: Path, monkeypatch):
    repo, plan = _repository(tmp_path, publication)
    runner = RagRefreshService(publication)
    runner.run(plan, repo)
    activate = publication.activate

    def change_policy(origin, manifest_id):
        publication.dao.set_enabled(origin, False, expected_generation=1)
        publication.dao.set_enabled(origin, True, expected_generation=2)
        return activate(origin, manifest_id)

    monkeypatch.setattr(publication, "activate", change_policy)
    with pytest.raises(PublicationConflictError, match="before refresh completion"):
        runner.run(plan, repo)
    assert publication.dao.head(BENCHMARK_ORIGIN).state.generation == 3


@pytest.mark.parametrize("trigger,event_revision", [("commit", None), ("commit", "b" * 40), ("manual", "a" * 40)])
def test_commit_revision_must_match_plan(publication, tmp_path: Path, trigger, event_revision):
    repo, plan = _repository(tmp_path, publication)
    with pytest.raises(PublicationError, match="exact reviewed"):
        RagRefreshService(publication).run(plan, repo, trigger=trigger, event_revision=event_revision)
    assert publication.dao.head(BENCHMARK_ORIGIN).manifest is None


def test_invalid_plans_do_not_expand_scope(publication, tmp_path: Path):
    _, plan = _repository(tmp_path, publication)
    for update in (
        {"expected_counts": {"source": 2}},
        {"expected_counts": {"source": 1, "passage": True}},
        {"triggers": ()},
        {"figure_reviews": {"unreviewed.png": {"coverage": "unknown", "reason": "fixture"}}},
        {"inventory": {**plan.inventory.model_dump(), "complete": False}},
        {"inventory": {**plan.inventory.model_dump(), "origin_id": "https://unapproved.invalid/repo"}},
        {"inventory": {**plan.inventory.model_dump(), "files": ()}, "expected_counts": {}},
    ):
        with pytest.raises((ValidationError, PublicationError)):
            RefreshPlan.model_validate({**plan.model_dump(), **update})
    root, prism = _snapshot(tmp_path, publication, "2026-10-13")
    with pytest.raises(ValidationError, match="manual"):
        RefreshPlan.model_validate({**prism.model_dump(), "triggers": ("periodic",)})
    with pytest.raises(PublicationError, match="not approved"):
        RagRefreshService(publication).run(prism, root, trigger="periodic")


def test_cli_refresh_reports_status_rollback_and_failure(publication, postgres_engine, tmp_path, monkeypatch, capsys):
    repo, plan = _repository(tmp_path, publication)
    config = tmp_path / "plan.json"
    config.write_text(plan.model_dump_json())
    monkeypatch.setenv("TEST_RAG_REFRESH_URL", postgres_engine.url.render_as_string(hide_password=False))
    schema = postgres_engine.url.query["options"].removeprefix("-csearch_path=")
    args = ["--database-url-env", "TEST_RAG_REFRESH_URL", "--schema", schema, "--data-dir", str(tmp_path / "evidence")]
    assert main(["refresh", *args, "--plan", str(config), "--source", str(repo)]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["status"] == "published"
    assert main(["status", *args, "--origin", BENCHMARK_ORIGIN]) == 0
    assert json.loads(capsys.readouterr().out)["state"]["generation"] == 1
    (repo / "docs/report.md").write_text("# New\nAnother workload definition.\n")
    update = _commit_plan(repo, publication)
    config.write_text(update.model_dump_json())
    assert (
        main(
            [
                "refresh",
                *args,
                "--plan",
                str(config),
                "--source",
                str(repo),
                "--trigger",
                "commit",
                "--event-revision",
                update.inventory.revision,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "published"
    assert (
        main(
            [
                "rollback",
                *args,
                "--origin",
                BENCHMARK_ORIGIN,
                "--manifest-id",
                published["publication"]["manifest_id"],
                "--expected-generation",
                "2",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["generation"] == 3
    assert main(["refresh", *args, "--plan", str(config), "--source", str(repo), "--trigger", "periodic"]) == 1
    failed = capsys.readouterr()
    assert failed.out == ""
    assert json.loads(failed.err)["status"] == "failed"
    assert json.loads(failed.err)["error_type"] == "PublicationConflictError"
