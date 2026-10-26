"""Read-only, test-time serving facts shared by benchmark producers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from llm_d_lens.agentic.benchmark_evidence import (
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
)
from llm_d_lens.db.dao.agentic_benchmark_record import BenchmarkRecordDao, BenchmarkRecordDaoError
from llm_d_lens.deploy.contracts import DeployableConfiguration
from llm_d_lens.hardware.resolver import resolve_by_resource
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum
from llm_d_lens.infrastructure.kubernetes import scoped_runner

logger = logging.getLogger(__name__)

# Read headers and device properties, never load weights or download model code.
_MODEL_PROBE = r"""
import json, math, re, sys
from importlib.metadata import version
from pathlib import Path

source, model_id, revision, quantization = sys.argv[1:]
model = {"model_id": model_id, "revision": revision or None}
runtime, hardware, warnings = {}, {}, []
try:
    if not source:
        raise ValueError("Serving model source is unavailable")
    root = Path(source)
    if not root.is_dir():
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Unpinned serving revision")
        from huggingface_hub import snapshot_download
        root = Path(snapshot_download(source, revision=revision, local_files_only=True))
    config = json.loads((root / "config.json").read_text())
    text = config.get("text_config") or config
    experts = text.get("num_local_experts") or text.get("num_experts") or text.get("n_routed_experts")
    quant = quantization or (config.get("quantization_config") or {}).get("quant_method") or "none"
    model.update(family=text.get("model_type"), architecture="moe" if experts else "dense",
                 quantization=quant, tokenizer_id=model_id)
    if quant == "none":
        from safetensors import safe_open
        parameters, seen = 0, set()
        files = sorted(root.glob("*.safetensors"))
        if not files:
            raise ValueError("No local parameter metadata")
        for file in files:
            with safe_open(file, framework="pt", device="cpu") as weights:
                for key in weights.keys():
                    if key in seen:
                        raise ValueError("Duplicate tensor metadata")
                    seen.add(key)
                    parameters += math.prod(weights.get_slice(key).get_shape())
        model["parameter_count_b"] = parameters / 1e9
