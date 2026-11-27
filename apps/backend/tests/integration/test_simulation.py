import asyncio
import importlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import llm_d_lens.simulation.service as simulation_service
from llm_d_lens.api import app
from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
from llm_d_lens.model_service.contracts import ModelServiceGroup
from llm_d_lens.simulation.backends import (
    Backend,
    get_backend,
    list_backends,
    live_completion_timeline_from_artifacts,
    live_summary_from_artifacts,
    register_backend,
    response_code_issues_from_artifacts,
    status_code_breakdown_from_artifacts,
)
from llm_d_lens.simulation.backends import command_for_log as _command_for_log
from llm_d_lens.simulation.backends.aiperf import AIPerfBackend
from llm_d_lens.simulation.endpoints import models_url
from llm_d_lens.simulation.errors import (
    SimulationCancelledError,
    SimulationConfigurationError,
)
from llm_d_lens.simulation.incluster import resolve_incluster_target
from llm_d_lens.simulation.models import (
    ArtifactRecord,
    BackendCommand,
    BackendDescriptor,
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationRequestResult,
    SimulationResponseCodeIssuePage,
    SimulationResult,
    SimulationTask,
    SimulationTaskCreateRequest,
    SimulationTaskListPage,
    SimulationTaskRerunRequest,
    SimulationTrace,
)
from llm_d_lens.simulation.process import CommandResult, RunContext, execute_command
from llm_d_lens.simulation.service import (
    create_task,
    delete_task,
    list_tasks,
    load_task,
    rerun_task,
    reset_service_state,
    save_task,
    stop_task,
)
from llm_d_lens.simulation.traces import (
    BailianTrace,
    BasetenTrace,
    BaseTrace,
    BurstGPTTrace,
    MooncakeTrace,
    WekaPublicDatasetTrace,
    trace_registry,
)


@pytest.fixture
def simulation_root(tmp_path, monkeypatch):
    traces = tmp_path / "datasets"
    traces.mkdir()
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIPERF_EXECUTABLE", sys.executable)
    reset_service_state()
    monkeypatch.setattr(simulation_service, "discover_models", AsyncMock(return_value={"models": ["model"]}))
    yield tmp_path
    reset_service_state()
    shutil.rmtree(tmp_path, ignore_errors=True)


def task_record(root: Path, task_id: str, status: str = "completed") -> SimulationTask:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"task {task_id}",
        description="",
        scenario="chat",
        status=status,
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        simulation=SimulationConfig(
            backend="aiperf",
            backend_options={},
            duration_seconds=60,
            num_requests=None,
            stream=True,
            warmup_enabled=False,
            grace_period_seconds=30,
        ),
        prompt=SimulationPrompt(
            type="trace",
            dataset=SimulationDataset(
                name="mooncake-arxiv",
                scenario="chat",
                tokenizer=None,
            ),
            trace=SimulationTrace(
                path="mooncake_trace.jsonl",
                format="mooncake_trace",
                fixed_schedule=True,
                timeout_seconds=900,
                synthesis_speedup_ratio=1,
            ),
        ),
        task_dir=str(root / "artifacts" / "simulations" / task_id),
        progress_percent=100 if status == "completed" else 0,
        progress_message=status,
        logs=["large log"],
        result=None,
        error_message=None,
        created_at=now,
        started_at=None,
        completed_at=now if status in {"completed", "failed", "cancelled"} else None,
        owner_pid=None,
        owner_instance_id=None,
    )


def update_simulation(task: SimulationTask, **changes) -> None:
    task.simulation = SimulationConfig.model_validate({**task.simulation.model_dump(mode="python"), **changes})


def update_dataset(task: SimulationTask, **changes) -> None:
    dataset = SimulationDataset.model_validate({**task.prompt.dataset.model_dump(mode="python"), **changes})
    task.prompt = task.prompt.model_copy(update={"dataset": dataset}, deep=True)


def update_trace(task: SimulationTask, **changes) -> None:
    trace = SimulationTrace.model_validate({**task.prompt.trace.model_dump(mode="python"), **changes})
    task.prompt = task.prompt.model_copy(update={"trace": trace}, deep=True)


def test_catalog_and_fastapi_contract(simulation_root):
    backends = list_backends()
    assert all(isinstance(backend, BackendDescriptor) for backend in backends)
    assert [backend.name for backend in backends] == ["aiperf"]
    assert all(backend.version for backend in backends)
    capabilities = {backend.name: backend.capabilities for backend in backends}
    assert capabilities["aiperf"]["supports_trace_range"] is True
    assert capabilities["aiperf"]["supports_concurrency"] is True
    assert capabilities["aiperf"]["scale_factor_trace_formats"] == ["baseten_trace"]
    assert capabilities["aiperf"]["trace_format_capabilities"] == {
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
    }
    aiperf_options = {option["name"]: option for option in capabilities["aiperf"]["advanced_options"]}
    assert set(aiperf_options) >= {
        "random_seed",
        "num_profile_runs",
        "concurrency",
        "trajectory_start_min_ratio",
        "trajectory_start_max_ratio",
        "max_context_length",
    }
    assert len(capabilities["aiperf"]["inferencex_agentx_mvp_weka_datasets"]) == 14
    assert aiperf_options["trajectory_start_min_ratio"] == {
        "name": "trajectory_start_min_ratio",
        "label": "Trajectory start min ratio",
        "minimum": 0.0,
        "maximum": 1.0,
        "default": 0.25,
    }
    assert aiperf_options["trajectory_start_max_ratio"] == {
        "name": "trajectory_start_max_ratio",
        "label": "Trajectory start max ratio",
        "minimum": 0.0,
        "maximum": 1.0,
        "default": 0.75,
    }
    assert aiperf_options["random_seed"] == {
        "name": "random_seed",
        "label": "Random seed",
        "minimum": 0,
        "default": 666,
    }
    assert "trace_formats" not in aiperf_options["concurrency"]
    assert aiperf_options["concurrency"]["minimum"] == 1
    assert aiperf_options["concurrency"]["default"] == 1
    assert aiperf_options["max_context_length"]["trace_formats"] == ["weka_public_dataset"]
    assert aiperf_options["trace_idle_gap_cap_seconds"]["trace_formats"] == ["weka_public_dataset"]
    assert trace_registry.get("bailian-coder")["scenario"] == "coding"
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/simulation/health").status_code == 200
        assert client.get("/api/simulation/docs").status_code == 200
        assert client.get("/api/simulation/openapi.json").status_code == 200
        backends_resp = client.get("/api/simulation/backends")
        assert backends_resp.status_code == 200
        assert [item["name"] for item in backends_resp.json()["backends"]] == ["aiperf"]
        scenarios = client.get("/api/simulation/scenarios").json()["scenarios"]
        assert [scenario["name"] for scenario in scenarios] == [
            "chat",
            "api-calling",
            "coding",
        ]
        datasets = client.get("/api/simulation/trace-datasets").json()["datasets"]
        assert all(item["supported_backends"] == ["aiperf"] for item in datasets)
        coder = next(item for item in datasets if item["name"] == "bailian-coder")
        assert coder["supported_backends"] == ["aiperf"]
        baseten = next(item for item in datasets if item["name"] == "baseten-synthetic")
        assert baseten["supported_backends"] == ["aiperf"]
        weka = next(item for item in datasets if item["name"] == "weka-claude-code")
        assert weka["scenario"] == "coding"
        assert weka["supported_backends"] == ["aiperf"]
        assert weka["source_type"] == "external_dataset"
        assert weka["downloaded"] is True
        burst = next(item for item in datasets if item["name"] == "burstgpt-conversation")
        assert burst["supported_backends"] == ["aiperf"]
        assert not any(item["name"].startswith("azure-") for item in datasets)
        assert (
            client.post(
                "/api/simulation/trace-datasets/download",
                json={"dataset": "unknown", "url": "https://example.com/trace"},
            ).status_code
            == 422
        )
        assert client.get("/api/simulation/tasks?created_after=not-a-date").status_code == 422
        assert (
            client.post(
                "/api/simulation/models/discover",
                json={"endpoint_url": "not-a-url"},
            ).status_code
            == 400
        )


def test_backend_registration_rejects_duplicate_names():
    list_backends()

    class DuplicateBackend(Backend):
        name = "aiperf"
        display_name = "Duplicate"
        trace_formats = []
        capabilities = {}

        async def validate_backend(self, task, context=None):
            return None

        async def run(self, task, context):
            return {}

    with pytest.raises(ValueError, match="already registered"):
        register_backend(DuplicateBackend)


def test_backend_discovery_and_module_boundaries():
    discovered = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from llm_d_lens.simulation.backends.registry import list_backends; "
                "print(json.dumps([item.name for item in list_backends()]))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(discovered.stdout) == ["aiperf"]
    assert AIPerfBackend.__module__ == "llm_d_lens.simulation.backends.aiperf"
    base_source = Path(importlib.import_module("llm_d_lens.simulation.backends.base").__file__).read_text(
        encoding="utf-8"
    )
    assert "AIPerf" not in base_source
    assert "Trace-Replayer" not in base_source
    assert ".aiperf" not in base_source
    assert ".trace_replayer" not in base_source


def test_non_command_backend_contract():
    class InProcessBackend(Backend):
        name = "in-process"
        display_name = "In-process"
        trace_formats = []
        capabilities = {"prompt_kinds": ["trace"]}

        async def validate_backend(self, task):
            return None

        async def run(self, task, context):
            return SimulationResult(
                run_id=task.id,
                backend="aiperf",
                backend_version="in-process",
                summary={},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=[],
            )

    backend = InProcessBackend()
    assert backend.descriptor().available is True
    result = asyncio.run(backend.run(task_record(Path("."), "1234abcd"), None))
    assert isinstance(result, SimulationResult)
    assert result.run_id == "1234abcd"


def test_task_details_backfills_backend_version(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult(
        run_id=task.id,
        backend="aiperf",
        backend_version=None,
        summary={},
        artifacts=[],
        per_request=[],
        backend_metrics={},
        warnings=[],
    )
    asyncio.run(save_task(task))

    with TestClient(app) as client:
        response = client.get("/api/simulation/tasks/1234abcd")

    assert response.status_code == 200
    assert response.json()["result"]["backend_version"] == AIPerfBackend.VERSION
    loaded = asyncio.run(load_task(task.id))
    assert loaded is not None
    assert loaded.result is not None
    assert loaded.result.backend_version == AIPerfBackend.VERSION
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["source_version"]["backend_version"] == "unknown"


@pytest.mark.asyncio
async def test_typed_persistence_round_trip_preserves_result_extensions(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult.model_validate(
        {
            "run_id": task.id,
            "backend": "aiperf",
            "backend_version": "0.12.0",
            "summary": {"custom_summary_metric": {"value": 7}},
            "artifacts": [
                SimulationArtifact(
                    kind="summary",
                    path=str(Path(task.task_dir) / "artifacts" / "summary.json"),
                    media_type="application/json",
                )
            ],
            "per_request": [{"request_id": "request-1", "ttft_ms": None}],
            "backend_metrics": {"custom_backend_metric": True},
            "warnings": [],
            "historical_result_extension": {"kept": True},
        }
    )

    await save_task(task)
    loaded = await load_task(task.id)

    assert isinstance(loaded, SimulationTask)
    assert loaded is not task
    assert isinstance(loaded.result, SimulationResult)
    assert isinstance(loaded.result.artifacts[0], SimulationArtifact)
    assert isinstance(loaded.result.per_request[0], SimulationRequestResult)
    assert loaded.result.summary["custom_summary_metric"] == {"value": 7}
    assert loaded.result.per_request[0].request_id == "request-1"
    assert loaded.result.per_request[0].ttft_ms is None
    assert loaded.result.model_extra["historical_result_extension"] == {"kept": True}
    persisted = SimulationTaskDao().get(task.id)
    assert persisted is not None
    assert persisted.result is not None
    assert persisted.result.model_extra["historical_result_extension"] == {"kept": True}


@pytest.mark.asyncio
async def test_historical_task_json_loads_with_defaults_and_typed_results(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    payload = task.model_dump(mode="json", exclude={"live_summary"})
    payload.pop("endpoint_mode")
    payload.pop("endpoint_namespace")
    payload.pop("endpoint_service")
    payload["prompt"]["trace"].pop("start_seconds")
    payload["prompt"]["trace"].pop("end_seconds")
    payload["result"] = {
        "run_id": task.id,
        "backend": "aiperf",
        "summary": {"ttft": None, "legacy_metric": 9},
        "artifacts": [
            {
                "kind": "summary",
                "path": str(Path(task.task_dir) / "summary.json"),
                "media_type": "application/json",
            }
        ],
        "per_request": [],
        "backend_metrics": {},
        "warnings": [],
        "legacy_parser_detail": "preserve-me",
    }
    directory = Path(task.task_dir)
    directory.mkdir(parents=True)
    (directory / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = await load_task(task.id)

    assert isinstance(loaded, SimulationTask)
    assert loaded.endpoint_mode == "external"
    assert loaded.prompt.trace.start_seconds == 0
    assert loaded.prompt.trace.end_seconds is None
    assert isinstance(loaded.result, SimulationResult)
    assert loaded.result.backend_version is None
    assert loaded.result.summary["ttft"] is None
    assert loaded.result.model_extra["legacy_parser_detail"] == "preserve-me"
    await save_task(loaded)
    (directory / "task.json").unlink()
    round_trip = await load_task(task.id)
    assert round_trip is not None
    assert round_trip.result is not None
    assert round_trip.result.model_extra["legacy_parser_detail"] == "preserve-me"


def test_model_freezing_lifecycle_mutation_and_deep_copy(simulation_root):
    task = task_record(simulation_root, "1234abcd", "queued")
    with pytest.raises(ValidationError):
        task.simulation.duration_seconds = 90
    with pytest.raises(ValidationError):
        task.prompt.trace.path = "other.jsonl"
    with pytest.raises(ValidationError):
        task.status = "invalid"

    copied = task.model_copy(deep=True)
    copied.status = "running"
    copied.logs.append("copy-only")
    update_simulation(copied, backend_options={"threads": 4})

    assert task.status == "queued"
    assert task.logs == ["large log"]
    assert task.simulation.backend_options == {}
    assert copied.status == "running"
    assert copied.simulation.backend_options == {"threads": 4}


@pytest.mark.asyncio
async def test_service_passes_typed_task_to_backend(simulation_root, monkeypatch):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    backend = get_backend("aiperf")
    received = []

    async def validate(task):
        received.append(task)

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)
    request = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        trace_dataset="mooncake-arxiv",
        trace_path=trace.name,
    )

    task = await create_task(request)

    assert isinstance(task, SimulationTask)
    assert received == [task]
    simulation_service.discover_models.assert_awaited_once_with(request.endpoint_url, api_key=None)
    assert simulation_service.tasks[task.id] is task


def test_create_api_keeps_json_shape_and_passes_typed_request(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd", "queued")
    received = []

    async def fake_create(request, **_kwargs):
        received.append(request)
        return task

    monkeypatch.setattr(importlib.import_module("llm_d_lens.simulation.router"), "create_task", fake_create)
    with TestClient(app) as client:
        response = client.post(
            "/api/simulation/tasks",
            json={
                "name": "API replay",
                "description": "",
                "scenario": "chat",
                "backend": "aiperf",
                "endpoint_url": "http://127.0.0.1:8000",
                "model_name": "model",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": "mooncake_trace.jsonl",
                "duration_seconds": 60,
                "scale_factor": 1,
                "trace_timeout_seconds": 3600,
                "fixed_schedule": True,
                "stream": True,
                "backend_options": {},
            },
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"] == task.id
    assert payload["status"] == "queued"
    assert payload["task"]["id"] == task.id
    assert payload["task"]["simulation"]["backend"] == "aiperf"
    assert "live_summary" not in payload["task"]
    assert len(received) == 1
    assert isinstance(received[0], SimulationTaskCreateRequest)


def test_models_url_normalization():
    assert models_url("http://localhost:8000") == "http://localhost:8000/v1/models"
    assert models_url("https://example.com/v1") == "https://example.com/v1/models"
    assert models_url("https://example.com/v1/chat/completions?ignored=true") == "https://example.com/v1/models"


def test_command_log_quotes_arguments_and_redacts_url_credentials():
    command = _command_for_log(
        "/opt/trace replayer",
        [
            "--endpoint",
            "https://user:secret@example.com:8443/v1/chat/completions?token=secret#fragment",
            "--model-name",
            "model name",
        ],
    )

    assert command == (
        "'/opt/trace replayer' --endpoint https://example.com:8443/v1/chat/completions --model-name 'model name'"
    )
    assert "secret" not in command
    assert "user" not in command


@pytest.mark.parametrize(
    ("environment_name", "backend_type"),
    [
        ("AIPERF_EXECUTABLE", AIPerfBackend),
    ],
)
@pytest.mark.asyncio
async def test_backend_installs_its_executable(
    simulation_root,
    monkeypatch,
    environment_name,
    backend_type,
):
    monkeypatch.delenv(environment_name)
    monkeypatch.setenv(
        "SIMULATION_BACKEND_CACHE_DIR",
        str(simulation_root / "backend-cache"),
    )
    if environment_name == "AIPERF_EXECUTABLE":
        monkeypatch.setattr(
            importlib.import_module(backend_type.__module__).sys,
            "executable",
            str(simulation_root / "runtime" / "bin" / "python"),
        )
    monkeypatch.setattr(
        importlib.import_module(backend_type.__module__),
        "which",
        lambda executable: None,
    )
    backend = backend_type()
    destination = backend.managed_executable_path()

    def install(path):
        assert path == destination
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(backend_type, "_install", staticmethod(install))
    logs = []
    context = RunContext(asyncio.Event(), logs.append, lambda *_args: None)
    executable = await backend.prepare_executable(context)

    assert executable == str(destination)
    assert destination.is_file()
    assert "install" in logs[0].lower()
    assert str(destination) in logs[1]


@pytest.mark.parametrize("backend", [AIPerfBackend()])
@pytest.mark.asyncio
async def test_all_command_backends_log_the_launch_command(
    simulation_root,
    monkeypatch,
    backend,
):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend=backend.name)
    if backend.name == "aiperf":
        update_trace(task, start_seconds=10, end_seconds=20)
    executable = sys.executable
    logs = []

    async def validate(_task):
        return None

    async def prepare(_context):
        return executable

    async def command(_task):
        args = ["--version"]
        if backend.name == "aiperf":
            args.extend(["--input-file", str(simulation_root / "selected_trace.parquet")])
        return BackendCommand(args=tuple(args), timeout_seconds=10)

    async def parse(_task, _result, _version):
        return SimulationResult(
            run_id=_task.id,
            backend=backend.name,
            backend_version=_version,
            summary={},
            artifacts=[],
            per_request=[],
            backend_metrics={},
            warnings=[],
        )

    async def execute(*_args, **_kwargs):
        root = Path(task.task_dir)
        root.mkdir(parents=True)
        now = datetime.now(UTC)
        return CommandResult(0, now, now, root / "stdout.log", root / "stderr.log")

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(backend, "prepare_executable", prepare)
    monkeypatch.setattr(backend, "command", command)
    monkeypatch.setattr(backend, "parse", parse)
    monkeypatch.setattr(backend, "expected_request_count", lambda _task: 1)
    monkeypatch.setattr("llm_d_lens.simulation.backends.base.execute_command", execute)
    context = RunContext(asyncio.Event(), logs.append, lambda *_args: None)

    await backend.run(task, context)

    assert any(log.startswith(f"Command: {executable} --version") for log in logs)
    if backend.name == "aiperf":
        assert any(
            "Replay range: 10.000s-20.000s source time" in log
            and f"input={simulation_root / 'selected_trace.parquet'}" in log
            for log in logs
        )


@pytest.mark.asyncio
async def test_rerun_copies_configuration(simulation_root, monkeypatch):
    original = task_record(simulation_root, "1234abcd")
    original.name = "production replay"
    original.endpoint_mode = "in-cluster"
    original.endpoint_namespace = "serving"
    original.endpoint_service = "gateway"
    original.simulation = SimulationConfig(
        backend="aiperf",
        backend_options={"threads": 4},
        duration_seconds=12.5,
        num_requests=None,
        stream=True,
        warmup_enabled=False,
        grace_period_seconds=7.5,
    )
    update_dataset(original, tokenizer="/models/tokenizer.json")
    update_trace(original, start_seconds=10, end_seconds=40, timeout_seconds=90.5)
    await save_task(original)
    monkeypatch.setattr("llm_d_lens.simulation.service._schedule_task", lambda _task: None)

    class AvailableBackend:
        async def validate(self, _task):
            return None

    monkeypatch.setattr(simulation_service, "get_backend", lambda _name: AvailableBackend())
    rerun = await rerun_task(original.id)
    assert rerun.name == f"production replay · rerun {rerun.id}"
    assert rerun.endpoint_mode == "in-cluster"
    assert rerun.endpoint_namespace == "serving"
    assert rerun.prompt.dataset.tokenizer == "/models/tokenizer.json"
    assert rerun.prompt.trace.start_seconds == 10
    assert rerun.prompt.trace.end_seconds == 40
    assert rerun.prompt.trace.timeout_seconds == 90.5
    assert rerun.simulation.duration_seconds == 12.5
    assert rerun.simulation.grace_period_seconds == 7.5
    assert rerun.simulation.backend_options == {"threads": 4, "random_seed": 666}
    assert rerun.result is None
    assert rerun.logs == []


@pytest.mark.asyncio
async def test_stop_is_idempotent_for_terminal_tasks(simulation_root):
    for index, status in enumerate(("completed", "failed", "cancelled")):
        task = task_record(simulation_root, f"1234abc{index}", status)
        await save_task(task)
        await stop_task(task.id)
        loaded = await load_task(task.id)
        assert loaded.status == status


def test_simulation_config_backend_options_are_immutable():
    config = SimulationConfig(
        backend="aiperf",
        backend_options={"nested": {"percentiles": [90, 99]}},
        duration_seconds=60,
        num_requests=None,
        stream=True,
        warmup_enabled=False,
        grace_period_seconds=30,
    )

    with pytest.raises(TypeError, match="cannot be modified"):
        config.backend_options["threads"] = 4
    with pytest.raises(TypeError, match="cannot be modified"):
        config.backend_options["nested"]["threads"] = 4
    with pytest.raises(TypeError):
        config.backend_options["nested"]["percentiles"][0] = 50
    assert config.model_dump(mode="json")["backend_options"] == {"nested": {"percentiles": [90, 99]}}


@pytest.mark.asyncio
async def test_same_target_tasks_run_in_creation_order(simulation_root, monkeypatch):
    first = task_record(simulation_root, "11111111", "queued")
    second = task_record(simulation_root, "22222222", "queued")
    started = []
    first_release = asyncio.Event()
    second_release = asyncio.Event()

    async def fake_run(task):
        task.status = "running"
        started.append(task.id)
        await (first_release if task.id == first.id else second_release).wait()
        task.status = "completed"

    monkeypatch.setattr(simulation_service, "_run_task", fake_run)
    simulation_service._schedule_task(first)
    simulation_service._schedule_task(second)
    scheduled = list(simulation_service.runners.values())
    await asyncio.sleep(0.05)
    assert started == ["11111111"]
    assert second.status == "queued"

    first_release.set()
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert started == ["11111111", "22222222"]
    second_release.set()
    await asyncio.gather(*scheduled)


@pytest.mark.asyncio
async def test_different_targets_run_concurrently(simulation_root, monkeypatch):
    first = task_record(simulation_root, "33333333", "queued")
    second = task_record(simulation_root, "44444444", "queued").model_copy(
        update={"endpoint_url": "http://127.0.0.1:8001"}
    )
    started = []
    release = asyncio.Event()

    async def fake_run(task):
        task.status = "running"
        started.append(task.id)
        await release.wait()
        task.status = "completed"

    monkeypatch.setattr(simulation_service, "_run_task", fake_run)
    simulation_service._schedule_task(first)
    simulation_service._schedule_task(second)
    scheduled = list(simulation_service.runners.values())
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert started == ["33333333", "44444444"]
    release.set()
    await asyncio.gather(*scheduled)


@pytest.mark.asyncio
async def test_backend_scenario_dataset_file_binding(simulation_root):
    trace = simulation_root / "datasets" / "qwen_coder_blksz_16.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    request = SimulationTaskCreateRequest(
        name="mismatch",
        description="",
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        trace_dataset="bailian-coder",
        trace_path=trace.name,
        duration_seconds=60,
        scale_factor=1,
        trace_timeout_seconds=3600,
        fixed_schedule=True,
        stream=True,
        backend_options={},
    )
    with pytest.raises(SimulationConfigurationError, match="belongs to scenario 'coding'"):
        await create_task(request)
    request = request.model_copy(update={"scenario": "coding", "trace_path": "other.jsonl"})
    with pytest.raises(SimulationConfigurationError, match="does not match"):
        await create_task(request)
    request = request.model_copy(update={"trace_path": trace.name, "scale_factor": 2})
    with pytest.raises(SimulationConfigurationError, match="does not support scale_factor"):
        await create_task(request)
    request = request.model_copy(
        update={
            "scale_factor": 1,
            "trace_start_seconds": 1,
            "trace_end_seconds": None,
        }
    )
    with pytest.raises(SimulationConfigurationError, match="trace_end_seconds is required"):
        await create_task(request)


@pytest.mark.parametrize("backend", [AIPerfBackend()])
@pytest.mark.asyncio
async def test_backends_validate_error_rate_slo(simulation_root, backend):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend=backend.name, backend_options={"error_rate_slo": 101})
    with pytest.raises(
        SimulationConfigurationError,
        match="error_rate_slo must be a percentage from 0 to 100",
    ):
        await backend.validate(task)


@pytest.mark.asyncio
async def test_persistence_listing_recovery_and_safe_deletion(simulation_root):
    completed = task_record(simulation_root, "11111111")
    completed.created_at = "2026-01-01T00:00:00.000Z"
    interrupted = task_record(simulation_root, "22222222", "running")
    interrupted.created_at = "2026-01-02T00:00:00.000Z"
    interrupted.model_name = "other-model"
    interrupted.endpoint_url = "http://inference.example/v1"
    update_dataset(interrupted, name="other-dataset")
    await asyncio.gather(save_task(completed), save_task(interrupted))
    listed = await list_tasks(limit=100)
    assert isinstance(listed, SimulationTaskListPage)
    assert [task.id for task in listed.tasks] == ["22222222", "11111111"]
    assert listed.tasks[0].logs == []
    assert listed.facets["models"] == ["model", "other-model"]
    second_page = await list_tasks(offset=1, limit=1)
    assert second_page.offset == 1
    assert second_page.limit == 1
    assert [task.id for task in second_page.tasks] == ["11111111"]
    assert (await list_tasks(model="other-model", limit=1)).total == 1
    assert (await list_tasks(dataset="other-dataset", limit=1)).total == 1
    assert (await list_tasks(endpoint="INFERENCE.EXAMPLE", limit=1)).total == 1
    assert (await list_tasks(created_after="2026-01-01T12:00:00+00:00", limit=1)).total == 1
    recovered = await load_task("22222222")
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.progress_message == "Simulation interrupted by service restart"
    await delete_task("11111111")
    assert not Path(completed.task_dir).exists()
    assert await load_task("11111111") is None


def test_trace_path_and_format_validation(simulation_root):
    trace = simulation_root / "datasets" / "valid.jsonl"
    trace.write_text(
        '{"timestamp": 1, "input_length": 2, "output_length": 3}\n',
        encoding="utf-8",
    )
    MooncakeTrace(trace).validate()
    assert isinstance(trace_registry.resolve(trace.name, "mooncake_trace"), MooncakeTrace)
    assert isinstance(trace_registry.resolve(trace.name, "bailian_trace"), BailianTrace)
    assert isinstance(trace_registry.resolve(trace.name, "baseten_trace"), BasetenTrace)
    assert isinstance(trace_registry.resolve(trace.name, "burst_gpt_trace"), BurstGPTTrace)
    assert isinstance(
        trace_registry.resolve("semianalysis_cc_traces_weka_no_subagents", "weka_public_dataset"),
        WekaPublicDatasetTrace,
    )
    outside = simulation_root / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SimulationConfigurationError, match="inside"):
        trace_registry.resolve("../outside.jsonl", "mooncake_trace")
    lfs = simulation_root / "datasets" / "lfs.jsonl"
    lfs.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Git LFS pointer"):
        MooncakeTrace(lfs).validate()


