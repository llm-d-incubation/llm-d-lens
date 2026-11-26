"""Tests for the AISimulate estimate adapter."""

from types import SimpleNamespace

import pytest

from llm_d_lens.estimate_tools.aic import service


def test_fixed_estimate_uses_engine_replay(monkeypatch):
    from aisimulate.runner import EngineReplayRunner

    captured = []

    def replay(runner, spec, **kwargs):
        captured.append(spec)
        return SimpleNamespace(
            metrics={
                "mean_ttft_ms": 100.0,
                "mean_tpot_ms": 10.0,
                "output_throughput_tok_s": 200.0,
                "completed_requests": 32,
                "num_requests": 32,
                "duration_ms": 5000.0,
                "num_ttft_samples": 32,
                "num_tpot_samples": 32,
            },
            metadata={},
        )

    monkeypatch.setattr(EngineReplayRunner, "run", replay)
    result = service.estimate_sync(
        estimate_request(
            model_name="example/model",
            scenario="inference_scheduling",
            gpu_count=4,
            tp=2,
            replicas=2,
            ttft_target_ms=2000,
        )
    )
    assert len(captured) == 1
    assert captured[0].backend_deployment.num_workers == 2
    assert captured[0].backend_deployment.agg_engine_args["tensor_parallel_size"] == 2
    assert result["ttft_ms"] == 100.0
    assert result["throughput_tokens_per_sec"] == 200.0
    assert result["execution_mode"] == "engine_replay"


def estimate_request(**kwargs):
    return service.AICEstimateRequest(aic_system_name="test-system", **kwargs)


def test_recommend_uses_engine_sweeper_without_replica_scaling(monkeypatch):
    from aisimulate.sweeper import Sweeper

    captured = []

    def sweep(sweeper, config, **kwargs):
        captured.append(config)
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    candidate_id="candidate-000001",
                    status="feasible",
                    used_gpus=4,
                    config={"deployment_mode": "agg", "tp": 2, "replicas": 2},
                    metrics={
                        "mean_ttft_ms": 100,
                        "mean_tpot_ms": 10,
                        "output_throughput_tok_s": 200,
                        "num_requests": 32,
                        "completed_requests": 32,
                        "duration_ms": 5000,
                        "num_ttft_samples": 32,
                        "num_tpot_samples": 32,
                    },
                )
            ],
            views=SimpleNamespace(top_n=["candidate-000001"]),
        )

    monkeypatch.setattr(Sweeper, "run", sweep)
    monkeypatch.setattr(service, "_cli", lambda: pytest.fail("No analytical recommendation"))
    result = service.search_sync(
        service.AICRequest(
            model_name="Qwen/Qwen3-8B",
            aic_system_name="h100_sxm",
            gpu_count=8,
            target_concurrency=8,
        )
    )
    assert len(captured) == 1
    assert captured[0].search_space.gpu_budget == 8
    assert captured[0].workload.concurrency == 8
    assert result.configs[0]["replicas"] == 2
    assert result.configs[0]["num_total_gpus"] == 4
    assert result.configs[0]["throughput_tokens_per_sec"] == 200
    assert result.configs[0]["execution_mode"] == "engine_replay"


@pytest.mark.parametrize("target", ["target_concurrency", "target_request_rate"])
def test_search_routes_explicit_load_to_engine_sweeper(monkeypatch, target):
    captured = sweep_stub(monkeypatch)
    result = service.search_sync(
        service.AICRequest(model_name="Qwen/Qwen3-8B", aic_system_name="test-system", gpu_count=8, **{target: 10})
    )
    config = captured[0]
    assert getattr(config.workload, "concurrency" if target == "target_concurrency" else "request_rate") == 10
    assert config.workload.isl == 4000
    assert config.workload.osl == 1000
    assert config.goal.target == "min_gpus"
    assert config.sweep.algorithm == "random"
    assert config.sweep.seed == 42
    assert config.sweep.parallel_evals == 1
    assert config.sweep.max_trials == 32
    assert result.configs[0]["replicas"] == 2
    assert result.configs[0]["num_total_gpus"] == 4
    assert result.configs[0]["throughput_tokens_per_sec"] == 200


