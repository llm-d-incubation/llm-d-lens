"""AIPerf simulation backend."""

from __future__ import annotations

import asyncio
import math
import os
import sys
from collections.abc import Mapping
from functools import singledispatchmethod
from pathlib import Path
from typing import Any, ClassVar

from llm_d_lens.infrastructure.hostos import current_host
from llm_d_lens.infrastructure.process import run_sync, which
from llm_d_lens.infrastructure.storage.paths import storage_path

from ..errors import SimulationConfigurationError, SimulationError, SimulationResultParseError
from ..models import (
    ArtifactRecord,
    BackendCommand,
    BackendDescriptor,
    SimulationResult,
    SimulationTask,
)
from ..process import CommandResult, RunContext
from ..telemetry_post import redact_manifest_payload
from ..traces import BasetenTrace, BaseTrace, WekaPublicDatasetTrace, trace_registry
from .analytics import artifact_error_message
from .analytics import (
    completion_timeline as _completion_timeline,
)
from .analytics import (
    latency_timeline as _latency_timeline,
)
from .analytics import (
    number as _number,
)
from .analytics import (
    rate_timelines as _rate_timelines,
)
from .analytics import (
    read_object as _read_object,
)
from .analytics import (
    visit_json_lines as _visit_json_lines,
)
from .base import CommandBackend
from .base import artifact as _artifact
from .registry import register_backend


def _average(raw: dict, name: str) -> float:
    value = raw.get(name)
    if isinstance(value, dict):
        value = value.get("avg", value.get("mean"))
    return _number(value)


def _metric(raw: dict, name: str) -> dict | None:
    value = raw.get(name)
    if not isinstance(value, dict):
        return None
    fields = {
        "mean_ms": value.get("avg", value.get("mean")),
        "p50_ms": value.get("p50"),
        "p90_ms": value.get("p90"),
        "p95_ms": value.get("p95"),
        "p99_ms": value.get("p99"),
        "min_ms": value.get("min"),
        "max_ms": value.get("max"),
        "std_ms": value.get("std"),
        "count": value.get("count"),
        "sum_ms": value.get("sum"),
    }
    metric = {
        key: parsed for key, raw_value in fields.items() if math.isfinite(parsed := _number(raw_value, float("nan")))
    }
    if not metric:
        return None
    if value.get("unit"):
        metric["unit"] = value["unit"]
    return metric


def _aiperf_profiling_record(record: dict) -> bool:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return True
    return metadata.get("benchmark_phase") in (None, "profiling") and metadata.get("phase_kind") in (
        None,
        "profiling",
    )


def _find_artifact_file(root: Path, filename: str) -> Path | None:
    """Locate an artifact file under ``root``, preferring direct files and skipping ``checkpoints/``."""
    direct = root / filename
    if direct.is_file():
        return direct
    if not root.is_dir():
        return None
    candidates = [
        path for path in root.rglob(filename) if path.is_file() and "checkpoints" not in path.relative_to(root).parts
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (len(path.relative_to(root).parts), str(path)))
    return candidates[0]


def _response_status_code(record: dict) -> int | None:
    for container in (record, record.get("metadata"), record.get("trace_data"), record.get("error")):
        if not isinstance(container, dict):
            continue
        for key in ("status", "status_code", "response_status_code", "code"):
            value = _number(container.get(key), float("nan"))
            if math.isfinite(value) and value.is_integer() and 100 <= value <= 599:
                return int(value)
    return None


def _error_message(record: dict) -> str | None:
    return artifact_error_message(record, record.get("error"))


DEFAULT_RANDOM_SEED = 666
INFERENCEX_AGENTX_MVP_SCENARIO = "inferencex-agentx-mvp"
INFERENCEX_AGENTX_MVP_MIN_DURATION_SECONDS = 900
INFERENCEX_AGENTX_MVP_WEKA_DATASETS: frozenset[str] = frozenset(
    {
        "semianalysis_cc_traces_weka_with_subagents",
        "semianalysis_cc_traces_weka_with_subagents_256k",
        "semianalysis_cc_traces_weka_with_subagents_060226",
        "semianalysis_cc_traces_weka_with_subagents_060226_256k",
        "semianalysis_cc_traces_weka_with_subagents_060526",
        "semianalysis_cc_traces_weka_with_subagents_060526_256k",
        "semianalysis_cc_traces_weka_with_subagents_060826",
        "semianalysis_cc_traces_weka_with_subagents_060826_256k",
        "semianalysis_cc_traces_weka_061326",
        "semianalysis_cc_traces_weka_061326_256k",
        "semianalysis_cc_traces_weka_061526",
        "semianalysis_cc_traces_weka_061526_256k",
        "semianalysis_cc_traces_weka_062126",
        "semianalysis_cc_traces_weka_062126_256k",
    }
)


