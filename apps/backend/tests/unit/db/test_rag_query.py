from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, event
from sqlalchemy.orm import sessionmaker

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.contracts import RagItem, normalize_unit
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    SourceApproval,
    item_id,
)
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.repository_corpus import parse_markdown
from llm_d_lens.benchmark_rag.service import (
    EvidenceUnavailableError,
    PublicRagQueryService,
    _verify_measurement,
    plan_query,
)
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, PRISM_ORIGIN, allowed_paths
from llm_d_lens.core.exceptions import ConflictError, ForbiddenError, UnauthenticatedError
from llm_d_lens.db.dao.rag_query import QueryMatch, RagQueryDao

_PATH = "archives/llm-d-benchmarks.json"
_REVISION = "2026-10-13"
_GUIDE_REVISION = "a" * 40
_RAW = (
    b'{"runs":[{"id":"run-a","throughput":6131.06177725188,"latency":1.234567890123456789},'
    b'{"id":"run-b","throughput":99,"latency":0.5}]}'
)


def reader() -> Principal:
    return Principal(
        user_id="rag-reader",
        username="reader",
        bindings=(RoleBinding(role="custom", scope_type=ScopeType.GLOBAL),),
        role_permissions={"custom": frozenset({QUERY_PERMISSION})},
    )


def approval(origin: str) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated query fixture",
        approval_reference="fixture only",
        license_reference="not production approval",
        parser_version="query-fixture-v1",
        allowed_paths=allowed_paths(origin),
    )


def publish_numeric(publication: RagPublicationService, revision: str = _REVISION, *, activate: bool = True):
    digest = publication.archive.put(_RAW)
    file = InventoryFile(path=_PATH, sha256=digest, format="json")
    inventory = PinnedInventory(origin_id=PRISM_ORIGIN, revision=revision, complete=True, files=(file,))
    items = []
    for index, (identity, hardware, throughput, latency, unit) in enumerate(
        (
            ("run-a", "H100", Decimal("6131.06177725188"), Decimal("1.234567890123456789"), "output tokens/s"),
            ("run-b", "H200", Decimal("99"), Decimal("0.5"), "tokens/s"),
        )
    ):
        key = f"{_PATH}#experiment:{identity}"
        experiment = RagItem(
            item_id=item_id(PRISM_ORIGIN, key, revision),
            logical_key=key,
            kind="experiment",
            origin_id=PRISM_ORIGIN,
            source_revision=revision,
            content_sha256=digest,
            access_classification="verified_public",
            source_path=_PATH,
            format="json",
            parent_id=item_id(PRISM_ORIGIN, f"file:{_PATH}", revision),
            model="Qwen3-0.6B",
            hardware=hardware,
            runtime="reported-runtime",
            workload={"concurrency": 4 if index == 0 else None},
            configuration={"scenario": identity},
            source_pointer={"record_id": identity, "json_pointer": f"/runs/{index}"},
        )
        items.append(experiment)
        for metric, value, measurement_unit in (("throughput", throughput, unit), ("latency", latency, "s")):
            key = f"{_PATH}#measurement:{identity}:{metric}"
            normalized = normalize_unit(value, measurement_unit)
            items.append(
                RagItem(
                    item_id=item_id(PRISM_ORIGIN, key, revision),
                    logical_key=key,
                    kind="measurement",
                    origin_id=PRISM_ORIGIN,
                    source_revision=revision,
                    content_sha256=digest,
                    access_classification="verified_public",
                    source_path=_PATH,
                    format="json",
                    parent_id=experiment.item_id,
                    experiment_id=experiment.item_id,
                    metric=metric,
                    statistic="mean",
                    value=value,
                    original_value=value,
                    unit=measurement_unit,
                    original_unit=measurement_unit,
                    normalized_value=normalized[0] if normalized else None,
                    normalized_unit=normalized[1] if normalized else None,
                    normalization_rule=normalized[2] if normalized else None,
                    evidence_type="raw_json",
                    series={"scenario": identity},
                    source_pointer={"record_id": identity, "json_pointer": f"/runs/{index}/{metric}"},
                )
            )
    publication.register_source(PRISM_ORIGIN, approval(PRISM_ORIGIN))
    manifest = publication.stage(
        inventory,
        (ParsedFile(path=_PATH, items=tuple(items)),),
        expected_counts={"source": 1, "experiment": 2, "measurement": 4},
    )
    return publication.activate(PRISM_ORIGIN, manifest.item_id) if activate else manifest


