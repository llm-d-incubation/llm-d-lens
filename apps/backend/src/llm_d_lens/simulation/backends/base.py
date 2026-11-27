"""Backend-neutral Simulation backend contracts and command execution."""

from __future__ import annotations

import asyncio
import math
import shlex
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from ..errors import SimulationConfigurationError, SimulationError
from ..incluster import execute_command_in_pod, resolve_incluster_target
from ..models import (
    ArtifactRecord,
    BackendAvailability,
    BackendCommand,
    BackendDescriptor,
    ScenarioDescriptor,
    SimulationArtifact,
    SimulationResponseCodeIssue,
    SimulationResponseCodeIssueSlice,
    SimulationResult,
    SimulationTask,
)
from ..process import CommandResult, RunContext, executable_availability, execute_command
from ..telemetry_live import LiveTelemetryCollector, classify_error_category, live_telemetry_registry
from ..telemetry_post import redact_secret_text, resolve_target_deployments
from ..traces import trace_registry
from .analytics import (
    completion_timeline,
    goodput_timeline,
    latency_heatmap,
    latency_timeline,
    rate_timelines,
    status_code_breakdown,
)

SCENARIOS = [
    ScenarioDescriptor(
        name="chat",
        display_name="Chat",
        description="Conversational and general assistant traffic.",
    ),
    ScenarioDescriptor(
        name="api-calling",
        display_name="Tool & API Use",
        description="Tool-use, agent, and business API traffic.",
    ),
    ScenarioDescriptor(
        name="coding",
        display_name="Code Generation",
        description="Code generation and software engineering traffic.",
    ),
]


def command_for_log(executable: str, args: Sequence[str]) -> str:
    sanitized = list(args)
    for index, value in enumerate(sanitized):
        if value == "--api-key" and index + 1 < len(sanitized):
            sanitized[index + 1] = "<redacted>"
            continue
        if value.startswith("--api-key="):
            sanitized[index] = "--api-key=<redacted>"
            continue
        if value not in {"--endpoint", "--url"} or index + 1 >= len(sanitized):
            continue
        parsed = urlparse(sanitized[index + 1])
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
        sanitized[index + 1] = parsed._replace(netloc=netloc, params="", query="", fragment="").geturl()
    return shlex.join([executable, *sanitized])


def artifact(kind: str, path: Path, media_type: str) -> SimulationArtifact:
    return SimulationArtifact(kind=kind, path=str(path), media_type=media_type)


