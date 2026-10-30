"""Tests for Agentic planning evidence resolution."""

import asyncio
from types import SimpleNamespace

import pytest

from llm_d_lens.agentic.facts import (
    ResolvedPlanningFacts,
    rank_public_rag_evidence,
    resolve_planning_facts,
    resolve_public_rag_evidence,
    resolve_retrieval_hardware,
    with_public_rag_evidence,
)
from llm_d_lens.agentic.planner import PlanningFacts, WorkloadProfile, normalize_planning_facts
from llm_d_lens.agentic.public_benchmark_record.publication_contracts import PublicationError
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    Citation,
    QueryEvidence,
    QueryFilters,
    QueryResponse,
)
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
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


def test_public_latency_preserves_requested_statistic_and_strict_boundary():
    from llm_d_lens.agentic.planner import normalize_planning_facts

    facts = normalize_planning_facts(PlanningFacts(8, 80, 2), "TTFT P95 < 2 s")
    median = {**_observation("median", value="1", unit="s"), "statistic": "p50"}
    _, matches = rank_public_rag_evidence([median], facts)
    assert matches[0]["latency"]["status"] == "unknown"
    p95 = {**_observation("p95", value="2", unit="s"), "statistic": "p95"}
    _, matches = rank_public_rag_evidence([p95], facts)
    assert matches[0]["latency"]["status"] == "not_satisfied"
    field = matches[0]["latency"]["fields"]["ttft"]
    assert field["operator"] == "<" and field["statistic"] == "p95"
    assert field["observations"][0]["value_ms"] == 2000


@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_latency_is_unknown_not_satisfied_without_source_rewrite(value):
    from llm_d_lens.agentic.planner import normalize_planning_facts

    facts = normalize_planning_facts(PlanningFacts(8, 80, 2), "TTFT mean < 2 s")
    record = {**_observation("invalid-latency", value=value, unit="s"), "statistic": "mean"}
    ranked, matches = rank_public_rag_evidence([record], facts)
    assert matches[0]["latency"]["status"] == "unknown"
    assert matches[0]["latency"]["unknown_metrics"] == ["ttft"]
    assert ranked[0]["value"] == value


@pytest.mark.parametrize("source", ["text", "form", "legacy"])
@pytest.mark.parametrize(
    "statistic,value,status",
    [("mean", "1", "satisfied"), ("mean", "3", "not_satisfied"), ("p95", "1", "unknown"), (None, "1", "satisfied")],
)
def test_public_latency_defaults_to_mean_without_relabelling_observations(source, statistic, value, status):
    from llm_d_lens.agentic.planner import normalize_planning_facts

    facts = PlanningFacts(8, 80, 2, ttft_slo_ms=2000 if source != "text" else None)
    if source != "legacy":
        facts = normalize_planning_facts(facts, "TTFT < 2 s" if source == "text" else "")
    record = {**_observation("observation", value=value, unit="s"), "statistic": statistic}
    ranked, matches = rank_public_rag_evidence([record], facts)
    assert ranked == [record]
    assert ranked[0]["statistic"] == statistic
    latency = matches[0]["latency"]
    assert latency["status"] == status
    assert latency["fields"]["ttft"]["statistic"] == "mean"
    assert latency["unknown_metrics"] == (["ttft"] if status == "unknown" else [])


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
    facts = normalize_planning_facts(PlanningFacts(8, 80, 5), "TTFT P90 <= 2000 ms")
    ranked, matches = rank_public_rag_evidence(records, facts)
    assert [item["item_id"] for item in ranked] == ["pass", "miss", "empty", "unknown"]
    assert [match["latency"]["status"] for match in matches] == ["satisfied", "not_satisfied", "unknown", "unknown"]
    assert matches[1]["latency"]["relative_violation"] == pytest.approx(0.5)
    assert all(match["workload"]["requested"] == {} for match in matches)