def publish_context(publication: RagPublicationService):
    text = (
        "# Benchmark Report\nA benchmark report captures aggregate performance statistics for a workload "
        "and environment, rather than detailed traces for individual requests.\n"
        '```json\n{"throughput": 999999999}\n```\n'
    )
    path = "docs/benchmark_report.md"
    data = text.encode()
    file = InventoryFile(path=path, sha256=publication.archive.put(data), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision=_GUIDE_REVISION, complete=True, files=(file,))
    parsed = parse_markdown(file, inventory, data)
    publication.register_source(BENCHMARK_ORIGIN, approval(BENCHMARK_ORIGIN))
    manifest = publication.stage(
        inventory, (ParsedFile(path=path, items=parsed),), expected_counts={"source": 1, "passage": 1}
    )
    publication.activate(BENCHMARK_ORIGIN, manifest.item_id)
    path = "guides/agentic-serving/benchmark-results/latency_vs_throughput.png"
    file = InventoryFile(path=path, sha256=publication.archive.put(b"\x89PNG\r\n\x1a\n"), format="png")
    inventory = PinnedInventory(origin_id=LLM_D_ORIGIN, revision=_GUIDE_REVISION, complete=True, files=(file,))
    key = f"{path}#figure"
    figure = RagItem(
        item_id=item_id(LLM_D_ORIGIN, key, _GUIDE_REVISION),
        kind="figure",
        logical_key=key,
        origin_id=LLM_D_ORIGIN,
        source_revision=_GUIDE_REVISION,
        content_sha256=file.sha256,
        access_classification="verified_public",
        source_path=path,
        format="png",
        parent_id=item_id(LLM_D_ORIGIN, f"file:{path}", _GUIDE_REVISION),
        caption="Latency versus throughput",
        coverage="summary_only",
        source_pointer={"numeric_extraction": "none", "references": []},
    )
    publication.register_source(LLM_D_ORIGIN, approval(LLM_D_ORIGIN))
    image_items = [figure]
    for index in range(2):
        key = f"{path}#fixture-experiment:{index}"
        experiment = RagItem(
            item_id=item_id(LLM_D_ORIGIN, key, _GUIDE_REVISION),
            logical_key=key,
            kind="experiment",
            origin_id=LLM_D_ORIGIN,
            source_revision=_GUIDE_REVISION,
            content_sha256=file.sha256,
            access_classification="verified_public",
            source_path=path,
            format="png",
            parent_id=figure.parent_id,
            model="fixture-model",
            hardware="H100",
            workload={"concurrency": index + 1},
            source_pointer={"fixture": index},
        )
        key = f"{path}#fixture-link:{index}"
        image_items.extend(
            (
                experiment,
                RagItem(
                    item_id=item_id(LLM_D_ORIGIN, key, _GUIDE_REVISION),
                    logical_key=key,
                    kind="figure_link",
                    origin_id=LLM_D_ORIGIN,
                    source_revision=_GUIDE_REVISION,
                    content_sha256=file.sha256,
                    access_classification="verified_public",
                    source_path=path,
                    format="png",
                    parent_id=figure.item_id,
                    referenced_item_id=experiment.item_id,
                    source_pointer={"basis": "explicit isolated fixture"},
                ),
            )
        )
    manifest = publication.stage(
        inventory,
        (ParsedFile(path=path, items=tuple(image_items)),),
        expected_counts={"source": 1, "figure": 1, "experiment": 2, "figure_link": 2},
    )
    publication.activate(LLM_D_ORIGIN, manifest.item_id)


@pytest.fixture
def query_service(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "query-evidence"))
    dao = RagQueryDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))
    service = PublicRagQueryService(dao=dao)
    publication = RagPublicationService(dao=dao, archive=service.archive)
    publish_numeric(publication)
    publish_context(publication)
    return service


def numeric(**fields) -> QueryRequest:
    return QueryRequest(
        question="Reported throughput",
        intent="numeric",
        filters=QueryFilters(record_id="run-a", metric="throughput", **fields),
    )


