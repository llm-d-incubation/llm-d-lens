"""Tests for Agentic planning evidence resolution."""

import asyncio
from types import SimpleNamespace

import pytest

from llm_d_lens.agentic.facts import (
    ResolvedPlanningFacts,
    _configuration_topology,
    rank_public_rag_evidence,
    resolve_planning_facts,
    resolve_public_rag_evidence,
    with_public_rag_evidence,
)
from llm_d_lens.agentic.planner import PlanningFacts, WorkloadProfile
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.benchmark_rag.query_contracts import Citation, QueryEvidence, QueryFilters, QueryResponse
from llm_d_lens.core.exceptions import DomainError


def _public_response(*, evidence=True):
    return QueryResponse(
        question="Published deployment benchmark evidence",
        intent="comparison",
        filters=QueryFilters(model="Qwen/Qwen3-8B"),
        status="answered" if evidence else "abstained",
        answer="Published results." if evidence else "No matches.",
        evidence=(
            (
                QueryEvidence(
                    item_id="observation-1",
                    evidence_type="published_table",
                    citation=Citation(
                        origin_id="llm-d:guides",
                        source_revision="revision-1",
                        source_path="guides/results.md",
                        sha256="a" * 64,
                        artifact_uri="artifact://benchmark-rag/source-1",
                        source_pointer={"row": 1},
                        published_manifest_id="manifest-1",
                    ),
                    metric="ttft",
                    statistic="p95",
                    value="125.50",
                    unit="ms",
                ),
            )
            if evidence
            else ()
        ),
        matched_items=1 if evidence else 0,
        returned_observations=1 if evidence else 0,
        truncated=False,
    )


def _observation(
    identity, *, workload=None, metric="ttft", value="100", unit="ms", experiment=None, series=None, report=""
):
    return {
        "item_id": identity,
        "metric": metric,
        "statistic": "p90",
        "value": value,
        "unit": unit,
        "citation": {"origin_id": "public", "sha256": "a" * 64},
        "experiment": {
            "item_id": experiment or identity,
            "workload": workload,
            "configuration": {"tp": 2, "published_report": report},
            "runtime": "vLLM",
        },
        "series": series,
    }


def test_public_workload_ranks_nearby_records_and_retains_unknown_fields():
    records = [
        _observation("far", workload={"mean_input_tokens": 8000, "mean_output_tokens": 500}),
        _observation("unknown"),
        _observation("near", workload={"mean_input_tokens": "4096", "mean_output_tokens": 1024}),
    ]
    facts = PlanningFacts(
        8,
        80,
        5,
        workload_profile=WorkloadProfile(mean_input_tokens=4000, mean_output_tokens=1000, p95_input_tokens=4000),
    )
    ranked, matches = rank_public_rag_evidence(records, facts)
    assert [item["item_id"] for item in ranked] == ["near", "far", "unknown"]
    assert matches[0]["workload"]["coverage"] == pytest.approx(2 / 3)
    assert matches[0]["workload"]["fields"]["mean_input_tokens"]["difference"] == 96
    assert matches[0]["workload"]["unknown_fields"] == ["p95_input_tokens"]
    assert matches[-1]["workload"]["similarity"] is None
    assert matches[0]["configuration_basis"]["configuration"] == {"tp": 2}
    assert records[0]["experiment"]["configuration"]["published_report"] == ""


def test_public_workload_uses_labelled_nominal_prompt_shape_without_inventing_p95():
    record = _observation(
        "report", report="6,000-token shared system prompt + 1,200-token question, 1,000-token output"
    )
    facts = PlanningFacts(
        8,
        80,
        5,
        workload_profile=WorkloadProfile(
            mean_input_tokens=4000, mean_output_tokens=1000, p95_input_tokens=4000, shared_prefix_ratio=0
        ),
    )
    _, matches = rank_public_rag_evidence([record], facts)
    match = matches[0]["workload"]
    assert match["fields"]["mean_input_tokens"]["reported"] == 7200
    assert match["fields"]["mean_input_tokens"]["relative_difference"] == pytest.approx(0.8)
    assert match["fields"]["mean_output_tokens"]["reported"] == 1000
    assert match["fields"]["mean_input_tokens"]["basis"].endswith("proxy_not_measured_mean")
    assert match["unknown_fields"] == ["p95_input_tokens", "shared_prefix_ratio"]
    assert match["nominal_prompt_shape"]["shared_prefix_tokens"] == 6000