@pytest.mark.parametrize("with_workload", [False, True])
@pytest.mark.parametrize("with_target", [False, True])
def test_experiment_ranking_workload_first_then_lower_latency(with_workload, with_target):
    facts = normalize_planning_facts(
        PlanningFacts(
            8,
            80,
            5,
            workload_profile=WorkloadProfile(mean_input_tokens=1000) if with_workload else None,
        ),
        "TTFT mean <= 1000 ms" if with_target else "",
    )
    records = [
        {**_observation("a-near-boundary", value="990", workload={"input_tokens": "1000"}), "statistic": "mean"},
        {**_observation("z-fast-matching", value="10", workload={"input_tokens": "1000"}), "statistic": "mean"},
        {**_observation("far-fastest", value="1", workload={"input_tokens": "10000"}), "statistic": "mean"},
        {**_observation("zero", value="0", workload={"input_tokens": "1000"}), "statistic": "mean"},
    ]
    ranked, annotations = rank_public_rag_evidence(records, facts)
    identities = [entry["item_id"] for entry in ranked]
    assert identities[0] == ("z-fast-matching" if with_workload else "far-fastest")
    assert identities.index("z-fast-matching") < identities.index("a-near-boundary") < identities.index("zero")
    assert annotations[0]["ranking"]["policy"] == "workload-first-lower-latency-v1"


def test_latency_pareto_preserves_tradeoffs_and_separates_series_statistics():
    facts = normalize_planning_facts(PlanningFacts(8, 80, 5), "TTFT mean < 1000 ms; TPOT mean < 10 ms")
    records = []
    for experiment, ttft, tpot in (("fast-prefill", 5, 9), ("fast-decode", 10, 5), ("dominated", 20, 10)):
        records.extend(
            [
                {**_observation(experiment + "-ttft", experiment=experiment, value=str(ttft)), "statistic": "mean"},
                {
                    **_observation(
                        experiment + "-tpot", experiment=experiment, metric="tpot", value=str(tpot), unit="ms/token"
                    ),
                    "statistic": "mean",
                },
            ]
        )
    records.append(
        {
            **_observation("other-series", experiment="fast-decode", value="1", series={"concurrency": 100}),
            "statistic": "p99",
        }
    )
    _, matches = rank_public_rag_evidence(records, facts)
    by_id = {entry["evidence_id"].removeprefix("public-rag:"): entry["ranking"] for entry in matches}
    assert by_id["fast-prefill-ttft"]["dominated_by"] == 0
    assert by_id["fast-decode-ttft"]["dominated_by"] == 0
    assert by_id["dominated-ttft"]["dominated_by"] == 2
    assert by_id["other-series"]["latency_values_ms"] == {}
    assert by_id["fast-decode-ttft"]["latency_values_ms"] == {"ttft:mean": 10, "tpot:mean": 5}


def test_workload_missing_fields_do_not_outscore_complete_exact_match():
    facts = PlanningFacts(8, 80, 5, workload_profile=WorkloadProfile(mean_input_tokens=1000, mean_output_tokens=100))
    records = [
        _observation("partial", workload={"input_tokens": 1000}, value="1"),
        _observation("complete", workload={"input_tokens": 1000, "output_tokens": 100}, value="100"),
    ]
    ranked, _ = rank_public_rag_evidence(records, facts)
    assert ranked[0]["item_id"] == "complete"


def test_nominal_report_shape_is_context_not_measured_ranking_workload():
    facts = PlanningFacts(8, 80, 5, workload_profile=WorkloadProfile(mean_input_tokens=1000))
    records = [
        _observation(
            "nominal",
            value="1",
            report="600-token shared system prompt + 400-token question, 100-token output",
        ),
        _observation("measured", value="100", workload={"input_tokens": 1100}),
    ]
    ranked, annotations = rank_public_rag_evidence(records, facts)
    assert ranked[0]["item_id"] == "measured"
    assert annotations[1]["workload"]["nominal_prompt_shape"] is not None
    assert annotations[1]["ranking"]["workload_score"] is None