@pytest.mark.parametrize(
    "principal",
    [
        None,
        Principal(user_id="system", username="system", authz_version=0),
        Principal(user_id="unprivileged", username="user"),
        replace(
            reader(), bindings=(RoleBinding(role="custom", scope_type=ScopeType.CLUSTER, scope_cluster_id="cluster-a"),)
        ),
    ],
)
def test_service_authorization_precedes_every_read(principal):
    class NeverRead:
        def search(self, *args, **kwargs):
            pytest.fail("unauthorized request reached storage")

    service = PublicRagQueryService(dao=NeverRead())
    with pytest.raises((UnauthenticatedError, ForbiddenError)):
        service.query(numeric(), principal)


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "x", "principal": {"user_id": "admin"}},
        {"question": "x", "filters": {"origins": ["private:secret"]}},
        {"question": "x", "filters": {"value_min": "1"}},
        {"question": "x", "filters": {"unit": "s", "value_min": "2", "value_max": "1"}},
        {"question": "x", "filters": {"source_path": "../escape"}},
        {"question": "x", "limit": 51},
        {"question": " "},
    ],
)
def test_query_contract_rejects_invalid_or_unscoped_payloads(payload):
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(payload)


def test_numeric_values_units_and_conditions_are_exact(query_service, postgres_engine):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        result = query_service.query(numeric(), reader())
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert result.status == "answered"
    assert result.matched_items == 1
    evidence = result.evidence[0]
    assert evidence.value == Decimal("6131.06177725188")
    assert evidence.unit == "output tokens/s"
    assert evidence.experiment.model == "Qwen3-0.6B"
    assert evidence.experiment.workload == {"concurrency": 4}
    assert evidence.citation.source_pointer["json_pointer"] == "/runs/0/throughput"
    assert evidence.citation.sha256 == hashlib.sha256(_RAW).hexdigest()
    assert "6131.06177725188" in result.answer
    assert result.internal_branch_enabled is False
    assert not any("agentic_benchmark_records" in statement for statement in statements)
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)


def test_required_unknown_condition_and_denominators_do_not_match(query_service):
    assert query_service.query(numeric(hardware="unreported accelerator"), reader()).matched_items == 0
    assert query_service.query(numeric(unit="tokens/s"), reader()).matched_items == 0
    assert query_service.query(numeric(workload={"concurrency": 8}), reader()).matched_items == 0
    assert query_service.query(numeric(series={"scenario": "run-b"}), reader()).matched_items == 0
    assert query_service.query(numeric(series={"scenario": "run-a"}), reader()).matched_items == 1
    result = query_service.query(numeric(source_revision="2026-10-20"), reader())
    assert result.status == "abstained" and not result.evidence


@pytest.mark.parametrize(
    "question",
    [
        "What was the p99 latency for Qwen3-0.6B on H100?",
        "What was the throughput for Llama-3.1-70B on H100?",
    ],
)
def test_question_conditions_cannot_return_a_different_statistic_or_model(query_service, question):
    result = query_service.query(QueryRequest(question=question), reader())
    assert result.status == "abstained"
    assert result.matched_items == 0
    assert not result.evidence
    assert [item.code for item in result.abstentions] == ["no_exact_match"]


def test_question_mean_statistic_still_returns_matching_measurement(query_service):
    result = query_service.query(QueryRequest(question="What was the mean latency for Qwen3-0.6B on H100?"), reader())
    assert result.status == "answered"
    assert result.filters.statistic == "mean"
    assert result.evidence[0].statistic == "mean"
    assert result.evidence[0].value == Decimal("1.234567890123456789")


def test_numeric_normalization_range_and_statistic_alias(query_service):
    result = query_service.query(
        QueryRequest(
            question="Latency under the explicit conditions",
            intent="numeric",
            filters=QueryFilters(
                record_id="run-a",
                metric="latency",
                statistic="avg",
                normalized=True,
                unit="ms",
                value_min=Decimal("1200"),
                value_max=Decimal("1300"),
            ),
        ),
        reader(),
    )
    assert result.matched_items == 1
    assert result.evidence[0].value == Decimal("1.234567890123456789")
    assert result.evidence[0].unit == "s"
    assert result.evidence[0].normalized_value == Decimal("1234.567890123456789000")


