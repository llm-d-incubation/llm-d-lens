import pytest

from llm_d_bench.deploy.capabilities import shared_prefix_routing_workload
from llm_d_bench.evaluate.models import BenchmarkSpec
from llm_d_bench.evaluate.timing import benchmark_timing


def test_default_budget_scales_with_workload_and_parallel_instances():
    quick = BenchmarkSpec(shared_prefix=shared_prefix_routing_workload())
    full = BenchmarkSpec(shared_prefix=shared_prefix_routing_workload(full=True))
    assert benchmark_timing(quick)["measured_requests"] == 24
    assert benchmark_timing(quick)["timeout_seconds"] == 900
    timing = benchmark_timing(full)
    assert timing["measured_requests"] == 780
    assert 1800 < timing["timeout_seconds"] <= 14400
    assert timing["lower_seconds"] < 2400 < timing["upper_seconds"]
    parallel = benchmark_timing(full.model_copy(update={"parallelism": 2}))
    assert parallel["measured_requests"] == 1560
    assert parallel["upper_seconds"] > timing["upper_seconds"]


def test_manual_timeout_is_preserved_and_warns_if_too_short():
    timing = benchmark_timing(
        BenchmarkSpec(shared_prefix=shared_prefix_routing_workload(full=True), wait_timeout_seconds=60)
    )
    assert timing["timeout_mode"] == "manual"
    assert timing["timeout_seconds"] == 60
    assert timing["timeout_warning"]


def test_estimate_never_shortens_the_configured_traffic_window():
    workload = shared_prefix_routing_workload()
    workload["stages"] = [{"rate": 0.01, "duration": 3600}]
    timing = benchmark_timing(BenchmarkSpec(shared_prefix=workload))
    assert timing["lower_seconds"] >= 3600


def test_matrix_budget_includes_warmup_and_each_point_but_timeout_is_per_invocation():
    timing = benchmark_timing(
        BenchmarkSpec(
            matrix=[{"isl": 128, "osl": 64}, {"isl": 4096, "osl": 256}],
            concurrency_stages=[{"concurrency": 1, "num_requests": 10}],
            warmup_requests=2,
        )
    )
    assert timing["measured_requests"] == 20
    assert set(timing["phases"]) == {"warmup", "matrix-0", "matrix-1"}
    assert timing["upper_seconds"] == sum(phase["upper_seconds"] for phase in timing["phases"].values())


@pytest.mark.parametrize(
    "values",
    [{}, {"harness": "other"}, {"shared_prefix": {**shared_prefix_routing_workload(), "enable_multi_turn_chat": True}}],
)
def test_unknown_workloads_do_not_claim_a_finish_time(values):
    timing = benchmark_timing(BenchmarkSpec(**values))
    assert timing["basis"] == "unknown"
    assert timing["upper_seconds"] is None
    assert timing["timeout_seconds"] == 7200


def test_large_workload_budget_is_bounded_and_warns():
    workload = shared_prefix_routing_workload(full=True)
    workload["stages"] = [{"rate": 100, "duration": 3600}]
    timing = benchmark_timing(BenchmarkSpec(shared_prefix=workload))
    assert timing["timeout_seconds"] == 14400
    assert timing["timeout_warning"]


@pytest.mark.asyncio
async def test_estimate_endpoint_uses_the_execution_budget_without_launching():
    from llm_d_bench.evaluate.router import estimate_benchmark_timing

    spec = BenchmarkSpec(shared_prefix=shared_prefix_routing_workload())
    assert await estimate_benchmark_timing(spec) == benchmark_timing(spec)