@register_backend
class AIPerfBackend(CommandBackend):
    name = "aiperf"
    display_name = "AIPerf"
    VERSION = "0.14.0"
    DEFAULT_RANDOM_SEED: ClassVar[int] = DEFAULT_RANDOM_SEED
    INFERENCEX_AGENTX_MVP_SCENARIO: ClassVar[str] = INFERENCEX_AGENTX_MVP_SCENARIO
    INFERENCEX_AGENTX_MVP_MIN_DURATION_SECONDS: ClassVar[int] = INFERENCEX_AGENTX_MVP_MIN_DURATION_SECONDS
    INFERENCEX_AGENTX_MVP_WEKA_DATASETS: ClassVar[frozenset[str]] = INFERENCEX_AGENTX_MVP_WEKA_DATASETS
    # aiperf is a pure Python PyPI package. In-cluster runs use the official
    # nvcr.io/nvidia/ai-dynamo/aiperf image (see
    # llm_d_lens.simulation.incluster.incluster_image), which already has it
    # pre-installed; ``pip install`` only kicks in as a fallback if that
    # image is overridden to a bare Python one.
    pip_installable_in_pod = "aiperf"
    _install_lock = asyncio.Lock()
    trace_formats: ClassVar[list[str]] = [
        "mooncake_trace",
        "bailian_trace",
        "baseten_trace",
        "burst_gpt_trace",
        "weka_public_dataset",
    ]
    capabilities: ClassVar[dict] = {
        "prompt_kinds": ["trace"],
        "arrival_patterns": [],
        "supports_duration": True,
        "supports_request_count": False,
        "supports_request_rate": False,
        "supports_streaming": True,
        "supports_per_request_results": True,
        "supports_cancellation": True,
        "supports_trace_range": True,
        "supports_concurrency": True,
        "scale_factor_trace_formats": ["baseten_trace"],
        "inferencex_agentx_mvp_weka_datasets": sorted(INFERENCEX_AGENTX_MVP_WEKA_DATASETS),
        "trace_format_capabilities": {
            "mooncake_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
                "supports_fixed_schedule": True,
                "supports_concurrency": True,
            },
            "bailian_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
                "supports_fixed_schedule": True,
                "supports_concurrency": True,
            },
            "baseten_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": True,
                "supports_fixed_schedule": True,
                "supports_concurrency": True,
            },
            "burst_gpt_trace": {
                "supports_trace_range": True,
                "supports_scale_factor": False,
                "supports_fixed_schedule": True,
                "supports_concurrency": True,
            },
            "weka_public_dataset": {
                "supports_trace_range": False,
                "supports_scale_factor": False,
                "supports_fixed_schedule": False,
                "supports_concurrency": True,
            },
        },
        "advanced_options": [
            {
                "name": "random_seed",
                "label": "Random seed",
                "minimum": 0,
                "default": DEFAULT_RANDOM_SEED,
            },
            {
                "name": "num_profile_runs",
                "label": "Profile runs",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
            {
                "name": "profile_run_cooldown_seconds",
                "label": "Run cooldown (seconds)",
                "minimum": 0,
            },
            {
                "name": "concurrency",
                "label": "Session concurrency",
                "minimum": 1,
                "default": 1,
            },
            {
                "name": "trajectory_start_min_ratio",
                "label": "Trajectory start min ratio",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.25,
            },
            {
                "name": "trajectory_start_max_ratio",
                "label": "Trajectory start max ratio",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.75,
            },
            {
                "name": "max_context_length",
                "label": "Maximum context length",
                "minimum": 1,
                "trace_formats": ["weka_public_dataset"],
            },
            {
                "name": "trace_idle_gap_cap_seconds",
                "label": "Trace idle-gap cap (seconds)",
                "minimum": 0,
                "trace_formats": ["weka_public_dataset"],
            },
        ],
    }
    allowed_options: ClassVar[set[str]] = {
        "ui_type",
        "random_seed",
        "num_profile_runs",
        "profile_run_cooldown_seconds",
        "concurrency",
        "trajectory_start_min_ratio",
        "trajectory_start_max_ratio",
        "max_context_length",
        "trace_idle_gap_cap_seconds",
        "error_rate_slo",
        "ttft_slo",
        "tpot_slo",
        "e2e_slo",
        "warmup_seconds",
        "collect_server_telemetry",
    }

    @property
    def executable(self) -> str:
        configured = os.environ.get("AIPERF_EXECUTABLE")
        if configured:
            return str(Path(configured).expanduser())
        return which("aiperf") or str(self.managed_executable_path())

    @staticmethod
    def managed_executable_path() -> Path:
        # Do not resolve symlinks: in a venv ``sys.executable`` is often a
        # symlink chain to the base interpreter (e.g. a venv's python ->
        # /usr/bin/python3). pip installs console scripts into the venv's
        # executable directory, so resolving would point at the wrong one.
        # ``Path(sys.executable).parent.parent`` is the current virtualenv root.
        return current_host().venv_executable(Path(sys.executable).parent.parent, "aiperf")

    def descriptor(self) -> BackendDescriptor:
        executable = self.executable
        available = bool(Path(executable).is_file() or which(executable))
        configured = bool(os.environ.get("AIPERF_EXECUTABLE"))
        return BackendDescriptor(
            name=self.name,
            display_name=self.display_name,
            api_version=1,
            available=available or not configured,
            version=self.backend_version(None),
            unavailable_reason=(
                f"Configured AIPerf executable '{executable}' was not found" if configured and not available else None
            ),
            managed_installation=not available and not configured,
            scenarios=self.scenarios,
            capabilities={**self.capabilities, "trace_formats": self.trace_formats},
        )

    def backend_version(self, detected_version: str | None) -> str:
        return detected_version or self.VERSION

    @classmethod
    def _install(cls, destination: Path) -> None:
        cache = storage_path("cache", "backends")
        lock_root = cache / cls.name
        lock_root.mkdir(parents=True, exist_ok=True)
        with lock_root.joinpath(".install.lock").open("w", encoding="utf-8") as lock:
            current_host().try_lock(lock)
            if destination.is_file():
                return
            try:
                result = run_sync(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        f"aiperf=={cls.VERSION}",
                    ],
                    timeout=1800,
                )
            except TimeoutError as error:
                raise SimulationConfigurationError("AIPerf installation timed out after 30 minutes") from error
            if result.returncode != 0 or not destination.is_file():
                detail = (result.stderr or result.stdout or "pip install failed").strip()
                raise SimulationConfigurationError(f"Unable to install AIPerf: {detail[-2000:]}")

    async def prepare_executable(self, context: RunContext) -> str:
        executable = self.executable
        if Path(executable).is_file() or which(executable):
            return executable
        if os.environ.get("AIPERF_EXECUTABLE"):
            raise SimulationConfigurationError(f"Configured AIPerf executable '{executable}' was not found")
        async with self._install_lock:
            destination = self.managed_executable_path()
            if not destination.is_file():
                context.log(
                    f"AIPerf was not found; installing version {self.VERSION} "
                    f"into the active Python environment at {Path(sys.executable).parent}"
                )
                await asyncio.to_thread(self._install, destination)
                context.log(f"AIPerf installation completed: {destination}")
            return str(destination)

    def normalize_backend_options(self, options: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(options)
        if normalized.get("random_seed") is None:
            normalized["random_seed"] = self.DEFAULT_RANDOM_SEED
        for option in ("trajectory_start_min_ratio", "trajectory_start_max_ratio"):
            if option not in normalized:
                continue
            value = normalized[option]
            if value is None or (isinstance(value, str) and not value.strip()):
                normalized.pop(option)
        return normalized

    async def validate_backend(self, task: SimulationTask) -> None:
        if not task.simulation.stream:
            raise SimulationConfigurationError("AIPerf requires streaming responses (stream=True)")
        trace = task.prompt.trace
        runtime_trace = trace_registry.resolve(trace.path, trace.format)
        options = task.simulation.backend_options
        unknown = sorted(set(options) - self.allowed_options)
        if unknown:
            raise SimulationConfigurationError(f"Unsupported backend options: {', '.join(unknown)}")
        integer_ranges = {
            "random_seed": (0, None),
            "num_profile_runs": (1, 10),
            "concurrency": (1, None),
            "max_context_length": (1, None),
        }
        for option, (minimum, maximum) in integer_ranges.items():
            if option not in options:
                continue
            value = options[option]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
                or (maximum is not None and value > maximum)
            ):
                maximum_text = f" and at most {maximum}" if maximum is not None else ""
                raise SimulationConfigurationError(
                    f"AIPerf {option} must be an integer of at least {minimum}{maximum_text}"
                )
        for option in ("profile_run_cooldown_seconds", "trace_idle_gap_cap_seconds"):
            if option in options and (
                not isinstance(options[option], (int, float))
                or isinstance(options[option], bool)
                or not math.isfinite(float(options[option]))
                or float(options[option]) < 0
            ):
                raise SimulationConfigurationError(f"AIPerf {option} must be a non-negative number")
        for option in ("trajectory_start_min_ratio", "trajectory_start_max_ratio"):
            if option not in options:
                continue
            value = options[option]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise SimulationConfigurationError(f"AIPerf {option} must be a finite number between 0.0 and 1.0")
        if (
            "trajectory_start_min_ratio" in options
            and "trajectory_start_max_ratio" in options
            and float(options["trajectory_start_min_ratio"]) > float(options["trajectory_start_max_ratio"])
        ):
            raise SimulationConfigurationError(
                "AIPerf trajectory_start_min_ratio must be less than or equal to trajectory_start_max_ratio"
            )
        if (
            "trajectory_start_min_ratio" in options or "trajectory_start_max_ratio" in options
        ) and not self._supports_inferencex_agentx_mvp(runtime_trace, trace, options):
            raise SimulationConfigurationError(
                "AIPerf trajectory_start_min_ratio and trajectory_start_max_ratio are only supported "
                "for inferencex-agentx-mvp compatible workloads"
            )
        if float(options.get("profile_run_cooldown_seconds", 0)) > 0 and int(options.get("num_profile_runs", 1)) <= 1:
            raise SimulationConfigurationError(
                "AIPerf profile_run_cooldown_seconds requires num_profile_runs greater than 1"
            )
        if task.simulation.duration_seconds < 1:
            raise SimulationConfigurationError("AIPerf Simulation requires duration_seconds of at least 1")
        if trace.format not in self.trace_formats:
            raise SimulationConfigurationError(f"Unsupported AIPerf trace format '{trace.format}'")
        if trace.timeout_seconds <= 0:
            raise SimulationConfigurationError("AIPerf trace_timeout_seconds must be greater than zero")
        if trace.synthesis_speedup_ratio <= 0:
            raise SimulationConfigurationError("AIPerf synthesis_speedup_ratio must be greater than zero")
        self._validate_trace(runtime_trace, trace, options)

    @staticmethod
    def _uses_concurrency_mode(trace, options: Mapping[str, Any]) -> bool:
        """Return whether trace replay runs in concurrency-slot mode instead of fixed schedule."""
        return not trace.fixed_schedule or options.get("concurrency") is not None

    @classmethod
    def _supports_inferencex_agentx_mvp(
        cls,
        runtime_trace: BaseTrace,
        trace: Any,
        options: Mapping[str, Any],
    ) -> bool:
        """Return whether the workload supports AIPerf's ``inferencex-agentx-mvp`` scenario."""
        if isinstance(runtime_trace, WekaPublicDatasetTrace):
            return runtime_trace.dataset in cls.INFERENCEX_AGENTX_MVP_WEKA_DATASETS
        return runtime_trace.format == "mooncake_trace" and cls._uses_concurrency_mode(trace, options)

    @staticmethod
    def _reject_weka_only_options(trace, options: dict) -> None:
        unsupported = {"max_context_length", "trace_idle_gap_cap_seconds"}.intersection(options)
        if unsupported:
            raise SimulationConfigurationError(
                f"AIPerf options are not supported by {trace.format}: {', '.join(sorted(unsupported))}"
            )

    @singledispatchmethod
    def _validate_trace(self, runtime_trace: BaseTrace, trace, options: dict) -> None:
        raise SimulationConfigurationError(f"AIPerf does not support {runtime_trace.format}")

    @_validate_trace.register
    def _(self, runtime_trace: BaseTrace, trace, options: dict) -> None:
        self._reject_weka_only_options(trace, options)
        if not self._uses_concurrency_mode(trace, options) and trace.synthesis_speedup_ratio != 1:
            raise SimulationConfigurationError(
                f"AIPerf {trace.format} does not support synthesis_speedup_ratio values other than 1.0"
            )

    @_validate_trace.register
    def _(self, runtime_trace: BasetenTrace, trace, options: dict) -> None:
        self._reject_weka_only_options(trace, options)

    @_validate_trace.register
    def _(self, runtime_trace: WekaPublicDatasetTrace, trace, options: dict) -> None:
        if trace.synthesis_speedup_ratio != 1:
            raise SimulationConfigurationError(
                f"AIPerf {trace.format} does not support synthesis_speedup_ratio values other than 1.0"
            )

    def _file_trace_args(
        self,
        runtime_trace: BaseTrace,
        trace,
        options: dict,
        *,
        scale_flag: str,
        relative_offsets: bool,
    ) -> list[str]:
        args = [
            "--input-file",
            str(runtime_trace.path),
            "--custom-dataset-type",
            runtime_trace.format,
        ]
        if self._uses_concurrency_mode(trace, options):
            concurrency = options.get("concurrency")
            args.extend(["--concurrency", str(1 if concurrency is None else concurrency)])
            return args
        args.extend(
            [
                "--fixed-schedule",
                scale_flag,
                str(trace.synthesis_speedup_ratio),
            ]
        )
        if trace.end_seconds is not None:
            index = runtime_trace.build_timeline_index()
            offset_origin = 0.0 if relative_offsets else float(index["minimum_timestamp"])
            args.extend(
                [
                    "--fixed-schedule-start-offset",
                    str(round((offset_origin + trace.start_seconds) * 1000)),
                    "--fixed-schedule-end-offset",
                    str(round((offset_origin + trace.end_seconds) * 1000)),
                ]
            )
        return args

    @singledispatchmethod
    def _trace_args(self, runtime_trace: BaseTrace, trace, options: dict) -> tuple[str, list[str]]:
        raise SimulationConfigurationError(f"AIPerf does not support {runtime_trace.format}")

    @_trace_args.register
    def _(self, runtime_trace: BaseTrace, trace, options: dict) -> tuple[str, list[str]]:
        return "chat", self._file_trace_args(
            runtime_trace,
            trace,
            options,
            scale_flag="--synthesis-speedup-ratio",
            relative_offsets=False,
        )

    @_trace_args.register
    def _(self, runtime_trace: BasetenTrace, trace, options: dict) -> tuple[str, list[str]]:
        return "completions", self._file_trace_args(
            runtime_trace,
            trace,
            options,
            scale_flag="--replay-speedup",
            relative_offsets=True,
        )

    @_trace_args.register
    def _(self, runtime_trace: WekaPublicDatasetTrace, trace, options: dict) -> tuple[str, list[str]]:
        concurrency = options.get("concurrency")
        args = [
            "--public-dataset",
            runtime_trace.dataset,
            "--concurrency",
            str(1 if concurrency is None else concurrency),
        ]
        flags = {
            "max_context_length": "--max-context-length",
            "trace_idle_gap_cap_seconds": "--trace-idle-gap-cap-seconds",
        }
        for option, flag in flags.items():
            if option in options:
                args.extend([flag, str(options[option])])
        return "chat", args

    async def command(self, task: SimulationTask) -> BackendCommand:
        trace = task.prompt.trace
        runtime_trace = trace_registry.resolve(trace.path, trace.format)
        options = task.simulation.backend_options
        endpoint_type, trace_args = await asyncio.to_thread(self._trace_args, runtime_trace, trace, options)
        args = [
            "profile",
            "--model",
            task.model_name,
            "--url",
            task.endpoint_url,
            "--endpoint-type",
            endpoint_type,
            "--artifact-dir",
            str(Path(task.task_dir) / "artifacts" / self.name),
            "--ui-type",
            str(options.get("ui_type", "none")),
        ]
        args.extend(
            [
                "--streaming",
                "--use-server-token-count",
                "--slice-duration",
                "1.0",
            ]
        )
        if self._supports_inferencex_agentx_mvp(runtime_trace, trace, options):
            args.extend(["--scenario", self.INFERENCEX_AGENTX_MVP_SCENARIO])
            if (
                task.simulation.duration_seconds < self.INFERENCEX_AGENTX_MVP_MIN_DURATION_SECONDS
                or runtime_trace.format == "mooncake_trace"
            ):
                args.append("--unsafe-override")
            if "trajectory_start_min_ratio" in options and options["trajectory_start_min_ratio"] is not None:
                args.extend(["--trajectory-start-min-ratio", str(options["trajectory_start_min_ratio"])])
            if "trajectory_start_max_ratio" in options and options["trajectory_start_max_ratio"] is not None:
                args.extend(["--trajectory-start-max-ratio", str(options["trajectory_start_max_ratio"])])
        args.extend(trace_args)
        args.extend(
            [
                "--benchmark-duration",
                str(task.simulation.duration_seconds),
                "--benchmark-grace-period",
                str(task.simulation.grace_period_seconds),
            ]
        )
        tokenizer = task.prompt.dataset.tokenizer
        if tokenizer:
            args.extend(["--tokenizer", tokenizer])
        raw_seed = options.get("random_seed")
        seed = self.DEFAULT_RANDOM_SEED if raw_seed is None else raw_seed
        args.extend(["--random-seed", str(seed)])
        if "num_profile_runs" in options:
            args.extend(["--num-profile-runs", str(options["num_profile_runs"])])
        if "profile_run_cooldown_seconds" in options and int(options.get("num_profile_runs", 1)) > 1:
            args.extend(
                [
                    "--profile-run-cooldown-seconds",
                    str(options["profile_run_cooldown_seconds"]),
                ]
            )
        if task.api_key:
            # Shared-Gateway deployments require a model access token; AIPerf
            # sends it as ``Authorization: Bearer <api-key>``.
            args.extend(["--api-key", task.api_key])
        return BackendCommand(
            args=tuple(args),
            timeout_seconds=math.ceil(trace.timeout_seconds + task.simulation.grace_period_seconds + 180),
        )

    def artifact_records(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[ArtifactRecord]:
        root = Path(task.task_dir) / "artifacts" / self.name
        path = _find_artifact_file(root, "profile_export.jsonl")
        if path is None:
            return []
        records: list[ArtifactRecord] = []

        def visit(record: dict) -> None:
            if not _aiperf_profiling_record(record):
                return
            metadata = record.get("metadata")
            metrics = record.get("metrics")
            metadata = metadata if isinstance(metadata, dict) else {}
            metrics = metrics if isinstance(metrics, dict) else {}

            def metric(name: str) -> float | None:
                raw = metrics.get(name)
                value = _number(raw.get("value") if isinstance(raw, dict) else raw, float("nan"))
                return value if math.isfinite(value) else None

            input_tokens = metric("input_sequence_length")
            if input_tokens is None:
                input_tokens = metric("usage_prompt_tokens")
            output_tokens = metric("output_sequence_length")
            if output_tokens is None:
                output_tokens = metric("usage_completion_tokens")
            mismatch_percent = metric("osl_mismatch_diff_pct")
            requested_output_tokens = output_tokens
            if output_tokens is not None and mismatch_percent is not None:
                ratio = 1 + mismatch_percent / 100
                if ratio > 0:
                    requested_output_tokens = round(output_tokens / ratio)
            started_ns = _number(metadata.get("request_start_ns"), float("nan"))
            completed_ns = _number(metadata.get("request_end_ns"), float("nan"))
            credit_issued_ns = _number(metadata.get("credit_issued_ns"), float("nan"))
            started = started_ns / 1_000_000_000
            completed = completed_ns / 1_000_000_000
            arrived_value = started if math.isfinite(started) else None
            completed_value = completed if math.isfinite(completed) else None
            drift_ms: float | None = None
            if math.isfinite(started_ns) and math.isfinite(credit_issued_ns):
                candidate_drift = (started_ns - credit_issued_ns) / 1_000_000
                if candidate_drift >= 0:
                    drift_ms = candidate_drift
            status_code = _response_status_code(record)
            error = bool(record.get("error") or record.get("timeout"))
            error_message = _error_message(record)
            if error_message is None and record.get("timeout"):
                error_message = "Request timed out"
            records.append(
                ArtifactRecord(
                    arrived=arrived_value,
                    completed=completed_value,
                    rate_completed=completed_value if completed_value is not None else arrived_value,
                    successful=not error,
                    issue_failed=error or (status_code is not None and not 200 <= status_code < 300),
                    status_code=status_code,
                    error=error_message,
                    request_id=(
                        record.get("request_id") or metadata.get("request_id") or record.get("id") or metadata.get("id")
                    ),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    requested_output_tokens=requested_output_tokens,
                    latency_ms=metric("request_latency"),
                    ttft_ms=metric("time_to_first_token"),
                    tpot_ms=metric("inter_token_latency"),
                    s_time_drift_ms=drift_ms,
                )
            )

        _visit_json_lines(path, "Invalid AIPerf record", visit, tolerate_incomplete=tolerate_incomplete)
        return records

    def artifact_time_origin(self, records: list[ArtifactRecord]) -> float:
        return min((record.arrived for record in records if record.arrived is not None), default=0)

    def expected_request_count(self, task: SimulationTask) -> int | None:
        if self._uses_concurrency_mode(task.prompt.trace, task.simulation.backend_options):
            return None
        return super().expected_request_count(task)

    def arrival_schedule_seconds(self, task: SimulationTask) -> list[float]:
        if self._uses_concurrency_mode(task.prompt.trace, task.simulation.backend_options):
            return []
        return super().arrival_schedule_seconds(task)

    async def parse(
        self,
        task: SimulationTask,
        command: CommandResult,
        version: str | None,
    ) -> SimulationResult:
        root = Path(task.task_dir) / "artifacts" / self.name

        def named(filename: str) -> Path | None:
            return _find_artifact_file(root, filename)

        summary_path = named("profile_export_aiperf.json")
        if summary_path is None:
            raise SimulationResultParseError("AIPerf did not produce profile_export_aiperf.json")
        raw = _read_object(summary_path, "Invalid AIPerf summary")
        requests_path = named("profile_export.jsonl")
        successful = failed = 0
        total_input = total_output = 0.0
        saw_profiling_records = False
        saw_input_tokens = False
        saw_output_tokens = False
        completion_seconds: list[float] = []
        successful_completion_seconds: list[float] = []
        failed_completion_seconds: list[float] = []
        client_timeout_completion_seconds: list[float] = []
        backend_error_completion_seconds: list[float] = []
        request_start_seconds: list[float] = []
        latency_records: list[tuple[float, float]] = []
        ttft_records: list[tuple[float, float]] = []
        tpot_records: list[tuple[float, float]] = []
        rate_records: list[tuple[float, float, float, bool]] = []

        def visit(record: dict) -> None:
            nonlocal successful, failed, total_input, total_output
            nonlocal saw_profiling_records, saw_input_tokens, saw_output_tokens
            if not _aiperf_profiling_record(record):
                return
            saw_profiling_records = True
            metadata = record.get("metadata")
            started = completed = float("nan")
            if isinstance(metadata, dict):
                metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
                started = _number(metadata.get("request_start_ns"), float("nan"))
                completed = _number(metadata.get("request_end_ns"), float("nan"))
                if math.isfinite(started):
                    request_start_seconds.append(started / 1_000_000_000)
                if math.isfinite(completed):
                    completion_seconds.append(completed / 1_000_000_000)
                request_latency = metrics.get("request_latency")
                latency_ms = _number(
                    (request_latency.get("value") if isinstance(request_latency, dict) else request_latency),
                    float("nan"),
                )
                if math.isfinite(started) and math.isfinite(latency_ms):
                    arrived = started / 1_000_000_000
                    latency_records.append((arrived, latency_ms))
                    for metric_name, destination in (
                        ("time_to_first_token", ttft_records),
                        ("inter_token_latency", tpot_records),
                    ):
                        metric = metrics.get(metric_name)
                        value = _number(
                            metric.get("value") if isinstance(metric, dict) else metric,
                            float("nan"),
                        )
                        if math.isfinite(value):
                            destination.append((arrived, value))
            if record.get("error"):
                failed += 1
                timestamp = completed / 1_000_000_000 if math.isfinite(completed) else started / 1_000_000_000
                if math.isfinite(timestamp):
                    failed_completion_seconds.append(timestamp)
                    if _response_status_code(record) is None:
                        client_timeout_completion_seconds.append(timestamp)
                    else:
                        backend_error_completion_seconds.append(timestamp)
                    rate_records.append((started / 1_000_000_000, timestamp, 0, True))
                return
            successful += 1
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}

            def _record_metric(primary: str, fallback: str) -> float:
                for name in (primary, fallback):
                    raw_val = metrics.get(name)
                    parsed = _number(raw_val.get("value") if isinstance(raw_val, dict) else raw_val, float("nan"))
                    if math.isfinite(parsed):
                        return parsed
                return float("nan")

            input_val = _record_metric("input_sequence_length", "usage_prompt_tokens")
            output_val = _record_metric("output_sequence_length", "usage_completion_tokens")
            if math.isfinite(input_val):
                saw_input_tokens = True
                total_input += input_val
            if math.isfinite(output_val):
                saw_output_tokens = True
                total_output += output_val
            timestamp = completed / 1_000_000_000 if math.isfinite(completed) else started / 1_000_000_000
            if math.isfinite(timestamp):
                successful_completion_seconds.append(timestamp)
                rate_records.append(
                    (
                        started / 1_000_000_000,
                        timestamp,
                        output_val if math.isfinite(output_val) else 0.0,
                        False,
                    )
                )

        if requests_path:
            _visit_json_lines(requests_path, "Invalid AIPerf record", visit)
        total = successful + failed
        if raw.get("request_count") is not None:
            successful = _average(raw, "request_count")
        if raw.get("error_request_count") is not None:
            failed = _average(raw, "error_request_count")
        if raw.get("request_count") is not None or raw.get("error_request_count") is not None:
            total = successful + failed
        if raw.get("total_isl") is not None:
            total_input = _average(raw, "total_isl")
        elif raw.get("total_usage_prompt_tokens") is not None:
            total_input = _average(raw, "total_usage_prompt_tokens")
        if raw.get("total_osl") is not None:
            total_output = _average(raw, "total_osl")
        elif raw.get("total_output_tokens") is not None:
            total_output = _average(raw, "total_output_tokens")
        elif raw.get("total_usage_completion_tokens") is not None:
            total_output = _average(raw, "total_usage_completion_tokens")
        latency = _metric(raw, "request_latency")
        ttft = _metric(raw, "time_to_first_token")
        tpot = _metric(raw, "inter_token_latency")
        warnings: list[str] = []
        missing_streaming_metrics = [name for name, value in (("TTFT", ttft), ("TPOT", tpot)) if value is None]
        if missing_streaming_metrics:
            unavailable = " and ".join(missing_streaming_metrics)
            if task.simulation.stream:
                warnings.append(f"AIPerf did not export {unavailable} metrics for this task.")
            else:
                warnings.append(
                    f"{unavailable} metrics are unavailable because this task used non-streaming "
                    "responses. Enable Stream responses to collect them."
                )
        # AIPerf ends its own benchmark cleanly after the grace period, cancelling
        # any still in-flight requests, so `command.timed_out` is rarely set. Detect
        # the same condition from the summary: fewer completed requests than the
        # trace schedule expected means the grace period cut requests short.
        try:
            expected = self.expected_request_count(task)
        except SimulationError:
            expected = None
        if command.timed_out or (expected is not None and total < expected):
            warnings.append(
                "Simulation reached the grace-period limit while requests were still in-flight; "
                "results reflect only the requests that completed before the limit."
            )
        drift_values = {
            percentile: _average(raw, f"replay_sched_lag_{percentile}")
            for percentile in ("p50", "p90", "p99")
            if raw.get(f"replay_sched_lag_{percentile}") is not None
        }
        drift = {f"{percentile}_ms": value for percentile, value in drift_values.items()} if drift_values else None
        artifacts = self.command_artifacts(command)
        for path, kind, media in [
            (summary_path, "summary", "application/json"),
            (requests_path, "per_request", "application/x-ndjson"),
            (named("profile_export_aiperf.csv"), "summary_csv", "text/csv"),
            (named("profile_export_aiperf_timeslices.json"), "timeslices_json", "application/json"),
            (named("profile_export_aiperf_timeslices.csv"), "timeslices_csv", "text/csv"),
            (named("inputs.json"), "inputs", "application/json"),
            (named("aiperf.log"), "tool_log", "text/plain"),
        ]:
            if path:
                artifacts.append(_artifact(kind, path, media))
        wall_time = (command.completed_at - command.started_at).total_seconds()
        origin_seconds = min(request_start_seconds) if request_start_seconds else 0
        request_span = (
            max(completion_seconds) - min(request_start_seconds)
            if request_start_seconds and completion_seconds
            else 0.0
        )
        benchmark_duration = _average(raw, "benchmark_duration")
        num_profile_runs = max(1, int(task.simulation.backend_options.get("num_profile_runs", 1) or 1))
        use_request_span = request_span > 0 and (num_profile_runs <= 1 or benchmark_duration <= 0)
        duration_seconds = (
            request_span if use_request_span else (benchmark_duration if benchmark_duration > 0 else wall_time)
        )
        has_raw_request_count = raw.get("request_count") is not None
        has_raw_error_count = raw.get("error_request_count") is not None
        has_raw_input = raw.get("total_isl") is not None or raw.get("total_usage_prompt_tokens") is not None
        has_raw_output = (
            raw.get("total_osl") is not None
            or raw.get("total_output_tokens") is not None
            or raw.get("total_usage_completion_tokens") is not None
        )

        has_request_counts = saw_profiling_records or has_raw_request_count or has_raw_error_count
        has_input_tokens = saw_input_tokens or has_raw_input
        has_output_tokens = saw_output_tokens or has_raw_output
        if task.simulation.stream and successful > 0 and not has_input_tokens and not has_output_tokens:
            warnings.append(
                "AIPerf did not record server token counts for this task because the endpoint "
                "omitted usage statistics in its responses."
            )

        if not use_request_span and num_profile_runs > 1:
            per_run_successful = successful if has_raw_request_count else (successful / num_profile_runs)
            per_run_failed = failed if has_raw_error_count else (failed / num_profile_runs)
            rate_total = per_run_successful + per_run_failed
            rate_input = total_input if has_raw_input else (total_input / num_profile_runs)
            rate_output = total_output if has_raw_output else (total_output / num_profile_runs)
        else:
            rate_total = total
            rate_input = total_input
            rate_output = total_output

        throughput_rps = (
            rate_total / duration_seconds
            if duration_seconds > 0 and has_request_counts
            else _average(raw, "request_throughput")
        )
        throughput_tps = (
            rate_output / duration_seconds
            if duration_seconds > 0 and has_output_tokens
            else _average(raw, "output_token_throughput")
        )
        input_throughput_tps = (
            rate_input / duration_seconds
            if duration_seconds > 0 and has_input_tokens
            else _average(raw, "input_token_throughput")
        )
        total_throughput_tps = (
            (rate_input + rate_output) / duration_seconds
            if duration_seconds > 0 and (has_input_tokens or has_output_tokens)
            else _average(raw, "total_token_throughput")
        )
        throughput_timeline, error_timeline = _rate_timelines(
            rate_records,
            origin_seconds=origin_seconds,
        )
        parsed_records = self.artifact_records(task)
        latency_heatmaps = self.latency_timelines_from_artifacts(task, records=parsed_records)
        return SimulationResult(
            run_id=task.id,
            backend=self.name,
            backend_version=version,
            summary={
                "total_requests": total,
                "successful_requests": successful,
                "failed_requests": failed,
                "success_rate": successful / total * 100 if total else 0,
                "duration_seconds": duration_seconds,
                "throughput_rps": throughput_rps,
                "throughput_tps": throughput_tps,
                "input_throughput_tps": input_throughput_tps,
                "total_throughput_tps": total_throughput_tps,
                "request_error_rate": _average(raw, "request_error_rate"),
                "effective_concurrency": _average(raw, "effective_concurrency"),
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "completion_timeline": _completion_timeline(
                    completion_seconds,
                    arrival_seconds=(
                        [origin_seconds + value for value in schedule]
                        if (schedule := self.arrival_schedule_seconds(task))
                        else request_start_seconds
                    ),
                    successful_completion_seconds=successful_completion_seconds,
                    failed_completion_seconds=failed_completion_seconds,
                    client_timeout_completion_seconds=client_timeout_completion_seconds,
                    backend_error_completion_seconds=backend_error_completion_seconds,
                    origin_seconds=origin_seconds,
                ),
                "latency_timeline": _latency_timeline(
                    latency_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_timeline": _latency_timeline(
                    ttft_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "tpot_timeline": _latency_timeline(
                    tpot_records,
                    arrival_seconds=request_start_seconds,
                    origin_seconds=(min(request_start_seconds) if request_start_seconds else 0),
                    end_seconds=(max(completion_seconds) if completion_seconds else None),
                ),
                "ttft_heatmap": latency_heatmaps["ttft_heatmap"],
                "tpot_heatmap": latency_heatmaps["tpot_heatmap"],
                "throughput_timeline": throughput_timeline,
                "goodput_timeline": self.goodput_timeline_from_artifacts(task, records=parsed_records),
                "error_timeline": error_timeline,
                "status_code_breakdown": self.status_code_breakdown_from_artifacts(task, records=parsed_records),
                "latency": latency,
                "ttft": ttft,
                "tpot": tpot,
                "drift": drift,
            },
            artifacts=artifacts,
            per_request=[],
            backend_metrics=redact_manifest_payload(raw),
            warnings=warnings,
        )