def test_timeline_index(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000, 1500, 2000]
        )
        + "\n",
        encoding="utf-8",
    )
    timeline = MooncakeTrace(trace).timeline(3)
    assert timeline["duration_seconds"] == 2
    assert timeline["request_count"] == 5
    assert sum(item["request_count"] for item in timeline["bins"]) == 5

    with TestClient(app) as client:
        response = client.get("/api/simulation/trace-datasets/mooncake-arxiv/timeline?bins=50")
    assert response.status_code == 200
    assert response.json()["duration_seconds"] == 2


@pytest.mark.asyncio
async def test_baseten_dataset_uses_native_range_with_replay_speedup(simulation_root):
    result = await BaseTrace.download("baseten-synthetic")
    source = Path(result["path"])
    index = BasetenTrace(source).build_timeline_index()
    assert index["request_count"] == 600
    assert index["duration_seconds"] == 599

    task = task_record(simulation_root, "1234abcd")
    update_trace(
        task,
        path=source.name,
        format="baseten_trace",
        start_seconds=10,
        end_seconds=20,
        synthesis_speedup_ratio=2,
    )
    command = await AIPerfBackend().command(task)
    assert isinstance(command, BackendCommand)
    args = command.args
    assert args[args.index("--custom-dataset-type") + 1] == "baseten_trace"
    assert args[args.index("--replay-speedup") + 1] == "2.0"
    assert "--synthesis-speedup-ratio" not in args
    assert Path(args[args.index("--input-file") + 1]) == source
    assert args[args.index("--fixed-schedule-start-offset") + 1] == "10000"
    assert args[args.index("--fixed-schedule-end-offset") + 1] == "20000"

    update_trace(task, synthesis_speedup_ratio=1)
    native_command = await AIPerfBackend().command(task)
    native_args = native_command.args
    assert Path(native_args[native_args.index("--input-file") + 1]) == source
    assert native_args[native_args.index("--fixed-schedule-start-offset") + 1] == "10000"
    assert native_args[native_args.index("--fixed-schedule-end-offset") + 1] == "20000"


@pytest.mark.asyncio
async def test_aiperf_command_passes_api_key_and_redacts_it_in_logs(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        json.dumps({"timestamp": 0, "input_length": 2, "output_length": 3, "hash_ids": []}) + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=trace.name)
    task.api_key = "lens-mk-super-secret"
    # The run-only token must never be serialized (persisted or returned by the API).
    assert "api_key" not in task.model_dump()
    args = (await AIPerfBackend().command(task)).args
    assert args[args.index("--api-key") + 1] == "lens-mk-super-secret"
    logged = _command_for_log("aiperf", args)
    assert "lens-mk-super-secret" not in logged
    assert "<redacted>" in logged


@pytest.mark.asyncio
async def test_aiperf_weka_public_dataset_command(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.scenario = "coding"
    update_dataset(task, name="weka-claude-code", scenario="coding")
    update_trace(
        task,
        path="semianalysis_cc_traces_weka_no_subagents",
        format="weka_public_dataset",
        fixed_schedule=False,
    )
    update_simulation(
        task,
        backend_options={
            "random_seed": 42,
            "num_profile_runs": 2,
            "profile_run_cooldown_seconds": 3,
            "concurrency": 4,
            "max_context_length": 262144,
            "trace_idle_gap_cap_seconds": 10,
            "ttft_slo": 0.5,
            "tpot_slo": 0.05,
            "error_rate_slo": 1.5,
        },
    )
    backend = AIPerfBackend()
    await backend.validate(task)
    args = (await backend.command(task)).args
    assert args[args.index("--public-dataset") + 1] == ("semianalysis_cc_traces_weka_no_subagents")
    assert args[args.index("--concurrency") + 1] == "4"
    assert args[args.index("--max-context-length") + 1] == "262144"
    assert args[args.index("--trace-idle-gap-cap-seconds") + 1] == "10"
    assert args[args.index("--num-profile-runs") + 1] == "2"
    assert args[args.index("--profile-run-cooldown-seconds") + 1] == "3"
    assert args[args.index("--random-seed") + 1] == "42"
    assert "--ttft-slo" not in args
    assert "--tpot-slo" not in args
    assert "--error-rate-slo" not in args
    assert "--input-file" not in args
    assert "--fixed-schedule" not in args


@pytest.mark.asyncio
async def test_burstgpt_filter_timeline_and_aiperf_command(simulation_root):
    source = simulation_root / "burstgpt.csv"
    source.write_text(
        "Timestamp,Model,Request tokens,Response tokens,Total tokens,Log Type\n"
        "5,ChatGPT,100,20,120,Conversation log\n"
        "7,ChatGPT,200,30,230,API log\n"
        "15,ChatGPT,300,40,340,Conversation log\n",
        encoding="utf-8",
    )
    selected = simulation_root / "datasets" / "burstgpt_conversation.csv"
    trace_registry.get_variant("burstgpt-conversation").transform_download(
        source,
        selected,
    )
    trace = BurstGPTTrace(selected)
    trace.validate()
    index = trace.build_timeline_index()
    assert index["request_count"] == 2
    assert index["duration_seconds"] == 10
    assert "API log" not in selected.read_text(encoding="utf-8")

    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=selected.name, format="burst_gpt_trace")
    backend = AIPerfBackend()
    await backend.validate(task)
    args = (await backend.command(task)).args
    assert args[args.index("--custom-dataset-type") + 1] == "burst_gpt_trace"
    assert args[args.index("--input-file") + 1] == str(selected)


@pytest.mark.asyncio
async def test_task_range_is_persisted_and_drives_duration(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 1000, 2000, 3000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = await create_task(
        SimulationTaskCreateRequest(
            name="range",
            description="",
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="model",
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
            duration_seconds=99,
            scale_factor=1,
            trace_start_seconds=0.5,
            trace_end_seconds=2.5,
            trace_timeout_seconds=3600,
            fixed_schedule=True,
            stream=True,
            backend_options={},
        )
    )
    task.status = "cancelled"
    assert task.simulation.duration_seconds == 2
    assert task.prompt.trace.start_seconds == 0.5
    assert task.prompt.trace.end_seconds == 2.5


@pytest.mark.asyncio
async def test_aiperf_command_uses_native_range_offsets(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000, 1500, 2000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_simulation(
        task,
        backend_options={
            "num_profile_runs": 1,
            "profile_run_cooldown_seconds": 0,
        },
    )
    update_trace(task, path=trace.name, start_seconds=0.5, end_seconds=1.5)
    args = (await AIPerfBackend().command(task)).args
    assert Path(args[args.index("--input-file") + 1]) == trace
    assert args[args.index("--fixed-schedule-start-offset") + 1] == "500"
    assert args[args.index("--fixed-schedule-end-offset") + 1] == "1500"
    assert not (Path(task.task_dir) / "artifacts" / "aiperf").exists()
    assert "--profile-run-cooldown-seconds" not in args


def _write_aiperf_file_trace(simulation_root: Path, trace_format: str) -> Path:
    datasets = simulation_root / "datasets"
    if trace_format == "baseten_trace":
        target = datasets / "baseten_synthetic_trace.parquet"
        BasetenTrace.generate(target)
        return target
    if trace_format == "burst_gpt_trace":
        target = datasets / "burstgpt_conversation.csv"
        target.write_text(
            "Timestamp,Model,Request tokens,Response tokens,Log Type\n"
            "0,ChatGPT,100,20,Conversation log\n"
            "2,ChatGPT,120,30,Conversation log\n",
            encoding="utf-8",
        )
        return target
    target = datasets / f"{trace_format}.jsonl"
    target.write_text(
        "\n".join(
            json.dumps({"timestamp": ts, "input_length": 2, "output_length": 3, "hash_ids": []})
            for ts in (0, 1000, 2000)
        )
        + "\n",
        encoding="utf-8",
    )
    return target


@pytest.mark.parametrize(
    ("trace_format", "expected_endpoint"),
    [
        ("mooncake_trace", "chat"),
        ("bailian_trace", "chat"),
        ("baseten_trace", "completions"),
        ("burst_gpt_trace", "chat"),
    ],
)
@pytest.mark.asyncio
async def test_aiperf_concurrency_mode_file_trace_formats(
    simulation_root,
    trace_format,
    expected_endpoint,
):
    source = _write_aiperf_file_trace(simulation_root, trace_format)
    backend = AIPerfBackend()

    # 1) fixed_schedule=False with explicit concurrency overrides fixed schedule & offsets.
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=source.name, format=trace_format, fixed_schedule=False, start_seconds=0.5, end_seconds=1.5)
    update_simulation(task, backend_options={"concurrency": 8})
    await backend.validate(task)
    assert backend.expected_request_count(task) is None
    assert backend.arrival_schedule_seconds(task) == []
    args = (await backend.command(task)).args
    assert args[args.index("--endpoint-type") + 1] == expected_endpoint
    assert Path(args[args.index("--input-file") + 1]) == source
    assert args[args.index("--custom-dataset-type") + 1] == trace_format
    assert args[args.index("--concurrency") + 1] == "8"
    for omitted in (
        "--fixed-schedule",
        "--synthesis-speedup-ratio",
        "--replay-speedup",
        "--fixed-schedule-start-offset",
        "--fixed-schedule-end-offset",
    ):
        assert omitted not in args

    # 2) fixed_schedule=False with omitted concurrency defaults to --concurrency 1.
    update_simulation(task, backend_options={})
    await backend.validate(task)
    assert backend.expected_request_count(task) is None
    assert backend.arrival_schedule_seconds(task) == []
    default_args = (await backend.command(task)).args
    assert default_args[default_args.index("--concurrency") + 1] == "1"
    assert "--fixed-schedule" not in default_args

    # 3) Explicit concurrency with fixed_schedule=True switches to concurrency-slot mode.
    update_trace(task, fixed_schedule=True)
    update_simulation(task, backend_options={"concurrency": 4})
    await backend.validate(task)
    assert backend.expected_request_count(task) is None
    assert backend.arrival_schedule_seconds(task) == []
    override_args = (await backend.command(task)).args
    assert override_args[override_args.index("--concurrency") + 1] == "4"
    assert "--fixed-schedule" not in override_args
    assert "--fixed-schedule-start-offset" not in override_args
    assert "--fixed-schedule-end-offset" not in override_args


@pytest.mark.asyncio
async def test_aiperf_weka_defaults_concurrency_to_one_when_omitted(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.scenario = "coding"
    update_dataset(task, name="weka-claude-code", scenario="coding")
    update_trace(
        task,
        path="semianalysis_cc_traces_weka_no_subagents",
        format="weka_public_dataset",
        fixed_schedule=False,
    )
    update_simulation(task, backend_options={})
    backend = AIPerfBackend()
    await backend.validate(task)
    args = (await backend.command(task)).args
    assert args[args.index("--public-dataset") + 1] == "semianalysis_cc_traces_weka_no_subagents"
    assert args[args.index("--concurrency") + 1] == "1"
    assert "--fixed-schedule" not in args


@pytest.mark.asyncio
async def test_aiperf_fixed_schedule_mode_preserved_when_concurrency_omitted(simulation_root):
    backend = AIPerfBackend()
    mooncake = _write_aiperf_file_trace(simulation_root, "mooncake_trace")
    task = task_record(simulation_root, "1234abcd")
    update_trace(
        task,
        path=mooncake.name,
        format="mooncake_trace",
        fixed_schedule=True,
        start_seconds=0.5,
        end_seconds=1.5,
        synthesis_speedup_ratio=1,
    )
    update_simulation(task, backend_options={})
    await backend.validate(task)
    assert backend.expected_request_count(task) == 1
    assert backend.arrival_schedule_seconds(task) == [0.5]
    args = (await backend.command(task)).args
    assert "--fixed-schedule" in args
    assert "--concurrency" not in args
    assert args[args.index("--synthesis-speedup-ratio") + 1] == "1.0"
    assert args[args.index("--fixed-schedule-start-offset") + 1] == "500"
    assert args[args.index("--fixed-schedule-end-offset") + 1] == "1500"

    update_trace(task, synthesis_speedup_ratio=2)
    with pytest.raises(SimulationConfigurationError, match="synthesis_speedup_ratio"):
        await backend.validate(task)


@pytest.mark.parametrize("invalid_concurrency", [0, -1, "abc", True, 1.5, None])
@pytest.mark.parametrize("trace_format", ["mooncake_trace", "baseten_trace", "weka_public_dataset"])
@pytest.mark.asyncio
async def test_aiperf_rejects_invalid_concurrency(simulation_root, trace_format, invalid_concurrency):
    task = task_record(simulation_root, "1234abcd")
    if trace_format == "weka_public_dataset":
        update_trace(
            task,
            path="semianalysis_cc_traces_weka_no_subagents",
            format=trace_format,
            fixed_schedule=False,
        )
    else:
        source = _write_aiperf_file_trace(simulation_root, trace_format)
        update_trace(task, path=source.name, format=trace_format, fixed_schedule=False)
    update_simulation(task, backend_options={"concurrency": invalid_concurrency})
    with pytest.raises(SimulationConfigurationError, match="concurrency must be an integer of at least 1"):
        await AIPerfBackend().validate(task)


@pytest.mark.parametrize("option_name", ["max_context_length", "trace_idle_gap_cap_seconds"])
@pytest.mark.parametrize("trace_format", ["mooncake_trace", "baseten_trace"])
@pytest.mark.asyncio
async def test_aiperf_file_traces_still_reject_weka_only_options(simulation_root, trace_format, option_name):
    source = _write_aiperf_file_trace(simulation_root, trace_format)
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=source.name, format=trace_format, fixed_schedule=False)
    update_simulation(task, backend_options={"concurrency": 4, option_name: 100})
    with pytest.raises(SimulationConfigurationError, match="AIPerf options are not supported"):
        await AIPerfBackend().validate(task)


def test_backend_expected_request_counts_follow_end_boundary_semantics(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps({"timestamp": timestamp, "input_length": 8, "output_length": 2})
            for timestamp in (1000, 2000, 3000)
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=trace.name, start_seconds=0, end_seconds=2)

    update_simulation(task, backend="aiperf", backend_options={"num_profile_runs": 2})
    assert AIPerfBackend().expected_request_count(task) == 4


@pytest.mark.asyncio
async def test_process_cancellation_and_runner_cancellation_terminate_child(simulation_root):
    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda _percent, _message: None,
    )
    running = asyncio.create_task(
        execute_command(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            simulation_root / "cancel",
            60,
            context,
        )
    )
    await asyncio.sleep(0.1)
    context.cancel_event.set()
    with pytest.raises(SimulationCancelledError):
        await running
    assert context.process is None

    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda _percent, _message: None,
    )
    running = asyncio.create_task(
        execute_command(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            simulation_root / "runner-cancel",
            60,
            context,
        )
    )
    await asyncio.sleep(0.1)
    process = context.process
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert process is not None and process.returncode is not None