@pytest.mark.parametrize(
    ("requested", "reported", "metric", "value", "statistic", "queue"),
    [
        (None, {}, "throughput", "42", "mean", "fallback"),
        (1000, {}, "ttft", "0", "mean", "fallback"),
        (1000, {}, "ttft", "1", "p99", "fallback"),
        (1000, {"input_tokens": 0}, "throughput", "42", "mean", "ranked"),
        (1000, {"input_tokens": 1000}, "throughput", "42", "mean", "ranked"),
        (None, {}, "ttft", "5", "mean", "ranked"),
    ],
)
def test_experiment_fallback_requires_no_comparable_workload_and_no_valid_latency(
    requested, reported, metric, value, statistic, queue
):
    facts = normalize_planning_facts(
        PlanningFacts(
            8, 80, 5, workload_profile=WorkloadProfile(mean_input_tokens=requested) if requested is not None else None
        ),
        "TTFT mean <= 10 ms",
    )
    entry = {
        **_observation("reference", workload=reported, metric=metric, value=value),
        "statistic": statistic,
    }
    _, matches = rank_public_rag_evidence([entry], facts)
    ranking = matches[0]["ranking"]
    assert ranking["queue"] == queue
    assert ranking["fallback_reason"] == ("no_comparable_workload_or_valid_latency" if queue == "fallback" else None)


@pytest.mark.parametrize("target_statistic", ["mean", "p99"])
def test_missing_statistic_defaults_to_mean_for_ranking_and_slo_comparison(target_statistic):
    facts = normalize_planning_facts(PlanningFacts(8, 80, 5), f"TTFT {target_statistic} <= 100 ms")
    entry = {**_observation("unreported", value="50"), "statistic": None}
    _, matches = rank_public_rag_evidence([entry], facts)
    match = matches[0]
    if target_statistic == "mean":
        assert match["ranking"]["latency_values_ms"] == {"ttft:mean": 50}
        assert match["ranking"]["queue"] == "ranked"
        assert match["latency"]["status"] == "satisfied"
        measured = match["latency"]["fields"]["ttft"]["observations"][0]
        assert measured["statistic"] == "mean"
        assert measured["statistic_original"] is None
        assert measured["statistic_basis"] == "default_mean"
    else:
        assert match["ranking"]["latency_values_ms"] == {}
        assert match["latency"]["status"] == "unknown"
    assert entry["statistic"] is None


def test_public_latency_does_not_mix_series_and_preserves_missing_metrics_and_statistics():
    records = [
        _observation("ttft-rate1", experiment="same", series={"rate": 1}, value="1000"),
        _observation("tpot-rate2", experiment="same", series={"rate": 2}, metric="tpot", value="20", unit="ms/token"),
        _observation("tpot-rate1", experiment="same", series={"rate": 1}, metric="tpot", value="0.04", unit="s/token"),
    ]
    facts = normalize_planning_facts(PlanningFacts(8, 80, 5), "TTFT P90 <= 2000 ms; TPOT P90 <= 30 ms")
    _, matches = rank_public_rag_evidence(records, facts)
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


@pytest.mark.parametrize("mean_value,expected", [(None, "unknown"), ("300", "not_satisfied"), ("100", "satisfied")])
def test_public_latency_checks_every_requested_statistic_in_the_same_experiment(mean_value, expected):
    facts = normalize_planning_facts(PlanningFacts(8, 80, 5), "mean TTFT < 200ms; P95 TTFT < 500ms")
    records = [{**_observation("p95", experiment="same"), "statistic": "p95"}]
    if mean_value is not None:
        records.append({**_observation("mean", value=mean_value, experiment="same"), "statistic": "mean"})
    _, matches = rank_public_rag_evidence(records, facts)
    assert all(match["latency"]["status"] == expected for match in matches)


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
        "llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryDao",
        lambda: SimpleNamespace(hardware_options=lambda _filters: ("Intel B60 XPU", "NVIDIA A100 80GB PCIe")),
    )
    return hardware


