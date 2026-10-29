"""Structured SQL refresh; document lifecycle coverage lives in benchmark_rag."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.admin import _parser
from llm_d_lens.benchmark_rag.prism import PARSER_VERSION as PRISM_PARSER
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    PinnedInventory,
    PublicationConflictError,
    PublicationError,
    SourceApproval,
    content_digest,
)
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.refresh import RagRefreshService, RefreshPlan
from llm_d_lens.benchmark_rag.service import PublicRagQueryService
from llm_d_lens.benchmark_rag.sources import (
    PRISM_ORIGIN,
    WEB_SOURCE_PATHS,
    allowed_paths,
    input_format,
    validate_approval,
    web_source_url,
)
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.dao.rag_query import RagQueryDao


@pytest.fixture
def publication(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    return RagPublicationService(RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False)))


def _approval(origin: str = PRISM_ORIGIN) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated refresh test",
        approval_reference="test only, not production approval",
        license_reference="fixture",
        parser_version=PRISM_PARSER if origin == PRISM_ORIGIN else "unavailable-document-parser",
        allowed_paths=allowed_paths(origin),
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
    approval = _approval()
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


def test_refresh_cli_allows_managed_repository_source():
    args = _parser().parse_args(["refresh", "--data-dir", "/srv/lens-data", "--plan", "/secure/approved-plan.json"])
    assert args.source is None


def test_prism_refresh_still_requires_local_snapshot(publication, tmp_path):
    _, plan = _snapshot(tmp_path, publication, "2026-10-13")
    with pytest.raises(PublicationError, match="explicit local snapshot"):
        RagRefreshService(publication).run(plan)
    assert publication.dao.head(PRISM_ORIGIN).manifest is None


def test_prism_refresh_replay_is_idempotent(publication, tmp_path):
    root, plan = _snapshot(tmp_path, publication, "2026-10-13")
    runner = RagRefreshService(publication)
    first = runner.run(plan, root)
    repeated = runner.run(plan, root)
    assert first.status == "published" and repeated.status == "unchanged"
    assert repeated.publication.generation == first.publication.generation


def test_prism_new_snapshot_exact_answer_old_citation_and_revocation(publication, tmp_path):
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
def test_prism_refresh_failure_preserves_head(publication, tmp_path, fault):
    runner = RagRefreshService(publication)
    original, first = _snapshot(tmp_path, publication, "2026-10-13")
    runner.run(first, original)
    root, plan = _snapshot(tmp_path, publication, "2026-10-20")
    if fault == "count":
        plan = RefreshPlan.model_validate({**plan.model_dump(), "expected_counts": {"source": 6, "experiment": 2}})
    elif fault == "measurement-count":
        plan = RefreshPlan.model_validate(
            {**plan.model_dump(), "expected_counts": {"source": 6, "experiment": 1, "measurement": 2}}
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
            {**plan.model_dump(), "inventory": {**plan.inventory.model_dump(), "files": files}}
        )
    before = publication.dao.head(PRISM_ORIGIN)
    with pytest.raises(PublicationError):
        runner.run(plan, root)
    assert publication.dao.head(PRISM_ORIGIN) == before


def test_reviewed_snapshot_cannot_undo_rollback_or_policy_aba(publication, tmp_path):
    root, first = _snapshot(tmp_path, publication, "2026-10-13")
    runner = RagRefreshService(publication)
    runner.run(first, root)
    newer, update = _snapshot(tmp_path, publication, "2026-10-20")
    head = publication.dao.head(PRISM_ORIGIN)
    publication.dao.set_enabled(PRISM_ORIGIN, False, expected_generation=head.state.generation)
    publication.dao.set_enabled(PRISM_ORIGIN, True, expected_generation=head.state.generation + 1)
    with pytest.raises(PublicationConflictError):
        runner.run(update, newer)


def test_invalid_plans_do_not_expand_scope(publication, tmp_path):
    root, plan = _snapshot(tmp_path, publication, "2026-10-13")
    for update in (
        {"expected_counts": {"source": 2}},
        {"expected_counts": {"source": 6, "measurement": True}},
        {"triggers": ()},
        {"triggers": ("periodic",)},
        {"inventory": {**plan.inventory.model_dump(), "complete": False}},
        {"inventory": {**plan.inventory.model_dump(), "origin_id": "https://unapproved.invalid/repo"}},
        {"inventory": {**plan.inventory.model_dump(), "files": ()}, "expected_counts": {}},
    ):
        with pytest.raises((ValidationError, PublicationError)):
            RefreshPlan.model_validate({**plan.model_dump(), **update})
    with pytest.raises(PublicationError, match="not approved"):
        RagRefreshService(publication).run(plan, root, trigger="periodic")


@pytest.mark.parametrize(
    "origin,path",
    [
        ("https://llm-d.ai.evil.invalid", "blog/article.md"),
        ("http://llm-d.ai", "blog/article.md"),
        ("https://medium.com", "unapproved-publication/article.md"),
        ("https://github.com", "unapproved/repo/blob/main/report.md"),
        ("https://cloud.google.com", "account/private.md"),
        ("https://llm-d.ai", "blog/../../private.md"),
        ("https://llm-d.ai", "blog/raw.json"),
    ],
)
def test_web_source_scope_does_not_approve_arbitrary_sites_or_paths(origin, path):
    with pytest.raises(PublicationError):
        web_source_url(origin, path)
    if origin in WEB_SOURCE_PATHS and ".." not in path:
        assert input_format(origin, path) is None


def test_web_approval_can_narrow_to_reviewed_articles_but_cannot_expand_scope():
    origin = "https://llm-d.ai"
    path = "blog/kvcache-wins-you-can-see.md"
    approval = _approval(origin).model_copy(update={"allowed_paths": (path,)})
    validate_approval(origin, approval)
    assert input_format(origin, path, approval) == "markdown"
    assert input_format(origin, "blog/unreviewed.md", approval) is None
    for paths in (("**/*.md",), ("private/article.md",), (path, path)):
        with pytest.raises(PublicationError):
            validate_approval(origin, approval.model_copy(update={"allowed_paths": paths}))


@pytest.mark.parametrize(
    "path",
    [
        "kserve/website/blob/main/blog/2026-04-21-production-grade-llm-inference-kserve-llm-d-vllm.md",
        "llm-d/llm-d-kv-cache/blob/main/benchmarking/73-capacity/README.md",
    ],
)
def test_github_web_source_url_preserves_markdown_extension(path):
    origin = "https://github.com"
    assert input_format(origin, path) == "markdown"
    assert web_source_url(origin, path) == f"{origin}/{path}"


def test_web_document_refresh_cannot_repopulate_sql(publication, tmp_path):
    origin = "https://llm-d.ai"
    path = "blog/kvcache-wins-you-can-see.md"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("# llm-d\nPublic document context.\n")
    file = InventoryFile(path=path, sha256=hashlib.sha256(target.read_bytes()).hexdigest(), format="markdown")
    approval = _approval(origin)
    with pytest.raises(ValidationError, match="document provider is not installed"):
        RefreshPlan(
            review_reference="reviewed document-only snapshot",
            approval=approval,
            inventory=PinnedInventory(
                origin_id=origin, revision=content_digest({path: file.sha256}), complete=True, files=(file,)
            ),
            expected_counts={"source": 1, "passage": 1},
            base_generation=0,
            base_manifest_id=None,
        )
