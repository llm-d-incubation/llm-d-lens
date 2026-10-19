from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from llm_d_lens.agentic.benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
    WorkloadProfile,
)
from llm_d_lens.auth.contracts import Principal, RoleBinding
from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    SourceApproval,
    item_id,
)
from llm_d_lens.benchmark_rag.query_contracts import (
    QUERY_PERMISSION,
    AgenticCitation,
    QueryFilters,
    QueryRequest,
)
from llm_d_lens.benchmark_rag.service import PublicRagQueryService
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN, allowed_paths
from llm_d_lens.core.exceptions import ConflictError
from llm_d_lens.db.dao.agentic_benchmark_record import BenchmarkRecordDao
from llm_d_lens.db.dao.rag_query import RagQueryDao
from llm_d_lens.db.models.agentic_benchmark_record import BenchmarkRecordRow
from llm_d_lens.db.models.evaluate_run import EvaluateRunRow
from llm_d_lens.db.models.user import UserRow

PATH = "archives/llm-d-benchmarks.json"
REVISION = "2026-10-13"


def record(run_id: str, *, value: float = 42) -> BenchmarkRecord:
    return BenchmarkRecord(
        benchmark_id=f"evaluate:{run_id}",
        timestamp=datetime(2026, 10, 19, tzinfo=UTC),
        outcome="succeeded",
        model=ModelDescriptor(
            model_id="Qwen3-0.6B",
            family="Qwen",
            architecture="dense",
            parameter_count_b=0.6,
            quantization="bf16",
            tokenizer_id="Qwen3-0.6B",
        ),
        hardware=HardwareDescriptor(accelerator="H100", vram_per_gpu_gib=80, gpu_count=1),
        runtime=RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
        workload=WorkloadProfile(
            mean_input_tokens=100,
            p95_input_tokens=200,
            mean_output_tokens=50,
            concurrency=4,
            request_rate=1,
            shared_prefix_ratio=0,
            prefill_fraction=0,
        ),
        configuration=CandidateConfiguration(topology="baseline", decode_tp=1, decode_replicas=1),
        metrics=BenchmarkMetrics(throughput_tokens_per_s=value),
    )


@pytest.fixture
def branch(postgres_engine, tmp_path, monkeypatch):
    from llm_d_lens.db.dao.rag_agentic_query import RagAgenticQueryDao

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "agentic-service"))
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    user_ids = (str(uuid4()), str(uuid4()))
    run_ids = (str(uuid4()), str(uuid4()))
    with factory.begin() as session:
        session.add_all(UserRow(id=user_id, username=f"user-{index}") for index, user_id in enumerate(user_ids))
        session.flush()
        session.add_all(
            EvaluateRunRow(
                id=run_id,
                kind="benchmark",
                status="succeeded",
                cluster_id="cluster1",
                extra={"owner_user_id": owner_id},
            )
            for run_id, owner_id in zip(run_ids, user_ids, strict=True)
        )
    for run_id in run_ids:
        BenchmarkRecordDao(factory).save(record(run_id))
    principal = Principal(
        user_id=user_ids[0],
        username="user-0",
        bindings=(RoleBinding("rag"), RoleBinding("self")),
        role_permissions={"rag": frozenset({QUERY_PERMISSION}), "self": frozenset({"evaluate:run:read"})},
        role_self_scoped={"self": frozenset({"evaluate:run:read"})},
    )
    service = PublicRagQueryService(dao=RagQueryDao(factory), agentic_dao=RagAgenticQueryDao(factory))
    return service, principal, factory, run_ids, user_ids


def request(*, internal=True, **filters):
    return QueryRequest(
        question="Reported throughput",
        intent="numeric",
        include_internal=internal,
        filters=QueryFilters(model="Qwen3-0.6B", metric="throughput", **filters),
    )


def publish(service, run_id: str):
    identity = f"evaluate:{run_id}"
    data = json.dumps([{"id": identity, "throughput": 42}]).encode()
    digest = service.archive.put(data)
    inventory = PinnedInventory(
        origin_id=PRISM_ORIGIN,
        revision=REVISION,
        complete=True,
        files=(InventoryFile(path=PATH, sha256=digest, format="json"),),
    )
    common = {
        "origin_id": PRISM_ORIGIN,
        "source_revision": REVISION,
        "source_path": PATH,
        "content_sha256": digest,
        "access_classification": "verified_public",
        "format": "json",
    }
    key = f"{PATH}#experiment"
    experiment = RagItem(
        **common,
        kind="experiment",
        item_id=item_id(PRISM_ORIGIN, key, REVISION),
        logical_key=key,
        parent_id=item_id(PRISM_ORIGIN, f"file:{PATH}", REVISION),
        model="Qwen3-0.6B",
        hardware="H100",
        runtime="vllm",
        workload={"concurrency": 4},
        source_pointer={"record_id": identity, "json_pointer": "/0"},
    )
    key = f"{PATH}#measurement"
    measurement = RagItem(
        **common,
        kind="measurement",
        item_id=item_id(PRISM_ORIGIN, key, REVISION),
        logical_key=key,
        parent_id=experiment.item_id,
        experiment_id=experiment.item_id,
        evidence_type="raw_json",
        metric="throughput",
        value=Decimal("42"),
        original_value=Decimal("42"),
        unit="tokens/s",
        original_unit="tokens/s",
        source_pointer={"json_pointer": "/0/throughput"},
    )
    publication = RagPublicationService(dao=service.dao, archive=service.archive)
    publication.register_source(
        PRISM_ORIGIN,
        SourceApproval(
            verified_public=True,
            approved_by="isolated service fixture",
            approval_reference="fixture only",
            license_reference="fixture only",
            parser_version="fixture-v1",
            allowed_paths=allowed_paths(PRISM_ORIGIN),
        ),
    )
    manifest = publication.stage(
        inventory,
        (ParsedFile(path=PATH, items=(experiment, measurement)),),
        expected_counts={"source": 1, "experiment": 1, "measurement": 1},
    )
    publication.activate(PRISM_ORIGIN, manifest.item_id)