def test_fulltext_definition_is_not_example_measurement(query_service):
    result = query_service.query(QueryRequest(question="What is a benchmark report intended to capture?"), reader())
    assert result.status == "answered" and result.evidence
    assert all(item.evidence_type == "prose_summary" and item.value is None for item in result.evidence)
    assert "aggregate performance statistics" in result.answer
    no_measurement = query_service.query(
        QueryRequest(
            question="Example throughput",
            intent="numeric",
            filters=QueryFilters(
                origins=(BENCHMARK_ORIGIN,), source_path="docs/benchmark_report.md", metric="throughput"
            ),
        ),
        reader(),
    )
    assert no_measurement.matched_items == 0


def test_metric_definition_retrieves_pinned_prose_instead_of_requiring_experiment(query_service):
    publication = RagPublicationService(dao=query_service.dao, archive=query_service.archive)
    data = b"# TTFT\nTTFT is time to first token, the delay before the first output token.\n"
    path = "docs/benchmark_report.md"
    file = InventoryFile(path=path, sha256=publication.archive.put(data), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="b" * 40, complete=True, files=(file,))
    manifest = publication.stage(
        inventory,
        (ParsedFile(path=path, items=parse_markdown(file, inventory, data)),),
        expected_counts={"source": 1, "passage": 1},
    )
    publication.activate(BENCHMARK_ORIGIN, manifest.item_id)
    result = query_service.query(QueryRequest(question="What is TTFT?"), reader())
    assert result.intent == "definition"
    assert result.status == "answered"
    assert result.matched_items == 1
    assert result.evidence[0].evidence_type == "prose_summary"
    assert result.evidence[0].value is None
    assert "time to first token" in result.answer


def test_exact_plot_points_abstain_with_image_context(query_service):
    result = query_service.query(
        QueryRequest(
            question="Can the exact unreported points in the Qwen3 Coder TPU latency-versus-throughput PNG be listed?"
        ),
        reader(),
    )
    assert result.intent == "figure" and result.status == "abstained"
    assert result.matched_items == 1 and result.evidence[0].evidence_type == "image_trend"
    assert result.evidence[0].value is None
    assert any(item.code == "plot_points_not_reconstructed" for item in result.abstentions)


def test_figure_conditions_use_published_links_without_duplicate_counts(query_service):
    result = query_service.query(
        QueryRequest(
            question="Explicitly linked figure scenarios",
            intent="figure",
            filters=QueryFilters(origins=(LLM_D_ORIGIN,), hardware="H100"),
        ),
        reader(),
    )
    assert result.matched_items == 1 and len(result.evidence) == 1
    assert len(result.evidence[0].related_experiments) == 2
    assert {context.workload["concurrency"] for context in result.evidence[0].related_experiments} == {1, 2}
    missing = query_service.query(
        QueryRequest(
            question="A required condition has no verified link",
            intent="figure",
            filters=QueryFilters(origins=(LLM_D_ORIGIN,), hardware="H200"),
        ),
        reader(),
    )
    assert missing.matched_items == 0 and not missing.evidence


def test_counts_are_filtered_before_limit_and_source_revocation(query_service):
    result = query_service.query(
        QueryRequest(
            question="List matching measurements",
            intent="numeric",
            limit=1,
            filters=QueryFilters(model="Qwen3-0.6B", metric="throughput"),
        ),
        reader(),
    )
    assert result.matched_items == 2 and len(result.evidence) == 1 and result.truncated is True
    head = query_service.dao.head(PRISM_ORIGIN)
    query_service.dao.set_enabled(PRISM_ORIGIN, False, expected_generation=head.state.generation)
    hidden = query_service.query(numeric(), reader())
    assert hidden.matched_items == 0 and not hidden.evidence


def test_revocation_during_evidence_read_discards_entire_response(query_service):
    original = query_service.archive.read

    def revoke(digest):
        data = original(digest)
        head = query_service.dao.head(PRISM_ORIGIN)
        query_service.dao.set_enabled(PRISM_ORIGIN, False, expected_generation=head.state.generation)
        return data

    query_service.archive.read = revoke
    with pytest.raises(ConflictError):
        query_service.query(numeric(), reader())


def test_missing_or_corrupt_bytes_are_errors_not_empty_success(query_service):
    root = query_service.archive._root(hashlib.sha256(_RAW).hexdigest())
    (root / "source").write_bytes(b"corrupt")
    with pytest.raises(EvidenceUnavailableError):
        query_service.query(numeric(), reader())