def test_search_rejects_joint_load_targets():
    with pytest.raises(ValueError, match="choose one and clear the other"):
        service.AICRequest(
            model_name="Qwen/Qwen3-8B",
            aic_system_name="test-system",
            target_concurrency=10,
            target_request_rate=2,
        )


def test_search_replay_defaults_are_explicit():
    request = service.AICRequest(model_name="Qwen/Qwen3-8B", aic_system_name="test-system")
    assert request.mean_input_tokens == 4000
    assert request.mean_output_tokens == 1000
    assert request.num_requests == 32
    assert request.max_evaluations == 32


def test_search_without_load_target_uses_replay_throughput_objective(monkeypatch):
    captured = sweep_stub(monkeypatch)
    service.search_sync(service.AICRequest(model_name="model", aic_system_name="system", gpu_count=4))
    assert captured[0].search_space.gpu_budget == 4
    assert captured[0].sweep.algorithm == "random"
    assert captured[0].goal.target == "throughput"
    assert captured[0].workload.concurrency == 1
    assert captured[0].workload.request_rate is None


@pytest.mark.parametrize("elapsed", [60.0, 61.0])
@pytest.mark.parametrize("feasible", [True, False])
def test_search_stops_at_wall_budget_and_keeps_completed_predictions(monkeypatch, elapsed, feasible):
    from aisimulate.sweeper import Sweeper

    sweep_stub(monkeypatch)
    run = Sweeper.run
    clock = [100.0]
    monkeypatch.setattr(service, "monotonic", lambda: clock[0])

    def bounded_run(sweeper, config, *, on_candidate, **kwargs):
        record = run(sweeper, config, **kwargs).candidates[0]
        if not feasible:
            record.status = "infeasible"
        clock[0] += elapsed
        on_candidate(record)
        pytest.fail("Search must stop after the wall budget rather than starting another candidate")

    monkeypatch.setattr(Sweeper, "run", bounded_run)
    if not feasible:
        with pytest.raises(service.AICError, match="wall-clock budget was exhausted"):
            service.search_sync(service.AICRequest(model_name="model", aic_system_name="system"))
        return
    result = service.search_sync(service.AICRequest(model_name="model", aic_system_name="system"))

    assert result.evaluated_candidates == 1
    assert result.configs[0]["num_total_gpus"] == 4
    assert not result.search_complete
    assert any("wall-clock budget" in warning for warning in result.warnings)


def test_legacy_experiment_normalization_scales_both_pd_worker_pools():
    result = service._candidate(
        "disagg",
        {
            "(p)tp": 2,
            "(p)workers": 1,
            "(d)tp": 1,
            "(d)workers": 2,
            "num_total_gpus": 4,
            "replicas_needed": 3,
            "tokens/s": 100,
        },
    )
    assert result["prefill_replicas"] == 3
    assert result["decode_replicas"] == 6
    assert result["num_total_gpus"] == 12
    assert result["throughput_tokens_per_sec"] == 300


def test_search_rejects_closest_recommendation_that_exceeds_slo(monkeypatch):
    sweep_stub(monkeypatch, mean_tpot_ms=50)
    with pytest.raises(service.AICError, match="no complete configuration meeting the TTFT/TPOT"):
        service.search_sync(
            service.AICRequest(
                model_name="model",
                aic_system_name="system",
                gpu_count=4,
                target_concurrency=10,
            )
        )


def test_search_rejects_recommendation_above_capacity(monkeypatch):
    sweep_stub(monkeypatch, used_gpus=6)
    with pytest.raises(service.AICError, match="4-GPU budget"):
        service.search_sync(
            service.AICRequest(
                model_name="Qwen/Qwen3-8B",
                aic_system_name="test-system",
                gpu_count=4,
                target_request_rate=10,
            )
        )