@pytest.mark.parametrize("simulated", [False, True])
@pytest.mark.parametrize("measured_vram_gib", [0, 80])
def test_retrieval_hardware_applies_server_simulation_before_requirements(
    monkeypatch, public_hardware, simulated, measured_vram_gib
):
    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_HARDWARE_OVERRIDES", '{"cluster-a":"H100"}' if simulated else "{}")
    public_hardware.update(gpuCount=4, vramBytes=4 * measured_vram_gib * 1024**3)
    requested = PlanningFacts(8, 32, 2)
    resolved, status = asyncio.run(
        resolve_retrieval_hardware(
            "cluster-a",
            "Qwen3-0.6B",
            requested,
            Principal("admin-user", "admin", bindings=(RoleBinding("admin"),)),
        )
    )
    assert status["status"] == "available"
    assert resolved.requirements.hardware.device_model == ("H100" if simulated else "A100")
    assert resolved.planning_hardware_model == ("H100" if simulated else None)
    assert status["hardware"] == ("H100" if simulated else "NVIDIA-A100-80GB-PCIe")
    assert resolved.free_gpu_count == requested.free_gpu_count
    assert resolved.vram_per_gpu_gib == requested.vram_per_gpu_gib
    assert public_hardware["accelerators"][0]["models"] == ["NVIDIA-A100-80GB-PCIe", "ampere"]
    assert public_hardware["vramBytes"] == 4 * measured_vram_gib * 1024**3
    if simulated:
        assert status["actual_model"] == "NVIDIA-A100-80GB-PCIe"
        assert status["simulation"] is True
        assert resolved.requirements.hardware.memory_gib == (measured_vram_gib or None)
    else:
        assert "simulation" not in status


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

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryService", forbidden_service)
    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryDao", forbidden_service)
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

        def query(self, request, caller, *, all_matches=False, experiment_limit=None):
            assert not all_matches
            assert experiment_limit == 50
            calls.append((request, caller))
            return _public_response(evidence=has_evidence)

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryService", QueryService)
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
    assert result.details["retrieval"]["all_matches"] is False
    assert result.details["retrieval"]["mode"] == ("approximate_workload" if expected_workload else "model_hardware")
    assert result.details["target_hardware"]["model"] == ("H100" if simulated else "NVIDIA-A100-80GB-PCIe")
    assert query.include_internal is False
    assert query.limit == 50
    assert result.status == ("available" if has_evidence else "no_matches")
    assert "SQL measured observations" in result.details["message"]
    if simulated:
        assert "Internal experiments are reported separately" in result.details["message"]
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

        def query(self, request, caller, *, all_matches=False, experiment_limit=None):
            raise failure

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryService", QueryService)
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
        "llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryDao",
        lambda: SimpleNamespace(
            hardware_options=lambda _filters: ("Intel B60 XPU", "NVIDIA A100 40GB PCIe", "NVIDIA A100 SXM4 40GB")
        ),
    )

    class QueryService:
        def __init__(self, *, dao):
            pass

        def query(self, request, caller, *, all_matches=False, experiment_limit=None):
            calls.append(request)
            return _public_response(evidence=profile_id == "intel-xpu")

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryService", QueryService)
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

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryDao", forbidden_dao)
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

        def query(self, request, caller, *, all_matches=False, experiment_limit=None):
            calls.append(request)
            return _public_response()

    monkeypatch.setattr("llm_d_lens.agentic.facts.PublicBenchmarkRecordQueryService", QueryService)
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


