"""Measured Evaluate results promoted to immutable Agentic evidence.

Run with ``python -m llm_d_lens.evaluation.benchmark_evidence`` to preview
historical ingestion; ``--apply`` writes only records that pass the same checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from llm_d_lens.agentic.benchmark_context import (
    capture_benchmark_context as capture_serving_context,
)
from llm_d_lens.agentic.benchmark_context import (
    persist_benchmark_records,
    validate_benchmark_context,
)
from llm_d_lens.agentic.benchmark_context import (
    positive as _positive,
)
from llm_d_lens.agentic.benchmark_context import (
    verify_benchmark_context as verify_benchmark_context,
)
from llm_d_lens.agentic.benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    WorkloadProfile,
)
from llm_d_lens.db.dao.evaluate_run import EvaluateRunDao

logger = logging.getLogger(__name__)


async def capture_benchmark_context(run: dict) -> None:
    if run.get("harness") != "inference-perf" or run.get("parallelism") != 1:
        run["benchmark_context_error"] = "Automatic evidence attribution requires single-worker inference-perf"
        logger.warning("Evaluate %s: %s", run["id"], run["benchmark_context_error"])
        return
    await capture_serving_context(run)


def _metrics(metrics: dict) -> BenchmarkMetrics:
    latencies = metrics.get("latency_distributions") or {}
    values = {
        "ttft_p95_ms": (latencies.get("ttft") or {}).get("p95_ms"),
        "tpot_p95_ms": (latencies.get("tpot") or {}).get("p95_ms"),
        "e2e_p95_ms": (latencies.get("request_latency") or {}).get("p95_ms"),
        "throughput_tokens_per_s": metrics.get("throughput_tps"),
    }
    for name, value in values.items():
        if value is not None:
            _positive(value, name)
    if not any(value is not None for value in values.values()):
        raise ValueError("No measured P95 latency or token throughput is available")
    successes, failures = metrics.get("success_count"), metrics.get("failure_count")
    if type(successes) is not int or type(failures) is not int or min(successes, failures) < 0:
        raise ValueError("Explicit measured success and failure counts are required")
    total = successes + failures
    if total <= 0 or type(metrics.get("request_count")) is not int or metrics["request_count"] != total:
        raise ValueError("Measured request counts are missing or inconsistent")
    return BenchmarkMetrics(**values, error_rate=failures / total)


def _concurrency(metrics: dict, stage_index: int | None) -> float:
    path = metrics.get("summary_path")
    if not path:
        raise ValueError("Measured concurrency requires per-request lifecycle artifacts")
    rows = json.loads(Path(path).with_name("per_request_lifecycle_metrics.json").read_text(encoding="utf-8"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid per-request lifecycle artifacts")
    if stage_index is not None and any(type(row.get("stage_id")) is not int for row in rows):
        raise ValueError("Per-request stage identities are missing")
    rows = (
        [row for row in rows if row.get("stage_id") == stage_index]
        if stage_index is not None
        else [row for row in rows if row.get("stage_id") != -1]
    )
    if len(rows) != metrics.get("request_count") or not rows:
        raise ValueError("Per-request coverage is incomplete")
    duration = _positive(metrics.get("benchmark_time_seconds"), "benchmark_time_seconds")
    total = 0.0
    successes = 0
    identities = set()
    for row in rows:
        if "error" not in row:
            raise ValueError("Per-request completion status is missing")
        successes += row["error"] is None
        identity = row.get("request_id")
        if identity is not None:
            if not isinstance(identity, str) or identity in identities:
                raise ValueError("Per-request identities are invalid or duplicated")
            identities.add(identity)
        start, end = row.get("start_time"), row.get("end_time")
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (start, end)):
            raise ValueError("Per-request timestamps are invalid")
        if end < start:
            raise ValueError("Per-request completion precedes its start")
        total += end - start
    if successes != metrics.get("success_count") or len(rows) - successes != metrics.get("failure_count"):
        raise ValueError("Per-request outcomes disagree with the lifecycle summary")
    if max(row["end_time"] for row in rows) - min(row["start_time"] for row in rows) > duration * 1.001 + 0.001:
        raise ValueError("Per-request timestamps exceed the measured duration")
    return _positive(total / duration, "measured mean concurrency")


def _workload(run: dict, metrics: dict, concurrency: Any, stage_index: int | None) -> WorkloadProfile:
    tokens = metrics.get("token_distributions") or {}
    mean_input = _positive((tokens.get("input") or {}).get("mean"), "measured mean input tokens")
    mean_output = _positive((tokens.get("output") or {}).get("mean"), "measured mean output tokens")
    if run.get("matrix"):
        shared_prefix = 0.0  # Generated matrix inputs are random, not shared-prefix traffic.
    elif run.get("shared_prefix"):
        spec = run["shared_prefix"]
        if spec.get("enable_multi_turn_chat"):
            raise ValueError("Multi-turn prefix reuse is not described by a single prefix ratio")
        shared_prefix = _positive(spec.get("system_prompt_len"), "system_prompt_len") / (
            spec["system_prompt_len"] + _positive(spec.get("question_len"), "question_len")
        )
    else:
        workload = yaml.safe_load(run["workload_yaml"]) if run.get("workload_yaml") else None
        if not workload and metrics.get("summary_path"):
            workload = yaml.safe_load(
                Path(metrics["summary_path"]).with_name("config.yaml").read_text(encoding="utf-8")
            )
        data = (workload or {}).get("data") or {}
        if data.get("type") != "random":
            raise ValueError("The saved workload does not establish its shared-prefix semantics")
        shared_prefix = 0.0
        stages = (workload.get("load") or {}).get("stages") or []
        if len(stages) > 1:
            raise ValueError("Mixed workload stages must not be promoted as one benchmark sample")
        if stages:
            concurrency = stages[0].get("concurrency_level")
    return WorkloadProfile(
        mean_input_tokens=mean_input,
        p95_input_tokens=_positive((tokens.get("input") or {}).get("p95"), "measured P95 input tokens"),
        mean_output_tokens=mean_output,
        concurrency=_positive(concurrency, "configured concurrency")
        if concurrency is not None
        else _concurrency(metrics, stage_index),
        request_rate=_positive(metrics.get("throughput_rps"), "measured request rate"),
        shared_prefix_ratio=shared_prefix,
        prefill_fraction=mean_input / (mean_input + mean_output),
    )


def normalize_evaluate_run(run: dict) -> tuple[list[BenchmarkRecord], list[dict]]:
    """Normalize saved evidence only; never consult current planning or live cluster state."""
    issues: list[dict] = []
    try:
        if run.get("kind") != "benchmark" or run.get("status") not in ("succeeded", "failed"):
            raise ValueError("Only succeeded or failed Evaluate benchmark runs are eligible")
        if run.get("harness") != "inference-perf" or type(run.get("parallelism")) is not int or run["parallelism"] != 1:
            raise ValueError("Evidence requires inference-perf with one worker and attributable lifecycle reports")
        run_id = str(UUID(run["id"]))
        timestamp, model, hardware, runtime, candidate = validate_benchmark_context(run)
        if run["status"] == "failed" and not run.get("error"):
            raise ValueError("Failed benchmark evidence requires an explicit failure reason")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return [], [{"sample": "run", "reason": str(error)}]

    samples = []
    if run.get("matrix"):
        for point_index, point in enumerate(run.get("matrix_results") or []):
            for stage_index, stage in enumerate(point.get("stage_metrics") or []):
                samples.append((f":matrix:{point_index}:stage:{stage_index}", stage, stage_index, point.get("status")))
    elif run.get("shared_prefix"):
        samples = [
            (f":stage:{index}", stage, index, run["status"])
            for index, stage in enumerate(run.get("rate_stage_results") or [])
        ]
    else:
        samples = [("", {"metrics": run.get("metrics")}, None, run["status"])]
    if not samples:
        return [], [{"sample": "run", "reason": "No individually measured workload stages are available"}]
    records = []
    for suffix, sample, stage_index, status in samples:
        try:
            if status not in ("succeeded", "failed"):
                raise ValueError("Workload sample has no terminal outcome")
            metrics = sample.get("metrics")
            if not isinstance(metrics, dict):
                raise ValueError("Measured lifecycle summary is missing")
            benchmark_id = f"evaluate:{run_id}{suffix}"
            workload = _workload(run, metrics, sample.get("concurrency"), stage_index)
            record = BenchmarkRecord(
                benchmark_id=benchmark_id,
                timestamp=timestamp,
                outcome=status,
                model=model,
                hardware=hardware,
                runtime=runtime,
                workload=workload,
                configuration=candidate,
                metrics=_metrics(metrics),
                failure_reason=run.get("error") if status == "failed" else None,
            )
            records.append(record)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            issues.append({"sample": suffix or "run", "reason": str(error)})
    return records, issues


def ingest_evaluate_run(run: dict, *, apply: bool = True) -> dict:
    records, issues = normalize_evaluate_run(run)
    return persist_benchmark_records(records, issues, source_id=run["id"], apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist eligible evidence; default is read-only preview")
    parser.add_argument("--run-id", action="append", type=UUID, help="Only process these saved Evaluate run IDs")
    args = parser.parse_args()
    dao = EvaluateRunDao()
    if args.run_id:
        runs = []
        for run_id in args.run_id:
            run = dao.get(str(run_id))
            if run is None:
                parser.error(f"Evaluate run not found: {run_id}")
            runs.append(run)
    else:
        runs = dao.list()
    failed = False
    for run in runs:
        payload = run.model_dump(mode="json")
        report = ingest_evaluate_run(payload, apply=args.apply)
        if args.apply and run.status in ("succeeded", "failed", "cancelled"):
            payload["benchmark_evidence"] = report
            dao.save(type(run).model_validate(payload))
        print(json.dumps({"run_id": run.id, "apply": args.apply, **report}))
        failed = failed or report["status"] == "error"
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