@pytest.mark.asyncio
async def test_process_reports_progress_without_expected_request_count(simulation_root):
    updates = []
    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda percent, message: updates.append((percent, message)),
    )

    await execute_command(
        sys.executable,
        ["-c", "import time; time.sleep(0.1)"],
        simulation_root / "unknown-request-count",
        10,
        context,
        progress_request_counts=lambda: (7, None),
    )

    assert updates
    assert updates[0][0] == 0
    assert "7 requests completed" in updates[0][1]


@pytest.mark.asyncio
async def test_aiperf_parser_preserves_streaming_metrics_and_ignores_warmup(simulation_root):
    task = task_record(simulation_root, "a1e0f001")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "request_count": {"unit": "requests", "avg": 1},
                "error_request_count": {"unit": "requests", "avg": 1},
                "benchmark_duration": {"unit": "sec", "avg": 2},
                "request_throughput": {"unit": "requests/sec", "avg": 1},
                "output_token_throughput": {"unit": "tokens/sec", "avg": 5},
                "input_token_throughput": {"unit": "tokens/sec", "avg": 10},
                "total_token_throughput": {"unit": "tokens/sec", "avg": 15},
                "request_error_rate": {"unit": "%", "avg": 50},
                "effective_concurrency": {"unit": "requests", "avg": 1.5},
                "total_isl": {"unit": "tokens", "avg": 20},
                "total_osl": {"unit": "tokens", "avg": 10},
                "request_latency": {"unit": "ms", "avg": 120, "p50": 110, "count": 1},
                "time_to_first_token": {
                    "unit": "ms",
                    "avg": 30,
                    "p50": 28,
                    "p99": 40,
                    "count": 1,
                },
                "inter_token_latency": {
                    "unit": "ms",
                    "avg": 8,
                    "p50": 7,
                    "p99": 11,
                    "count": 1,
                },
                "replay_sched_lag_p50": {"unit": "ms", "avg": 2},
                "replay_sched_lag_p90": {"unit": "ms", "avg": 4},
                "replay_sched_lag_p99": {"unit": "ms", "avg": 6},
            }
        ),
        encoding="utf-8",
    )
    records = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 1_120_000_000,
            },
            "metrics": {
                "request_latency": {"value": 120, "unit": "ms"},
                "time_to_first_token": {"value": 30, "unit": "ms"},
                "inter_token_latency": {"value": 8, "unit": "ms"},
                "input_sequence_length": {"value": 20, "unit": "tokens"},
                "output_sequence_length": {"value": 10, "unit": "tokens"},
                "osl_mismatch_diff_pct": {"value": -90, "unit": "%"},
            },
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 2_000_000_000,
                "request_end_ns": 2_050_000_000,
            },
            "metrics": {},
            "error": {"code": 503, "message": "unavailable"},
        },
        {
            "metadata": {
                "benchmark_phase": "warmup",
                "request_start_ns": 500_000_000,
                "request_end_ns": 600_000_000,
            },
            "metrics": {
                "request_latency": {"value": 100, "unit": "ms"},
                "input_sequence_length": {"value": 999, "unit": "tokens"},
                "output_sequence_length": {"value": 999, "unit": "tokens"},
            },
        },
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    now = datetime.now(UTC)
    result = await AIPerfBackend().parse(
        task,
        CommandResult(0, now, now, stdout, stderr),
        "0.12.0",
    )
    assert isinstance(result, SimulationResult)
    assert all(isinstance(artifact, SimulationArtifact) for artifact in result.artifacts)
    summary = result.summary
    assert summary["total_requests"] == 2
    assert summary["successful_requests"] == 1
    assert summary["failed_requests"] == 1
    assert summary["total_input_tokens"] == 20
    assert summary["total_output_tokens"] == 10
    assert summary["ttft"]["mean_ms"] == 30
    assert summary["ttft"]["count"] == 1
    assert summary["tpot"]["p99_ms"] == 11
    assert summary["duration_seconds"] == pytest.approx(1.05)
    assert summary["throughput_rps"] == pytest.approx(2 / 1.05)
    assert summary["throughput_tps"] == pytest.approx(10 / 1.05)
    assert summary["input_throughput_tps"] == pytest.approx(20 / 1.05)
    assert summary["total_throughput_tps"] == pytest.approx(30 / 1.05)
    assert summary["effective_concurrency"] == 1.5
    assert summary["drift"] == {"p50_ms": 2, "p90_ms": 4, "p99_ms": 6}
    assert sum(item["arrived_requests"] for item in summary["completion_timeline"]) == 2
    assert summary["tpot_heatmap"]["sequence_length_source"] == "requested"
    assert summary["tpot_heatmap"]["cells"][0]["sequence_length_start"] == 100
    assert summary["tpot_heatmap"]["cells"][0]["sequence_length_end"] == 101
    assert result.warnings == []


@pytest.mark.asyncio
async def test_aiperf_parser_normalizes_wall_clock_diluted_throughput(simulation_root):
    task = task_record(simulation_root, "a1e0f009")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    # Model a run where AIPerf raw throughput divided by 240s wall-clock wait time
    # even though request execution spanned ~135.5s.
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "request_count": {"unit": "requests", "avg": 121},
                "error_request_count": {"unit": "requests", "avg": 2},
                "benchmark_duration": {"unit": "sec", "avg": 135.517},
                "request_throughput": {"unit": "requests/sec", "avg": 0.5042},
                "output_token_throughput": {"unit": "tokens/sec", "avg": 218.60},
                "input_token_throughput": {"unit": "tokens/sec", "avg": 3569.17},
                "total_token_throughput": {"unit": "tokens/sec", "avg": 3787.77},
                "total_isl": {"unit": "tokens", "avg": 856606},
                "total_osl": {"unit": "tokens", "avg": 52464},
                "request_latency": {"unit": "ms", "avg": 13293.4, "p50": 9133.5, "count": 121},
                "time_to_first_token": {"unit": "ms", "avg": 380.5, "p50": 206.2, "count": 121},
                "inter_token_latency": {"unit": "ms", "avg": 30.6, "p50": 28.1, "count": 121},
            }
        ),
        encoding="utf-8",
    )
    records = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 10_000_000_000,
                "request_end_ns": 20_000_000_000,
            },
            "metrics": {
                "request_latency": {"value": 10_000, "unit": "ms"},
                "time_to_first_token": {"value": 200, "unit": "ms"},
                "inter_token_latency": {"value": 28, "unit": "ms"},
                "input_sequence_length": {"value": 856606, "unit": "tokens"},
                "output_sequence_length": {"value": 52464, "unit": "tokens"},
            },
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 140_000_000_000,
                "request_end_ns": 145_528_000_000,
            },
            "metrics": {},
            "error": {"code": 503, "message": "unavailable"},
        },
    ]
    jsonl_path = artifacts / "profile_export.jsonl"
    jsonl_path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 0, 4, 0, tzinfo=UTC)
    result = await AIPerfBackend().parse(
        task,
        CommandResult(0, started, completed, stdout, stderr),
        "0.12.0",
    )
    summary = result.summary
    assert summary["duration_seconds"] == pytest.approx(135.528)
    assert summary["throughput_rps"] == pytest.approx(123 / 135.528)
    assert summary["throughput_tps"] == pytest.approx(52464 / 135.528)
    assert summary["input_throughput_tps"] == pytest.approx(856606 / 135.528)
    assert summary["total_throughput_tps"] == pytest.approx((856606 + 52464) / 135.528)

    # Without per-request JSONL, fall back to benchmark_duration rather than 240s wall_time.
    jsonl_path.unlink()
    fallback = await AIPerfBackend().parse(
        task,
        CommandResult(0, started, completed, stdout, stderr),
        "0.12.0",
    )
    assert fallback.summary["duration_seconds"] == pytest.approx(135.517)
    assert fallback.summary["throughput_rps"] == pytest.approx(123 / 135.517)
    assert fallback.summary["throughput_tps"] == pytest.approx(52464 / 135.517)


@pytest.mark.asyncio
async def test_aiperf_parser_multi_run_and_missing_record_fallbacks(simulation_root):
    # 1. num_profile_runs > 1 prefers per-run benchmark_duration over multi-run JSONL span
    # and normalizes JSONL-only counts/tokens by num_profile_runs when raw omits totals.
    task = task_record(simulation_root, "a1e0f010")
    update_simulation(task, backend="aiperf", backend_options={"num_profile_runs": 2})
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "benchmark_duration": {"unit": "sec", "avg": 10.0},
                "request_throughput": {"unit": "requests/sec", "avg": 0.1},
                "output_token_throughput": {"unit": "tokens/sec", "avg": 1.0},
            }
        ),
        encoding="utf-8",
    )
    multi_records = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 0,
                "request_end_ns": 10_000_000_000,
            },
            "metrics": {
                "input_sequence_length": {"value": 40, "unit": "tokens"},
                "output_sequence_length": {"value": 20, "unit": "tokens"},
            },
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 60_000_000_000,
                "request_end_ns": 70_000_000_000,
            },
            "metrics": {
                "input_sequence_length": {"value": 60, "unit": "tokens"},
                "output_sequence_length": {"value": 30, "unit": "tokens"},
            },
        },
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(record)}\n" for record in multi_records),
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    multi_res = await AIPerfBackend().parse(task, CommandResult(0, now, now, stdout, stderr), "0.12.0")
    assert multi_res.summary["duration_seconds"] == pytest.approx(10.0)
    assert multi_res.summary["throughput_rps"] == pytest.approx(1.0 / 10.0)
    assert multi_res.summary["throughput_tps"] == pytest.approx(25.0 / 10.0)
    assert multi_res.summary["input_throughput_tps"] == pytest.approx(50.0 / 10.0)
    assert multi_res.summary["total_throughput_tps"] == pytest.approx(75.0 / 10.0)

    # 2. Empty profiling JSONL and zero-duration fallback preserve raw throughput values
    task_empty = task_record(simulation_root, "a1e0f011")
    empty_artifacts = Path(task_empty.task_dir) / "artifacts" / "aiperf"
    empty_artifacts.mkdir(parents=True)
    e_stdout = empty_artifacts / "stdout.log"
    e_stderr = empty_artifacts / "stderr.log"
    e_stdout.write_text("", encoding="utf-8")
    e_stderr.write_text("", encoding="utf-8")
    (empty_artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "benchmark_duration": {"unit": "sec", "avg": 0.0},
                "request_throughput": {"unit": "requests/sec", "avg": 2.5},
                "output_token_throughput": {"unit": "tokens/sec", "avg": 50.0},
                "input_token_throughput": {"unit": "tokens/sec", "avg": 100.0},
                "total_token_throughput": {"unit": "tokens/sec", "avg": 150.0},
            }
        ),
        encoding="utf-8",
    )
    (empty_artifacts / "profile_export.jsonl").write_text(
        json.dumps({"metadata": {"benchmark_phase": "warmup"}, "metrics": {}}) + "\n",
        encoding="utf-8",
    )
    zero_res = await AIPerfBackend().parse(task_empty, CommandResult(0, now, now, e_stdout, e_stderr), "0.12.0")
    assert zero_res.summary["duration_seconds"] == 0.0
    assert zero_res.summary["throughput_rps"] == pytest.approx(2.5)
    assert zero_res.summary["throughput_tps"] == pytest.approx(50.0)
    assert zero_res.summary["input_throughput_tps"] == pytest.approx(100.0)
    assert zero_res.summary["total_throughput_tps"] == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_aiperf_parser_reports_non_streaming_metrics_as_unavailable(simulation_root):
    task = task_record(simulation_root, "a1e0f002")
    update_simulation(task, stream=False)
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "request_count": {"unit": "requests", "avg": 7},
                "error_request_count": {"unit": "requests", "avg": 2},
                "total_isl": {"unit": "tokens", "avg": 70},
                "total_osl": {"unit": "tokens", "avg": 21},
                "request_latency": {"unit": "ms", "avg": 100},
            }
        ),
        encoding="utf-8",
    )

    now = datetime.now(UTC)
    result = await AIPerfBackend().parse(
        task,
        CommandResult(0, now, now, stdout, stderr),
        "0.12.0",
    )
    summary = result.summary
    assert summary["total_requests"] == 9
    assert summary["successful_requests"] == 7
    assert summary["failed_requests"] == 2
    assert summary["total_input_tokens"] == 70
    assert summary["total_output_tokens"] == 21
    assert summary["ttft"] is None
    assert summary["tpot"] is None
    assert "non-streaming responses" in result.warnings[0]


def test_aiperf_timeout_record_is_reported_as_failure(simulation_root):
    task = task_record(simulation_root, "a1e0f003")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    (artifacts / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "benchmark_phase": "profiling",
                    "request_start_ns": 1_000_000_000,
                    "request_end_ns": 2_000_000_000,
                },
                "metrics": {},
                "timeout": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = AIPerfBackend().artifact_records(task)

    assert len(records) == 1
    assert records[0].successful is False
    assert records[0].issue_failed is True
    assert records[0].error == "Request timed out"


def test_live_completion_timeline_ignores_partial_record(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    (artifacts / "profile_export.jsonl").write_text(
        '{"metadata":{"benchmark_phase":"profiling","request_start_ns":100000000,"request_end_ns":500000000,"status_code":200},"metrics":{}}\n'
        '{"metadata":{"benchmark_phase":"profiling","request_start_ns":750000000',
        encoding="utf-8",
    )

    timeline = live_completion_timeline_from_artifacts(task)

    assert sum(point["arrived_requests"] for point in timeline) == 1
    assert sum(point["completed_requests"] for point in timeline) == 1


def test_live_summary_reports_current_metrics(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    lines = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 0,
                "request_end_ns": 1_000_000_000,
                "status_code": 200,
            },
            "metrics": {
                "input_sequence_length": {"value": 128},
                "output_sequence_length": {"value": 20},
                "time_to_first_token": {"value": 100},
                "inter_token_latency": {"value": 10},
            },
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 2_000_000_000,
                "status_code": 200,
            },
            "metrics": {
                "input_sequence_length": {"value": 128},
                "output_sequence_length": {"value": 40},
                "time_to_first_token": {"value": 300},
                "inter_token_latency": {"value": 30},
            },
        },
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(line)}\n" for line in lines),
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["throughput_rps"] == 1
    assert summary["throughput_tps"] == 30
    assert summary["total_requests"] == 2
    assert summary["successful_requests"] == 2
    assert summary["failed_requests"] == 0
    assert summary["success_rate"] == 100
    assert summary["ttft"]["mean_ms"] == 200
    assert summary["tpot"]["mean_ms"] == 20


def test_aiperf_live_summary_counts_partial_error_output_tokens(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    (artifacts / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "benchmark_phase": "profiling",
                    "request_start_ns": 1_000_000_000,
                    "request_end_ns": 2_000_000_000,
                },
                "metrics": {"output_sequence_length": {"value": 7}},
                "error": {"code": 500, "message": "partial response"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["failed_requests"] == 1
    assert summary["throughput_tps"] == 7


def test_live_summary_reports_error_timeline(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    lines = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 0,
                "request_end_ns": 1_000_000_000,
                "status_code": 200,
            },
            "metrics": {"output_sequence_length": {"value": 20}},
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 2_000_000_000,
            },
            "metrics": {"output_sequence_length": {"value": 0}},
            "error": {"code": 429, "message": "rate limit"},
        },
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(line)}\n" for line in lines),
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["throughput_timeline"]
    assert sum(point["failed_requests"] for point in summary["error_timeline"]) == 1
    assert summary["error_timeline"][-1]["cumulative_failures"] == 1
    assert summary["error_timeline"][-1]["error_rate_percent"] == 50


def test_status_code_breakdown_supports_arbitrary_codes(simulation_root):
    task = task_record(simulation_root, "1234abcd", "completed")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    lines = [
        {"metadata": {"benchmark_phase": "profiling", "status_code": 200}, "metrics": {}},
        {"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": {"code": 401}},
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 1_250_000_000,
            },
            "metrics": {"input_sequence_length": {"value": 12}, "output_sequence_length": {"value": 0}},
            "error": {"code": 418, "message": "teapot rejected request"},
        },
        {"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": {"code": 429}},
        {"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": {"code": 500}},
        {"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": {"code": 503}},
        {"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": "connection reset"},
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(line)}\n" for line in lines),
        encoding="utf-8",
    )

    breakdown = status_code_breakdown_from_artifacts(task)
    records = AIPerfBackend().artifact_records(task)

    assert all(isinstance(record, ArtifactRecord) for record in records)
    assert records[2].status_code == 418
    assert records[2].error == "teapot rejected request"
    assert [item["status_code"] for item in breakdown] == [401, 418, 429, 500, 503, None]
    assert all(item["count"] == 1 for item in breakdown)
    assert sum(item["percentage"] for item in breakdown) == pytest.approx(100)
    issues = response_code_issues_from_artifacts(task, 418)
    assert issues.total == 1
    assert issues.items[0].model_dump(mode="json") == {
        "request_number": 3,
        "request_id": None,
        "status_code": 418,
        "error": "teapot rejected request",
        "started_at_seconds": 1,
        "latency_ms": 250,
        "input_tokens": 12,
        "output_tokens": 0,
    }
    assert response_code_issues_from_artifacts(task, 418, offset=1, limit=1).items == []

    page = SimulationResponseCodeIssuePage(
        task_id=task.id,
        status_code=418,
        items=issues.items,
        total=issues.total,
        offset=0,
        limit=100,
    )
    assert page.model_dump(mode="json", by_alias=True)["issues"][0]["status_code"] == 418

    asyncio.run(save_task(task))
    with TestClient(app) as client:
        response = client.get(f"/api/simulation/tasks/{task.id}/response-code-issues?status_code=418")
    assert response.status_code == 200
    assert response.json() == {
        "task_id": task.id,
        "status_code": 418,
        "issues": [
            {
                "request_number": 3,
                "request_id": None,
                "status_code": 418,
                "error": "teapot rejected request",
                "started_at_seconds": 1.0,
                "latency_ms": 250.0,
                "input_tokens": 12.0,
                "output_tokens": 0.0,
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 100,
    }


def test_resolve_task_endpoint_url_uses_incluster_endpoint_for_deployment_mode(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://vllm.my-namespace.svc:8000",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
    )
    monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda execution_id: context)

    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )

    assert resolved.url == "http://vllm.my-namespace.svc:8000"
    assert resolved.namespace == "my-namespace"
    assert resolved.cluster_id == "cluster-123"
    assert resolved.deployment_name == "qwen3-0.6b"