def test_manifest_membership_not_per_row_published_flag(query_service):
    original = query_service.dao.list_published(PRISM_ORIGIN, kind="measurement")[0]
    key = f"{_PATH}#unmanifested"
    forged = original.model_copy(
        update={
            "item_id": item_id(PRISM_ORIGIN, key, _REVISION),
            "logical_key": key,
            "publication_state": "published",
        }
    )
    query_service.dao.save(forged)
    result = query_service.query(numeric(), reader())
    assert result.matched_items == 1
    assert forged.item_id not in {item.item_id for item in result.evidence}


def test_old_release_and_generation_behavior(query_service):
    publication = RagPublicationService(dao=query_service.dao, archive=query_service.archive)
    old = query_service.dao.head(PRISM_ORIGIN)
    publish_numeric(publication, revision="2026-10-20", activate=False)
    assert query_service.query(numeric(source_revision="2026-10-20"), reader()).matched_items == 0
    assert query_service.query(numeric(source_revision=_REVISION), reader()).matched_items == 1
    publish_numeric(publication, revision="2026-10-20")
    assert query_service.query(numeric(source_revision=_REVISION), reader()).matched_items == 0
    head = query_service.dao.head(PRISM_ORIGIN)
    publication.rollback(PRISM_ORIGIN, old.registry.published_manifest_id, expected_generation=head.state.generation)
    assert query_service.query(numeric(source_revision=_REVISION), reader()).matched_items == 1


def test_question_plans_are_auditable_and_not_inferred_from_unrelated_permissions():
    request = QueryRequest(
        question="What was the recorded throughput for Prism ID 02c8f038-1cad-4ee8-a72e-724affa923f4?"
    )
    plan = plan_query(request)
    assert plan.filters.origins == (PRISM_ORIGIN,)
    assert plan.filters.record_id == "02c8f038-1cad-4ee8-a72e-724affa923f4"
    assert plan.filters.metric == "throughput"
    assert plan_query(QueryRequest(question="Throughput for an unspecified model")).precondition is not None
    assert plan_query(QueryRequest(question="Show a diagram")).intent == "figure"
    assert plan_query(QueryRequest(question="Compare H200 weight loading times")).intent == "comparison"
    assert plan_query(QueryRequest(question="Show internal records")).precondition.code == "internal_branch_disabled"