def sweep_stub(monkeypatch, *, used_gpus=4, topology=None, status="feasible", **metrics):
    from aisimulate.sweeper import Sweeper

    captured = []

    def sweep(sweeper, config, **kwargs):
        captured.append(config)
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    status=status,
                    used_gpus=used_gpus,
                    config=topology or {"deployment_mode": "agg", "tp": 2, "replicas": 2},
                    metrics={
                        "mean_ttft_ms": 100,
                        "mean_tpot_ms": 10,
                        "output_throughput_tok_s": 200,
                        "num_requests": config.workload.request_count,
                        "completed_requests": config.workload.request_count,
                        "duration_ms": 5000,
                        "num_ttft_samples": 32,
                        "num_tpot_samples": 32,
                        **metrics,
                    },
                )
            ]
        )

    monkeypatch.setattr(Sweeper, "run", sweep)
    monkeypatch.setattr(service, "_cli", lambda: pytest.fail("No analytical fallback"))
    return captured


@pytest.mark.parametrize(
    "changes",
    [
        {"mean_ttft_ms": float("inf")},
        {"completed_requests": 31},
        {"num_ttft_samples": 0},
        {"duration_ms": 0},
        {"status": "failed"},
        {"topology": {"deployment_mode": "agg", "tp": 1, "replicas": 1, "attention_dp": 4}},
    ],
)
def test_recommend_rejects_unverifiable_candidates(monkeypatch, changes):
    sweep_stub(monkeypatch, **changes)
    with pytest.raises(service.AICError, match="bounded search"):
        service.search_sync(service.AICRequest(model_name="model", aic_system_name="system"))


def test_recommend_preserves_pd_worker_allocation(monkeypatch):
    sweep_stub(
        monkeypatch,
        used_gpus=5,
        topology={
            "deployment_mode": "disagg",
            "prefill_tp": 1,
            "prefill_replicas": 2,
            "decode_tp": 1,
            "decode_replicas": 3,
        },
    )
    result = service.search_sync(service.AICRequest(model_name="model", aic_system_name="system"))
    assert result.configs[0]["prefill_replicas"] == 2
    assert result.configs[0]["decode_replicas"] == 3
    assert result.configs[0]["throughput_tokens_per_sec"] == 200


def test_recommend_rejects_fractional_concurrency(monkeypatch):
    sweep_stub(monkeypatch)
    with pytest.raises(service.AICError, match="integer concurrency"):
        service.search_sync(
            service.AICRequest(
                model_name="model",
                aic_system_name="system",
                target_concurrency=1.5,
            )
        )


def test_estimate_rejects_a_different_aggregated_topology(monkeypatch):
    from aisimulate.runner import EngineReplayRunner

    def fail(*args, **kwargs):
        raise RuntimeError("replay unavailable")

    monkeypatch.setattr(EngineReplayRunner, "run", fail)
    monkeypatch.setattr(service, "experiments_sync", lambda request: pytest.fail("No analytical fallback"))
    with pytest.raises(service.AICError, match="replay unavailable"):
        service.estimate_sync(
            estimate_request(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=4,
                tp=2,
                replicas=2,
                ttft_target_ms=200,
            )
        )


def test_estimate_passes_exact_aggregated_topology_and_workload(monkeypatch):
    captured = replay_stub(monkeypatch)
    result = service.estimate_sync(
        estimate_request(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-8B",
            gpu_count=4,
            tp=2,
            replicas=2,
            mean_input_tokens=4096,
            mean_output_tokens=256,
            ttft_target_ms=200,
        )
    )

    assert result["ttft_ms"] == 100
    assert result["num_total_gpus"] == 4
    assert captured[0].backend_deployment.num_workers == 2
    assert captured[0].backend_deployment.agg_engine_args["tensor_parallel_size"] == 2
    assert captured[0].workload["isl"] == 4096


def test_estimate_rejects_inconsistent_gpu_count(monkeypatch):
    monkeypatch.setattr(service, "experiments_sync", lambda request: pytest.fail("SDK must not be called"))

    with pytest.raises(service.AICError, match="requires 4 GPUs"):
        service.estimate_sync(
            estimate_request(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=8,
                tp=2,
                replicas=2,
            )
        )


def test_estimate_rejects_unverifiable_pipeline_parallelism(monkeypatch):
    monkeypatch.setattr(service, "experiments_sync", lambda request: pytest.fail("SDK must not be called"))
    with pytest.raises(service.AICError, match="pipeline parallelism"):
        service.estimate_sync(
            estimate_request(
                scenario="inference_scheduling",
                model_name="Qwen/Qwen3-8B",
                gpu_count=4,
                tp=2,
                pp=2,
            )
        )