def test_resolve_task_endpoint_url_prefers_cluster_gateway(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://optimized-baseline-epp.my-namespace.svc:80",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
        uses_shared_gateway=True,
    )
    monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda execution_id: context)

    async def gateway(_cluster_id):
        return "http://10.0.0.5:30012"

    monkeypatch.setattr(simulation_service, "_cluster_gateway_endpoint", gateway)

    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )

    assert resolved.url == "http://10.0.0.5:30012"

    context.uses_shared_gateway = False
    direct = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )
    assert direct.url == "http://optimized-baseline-epp.my-namespace.svc:80"


def test_resolve_task_endpoint_url_rejects_deployment_without_endpoint(monkeypatch):
    import types

    context = types.SimpleNamespace(endpoint=None, namespace="my-namespace", cluster_id=None, display_name=None)
    monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda execution_id: context)

    with pytest.raises(SimulationConfigurationError):
        asyncio.run(
            simulation_service._resolve_task_endpoint_url(
                endpoint_mode="deployment",
                endpoint_url="http://127.0.0.1:18042",
                endpoint_deployment_execution_id="exec-1",
            )
        )


def test_resolve_task_endpoint_url_rejects_incompatible_deployment_guide(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://pd-disaggregation-epp.my-namespace.svc:80",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="test1",
        guide="pd-disaggregation",
    )
    monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda execution_id: context)

    with pytest.raises(SimulationConfigurationError, match="pd-disaggregation"):
        asyncio.run(
            simulation_service._resolve_task_endpoint_url(
                endpoint_mode="deployment",
                endpoint_url="http://127.0.0.1:18042",
                endpoint_deployment_execution_id="exec-1",
                backend_name="aiperf",
                backend_capabilities={"incompatible_deployment_guides": ["pd-disaggregation"]},
            )
        )

    # A guide not on the backend's incompatibility list is unaffected.
    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
            backend_name="aiperf",
            backend_capabilities={"incompatible_deployment_guides": []},
        )
    )
    assert resolved.url == "http://pd-disaggregation-epp.my-namespace.svc:80"


def test_resolve_incluster_target_only_applies_to_deployment_mode_with_namespace():
    task = task_record(Path("/tmp"), "1234abcd")
    assert resolve_incluster_target(task) is None

    task.endpoint_mode = "deployment"
    assert resolve_incluster_target(task) is None  # no namespace resolved yet

    task.endpoint_namespace = "my-namespace"
    task.endpoint_cluster_id = "cluster-123"
    assert resolve_incluster_target(task) == ("my-namespace", "cluster-123")


def test_incluster_image_defaults_aiperf_to_official_image(monkeypatch):
    from llm_d_lens.simulation import incluster

    monkeypatch.delenv("SIMULATION_INCLUSTER_IMAGE", raising=False)
    monkeypatch.delenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", raising=False)
    assert incluster.incluster_image("aiperf") == "nvcr.io/nvidia/ai-dynamo/aiperf:0.14.0"
    assert incluster.incluster_image("other") == "python:3.12-slim"
    assert incluster.incluster_image(None) == "python:3.12-slim"


def test_incluster_image_aiperf_specific_override(monkeypatch):
    from llm_d_lens.simulation import incluster

    monkeypatch.delenv("SIMULATION_INCLUSTER_IMAGE", raising=False)
    monkeypatch.setenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", "example.com/custom/aiperf:test")
    assert incluster.incluster_image("aiperf") == "example.com/custom/aiperf:test"
    assert incluster.incluster_image("other") == "python:3.12-slim"


def test_incluster_image_global_override_wins_over_aiperf_default(monkeypatch):
    from llm_d_lens.simulation import incluster

    monkeypatch.setenv("SIMULATION_INCLUSTER_IMAGE", "example.com/custom/all:test")
    monkeypatch.setenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", "example.com/custom/aiperf:test")
    assert incluster.incluster_image("aiperf") == "example.com/custom/all:test"
    assert incluster.incluster_image("other") == "example.com/custom/all:test"