def test_public_latency_only_orders_pass_miss_unknown_without_filtering():
    records = [
        _observation("miss", value="3", unit="s"),
        _observation("unknown", metric="throughput", value="500", unit="tokens/s"),
        _observation("pass", value="500"),
        _observation("empty", value="0", report="0.0 = empty latency histogram"),
    ]
    ranked, matches = rank_public_rag_evidence(records, PlanningFacts(8, 80, 5, ttft_slo_ms=2000))
    assert [item["item_id"] for item in ranked] == ["pass", "miss", "empty", "unknown"]
    assert [match["latency"]["status"] for match in matches] == ["satisfied", "not_satisfied", "unknown", "unknown"]
    assert matches[1]["latency"]["relative_violation"] == pytest.approx(0.5)
    assert all(match["workload"]["requested"] == {} for match in matches)


def test_public_latency_does_not_mix_series_and_preserves_missing_metrics_and_statistics():
    records = [
        _observation("ttft-rate1", experiment="same", series={"rate": 1}, value="1000"),
        _observation("tpot-rate2", experiment="same", series={"rate": 2}, metric="tpot", value="20", unit="ms/token"),
        _observation("tpot-rate1", experiment="same", series={"rate": 1}, metric="tpot", value="0.04", unit="s/token"),
    ]
    _, matches = rank_public_rag_evidence(records, PlanningFacts(8, 80, 5, ttft_slo_ms=2000, tpot_slo_ms=30))
    by_id = {match["evidence_id"]: match["latency"] for match in matches}
    assert by_id["public-rag:ttft-rate1"]["status"] == "not_satisfied"
    assert by_id["public-rag:tpot-rate2"]["status"] == "unknown"
    assert by_id["public-rag:tpot-rate2"]["unknown_metrics"] == ["ttft"]
    observation = by_id["public-rag:ttft-rate1"]["fields"]["tpot"]["observations"][0]
    assert observation["value_ms"] == 40
    assert observation["statistic"] == "p90"


def test_public_no_workload_or_latency_returns_all_records_without_inferred_targets():
    records = [_observation(f"record-{index:03}") for index in range(75)]
    ranked, matches = rank_public_rag_evidence(records, PlanningFacts(8, 80, 5, use_case="code-generation"))
    assert len(ranked) == 75
    assert all(match["workload"]["requested"] == {} for match in matches)
    assert all(match["latency"]["status"] == "not_requested" for match in matches)


@pytest.fixture
def public_hardware(monkeypatch):
    hardware = {"accelerators": [{"id": "nvidia", "gpuCount": 4, "models": ["NVIDIA-A100-80GB-PCIe", "ampere"]}]}

    async def overview(cluster):
        assert cluster.id == "cluster-a"
        return {"hardware": hardware}

    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )
    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.RagQueryDao",
        lambda: SimpleNamespace(hardware_options=lambda _filters: ("Intel B60 XPU", "NVIDIA A100 80GB PCIe")),
    )
    return hardware


@pytest.mark.parametrize(
    "principal",
    [
        None,
        Principal("system", "system", bindings=(RoleBinding("admin"),), authz_version=0),
        Principal("user", "user"),
        Principal(
            "user",
            "user",
            bindings=(RoleBinding("reader", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),),
            role_permissions={"reader": frozenset({"benchmark-rag:query:read"})},
        ),
    ],
)
def test_public_rag_checks_global_permission_before_constructing_query_service(monkeypatch, principal):
    def forbidden_service():
        pytest.fail("Unauthorized recommendations must not access the public corpus")

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicRagQueryService", forbidden_service)
    monkeypatch.setattr("llm_d_lens.agentic.facts.RagQueryDao", forbidden_service)
    monkeypatch.setattr("llm_d_lens.agentic.facts.registry.require_cluster", lambda _id: forbidden_service())
    result = asyncio.run(resolve_public_rag_evidence("Qwen/Qwen3-8B", principal, cluster_server_id="cluster-a"))
    assert result.status == "forbidden"
    assert "permission" in result.details["message"]
    assert "response" not in result.details