def test_estimate_rejects_a_different_disaggregated_worker_allocation(monkeypatch):
    replay_stub(monkeypatch, completed_requests=31)
    with pytest.raises(service.AICError, match="complete every"):
        service.estimate_sync(
            estimate_request(
                scenario="pd_disaggregation",
                model_name="Qwen/Qwen3-8B",
                gpu_count=3,
                prefill_tp=2,
                prefill_replicas=1,
                decode_tp=1,
                decode_replicas=1,
                ttft_target_ms=200,
            )
        )


def test_estimate_accepts_matching_disaggregated_topology(monkeypatch):
    captured = replay_stub(monkeypatch)
    result = service.estimate_sync(
        estimate_request(
            scenario="pd_disaggregation",
            model_name="Qwen/Qwen3-8B",
            gpu_count=4,
            prefill_tp=2,
            prefill_replicas=1,
            decode_tp=1,
            decode_replicas=2,
            ttft_target_ms=200,
        )
    )

    assert result["tpot_ms"] == 10
    deployment = captured[0].backend_deployment
    assert deployment.prefill_engine_args["tensor_parallel_size"] == 2
    assert deployment.decode_engine_args["tensor_parallel_size"] == 1
    assert deployment.num_prefill_workers == 1
    assert deployment.num_decode_workers == 2
    assert result["num_total_gpus"] == 4


def test_estimate_without_targets_uses_replay_with_fixed_lengths(monkeypatch):
    captured = replay_stub(monkeypatch)
    result = service.estimate_sync(
        estimate_request(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=1,
            tp=1,
            replicas=1,
            mean_input_tokens=1024,
            mean_output_tokens=256,
        )
    )

    assert result["ttft_ms"] == 100
    assert result["throughput_tokens_per_sec"] == 200
    assert captured[0].workload["isl"] == 1024
    assert captured[0].workload["osl"] == 256
    assert captured[0].workload["random_range_ratio"] == 1.0


def test_estimate_rejects_zero_latency_samples(monkeypatch):
    replay_stub(monkeypatch, num_ttft_samples=0)
    with pytest.raises(service.AICError, match="num_ttft_samples"):
        service.estimate_sync(
            estimate_request(
                scenario="pd_disaggregation",
                model_name="Qwen/Qwen3-0.6B",
                gpu_count=2,
                prefill_tp=1,
                prefill_replicas=1,
                decode_tp=1,
                decode_replicas=1,
            )
        )


def test_estimate_without_targets_replays_disaggregation(monkeypatch):
    captured = replay_stub(monkeypatch)
    result = service.estimate_sync(
        estimate_request(
            scenario="pd_disaggregation",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=5,
            prefill_tp=1,
            prefill_replicas=2,
            decode_tp=1,
            decode_replicas=3,
            mean_input_tokens=1024,
            mean_output_tokens=256,
        )
    )

    assert result["throughput_tokens_per_sec"] == 200
    assert result["prefill_replicas"] == 2
    assert result["decode_replicas"] == 3
    assert captured[0].backend_deployment.num_prefill_workers == 2
    assert captured[0].backend_deployment.num_decode_workers == 3
    assert captured[0].workload["isl"] == 1024 and captured[0].workload["osl"] == 256


def test_estimate_reports_measured_multi_replica_throughput(monkeypatch):
    replay_stub(monkeypatch)
    result = service.estimate_sync(
        estimate_request(
            scenario="inference_scheduling",
            model_name="Qwen/Qwen3-0.6B",
            gpu_count=2,
            tp=1,
            replicas=2,
        )
    )

    assert result["replicas"] == 2
    assert result["throughput_tokens_per_sec"] == 200


