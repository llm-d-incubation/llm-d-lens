from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_d_lens.simulation.models import (
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationResult,
    SimulationTask,
    SimulationTrace,
)
from llm_d_lens.simulation.service import load_task, reset_service_state, save_task


@pytest.fixture
def simulation_root(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    data = tmp_path / "data"
    traces.mkdir()
    monkeypatch.setenv("TRACE_REPLAY_DATA_DIR", str(traces))
    monkeypatch.setenv("LENS_DATA_DIR", str(data))
    reset_service_state()
    yield tmp_path
    reset_service_state()
    shutil.rmtree(tmp_path, ignore_errors=True)


def task_record(root: Path, task_id: str) -> SimulationTask:
    from llm_d_lens.simulation.service import task_root

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"task {task_id}",
        description="",
        scenario="chat",
        status="completed",
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
            dataset=SimulationDataset(name="mooncake-arxiv", scenario="chat", tokenizer=None),
            trace=SimulationTrace(
                path="mooncake_trace.jsonl",
                format="mooncake_trace",
                fixed_schedule=True,
                timeout_seconds=900,
                synthesis_speedup_ratio=1,
            ),
        ),
        task_dir=str(task_root() / task_id),
        progress_percent=100,
        progress_message="completed",
        logs=["done"],
        result=None,
        error_message=None,
        created_at=now,
        started_at=None,
        execution_started_at=None,
        completed_at=now,
        owner_pid=None,
        owner_instance_id=None,
    )


@pytest.mark.asyncio
async def test_persistence_round_trip_rehydrates_per_request_and_result_extra(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult.model_validate(
        {
            "run_id": task.id,
            "backend": "aiperf",
            "backend_version": "0.12.0",
            "summary": {"throughput_tps": 7},
            "artifacts": [
                SimulationArtifact(
                    kind="summary",
                    path=str(Path(task.task_dir) / "artifacts" / "summary.json"),
                    media_type="application/json",
                )
            ],
            "per_request": [{"request_id": "request-1", "ttft_ms": 1.5}],
            "backend_metrics": {"custom_backend_metric": True},
            "warnings": [],
            "historical_result_extension": {"kept": True},
        }
    )

    await save_task(task)
    loaded = await load_task(task.id)

    assert loaded is not None
    assert loaded.result is not None
    assert loaded.result.per_request[0].request_id == "request-1"
    assert loaded.result.per_request[0].ttft_ms == 1.5
    assert loaded.result.model_extra["historical_result_extension"] == {"kept": True}


@pytest.mark.asyncio
async def test_persistence_round_trips_token_id_and_owner_without_polluting_backend_options(simulation_root):
    from llm_d_lens.auth.records import UserRecord
    from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
    from llm_d_lens.db.dao.user import UserDao
    from llm_d_lens.db.models.simulation_task import SimulationTaskRow

    # 1. Synthetic owner_user_id (no UserRow) must not trigger SQLite FK IntegrityError
    synthetic_task = task_record(simulation_root, "abcd1234").model_copy(
        update={
            "api_key": "lens-mk-never-persisted",
            "model_access_token_id": "mat-abc123",
            "owner_user_id": "synthetic-user-1",
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={"threads": 4},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )
    await save_task(synthetic_task)
    reset_service_state()
    loaded_synthetic = await load_task(synthetic_task.id)
    assert loaded_synthetic is not None
    assert loaded_synthetic.api_key is None
    assert loaded_synthetic.model_access_token_id == "mat-abc123"  # noqa: S105
    assert loaded_synthetic.owner_user_id == "synthetic-user-1"
    assert dict(loaded_synthetic.simulation.backend_options) == {"threads": 4}
    with SimulationTaskDao()._read_only() as session:
        row = session.get(SimulationTaskRow, synthetic_task.id)
        assert row is not None
        assert row.owner_user_id is None

    # 2. Real UserRow populates the indexed owner_user_id column too
    user = UserDao().create(UserRecord(username="real-sim-owner"))
    real_task = task_record(simulation_root, "abcd5678").model_copy(
        update={
            "model_access_token_id": "mat-xyz789",
            "owner_user_id": user.id,
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={"threads": 2},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )
    SimulationTaskDao().save(real_task)
    reset_service_state()
    loaded_real = await load_task(real_task.id)
    assert loaded_real is not None
    assert loaded_real.model_access_token_id == "mat-xyz789"  # noqa: S105
    assert loaded_real.owner_user_id == user.id
    assert dict(loaded_real.simulation.backend_options) == {"threads": 2}
    with SimulationTaskDao()._read_only() as session:
        row = session.get(SimulationTaskRow, real_task.id)
        assert row is not None
        assert row.owner_user_id == user.id


def test_request_models_reject_lens_reserved_backend_options_and_conflicting_tokens():
    from pydantic import ValidationError

    from llm_d_lens.simulation.models import (
        ModelDiscoveryRequest,
        SimulationTaskCreateRequest,
        SimulationTaskRerunRequest,
    )

    with pytest.raises(ValidationError, match="__lens_"):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
            backend_options={"__lens_model_access_token_id__": "mat-smuggled"},
        )

    with pytest.raises(ValidationError, match="not both"):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            api_key="lens-mk-raw",
            model_access_token_id="mat-123",  # noqa: S106
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
        )

    with pytest.raises(ValidationError, match="not both"):
        SimulationTaskRerunRequest(api_key="lens-mk-raw", model_access_token_id="mat-123")  # noqa: S106

    with pytest.raises(ValidationError, match="not both"):
        ModelDiscoveryRequest(
            endpoint_url="http://127.0.0.1:8000",
            api_key="lens-mk-raw",
            model_access_token_id="mat-123",  # noqa: S106
        )

    valid_group_req = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://127.0.0.1:8000",
        model_name="m",
        model_service_group_id="msg-1",
        model_access_token_id="mat-123",  # noqa: S106
        trace_dataset="mooncake-arxiv",
        trace_path="trace.jsonl",
    )
    assert valid_group_req.model_access_token_id == "mat-123"  # noqa: S105