@pytest.mark.parametrize("has_evidence", [True, False])
@pytest.mark.parametrize("simulated", [False, True])
@pytest.mark.parametrize(
    ("profile", "expected_workload"),
    [
        (None, {}),
        (WorkloadProfile(), {}),
        (WorkloadProfile(shared_prefix_ratio=0), {"shared_prefix_ratio": 0}),
        (
            WorkloadProfile(mean_input_tokens=2048, mean_output_tokens=256, request_rate=10),
            {"mean_input_tokens": 2048, "mean_output_tokens": 256, "request_rate": 10},
        ),
    ],
)
def test_public_rag_is_public_only_exact_model_and_preserves_citations(
    monkeypatch, has_evidence, simulated, profile, expected_workload, public_hardware
):
    principal = Principal("admin-user", "admin", bindings=(RoleBinding("admin"),))
    calls = []

    class QueryService:
        def __init__(self, *, dao):
            pass

        def query(self, request, caller, *, all_matches=False):
            assert all_matches
            calls.append((request, caller))
            return _public_response(evidence=has_evidence)

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicRagQueryService", QueryService)
    if simulated:
        monkeypatch.setenv("AGENTIC_TEST_CLUSTER_HARDWARE_OVERRIDES", '{"cluster-a":"H100"}')
    result = asyncio.run(
        resolve_public_rag_evidence(
            "Qwen/Qwen3-8B",
            principal,
            cluster_server_id="cluster-a",
            planning_facts=PlanningFacts(8, 32, 4, use_case="code-generation", workload_profile=profile),
        )
    )
    query, caller = calls[0]
    assert caller is principal
    assert query.filters.model == "Qwen/Qwen3-8B"
    assert query.filters.hardware == ("H100" if simulated else "NVIDIA A100 80GB PCIe")
    assert query.filters.workload == {}
    assert result.details["retrieval"]["workload"] == expected_workload
    assert result.details["retrieval"]["all_matches"] is True
    assert result.details["retrieval"]["mode"] == ("approximate_workload" if expected_workload else "model_hardware")
    assert result.details["target_hardware"]["model"] == ("H100" if simulated else "NVIDIA-A100-80GB-PCIe")
    assert query.include_internal is False
    assert query.limit == 50
    assert result.status == ("available" if has_evidence else "no_matches")
    context = with_public_rag_evidence(
        ResolvedPlanningFacts(facts=PlanningFacts(8, 32, 4), cluster_snapshot={}), result
    )
    assert context.evidence[-1] == result
    assert context.trace[0]["phase"] == "public_rag"
    if has_evidence:
        observation = context.facts.public_rag_evidence[0]
        assert observation["evidence_id"] == "public-rag:observation-1"
        assert observation["value"] == "125.50"
        assert observation["citation"]["sha256"] == "a" * 64
        assert observation["citation"]["source_revision"] == "revision-1"
        # Retrieval alone is not a claim that the evidence affected selection.
        assert context.facts.evidence_ids == ()
    else:
        assert context.facts.public_rag_evidence == ()


@pytest.mark.parametrize("failure", [DomainError("unavailable"), PublicationError("invalid manifest"), TimeoutError()])
def test_public_rag_failure_is_explicit_and_non_fatal(monkeypatch, failure, caplog, public_hardware):
    principal = Principal("admin-user", "admin", bindings=(RoleBinding("admin"),))

    class QueryService:
        def __init__(self, *, dao):
            pass

        def query(self, request, caller, *, all_matches=False):
            raise failure

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicRagQueryService", QueryService)
    result = asyncio.run(resolve_public_rag_evidence("Qwen/Qwen3-8B", principal, cluster_server_id="cluster-a"))
    assert result.status == "unavailable"
    assert "continuing" in result.details["message"]
    assert "response" not in result.details
    assert "Agentic public RAG retrieval unavailable" in caplog.text


