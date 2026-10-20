"""Composition policy tests isolate external AI and infrastructure."""

import asyncio
from types import SimpleNamespace

from llm_d_lens.agentic.composition import CompositionSelectionRequest, cost_order, select_composition_optimizations
from llm_d_lens.agentic.models import AgenticCandidate, AgenticDeploymentCreateRequest
from llm_d_lens.agentic.planner import PlannedCandidate


def candidate(name, cards, status):
    return PlannedCandidate(
        id=name,
        replicas=1,
        tensor_parallel_size=cards,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        required_gpus=cards,
        deployable=True,
        slo_status=status,
    )


def run(mode):
    return SimpleNamespace(
        request=AgenticDeploymentCreateRequest(
            model="org/model",
            deployment_name="test",
            cluster_session_id="a" * 36,
            composition_mode=mode,
            storage_volume_id="volume",
            replicas=1,
            tensor_parallel_size=1,
            planning_facts={"model_weight_gib": 8, "vram_per_gpu_gib": 24, "free_gpu_count": 8},
        ),
        selected_candidate=AgenticCandidate(
            id="base", replicas=1, tensor_parallel_size=1, max_model_len=4096, gpu_memory_utilization=0.9
        ),
    )


def options():
    return CompositionSelectionRequest(options=[{"id": "prefix", "label": "Prefix", "guide": "baseline"}])


def test_sla_precedes_cost_and_known_failures_are_not_returned():
    ordered = cost_order(
        [
            candidate("unknown", 1, "unknown"),
            candidate("large", 4, "satisfied"),
            candidate("small", 2, "satisfied"),
            candidate("failed", 1, "not_satisfied"),
        ]
    )
    assert [item.id for item in ordered] == ["small", "large", "unknown"]


def test_advanced_requests_all_options_without_external_ai(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Advanced must not call AI")

    monkeypatch.setattr("llm_d_lens.agentic.composition.OpenAICompatiblePlanner.from_provider_id", forbidden)
    result = asyncio.run(select_composition_optimizations(run("advanced"), options()))
    assert result["enabled"] == ["prefix"]


def test_agentic_invalid_selection_falls_back_without_enabling_unknown_options(monkeypatch):
    class Client:
        async def complete(self, **kwargs):
            assert "SLA" in kwargs["system_prompt"]
            return '{"enabled":["invented"],"rationale":"invalid"}'

    settings = SimpleNamespace(model_dump=lambda **kwargs: {})
    monkeypatch.setattr(
        "llm_d_lens.agentic.composition.OpenAICompatiblePlanner.from_provider_id",
        lambda _: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr("llm_d_lens.agentic.composition.client_for", lambda **kwargs: Client())
    result = asyncio.run(select_composition_optimizations(run("agentic"), options()))
    assert result["enabled"] == []
    assert result["source"] == "deterministic"


def test_agentic_accepts_bounded_ai_selection(monkeypatch):
    class Client:
        async def complete(self, **kwargs):
            return '{"enabled":["prefix"],"rationale":"Repeated prompts"}'

    settings = SimpleNamespace(model_dump=lambda **kwargs: {})
    monkeypatch.setattr(
        "llm_d_lens.agentic.composition.OpenAICompatiblePlanner.from_provider_id",
        lambda _: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr("llm_d_lens.agentic.composition.client_for", lambda **kwargs: Client())
    result = asyncio.run(select_composition_optimizations(run("agentic"), options()))
    assert result["enabled"] == ["prefix"]
    assert result["source"] == "ai"
