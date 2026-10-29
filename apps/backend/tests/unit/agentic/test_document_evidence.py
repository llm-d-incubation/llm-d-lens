"""Document source contracts remain usable without a document implementation."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from llm_d_lens.agentic import document_evidence as documents
from llm_d_lens.agentic.document_evidence import DocumentEvidenceOutcome, DocumentEvidencePacket
from llm_d_lens.agentic.facts import PlanningEvidence, ResolvedPlanningFacts
from llm_d_lens.agentic.models import AgenticCandidateRefinementRequest, AgenticDeploymentCreateRequest
from llm_d_lens.agentic.planning_snapshot import PlanningSnapshotStore
from llm_d_lens.agentic.service import AgenticDeploymentService
from llm_d_lens.auth.contracts import Principal, RoleBinding
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, Citation
from llm_d_lens.core.exceptions import ForbiddenError


def _principal():
    return Principal(
        user_id="allowed",
        username="test",
        bindings=(RoleBinding("reader"),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )


def _packet():
    return DocumentEvidencePacket(
        citation_id="document:chunk-0",
        evidence_id="passage-0",
        chunk_id="chunk-0",
        retrieval_build_id="build-1",
        retrieval_mode="opensearch_lexical",
        matched_branches=("lexical",),
        title="Deployment guidance",
        text="Untrusted deployment guide.",
        citation=Citation(
            origin_id="guide:llm-d",
            source_revision="rev-1",
            source_path="guide.md",
            sha256="a" * 64,
            artifact_uri="artifact://public/guide.md",
            source_pointer={"line_start": 3, "line_end": 4},
            published_manifest_id="manifest-1",
        ),
    )


@pytest.mark.parametrize("principal", [None, _principal()])
def test_provider_is_explicitly_disabled_without_configuration(monkeypatch, principal):
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://must-not-be-used.invalid")
    outcome = asyncio.run(documents.resolve_document_evidence("deployment guidance", principal))
    assert outcome.status == "disabled"
    assert outcome.mode == "disabled"
    assert outcome.reason == "provider_not_installed"
    assert not outcome.packets
    assert outcome.retrieved_count == 0
    assert outcome.metadata()["reason"] == "provider_not_installed"


def test_saved_packets_cannot_be_revalidated_by_an_absent_provider():
    documents.assert_document_sources_current(())
    with pytest.raises(ForbiddenError, match="provider is not installed"):
        documents.assert_document_sources_current((_packet(),))


def test_packet_contract_preserves_provenance_and_rejects_invalid_offsets():
    packet = _packet()
    assert packet.source_timestamp is None
    assert packet.last_successful_sync_at is None
    assert packet.citation.published_manifest_id == "manifest-1"
    with pytest.raises(ValidationError, match="offsets"):
        DocumentEvidencePacket.model_validate({**packet.model_dump(), "chunk_start": 1, "chunk_end": 2})
    with pytest.raises(ValidationError, match="only enabled"):
        DocumentEvidenceOutcome(status="disabled", packets=(packet,))
    with pytest.raises(ValueError, match="unknown document citation"):
        documents.validate_document_citations(["document:invented"], (packet,))


def test_document_context_is_untrusted_and_separate_from_measurements():
    context = documents.document_context((_packet(),))
    assert "not measured numeric performance" in context["document_evidence_usage"]
    assert context["document_evidence"][0]["text"] == _packet().text
    assert documents.document_context(()) == {}


@pytest.fixture
def orchestration(monkeypatch, tmp_path):
    from llm_d_lens.benchmark_rag.case_retrieval import InternalCaseOutcome

    captured = []

    async def cache(*_args, **_kwargs):
        return None

    async def public_context(_self, _cluster, _model, facts, _principal, _progress):
        return facts, PlanningEvidence(
            id="public-rag:query",
            source="public-benchmark-rag",
            status="empty",
            details={"message": "No measured public observations."},
        )

    async def resolve(_cluster, _model, facts):
        captured.append(facts)
        return ResolvedPlanningFacts(facts=facts, cluster_snapshot={})

    async def internal(*_args, **_kwargs):
        return InternalCaseOutcome(status="empty")

    async def hardware(_cluster, _model, facts, _principal):
        return facts, {"status": "unavailable"}

    for module in ("generator", "planner", "facts"):
        monkeypatch.setattr(f"llm_d_lens.agentic.{module}.load_model_config", lambda _model: None)
    monkeypatch.setattr("llm_d_lens.agentic.service.resolve_retrieval_hardware", hardware)
    monkeypatch.setattr(AgenticDeploymentService, "_validate_model_cache", staticmethod(cache))
    monkeypatch.setattr(AgenticDeploymentService, "_public_rag_context", public_context)
    monkeypatch.setattr("llm_d_lens.agentic.service.resolve_planning_facts", resolve)
    monkeypatch.setattr("llm_d_lens.agentic.service.resolve_internal_cases", internal)
    monkeypatch.setattr("llm_d_lens.agentic.service.require_active_session", lambda _id: SimpleNamespace(server_id="c"))
    monkeypatch.setattr("llm_d_lens.agentic.service.OpenAICompatiblePlanner.from_provider_id", lambda _id: None)
    service = AgenticDeploymentService(snapshot_store=PlanningSnapshotStore(tmp_path))
    request = AgenticDeploymentCreateRequest(
        model="model",
        deployment_name="split-test",
        cluster_session_id="a" * 36,
        storage_volume_id="volume",
        replicas=1,
        tensor_parallel_size=1,
        planning_facts={"model_weight_gib": 8, "vram_per_gpu_gib": 32, "free_gpu_count": 4},
    )
    return service, request, captured, tmp_path


def test_actual_create_refine_works_without_document_backend(orchestration):
    service, request, captured, root = orchestration
    run = asyncio.run(service.create(request, principal=_principal()))
    assert run.selected_candidate is not None
    assert not captured[-1].document_evidence
    evidence = next(item for item in run.planning_evidence if item.source == "public-document-rag")
    assert evidence.status == "disabled"
    assert evidence.details["reason"] == "provider_not_installed"
    snapshot = json.loads((root / run.planning_snapshot_path).read_text())
    assert not snapshot["resolved_planning_facts"]["document_evidence"]
    run = asyncio.run(
        service.refine(
            run.id,
            AgenticCandidateRefinementRequest(planner_prompt="refine"),
            principal=_principal(),
        )
    )
    assert run.selected_candidate is not None
    assert not captured[-1].document_evidence
    assert "document:chunk-" not in run.model_dump_json()
    assert any(item.source == "configuration-cases" for item in run.planning_evidence)


def test_refine_removes_document_packets_from_an_old_plan(orchestration, monkeypatch):
    service, request, captured, _root = orchestration

    async def previously_enabled(*_args):
        return DocumentEvidenceOutcome(status="available", mode="enabled", packets=(_packet(),))

    with monkeypatch.context() as enabled:
        enabled.setattr("llm_d_lens.agentic.service.resolve_document_evidence", previously_enabled)
        enabled.setattr("llm_d_lens.agentic.service.assert_document_sources_current", lambda _packets: None)
        run = asyncio.run(service.create(request, principal=_principal()))
    assert captured[-1].document_evidence
    run.decision_evidence_ids.append("document:chunk-0")
    run.selected_candidate.evidence.append("document:chunk-0")
    run = asyncio.run(
        service.refine(
            run.id,
            AgenticCandidateRefinementRequest(planner_prompt="without documents"),
            principal=_principal(),
        )
    )
    assert not captured[-1].document_evidence
    assert "document:chunk-0" not in run.model_dump_json()
