"""Measured Evaluate results promoted to immutable Agentic evidence.

Run with ``python -m llm_d_lens.evaluation.benchmark_evidence`` to preview
historical ingestion; ``--apply`` writes only records that pass the same checks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from llm_d_lens.agentic.benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
    WorkloadProfile,
)
from llm_d_lens.db.dao.agentic_benchmark_record import BenchmarkRecordDao, BenchmarkRecordDaoError
from llm_d_lens.db.dao.evaluate_run import EvaluateRunDao
from llm_d_lens.deploy.contracts import DeployableConfiguration
from llm_d_lens.hardware.resolver import resolve_by_resource
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum
from llm_d_lens.infrastructure.kubernetes import scoped_runner

logger = logging.getLogger(__name__)

# This executes read-only metadata queries inside the actual serving container.
# Never load weights, download a different revision, or execute model custom code.
_MODEL_PROBE = r"""
import json, math, re, sys
from importlib.metadata import version
from pathlib import Path
import torch
from safetensors import safe_open
from huggingface_hub import snapshot_download

source, model_id, revision, quantization = sys.argv[1:]
root = Path(source)
if not root.is_dir():
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A cached Hub model needs an immutable serving revision, not the latest cached branch")
    root = Path(snapshot_download(source, revision=revision or None, local_files_only=True))
config = json.loads((root / "config.json").read_text())
text = config.get("text_config") or config
parameters = 0
seen = set()
files = sorted(root.glob("*.safetensors"))
if not files:
    raise ValueError("Local safetensors parameter metadata is unavailable")
for file in files:
    with safe_open(file, framework="pt", device="cpu") as weights:
        for key in weights.keys():
            if key in seen:
                raise ValueError("Duplicate tensor metadata; parameter count is ambiguous")
            seen.add(key)
            parameters += math.prod(weights.get_slice(key).get_shape())
device = torch.get_device_module()
properties = [device.get_device_properties(i) for i in range(device.device_count())]
if not properties or len({(p.name, p.total_memory) for p in properties}) != 1:
    raise ValueError("Heterogeneous or missing accelerator properties")
experts = text.get("num_local_experts") or text.get("num_experts") or text.get("n_routed_experts")
quant = quantization or (config.get("quantization_config") or {}).get("quant_method") or "none"
if quant != "none":
    raise ValueError("Packed quantized tensors do not establish the uncompressed model parameter count")