def test_rest_uses_identical_database_evidence_contract(query_service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from llm_d_lens.benchmark_rag import router

    monkeypatch.setattr(router, "get_service", lambda: query_service)
    monkeypatch.setattr(router, "current_principal", lambda _request: reader())
    app = FastAPI()
    app.include_router(router.router)
    request = numeric()
    expected = query_service.query(request, reader()).model_dump(mode="json")
    with TestClient(app) as client:
        response = client.post("/api/v1/benchmark-rag/query", json=request.model_dump(mode="json", exclude_unset=True))
    assert response.status_code == 200
    assert response.json() == expected
    assert response.json()["evidence"][0]["value"] == "6131.06177725188"


def test_real_three_corpus_answer_acceptance(postgres_engine, tmp_path, monkeypatch):
    from llm_d_lens.benchmark_rag.prism import PARSER_VERSION as PRISM_PARSER_VERSION
    from llm_d_lens.benchmark_rag.prism import PrismSnapshotImporter
    from llm_d_lens.benchmark_rag.prism_initial import INITIAL_EXPERIMENTS, INITIAL_FILES, INITIAL_REVISION
    from llm_d_lens.benchmark_rag.repository_corpus import PARSER_VERSION, RepositoryCorpusImporter
    from llm_d_lens.benchmark_rag.repository_initial import (
        INITIAL_INVENTORY_DIGESTS,
        INITIAL_ITEM_COUNTS,
        INITIAL_REVISIONS,
        initial_figure_reviews,
    )

    root = Path(__file__).resolve().parents[5]
    snapshot = root / "private/prism-public-2026-10-13"
    repos = {LLM_D_ORIGIN: root.parent / "llm-d", BENCHMARK_ORIGIN: root.parent / "llm-d-benchmark"}
    if not snapshot.exists() or any(not (repo / ".git").exists() for repo in repos.values()):
        pytest.skip("real three-corpus acceptance needs the separately provisioned pinned local inputs")
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "real-query-evidence"))
    dao = RagQueryDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))
    service = PublicRagQueryService(dao=dao)
    publication = RagPublicationService(dao=dao, archive=service.archive)
    PrismSnapshotImporter(publication).import_snapshot(
        snapshot,
        INITIAL_REVISION,
        INITIAL_FILES,
        approval(PRISM_ORIGIN).model_copy(update={"parser_version": PRISM_PARSER_VERSION}),
        expected_experiments=INITIAL_EXPERIMENTS,
    )
    importer = RepositoryCorpusImporter(publication)
    for origin, repo in repos.items():
        importer.import_repository(
            repo,
            origin,
            INITIAL_REVISIONS[origin],
            approval(origin).model_copy(update={"parser_version": PARSER_VERSION}),
            expected_counts=INITIAL_ITEM_COUNTS[origin],
            expected_inventory_digest=INITIAL_INVENTORY_DIGESTS[origin],
            figure_reviews=initial_figure_reviews() if origin == LLM_D_ORIGIN else {},
        )
    prism = service.query(
        QueryRequest(question="What was the recorded throughput for Prism ID 02c8f038-1cad-4ee8-a72e-724affa923f4?"),
        reader(),
    )
    assert prism.status == "answered" and prism.matched_items == 1
    assert prism.evidence[0].value == Decimal("6131.06177725188")
    assert prism.evidence[0].unit == "output tokens/s"
    assert prism.evidence[0].experiment.model == "Qwen3-0.6B"
    weight = service.query(QueryRequest(question="Compare H200 gpt-oss-120b weight-load times", limit=50), reader())
    assert weight.status == "answered"
    assert {Decimal("0.93"), Decimal("30.3")} <= {entry.value for entry in weight.evidence}
    assert all(entry.unit == "s" and entry.evidence_type == "published_table" for entry in weight.evidence)
    conditions = json.dumps([entry.experiment.configuration for entry in weight.evidence]).lower()
    assert "nfs" in conditions and "warm" in conditions and "node" in conditions
    definition = service.query(QueryRequest(question="What is a benchmark report intended to capture?"), reader())
    assert definition.status == "answered"
    assert "aggregate benchmark results" in definition.answer
    assert "capture performance statistics" in definition.answer
    assert "rather than detailed traces" in definition.answer
    assert all(entry.value is None for entry in definition.evidence)
    plot = service.query(
        QueryRequest(question="Can the exact missing Qwen3 Coder TPU PNG latency-versus-throughput points be listed?"),
        reader(),
    )
    assert plot.status == "abstained" and plot.matched_items == 1
    assert plot.evidence[0].coverage == "summary_only" and plot.evidence[0].value is None

    figures = service.query(
        QueryRequest(
            question="Show explicitly linked H100 figures",
            intent="figure",
            filters=QueryFilters(origins=(LLM_D_ORIGIN,), hardware="H100"),
            limit=50,
        ),
        reader(),
    )
    assert figures.evidence
    assert len({entry.item_id for entry in figures.evidence}) == figures.matched_items
    assert all(entry.related_experiments for entry in figures.evidence)
    assert all(context.hardware == "H100" for entry in figures.evidence for context in entry.related_experiments)
    hidden_condition = service.query(
        QueryRequest(
            question="Show figures under an unknown condition",
            intent="figure",
            filters=QueryFilters(origins=(LLM_D_ORIGIN,), hardware="unreported hardware"),
        ),
        reader(),
    )
    assert hidden_condition.matched_items == 0 and not hidden_condition.evidence

    # Validate the runtime verifier against every reviewed table/JSON measurement, not only the answer samples.
    published = dao.list_published(LLM_D_ORIGIN)
    files = {item.source_path: item for item in published if item.source_role == "file"}
    head = dao.head(LLM_D_ORIGIN)
    cache = {}
    checked = 0
    for item in published:
        if item.kind != "measurement":
            continue
        if item.content_sha256 not in cache:
            cache[item.content_sha256] = service.archive.read(item.content_sha256)
        _verify_measurement(
            QueryMatch(
                item=item,
                experiment=None,
                source=files[item.source_path],
                manifest_id=head.registry.published_manifest_id,
                generation=head.state.generation,
            ),
            cache[item.content_sha256],
        )
        checked += 1
    assert checked == 2153