except (ImportError, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
    warnings.append("model:" + type(error).__name__)
if quantization:
    model["quantization"] = quantization
try:
    runtime.update(backend="vllm", backend_version=version("vllm"))
except ImportError as error:
    warnings.append("vllm:" + type(error).__name__)
try:
    import torch
    device = torch.get_device_module()
    runtime["accelerator_runtime"] = device.__name__ + ":torch-" + torch.__version__
    properties = [device.get_device_properties(i) for i in range(device.device_count())]
    if not properties or len({(p.name, p.total_memory) for p in properties}) != 1:
        raise ValueError("Heterogeneous or missing accelerator properties")
    hardware.update(accelerator=properties[0].name,
                    vram_per_gpu_gib=properties[0].total_memory / 1024**3,
                    gpu_count=len(properties))
except (ImportError, OSError, RuntimeError, ValueError, AttributeError) as error:
    warnings.append("hardware:" + type(error).__name__)
print(json.dumps({"model": model, "hardware": hardware, "runtime": runtime, "warnings": warnings}))
"""


async def enrich_model_metadata(run: dict) -> None:
    """Fetch public descriptive model facts, never serving settings or benchmark metrics.

    API revision and lookup time are retained separately from the actual serving
    revision. A lookup failure is reported but cannot prevent result archival.
    """
    saved = run.get("deployment_configuration")
    content = saved.get("content") if isinstance(saved, dict) else None
    model_spec = content.get("model") if isinstance(content, dict) else None
    model_id = model_spec.get("name") if isinstance(model_spec, dict) else None
    if not isinstance(model_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?", model_id
    ):
        run["benchmark_model_metadata_error"] = "Public metadata requires a saved Hub model identity"
        logger.warning("Benchmark %s: %s", run["id"], run["benchmark_model_metadata_error"])
        return
    context = run.get("benchmark_context")
    serving_model = context.get("model") if isinstance(context, dict) else None
    serving_model = serving_model if isinstance(serving_model, dict) else {}
    if all(serving_model.get(key) is not None for key in ("family", "architecture", "parameter_count_b")):
        return
    revision = serving_model.get("revision")
    suffix = f"/revision/{revision}" if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) else ""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(f"https://huggingface.co/api/models/{model_id}{suffix}")
            response.raise_for_status()
            info = response.json()
            sha = info.get("sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Model API did not return an immutable revision")
            response = await client.get(f"https://huggingface.co/{model_id}/resolve/{sha}/config.json")
            response.raise_for_status()
            config = response.json()
        text = config.get("text_config") or config
        experts = text.get("num_local_experts") or text.get("num_experts") or text.get("n_routed_experts")
        fields = {
            "family": text.get("model_type"),
            "architecture": "moe" if experts else "dense",
            "parameter_count_b": (info.get("safetensors") or {}).get("total"),
        }
        if fields["parameter_count_b"] is not None:
            fields["parameter_count_b"] = positive(fields["parameter_count_b"], "API parameter count") / 1e9
        descriptor = ModelDescriptor(model_id=model_id, **fields)
        run["benchmark_model_metadata"] = {
            "model": descriptor.model_dump(exclude_none=True),
            "source": "Hugging Face model API and config.json",
            "api_revision": sha,
            "serving_revision_pinned": revision == sha,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
        run.pop("benchmark_model_metadata_error", None)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as error:
        reason = f"Public model metadata lookup failed ({type(error).__name__})"
        run["benchmark_model_metadata_error"] = reason
        logger.warning("Benchmark %s: %s", run["id"], reason)


def _model_descriptor(run: dict, values: dict) -> ModelDescriptor:
    """Combine serving facts with descriptive public metadata without overwriting facts."""
    extra = run.get("benchmark_model_metadata") or {}
    public = extra.get("model") or {}
    merged = dict(values)
    if public.get("model_id") == values.get("model_id"):
        for name in ("family", "architecture", "parameter_count_b"):
            if merged.get(name) is None and public.get(name) is not None:
                merged[name] = public[name]
    return ModelDescriptor.model_validate(_available_descriptor_fields(merged), strict=True)


def _available_descriptor_fields(values: dict) -> dict:
    """Normalize explicit missing-value markers from collected metadata to null."""
    return {
        name: None
        if name != "model_id"
        and isinstance(value, str)
        and value.strip().lower() in ("", "unknown", "unavailable", "cluster")
        else value
        for name, value in values.items()
    }


def positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return float(value)


def configuration_snapshot(run: dict) -> tuple[DeployableConfiguration, CandidateConfiguration]:
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
        "node": pod.get("spec", {}).get("nodeName"),
        "image_id": status["imageID"],
        "restarts": status.get("restartCount"),
    }


async def _model_pods(run: dict) -> tuple[Any, list[tuple[dict, dict]]]:
    if not run.get("cluster_id") or not run.get("namespace"):
        raise ValueError("An explicit cluster and deployment namespace are required")
    runner = scoped_runner(run["cluster_id"])
    argv = ["kubectl", "get", "pods", "-n", run["namespace"], "-o", "json"]
    if run.get("pod_selector"):
        argv += ["-l", run["pod_selector"]]
    response = await runner.run(argv, timeout=20)
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


async def _capture(run: dict) -> None:
    configuration, candidate = configuration_snapshot(run)
    if run.get("use_baseline_endpoint"):
        raise ValueError("Same-pod baseline traffic cannot be attributed to a single serving topology")
    runner, targets = await _model_pods(run)
    expected_model = configuration.content.get("model", {}).get("name")
    if not expected_model:
        raise ValueError("The deployment snapshot does not identify the underlying model")
    routing = run.get("routing")
    model_identity = None
    if run.get("source") == "simulation":
        if not run.get("deployment_execution_id"):
            raise ValueError("Simulation model identity requires an attributable deployment execution")
        model_identity = {
            "source": "deployment-configuration",
            "model_id": expected_model,
            "execution_id": run["deployment_execution_id"],
        }
    if routing:
        if (
            run.get("source") != "simulation"
            or routing.get("model_ref") != expected_model
            or not routing.get("group_id")
            or routing["group_id"] != run.get("model_service_group_id")
            or routing.get("execution_id") != run.get("deployment_execution_id")
        ):
            raise ValueError("Model Service identity does not match the deployment snapshot")
        model_identity = {
            "source": "model-service-routing",
            "model_id": expected_model,
            **{key: routing[key] for key in ("group_id", "member_id", "execution_id")},
        }
    snapshots, identities, parallel_sizes, serving = [], [], [], []
    for pod, container in targets:
        source, tp = None, None
        arguments = []
        if not model_identity:
            arguments = container.get("command", []) + container.get("args", [])
            if not any("vllm" in arg for arg in arguments):
                raise ValueError("Automatic runtime metadata currently requires an identifiable vLLM container")
            source = _argument(arguments, "--model")
            if source is None and "serve" in arguments:
                source = arguments[arguments.index("serve") + 1]
            if not source:
                raise ValueError("The serving container does not expose its actual model source")
            if expected_model not in (source, _argument(arguments, "--served-model-name")):
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
        observed_arguments = {
            name: _argument(arguments, name) if not model_identity else None
            for name in (
                "--revision",
                "--quantization",
                "--tensor-parallel-size",
                "--max-model-len",
                "--gpu-memory-utilization",
                "--max-num-seqs",
                "--max-num-batched-tokens",
            )
        }
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
                "python3",
                "-c",
                _MODEL_PROBE,
                source or "",
                expected_model,
                observed_arguments["--revision"] or "",
                observed_arguments["--quantization"] or "",
            ],
            timeout=30,
        )
        if response.returncode:
            logger.warning("Benchmark %s serving metadata probe exited %s", run["id"], response.returncode)
            snapshot = {
                "model": {"model_id": expected_model, "revision": observed_arguments["--revision"]},
                "hardware": {},
                "runtime": {} if model_identity else {"backend": "vllm"},
                "warnings": [f"metadata probe exit {response.returncode}"],
            }
        else:
            snapshot = json.loads(response.stdout)
        if (
            tp is not None
            and snapshot["hardware"].get("gpu_count") is not None
            and snapshot["hardware"]["gpu_count"] != int(tp)
        ):
            raise ValueError("Visible devices in a serving container do not match its tensor parallelism")
        snapshots.append(snapshot)
        identities.append(_pod_identity(pod, container))
        serving.append(
            {
                "pod": pod["metadata"]["name"],
                "container": container["name"],
                "model_source": source,
                "arguments": observed_arguments,
                "enable_prefix_caching": True
                if "--enable-prefix-caching" in arguments
                else (False if "--no-enable-prefix-caching" in arguments else None),
            }
        )
    first = {}
    for section in ("model", "runtime", "hardware"):
        first[section] = {}
        for key in set().union(*(snapshot[section] for snapshot in snapshots)):
            values = [snapshot[section].get(key) for snapshot in snapshots]
            first[section][key] = values[0] if all(value == values[0] for value in values) else None
    counts = [item["hardware"].get("gpu_count") for item in snapshots]
    hardware = dict(first["hardware"], gpu_count=sum(counts) if all(count is not None for count in counts) else None)
    expected_tps = [candidate.decode_tp] * candidate.decode_replicas
    if candidate.prefill_tp is not None and candidate.prefill_replicas is not None:
        expected_tps += [candidate.prefill_tp] * candidate.prefill_replicas
    if hardware["gpu_count"] is not None and hardware["gpu_count"] != sum(expected_tps):
        raise ValueError("Visible model devices do not match the saved deployment allocation")
    if model_identity:
        if len(targets) != len(expected_tps) or (
            all(count is not None for count in counts) and sorted(counts) != sorted(expected_tps)
        ):
            raise ValueError("Serving containers and visible devices do not match the saved deployment topology")
    elif sorted(parallel_sizes) != sorted(expected_tps):
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
        "serving_configuration": serving,
        "replica_metadata": snapshots,
        "warnings": [warning for snapshot in snapshots for warning in snapshot.get("warnings", [])],
    }
    if model_identity:
        run["benchmark_context"]["model_identity"] = model_identity
        run["benchmark_context"]["warnings"].append(
            "Model identity comes from validated deployment attribution; "
            "serving model source and command options are unknown"
        )
    run.pop("benchmark_context_error", None)


async def capture_benchmark_context(run: dict) -> None:
    try:
        await asyncio.wait_for(_capture(run), timeout=60)
    except (OSError, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        run.pop("benchmark_context", None)
        run["benchmark_context_error"] = str(error) or "Serving metadata collection exceeded 60 seconds"
        logger.warning("Benchmark %s metadata unavailable: %s", run["id"], run["benchmark_context_error"])
    await enrich_model_metadata(run)


async def verify_benchmark_context(run: dict) -> None:
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
    except (OSError, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as error:
        context.pop("verified_at", None)
        run["benchmark_context_error"] = str(error)
        logger.warning("Benchmark %s metadata verification failed: %s", run["id"], error)


def validate_benchmark_context(
    run: dict,
    *,
    allow_saved_configuration: bool = False,
) -> tuple[datetime, ModelDescriptor, HardwareDescriptor, RuntimeFingerprint, CandidateConfiguration]:
    """Validate attribution; optionally archive saved Evaluate configuration without claiming a live snapshot.

    Without a serving snapshot, hardware remains unknown and is ineligible for
    predictive matching. Public model descriptors do not establish serving identity.
    """
    context = run.get("benchmark_context") or {}
    configuration, candidate = configuration_snapshot(run)
    if run.get("use_baseline_endpoint"):
        raise ValueError("Same-pod baseline topology is not attributable")
    if not context and allow_saved_configuration:
        timestamp = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
        started = datetime.fromisoformat(run["benchmark_started_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or started.tzinfo is None or started > timestamp:
            raise ValueError("Measurement timestamps do not establish a valid test window")
        model = _model_descriptor(run, {"model_id": configuration.content.get("model", {}).get("name")})
        if not model.model_id.strip():
            raise ValueError("The saved deployment configuration must identify the model")
        backend = (run.get("configuration") or {}).get("runtime")
        return timestamp, model, HardwareDescriptor(), RuntimeFingerprint(backend=backend), candidate
    if context.get("schema_version") != 1 or not context.get("verified_at") or run.get("benchmark_context_error"):
        raise ValueError(run.get("benchmark_context_error") or "Verified test-time metadata snapshot is missing")
    if context.get("configuration_checksum") != configuration.checksum:
        raise ValueError("Metadata snapshot refers to a different deployment configuration")
    model = _model_descriptor(run, context.get("model") or {})
    hardware = HardwareDescriptor.model_validate(
        _available_descriptor_fields(context.get("hardware") or {}), strict=True
    )
    runtime = RuntimeFingerprint.model_validate(_available_descriptor_fields(context.get("runtime") or {}), strict=True)
    if model.parameter_count_b is not None:
        positive(model.parameter_count_b, "parameter count")
    if hardware.vram_per_gpu_gib is not None:
        positive(hardware.vram_per_gpu_gib, "per-device memory")
    if model.active_parameter_count_b is not None:
        positive(model.active_parameter_count_b, "active parameter count")
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
        if value is not None and (not value.strip() or value.lower() in ("unknown", "cluster")):
            raise ValueError("Model, hardware and runtime identity fields must be known")
    if model.model_id != configuration.content.get("model", {}).get("name"):
        raise ValueError("Model metadata does not match the deployment snapshot")
    expected_gpus = candidate.decode_tp * candidate.decode_replicas
    if candidate.prefill_tp is not None and candidate.prefill_replicas is not None:
        expected_gpus += candidate.prefill_tp * candidate.prefill_replicas
    if hardware.gpu_count is not None and hardware.gpu_count != expected_gpus:
        raise ValueError("Hardware allocation and deployment topology disagree")
    timestamp = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(run["benchmark_started_at"].replace("Z", "+00:00"))
    captured = datetime.fromisoformat(context["captured_at"].replace("Z", "+00:00"))
    verified = datetime.fromisoformat(context["verified_at"].replace("Z", "+00:00"))
    if any(t.tzinfo is None for t in (timestamp, started, captured, verified)) or not (
        captured <= started <= timestamp <= verified
    ):
        raise ValueError("Metadata and measurement timestamps do not establish a valid test window")
    return timestamp, model, hardware, runtime, candidate


def persist_benchmark_records(
    records: list[BenchmarkRecord], issues: list[dict], *, source_id: str, apply: bool
) -> dict:
    dao = BenchmarkRecordDao()
    try:
        existing = [record.benchmark_id for record in records if dao.get(record.benchmark_id) is not None]
        pending = [record for record in records if record.benchmark_id not in existing]
        inserted = dao.save_many(pending) if apply and pending else []
        if apply:
            existing += [record.benchmark_id for record in pending if record.benchmark_id not in inserted]
    except BenchmarkRecordDaoError:
        logger.exception("Benchmark %s evidence persistence failed", source_id)
        return {
            "schema_version": 1,
            "status": "error",
            "record_ids": [],
            "issues": [{"sample": "run", "reason": "Unable to persist benchmark evidence; retry backfill"}],
        }
    status = "stored" if apply or not pending else "eligible"
    if issues:
        logger.warning("Benchmark %s evidence: %s", source_id, issues)
    return {
        "schema_version": 1,
        "status": status if records and not issues else "partial" if records else "skipped",
        "record_ids": [record.benchmark_id for record in records],
        "existing_ids": existing,
        "inserted_ids" if apply else "eligible_ids": inserted if apply else [record.benchmark_id for record in pending],
        "issues": issues,
    }