print(json.dumps({
    "model": {
        "model_id": model_id, "family": text["model_type"],
        "architecture": "moe" if experts else "dense",
        "parameter_count_b": parameters / 1e9, "quantization": quant,
        "tokenizer_id": model_id, "revision": revision or config.get("_commit_hash")
    },
    "hardware": {
        "accelerator": properties[0].name,
        "vram_per_gpu_gib": properties[0].total_memory / 1024**3,
        "gpu_count": len(properties)
    },
    "runtime": {
        "backend": "vllm", "backend_version": version("vllm"),
        "accelerator_runtime": device.__name__ + ":torch-" + torch.__version__
    }
}))
"""


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return float(value)


def _configuration(run: dict) -> tuple[DeployableConfiguration, CandidateConfiguration]:
    configuration = DeployableConfiguration.model_validate(run.get("deployment_configuration"))
    if configuration.checksum != configuration_checksum(configuration.content):
        raise ValueError("Deployment configuration checksum does not match the saved content")
    content = configuration.content
    decode = content.get("decode") or content.get("serving") or {}
    prefill = content.get("prefill") or {}
    candidate = CandidateConfiguration.model_validate(
        {
            "topology": configuration.provider_ref,
            "decode_tp": decode.get("tensorParallelSize"),
            "decode_replicas": decode.get("replicaCount"),
            "prefill_tp": prefill.get("tensorParallelSize"),
            "prefill_replicas": prefill.get("replicaCount"),
            "optimizations": tuple(filter(None, [content.get("guideVariant")])),
        },
        strict=True,
    )
    if (candidate.prefill_tp is None) != (candidate.prefill_replicas is None):
        raise ValueError("Prefill TP and replica count must both be present")
    return configuration, candidate


def _pod_identity(pod: dict, container: dict) -> dict:
    status = next(
        (item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == container["name"]),
        {},
    )
    if not status.get("ready") or not status.get("imageID"):
        raise ValueError("Model container is not ready or has no resolved image identity")
    return {
        "pod": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "container": container["name"],
        "image_id": status["imageID"],
        "restarts": status.get("restartCount"),
    }


async def _model_pods(run: dict) -> tuple[Any, list[tuple[dict, dict]]]:
    if not run.get("cluster_id") or not run.get("namespace"):
        raise ValueError("An explicit cluster and deployment namespace are required")
    runner = scoped_runner(run["cluster_id"])
    response = await runner.run(["kubectl", "get", "pods", "-n", run["namespace"], "-o", "json"], timeout=20)
    if response.returncode:
        raise ValueError("Unable to read model pods from the selected cluster")
    targets = []
    for pod in json.loads(response.stdout).get("items", []):
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        for container in pod.get("spec", {}).get("containers", []):
            resources = container.get("resources") or {}
            allocation = {**resources.get("requests", {}), **resources.get("limits", {})}
            if any(resolve_by_resource(key) for key in allocation) or resources.get("claims"):
                targets.append((pod, container))
    if not targets:
        raise ValueError("No accelerator-backed model containers were found")
    return runner, targets


def _argument(arguments: list[str], name: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith(name + "="):
            return argument.partition("=")[2]
        if argument == name and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


async def _capture_benchmark_context(run: dict) -> None:
    """Freeze actual model/runtime/device metadata before benchmark traffic."""
    try:
        if run.get("harness") != "inference-perf" or run.get("parallelism") != 1:
            raise ValueError("Automatic evidence attribution requires single-worker inference-perf")
        configuration, candidate = _configuration(run)
        if run.get("use_baseline_endpoint"):
            raise ValueError("Same-pod baseline traffic cannot be attributed to a single serving topology")
        runner, targets = await _model_pods(run)
        expected_model = configuration.content.get("model", {}).get("name")
        if not expected_model:
            raise ValueError("The deployment snapshot does not identify the underlying model")
        snapshots, identities, parallel_sizes = [], [], []
        for pod, container in targets:
            arguments = container.get("command", []) + container.get("args", [])
            if not any("vllm" in arg for arg in arguments):
                raise ValueError("Automatic runtime metadata currently requires an identifiable vLLM container")
            source = _argument(arguments, "--model")
            if source is None and "serve" in arguments:
                source = arguments[arguments.index("serve") + 1]
            if not source:
                raise ValueError("The serving container does not expose its actual model source")
            if (_argument(arguments, "--served-model-name") or source) != expected_model:
                raise ValueError("Serving model identity does not match the deployment snapshot")
            if _argument(arguments, "--tokenizer") not in (None, source, expected_model):
                raise ValueError("A separate tokenizer needs explicit provenance before evidence ingestion")
            if _argument(arguments, "--tokenizer-revision") not in (None, _argument(arguments, "--revision")):
                raise ValueError("A separate tokenizer revision needs explicit provenance before evidence ingestion")
            if _argument(arguments, "--pipeline-parallel-size") not in (None, "1"):
                raise ValueError("Pipeline-parallel topology is not representable by this benchmark schema")
            tp = _argument(arguments, "--tensor-parallel-size")
            if tp is None:
                raise ValueError("Actual serving tensor parallelism is not recorded in the container arguments")
            parallel_sizes.append(int(tp))
            response = await runner.run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    run["namespace"],
                    pod["metadata"]["name"],
                    "-c",
                    container["name"],
                    "--",
                    "python",
                    "-c",
                    _MODEL_PROBE,
                    source,
                    expected_model,
                    _argument(arguments, "--revision") or "",
                    _argument(arguments, "--quantization") or "",
                ],
                timeout=30,
            )
            if response.returncode:
                raise ValueError("Serving-container metadata probe failed; no inferred metadata was substituted")
            snapshots.append(json.loads(response.stdout))
            identities.append(_pod_identity(pod, container))
        first = snapshots[0]
        for snapshot in snapshots[1:]:
            if any(snapshot[key] != first[key] for key in ("model", "runtime")):
                raise ValueError("Serving replicas have inconsistent model or runtime metadata")
            if any(snapshot["hardware"][key] != first["hardware"][key] for key in ("accelerator", "vram_per_gpu_gib")):
                raise ValueError("Serving replicas have heterogeneous accelerators")
        hardware = dict(first["hardware"], gpu_count=sum(item["hardware"]["gpu_count"] for item in snapshots))
        expected_gpus = candidate.decode_tp * candidate.decode_replicas
        if candidate.prefill_tp is not None and candidate.prefill_replicas is not None:
            expected_gpus += candidate.prefill_tp * candidate.prefill_replicas
        if hardware["gpu_count"] != expected_gpus:
            raise ValueError("Visible model devices do not match the saved deployment allocation")
        expected_tps = [candidate.decode_tp] * candidate.decode_replicas
        if candidate.prefill_tp is not None and candidate.prefill_replicas is not None:
            expected_tps += [candidate.prefill_tp] * candidate.prefill_replicas
        if sorted(parallel_sizes) != sorted(expected_tps):
            raise ValueError("Actual serving tensor parallelism does not match the deployment configuration")
        digests = {identity["image_id"] for identity in identities}
        if len(digests) == 1:
            digest = re.search(r"sha256:[0-9a-f]{64}$", next(iter(digests)))
            if digest:
                first["runtime"]["container_digest"] = digest[0]
        run["benchmark_context"] = {
            "schema_version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "source": "serving-container metadata via Kubernetes API",
            "configuration_checksum": configuration.checksum,
            "model": first["model"],
            "hardware": hardware,
            "runtime": first["runtime"],
            "pods": identities,
        }
        run.pop("benchmark_context_error", None)
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError, IndexError) as error:
        run["benchmark_context_error"] = str(error)
        logger.warning("Evaluate %s metadata unavailable: %s", run["id"], error)


async def capture_benchmark_context(run: dict) -> None:
    """Bound metadata enrichment so unavailable APIs cannot stall benchmark startup."""
    try:
        await asyncio.wait_for(_capture_benchmark_context(run), timeout=60)
    except TimeoutError:
        run["benchmark_context_error"] = "Serving metadata collection exceeded 60 seconds"
        logger.warning("Evaluate %s: %s", run["id"], run["benchmark_context_error"])


async def verify_benchmark_context(run: dict) -> None:
    """Reject measurements if serving pods changed or restarted during the run."""
    context = run.get("benchmark_context")
    if not context:
        return
    try:
        _, targets = await _model_pods(run)
        identities = [_pod_identity(pod, container) for pod, container in targets]
        if sorted(identities, key=lambda item: (item["pod"], item["container"])) != sorted(
            context["pods"], key=lambda item: (item["pod"], item["container"])
        ):
            raise ValueError("Model containers changed during the benchmark")
        context["verified_at"] = datetime.now(UTC).isoformat()
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as error:
        context.pop("verified_at", None)
        run["benchmark_context_error"] = str(error)
        logger.warning("Evaluate %s metadata verification failed: %s", run["id"], error)


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
        context = run.get("benchmark_context") or {}
        if context.get("schema_version") != 1 or not context.get("verified_at") or run.get("benchmark_context_error"):
            raise ValueError(run.get("benchmark_context_error") or "Verified test-time metadata snapshot is missing")
        configuration, candidate = _configuration(run)
        if run.get("use_baseline_endpoint"):
            raise ValueError("Same-pod baseline topology is not attributable")
        if context.get("configuration_checksum") != configuration.checksum:
            raise ValueError("Metadata snapshot refers to a different deployment configuration")
        model = ModelDescriptor.model_validate(context.get("model"), strict=True)
        hardware = HardwareDescriptor.model_validate(context.get("hardware"), strict=True)
        runtime = RuntimeFingerprint.model_validate(context.get("runtime"), strict=True)
        _positive(model.parameter_count_b, "parameter count")
        _positive(hardware.vram_per_gpu_gib, "per-device memory")
        if model.active_parameter_count_b is not None:
            _positive(model.active_parameter_count_b, "active parameter count")
        for value in (
            model.model_id,
            model.family,
            model.quantization,
            model.tokenizer_id,
            hardware.accelerator,
            runtime.backend,
            runtime.backend_version,
            runtime.accelerator_runtime,
        ):
            if not value.strip() or value.lower() in ("unknown", "cluster"):
                raise ValueError("Model, hardware and runtime identity fields must be known")
        if model.model_id != configuration.content.get("model", {}).get("name"):
            raise ValueError("Model metadata does not match the deployment snapshot")
        expected_gpus = candidate.decode_tp * candidate.decode_replicas
        if candidate.prefill_tp is not None and candidate.prefill_replicas is not None:
            expected_gpus += candidate.prefill_tp * candidate.prefill_replicas
        if hardware.gpu_count != expected_gpus:
            raise ValueError("Hardware allocation and deployment topology disagree")
        timestamp = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
        started = datetime.fromisoformat(run["benchmark_started_at"].replace("Z", "+00:00"))
        captured = datetime.fromisoformat(context["captured_at"].replace("Z", "+00:00"))
        verified = datetime.fromisoformat(context["verified_at"].replace("Z", "+00:00"))
        if any(t.tzinfo is None for t in (timestamp, started, captured, verified)) or not (
            captured <= started <= timestamp <= verified
        ):
            raise ValueError("Metadata and measurement timestamps do not establish a valid test window")
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
    dao = BenchmarkRecordDao()
    try:
        existing = [record.benchmark_id for record in records if dao.get(record.benchmark_id) is not None]
        pending = [record for record in records if record.benchmark_id not in existing]
        inserted = []
        if apply and pending:
            inserted = dao.save_many(pending)
            existing += [record.benchmark_id for record in pending if record.benchmark_id not in inserted]
    except BenchmarkRecordDaoError:
        logger.exception("Evaluate %s evidence persistence failed", run.get("id"))
        return {
            "schema_version": 1,
            "status": "error",
            "record_ids": [],
            "issues": [{"sample": "run", "reason": "Unable to persist benchmark evidence; retry backfill"}],
        }
    status = "stored" if apply or not pending else "eligible"
    report = {
        "schema_version": 1,
        "status": status if records and not issues else "partial" if records else "skipped",
        "record_ids": [record.benchmark_id for record in records],
        "existing_ids": existing,
        "inserted_ids" if apply else "eligible_ids": inserted if apply else [record.benchmark_id for record in pending],
        "issues": issues,
    }
    if issues:
        logger.warning("Evaluate %s benchmark evidence: %s", run.get("id"), issues)
    return report


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