@pytest.mark.asyncio
async def test_persistence_round_trips_hardware_via_internal_key_without_polluting_backend_options(simulation_root):
    from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
    from llm_d_lens.db.models.simulation_task import _INTERNAL_HARDWARE_KEY, SimulationTaskRow

    task = task_record(simulation_root, "beef1234").model_copy(
        update={
            "hardware": "Intel Arc Pro B60",
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={"concurrency": 8},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )
    await save_task(task)
    reset_service_state()

    loaded = await load_task(task.id)
    assert loaded is not None
    assert loaded.hardware == "Intel Arc Pro B60"
    assert dict(loaded.simulation.backend_options) == {"concurrency": 8}
    assert _INTERNAL_HARDWARE_KEY not in loaded.simulation.backend_options

    with SimulationTaskDao()._read_only() as session:
        row = session.get(SimulationTaskRow, task.id)
        assert row is not None
        assert row.simulation_backend_options[_INTERNAL_HARDWARE_KEY] == "Intel Arc Pro B60"

    # Spoofed __lens_hardware__ inside backend_options is rejected on request and stripped on persist/read
    from pydantic import ValidationError

    from llm_d_lens.simulation.models import SimulationTaskCreateRequest

    with pytest.raises(ValidationError, match="__lens_"):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
            backend_options={_INTERNAL_HARDWARE_KEY: "Spoofed GPU"},
        )

    spoofed_task = task_record(simulation_root, "beef5678").model_copy(
        update={
            "hardware": None,
            "simulation": SimulationConfig(
                backend="aiperf",
                backend_options={_INTERNAL_HARDWARE_KEY: "Spoofed GPU", "concurrency": 2},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
        }
    )
    await save_task(spoofed_task)
    reset_service_state()
    loaded_spoofed = await load_task(spoofed_task.id)
    assert loaded_spoofed is not None
    assert loaded_spoofed.hardware is None
    assert dict(loaded_spoofed.simulation.backend_options) == {"concurrency": 2}
    assert _INTERNAL_HARDWARE_KEY not in loaded_spoofed.simulation.backend_options


def test_create_request_defaults_to_aiperf_and_rejects_retired_trace_replayer():
    from pydantic import ValidationError

    from llm_d_lens.simulation.models import SimulationTaskCreateRequest

    default_req = SimulationTaskCreateRequest(
        scenario="chat",
        endpoint_url="http://127.0.0.1:8000",
        model_name="m",
        trace_dataset="mooncake-arxiv",
        trace_path="trace.jsonl",
    )
    assert default_req.backend == "aiperf"

    with pytest.raises(ValidationError):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="trace-replayer",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
        )


@pytest.mark.asyncio
async def test_historical_trace_replayer_row_and_random_seed_persistence(simulation_root):
    from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
    from llm_d_lens.simulation.service import delete_task, list_tasks

    hist_task = task_record(simulation_root, "7777aaaa").model_copy(
        update={
            "simulation": SimulationConfig(
                backend="trace-replayer",
                backend_options={"threads": 16},
                duration_seconds=60,
                num_requests=None,
                stream=True,
                warmup_enabled=False,
                grace_period_seconds=30,
            ),
            "result": SimulationResult(
                run_id="7777aaaa",
                backend="trace-replayer",
                backend_version="0.2.0",
                summary={"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=[],
            ),
        }
    )
    Path(hist_task.task_dir).mkdir(parents=True, exist_ok=True)
    SimulationTaskDao().save(hist_task)
    reset_service_state()

    loaded = await load_task(hist_task.id)
    assert loaded is not None
    assert loaded.simulation.backend == "trace-replayer"
    assert loaded.result is not None and loaded.result.backend == "trace-replayer"

    listed_all = await list_tasks(limit=20)
    assert any(item.id == hist_task.id for item in listed_all.tasks)
    listed_legacy = await list_tasks(backend="trace-replayer", limit=20)
    assert [item.id for item in listed_legacy.tasks] == [hist_task.id]

    for seed_idx, seed_val in enumerate((666, 0, 42)):
        seed_task = task_record(simulation_root, f"8888000{seed_idx}").model_copy(
            update={
                "simulation": SimulationConfig(
                    backend="aiperf",
                    backend_options={"random_seed": seed_val},
                    duration_seconds=60,
                    num_requests=None,
                    stream=True,
                    warmup_enabled=False,
                    grace_period_seconds=30,
                )
            }
        )
        await save_task(seed_task)
        reset_service_state()
        reloaded_seed = await load_task(seed_task.id)
        assert reloaded_seed is not None
        assert reloaded_seed.simulation.backend_options["random_seed"] == seed_val

    await delete_task(hist_task.id)
    assert await load_task(hist_task.id) is None