class Backend(ABC):
    name: str
    display_name: str
    trace_formats: ClassVar[list[str]]
    capabilities: ClassVar[dict[str, Any]]
    scenarios: ClassVar[list[ScenarioDescriptor]] = SCENARIOS
    tolerate_timeout: ClassVar[bool] = False

    def normalize_backend_options(self, options: Mapping[str, Any]) -> dict[str, Any]:
        """Return a normalized copy of backend_options with backend defaults applied."""
        return dict(options)

    async def validate(self, task: SimulationTask) -> None:
        options = task.simulation.backend_options
        if "error_rate_slo" in options:
            value = options["error_rate_slo"]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 100
            ):
                raise SimulationConfigurationError("error_rate_slo must be a percentage from 0 to 100")
        for name in ("ttft_slo", "tpot_slo", "e2e_slo"):
            if name not in options or options[name] is None:
                continue
            value = options[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise SimulationConfigurationError(f"{name} must be a non-negative number")
        if "warmup_seconds" in options and options["warmup_seconds"] is not None:
            warmup = options["warmup_seconds"]
            if (
                not isinstance(warmup, (int, float))
                or isinstance(warmup, bool)
                or not math.isfinite(float(warmup))
                or float(warmup) < 0
                or float(warmup) >= float(task.simulation.duration_seconds)
            ):
                raise SimulationConfigurationError(
                    "warmup_seconds must be a non-negative number less than duration_seconds"
                )
        if (
            "collect_server_telemetry" in options
            and options["collect_server_telemetry"] is not None
            and not isinstance(options["collect_server_telemetry"], bool)
        ):
            raise SimulationConfigurationError("collect_server_telemetry must be a boolean")
        await self.validate_backend(task)

    @abstractmethod
    async def validate_backend(self, task: SimulationTask) -> None: ...

    @abstractmethod
    async def run(self, task: SimulationTask, context: RunContext) -> SimulationResult: ...

    def availability(self) -> BackendAvailability:
        return BackendAvailability(
            available=True,
            version=self.backend_version(None),
            unavailable_reason=None,
        )

    def descriptor(self) -> BackendDescriptor:
        availability = self.availability()
        return BackendDescriptor(
            name=self.name,
            display_name=self.display_name,
            api_version=1,
            available=availability.available,
            version=availability.version,
            unavailable_reason=availability.unavailable_reason,
            scenarios=self.scenarios,
            capabilities={**self.capabilities, "trace_formats": self.trace_formats},
        )

    def backend_version(self, detected_version: str | None) -> str | None:
        return detected_version

    def artifact_records(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
    ) -> list[ArtifactRecord]:
        return []

    def artifact_time_origin(self, records: list[ArtifactRecord]) -> float:
        return 0

    def expected_request_count(self, task: SimulationTask) -> int | None:
        trace = task.prompt.trace
        count = trace_registry.resolve(trace.path, trace.format).expected_request_count(
            trace,
            task.simulation.duration_seconds,
            include_end=False,
        )
        if count is not None and self.name == "aiperf":
            count *= int(task.simulation.backend_options.get("num_profile_runs", 1))
        return count

    def arrival_schedule_seconds(self, task: SimulationTask) -> list[float]:
        """Simulation-relative arrival times for every request the trace schedules
        within the replay window, independent of whether each request completed.

        Returns an empty list when the trace is unavailable so callers can fall
        back to per-request records.
        """
        trace = task.prompt.trace
        try:
            resolved = trace_registry.resolve(trace.path, trace.format)
            end_seconds = (
                trace.end_seconds
                if trace.end_seconds is not None
                else trace.start_seconds + task.simulation.duration_seconds * trace.synthesis_speedup_ratio
            )
            return resolved.arrival_schedule(
                start_seconds=trace.start_seconds,
                end_seconds=end_seconds,
                speedup_ratio=trace.synthesis_speedup_ratio,
                include_end=False,
            )
        except (OSError, ValueError, TypeError, SimulationError):
            return []

    def completed_request_count(self, task: SimulationTask) -> int:
        collector = live_telemetry_registry.get(task.id)
        if collector is not None and getattr(self, "live_tail_relative_path", None) is not None:
            return collector.completed_count()
        return len(self.artifact_records(task, tolerate_incomplete=True))

    def _resolve_records(
        self,
        task: SimulationTask,
        records: list[ArtifactRecord] | None,
        *,
        tolerate_incomplete: bool,
    ) -> list[ArtifactRecord]:
        if records is not None:
            return records
        return self.artifact_records(task, tolerate_incomplete=tolerate_incomplete)

    def completion_timeline_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
    ) -> list[dict[str, Any]]:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        origin = self.artifact_time_origin(records)
        schedule = self.arrival_schedule_seconds(task)
        arrival_seconds = (
            [origin + value for value in schedule]
            if schedule
            else [record.arrived for record in records if record.arrived is not None]
        )
        return completion_timeline(
            [record.completed for record in records if record.completed is not None],
            arrival_seconds=arrival_seconds,
            successful_completion_seconds=[
                record.completed for record in records if record.completed is not None and not record.failed_issue
            ],
            failed_completion_seconds=[
                record.completed for record in records if record.completed is not None and record.failed_issue
            ],
            client_timeout_completion_seconds=[
                record.completed
                for record in records
                if record.completed is not None and record.failed_issue and record.status_code is None
            ],
            backend_error_completion_seconds=[
                record.completed
                for record in records
                if record.completed is not None and record.failed_issue and record.status_code is not None
            ],
            origin_seconds=origin,
        )

    def latency_timelines_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
    ) -> dict[str, Any]:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        origin = self.artifact_time_origin(records)
        arrivals = [record.latency_arrival for record in records if record.latency_arrival is not None]
        completions = [record.latency_completion for record in records if record.latency_completion is not None]

        def metric(name: str) -> list[tuple[float, float]]:
            values: list[tuple[float, float]] = []
            for record in records:
                arrived = record.latency_arrival
                value = getattr(record, name)
                if arrived is not None and value is not None:
                    values.append((arrived, value))
            return values

        end = max(completions) if completions else None
        ttft_heatmap = latency_heatmap(
            [
                (record.latency_arrival, record.input_tokens, record.ttft_ms)
                for record in records
                if record.latency_arrival is not None and record.input_tokens is not None and record.ttft_ms is not None
            ],
            metric="ttft",
            sequence_length="input",
            sequence_length_source="observed",
            origin_seconds=origin,
        )
        tpot_heatmap = latency_heatmap(
            [
                (
                    record.latency_arrival,
                    (
                        record.requested_output_tokens
                        if record.requested_output_tokens is not None
                        else record.output_tokens
                    ),
                    record.tpot_ms,
                )
                for record in records
                if record.latency_arrival is not None
                and (record.requested_output_tokens is not None or record.output_tokens is not None)
                and record.tpot_ms is not None
            ],
            metric="tpot",
            sequence_length="output",
            sequence_length_source="requested",
            origin_seconds=origin,
        )
        return {
            "latency_timeline": latency_timeline(
                metric("latency_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "ttft_timeline": latency_timeline(
                metric("ttft_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "tpot_timeline": latency_timeline(
                metric("tpot_ms"),
                arrival_seconds=arrivals,
                origin_seconds=origin,
                end_seconds=end,
            ),
            "ttft_heatmap": ttft_heatmap.model_dump(mode="json") if ttft_heatmap is not None else None,
            "tpot_heatmap": tpot_heatmap.model_dump(mode="json") if tpot_heatmap is not None else None,
        }

    def rate_timelines_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        return rate_timelines(
            [
                (
                    record.arrived,
                    record.rate_completion,
                    record.rate_tokens,
                    record.failed_issue,
                )
                for record in records
                if record.arrived is not None and record.rate_completion is not None
            ],
            origin_seconds=self.artifact_time_origin(records),
        )

    def goodput_timeline_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
        warmup_seconds: float | None = None,
        e2e_slo: float | None = None,
    ) -> list[dict[str, Any]]:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        options = task.simulation.backend_options
        ttft_slo_ms = float(options["ttft_slo"]) * 1000 if options.get("ttft_slo") is not None else None
        tpot_slo_ms = float(options["tpot_slo"]) * 1000 if options.get("tpot_slo") is not None else None
        e2e_slo_val = e2e_slo if e2e_slo is not None else options.get("e2e_slo")
        e2e_slo_ms = float(e2e_slo_val) * 1000 if e2e_slo_val is not None else None
        warmup_val = float(warmup_seconds if warmup_seconds is not None else (options.get("warmup_seconds") or 0.0))
        origin = self.artifact_time_origin(records)

        def qualifies(record: ArtifactRecord) -> bool:
            if not record.successful or record.failed_issue:
                return False
            if warmup_val > 0 and record.rate_completion is not None and (record.rate_completion - origin) < warmup_val:
                return False
            if ttft_slo_ms is not None and (record.ttft_ms is None or record.ttft_ms > ttft_slo_ms):
                return False
            if tpot_slo_ms is not None and (record.tpot_ms is None or record.tpot_ms > tpot_slo_ms):
                return False
            return not (e2e_slo_ms is not None and (record.latency_ms is None or record.latency_ms > e2e_slo_ms))

        return [
            point.model_dump(mode="json")
            for point in goodput_timeline(
                [
                    (record.rate_completion, qualifies(record))
                    for record in records
                    if record.rate_completion is not None
                ],
                origin_seconds=origin,
            )
        ]

    def status_code_breakdown_from_artifacts(
        self,
        task: SimulationTask,
        *,
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
    ) -> list[dict[str, Any]]:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        return status_code_breakdown(Counter(record.status_code for record in records if record.failed_issue))

    @staticmethod
    def _record_error_category(record: ArtifactRecord) -> str:
        if getattr(record, "error_category", None):
            return str(record.error_category)
        is_transport = (
            record.arrived is None
            and record.completed is None
            and isinstance(record.error, str)
            and (record.error.startswith("Stream error:") or record.error.startswith("Transport error:"))
        )
        return classify_error_category(record.status_code, record.error, is_transport=is_transport)

    def response_code_issues_from_artifacts(
        self,
        task: SimulationTask,
        status_code: int | str | None = "all",
        *,
        offset: int = 0,
        limit: int = 100,
        sort: Literal["newest", "request_number"] = "request_number",
        tolerate_incomplete: bool = False,
        records: list[ArtifactRecord] | None = None,
        include_category: bool | None = None,
    ) -> SimulationResponseCodeIssueSlice:
        records = self._resolve_records(task, records, tolerate_incomplete=tolerate_incomplete)
        emit_category = isinstance(status_code, str) if include_category is None else include_category
        counts_by_code: dict[str, int] = {
            "all": 0,
            "4xx": 0,
            "5xx": 0,
            "timeout": 0,
            "transport": 0,
            "unknown": 0,
        }
        filter_str = str(status_code) if status_code is not None else "unknown"
        numeric_filter: int | None = None
        if isinstance(status_code, int):
            numeric_filter = status_code
        elif isinstance(status_code, str) and status_code not in {
            "all",
            "4xx",
            "5xx",
            "timeout",
            "transport",
            "unknown",
        }:
            numeric_filter = int(status_code)

        matching: list[tuple[int, ArtifactRecord, str]] = []
        for index, record in enumerate(records, 1):
            if not record.failed_issue:
                continue
            category = self._record_error_category(record)
            counts_by_code["all"] += 1
            counts_by_code[category] = counts_by_code.get(category, 0) + 1
            if record.status_code is not None:
                code_key = str(record.status_code)
                counts_by_code[code_key] = counts_by_code.get(code_key, 0) + 1

            matches = False
            if status_code == "all":
                matches = True
            elif status_code in {"4xx", "5xx", "timeout", "transport"}:
                matches = category == status_code
            elif status_code is None or filter_str == "unknown":
                matches = record.status_code is None
            elif numeric_filter is not None:
                matches = record.status_code == numeric_filter
            if matches:
                matching.append((index, record, category))

        if sort == "newest":
            matching.reverse()

        issues = []
        for request_number, record, category in matching[offset : offset + limit]:
            latency_ms = (
                max(0, record.completed - record.arrived) * 1000
                if record.arrived is not None and record.completed is not None
                else None
            )
            issues.append(
                SimulationResponseCodeIssue(
                    request_number=request_number,
                    request_id=record.request_id,
                    status_code=record.status_code,
                    category=category if emit_category else None,
                    error=redact_secret_text(record.error),
                    started_at_seconds=record.arrived,
                    latency_ms=latency_ms,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                )
            )
        return SimulationResponseCodeIssueSlice(
            total=len(matching),
            items=issues,
            counts_by_code=counts_by_code if emit_category else {},
        )

    def live_summary_from_artifacts(self, task: SimulationTask) -> dict[str, Any]:
        completion = self.completion_timeline_from_artifacts(task, tolerate_incomplete=True)
        latency = self.latency_timelines_from_artifacts(task, tolerate_incomplete=True)
        throughput, errors = self.rate_timelines_from_artifacts(task, tolerate_incomplete=True)
        goodput = self.goodput_timeline_from_artifacts(task, tolerate_incomplete=True)
        duration = throughput[-1]["end_seconds"] if throughput else 0
        records = self.artifact_records(task, tolerate_incomplete=True)
        origin = self.artifact_time_origin(records)
        completed = sum(point["completed_requests"] for point in throughput)
        output_tokens = sum(point["output_tokens"] for point in throughput)
        successful = sum(point["successful_requests"] for point in completion)
        failed = sum(point["failed_requests"] for point in completion)
        total = successful + failed

        def mean_metric(timeline: list[dict[str, Any]]) -> dict[str, float | None]:
            samples = [
                (point["request_count"], point["average_latency_ms"])
                for point in timeline
                if point["request_count"] and point["average_latency_ms"] is not None
            ]
            count = sum(item_count for item_count, _ in samples)
            return {"mean_ms": sum(item_count * value for item_count, value in samples) / count if count else None}

        return {
            "completion_timeline": completion,
            "throughput_timeline": throughput,
            "goodput_timeline": goodput,
            "error_timeline": errors,
            "timeline_origin_seconds": origin,
            "status_code_breakdown": self.status_code_breakdown_from_artifacts(task, tolerate_incomplete=True),
            "total_requests": total,
            "successful_requests": successful,
            "failed_requests": failed,
            "success_rate": successful / total * 100 if total else None,
            "throughput_rps": completed / duration if duration else None,
            "throughput_tps": output_tokens / duration if duration else None,
            "ttft": mean_metric(latency["ttft_timeline"]),
            "tpot": mean_metric(latency["tpot_timeline"]),
        }


class CommandBackend(Backend):
    #: Bare command name (e.g. ``"aiperf"``) if this backend can be installed
    #: on demand *inside* an in-cluster simulation pod via ``pip install``.
    #: When set, in-cluster runs skip host-side installation/availability
    #: checks entirely (the host never needs the tool) and the pod-side
    #: bootstrap in :mod:`llm_d_lens.simulation.incluster` installs it by
    #: name. Backends that require a host toolchain to build leave this
    #: ``None`` and keep relying on the host-built artifact being staged into
    #: the pod.
    pip_installable_in_pod: ClassVar[str | None] = None
    #: Relative filename under ``artifact_dir`` streamed incrementally via
    #: ``tail -c +<offset+1>`` when running inside a cluster Pod.
    live_tail_relative_path: ClassVar[str | None] = None

    @property
    @abstractmethod
    def executable(self) -> str: ...

    @abstractmethod
    async def command(self, task: SimulationTask) -> BackendCommand: ...

    @abstractmethod
    async def parse(
        self,
        task: SimulationTask,
        command: CommandResult,
        version: str | None,
    ) -> SimulationResult: ...

    def availability(self) -> BackendAvailability:
        return executable_availability(self.executable)

    async def prepare_executable(self, context: RunContext) -> str:
        return self.executable

    async def run(self, task: SimulationTask, context: RunContext) -> SimulationResult:
        context.progress(0, "Preparing simulation")
        await self.validate(task)
        incluster_target = resolve_incluster_target(task)
        if task.endpoint_mode == "deployment" and incluster_target is None:
            # Deployment-bound tasks must always run from inside an in-cluster
            # test pod so traffic is load-balanced across every replica the
            # same way real clients see it; there is no host-side fallback
            # ("local endpoint") execution path anymore. Reaching this means
            # the deployment's namespace/cluster could not be resolved.
            raise SimulationConfigurationError(
                "Deployment-bound simulation tasks require an in-cluster test pod, but the "
                "target deployment's namespace/cluster could not be resolved."
            )
        detected_version: str | None = None
        if incluster_target is not None and self.pip_installable_in_pod:
            # The tool is installed on demand inside the pod; the host
            # never needs it, so skip the host-side install/probe and just
            # use the bare command name for logging and pod execution.
            executable = self.pip_installable_in_pod
        else:
            executable = await self.prepare_executable(context)
            availability = executable_availability(executable)
            if not availability.available:
                raise SimulationConfigurationError(availability.unavailable_reason or "Backend unavailable")
            detected_version = availability.version
        command = await self.command(task)
        trace = task.prompt.trace
        if trace.end_seconds is not None:
            input_path = None
            for flag in ("--input-file", "--dataset-path"):
                if flag in command.args:
                    input_path = command.args[command.args.index(flag) + 1]
                    break
            context.log(
                "Replay range: "
                f"{trace.start_seconds:.3f}s-{trace.end_seconds:.3f}s source time; "
                f"scale={trace.synthesis_speedup_ratio:g}x; "
                f"duration={task.simulation.duration_seconds}s" + (f"; input={input_path}" if input_path else "")
            )
        context.log(f"Command: {command_for_log(executable, command.args)}")
        expected_requests = await asyncio.to_thread(self.expected_request_count, task)
        context.progress(0, "Starting simulation backend")
        artifact_dir = Path(task.task_dir) / "artifacts" / self.name
        requests_path = artifact_dir / (self.live_tail_relative_path or "requests.jsonl")
        stdout_path = artifact_dir / "stdout.log"
        arrival_schedule = await asyncio.to_thread(self.arrival_schedule_seconds, task)
        target_deployments = await resolve_target_deployments(task)
        collector = LiveTelemetryCollector(
            task,
            requests_path=requests_path,
            stdout_path=stdout_path,
            arrival_schedule=arrival_schedule,
            target_deployments=target_deployments,
        )
        live_telemetry_registry.register(task.id, collector)
        loop = asyncio.get_running_loop()
        original_on_ready = context.on_ready
        collector_started = False
        start_task: asyncio.Task[None] | None = None

        def on_ready_with_collector() -> None:
            nonlocal collector_started, start_task
            if original_on_ready is not None:
                original_on_ready()
            collector.mark_execution_started()
            if not collector_started:
                collector_started = True
                start_task = loop.create_task(collector.start())

        context.on_ready = on_ready_with_collector
        progress_fn = (
            (lambda: (collector.completed_count(), expected_requests))
            if self.live_tail_relative_path is not None
            else (lambda: (self.completed_request_count(task), expected_requests))
        )
        try:
            if incluster_target is not None:
                namespace, cluster_id = incluster_target
                result = await execute_command_in_pod(
                    namespace=namespace,
                    cluster_id=cluster_id,
                    task_dir=Path(task.task_dir),
                    executable=executable,
                    args=command.args,
                    artifact_dir=artifact_dir,
                    timeout_seconds=command.timeout_seconds,
                    context=context,
                    env=dict(command.env) or None,
                    progress_request_counts=progress_fn,
                    tolerate_timeout=self.tolerate_timeout,
                    live_tail_relative_path=self.live_tail_relative_path,
                )
            else:
                result = await execute_command(
                    executable,
                    command.args,
                    artifact_dir,
                    command.timeout_seconds,
                    context,
                    progress_request_counts=progress_fn,
                    tolerate_timeout=self.tolerate_timeout,
                    env=dict(command.env) or None,
                )
            collector.set_phase("Collecting Telemetry")
            context.progress(96, "Collecting telemetry and analyzing results")
            parsed = await self.parse(task, result, self.backend_version(detected_version))
            context.progress(98, "Finalizing simulation results")
            return parsed
        finally:
            context.on_ready = original_on_ready
            if start_task is not None and not start_task.done():
                start_task.cancel()
            if start_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await start_task
            try:
                await collector.stop()
            finally:
                live_telemetry_registry.unregister(task.id)

    @staticmethod
    def command_artifacts(result: CommandResult) -> list[SimulationArtifact]:
        return [
            artifact("stdout", result.stdout_path, "text/plain"),
            artifact("stderr", result.stderr_path, "text/plain"),
        ]