@pytest.mark.parametrize(
    ("profile_id", "models", "expected"),
    [
        ("intel-xpu", ["B60"], "Intel B60 XPU"),
        ("nvidia", ["NVIDIA-A100-80GB-PCIe", "ampere"], "NVIDIA-A100-80GB-PCIe"),
        ("nvidia", ["NVIDIA-A100-SXM4-80GB"], "NVIDIA-A100-SXM4-80GB"),
    ],
)
def test_public_rag_never_broadens_to_other_hardware(monkeypatch, public_hardware, profile_id, models, expected):
    public_hardware["accelerators"] = [{"id": profile_id, "gpuCount": 4, "models": models}]
    calls = []
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.RagQueryDao",
        lambda: SimpleNamespace(
            hardware_options=lambda _filters: ("Intel B60 XPU", "NVIDIA A100 40GB PCIe", "NVIDIA A100 SXM4 40GB")
        ),
    )

    class QueryService:
        def __init__(self, *, dao):
            pass

        def query(self, request, caller, *, all_matches=False):
            calls.append(request)
            return _public_response(evidence=profile_id == "intel-xpu")

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicRagQueryService", QueryService)
    result = asyncio.run(
        resolve_public_rag_evidence(
            "Qwen/Qwen3-8B",
            Principal("admin-user", "admin", bindings=(RoleBinding("admin"),)),
            cluster_server_id="cluster-a",
        )
    )
    assert len(calls) == 1
    assert calls[0].filters.hardware == expected
    assert result.status == ("available" if profile_id == "intel-xpu" else "no_matches")
    if profile_id == "nvidia":
        assert "no cross-hardware fallback" in result.details["message"]
        assert expected in result.details["message"]


@pytest.mark.parametrize(
    "accelerators",
    [
        [],
        [{"id": "unknown", "gpuCount": 1, "models": ["A100"]}],
        [{"id": "nvidia", "gpuCount": 1, "models": []}],
        [{"id": "nvidia", "gpuCount": 1, "models": ["ampere"]}],
        [{"id": "nvidia", "gpuCount": 2, "models": ["A100", "H100"]}],
        [{"id": "nvidia", "gpuCount": 2, "models": ["A100", "A100-80GB-PCIe"]}],
        [
            {"id": "nvidia", "gpuCount": 1, "models": ["A100"]},
            {"id": "intel-xpu", "gpuCount": 1, "models": ["B60"]},
        ],
    ],
)
def test_public_rag_unknown_or_mixed_hardware_does_not_query_corpus(monkeypatch, public_hardware, accelerators):
    public_hardware["accelerators"] = accelerators

    def forbidden_dao():
        pytest.fail("Unknown or mixed hardware must not cause an unfiltered corpus query")

    monkeypatch.setattr("llm_d_lens.agentic.facts.RagQueryDao", forbidden_dao)
    result = asyncio.run(
        resolve_public_rag_evidence(
            "Qwen/Qwen3-8B",
            Principal("admin-user", "admin", bindings=(RoleBinding("admin"),)),
            cluster_server_id="cluster-a",
        )
    )
    assert result.status == "unavailable"
    assert result.details["code"] == "rag_hardware_unresolved"


@pytest.mark.parametrize(
    ("cluster_defaults", "expected", "overridden"),
    [
        ('{"cluster-a":"H100"}', "H100", True),
        ('{"different-cluster":"H100"}', "NVIDIA A100 80GB PCIe", False),
        ("{}", "NVIDIA A100 80GB PCIe", False),
    ],
)
def test_test_hardware_is_cluster_scoped_visible_and_reversible(
    monkeypatch, public_hardware, cluster_defaults, expected, overridden
):
    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_HARDWARE_OVERRIDES", cluster_defaults)
    calls = []

    class QueryService:
        def __init__(self, *, dao):
            pass

        def query(self, request, caller, *, all_matches=False):
            calls.append(request)
            return _public_response()

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicRagQueryService", QueryService)
    evidence = asyncio.run(
        resolve_public_rag_evidence(
            "Qwen/Qwen3-8B",
            Principal("admin-user", "admin", bindings=(RoleBinding("admin"),)),
            cluster_server_id="cluster-a",
        )
    )
    assert calls[0].filters.hardware == expected
    assert calls[0].filters.model == "Qwen/Qwen3-8B"
    assert evidence.details["target_hardware"]["model"] == (expected if overridden else "NVIDIA-A100-80GB-PCIe")
    assert bool(evidence.details["target_hardware"].get("override")) is overridden
    context = with_public_rag_evidence(
        ResolvedPlanningFacts(facts=PlanningFacts(8, 32, 4), cluster_snapshot={}), evidence
    )
    observation = context.facts.public_rag_evidence[0]
    assert "hardware_reference_only" not in observation
    assert context.facts.planning_hardware_model == (expected if overridden else None)
    if overridden:
        assert evidence.details["target_hardware"]["actual_model"] == "NVIDIA-A100-80GB-PCIe"
        assert evidence.details["target_hardware"]["simulation"] is True
        assert "TEST HARDWARE IDENTITY" in evidence.details["message"]
        assert "deployment is disabled" in evidence.details["message"]
    assert public_hardware["accelerators"][0]["models"] == ["NVIDIA-A100-80GB-PCIe", "ampere"]