def test_pod_manifest_runs_as_root_for_arbitrary_path_staging():
    from llm_d_lens.simulation.incluster import _pod_manifest

    manifest = _pod_manifest("pod-name", "ns", "nvcr.io/nvidia/ai-dynamo/aiperf:0.14.0", 60)
    container = manifest["spec"]["containers"][0]
    assert container["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}


def test_pod_exec_command_aiperf_invokes_bare_name_without_copy():
    from llm_d_lens.simulation.incluster import _pod_exec_command

    command = _pod_exec_command("/home/user/.venv/bin/aiperf", ["profile"], executable_copied=False)
    assert "aiperf profile" in command
    # No self-install needed when it's already baked into the dedicated image,
    # but the fallback check must still be present for image overrides.
    assert "command -v aiperf" in command
    assert "aiperf==0.14.0" in command
    assert "/home/user/.venv/bin/aiperf" not in command


@pytest.mark.asyncio
async def test_command_backend_runs_in_pod_for_deployment_endpoint(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd")
    task.endpoint_mode = "deployment"
    task.endpoint_namespace = "my-namespace"
    task.endpoint_cluster_id = "cluster-123"
    backend = get_backend("aiperf")

    async def validate(_task):
        return None

    async def prepare(_context):
        return sys.executable

    async def command(_task):
        return BackendCommand(args=("--version",), timeout_seconds=10)

    async def parse(_task, _result, _version):
        return SimulationResult(
            run_id=_task.id,
            backend="aiperf",
            backend_version=_version,
            summary={},
            artifacts=[],
            per_request=[],
            backend_metrics={},
            warnings=[],
        )

    calls = {}

    async def fake_execute_command_in_pod(**kwargs):
        calls.update(kwargs)
        root = Path(task.task_dir)
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        return CommandResult(0, now, now, root / "stdout.log", root / "stderr.log")

    async def fail_execute_command(*_args, **_kwargs):
        raise AssertionError("Local execute_command should not run for deployment-mode tasks")

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(backend, "prepare_executable", prepare)
    monkeypatch.setattr(backend, "command", command)
    monkeypatch.setattr(backend, "parse", parse)
    monkeypatch.setattr(backend, "expected_request_count", lambda _task: 1)
    monkeypatch.setattr("llm_d_lens.simulation.backends.base.execute_command_in_pod", fake_execute_command_in_pod)
    monkeypatch.setattr("llm_d_lens.simulation.backends.base.execute_command", fail_execute_command)

    context = RunContext(asyncio.Event(), lambda *_args: None, lambda *_args: None)
    await backend.run(task, context)

    assert calls["namespace"] == "my-namespace"
    assert calls["cluster_id"] == "cluster-123"
    assert calls["task_dir"] == Path(task.task_dir)


def test_simulation_storage_uses_unified_roots(tmp_path, monkeypatch):
    from llm_d_lens.infrastructure.storage.paths import storage_path

    for name in (
        "SIMULATION_TASK_ROOT",
        "TRACE_REPLAY_DATA_DIR",
        "SIMULATION_BACKEND_CACHE_DIR",
        "SIMULATION_TOKENIZER_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    assert simulation_service.task_root() == tmp_path / "data/artifacts/simulations"
    assert BaseTrace.trace_root() == tmp_path / "data/datasets"
    assert storage_path("cache", "backends") == tmp_path / "cache/backends"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_terminal_simulation_registers_actual_files(simulation_root, status):
    import hashlib

    task = task_record(simulation_root, "aabbcc01", status)
    directory = Path(task.task_dir)
    payload = directory / "artifacts/aiperf/stdout.log"
    payload.parent.mkdir(parents=True)
    payload.write_text("real backend output\n", encoding="utf-8")
    await save_task(task)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["owner_type"] == "simulation"
    assert manifest["owner_id"] == task.id
    assert manifest["status"] == status
    record = next(item for item in manifest["files"] if item["path"] == "artifacts/aiperf/stdout.log")
    assert record["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert record["uri"] == f"lens-artifact://simulation/{task.id}/artifacts/aiperf/stdout.log"
    assert task.artifact_uri == f"lens-artifact://simulation/{task.id}/"
    assert manifest["source_version"]["backend"] == "aiperf"
    assert manifest["source_version"]["dataset"] == "mooncake-arxiv"


@pytest.mark.asyncio
async def test_dataset_download_has_distinct_manifest_and_provenance(simulation_root):
    result = await BaseTrace.download("baseten-synthetic")
    source = Path(result["path"])
    manifest_path = source.with_name(source.name + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["owner_type"] == "dataset"
    assert manifest["owner_id"] == "baseten-synthetic"
    assert {item["path"] for item in manifest["files"]} == {
        source.name,
        source.name + ".metadata.json",
        BasetenTrace(source).timeline_index_path.name,
    }
    assert not (source.parent / "manifest.json").exists()
    assert result["artifact_uri"] == f"lens-artifact://dataset/baseten-synthetic/{source.name}"
    assert manifest["source_version"]["source"] == "generated"


@pytest.mark.asyncio
async def test_simulation_failure_retains_log_truncation_and_configuration_ids(simulation_root, monkeypatch):
    from types import SimpleNamespace

    task = task_record(simulation_root, "aabbcc02", "queued")
    monkeypatch.setenv("SIMULATION_MAX_TASK_LOG_LINES", "1")

    async def resolve(**kwargs):
        return simulation_service.ResolvedTaskEndpoint(url=task.endpoint_url, configuration_ids=("configuration-123",))

    async def fail(task, context):
        context.log("first line")
        context.log("last line")
        raise SimulationConfigurationError("backend failed")

    monkeypatch.setattr(simulation_service, "_resolve_task_endpoint_url", resolve)
    monkeypatch.setattr(
        simulation_service,
        "get_backend",
        lambda name: SimpleNamespace(
            run=fail,
            descriptor=lambda: SimpleNamespace(version="test-version"),
        ),
    )
    await simulation_service._run_task(task)
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["configuration_ids"] == ["configuration-123"]
    assert manifest["source_version"]["backend_version"] == "test-version"
    assert manifest["truncated"] is True
    stored = SimulationTaskDao().get(task.id)
    assert stored.logs_truncated is True
    assert stored.configuration_ids == ["configuration-123"]
    assert stored.logs == task.logs
    assert len(task.logs) == 1


def test_deployment_execution_context_exposes_configuration_artifacts(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.deploy import executions

    execution = SimpleNamespace(
        execution_id="execution-123",
        status="ready",
        namespace="default",
        provenance={},
        endpoint=None,
        forwarded_endpoint=None,
        artifact=SimpleNamespace(configuration_artifact_ids=["configuration-123"]),
    )
    monkeypatch.setattr(
        executions,
        "serving_metadata",
        lambda *args: dict.fromkeys(
            (
                "name",
                "model_name",
                "backend",
                "guide",
                "replicas",
                "tensor_parallel_size",
                "image",
                "max_model_len",
                "gpu_memory_utilization",
                "enable_prefix_caching",
                "max_num_seqs",
                "max_num_batched_tokens",
                "storage_type",
                "storage_volume_id",
                "mount_path",
                "pvc_name",
                "custom_parameters",
            )
        ),
    )
    context = executions._context(None, None, execution)
    assert context.configuration_artifact_ids == ("configuration-123",)


@pytest.mark.asyncio
async def test_terminal_manifest_failure_does_not_publish_artifact_refs(simulation_root, monkeypatch):
    task = task_record(simulation_root, "aabbcc03")
    await save_task(task)
    assert task.artifact_uri

    def fail_registration(*args, **kwargs):
        raise OSError("manifest disk failure")

    monkeypatch.setattr(simulation_service, "register_artifacts", fail_registration)
    with pytest.raises(OSError, match="manifest disk failure"):
        await save_task(task)
    payload = SimulationTaskDao().get(task.id).model_dump(mode="json")
    assert not payload.get("artifact_uri")
    assert task.artifact_uri is None
    assert "manifest disk failure" in payload["artifact_error"]
    assert not (Path(task.task_dir) / "manifest.json").exists()
    loaded = await load_task(task.id)
    assert loaded.artifact_uri is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_interrupted_simulation_native_files_marked_incomplete(simulation_root, status):
    task = task_record(simulation_root, "aabbcc04", status)
    output = Path(task.task_dir) / "artifacts/requests.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text('{"partial":')
    await save_task(task)
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["truncated"]
    assert next(item for item in manifest["files"] if item["path"] == "artifacts/requests.jsonl")["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_fails", [False, True])
async def test_cancelled_pod_collects_evidence_before_cleanup(simulation_root, monkeypatch, copy_fails):
    from unittest.mock import AsyncMock

    from llm_d_lens.simulation import incluster

    events = []
    pod = incluster._PodHandle("test", "default", None)
    monkeypatch.setattr(incluster, "_create_pod", AsyncMock(return_value=pod))
    monkeypatch.setattr(incluster, "_wait_pod_running", AsyncMock(side_effect=SimulationCancelledError("cancelled")))

    async def collect(*args):
        events.append("collect")
        if copy_fails:
            raise OSError("copy failed")
        return True

    async def cleanup(*args):
        events.append("delete")

    monkeypatch.setattr(incluster, "_copy_out_of_pod", collect)
    monkeypatch.setattr(incluster, "_delete_pod", cleanup)
    context = RunContext(asyncio.Event(), lambda message: None, lambda *args: None)
    with pytest.raises(SimulationCancelledError):
        await incluster.execute_command_in_pod(
            namespace="default",
            cluster_id=None,
            task_dir=simulation_root,
            executable="aiperf",
            args=[],
            artifact_dir=simulation_root / "artifacts",
            timeout_seconds=30,
            context=context,
        )
    assert events == ["collect", "delete"]
    if copy_fails:
        assert context.artifacts_incomplete


@pytest.mark.asyncio
async def test_loading_changed_task_does_not_advertise_stale_manifest(simulation_root):
    task = task_record(simulation_root, "aabbcc05")
    await save_task(task)
    loaded = await load_task(task.id)
    assert loaded.artifact_uri == task.artifact_uri
    # Reproduce interruption after the new DB record is saved but before
    # the old on-disk manifest is replaced.
    task.description = "new record before manifest publication"
    task.artifact_uri = None
    SimulationTaskDao().save(task)
    loaded = await load_task(task.id)
    assert loaded.artifact_uri is None


@pytest.mark.asyncio
async def test_tolerated_host_timeout_marks_artifacts_incomplete(simulation_root):
    context = RunContext(asyncio.Event(), lambda message: None, lambda *args: None)
    result = await execute_command(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        simulation_root / "timeout-artifacts",
        0.05,
        context,
        tolerate_timeout=True,
    )
    assert result.timed_out
    assert context.artifacts_incomplete


@pytest.mark.asyncio
@pytest.mark.parametrize("served_name", [None, "Qwen/Qwen3-8B"])
async def test_create_task_resolves_a_model_service_group_to_its_healthy_member(
    simulation_root, monkeypatch, served_name
):
    """A Simulation task may target a published Model Service instead of picking a
    deployment execution directly -- only a Model Service is continuously
    health-probed, so this is the only existing-endpoint path with live backend
    liveness (see llm_d_lens/model_service/resolution.py).
    """
    import types

    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    group = ModelServiceGroup(id="msg-1", name="qwen3-prod", model_ref="Qwen/Qwen3-0.6B", served_name=served_name)
    member = types.SimpleNamespace(execution_id="exec-1")

    class _FakeGroups:
        def get(self, group_id):
            return group if group_id == "msg-1" else None

    class _FakeModelService:
        groups = _FakeGroups()

        def authorized_members(self, group, principal):  # noqa: ARG002
            return [member]

    monkeypatch.setattr("llm_d_lens.model_service.service.default_service", lambda: _FakeModelService())
    context = types.SimpleNamespace(
        endpoint="http://vllm.my-namespace.svc:8000",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
    )
    monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda execution_id: context)
    gateway = AsyncMock(return_value="http://gateway.model-service.svc:80")
    monkeypatch.setattr(simulation_service, "_cluster_gateway_endpoint", gateway)

    request = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_mode="external",
        endpoint_url="http://model-service.internal",
        model_name="placeholder",
        model_service_group_id="msg-1",
        api_key="lens-mk-x",
        trace_dataset="mooncake-arxiv",
        trace_path=trace.name,
    )

    task = await create_task(request)

    assert task.endpoint_mode == "deployment"
    assert task.endpoint_deployment_execution_id == "exec-1"
    assert task.model_name == "qwen3-prod"
    assert task.prompt.dataset.tokenizer == (served_name or group.model_ref)
    assert task.endpoint_url == "http://gateway.model-service.svc:80"
    gateway.assert_awaited_once_with("cluster-123")


@pytest.mark.asyncio
async def test_model_service_task_repairs_gateway_by_public_name(monkeypatch, simulation_root):
    """Reruns from before group ids were persisted still reconcile the Gateway."""
    import types

    group = types.SimpleNamespace(id="msg-1", cluster_id="cluster-1", name="qwen3")

    class _Groups:
        def get(self, _group_id):
            return None

        def list(self):
            return [group]

    monkeypatch.setattr(
        "llm_d_lens.model_service.service.default_service",
        lambda: types.SimpleNamespace(groups=_Groups()),
    )
    calls = []

    class _GatewayOps:
        async def reconcile_cluster_gateway(self, cluster_id):
            calls.append(cluster_id)
            return types.SimpleNamespace(status="succeeded", message="")

    monkeypatch.setattr("llm_d_lens.model_service.gateway_ops.GatewayOpsService", _GatewayOps)
    task = task_record(simulation_root, "aaaaaaaa", "queued").model_copy(
        update={
            "endpoint_mode": "deployment",
            "endpoint_cluster_id": "cluster-1",
            "model_name": "qwen3",
            "model_service_group_id": None,
        }
    )

    await simulation_service._ensure_model_service_gateway(task)

    assert calls == ["cluster-1"]


def test_model_service_group_id_requires_an_api_key():
    with pytest.raises(ValidationError, match="api_key"):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://model-service.internal",
            model_name="placeholder",
            model_service_group_id="msg-1",
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["Temporary failure in name resolution", "Connection refused", "Model discovery returned HTTP 401"]
)
async def test_unreachable_endpoint_blocks_creation_without_persistence(simulation_root, monkeypatch, failure):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n')
    probe = AsyncMock(side_effect=SimulationConfigurationError(failure))
    save = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(simulation_service, "discover_models", probe)
    monkeypatch.setattr(simulation_service, "save_task", save)
    monkeypatch.setattr(simulation_service, "_schedule_task", schedule)
    request = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://unreachable.svc.cluster.local",
        api_key="test-token",
        model_name="manually-entered-model",
        trace_dataset="mooncake-arxiv",
        trace_path=trace.name,
    )
    with pytest.raises(SimulationConfigurationError, match=failure):
        await create_task(request)
    probe.assert_awaited_once_with(request.endpoint_url, api_key="test-token")
    save.assert_not_awaited()
    schedule.assert_not_called()
    assert not simulation_service.tasks
    assert simulation_service.pending_creates == 0


@pytest.mark.asyncio
async def test_unreachable_endpoint_blocks_rerun(simulation_root, monkeypatch):
    original = task_record(simulation_root, "1234abcd")
    await save_task(original)
    probe = AsyncMock(side_effect=SimulationConfigurationError("Connection refused"))
    save = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(simulation_service, "discover_models", probe)
    monkeypatch.setattr(simulation_service, "save_task", save)
    monkeypatch.setattr(simulation_service, "_schedule_task", schedule)
    with pytest.raises(SimulationConfigurationError, match="Connection refused"):
        await rerun_task(
            original.id,
            override=SimulationTaskRerunRequest(
                endpoint_url="http://new-endpoint.local",
                api_key="new-token",
            ),
        )
    probe.assert_awaited_once_with("http://new-endpoint.local", api_key="new-token")
    save.assert_not_awaited()
    schedule.assert_not_called()
    assert simulation_service.pending_creates == 0


def test_discovery_api_forwards_token_without_serializing_it(simulation_root, monkeypatch):
    probe = AsyncMock(return_value={"endpoint": "http://endpoint.local", "models": ["model"]})
    monkeypatch.setattr(importlib.import_module("llm_d_lens.simulation.router"), "discover_models", probe)
    with TestClient(app) as client:
        response = client.post(
            "/api/simulation/models/discover",
            json={
                "endpoint_url": "http://endpoint.local",
                "api_key": "test-token",
            },
        )
    assert response.status_code == 200
    assert "test-token" not in response.text
    probe.assert_awaited_once_with("http://endpoint.local", api_key="test-token")


def test_create_api_rejects_unreachable_endpoint(simulation_root, monkeypatch):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n')
    probe = AsyncMock(side_effect=SimulationConfigurationError("Connection refused"))
    save = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(simulation_service, "discover_models", probe)
    monkeypatch.setattr(simulation_service, "save_task", save)
    monkeypatch.setattr(simulation_service, "_schedule_task", schedule)
    with TestClient(app) as client:
        response = client.post(
            "/api/simulation/tasks",
            json={
                "scenario": "chat",
                "backend": "aiperf",
                "endpoint_mode": "external",
                "endpoint_url": "http://unreachable.local",
                "model_name": "manual-model",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": trace.name,
            },
        )
    assert response.status_code == 400
    assert "Connection refused" in response.text
    save.assert_not_awaited()
    schedule.assert_not_called()


def _ai_provider_record(
    *,
    provider_id: str = "ai-provider-1",
    name: str = "Primary OpenAI",
    model: str = "gpt-4o-mini",
    api_key: str = "sk-provider-secret-987654",
    provider_type: str = "openai",
):
    from llm_d_lens.ai_providers.contracts import AIProvider

    return AIProvider(
        id=provider_id,
        name=name,
        baseUrl="https://llm.example.com/v1",
        model=model,
        apiKey=api_key,
        providerType=provider_type,
        timeoutSeconds=30,
    )


def _rich_telemetry_payload(task_id: str, *, num_pods: int = 2) -> dict:
    per_pod = [
        {
            "pod": f"vllm-decode-{idx}",
            "role": "decode",
            "request_rate_rps": {"mean": 12.5 + idx},
            "running_requests": {"mean": 6.0, "max": 12.0},
            "waiting_requests": {"mean": 2.0, "max": 8.0},
            "kv_cache_usage_percent": {"mean": 78.0, "max": 94.5},
            "prefix_cache_hit_percent": {"mean": 41.0},
        }
        for idx in range(num_pods)
    ]
    return {
        "task_id": task_id,
        "section": "all",
        "capabilities": {
            "has_server_telemetry": True,
            "has_sessions": True,
            "has_hardware_profile": True,
            "has_system_telemetry": True,
        },
        "forensics": {
            "verdict": {
                "code": "kv_cache_eviction",
                "label": "KV Cache & Eviction Bound",
                "severity": "critical",
                "attainment_pct": 81.2,
                "reason": "KV cache reached 94.5% peak.",
            },
            "findings": [
                {
                    "severity": "critical",
                    "code": f"kv_pressure_{idx}",
                    "title": "High KV cache pressure",
                    "category": "KV Cache",
                    "metric": "kv_cache_peak_pct",
                    "value": "94.5%",
                    "recommendation": "Increase --gpu-memory-utilization or enable CPU KV offload.",
                }
                for idx in range(1 if num_pods <= 2 else 20)
            ],
            "pod_skew": {
                "available": True,
                "topology_mode": "disaggregated",
                "skew_ratio": 2.4,
                "pods": [{"pod": f"vllm-decode-{idx}", "kv_cache_peak_pct": 94.5} for idx in range(num_pods)],
            },
            "ttft_decomposition": {"available": True, "client_ttft_p90_ms": 1420.0, "vllm_queue_p90_ms": 620.0},
            "routing_vs_cache": {"available": True, "epp_expected_hit_pct": 65.0, "vllm_actual_hit_pct": 41.0},
        },
        "client_series": {
            "windows": {
                "profiling": {
                    "request_count": 120,
                    "successful_requests": 118,
                    "failed_requests": 2,
                    "error_rate_pct": 1.67,
                    "slo_attainment_pct": 81.2,
                    "throughput_rps": 4.2,
                    "output_tok_per_s": 512.4,
                    "ttft_ms": {"p50": 320.0, "p90": 1420.0, "p99": 2100.0},
                    "tpot_ms": {"p50": 24.5, "p90": 48.0, "p99": 72.0},
                    "e2e_ms": {"p50": 1800.0, "p90": 4200.0, "p99": 5900.0},
                    "drift_ms": {"p50": 4.0, "p90": 18.0, "max": 45.0},
                },
                "full": {
                    "request_count": 140,
                    "successful_requests": 138,
                    "failed_requests": 2,
                    "error_rate_pct": 1.43,
                    "slo_attainment_pct": 80.0,
                    "throughput_rps": 4.0,
                    "output_tok_per_s": 490.0,
                    "ttft_ms": {"p50": 350.0, "p90": 1500.0, "p99": 2300.0},
                },
            },
            "cold_start_comparison": {
                "mode": "configured_warmup",
                "cold_to_steady_ratio": 1.4,
                "warmup": {"ttft_ms": {"p50": 448.0}},
                "steady_state": {"ttft_ms": {"p50": 320.0}},
            },
            "token_flow": {"sent_tokens": 240000, "client_cached_tokens": 96000, "raw_session_reusable_tokens": 110000},
            "error_summary": {"total_errors": 2, "error_rate_pct": 1.43, "counts_by_code": {"all": 2, "503": 2}},
        },
        "server_telemetry": {
            "status": "available",
            "topology_mode": "disaggregated",
            "hardware_profiles": [{"id": "nvidia-h100", "display_name": "NVIDIA H100", "vendor": "nvidia"}],
            "summary": {
                "running_requests": {"mean": 12.0, "max": 24.0},
                "queue_depth": {"mean": 3.5, "max": 11.0},
                "kv_cache_usage_percent": {"mean": 78.0, "max": 94.5},
                "prefix_cache_hit_percent": {"mean": 41.0},
                "cpu_cache_usage_percent": {"mean": 32.0, "max": 58.0},
                "kv_offload_bytes_per_second": {"mean": 104857600.0},
                "vllm_preemptions_per_second": {"mean": 0.4, "max": 2.0},
                "input_token_rate_tps": {"mean": 2100.0},
                "output_token_rate_tps": {"mean": 512.4},
                "gpu_utilization_percent": {"mean": 88.5, "max": 97.0},
                "gpu_power_watts": {"mean": 340.0, "max": 410.0},
                "gpu_temperature_celsius": {"mean": 71.0, "max": 76.0},
            },
            "router": {"scheduler_latency_p90_ms": {"p90": 3.2}, "prefix_cache_hit_percent": {"mean": 65.0}},
            "per_pod": per_pod,
            "system": {
                "available": True,
                "cpu_avg_pct": 46.2,
                "cpu_peak_pct": 68.4,
                "mem_total_bytes": 512 * 1024**3,
                "mem_avail_bytes": 240 * 1024**3,
                "disk_read_bps": 4096.0,
            },
        },
        "sessions_summary": {
            "available": True,
            "theoretical_prefix_overlap_pct": 54.0,
            "reusable_prefix_tokens": 110000,
        },
        "run_manifest": {
            "target": {"model_name": "Qwen/Qwen3-8B", "topology_mode": "disaggregated"},
            "hardware_profile": {"id": "nvidia-h100"},
        },
    }


def test_ai_analysis_happy_path_model_override_and_compact_evidence(simulation_root, monkeypatch):
    import types

    task = task_record(simulation_root, "abcd1234", "completed").model_copy(
        update={
            "model_name": "Qwen/Qwen3-8B",
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={"warmup_seconds": 10, "ttft_slo": 1.0, "concurrency": 16},
                duration_seconds=120,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )
    provider = _ai_provider_record()
    captured: dict = {}

    class _FakeClient:
        async def complete(self, *, system_prompt, user_content, max_tokens=2048, temperature=0):
            captured["system_prompt"] = system_prompt
            captured["user_content"] = user_content
            captured["max_tokens"] = max_tokens
            captured["temperature"] = temperature
            return "## Executive Summary & Primary Bottleneck\nKV cache hit 94.5% peak."

    def _fake_client_for(**kwargs):
        captured["client_for"] = kwargs
        return _FakeClient()

    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        simulation_service, "get_task_telemetry", AsyncMock(return_value=_rich_telemetry_payload(task.id))
    )
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(
            list=lambda: [provider], get=lambda pid: provider if pid == provider.id else None
        ),
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", _fake_client_for)

    with TestClient(app) as client:
        response = client.post(
            f"/api/simulation/tasks/{task.id}/ai-analysis",
            json={"model": "gpt-4o"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == task.id
    assert body["provider_id"] == provider.id
    assert body["provider_name"] == provider.name
    assert body["model"] == "gpt-4o"
    assert body["telemetry_scope"] == "full_telemetry"
    assert "Executive Summary" in body["analysis"]
    assert body["generated_at"].endswith("Z")

    assert captured["client_for"]["model"] == "gpt-4o"
    assert captured["client_for"]["api_key"] == provider.api_key
    assert provider.timeout_seconds == 30
    assert captured["client_for"]["timeout_seconds"] >= 180.0
    assert captured["max_tokens"] == 8192
    assert captured["temperature"] == 0.2
    prompt_lower = captured["system_prompt"].lower()
    assert "inference performance engineer" in prompt_lower
    assert "--gpu-memory-utilization" in prompt_lower
    assert "--max-num-seqs" in prompt_lower
    assert "--enable-prefix-caching" in prompt_lower
    assert "inferencepool" in prompt_lower
    assert "epp" in prompt_lower
    assert "disaggregation" in prompt_lower
    assert "nixl" in prompt_lower
    assert "already-enabled" in prompt_lower
    assert "1420.0" in captured["user_content"]
    assert "kv_cache_eviction" in captured["user_content"]
    assert "cpu_avg_pct" in captured["user_content"]
    assert "token_flow" in captured["user_content"]
    assert provider.api_key not in response.text

    # Large telemetry payload still stays strictly <= 12,000 chars for evidence pack
    large_pack, _scope = simulation_service._build_telemetry_evidence_pack(
        task, _rich_telemetry_payload(task.id, num_pods=120)
    )
    serialized = simulation_service._serialize_evidence_pack(large_pack)
    assert len(serialized) <= 12000

    # Verify _compute_telemetry_scope returns "server_only" when server metrics exist without client metrics
    server_only_scope = simulation_service._compute_telemetry_scope(
        {"server_telemetry": {"status": "available", "summary": {"running_requests": {"mean": 1.0}}}}
    )
    assert server_only_scope == "server_only"


def test_ai_analysis_stage_breakdown_protection_and_prompt_terminology(simulation_root):
    import re

    task = task_record(simulation_root, "abcd1299", "completed").model_copy(
        update={
            "model_name": "Qwen/Qwen3-8B",
            "endpoint_url": "https://gateway.internal.example/v1/" + ("segment/" * 60),
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={"warmup_seconds": 10, "ttft_slo": 1.0, "concurrency": 16, "extra": "x" * 4000},
                duration_seconds=120,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )

    prompt = simulation_service._AI_ANALYSIS_SYSTEM_PROMPT
    assert re.search(r"\brouter\b", prompt, re.IGNORECASE) is None
    assert re.search(r"\bsla\b", prompt, re.IGNORECASE) is None
    assert "Inference Gateway / EPP" in prompt
    assert "stage_latency_breakdown" in prompt
    assert "Request Stage Latency Breakdown" in prompt
    assert "KV Transfer (NIXL)" in prompt
    assert "Server TTFT Residual" in prompt
    assert "Client / Gateway / Network" in prompt
    assert "role_breakdown" in prompt
    assert "reconciliation" in prompt
    assert "strictly additive" in prompt

    rich = _rich_telemetry_payload(task.id, num_pods=160)
    rich["forensics"]["findings"] = [
        {
            "severity": "warning",
            "code": f"finding_{i}",
            "title": f"Finding {i}",
            "description": "d" * 600,
            "recommendation": "r" * 400,
        }
        for i in range(12)
    ]
    rich["forensics"]["stage_latency_breakdown"] = {
        "available": True,
        "source": "server_telemetry",
        "has_p90_series": True,
        "has_epp_series": True,
        "primary_slice": "p50",
        "dominant_e2e_stage": "decode_time",
        "dominant_ttft_stage": "prefill_time",
        "dominant_e2e_stage_p90": "queue_time",
        "dominant_ttft_stage_p90": "queue_time",
        "p50": {
            "available": True,
            "epp_scheduling_ms": 2.4,
            "queue_time_ms": 18.0,
            "prefill_time_ms": 140.0,
            "decode_time_ms": 820.0,
            "gateway_overhead_ms": 14.6,
            "ttft_stage_ms": 175.0,
            "total_stage_ms": 995.0,
            "share_of_e2e_pct": {
                "epp_scheduling": 0.2,
                "queue_time": 1.8,
                "prefill_time": 14.1,
                "decode_time": 82.4,
                "gateway_overhead": 1.5,
            },
            "share_of_ttft_pct": {
                "epp_scheduling": 1.4,
                "queue_time": 10.3,
                "prefill_time": 80.0,
                "gateway_overhead": 8.3,
            },
        },
        "p90": {
            "available": True,
            "epp_scheduling_ms": 5.0,
            "queue_time_ms": 1200.0,
            "prefill_time_ms": 210.0,
            "decode_time_ms": 980.0,
            "gateway_overhead_ms": 25.0,
            "ttft_stage_ms": 1440.0,
            "total_stage_ms": 2420.0,
            "share_of_e2e_pct": {
                "epp_scheduling": 0.2,
                "queue_time": 49.6,
                "prefill_time": 8.7,
                "decode_time": 40.5,
                "gateway_overhead": 1.0,
            },
            "share_of_ttft_pct": {
                "epp_scheduling": 0.3,
                "queue_time": 83.3,
                "prefill_time": 14.6,
                "gateway_overhead": 1.7,
            },
        },
        "peak_window_shift": {
            "detected": True,
            "baseline_stage": "decode_time",
            "peak_stage": "queue_time",
            "peak_t_seconds": 85.0,
            "peak_total_ms": 2950.0,
            "peak_stage_ms": 1840.0,
            "peak_stage_share_pct": 62.4,
            "queue_peak_ms": 1840.0,
            "queue_peak_t_seconds": 85.0,
            "tail_p90_stage": "queue_time",
            "summary": "Peak window at t=85s shifted dominant stage from Decode Time to Queue Time.",
        },
        "series": [
            {
                "t": float(i * 5),
                "epp_sched_ms": 2.0,
                "queue_ms": 1840.0 if i == 17 else 18.0,
                "prefill_ms": 140.0,
                "decode_ms": 820.0,
                "epp_sched_p90_ms": 5.0,
                "queue_p90_ms": 2100.0 if i == 17 else 40.0,
                "prefill_p90_ms": 210.0,
                "decode_p90_ms": 980.0,
            }
            for i in range(40)
        ],
    }

    pack, _scope = simulation_service._build_telemetry_evidence_pack(task, rich)
    forensics_keys = list(pack["forensics"].keys())
    assert forensics_keys[:2] == ["verdict", "stage_latency_breakdown"]
    slb = pack["forensics"]["stage_latency_breakdown"]
    assert len(slb["time_progression"]) <= 10
    assert slb["time_progression"][0]["t"] == 0.0
    assert slb["time_progression"][-1]["t"] == 195.0
    assert any(pt["t"] == 85.0 for pt in slb["time_progression"])

    # Worst-case compaction <= 12,000 chars preserves valid JSON & summary shares
    serialized = simulation_service._serialize_evidence_pack(pack)
    assert len(serialized) <= 12_000
    assert not serialized.endswith("...[truncated]")
    parsed = json.loads(serialized)
    parsed_slb = parsed["forensics"]["stage_latency_breakdown"]
    assert parsed_slb["dominant_e2e_stage"] == "decode_time"
    assert parsed_slb["dominant_e2e_stage_p90"] == "queue_time"
    assert parsed_slb["p50"]["share_of_e2e_pct"]["decode_time"] == 82.4
    assert parsed_slb["p90"]["share_of_e2e_pct"]["queue_time"] == 49.6
    assert parsed_slb["peak_window_shift"]["detected"] is True
    assert "time_progression" not in parsed_slb

    # Non-finite / bool numbers in persisted stage_latency_breakdown are stripped before serialization
    dirty_slb = {
        "available": True,
        "source": "server_telemetry",
        "p50": {
            "available": True,
            "queue_time_ms": float("nan"),
            "prefill_time_ms": True,
            "decode_first_step_ms": float("nan"),
            "prefill_engine_overhead_ms": float("inf"),
            "decode_handoff_overhead_ms": True,
            "decode_time_ms": 500.0,
            "share_of_e2e_pct": {"decode_time": 100.0, "queue_time": float("inf")},
        },
        "peak_window_shift": {"detected": True, "peak_total_ms": float("nan"), "queue_peak_ms": True},
        "series": [
            {"t": 0.0, "queue_ms": float("nan"), "decode_ms": 500.0},
            {"t": float("inf"), "decode_ms": 600.0},
        ],
    }
    compacted_dirty = simulation_service._compact_stage_latency_breakdown(dirty_slb)
    json.dumps(compacted_dirty, allow_nan=False)
    assert "queue_time_ms" not in compacted_dirty["p50"]
    assert "prefill_time_ms" not in compacted_dirty["p50"]
    assert "decode_first_step_ms" not in compacted_dirty["p50"]
    assert "prefill_engine_overhead_ms" not in compacted_dirty["p50"]
    assert "decode_handoff_overhead_ms" not in compacted_dirty["p50"]
    assert "queue_time" not in compacted_dirty["p50"]["share_of_e2e_pct"]
    assert "peak_total_ms" not in compacted_dirty["peak_window_shift"]
    assert "queue_peak_ms" not in compacted_dirty["peak_window_shift"]
    assert len(compacted_dirty["time_progression"]) == 2
    assert "queue_ms" not in compacted_dirty["time_progression"][0]
    assert "t" not in compacted_dirty["time_progression"][1]

    # New stage-breakdown fields: kv_transfer_ms, engine_overhead_ms, P/D engine-overhead sub-fields,
    # ttft_unattributed_ms, mean slice gating, caveat, reconciliation, and role_breakdown
    rich_slb = {
        "available": True,
        "source": "server_telemetry",
        "caveat": "Sample caveat",
        "p50": {
            "available": True,
            "epp_scheduling_ms": 0.2,
            "queue_time_ms": 50.0,
            "kv_transfer_ms": 1150.0,
            "prefill_time_ms": 300.0,
            "engine_overhead_ms": 40.0,
            "decode_first_step_ms": 12.5,
            "prefill_engine_overhead_ms": 17.5,
            "decode_handoff_overhead_ms": 10.0,
            "gateway_overhead_ms": 20.0,
            "ttft_unattributed_ms": 10.0,
            "decode_time_ms": 8000.0,
            "ttft_stage_ms": 1570.2,
            "total_stage_ms": 9570.2,
            "reconciliation": {"client_e2e_ms": 9600.0, "total_stage_ms": 9570.2, "quantile_non_additive": True},
        },
        "mean": {
            "available": True,
            "source": "server_telemetry",
            "kv_transfer_ms": 1200.0,
            "decode_first_step_ms": 23.585,
            "prefill_engine_overhead_ms": 403.447,
            "decode_handoff_overhead_ms": 348.845,
            "decode_time_ms": 8500.0,
            "reconciliation": {"client_e2e_ms": 10000.0, "total_stage_ms": 9700.0, "quantile_non_additive": False},
        },
        "role_breakdown": {
            "prefill": {"waiting_mean": 0.0, "running_mean": 2.0},
            "decode": {"queue_raw_p50_ms": 1200.0, "queue_excl_kv_p50_ms": 50.0},
        },
    }
    compacted_rich = simulation_service._compact_stage_latency_breakdown(rich_slb)
    assert compacted_rich["caveat"] == "Sample caveat"
    assert compacted_rich["p50"]["kv_transfer_ms"] == 1150.0
    assert compacted_rich["p50"]["engine_overhead_ms"] == 40.0
    assert compacted_rich["p50"]["decode_first_step_ms"] == 12.5
    assert compacted_rich["p50"]["prefill_engine_overhead_ms"] == 17.5
    assert compacted_rich["p50"]["decode_handoff_overhead_ms"] == 10.0
    assert compacted_rich["p50"]["ttft_unattributed_ms"] == 10.0
    assert compacted_rich["p50"]["reconciliation"]["quantile_non_additive"] is True
    assert compacted_rich["mean"]["kv_transfer_ms"] == 1200.0
    assert compacted_rich["mean"]["decode_first_step_ms"] == 23.585
    assert compacted_rich["mean"]["prefill_engine_overhead_ms"] == 403.447
    assert compacted_rich["mean"]["decode_handoff_overhead_ms"] == 348.845
    assert compacted_rich["mean"]["reconciliation"]["quantile_non_additive"] is False
    assert compacted_rich["role_breakdown"]["prefill"]["waiting_mean"] == 0.0
    assert compacted_rich["role_breakdown"]["decode"]["queue_excl_kv_p50_ms"] == 50.0

    # mean slice with client_fallback source is omitted from compacted output
    rich_slb["mean"]["source"] = "client_fallback"
    assert "mean" not in simulation_service._compact_stage_latency_breakdown(rich_slb)


def test_ai_analysis_secret_redaction_and_fallback_scope(simulation_root, monkeypatch):
    import types

    task = task_record(simulation_root, "abcd1235", "failed").model_copy(
        update={
            "endpoint_url": "https://deploy-user:url-secret-pass@vllm.example.com/v1;params=psecret?sig=secret123#fragsecret",
            "api_key": "task-secret-key-xyz",
            "error_message": "Upstream failed with Bearer sk-embedded-secret-123456",
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={
                    "warmup_seconds": 5,
                    "api_key": "opt-api-key-111",
                    "token": "opt-token-222",
                    "authorization": "Bearer opt-auth-333",
                    "password": "opt-password-444",
                    "secret": "opt-secret-555",
                },
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
            "result": SimulationResult(
                run_id="run-1",
                backend="aiperf",
                summary={"request_count": 10, "api_key": "result-secret-666"},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=["Authorization: Bearer warn-secret-777"],
            ),
        }
    )
    provider = _ai_provider_record(api_key="sk-provider-secret-do-not-leak")
    captured: dict = {}

    class _FakeClient:
        async def complete(self, *, system_prompt, user_content, max_tokens=2048, temperature=0):
            captured["user_content"] = user_content
            return "Task failed early before telemetry was collected."

    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        simulation_service,
        "get_task_telemetry",
        AsyncMock(side_effect=SimulationConfigurationError("No telemetry artifacts")),
    )
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(list=lambda: [provider], get=lambda pid: provider),
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _FakeClient())

    with TestClient(app) as client:
        response = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})

    assert response.status_code == 200, response.text
    assert response.json()["telemetry_scope"] == "task_summary_only"
    prompt_text = captured["user_content"]
    for secret_value in (
        "url-secret-pass",
        "secret123",
        "sig=",
        "psecret",
        "fragsecret",
        "task-secret-key-xyz",
        "sk-embedded-secret-123456",
        "opt-api-key-111",
        "opt-token-222",
        "opt-auth-333",
        "opt-password-444",
        "opt-secret-555",
        "result-secret-666",
        "warn-secret-777",
        provider.api_key,
    ):
        assert secret_value not in prompt_text
    for stripped_key in ('"api_key"', '"token"', '"authorization"', '"password"', '"secret"'):
        assert stripped_key not in prompt_text


def test_ai_analysis_multiturn_followup(simulation_root, monkeypatch):
    import types

    task = task_record(simulation_root, "abcd1236", "completed")
    provider = _ai_provider_record()
    captured: dict = {}

    class _FakeClient:
        async def complete(self, *, system_prompt, user_content, max_tokens=2048, temperature=0):
            captured["system_prompt"] = system_prompt
            captured["user_content"] = user_content
            return "Follow-up recommendation based on KV cache metrics."

    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        simulation_service, "get_task_telemetry", AsyncMock(return_value=_rich_telemetry_payload(task.id))
    )
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(list=lambda: [provider], get=lambda pid: provider),
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _FakeClient())

    with TestClient(app) as client:
        response = client.post(
            f"/api/simulation/tasks/{task.id}/ai-analysis",
            json={
                "provider_id": provider.id,
                "messages": [
                    {"role": "user", "content": "What is the primary bottleneck?"},
                    {"role": "assistant", "content": "KV cache reached 94.5% peak."},
                ],
                "question": "Which vLLM flags fix this? Please reply in Simplified Chinese.",
            },
        )

    assert response.status_code == 200, response.text
    assert "What is the primary bottleneck?" in captured["user_content"]
    assert "KV cache reached 94.5% peak." in captured["user_content"]
    assert "Which vLLM flags fix this? Please reply in Simplified Chinese." in captured["user_content"]
    assert "simplified chinese" in captured["system_prompt"].lower()


