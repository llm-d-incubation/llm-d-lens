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


@pytest.mark.parametrize("target", ["target_concurrency", "target_request_rate"])
def test_search_routes_load_target_to_recommend_and_scales_replicas(monkeypatch, target):
    import pandas as pd
    from aisimulate.legacy_cli import api

    captured = {}

    def recommend(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            chosen_exp="agg",
            best_configs={
                "agg": pd.DataFrame(
                    [{"tp": 2, "num_total_gpus": 2, "replicas_needed": 3, "tokens/s": 100, "ttft": 100, "tpot": 10}]
                )
            },
        )

    monkeypatch.setattr(api, "cli_recommend", recommend)
    monkeypatch.setattr(service, "_cli", lambda: (lambda **kwargs: pytest.fail("default called"), None, None))
    result = service.search_sync(
        service.AICRequest(model_name="Qwen/Qwen3-8B", aic_system_name="test-system", gpu_count=8, **{target: 10})
    )
    assert captured[target] == 10
    assert captured["isl"] == 4000
    assert captured["osl"] == 1000
    assert "total_gpus" not in captured
    assert result.configs[0]["replicas"] == 3
    assert result.configs[0]["num_total_gpus"] == 6
    assert result.configs[0]["throughput_tokens_per_sec"] == 300


def test_search_rejects_joint_load_targets():
    with pytest.raises(ValueError, match="choose one and clear the other"):
        service.AICRequest(
            model_name="Qwen/Qwen3-8B",
            aic_system_name="test-system",
            target_concurrency=10,
            target_request_rate=2,
        )


def test_search_defaults_match_native_search_and_recommend():
    from inspect import signature

    from aisimulate.legacy_cli.api import cli_default, cli_recommend

    request = service.AICRequest(model_name="Qwen/Qwen3-8B", aic_system_name="test-system")
    for operation in (cli_default, cli_recommend):
        assert request.mean_input_tokens == signature(operation).parameters["isl"].default
        assert request.mean_output_tokens == signature(operation).parameters["osl"].default


def test_search_without_load_target_keeps_default_search(monkeypatch):
    captured = {}

    def default(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(best_configs={}, chosen_exp=None)

    monkeypatch.setattr(service, "_cli", lambda: (default, None, None))
    service.search_sync(service.AICRequest(model_name="model", aic_system_name="system", gpu_count=4))
    assert captured["total_gpus"] == 4
    assert "target_concurrency" not in captured
    assert "target_request_rate" not in captured


def test_search_scales_both_pd_worker_pools():
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
    import pandas as pd
    from aisimulate.legacy_cli import api

    monkeypatch.setattr(service, "_cli", lambda: (None, None, None))
    monkeypatch.setattr(
        api,
        "cli_recommend",
        lambda **kwargs: SimpleNamespace(
            chosen_exp="agg",
            best_configs={"agg": pd.DataFrame([{"tp": 1, "num_total_gpus": 1, "ttft": 3000, "tpot": 50}])},
        ),
    )
    with pytest.raises(service.AICError, match="no configuration meeting the TTFT/TPOT"):
        service.search_sync(
            service.AICRequest(
                model_name="model",
                aic_system_name="system",
                gpu_count=4,
                target_concurrency=10,
            )
        )


def test_search_rejects_recommendation_above_capacity(monkeypatch):
    import pandas as pd
    from aisimulate.legacy_cli import api

    monkeypatch.setattr(service, "_cli", lambda: (None, None, None))
    monkeypatch.setattr(
        api,
        "cli_recommend",
        lambda **kwargs: SimpleNamespace(
            chosen_exp="agg",
            best_configs={"agg": pd.DataFrame([{"tp": 2, "num_total_gpus": 2, "replicas_needed": 3}])},
        ),
    )
    with pytest.raises(service.AICError, match="Minimum recommended GPUs: 6"):
        service.search_sync(
            service.AICRequest(
                model_name="Qwen/Qwen3-8B",
                aic_system_name="test-system",
                gpu_count=4,
                target_request_rate=10,
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