@pytest.mark.parametrize("setting", ["not-json", "[]", '{"cluster-a":42}', '{"cluster-a":""}'])
def test_public_rag_invalid_override_configuration_is_explicit(monkeypatch, public_hardware, setting):
    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_HARDWARE_OVERRIDES", setting)
    evidence = asyncio.run(
        resolve_public_rag_evidence(
            "Qwen/Qwen3-8B",
            Principal("admin-user", "admin", bindings=(RoleBinding("admin"),)),
            cluster_server_id="cluster-a",
        )
    )
    assert evidence.status == "unavailable"
    assert "response" not in evidence.details
    assert "code" in evidence.details


@pytest.mark.parametrize("nested", [False, True])
def test_simulated_overview_changes_identity_without_mutating_capacity_or_discovery(public_hardware, nested):
    from copy import deepcopy

    from llm_d_lens.agentic.facts import planning_hardware_overview

    original = {
        "hardware": {**public_hardware, "gpuCount": 4, "availableGpuCount": 3, "vramBytes": 4 * 80 * 1024**3},
        "nodes": [{"gpuModelsByProfile": {"nvidia": ["NVIDIA-A100-80GB-PCIe", "ampere"]}}],
    }
    if nested:
        original = {"kubernetes": original}
    before = deepcopy(original)
    assert planning_hardware_overview(original, None) is original
    effective = planning_hardware_overview(original, "H100")
    context = effective["kubernetes"] if nested else effective
    assert context["hardware"]["accelerators"][0]["models"] == ["H100"]
    assert context["nodes"][0]["gpuModelsByProfile"]["nvidia"] == ["H100"]
    assert context["hardware"]["gpuCount"] == 4
    assert context["hardware"]["availableGpuCount"] == 3
    assert context["hardware"]["vramBytes"] == 4 * 80 * 1024**3
    assert original == before


@pytest.mark.parametrize("model", ["unregistered-device", "Intel B60 XPU"])
def test_simulated_identity_rejects_unknown_or_different_profile(public_hardware, model):
    from llm_d_lens.agentic.facts import planning_hardware_overview
    from llm_d_lens.core.exceptions import DomainValidationError

    with pytest.raises(DomainValidationError, match="one AIC system"):
        planning_hardware_overview({"hardware": public_hardware}, model)


def test_resolver_uses_live_cluster_capacity_and_free_memory(monkeypatch):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {
            "hardware": {
                "gpuCount": 4,
                "availableGpuCount": 3,
                "vramBytes": 4 * 32 * 1024**3,
                "memoryBytes": 100 * 1024**3,
                "memoryUsagePercent": 25,
            },
        }

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=99,
                cpu_buffer_gib=1,
                gpu_memory_utilization=0.6,
            ),
            include_supplementary_evidence=False,
        )
    )

    assert resolved.facts.free_gpu_count == 3
    assert resolved.facts.vram_per_gpu_gib == 32
    assert resolved.facts.cpu_buffer_gib == 75
    assert resolved.facts.gpu_memory_utilization == 0.6
    assert resolved.facts.evidence_ids == ("cluster-overview:cluster-a",)