def test_ai_analysis_error_statuses_and_sanitized_upstream_errors(simulation_root, monkeypatch):
    import types

    import httpx

    from llm_d_lens.ai_providers.client import AIProviderClientError

    task = task_record(simulation_root, "abcd1237", "completed")
    provider = _ai_provider_record(api_key="sk-top-secret-upstream-key")

    # 1. Missing task -> 404
    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=None))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=None))
    with TestClient(app) as client:
        missing_resp = client.post("/api/simulation/tasks/deadbeef/ai-analysis", json={})
    assert missing_resp.status_code == 404

    monkeypatch.setattr(simulation_service, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        simulation_service, "get_task_telemetry", AsyncMock(return_value=_rich_telemetry_payload(task.id))
    )

    # 2. No configured provider -> 409
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(list=lambda: [], get=lambda _pid: None),
    )
    with TestClient(app) as client:
        no_provider_resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
    assert no_provider_resp.status_code == 409

    # 3. Unknown provider_id -> 404
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(list=lambda: [provider], get=lambda _pid: None),
    )
    with TestClient(app) as client:
        unknown_provider_resp = client.post(
            f"/api/simulation/tasks/{task.id}/ai-analysis",
            json={"provider_id": "missing-provider"},
        )
    assert unknown_provider_resp.status_code == 404

    # 4. Upstream AIProviderClientError -> 502 with unmasked scrubbed problem+json detail
    class _ErrorClient:
        async def complete(self, **_kwargs):
            raise AIProviderClientError(f"Upstream 401 with Bearer sk-top-secret-upstream-key and {provider.api_key}")

    monkeypatch.setattr(
        "llm_d_lens.ai_providers.service.default_service",
        lambda: types.SimpleNamespace(list=lambda: [provider], get=lambda pid: provider),
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _ErrorClient())
    with TestClient(app) as client:
        upstream_err_resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
    assert upstream_err_resp.status_code == 502
    assert upstream_err_resp.headers["content-type"].startswith("application/problem+json")
    upstream_err_body = upstream_err_resp.json()
    assert upstream_err_body["code"] == "ai_analysis_upstream_error"
    assert upstream_err_body["detail"] != "An unexpected error occurred."
    assert "Upstream 401" in upstream_err_body["detail"]
    assert provider.api_key not in upstream_err_resp.text
    assert "sk-top-secret-upstream-key" not in upstream_err_resp.text

    # 4b. UnsafeProviderEndpointError -> 502 with unmasked scrubbed problem+json detail
    from llm_d_lens.ai_providers.service import UnsafeProviderEndpointError

    class _UnsafeEndpointClient:
        async def complete(self, **_kwargs):
            raise UnsafeProviderEndpointError()

    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _UnsafeEndpointClient())
    with TestClient(app) as client:
        unsafe_err_resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
    assert unsafe_err_resp.status_code == 502
    assert unsafe_err_resp.headers["content-type"].startswith("application/problem+json")
    unsafe_err_body = unsafe_err_resp.json()
    assert unsafe_err_body["code"] == "ai_analysis_upstream_error"
    assert unsafe_err_body["detail"] != "An unexpected error occurred."
    assert "public addresses" in unsafe_err_body["detail"]

    # 5. Upstream timeout (both direct TimeoutException and AIProviderClientError wrapping TimeoutException) -> 504
    class _DirectTimeoutClient:
        async def complete(self, **_kwargs):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _DirectTimeoutClient())
    with TestClient(app) as client:
        timeout_resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
    assert timeout_resp.status_code == 504
    assert timeout_resp.headers["content-type"].startswith("application/problem+json")
    timeout_body = timeout_resp.json()
    assert timeout_body["code"] == "ai_analysis_upstream_error"
    assert timeout_body["detail"] != "An unexpected error occurred."
    assert "timed out" in timeout_body["detail"].lower()

    class _WrappedTimeoutClient:
        async def complete(self, **_kwargs):
            try:
                raise httpx.ReadTimeout("read timed out")
            except httpx.ReadTimeout as exc:
                raise AIProviderClientError(f"OpenAI-compatible provider request failed: {exc}") from exc

    monkeypatch.setattr("llm_d_lens.ai_providers.client.client_for", lambda **_kw: _WrappedTimeoutClient())
    with TestClient(app) as client:
        wrapped_timeout_resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
    assert wrapped_timeout_resp.status_code == 504
    assert wrapped_timeout_resp.headers["content-type"].startswith("application/problem+json")
    wrapped_timeout_body = wrapped_timeout_resp.json()
    assert wrapped_timeout_body["code"] == "ai_analysis_upstream_error"
    assert wrapped_timeout_body["detail"] != "An unexpected error occurred."
    assert "timed out" in wrapped_timeout_body["detail"].lower()


def test_ai_analysis_auth_route_registry_and_cluster_access_denied(simulation_root, monkeypatch):
    from llm_d_lens.assistant_tools.catalog import catalog
    from llm_d_lens.auth.routes import ROUTE_PERMISSIONS, validate_route_registry
    from llm_d_lens.core.exceptions import ForbiddenError

    assert validate_route_registry(app) == []
    by_route = {(r.method, r.path): r.permission for r in ROUTE_PERMISSIONS}
    assert by_route[("POST", "/api/simulation/tasks/{task_id}/ai-analysis")] == "simulation:task:read"

    if hasattr(app.state, "mcp_catalog"):
        delattr(app.state, "mcp_catalog")
    assert all(
        t["name"] != "analyze_simulation_task_with_ai"
        and t.get("route") != "/api/simulation/tasks/{task_id}/ai-analysis"
        for t in catalog(app)
    )

    task = task_record(simulation_root, "abcd1238", "completed").model_copy(
        update={"endpoint_cluster_id": "cluster-restricted"}
    )
    sim_router = importlib.import_module("llm_d_lens.simulation.router")
    monkeypatch.setattr(sim_router, "load_task", AsyncMock(return_value=task))

    def deny_cluster(_principal, cluster_id: str | None) -> None:
        if cluster_id == "cluster-restricted":
            raise ForbiddenError("cluster is not accessible")

    monkeypatch.setattr(sim_router, "require_cluster_access", deny_cluster)

    with TestClient(app) as client:
        resp = client.post(f"/api/simulation/tasks/{task.id}/ai-analysis", json={})
        extra_resp = client.post(
            f"/api/simulation/tasks/{task.id}/ai-analysis",
            json={"unexpected_field": "nope"},
        )
    assert resp.status_code == 403
    assert extra_resp.status_code == 422


def _setup_local_users_and_tokens(monkeypatch, tmp_path):
    from llm_d_lens.auth import master_key
    from llm_d_lens.auth.service import default_service as default_auth_service
    from llm_d_lens.auth.settings import AuthSettings, set_settings_for_testing
    from llm_d_lens.model_service.service import default_service as default_model_service

    monkeypatch.setattr(master_key, "master_key_path", lambda: tmp_path / "master_key.json")
    set_settings_for_testing(
        AuthSettings(
            auth_mode="local",
            secret_key="sim-integration-secret",  # noqa: S106 - test fixture
            session_ttl_seconds=3600,
            session_idle_seconds=3600,
            auto_seed_admin=False,
        )
    )
    auth_svc = default_auth_service()
    auth_svc.ensure_builtin_roles()
    user_a = auth_svc.bootstrap_admin("sim-user-a", "Correct-Horse-9!")
    user_b = auth_svc.create_user(username="sim-user-b", password="Correct-Horse-9!")  # noqa: S106 - test fixture
    admin_role = auth_svc.require_role("admin")
    auth_svc.add_user_binding(user_b.id, admin_role.id, scope_type="global")

    login_a = auth_svc._issue_session(user_a, ip=None, user_agent=None)
    login_b = auth_svc._issue_session(user_b, ip=None, user_agent=None)

    model_svc = default_model_service()
    token_a, _ = model_svc.tokens.create(user_a.id, name="user-a-key")
    token_b, _ = model_svc.tokens.create(user_b.id, name="user-b-key")
    return {
        "auth_svc": auth_svc,
        "model_svc": model_svc,
        "user_a": user_a,
        "user_b": user_b,
        "principal_a": login_a.principal,
        "principal_b": login_b.principal,
        "headers_a": {"Authorization": f"Bearer {login_a.token}"},
        "headers_b": {"Authorization": f"Bearer {login_b.token}"},
        "token_a": token_a,
        "token_b": token_b,
    }


def _fake_http_request(principal):
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(principal=principal))


def test_discover_models_with_model_access_token_id(simulation_root, monkeypatch):
    from datetime import timedelta

    from llm_d_lens.auth.settings import AuthSettings, reset_settings_override, set_settings_for_testing
    from llm_d_lens.model_service.contracts import DELEGATED_TOKEN_PREFIX, utcnow

    ctx = _setup_local_users_and_tokens(monkeypatch, simulation_root)
    captured_keys: list[str | None] = []

    async def fake_discover(endpoint_url: str, *, api_key: str | None = None):
        captured_keys.append(api_key)
        return {"endpoint": endpoint_url, "models": ["qwen3"]}

    monkeypatch.setattr(importlib.import_module("llm_d_lens.simulation.router"), "discover_models", fake_discover)
    try:
        with TestClient(app) as client:
            # 1. Valid owned active token -> mints short-lived delegated token
            ok_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_a"],
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": ctx["token_a"].id},
            )
            assert ok_resp.status_code == 200
            assert len(captured_keys) == 1
            minted = captured_keys[0]
            assert minted is not None and minted.startswith(DELEGATED_TOKEN_PREFIX)
            assert minted not in ok_resp.text
            validated = ctx["model_svc"].tokens.validate(minted)
            assert validated is not None and validated.id == ctx["token_a"].id

            # 2. Both api_key and model_access_token_id -> 422
            both_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_a"],
                json={
                    "endpoint_url": "http://endpoint.local",
                    "api_key": "lens-mk-raw",
                    "model_access_token_id": ctx["token_a"].id,
                },
            )
            assert both_resp.status_code == 422

            # 3. Cross-user token -> 400
            cross_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_b"],
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": ctx["token_a"].id},
            )
            assert cross_resp.status_code == 400

            # 4. Expired token -> 400
            exp_tok, _ = ctx["model_svc"].tokens.create(ctx["user_a"].id, name="expired-key")
            exp_tok.expires_at = utcnow() - timedelta(seconds=5)
            ctx["model_svc"].tokens._dao.save(exp_tok)
            expired_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_a"],
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": exp_tok.id},
            )
            assert expired_resp.status_code == 400

            # 5. Revoked token -> 400
            assert ctx["model_svc"].tokens.revoke(ctx["user_a"].id, ctx["token_a"].id) is True
            revoked_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_a"],
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": ctx["token_a"].id},
            )
            assert revoked_resp.status_code == 400

            # 6. Auth-disabled ("system" principal / None caller) -> 401
            set_settings_for_testing(AuthSettings(auth_mode="disabled", secret_key="sim-integration-secret"))  # noqa: S106
            disabled_resp = client.post(
                "/api/simulation/models/discover",
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": ctx["token_b"].id},
            )
            assert disabled_resp.status_code == 401

            # 7. Master key unconfigured (RuntimeError on mint) -> 503
            from llm_d_lens.auth.master_key import MasterKey
            from llm_d_lens.model_service import tokens as tokens_module

            set_settings_for_testing(
                AuthSettings(
                    auth_mode="local",
                    secret_key="",
                    session_ttl_seconds=3600,
                    session_idle_seconds=3600,
                    auto_seed_admin=False,
                )
            )
            monkeypatch.setattr(
                tokens_module,
                "ensure_master_key",
                lambda _settings: MasterKey(primary="", old=(), source="unconfigured"),
            )
            unconfigured_resp = client.post(
                "/api/simulation/models/discover",
                headers=ctx["headers_b"],
                json={"endpoint_url": "http://endpoint.local", "model_access_token_id": ctx["token_b"].id},
            )
            assert unconfigured_resp.status_code == 503
    finally:
        reset_settings_override()