def replay_stub(monkeypatch, **changes):
    from aisimulate.runner import EngineReplayRunner

    captured = []

    def replay(runner, spec, **kwargs):
        captured.append(spec)
        return SimpleNamespace(
            metrics={
                "mean_ttft_ms": 100,
                "mean_tpot_ms": 10,
                "output_throughput_tok_s": 200,
                "num_requests": 32,
                "completed_requests": 32,
                "duration_ms": 5000,
                "num_ttft_samples": 32,
                "num_tpot_samples": 32,
                **changes,
            }
        )

    monkeypatch.setattr(EngineReplayRunner, "run", replay)
    return captured


@pytest.mark.parametrize("load", [{"target_concurrency": 4}, {"target_request_rate": 2.0}])
def test_replay_honors_explicit_load_and_request_count(monkeypatch, load):
    from aisimulate.runner import _materialize_requests

    captured = replay_stub(monkeypatch, num_requests=8, completed_requests=8)
    service.estimate_sync(
        estimate_request(
            model_name="example/model",
            scenario="inference_scheduling",
            gpu_count=1,
            num_requests=8,
            **load,
        )
    )
    spec = captured[0]
    requests, concurrency = _materialize_requests(spec, 512)
    assert len(requests) == 8
    assert "source_type" not in spec.workload
    if "target_concurrency" in load:
        assert concurrency == 4
    else:
        assert concurrency is None
        assert spec.workload["request_rate"] == 2.0


@pytest.mark.parametrize(
    "changes",
    [
        {"mean_ttft_ms": float("nan")},
        {"mean_tpot_ms": -1},
        {"duration_ms": 0},
        {"num_requests": 31},
        {"num_tpot_samples": 0},
    ],
)
def test_replay_rejects_invalid_report(monkeypatch, changes):
    replay_stub(monkeypatch, **changes)
    with pytest.raises(service.AICError):
        service.estimate_sync(
            estimate_request(
                model_name="example/model",
                scenario="inference_scheduling",
                gpu_count=1,
            )
        )


def test_native_nan_values_do_not_crash_candidate_serialization(monkeypatch):
    import pandas as pd

    result = SimpleNamespace(
        chosen_exp="agg",
        best_configs={
            "agg": pd.DataFrame(
                [
                    {"tp": 2, "num_total_gpus": 2, "replicas_needed": float("nan"), "ttft": float("nan"), "tpot": 10},
                ]
            )
        },
    )
    monkeypatch.setattr(service, "_cli", lambda: (None, lambda **_: result, None))
    response = service.experiments_sync(service.AICExperimentRequest(yaml_text="agg: {}"))
    assert response.experiments[0]["replicas"] == 1
    assert response.experiments[0]["ttft_ms"] is None


def test_invalid_native_experiment_result_becomes_aic_error(monkeypatch):
    result = SimpleNamespace(chosen_exp="agg", best_configs={"agg": None})
    monkeypatch.setattr(service, "_cli", lambda: (None, lambda **_: result, None))
    with pytest.raises(service.AICError):
        service.experiments_sync(service.AICExperimentRequest(yaml_text="agg: {}"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, 1.5])
def test_native_invalid_topology_is_not_replaced_with_a_fabricated_count(value):
    with pytest.raises(service.AICError, match="invalid topology"):
        service._candidate("agg", {"tp": value, "num_total_gpus": 2})


@pytest.mark.parametrize(
    "name,row,mode",
    [
        ("manual", {"tp": 2, "num_total_gpus": 4}, "agg"),
        ("agg_vllm", {"tp": 2, "num_total_gpus": 4}, "agg"),
        ("my_pd_experiment", {"(p)tp": 1, "(d)tp": 2, "(p)workers": 2, "(d)workers": 1, "num_total_gpus": 4}, "disagg"),
    ],
)
def test_experiment_names_do_not_determine_serving_mode(monkeypatch, name, row, mode):
    import pandas as pd

    result = SimpleNamespace(chosen_exp=name, best_configs={name: pd.DataFrame([row])})
    monkeypatch.setattr(service, "_cli", lambda: (None, lambda **_: result, None))
    response = service.experiments_sync(service.AICExperimentRequest(yaml_text="manual: {}"))
    assert response.experiments[0]["mode"] == mode
    assert response.experiments[0]["num_total_gpus"] == 4
    assert response.experiments[0]["is_chosen"]