def test_resolver_preserves_operator_preference_for_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 1, "availableGpuCount": 1, "vramBytes": 32 * 1024**3}}

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                operator_preference="prefer prefix cache",
            ),
            include_supplementary_evidence=False,
        )
    )

    assert resolved.facts.operator_preference == "prefer prefix cache"


def test_resolver_applies_requested_total_or_custom_gpu_capacity(monkeypatch):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 8, "availableGpuCount": 3, "vramBytes": 8 * 32 * 1024**3}}

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    all_capacity = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                hardware_capacity_mode="all",
            ),
            include_supplementary_evidence=False,
        )
    )
    custom_capacity = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                hardware_capacity_mode="custom",
                custom_gpu_count=5,
            ),
            include_supplementary_evidence=False,
        )
    )

    assert all_capacity.facts.free_gpu_count == 8
    assert custom_capacity.facts.free_gpu_count == 5
    assert custom_capacity.evidence[0].details["capacity_mode"] == "custom"


def test_resolver_retains_hard_cluster_facts_when_supplementary_sources_fail(monkeypatch):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster",
        lambda cluster_id: SimpleNamespace(id=cluster_id),
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 1, "availableGpuCount": 1, "vramBytes": 32 * 1024**3}}

    async def unavailable_aic(_request):
        from llm_d_lens.aic.service import AICError

        raise AICError("offline")

    async def unavailable_simulation(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_lens.agentic.facts.check_support", unavailable_aic)
    monkeypatch.setattr("llm_d_lens.agentic.facts.list_tasks", unavailable_simulation)

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(model_weight_gib=8, vram_per_gpu_gib=32, free_gpu_count=1),
        )
    )

    assert resolved.facts.free_gpu_count == 1
    assert {item.source for item in resolved.evidence if item.status == "unavailable"} >= {"aic", "simulation"}


@pytest.mark.parametrize("target", [None, "concurrency", "request_rate"])
@pytest.mark.parametrize("simulated", [None, "H100"])
def test_resolver_uses_measured_workload_for_aic_and_normalizes_predictions(monkeypatch, target, simulated):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )

    async def overview(_cluster):
        return {
            "hardware": {
                "gpuCount": 4,
                "availableGpuCount": 4,
                "vramBytes": 4 * 32 * 1024**3,
                "accelerators": [{"id": "nvidia", "gpuCount": 4, "models": ["NVIDIA A100"]}],
            }
        }

    captured = {}

    async def support(_request):
        return SimpleNamespace(supported=True, model_dump=lambda: {"supported": True})

    async def aic_search(request):
        captured["request"] = request
        return SimpleNamespace(
            model_dump=lambda: {
                "configs": [
                    {
                        "mode": "agg",
                        "tp": 1,
                        "replicas": 1,
                        "ttft_ms": 300,
                        "tpot_ms": 25,
                        "throughput_tokens_per_sec": 700,
                    }
                ]
            }
        )

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_lens.agentic.facts.check_support", support)
    monkeypatch.setattr("llm_d_lens.agentic.facts.search", aic_search)
    monkeypatch.setattr("llm_d_lens.agentic.facts._historical_topology_by_execution", lambda: {})
    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                workload_profile=WorkloadProfile(
                    mean_input_tokens=4096, mean_output_tokens=512, **({target: 10} if target else {})
                ),
                planning_hardware_model=simulated,
            ),
        )
    )

    assert captured["request"].aic_system_name == ("h100_sxm" if simulated else "a100_sxm")
    assert resolved.facts.planning_hardware_model == simulated
    assert resolved.facts.free_gpu_count == 4
    assert resolved.facts.vram_per_gpu_gib == 32
    assert captured["request"].mean_input_tokens == 4096
    assert captured["request"].mean_output_tokens == 512
    assert captured["request"].target_concurrency == (10 if target == "concurrency" else None)
    assert captured["request"].target_request_rate == (10 if target == "request_rate" else None)
    assert resolved.trace[-1]["request"]["mean_input_tokens"] == 4096
    assert resolved.trace[-1]["request"]["mean_output_tokens"] == 512
    assert resolved.trace[-1]["search_status"] == "available"
    assert resolved.facts.aic_predictions[0].ttft_ms == 300