@pytest.mark.parametrize("nested", [False, True])
def test_test_capacity_override_is_scoped_private_and_idempotent(monkeypatch, nested):
    from llm_d_lens.agentic.facts import planning_hardware_overview

    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_VRAM_GIB_OVERRIDES", '{"cluster-a":80}')
    hardware = {"gpuCount": 7, "availableGpuCount": 3, "vramBytes": 0}
    original = {"kubernetes": {"hardware": hardware}} if nested else {"hardware": hardware}
    assert planning_hardware_overview(original, None, cluster_id="cluster-b") is original
    effective = planning_hardware_overview(original, None, cluster_id="cluster-a")
    repeated = planning_hardware_overview(effective, None, cluster_id="cluster-a")
    effective_hardware = (repeated["kubernetes"] if nested else repeated)["hardware"]
    assert hardware["vramBytes"] == 0
    assert effective_hardware["gpuCount"] == 7
    assert effective_hardware["availableGpuCount"] == 3
    assert effective_hardware["vramBytes"] == 7 * 80 * 1024**3
    assert effective_hardware["planningCapacitySimulation"]["observed_vram_bytes"] == 0
    assert effective_hardware["planningCapacitySimulation"]["simulation"] is True
    monkeypatch.delenv("AGENTIC_TEST_CLUSTER_VRAM_GIB_OVERRIDES")
    assert planning_hardware_overview(original, None, cluster_id="cluster-a") is original


@pytest.mark.parametrize(
    "configured",
    [
        "[]",
        "{",
        '{"cluster-a":true}',
        '{"cluster-a":0}',
        '{"cluster-a":"80"}',
        '{"cluster-a":-1}',
        '{"cluster-a":NaN}',
        '{"cluster-a":Infinity}',
    ],
)
def test_test_capacity_override_rejects_invalid_configuration(monkeypatch, configured):
    from llm_d_lens.agentic.facts import planning_hardware_overview
    from llm_d_lens.core.exceptions import DomainValidationError

    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_VRAM_GIB_OVERRIDES", configured)
    with pytest.raises(DomainValidationError, match="Test cluster VRAM overrides"):
        planning_hardware_overview({"hardware": {"gpuCount": 7}}, None, cluster_id="cluster-a")


def test_resolver_marks_test_capacity_and_keeps_real_count(monkeypatch):
    monkeypatch.setenv("AGENTIC_TEST_CLUSTER_VRAM_GIB_OVERRIDES", '{"cluster-a":80}')
    monkeypatch.setattr(
        "llm_d_lens.agentic.facts.registry.require_cluster", lambda cluster_id: SimpleNamespace(id=cluster_id)
    )

    async def overview(_cluster):
        return {"hardware": {"gpuCount": 7, "availableGpuCount": 3, "vramBytes": 0}}

    monkeypatch.setattr("llm_d_lens.agentic.facts.build_overview", overview)
    resolved = asyncio.run(
        resolve_planning_facts("cluster-a", "", PlanningFacts(8, 1, 0), include_supplementary_evidence=False)
    )
    assert resolved.facts.vram_per_gpu_gib == 80
    assert resolved.facts.free_gpu_count == 3
    assert resolved.evidence[0].details["simulation"] is True
    assert resolved.evidence[0].details["observed_vram_bytes"] == 0
    monkeypatch.delenv("AGENTIC_TEST_CLUSTER_VRAM_GIB_OVERRIDES")
    with pytest.raises(ValueError, match="usable accelerator VRAM"):
        asyncio.run(
            resolve_planning_facts("cluster-a", "", PlanningFacts(8, 1, 0), include_supplementary_evidence=False)
        )


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
    monkeypatch.setattr("llm_d_lens.simulation.service.list_tasks", unavailable_simulation)

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
    monkeypatch.setattr("llm_d_lens.simulation.service.list_tasks", history)

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

    assert captured == {}
    simulation_evidence = next(item for item in resolved.evidence if item.id == "simulation-history")
    assert simulation_evidence.status == "unavailable"
    assert simulation_evidence.details["reason"] == "unscoped_history_disabled"
    assert "observations" not in simulation_evidence.details