@pytest.mark.asyncio
async def test_create_and_run_task_mints_fresh_delegated_token_and_clears_in_finally(simulation_root, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from llm_d_lens.auth.settings import reset_settings_override
    from llm_d_lens.model_service.contracts import DELEGATED_TOKEN_PREFIX

    ctx = _setup_local_users_and_tokens(monkeypatch, simulation_root)
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")

    probe_keys: list[str | None] = []

    async def fake_discover(endpoint_url: str, *, api_key: str | None = None):
        probe_keys.append(api_key)
        return {"endpoint": endpoint_url, "models": ["model"]}

    monkeypatch.setattr(simulation_service, "discover_models", fake_discover)
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    try:
        # Unauthenticated / auth-disabled caller with model_access_token_id -> 401
        req_unauth = SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://endpoint.local",
            model_name="model",
            model_access_token_id=ctx["token_a"].id,
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
        )
        with pytest.raises(HTTPException) as unauth_exc:
            await create_task(req_unauth, http_request=None)
        assert unauth_exc.value.status_code == 401

        # Authenticated create_task -> mints 120s probe token, persists ID + owner, api_key stays None
        task = await create_task(req_unauth, http_request=_fake_http_request(ctx["principal_a"]))
        assert len(probe_keys) == 1
        assert probe_keys[0] is not None and probe_keys[0].startswith(DELEGATED_TOKEN_PREFIX)
        assert task.api_key is None
        assert task.model_access_token_id == ctx["token_a"].id
        assert task.owner_user_id == ctx["user_a"].id
        dumped = task.model_dump(mode="json")
        assert dumped["model_access_token_id"] == ctx["token_a"].id
        assert "api_key" not in dumped
        assert "owner_user_id" not in dumped

        reloaded = SimulationTaskDao().get(task.id)
        assert reloaded is not None
        assert reloaded.api_key is None
        assert reloaded.model_access_token_id == ctx["token_a"].id
        assert reloaded.owner_user_id == ctx["user_a"].id
        assert dict(reloaded.simulation.backend_options) == {"random_seed": 666}

        # _run_task mints a fresh delegated token for backend.run and clears task.api_key in finally
        seen_run_tokens: list[str | None] = []

        async def fake_run(running_task: SimulationTask, _context: RunContext):
            seen_run_tokens.append(running_task.api_key)
            return SimulationResult(
                run_id=running_task.id,
                backend="aiperf",
                backend_version="0.12.0",
                summary={},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=[],
            )

        monkeypatch.setattr(
            simulation_service,
            "get_backend",
            lambda _name: SimpleNamespace(
                validate=AsyncMock(),
                run=fake_run,
                artifact_records=lambda *_a, **_kw: [],
                descriptor=lambda: SimpleNamespace(version="0.12.0"),
            ),
        )
        await simulation_service._run_task(reloaded)
        assert reloaded.status == "completed"
        assert len(seen_run_tokens) == 1
        run_tok = seen_run_tokens[0]
        assert run_tok is not None and run_tok.startswith(DELEGATED_TOKEN_PREFIX)
        assert run_tok != probe_keys[0]
        assert ctx["model_svc"].tokens.validate(run_tok) is not None
        assert reloaded.api_key is None

        # backend.run failure still clears task.api_key in finally
        async def failing_run(running_task: SimulationTask, _context: RunContext):
            assert running_task.api_key is not None
            raise SimulationConfigurationError("boom during run")

        failing_task = await create_task(req_unauth, http_request=_fake_http_request(ctx["principal_a"]))
        monkeypatch.setattr(
            simulation_service,
            "get_backend",
            lambda _name: SimpleNamespace(
                validate=AsyncMock(),
                run=failing_run,
                artifact_records=lambda *_a, **_kw: [],
                descriptor=lambda: SimpleNamespace(version="0.12.0"),
            ),
        )
        await simulation_service._run_task(failing_task)
        assert failing_task.status == "failed"
        assert failing_task.api_key is None

        # SimulationCancelledError in _run_task still clears task.api_key in finally
        async def cancelled_run(running_task: SimulationTask, _context: RunContext):
            assert running_task.api_key is not None
            raise SimulationCancelledError("cancelled during run")

        cancelled_task = await create_task(req_unauth, http_request=_fake_http_request(ctx["principal_a"]))
        monkeypatch.setattr(
            simulation_service,
            "get_backend",
            lambda _name: SimpleNamespace(
                validate=AsyncMock(),
                run=cancelled_run,
                artifact_records=lambda *_a, **_kw: [],
                descriptor=lambda: SimpleNamespace(version="0.12.0"),
            ),
        )
        await simulation_service._run_task(cancelled_task)
        assert cancelled_task.status == "cancelled"
        assert cancelled_task.api_key is None

        # Cancelling a queued legacy task with stop_task clears task.api_key
        legacy_queued = task_record(simulation_root, "abcd9988", "queued").model_copy(
            update={"api_key": "lens-mk-legacy-queued"}
        )
        simulation_service.tasks[legacy_queued.id] = legacy_queued
        await save_task(legacy_queued)
        await stop_task(legacy_queued.id)
        stopped_queued = await load_task(legacy_queued.id)
        assert stopped_queued is not None
        assert stopped_queued.status == "cancelled"
        assert stopped_queued.api_key is None

        # Revoking token while queued fails _run_task cleanly without calling backend.run
        queued_task = await create_task(req_unauth, http_request=_fake_http_request(ctx["principal_a"]))
        assert ctx["model_svc"].tokens.revoke(ctx["user_a"].id, ctx["token_a"].id) is True
        await simulation_service._run_task(queued_task)
        assert queued_task.status == "failed"
        assert queued_task.api_key is None
        assert queued_task.error_message is not None and "not found or is inactive" in queued_task.error_message
    finally:
        reset_settings_override()


@pytest.mark.asyncio
async def test_rerun_task_enforces_token_ownership_and_never_inherits_foreign_token(simulation_root, monkeypatch):
    import types

    from fastapi import HTTPException

    from llm_d_lens.auth.settings import reset_settings_override
    from llm_d_lens.model_service.contracts import DELEGATED_TOKEN_PREFIX

    ctx = _setup_local_users_and_tokens(monkeypatch, simulation_root)
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")

    probe_keys: list[str | None] = []

    async def fake_discover(endpoint_url: str, *, api_key: str | None = None):
        probe_keys.append(api_key)
        return {"endpoint": endpoint_url, "models": ["model"]}

    monkeypatch.setattr(simulation_service, "discover_models", fake_discover)
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    try:
        # 1. External task owned by User A
        ext_req = SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://endpoint.local",
            model_name="model",
            model_access_token_id=ctx["token_a"].id,
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
        )
        ext_task = await create_task(ext_req, http_request=_fake_http_request(ctx["principal_a"]))
        ext_task.status = "completed"
        await save_task(ext_task)

        # Same owner (User A) inherits token_a and mints fresh probe token
        rerun_same = await rerun_task(ext_task.id, http_request=_fake_http_request(ctx["principal_a"]))
        assert rerun_same.model_access_token_id == ctx["token_a"].id
        assert rerun_same.owner_user_id == ctx["user_a"].id
        assert rerun_same.api_key is None
        assert probe_keys[-1] is not None and probe_keys[-1].startswith(DELEGATED_TOKEN_PREFIX)

        # Different user (User B) rerunning external task without override -> NEVER inherits User A's token
        rerun_other_ext = await rerun_task(ext_task.id, http_request=_fake_http_request(ctx["principal_b"]))
        assert rerun_other_ext.model_access_token_id is None
        assert rerun_other_ext.api_key is None
        assert rerun_other_ext.owner_user_id == ctx["user_b"].id
        assert probe_keys[-1] is None

        # User B trying to override with User A's token -> rejected
        with pytest.raises(SimulationConfigurationError, match="not found or is inactive"):
            await rerun_task(
                ext_task.id,
                override=SimulationTaskRerunRequest(model_access_token_id=ctx["token_a"].id),
                http_request=_fake_http_request(ctx["principal_b"]),
            )

        # User B overriding with User B's own token -> succeeds
        rerun_other_own = await rerun_task(
            ext_task.id,
            override=SimulationTaskRerunRequest(model_access_token_id=ctx["token_b"].id),
            http_request=_fake_http_request(ctx["principal_b"]),
        )
        assert rerun_other_own.model_access_token_id == ctx["token_b"].id
        assert rerun_other_own.owner_user_id == ctx["user_b"].id

        # None / auth-disabled caller supplying override.model_access_token_id -> 401
        with pytest.raises(HTTPException) as unauth_rerun_exc:
            await rerun_task(
                ext_task.id,
                override=SimulationTaskRerunRequest(model_access_token_id=ctx["token_b"].id),
                http_request=None,
            )
        assert unauth_rerun_exc.value.status_code == 401

        # 2. Model Service task owned by User A: User B cannot rerun without supplying own token/api_key
        group = ModelServiceGroup(id="msg-rerun-1", name="qwen3-prod", model_ref="Qwen/Qwen3-0.6B")
        member = types.SimpleNamespace(execution_id="exec-1")

        class _FakeGroups:
            def get(self, group_id):
                return group if group_id == "msg-rerun-1" else None

        class _FakeModelService:
            groups = _FakeGroups()
            tokens = ctx["model_svc"].tokens

            def authorized_members(self, _group, _principal):
                return [member]

        monkeypatch.setattr("llm_d_lens.model_service.service.default_service", lambda: _FakeModelService())
        exec_ctx = types.SimpleNamespace(
            endpoint="http://vllm.my-namespace.svc:8000",
            namespace="my-namespace",
            cluster_id="cluster-123",
            display_name="qwen3-0.6b",
        )
        monkeypatch.setattr("llm_d_lens.deploy.executions.get_execution_context", lambda _eid: exec_ctx)
        monkeypatch.setattr(
            simulation_service, "_cluster_gateway_endpoint", AsyncMock(return_value="http://gateway.svc:80")
        )

        ms_req = SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://model-service.internal",
            model_name="placeholder",
            model_service_group_id="msg-rerun-1",
            model_access_token_id=ctx["token_a"].id,
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
        )
        ms_task = await create_task(ms_req, http_request=_fake_http_request(ctx["principal_a"]))
        ms_task.status = "completed"
        await save_task(ms_task)

        with pytest.raises(SimulationConfigurationError, match="Select one of your active model access tokens"):
            await rerun_task(ms_task.id, http_request=_fake_http_request(ctx["principal_b"]))

        ms_rerun_b = await rerun_task(
            ms_task.id,
            override=SimulationTaskRerunRequest(model_access_token_id=ctx["token_b"].id),
            http_request=_fake_http_request(ctx["principal_b"]),
        )
        assert ms_rerun_b.model_access_token_id == ctx["token_b"].id
        assert ms_rerun_b.owner_user_id == ctx["user_b"].id

        # Rerun with override.api_key clears inherited model_access_token_id
        ms_rerun_raw_key = await rerun_task(
            ms_task.id,
            override=SimulationTaskRerunRequest(api_key="lens-mk-override-raw"),
            http_request=_fake_http_request(ctx["principal_a"]),
        )
        assert ms_rerun_raw_key.api_key == "lens-mk-override-raw"
        assert ms_rerun_raw_key.model_access_token_id is None

        # Legacy Model Service task (model_access_token_id=None, model_service_group_id set) rejects empty rerun
        legacy_ms_task = ms_task.model_copy(
            update={
                "id": "abcd7766",
                "task_dir": str(simulation_service.task_root() / "abcd7766"),
                "model_access_token_id": None,
                "status": "completed",
            }
        )
        await save_task(legacy_ms_task)
        with pytest.raises(SimulationConfigurationError, match="Select one of your active model access tokens"):
            await rerun_task(legacy_ms_task.id, http_request=_fake_http_request(ctx["principal_a"]))
        legacy_ms_rerun = await rerun_task(
            legacy_ms_task.id,
            override=SimulationTaskRerunRequest(model_access_token_id=ctx["token_a"].id),
            http_request=_fake_http_request(ctx["principal_a"]),
        )
        assert legacy_ms_rerun.model_access_token_id == ctx["token_a"].id

        # Legacy deployment task rebound via override.model_service_group_id rejects empty rerun, succeeds with token
        legacy_dep_task = task_record(simulation_root, "abcd7755", "completed").model_copy(
            update={
                "endpoint_mode": "deployment",
                "endpoint_deployment_execution_id": "exec-1",
                "model_service_group_id": None,
                "model_access_token_id": None,
            }
        )
        await save_task(legacy_dep_task)
        with pytest.raises(SimulationConfigurationError, match="Select one of your active model access tokens"):
            await rerun_task(
                legacy_dep_task.id,
                override=SimulationTaskRerunRequest(model_service_group_id="msg-rerun-1"),
                http_request=_fake_http_request(ctx["principal_a"]),
            )
        rebound_ms_rerun = await rerun_task(
            legacy_dep_task.id,
            override=SimulationTaskRerunRequest(
                model_service_group_id="msg-rerun-1",
                model_access_token_id=ctx["token_a"].id,
            ),
            http_request=_fake_http_request(ctx["principal_a"]),
        )
        assert rebound_ms_rerun.model_service_group_id == "msg-rerun-1"
        assert rebound_ms_rerun.model_access_token_id == ctx["token_a"].id

        # Same-owner rerun of Model Service task after revoking token_a fails cleanly
        assert ctx["model_svc"].tokens.revoke(ctx["user_a"].id, ctx["token_a"].id) is True
        with pytest.raises(SimulationConfigurationError, match="Select one of your active model access tokens"):
            await rerun_task(ms_task.id, http_request=_fake_http_request(ctx["principal_a"]))
    finally:
        reset_settings_override()


@pytest.mark.asyncio
async def test_hardware_backfill_cache_filtering_facets_and_search(simulation_root, monkeypatch):
    import yaml

    from llm_d_lens.dashboard.collector import CacheEntry, default_collector, reset_collector
    from llm_d_lens.simulation.service import get_task

    reset_collector()
    try:
        # 1. Task with server-telemetry.json accelerator_summary (model extracted)
        t_srv = task_record(simulation_root, "aa110001", "completed")
        t_srv_report = Path(t_srv.task_dir) / "artifacts" / "report"
        t_srv_report.mkdir(parents=True, exist_ok=True)
        (t_srv_report / "server-telemetry.json").write_text(
            json.dumps(
                {
                    "status": "available",
                    "accelerator_summary": "Intel Arc Pro B60",
                    "hardware_profiles": [
                        {
                            "id": "intel-xpu",
                            "display_name": "Intel GPU",
                            "models": ["Intel Arc Pro B60"],
                            "allocated_accelerators": 2,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        await save_task(t_srv)

        # 2. Historical task with run-manifest.yaml only (no server-telemetry.json)
        t_manifest = task_record(simulation_root, "aa110002", "completed")
        t_manifest_report = Path(t_manifest.task_dir) / "artifacts" / "report"
        t_manifest_report.mkdir(parents=True, exist_ok=True)
        (t_manifest_report / "run-manifest.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "hardware_profile": {
                        "id": "nvidia",
                        "display_name": "NVIDIA GPU",
                        "vendor": "nvidia",
                    },
                }
            ),
            encoding="utf-8",
        )
        await save_task(t_manifest)

        # 3. Task with endpoint_cluster_id resolved from fast in-memory cluster overview cache
        t_cluster = task_record(simulation_root, "aa110003", "completed").model_copy(
            update={"endpoint_mode": "deployment", "endpoint_cluster_id": "clus0001"}
        )
        default_collector()._overview["clus0001"] = CacheEntry(
            value={"hardware": {"accelerators": [{"id": "nvidia", "gpuCount": 4, "models": ["NVIDIA H100 80GB HBM3"]}]}}
        )
        await save_task(t_cluster)

        # 4. External URL task without hardware telemetry -> hardware remains None
        t_ext = task_record(simulation_root, "aa110004", "completed").model_copy(
            update={"endpoint_mode": "external", "endpoint_url": "https://api.external.example/v1"}
        )
        await save_task(t_ext)

        # Verify get_task and list_tasks return enriched hardware and populate facets
        fetched_srv = await get_task(t_srv.id)
        fetched_manifest = await get_task(t_manifest.id)
        fetched_cluster = await get_task(t_cluster.id)
        fetched_ext = await get_task(t_ext.id)
        assert fetched_srv is not None and fetched_srv.hardware == "Intel Arc Pro B60"
        assert fetched_manifest is not None and fetched_manifest.hardware == "NVIDIA GPU"
        assert fetched_cluster is not None and fetched_cluster.hardware == "NVIDIA H100 80GB HBM3"
        assert fetched_ext is not None and fetched_ext.hardware is None

        # Verify in-memory cache keyed by (task.id, task.status) prevents repeated disk reads
        read_calls = 0
        original_resolve = simulation_service._resolve_task_hardware

        def counting_resolve(task):
            nonlocal read_calls
            read_calls += 1
            return original_resolve(task)

        monkeypatch.setattr(simulation_service, "_resolve_task_hardware", counting_resolve)
        cached_again = await get_task(t_srv.id)
        assert cached_again is not None and cached_again.hardware == "Intel Arc Pro B60"
        assert read_calls == 0

        # Status change triggers fresh resolution for new (task.id, task.status) key
        t_srv.status = "failed"
        resolved_after_status_change = simulation_service._enrich_task_hardware(t_srv)
        assert resolved_after_status_change.hardware == "Intel Arc Pro B60"
        assert read_calls == 1

        # Active (running/queued) tasks must NOT sticky-cache None before telemetry/cluster overview warms up
        t_running = task_record(simulation_root, "aa110005", "running").model_copy(
            update={"endpoint_mode": "deployment", "endpoint_cluster_id": "clus0002"}
        )
        await save_task(t_running)
        simulation_service.tasks[t_running.id] = t_running
        simulation_service.runners[t_running.id] = asyncio.create_task(asyncio.sleep(10))
        try:
            before_warm = await get_task(t_running.id)
            assert before_warm is not None and before_warm.status == "running" and before_warm.hardware is None
            default_collector()._overview["clus0002"] = CacheEntry(
                value={
                    "hardware": {"accelerators": [{"id": "intel-xpu", "gpuCount": 2, "models": ["Intel Arc Pro B60"]}]}
                }
            )
            after_warm = await get_task(t_running.id)
            assert (
                after_warm is not None and after_warm.status == "running" and after_warm.hardware == "Intel Arc Pro B60"
            )
        finally:
            runner_task = simulation_service.runners.pop(t_running.id, None)
            if runner_task is not None:
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)

        # Verify API filtering, facets.hardware, and search haystack
        with TestClient(app) as client:
            all_page = client.get("/api/simulation/tasks?limit=20").json()
            assert all_page["facets"]["hardware"] == [
                "Intel Arc Pro B60",
                "NVIDIA GPU",
                "NVIDIA H100 80GB HBM3",
            ]

            filtered_page = client.get("/api/simulation/tasks?hardware=intel%20arc%20pro%20b60").json()
            assert filtered_page["total"] == 2
            assert {item["id"] for item in filtered_page["tasks"]} == {t_srv.id, t_running.id}
            assert all(item["hardware"] == "Intel Arc Pro B60" for item in filtered_page["tasks"])

            search_page = client.get("/api/simulation/tasks?search=H100").json()
            assert search_page["total"] == 1
            assert search_page["tasks"][0]["id"] == t_cluster.id
    finally:
        reset_collector()


@pytest.mark.asyncio
async def test_random_seed_defaults_zero_preservation_validation_command_and_rerun(simulation_root, monkeypatch):
    from llm_d_lens.simulation.backends.aiperf import DEFAULT_RANDOM_SEED

    assert DEFAULT_RANDOM_SEED == 666
    assert AIPerfBackend.DEFAULT_RANDOM_SEED == 666

    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    backend = AIPerfBackend()

    # 1. Omitted random_seed and random_seed=None normalize & persist to 666 and pass --random-seed 666
    for options in ({}, {"random_seed": None}):
        req = SimulationTaskCreateRequest(
            scenario="chat",
            endpoint_url="http://127.0.0.1:8000",
            model_name="model",
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
            backend_options=options,
        )
        created = await create_task(req)
        assert created.simulation.backend == "aiperf"
        assert created.simulation.backend_options["random_seed"] == 666
        persisted = SimulationTaskDao().get(created.id)
        assert persisted is not None
        assert persisted.simulation.backend_options["random_seed"] == 666
        args = (await backend.command(created)).args
        assert args[args.index("--random-seed") + 1] == "666"

    # 2. Explicit random_seed=0 (falsy zero) and random_seed=42 persist & emit exact --random-seed
    for explicit_seed in (0, 42):
        req = SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="model",
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
            backend_options={"random_seed": explicit_seed},
        )
        created = await create_task(req)
        assert created.simulation.backend_options["random_seed"] == explicit_seed
        persisted = SimulationTaskDao().get(created.id)
        assert persisted is not None
        assert persisted.simulation.backend_options["random_seed"] == explicit_seed
        args = (await backend.command(created)).args
        assert args[args.index("--random-seed") + 1] == str(explicit_seed)

    # 3. Direct AIPerfBackend.command() on a legacy task without random_seed still passes --random-seed 666
    legacy_aiperf = task_record(simulation_root, "abcd9001")
    update_trace(legacy_aiperf, path=trace.name)
    legacy_args = (await backend.command(legacy_aiperf)).args
    assert legacy_args[legacy_args.index("--random-seed") + 1] == "666"

    # 4. Invalid random_seed values are rejected
    for bad_seed in (-1, True, False, 1.5, "666"):
        bad_req = SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="model",
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
            backend_options={"random_seed": bad_seed},
        )
        with pytest.raises(SimulationConfigurationError, match="random_seed"):
            await create_task(bad_req)

        bad_task = task_record(simulation_root, "abcd9002")
        update_trace(bad_task, path=trace.name)
        update_simulation(bad_task, backend_options={"random_seed": bad_seed})
        with pytest.raises(SimulationConfigurationError, match="random_seed"):
            await backend.validate(bad_task)

    # 5. rerun_task() normalizes older aiperf tasks without random_seed to 666, and preserves 0 / 42
    await save_task(legacy_aiperf)
    rerun_default = await rerun_task(legacy_aiperf.id)
    assert rerun_default.simulation.backend_options["random_seed"] == 666

    for preserved_seed in (0, 42):
        seeded_source = task_record(simulation_root, f"abcd90{preserved_seed:02d}")
        update_trace(seeded_source, path=trace.name)
        update_simulation(seeded_source, backend_options={"random_seed": preserved_seed})
        await save_task(seeded_source)
        rerun_seeded = await rerun_task(seeded_source.id)
        assert rerun_seeded.simulation.backend_options["random_seed"] == preserved_seed