def test_load_prediction_failure_is_not_silently_discarded(monkeypatch):
    from llm_d_lens.agentic.facts import _supplementary_evidence
    from llm_d_lens.aic.service import AICError

    async def support(request):
        return SimpleNamespace(supported=True, model_dump=lambda: {"supported": True})

    async def search(request):
        raise AICError("Minimum recommended GPUs: 8; selected GPU budget: 4")

    monkeypatch.setattr("llm_d_lens.agentic.facts.check_support", support)
    monkeypatch.setattr("llm_d_lens.agentic.facts.search", search)
    with pytest.raises(ValueError, match="AIC load-target prediction failed: Minimum recommended GPUs: 8"):
        asyncio.run(
            _supplementary_evidence(
                "model",
                PlanningFacts(8, 32, 4, workload_profile=WorkloadProfile(request_rate=10)),
                4,
                {"accelerators": [{"id": "nvidia", "models": ["NVIDIA A100"]}]},
            )
        )


def test_resolver_does_not_persist_simulation_with_current_planning_facts(monkeypatch):
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 4, "availableGpuCount": 4, "vramBytes": 4 * 32 * 1024**3}}

    async def unavailable_aic(_request):
        from llm_d_lens.aic.service import AICError

        raise AICError("offline")

    captured = {}

    async def history(**kwargs):
        captured.update(kwargs)
        request = SimpleNamespace(successful=True, ttft_ms=310, tpot_ms=24, input_tokens=4096, output_tokens=512)
        result = SimpleNamespace(summary={"throughput_tps": 720}, per_request=[request])
        task = SimpleNamespace(
            id="abcdef12", scenario="chat", result=result, endpoint_deployment_execution_id="execution-1"
        )
        return SimpleNamespace(total=1, tasks=[task])

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    monkeypatch.setattr("llm_d_lens.agentic.facts.check_support", unavailable_aic)
    monkeypatch.setattr("llm_d_lens.agentic.facts.list_tasks", history)
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts._historical_topology_by_execution",
        lambda: {
            "execution-1": {
                "provider_ref": "pd-disaggregation",
                "replicas": 1,
                "tensor_parallel_size": 1,
                "prefill_replicas": 1,
                "prefill_tensor_parallel_size": 1,
                "guide_variant": "vllm",
                "max_model_len": 4096,
                "gpu_memory_utilization": 0.9,
                "required_gpus": 2,
            },
        },
    )

    resolved = asyncio.run(
        resolve_planning_facts(
            "cluster-a",
            "Qwen/Qwen3-8B",
            PlanningFacts(
                model_weight_gib=8,
                vram_per_gpu_gib=1,
                free_gpu_count=0,
                workload_profile=WorkloadProfile(mean_input_tokens=4096, mean_output_tokens=512),
            ),
        )
    )

    assert captured == {"status": "completed", "model": "Qwen/Qwen3-8B", "limit": 20}
    simulation_evidence = next(item for item in resolved.evidence if item.id == "simulation-history")
    assert simulation_evidence.details["total"] == 1
    assert simulation_evidence.details["benchmark_record_count"] == 0
    assert "observations" not in simulation_evidence.details


def test_configuration_topology_captures_candidate_equivalent_pd_fields():
    configuration = SimpleNamespace(
        provider_ref="pd-disaggregation",
        provenance={"agentic_candidate_id": "legacy-internal-id"},
        content={
            "decode": {"replicaCount": 2, "tensorParallelSize": 2, "maxModelLen": 8192, "gpuMemoryUtilization": 0.9},
            "prefill": {"replicaCount": 1, "tensorParallelSize": 2},
            "guideVariant": "vllm",
        },
    )

    topology = _configuration_topology(configuration)

    assert topology == {
        "candidate_id": "legacy-internal-id",
        "provider_ref": "pd-disaggregation",
        "replicas": 2,
        "tensor_parallel_size": 2,
        "prefill_replicas": 1,
        "prefill_tensor_parallel_size": 2,
        "guide_variant": "vllm",
        "max_model_len": 8192,
        "gpu_memory_utilization": 0.9,
        "required_gpus": 6,
    }
