"""Configuration-based benchmark budgets, not hardware performance predictions."""

from math import ceil

from .models import BenchmarkSpec


def benchmark_timing(spec: BenchmarkSpec) -> dict:
    """Estimate generated workloads and resolve a bounded per-invocation timeout."""
    phases = {}
    requests = 0
    parallelism = spec.parallelism

    def stage_seconds(count, input_tokens, output_tokens, concurrency, duration=0):
        aggregate = count * parallelism * (input_tokens / 2000 + output_tokens / 200)
        serial = ceil(count / concurrency) * (input_tokens / 1000 + output_tokens / 20)
        return max(duration, aggregate, serial)

    def phase(name, seconds, minimum_seconds=0):
        phases[name] = {
            "lower_seconds": ceil(120 + max(minimum_seconds, seconds / 4)),
            "upper_seconds": ceil(300 + seconds),
        }

    prefix = spec.shared_prefix
    if spec.harness == "inference-perf" and prefix and not prefix.enable_multi_turn_chat:
        seconds = 0
        for stage in prefix.stages:
            count = int(stage.rate * stage.duration)
            requests += count * parallelism
            seconds += stage_seconds(
                count, prefix.system_prompt_len + prefix.question_len, prefix.output_len, 8, stage.duration
            )
        phase("benchmark", seconds, sum(stage.duration for stage in prefix.stages))
    elif spec.harness == "inference-perf" and spec.matrix and spec.concurrency_stages:
        for index, point in enumerate(spec.matrix):
            seconds = 0
            for stage in spec.concurrency_stages:
                requests += stage.num_requests * parallelism
                seconds += stage_seconds(stage.num_requests, point.isl, point.osl, stage.concurrency)
            phase(f"matrix-{index}", seconds)
        if spec.warmup_requests:
            point = spec.matrix[0]
            phase("warmup", stage_seconds(spec.warmup_requests, point.isl, point.osl, 1))

    upper = max((value["upper_seconds"] for value in phases.values()), default=0)
    recommended = min(14400, max(900, ceil(upper * 1.5 + 120))) if phases else 7200
    effective = spec.wait_timeout_seconds if spec.wait_timeout_seconds is not None else recommended
    return {
        "basis": "configuration" if phases else "unknown",
        "confidence": "low",
        "measured_requests": requests if phases else None,
        "lower_seconds": sum(value["lower_seconds"] for value in phases.values()) if phases else None,
        "upper_seconds": sum(value["upper_seconds"] for value in phases.values()) if phases else None,
        "phases": phases,
        "timeout_mode": "manual" if spec.wait_timeout_seconds is not None else "auto",
        "timeout_seconds": effective,
        "recommended_timeout_seconds": recommended,
        "timeout_warning": "The timeout may expire before this workload finishes." if upper > effective else None,
        "assumptions": (
            "Rough configuration estimate, not measured hardware speed. Includes 2-5 minutes of harness setup "
            "per invocation; excludes deployment, runtime installation and final analysis. "
            "Assumes aggregate input/output rates of 2000/200 tokens per second and per-request rates of "
            "1000/20, with a fourfold faster lower bound. Cache reuse and hardware may change actual time."
            if phases
            else "Timing unavailable for this workload; automatic timeout uses a 2-hour fallback."
        ),
    }