def test_create_api_defaults_backend_to_aiperf_and_rejects_trace_replayer(simulation_root, monkeypatch):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/simulation/tasks",
            json={
                "name": "Retired Backend",
                "scenario": "chat",
                "backend": "trace-replayer",
                "endpoint_url": "http://127.0.0.1:8000",
                "model_name": "model",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": trace.name,
            },
        )
        assert rejected.status_code == 422

        accepted = client.post(
            "/api/simulation/tasks",
            json={
                "name": "Default Backend",
                "scenario": "chat",
                "endpoint_url": "http://127.0.0.1:8000",
                "model_name": "model",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": trace.name,
            },
        )
        assert accepted.status_code == 202, accepted.text
        payload = accepted.json()
        assert payload["task"]["simulation"]["backend"] == "aiperf"
        assert payload["task"]["simulation"]["backend_options"]["random_seed"] == 666
        persisted = SimulationTaskDao().get(payload["task_id"])
        assert persisted is not None
        assert persisted.simulation.backend == "aiperf"
        assert persisted.simulation.backend_options["random_seed"] == 666


@pytest.mark.asyncio
async def test_historical_trace_replayer_task_service_and_api_safety(simulation_root):
    from llm_d_lens.simulation.reporting import builder as report_builder
    from llm_d_lens.simulation.service import get_response_code_issues, get_task, resolve_task_artifact
    from llm_d_lens.simulation.telemetry_post import FORENSICS_SCHEMA_VERSION

    task = task_record(simulation_root, "7777bbbb", "completed").model_copy(
        update={
            "simulation": SimulationConfig(
                backend="trace-replayer",
                backend_options={"threads": 16, "num_producer": 8},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
            "result": SimulationResult(
                run_id="7777bbbb",
                backend="trace-replayer",
                backend_version=None,
                summary={},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=[],
            ),
        }
    )
    task_dir = Path(task.task_dir)
    report_dir = task_dir / "artifacts" / "report"
    tr_dir = task_dir / "artifacts" / "trace-replayer"
    report_dir.mkdir(parents=True, exist_ok=True)
    tr_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "index.html").write_text(
        f"<html>{report_builder.REPORT_TEMPLATE_VERSION_MARKER}</html>",
        encoding="utf-8",
    )
    (report_dir / "forensics.json").write_text(
        json.dumps({"schema_version": FORENSICS_SCHEMA_VERSION, "verdict": {"code": "healthy"}}),
        encoding="utf-8",
    )
    (tr_dir / "requests.jsonl").write_text('{"status": 200, "s_time": 0, "e_time": 100}\n', encoding="utf-8")
    (tr_dir / "stdout.log").write_text("legacy stdout\n", encoding="utf-8")
    (tr_dir / "stderr.log").write_text("legacy stderr\n", encoding="utf-8")

    SimulationTaskDao().save(task)
    reset_service_state()

    loaded = await load_task(task.id)
    assert loaded is not None and loaded.simulation.backend == "trace-replayer"

    listed = await list_tasks(backend="trace-replayer", limit=10)
    assert [item.id for item in listed.tasks] == [task.id]

    fetched = await get_task(task.id)
    assert fetched is not None and fetched.simulation.backend == "trace-replayer"

    issues_slice = await get_response_code_issues(task.id)
    assert issues_slice.total == 0
    assert issues_slice.items == []

    for kind in ("report_html", "forensics", "per_request", "stdout", "stderr"):
        resolved_path, _, _ = await resolve_task_artifact(loaded, kind)
        assert resolved_path.is_file()

    with pytest.raises(SimulationConfigurationError, match="no longer supported"):
        await rerun_task(task.id)

    with TestClient(app) as client:
        assert client.get(f"/api/simulation/tasks/{task.id}").status_code == 200
        issues_resp = client.get(f"/api/simulation/tasks/{task.id}/response-code-issues")
        assert issues_resp.status_code == 200
        assert issues_resp.json()["total"] == 0
        for kind in ("report_html", "forensics", "per_request", "stdout", "stderr"):
            assert client.get(f"/api/simulation/tasks/{task.id}/artifacts/{kind}").status_code == 200
        rerun_resp = client.post(f"/api/simulation/tasks/{task.id}/rerun")
        assert rerun_resp.status_code == 400
        del_resp = client.delete(f"/api/simulation/tasks/{task.id}")
        assert del_resp.status_code == 200
    assert await load_task(task.id) is None


@pytest.mark.asyncio
async def test_aiperf_014_always_on_flags_stream_validation_and_legacy_rerun(simulation_root, monkeypatch):
    assert AIPerfBackend.VERSION == "0.14.0"
    backend = AIPerfBackend()
    assert backend.descriptor().version == "0.14.0"

    mooncake = _write_aiperf_file_trace(simulation_root, "mooncake_trace")
    bailian = _write_aiperf_file_trace(simulation_root, "bailian_trace")
    baseten = _write_aiperf_file_trace(simulation_root, "baseten_trace")
    burst = _write_aiperf_file_trace(simulation_root, "burst_gpt_trace")

    # 1. validate_backend requires stream=True
    non_stream_task = task_record(simulation_root, "e1050001")
    update_trace(non_stream_task, path=mooncake.name, format="mooncake_trace", fixed_schedule=True)
    update_simulation(non_stream_task, stream=False)
    with pytest.raises(SimulationConfigurationError, match="stream=True"):
        await backend.validate(non_stream_task)

    # 2. rerun_task coerces stream=False on legacy aiperf tasks to stream=True and does not auto-fill trajectory ratios
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)
    update_simulation(
        non_stream_task,
        backend_options={"random_seed": 7, "trajectory_start_min_ratio": None, "trajectory_start_max_ratio": ""},
    )
    await save_task(non_stream_task)
    rerun = await rerun_task(non_stream_task.id)
    assert rerun.simulation.stream is True
    assert rerun.simulation.backend_options["random_seed"] == 7
    assert "trajectory_start_min_ratio" not in rerun.simulation.backend_options
    assert "trajectory_start_max_ratio" not in rerun.simulation.backend_options

    # 3. Always emits --streaming, --use-server-token-count, and --slice-duration 1.0 across all trace formats
    cases = [
        (mooncake.name, "mooncake_trace", True, {}),
        (mooncake.name, "mooncake_trace", False, {"concurrency": 2}),
        (bailian.name, "bailian_trace", True, {}),
        (baseten.name, "baseten_trace", False, {"concurrency": 2}),
        (burst.name, "burst_gpt_trace", True, {}),
        ("semianalysis_cc_traces_weka_no_subagents", "weka_public_dataset", False, {"concurrency": 2}),
        ("semianalysis_cc_traces_weka_with_subagents", "weka_public_dataset", False, {"concurrency": 2}),
    ]
    for path_val, fmt, fixed, opts in cases:
        t = task_record(simulation_root, "e1050002")
        update_trace(t, path=path_val, format=fmt, fixed_schedule=fixed)
        update_simulation(t, stream=True, backend_options=opts)
        await backend.validate(t)
        args = (await backend.command(t)).args
        assert "--streaming" in args
        assert "--use-server-token-count" in args
        assert args[args.index("--slice-duration") + 1] == "1.0"


@pytest.mark.asyncio
async def test_aiperf_014_inferencex_agentx_mvp_scenario_and_unsafe_override(simulation_root):
    backend = AIPerfBackend()
    mooncake = _write_aiperf_file_trace(simulation_root, "mooncake_trace")
    bailian = _write_aiperf_file_trace(simulation_root, "bailian_trace")
    baseten = _write_aiperf_file_trace(simulation_root, "baseten_trace")
    burst = _write_aiperf_file_trace(simulation_root, "burst_gpt_trace")

    # Compatible Weka subagent datasets: --scenario enabled; --unsafe-override only when < 900s
    for weka_dataset in (
        "semianalysis_cc_traces_weka_with_subagents",
        "semianalysis_cc_traces_weka_062126_256k",
    ):
        for duration, expect_unsafe in ((60, True), (899, True), (900, False), (1800, False)):
            task = task_record(simulation_root, "e1050003")
            update_trace(task, path=weka_dataset, format="weka_public_dataset", fixed_schedule=False)
            update_simulation(task, duration_seconds=duration, backend_options={"concurrency": 4})
            await backend.validate(task)
            args = (await backend.command(task)).args
            assert args[args.index("--scenario") + 1] == "inferencex-agentx-mvp"
            assert ("--unsafe-override" in args) is expect_unsafe

    # Weka without subagents: NO --scenario, NO --unsafe-override
    weka_no_sub = task_record(simulation_root, "e1050004")
    update_trace(
        weka_no_sub,
        path="semianalysis_cc_traces_weka_no_subagents",
        format="weka_public_dataset",
        fixed_schedule=False,
    )
    update_simulation(weka_no_sub, duration_seconds=60, backend_options={"concurrency": 4})
    await backend.validate(weka_no_sub)
    no_sub_args = (await backend.command(weka_no_sub)).args
    assert "--scenario" not in no_sub_args
    assert "--unsafe-override" not in no_sub_args

    # Mooncake in concurrency mode: --scenario enabled AND --unsafe-override ALWAYS emitted (even >= 900s)
    for fixed, opts, duration in (
        (False, {}, 60),
        (False, {"concurrency": 8}, 900),
        (True, {"concurrency": 2}, 1800),
    ):
        mc = task_record(simulation_root, "e1050005")
        update_trace(mc, path=mooncake.name, format="mooncake_trace", fixed_schedule=fixed)
        update_simulation(mc, duration_seconds=duration, backend_options=opts)
        await backend.validate(mc)
        mc_args = (await backend.command(mc)).args
        assert mc_args[mc_args.index("--scenario") + 1] == "inferencex-agentx-mvp"
        assert "--unsafe-override" in mc_args

    # Mooncake fixed-schedule and other trace formats (even concurrency): NO --scenario, NO --unsafe-override
    for path_val, fmt, fixed, opts in (
        (mooncake.name, "mooncake_trace", True, {}),
        (bailian.name, "bailian_trace", True, {}),
        (bailian.name, "bailian_trace", False, {"concurrency": 4}),
        (baseten.name, "baseten_trace", False, {"concurrency": 4}),
        (burst.name, "burst_gpt_trace", False, {"concurrency": 4}),
    ):
        other = task_record(simulation_root, "e1050006")
        update_trace(other, path=path_val, format=fmt, fixed_schedule=fixed)
        update_simulation(other, duration_seconds=60, backend_options=opts)
        await backend.validate(other)
        other_args = (await backend.command(other)).args
        assert "--scenario" not in other_args
        assert "--unsafe-override" not in other_args


@pytest.mark.asyncio
async def test_aiperf_014_trajectory_start_ratios_normalization_validation_and_cli(simulation_root):
    backend = AIPerfBackend()
    mooncake = _write_aiperf_file_trace(simulation_root, "mooncake_trace")
    bailian = _write_aiperf_file_trace(simulation_root, "bailian_trace")

    # 1. Normalization: never auto-fills 0.25/0.75; pops None/empty; preserves 0 / 0.0
    assert backend.normalize_backend_options({}) == {"random_seed": 666}
    assert backend.normalize_backend_options(
        {"trajectory_start_min_ratio": None, "trajectory_start_max_ratio": "  "}
    ) == {"random_seed": 666}
    assert backend.normalize_backend_options({"trajectory_start_min_ratio": 0, "trajectory_start_max_ratio": 0.0}) == {
        "random_seed": 666,
        "trajectory_start_min_ratio": 0,
        "trajectory_start_max_ratio": 0.0,
    }

    # 2. All 4 CLI ratio combinations on compatible mooncake concurrency
    for opts, exp_min, exp_max in (
        ({"concurrency": 4, "trajectory_start_min_ratio": 0.25, "trajectory_start_max_ratio": 0.75}, "0.25", "0.75"),
        ({"concurrency": 4, "trajectory_start_min_ratio": 0, "trajectory_start_max_ratio": 0.0}, "0", "0.0"),
        ({"concurrency": 4, "trajectory_start_min_ratio": 0.8}, "0.8", None),
        ({"concurrency": 4, "trajectory_start_max_ratio": 0.1}, None, "0.1"),
        ({"concurrency": 4}, None, None),
    ):
        t = task_record(simulation_root, "e1050007")
        update_trace(t, path=mooncake.name, format="mooncake_trace", fixed_schedule=False)
        update_simulation(t, backend_options=opts)
        await backend.validate(t)
        args = (await backend.command(t)).args
        if exp_min is not None:
            assert args[args.index("--trajectory-start-min-ratio") + 1] == exp_min
        else:
            assert "--trajectory-start-min-ratio" not in args
        if exp_max is not None:
            assert args[args.index("--trajectory-start-max-ratio") + 1] == exp_max
        else:
            assert "--trajectory-start-max-ratio" not in args

    # 3. Validation errors: bad types, out of range, non-finite, min > max, incompatible workloads
    for bad_opts in (
        {"concurrency": 4, "trajectory_start_min_ratio": True},
        {"concurrency": 4, "trajectory_start_max_ratio": False},
        {"concurrency": 4, "trajectory_start_min_ratio": "0.25"},
        {"concurrency": 4, "trajectory_start_min_ratio": None},
        {"concurrency": 4, "trajectory_start_min_ratio": -0.01},
        {"concurrency": 4, "trajectory_start_max_ratio": 1.01},
        {"concurrency": 4, "trajectory_start_min_ratio": float("nan")},
        {"concurrency": 4, "trajectory_start_max_ratio": float("inf")},
        {"concurrency": 4, "trajectory_start_min_ratio": 0.8, "trajectory_start_max_ratio": 0.2},
    ):
        bad = task_record(simulation_root, "e1050008")
        update_trace(bad, path=mooncake.name, format="mooncake_trace", fixed_schedule=False)
        update_simulation(bad, backend_options=bad_opts)
        with pytest.raises(SimulationConfigurationError, match="trajectory_start_"):
            await backend.validate(bad)

    for path_val, fmt, fixed, opts in (
        (mooncake.name, "mooncake_trace", True, {"trajectory_start_min_ratio": 0.25}),
        (bailian.name, "bailian_trace", False, {"concurrency": 4, "trajectory_start_max_ratio": 0.75}),
        (
            "semianalysis_cc_traces_weka_no_subagents",
            "weka_public_dataset",
            False,
            {"concurrency": 4, "trajectory_start_min_ratio": 0.0},
        ),
    ):
        incompat = task_record(simulation_root, "e1050009")
        update_trace(incompat, path=path_val, format=fmt, fixed_schedule=fixed)
        update_simulation(incompat, backend_options=opts)
        with pytest.raises(SimulationConfigurationError, match="inferencex-agentx-mvp"):
            await backend.validate(incompat)


@pytest.mark.asyncio
async def test_aiperf_014_parse_timeslices_checkpoints_warmup_and_server_tokens(simulation_root):
    from llm_d_lens.simulation.service import resolve_task_artifact

    backend = AIPerfBackend()
    task = task_record(simulation_root, "e105000a")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    checkpoints = artifacts / "checkpoints"
    nested = artifacts / "run_001"
    checkpoints.mkdir(parents=True)
    nested.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")

    # Checkpoint decoys sort alphabetically before profile_export* and must be ignored
    (checkpoints / "profile_export.jsonl").write_text(
        '{"metadata":{"benchmark_phase":"profiling"},"metrics":{}}\n', encoding="utf-8"
    )
    (checkpoints / "profile_export_aiperf.json").write_text('{"request_count":{"avg":999}}', encoding="utf-8")

    # Root summary with server token count totals (total_usage_*)
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "request_count": {"unit": "requests", "avg": 1},
                "error_request_count": {"unit": "requests", "avg": 0},
                "total_usage_prompt_tokens": {"unit": "tokens", "avg": 120},
                "total_usage_completion_tokens": {"unit": "tokens", "avg": 45},
                "request_latency": {"unit": "ms", "avg": 200},
                "time_to_first_token": {"unit": "ms", "avg": 40},
                "inter_token_latency": {"unit": "ms", "avg": 10},
            }
        ),
        encoding="utf-8",
    )
    # Timeslice artifacts registered when present
    (artifacts / "profile_export_aiperf_timeslices.json").write_text("[]", encoding="utf-8")
    (artifacts / "profile_export_aiperf_timeslices.csv").write_text("Timeslice\n", encoding="utf-8")

    # Nested non-checkpoint profile_export.jsonl (verifying direct root preferred AND checkpoints skipped when root missing)
    (nested / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "benchmark_phase": "profiling",
                    "phase_kind": "profiling",
                    "request_start_ns": 0,
                    "request_end_ns": 1_000_000_000,
                },
                "metrics": {"usage_prompt_tokens": {"value": 999}, "usage_completion_tokens": {"value": 999}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root_records = [
        # Warmup via phase_kind="warmup" even though benchmark_phase="profiling" -> must be excluded!
        {
            "metadata": {
                "request_id": "warmup-phase-kind",
                "benchmark_phase": "profiling",
                "phase_kind": "warmup",
                "request_start_ns": 100_000_000,
                "request_end_ns": 200_000_000,
            },
            "metrics": {"usage_prompt_tokens": {"value": 5000}, "usage_completion_tokens": {"value": 5000}},
        },
        # Real profiling record using usage_prompt_tokens / usage_completion_tokens fallbacks
        {
            "metadata": {
                "request_id": "prof-1",
                "benchmark_phase": "profiling",
                "phase_kind": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 2_000_000_000,
            },
            "metrics": {
                "request_latency": {"value": 200},
                "time_to_first_token": {"value": 40},
                "inter_token_latency": {"value": 10},
                "usage_prompt_tokens": {"value": 120},
                "usage_completion_tokens": {"value": 45},
            },
        },
    ]
    root_jsonl = artifacts / "profile_export.jsonl"
    root_jsonl.write_text("\n".join(json.dumps(r) for r in root_records) + "\n", encoding="utf-8")

    now = datetime.now(UTC)
    parsed_recs = backend.artifact_records(task)
    assert len(parsed_recs) == 1
    assert parsed_recs[0].request_id == "prof-1"
    assert parsed_recs[0].input_tokens == 120
    assert parsed_recs[0].output_tokens == 45

    res = await backend.parse(task, CommandResult(0, now, now, stdout, stderr), "0.14.0")
    assert res.summary["total_requests"] == 1
    assert res.summary["total_input_tokens"] == 120
    assert res.summary["total_output_tokens"] == 45
    assert res.summary["throughput_tps"] == pytest.approx(45.0)
    assert res.summary["input_throughput_tps"] == pytest.approx(120.0)
    assert res.summary["throughput_timeline"][0]["token_throughput_tps"] == pytest.approx(45.0)
    assert res.summary["throughput_timeline"][0]["output_tokens"] == pytest.approx(45.0)
    assert res.warnings == []
    artifact_kinds = {art.kind for art in res.artifacts}
    assert {"summary", "per_request", "timeslices_json", "timeslices_csv"} <= artifact_kinds

    # Remove root profile_export.jsonl -> service.resolve_task_artifact & backend must pick nested/profile_export.jsonl, NOT checkpoints/
    root_jsonl.unlink()
    resolved_path, _, _ = await resolve_task_artifact(task, "per_request")
    assert resolved_path == nested / "profile_export.jsonl"
    nested_recs = backend.artifact_records(task)
    assert len(nested_recs) == 1 and nested_recs[0].input_tokens == 999

    # Omitted usage warning when stream=True, successful > 0, and neither input nor output tokens recorded
    no_usage_task = task_record(simulation_root, "e105000b")
    nu_artifacts = Path(no_usage_task.task_dir) / "artifacts" / "aiperf"
    nu_artifacts.mkdir(parents=True)
    nu_stdout = nu_artifacts / "stdout.log"
    nu_stderr = nu_artifacts / "stderr.log"
    nu_stdout.write_text("", encoding="utf-8")
    nu_stderr.write_text("", encoding="utf-8")
    (nu_artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "request_count": {"unit": "requests", "avg": 2},
                "request_latency": {"unit": "ms", "avg": 100},
                "time_to_first_token": {"unit": "ms", "avg": 20},
                "inter_token_latency": {"unit": "ms", "avg": 5},
            }
        ),
        encoding="utf-8",
    )
    (nu_artifacts / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {"benchmark_phase": "profiling", "request_start_ns": 0, "request_end_ns": 1_000_000_000},
                "metrics": {"request_latency": {"value": 100}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nu_res = await backend.parse(no_usage_task, CommandResult(0, now, now, nu_stdout, nu_stderr), "0.14.0")
    assert any("omitted usage statistics" in warning for warning in nu_res.warnings)