def test_actual_service_principals_get_only_their_own_records(branch):
    service, owner, _factory, runs, users = branch
    first = service.query(request(), owner)
    second = service.query(request(), replace(owner, user_id=users[1], username="user-1"))
    assert first.matched_items == second.matched_items == 1
    assert first.returned_observations == second.returned_observations == 1
    assert first.evidence[0].citation.evaluate_run_id == runs[0]
    assert second.evidence[0].citation.evaluate_run_id == runs[1]
    assert runs[1] not in first.model_dump_json() and runs[0] not in second.model_dump_json()


def test_hidden_exact_ids_do_not_leak_match_or_count(branch):
    service, owner, _factory, runs, _users = branch
    hidden = service.query(request(record_id=f"evaluate:{runs[1]}"), owner)
    missing = service.query(request(record_id=f"evaluate:{uuid4()}"), owner)
    assert hidden.matched_items == missing.matched_items == 0
    assert hidden.evidence == missing.evidence == ()
    assert hidden.abstentions == missing.abstentions
    assert hidden.status == missing.status == "abstained"
    assert runs[1] not in hidden.answer


def test_default_performs_no_agentic_or_evaluation_query(branch, postgres_engine):
    service, owner, _factory, _runs, _users = branch
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        response = service.query(request(internal=False), owner)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert not response.internal_branch_enabled and not response.evidence
    assert not any("agentic_benchmark_records" in statement or "evaluate_runs" in statement for statement in statements)


def test_actual_verified_overlap_is_one_envelope_with_both_provenances(branch):
    service, owner, _factory, runs, _users = branch
    publish(service, runs[0])
    response = service.query(request(), owner)
    assert response.matched_items == 2
    assert response.returned_observations == len(response.evidence) == 1
    assert response.evidence[0].origin == "public_and_agentic"
    assert len(response.evidence[0].corroborating_citations) == 1
    assert response.evidence[0].identity_verification == "canonical_evaluate_run_and_metric"
    assert "both provenances" in response.answer


def test_inaccessible_overlap_never_adds_internal_status_or_provenance(branch):
    service, owner, _factory, runs, _users = branch
    publish(service, runs[1])
    no_eval = replace(owner, bindings=(RoleBinding("rag"),))
    response = service.query(request(), no_eval)
    assert response.matched_items == response.returned_observations == 1
    assert response.evidence[0].origin == "public"
    assert not response.evidence[0].corroborating_citations
    assert response.evidence[0].identity_verification is None
    assert "public_and_agentic" not in response.model_dump_json()


def test_same_values_but_unproven_identity_stay_separate(branch):
    service, owner, _factory, _runs, _users = branch
    publish(service, str(uuid4()))
    response = service.query(request(), owner)
    assert response.returned_observations == 2
    assert {entry.origin for entry in response.evidence} == {"public", "agentic"}
    assert all(not entry.corroborating_citations for entry in response.evidence)


def test_delete_or_revoke_during_public_bytes_discards_internal_response(branch):
    service, owner, factory, runs, _users = branch
    publish(service, runs[0])
    read = service.archive.read

    def revoke(digest):
        data = read(digest)
        with factory.begin() as session:
            session.delete(session.get(EvaluateRunRow, runs[0]))
        return data

    service.archive.read = revoke
    with pytest.raises(ConflictError):
        service.query(request(), owner)


def test_query_uses_no_writes_and_does_not_mutate_planner_record(branch, postgres_engine):
    service, owner, factory, _runs, _users = branch
    with factory() as session:
        before = [row.to_dto().model_dump(mode="json") for row in session.scalars(select(BenchmarkRecordRow))]
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        service.query(request(), owner)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
    with factory() as session:
        after = [row.to_dto().model_dump(mode="json") for row in session.scalars(select(BenchmarkRecordRow))]
    assert before == after


def test_real_rest_contract_preserves_authorized_internal_envelope(branch, monkeypatch):
    from llm_d_lens.benchmark_rag import router

    service, owner, _factory, _runs, _users = branch
    monkeypatch.setattr(router, "get_service", lambda: service)
    monkeypatch.setattr(router, "current_principal", lambda _request: owner)
    app = FastAPI()
    app.include_router(router.router)
    payload = request()
    expected = service.query(payload, owner).model_dump(mode="json")
    with TestClient(app) as client:
        response = client.post("/api/v1/benchmark-rag/query", json=payload.model_dump(mode="json", exclude_unset=True))
    assert response.status_code == 200 and response.json() == expected
    assert response.json()["evidence"][0]["value"] == "42.0"
    assert isinstance(service.query(payload, owner).evidence[0].citation, AgenticCitation)
