"""Unit tests for live incremental simulation telemetry collection and polling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_d_lens.simulation import incluster, service, telemetry_live
from llm_d_lens.simulation.backends.aiperf import AIPerfBackend
from llm_d_lens.simulation.models import (
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationTask,
    SimulationTrace,
)
from llm_d_lens.simulation.telemetry_live import (
    _BUCKET_SAMPLE_CAP,
    _ERROR_RING_CAP,
    _MAX_BUCKETS,
    _RESERVOIR_CAP,
    _SNAPSHOT_SERIES_CAP,
    LiveTelemetryCollector,
    live_telemetry_registry,
)


def _make_task(
    task_id: str = "a1b2c3d4",
    *,
    status: str = "running",
    backend: str = "aiperf",
    endpoint_mode: str = "external",
    endpoint_cluster_id: str | None = None,
    endpoint_namespace: str | None = None,
    endpoint_url: str = "http://127.0.0.1:8000",
    duration_seconds: int = 60,
    backend_options: dict | None = None,
) -> SimulationTask:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        scenario="chat",
        status=status,
        task_dir=f"/tmp/{task_id}",
        progress_percent=0,
        progress_message="",
        logs=[],
        endpoint_url=endpoint_url,
        endpoint_mode=endpoint_mode,
        endpoint_cluster_id=endpoint_cluster_id,
        endpoint_namespace=endpoint_namespace,
        model_name="test-model",
        simulation=SimulationConfig(
            backend=backend,
            backend_options=backend_options or {},
            duration_seconds=duration_seconds,
            num_requests=None,
            stream=True,
            warmup_enabled=False,
            grace_period_seconds=30,
        ),
        prompt=SimulationPrompt(
            type="trace",
            dataset=SimulationDataset(name="bailian", scenario="chat", tokenizer=None),
            trace=SimulationTrace(
                path="trace.jsonl",
                format="bailian_trace",
                start_seconds=0.0,
                end_seconds=60.0,
                synthesis_speedup_ratio=1.0,
                fixed_schedule=False,
                timeout_seconds=60.0,
            ),
        ),
        created_at=now,
        updated_at=now,
    )


def _make_collector(tmp_path: Path, task: SimulationTask | None = None, **kwargs) -> LiveTelemetryCollector:
    t = task or _make_task()
    return LiveTelemetryCollector(
        t,
        requests_path=tmp_path / "requests.jsonl",
        stdout_path=tmp_path / "stdout.log",
        **kwargs,
    )


def test_incremental_jsonl_tailing_partial_lines_and_truncation(tmp_path: Path):
    collector = _make_collector(tmp_path)
    req_path = tmp_path / "requests.jsonl"

    rec1 = {
        "request_id": "req-1",
        "s_time": 1000.0,
        "e_time": 1500.0,
        "input_length": 100,
        "output_length": 20,
        "status": 200,
        "first_token_time": 120.0,
        "avg_time_between_tokens": 15.0,
        "total_time": 500.0,
        "s_time_drift": 10.0,
    }
    rec2 = {
        "request_id": "req-2",
        "s_time": 2000.0,
        "e_time": 2400.0,
        "input_length": 50,
        "output_length": 10,
        "status": 503,
        "error": "upstream unavailable",
    }
    line1 = json.dumps(rec1) + "\n"
    line2 = json.dumps(rec2) + "\n"

    # Write first complete line plus half of second line (no trailing newline).
    split_at = len(line2) // 2
    req_path.write_text(line1 + line2[:split_at], encoding="utf-8")
    collector._poll_client_files()

    assert collector.completed_count() == 1
    assert collector._requests_offset == len((line1 + line2[:split_at]).encode("utf-8"))
    assert collector._requests_pending == line2[:split_at]

    # Append remainder of second line.
    with req_path.open("a", encoding="utf-8") as handle:
        handle.write(line2[split_at:])
    collector._poll_client_files()

    assert collector.completed_count() == 2
    assert collector._requests_pending == ""
    assert collector._successful_requests == 1
    assert collector._failed_requests == 1

    # File truncation / replacement resets cursor and ingests new content cleanly.
    rec3 = {
        "request_id": "req-3",
        "s_time": 100.0,
        "e_time": 200.0,
        "input_length": 10,
        "output_length": 5,
        "status": 200,
    }
    req_path.write_text(json.dumps(rec3) + "\n", encoding="utf-8")
    collector._poll_client_files()
    assert collector.completed_count() == 3


def test_stdout_transport_and_stream_error_ingestion(tmp_path: Path):
    collector = _make_collector(tmp_path)
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text(
        "\n".join(
            [
                "2026-01-01T00:00:01Z INFO starting replay",
                "Request#7::(128|16) error: connection reset by peer Authorization: Bearer sk-secret12345678",
                "Request#8::(64|8) stream error: unexpected EOF HF_TOKEN=hf_abcdefghij1234",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    collector._poll_client_files()

    assert collector._basetime_epoch is not None
    assert collector.completed_count() == 2
    assert collector._failed_requests == 2
    assert collector._counts_by_code["transport"] == 2

    issues = collector.get_live_issues(status_code="transport", sort="request_number")
    assert issues.total == 2
    assert [item.request_id for item in issues.items] == [7, 8]
    assert "sk-secret12345678" not in (issues.items[0].error or "")
    assert "hf_abcdefghij1234" not in (issues.items[1].error or "")
    assert "[REDACTED]" in (issues.items[0].error or "")
    assert "[REDACTED]" in (issues.items[1].error or "")


def test_backend_live_tail_relative_path_contract():
    assert AIPerfBackend.live_tail_relative_path is None


@pytest.mark.asyncio
async def test_incluster_tail_pod_file_once_offsets_and_symlink_rejection(tmp_path: Path, monkeypatch):
    recorded_cmds: list[list[str]] = []

    class FakeRunner:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self._stdout = stdout
            self._returncode = returncode

        async def run(self, cmd: list[str], timeout: int = 15):
            recorded_cmds.append(cmd)
            return SimpleNamespace(returncode=self._returncode, stdout=self._stdout)

    pod = incluster._PodHandle(name="sim-pod", namespace="sim-ns", cluster_id="c1")
    remote = Path("/tmp/remote/requests.jsonl")
    local = tmp_path / "requests.jsonl"

    monkeypatch.setattr(incluster, "scoped_runner", lambda _cid: FakeRunner('{"ok":1}\n'))
    new_offset = await incluster._tail_pod_file_once(pod, remote, local, 0)
    assert new_offset == 9
    assert recorded_cmds[-1][-2:] == ["+1", str(remote)]
    assert local.read_text(encoding="utf-8") == '{"ok":1}\n'

    monkeypatch.setattr(incluster, "scoped_runner", lambda _cid: FakeRunner("next\n"))
    new_offset_2 = await incluster._tail_pod_file_once(pod, remote, local, 42)
    assert new_offset_2 == 47
    assert recorded_cmds[-1][-2:] == ["+43", str(remote)]

    # Symlink target must be rejected without writing.
    real_target = tmp_path / "real.txt"
    real_target.write_text("keep", encoding="utf-8")
    symlink_local = tmp_path / "link.jsonl"
    symlink_local.symlink_to(real_target)
    monkeypatch.setattr(incluster, "scoped_runner", lambda _cid: FakeRunner("hijack\n"))
    rejected_offset = await incluster._tail_pod_file_once(pod, remote, symlink_local, 10)
    assert rejected_offset == 10
    assert real_target.read_text(encoding="utf-8") == "keep"


def test_collector_snapshot_live_summary_and_bounded_memory(tmp_path: Path):
    collector = _make_collector(
        tmp_path,
        arrival_schedule=[0.5, 1.0, 1.5, 2.0, 2.5],
        target_deployments=[{"execution_id": "exec-1", "namespace": "ns-1"}],
    )
    collector.mark_execution_started()

    # Feed > _MAX_BUCKETS worth of timestamps to trigger _compact_buckets().
    # Bucket width starts at 5.0s; index 720 is t=3600s.
    for i in range(12):
        t_ms = (i * 400.0 + 1.0) * 1000.0
        collector._ingest_request_record(
            {
                "request_id": f"r-{i}",
                "s_time": t_ms,
                "e_time": t_ms + 50.0,
                "input_length": 32,
                "output_length": 16,
                "status": 200 if i % 4 != 0 else 500,
                "error": "boom" if i % 4 == 0 else None,
                "first_token_time": 25.0,
                "avg_time_between_tokens": 5.0,
                "s_time_drift": 12.0,
            }
        )

    assert len(collector._buckets) <= _MAX_BUCKETS
    assert collector._bucket_width > 5.0

    # Verify _SNAPSHOT_SERIES_CAP and _ERROR_RING_CAP bounds.
    for i in range(_ERROR_RING_CAP + 25):
        collector._error_ring.append(
            {
                "request_number": i,
                "request_id": i,
                "status_code": 500,
                "category": "5xx",
                "error": "err",
                "started_at_seconds": float(i),
                "latency_ms": 10.0,
                "input_tokens": 1.0,
                "output_tokens": 1.0,
            }
        )
    assert len(collector._error_ring) == _ERROR_RING_CAP

    # Verify _RESERVOIR_CAP thinning keeps rolling_samples bounded.
    for i in range(_RESERVOIR_CAP + 50):
        collector._record_rolling_and_bucket(
            rel_s=5000.0 + (i * 0.0001),
            successful=True,
            output_tokens=1.0,
            ttft_ms=10.0,
            tpot_ms=2.0,
            drift_ms=1.0,
        )
    assert len(collector._rolling_samples) <= _RESERVOIR_CAP

    snap = collector.snapshot()
    summary = collector.live_summary()
    assert snap["task_id"] == "a1b2c3d4"
    assert snap["progress"]["completed"] == collector.completed_count()
    assert len(snap["series"]["client_throughput"]) <= _SNAPSHOT_SERIES_CAP
    assert len(snap["series"]["rolling_latency"]) <= _SNAPSHOT_SERIES_CAP
    assert summary["total_requests"] == collector.completed_count()
    assert "completion_timeline" in summary
    assert "goodput_timeline" in summary
    assert len(collector._buckets[next(iter(collector._buckets))].ttfts) <= _BUCKET_SAMPLE_CAP


@pytest.mark.asyncio
async def test_collector_start_stop_race_safety(tmp_path: Path):
    collector = _make_collector(tmp_path)
    req_path = tmp_path / "requests.jsonl"

    # Calling stop() before start() must permanently prevent start() from spawning tasks.
    await collector.stop()
    assert collector._stopped is True
    await collector.start()
    assert collector._running is False
    assert collector._client_task is None
    assert collector._server_task is None

    # Normal start -> stop drains trailing bytes.
    collector2 = _make_collector(tmp_path)
    await collector2.start()
    assert collector2._running is True
    assert collector2._client_task is not None
    req_path.write_text(
        json.dumps({"request_id": "drain-1", "s_time": 10.0, "e_time": 20.0, "status": 200}) + "\n",
        encoding="utf-8",
    )
    await collector2.stop()
    assert collector2._stopped is True
    assert collector2._running is False
    assert collector2._client_task is None
    assert collector2.completed_count() == 1


def test_collector_alert_hysteresis_on_client_and_server_ticks(tmp_path: Path):
    collector = _make_collector(tmp_path)
    collector._topology_mode = "disaggregated"

    # 20 samples in 15s window with 2 failures (10% >= 5% crit) and high drift (> 1000ms).
    for i in range(20):
        collector._ingest_request_record(
            {
                "request_id": f"h-{i}",
                "s_time": 1000.0 + i * 100.0,
                "e_time": 1050.0 + i * 100.0,
                "input_length": 10,
                "output_length": 5,
                "status": 500 if i < 2 else 200,
                "error": "err" if i < 2 else None,
                "s_time_drift": 1200.0,
            }
        )

    # Server tick alone must NOT advance client streaks (drift_streak stays 0, ERROR_BURST not evaluated).
    collector._server_ring.append(
        {
            "t": 5.0,
            "vllm_running": 4.0,
            "vllm_waiting": 10.0,
            "kv_cache_peak_pct": 92.0,
            "preemptions_rate": 0.0,
            "prefill_waiting": 10.0,
            "decode_waiting": 0.0,
            "prefill_running": 4.0,
            "decode_running": 4.0,
        }
    )
    collector._evaluate_alerts(client_tick=False, server_tick=True)
    assert collector._drift_streak == 0
    assert collector._queue_streak == 1
    assert collector._kv_streak == 1
    assert collector._pd_streak == 1
    codes = {a["code"]: a for a in collector._active_alerts}
    assert "ERROR_BURST" not in codes
    assert "SCHEDULE_DRIFT" not in codes
    assert "QUEUE_SATURATION" not in codes  # needs >= 3 scrapes
    assert "KV_EVICTION_RISK" not in codes  # needs >= 2 scrapes
    assert codes["PD_IMBALANCE"]["severity"] == "warn"  # ratio 11.0 >= 2.5, streak 1 < 3 -> warn

    # Client tick 1: ERROR_BURST triggers immediately (>=20 samples), drift_streak -> 1 (not yet active).
    collector._evaluate_alerts(client_tick=True, server_tick=False)
    assert collector._drift_streak == 1
    codes = {a["code"]: a for a in collector._active_alerts}
    assert codes["ERROR_BURST"]["severity"] == "crit"
    assert "SCHEDULE_DRIFT" not in codes

    # Client tick 2: drift_streak -> 2 -> SCHEDULE_DRIFT crit active.
    collector._evaluate_alerts(client_tick=True, server_tick=False)
    assert collector._drift_streak == 2
    codes = {a["code"]: a for a in collector._active_alerts}
    assert codes["SCHEDULE_DRIFT"]["severity"] == "crit"

    # Server tick 2: kv_streak -> 2 (KV_EVICTION_RISK warn), queue_streak -> 2 (still not active).
    collector._evaluate_alerts(client_tick=False, server_tick=True)
    codes = {a["code"]: a for a in collector._active_alerts}
    assert codes["KV_EVICTION_RISK"]["severity"] == "warn"
    assert "QUEUE_SATURATION" not in codes

    # Server tick 3: queue_streak -> 3 (QUEUE_SATURATION crit since waiting >= 2*running), pd_streak -> 3 (crit).
    collector._evaluate_alerts(client_tick=False, server_tick=True)
    codes = {a["code"]: a for a in collector._active_alerts}
    assert codes["QUEUE_SATURATION"]["severity"] == "crit"
    assert codes["PD_IMBALANCE"]["severity"] == "crit"


def test_collector_get_live_issues_filtering_sorting_and_legacy_mode(tmp_path: Path):
    collector = _make_collector(tmp_path)
    records = [
        {"request_id": "a", "s_time": 100.0, "e_time": 120.0, "status": 404, "error": "not found"},
        {"request_id": "b", "s_time": 200.0, "e_time": 220.0, "status": 502, "error": "bad gateway Bearer tok123"},
        {"request_id": "c", "s_time": 300.0, "e_time": 320.0, "status": "timeout", "timeout": "Request timed out"},
        {"request_id": "d", "s_time": 400.0, "e_time": 420.0, "status": None, "error": "weird failure"},
    ]
    for rec in records:
        collector._ingest_request_record(rec)
    collector._ingest_stdout_line("Request#9::(10|0) error: connection refused")

    all_newest = collector.get_live_issues(status_code="all", sort="newest")
    assert all_newest.total == 5
    assert [item.request_number for item in all_newest.items] == [5, 4, 3, 2, 1]
    assert all_newest.counts_by_code["4xx"] == 1
    assert all_newest.counts_by_code["5xx"] == 1
    assert all_newest.counts_by_code["timeout"] == 1
    assert all_newest.counts_by_code["transport"] == 1
    assert all_newest.counts_by_code["unknown"] == 1
    assert all_newest.counts_by_code["502"] == 1

    assert collector.get_live_issues(status_code="4xx").total == 1
    assert collector.get_live_issues(status_code="5xx").total == 1
    assert collector.get_live_issues(status_code="timeout").total == 1
    assert collector.get_live_issues(status_code="transport").total == 1
    assert collector.get_live_issues(status_code="unknown").total == 1

    numeric_502 = collector.get_live_issues(status_code="502", sort="request_number", include_category=False)
    assert numeric_502.total == 1
    assert numeric_502.items[0].status_code == 502
    assert numeric_502.items[0].category is None
    assert "tok123" not in (numeric_502.items[0].error or "")
    assert "Bearer [REDACTED]" in (numeric_502.items[0].error or "")


@pytest.mark.asyncio
async def test_service_o1_active_collector_polling_paths(tmp_path: Path, monkeypatch):
    task = _make_task("0123abcd", status="running", backend="aiperf")
    collector = _make_collector(tmp_path, task=task)
    collector._ingest_request_record(
        {
            "request_id": "req-o1",
            "s_time": 100.0,
            "e_time": 200.0,
            "input_length": 16,
            "output_length": 8,
            "status": 500,
            "error": "server error",
        }
    )

    live_telemetry_registry.register(task.id, collector)
    try:

        async def _fake_load_task(tid: str):
            return task if tid == task.id else None

        def _forbidden_disk_parse(*_args, **_kwargs):
            raise AssertionError("Disk artifact parser must not be called when active collector is registered")

        monkeypatch.setattr(service, "load_task", _fake_load_task)
        monkeypatch.setattr(service, "_repository_list_ids", lambda: [task.id])
        monkeypatch.setattr(service, "_legacy_task_ids", lambda: [])
        monkeypatch.setattr(service, "live_summary_from_artifacts", _forbidden_disk_parse)
        monkeypatch.setattr(
            AIPerfBackend,
            "response_code_issues_from_artifacts",
            _forbidden_disk_parse,
        )

        got = await service.get_task(task.id)
        assert got is not None
        assert got.live_summary is not None
        assert got.live_summary["total_requests"] == 1

        page = await service.list_tasks()
        assert page.total == 1
        assert page.tasks[0].live_summary is not None
        assert page.tasks[0].live_summary["total_requests"] == 1

        issues_page = await service.get_response_code_issues(task.id, "5xx")
        assert issues_page.total == 1
        assert issues_page.items[0].category == "5xx"
        assert issues_page.counts_by_code["5xx"] == 1
    finally:
        live_telemetry_registry.unregister(task.id)


def test_collector_burst_arrivals_delayed_completions_and_aiperf_format(tmp_path: Path):
    # 1. Trace-Replayer requests.jsonl: burst arrivals in 0..5s, delayed completions in buckets 1, 3, 5 (sparse gaps at 2, 4)
    collector = _make_collector(tmp_path)
    records = [
        {
            "request_id": "r1",
            "s_time": 1000.0,
            "e_time": 7500.0,
            "input_length": 100,
            "output_length": 20,
            "status": 200,
            "first_token_time": 150.0,
            "avg_time_between_tokens": 12.0,
            "total_time": 6500.0,
            "s_time_drift": 15.0,
        },
        {
            "request_id": "r2",
            "s_time": 2000.0,
            "e_time": 17500.0,
            "input_length": 120,
            "output_length": 30,
            "status": 200,
            "first_token_time": 250.0,
            "avg_time_between_tokens": 18.0,
            "total_time": 15500.0,
            "s_time_drift": 25.0,
        },
        {
            "request_id": "r3",
            "s_time": 3000.0,
            "e_time": 27500.0,
            "input_length": 140,
            "output_length": 40,
            "status": 200,
            "first_token_time": 350.0,
            "avg_time_between_tokens": 22.0,
            "total_time": 24500.0,
            "s_time_drift": 35.0,
        },
    ]
    for rec in records:
        collector._ingest_request_record(rec)

    snap = collector.snapshot()
    lat = snap["series"]["rolling_latency"]
    thr = snap["series"]["client_throughput"]
    # Contiguous 5s buckets 0..5 with zero sparse-bucket gaps
    assert [pt["t"] for pt in lat] == [2.5, 7.5, 12.5, 17.5, 22.5, 27.5]
    assert [pt["rps"] for pt in thr] == [0.0, 0.2, 0.0, 0.2, 0.0, 0.2]

    # Bucket 0 (arrivals only): no completed latency, drift populated
    assert lat[0]["ttft_p50_ms"] is None
    assert lat[0]["tpot_p50_ms"] is None
    assert lat[0]["e2e_p50_ms"] is None
    assert lat[0]["drift_p90_ms"] == 35.0

    # Completion buckets 1, 3, 5: TTFT/TPOT/E2E populated, drift None
    for idx, exp_ttft, exp_tpot, exp_e2e in (
        (1, 150.0, 12.0, 6500.0),
        (3, 250.0, 18.0, 15500.0),
        (5, 350.0, 22.0, 24500.0),
    ):
        assert lat[idx]["ttft_p50_ms"] == exp_ttft
        assert lat[idx]["tpot_p50_ms"] == exp_tpot
        assert lat[idx]["e2e_p50_ms"] == exp_e2e
        assert lat[idx]["drift_p90_ms"] is None

    summary = collector.live_summary()
    assert [b["arrived_requests"] for b in summary["completion_timeline"]] == [3, 0, 0, 0, 0, 0]
    assert [b["completed_requests"] for b in summary["completion_timeline"]] == [0, 1, 0, 1, 0, 1]

    # 2. AIPerf profile_export.jsonl format (metadata.request_start_ns / request_end_ns + metrics.*)
    aiperf_collector = _make_collector(tmp_path, task=_make_task("b2c3d4e5", backend="aiperf"))
    origin_ns = 1_795_597_273_000_000_000
    aiperf_collector._ingest_request_record(
        {
            "metadata": {
                "benchmark_phase": "warmup",
                "request_start_ns": origin_ns,
                "request_end_ns": origin_ns + 1_000_000_000,
            },
            "metrics": {"time_to_first_token": {"value": 999.0}},
        }
    )
    assert aiperf_collector.completed_count() == 0

    aiperf_collector._ingest_request_record(
        {
            "metadata": {
                "request_id": "ai-1",
                "benchmark_phase": "profiling",
                "request_start_ns": origin_ns,
                "request_end_ns": origin_ns + 16_000_000_000,
            },
            "metrics": {
                "input_sequence_length": {"value": 256},
                "output_sequence_length": {"value": 64},
                "time_to_first_token": {"value": 310.0},
                "inter_token_latency": {"value": 24.5},
                "request_latency": {"value": 16000.0},
            },
        }
    )
    assert aiperf_collector.completed_count() == 1
    assert aiperf_collector._successful_requests == 1
    aiperf_lat = aiperf_collector.snapshot()["series"]["rolling_latency"]
    assert aiperf_lat[0]["ttft_p50_ms"] is None
    assert aiperf_lat[3]["t"] == 17.5
    assert aiperf_lat[3]["ttft_p50_ms"] == 310.0
    assert aiperf_lat[3]["tpot_p50_ms"] == 24.5
    assert aiperf_lat[3]["e2e_p50_ms"] == 16000.0


def test_collector_schedule_drift_alert_uses_aiperf_guidance(tmp_path: Path):
    for idx, backend in enumerate(("aiperf", "trace-replayer")):
        collector = _make_collector(tmp_path, task=_make_task(f"bb00000{idx}", backend=backend))
        collector._record_rolling_and_bucket(
            rel_s=1.0,
            arrived_s=1.0,
            successful=True,
            output_tokens=10.0,
            ttft_ms=50.0,
            tpot_ms=10.0,
            drift_ms=600.0,
        )
        collector._evaluate_alerts(client_tick=True, server_tick=False)
        collector._evaluate_alerts(client_tick=True, server_tick=False)
        drift_alert = next(a for a in collector._active_alerts if a["code"] == "SCHEDULE_DRIFT")
        assert "AIPerf concurrency" in drift_alert["message"]
        assert "producers/threads" not in drift_alert["message"]


def test_redact_secret_text_and_command_for_log_handle_dotted_delegated_tokens():
    from llm_d_lens.simulation.backends.base import command_for_log
    from llm_d_lens.simulation.telemetry_live import redact_secret_text

    delegated = "lens-mk-sel.eyJ2IjoxLCJ0eXAiOiJkZWxlZ2F0ZWRfcnVuIn0.abc_123-XYZ"
    redacted_sentence = redact_secret_text(f"Token {delegated} failed.")
    assert delegated not in redacted_sentence
    assert "eyJ2" not in redacted_sentence
    assert redacted_sentence == "Token [REDACTED] failed."

    redacted_flag = redact_secret_text(f"aiperf --api-key={delegated} --model qwen")
    assert delegated not in redacted_flag
    assert "--api-key [REDACTED]" in redacted_flag

    logged_space = command_for_log(
        "aiperf", ["profile", "--api-key", delegated, "--url", "http://u:p@host:8000/v1?x=1"]
    )
    assert delegated not in logged_space
    assert "<redacted>" in logged_space
    assert "u:p@" not in logged_space

    logged_eq = command_for_log("aiperf", ["profile", f"--api-key={delegated}"])
    assert delegated not in logged_eq
    assert "--api-key=<redacted>" in logged_eq


@pytest.mark.asyncio
async def test_incluster_wraps_stdin_omits_pod_env_and_redacts_stderr(tmp_path: Path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from llm_d_lens.simulation import process as sim_process
    from llm_d_lens.simulation.errors import SimulationConfigurationError, SimulationExecutionError

    delegated = "lens-mk-sel.eyJ2IjoxLCJ0aWQiOiJtYXQtMSJ9.secretSig123"
    cmd = incluster._pod_exec_command(
        "aiperf",
        ["--model", "m"],
        executable_copied=True,
        env={"OPENAI_API_KEY": delegated},
    )
    assert f"export OPENAI_API_KEY={delegated}" in cmd
    with pytest.raises(SimulationConfigurationError, match="Invalid environment variable name"):
        incluster._pod_exec_command("aiperf", [], executable_copied=True, env={"BAD;KEY": "v"})

    pod = incluster._PodHandle("pod-1", "ns", "cluster-1")
    create_calls: list[dict] = []

    async def fake_create_pod(namespace, cluster_id, image, timeout_seconds, extra_env=None):
        create_calls.append({"namespace": namespace, "extra_env": extra_env})
        return pod

    spawn_calls: list[dict] = []
    written_stdin: list[bytes] = []

    class _FakeStdin:
        def __init__(self, *, fail_broken_pipe: bool = False) -> None:
            self.fail_broken_pipe = fail_broken_pipe
            self.closed = False

        def write(self, data: bytes) -> None:
            written_stdin.append(data)
            if self.fail_broken_pipe:
                raise BrokenPipeError("broken pipe")

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        async def read(self, _n: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    class _FakeProcess:
        def __init__(self, stderr_bytes: bytes, *, fail_broken_pipe: bool = False) -> None:
            self.stdin = _FakeStdin(fail_broken_pipe=fail_broken_pipe)
            self.stdout = _FakeStream([])
            self.stderr = _FakeStream([stderr_bytes])
            self.returncode = 1

        async def wait(self) -> int:
            return self.returncode

    async def fake_spawn(argv, **kwargs):
        spawn_calls.append({"argv": list(argv), "stdin": kwargs.get("stdin"), "env": kwargs.get("env")})
        return _FakeProcess(f"Authorization failed for {delegated}\n".encode())

    monkeypatch.setattr(incluster, "_create_pod", fake_create_pod)
    monkeypatch.setattr(incluster, "_wait_pod_running", AsyncMock())
    monkeypatch.setattr(incluster, "_pod_mkdirs", AsyncMock())
    monkeypatch.setattr(incluster, "_copy_into_pod", AsyncMock())
    monkeypatch.setattr(incluster, "_copy_out_of_pod", AsyncMock(return_value=True))
    monkeypatch.setattr(incluster, "_delete_pod", AsyncMock())
    monkeypatch.setattr(incluster, "kubeconfig_environment", lambda _cid: {"KUBECONFIG": "/tmp/kc"})
    monkeypatch.setattr(incluster, "spawn", fake_spawn)

    ctx = sim_process.RunContext(asyncio.Event(), lambda *_a: None, lambda *_a: None)
    with pytest.raises(SimulationExecutionError) as exc_info:
        await incluster.execute_command_in_pod(
            namespace="ns",
            cluster_id="cluster-1",
            task_dir=tmp_path,
            executable="aiperf",
            args=["--api-key", delegated],
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=30,
            context=ctx,
            env={"OPENAI_API_KEY": delegated},
        )

    assert create_calls == [{"namespace": "ns", "extra_env": None}]
    assert spawn_calls[0]["argv"] == ["kubectl", "exec", "-i", "-n", "ns", "pod-1", "--", "sh", "-s"]
    assert spawn_calls[0]["stdin"] == asyncio.subprocess.PIPE
    assert spawn_calls[0]["env"] == {"KUBECONFIG": "/tmp/kc"}
    script_text = written_stdin[0].decode("utf-8")
    assert script_text.startswith("{\n")
    assert script_text.endswith("\n} </dev/null\n")
    assert delegated in script_text
    assert delegated not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)

    # Also verify BrokenPipeError on stdin is suppressed, stdin.close() still runs, and process.execute_command redacts stderr
    broken_procs: list[_FakeProcess] = []

    async def fake_broken_pipe_spawn(argv, **kwargs):
        proc = _FakeProcess(f"broken {delegated}\n".encode(), fail_broken_pipe=True)
        broken_procs.append(proc)
        return proc

    monkeypatch.setattr(incluster, "spawn", fake_broken_pipe_spawn)
    with pytest.raises(SimulationExecutionError) as broken_exc:
        await incluster.execute_command_in_pod(
            namespace="ns",
            cluster_id="cluster-1",
            task_dir=tmp_path,
            executable="aiperf",
            args=[],
            artifact_dir=tmp_path / "artifacts2",
            timeout_seconds=30,
            context=ctx,
            env={"OPENAI_API_KEY": delegated},
        )
    assert broken_procs[0].stdin.closed is True
    assert delegated not in str(broken_exc.value)

    monkeypatch.setattr(sim_process, "spawn", fake_broken_pipe_spawn)
    with pytest.raises(SimulationExecutionError) as proc_exc:
        await sim_process.execute_command(
            "aiperf",
            [],
            tmp_path / "host-artifacts",
            30,
            ctx,
        )
    assert delegated not in str(proc_exc.value)
    assert "[REDACTED]" in str(proc_exc.value)


def _fake_prom_cm():
    import contextlib

    @contextlib.asynccontextmanager
    async def _cm(_endpoint, timeout=5.0):
        yield object()

    return _cm


@pytest.mark.asyncio
async def test_poll_prometheus_uses_1m_window_pod_scoping_and_new_engine_queries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [])
    monkeypatch.setattr(telemetry_live, "list_resources", lambda *_a, **_kw: _async_val([]))
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())

    async def _fake_prom_ep(_cluster_id):
        return SimpleNamespace(source="prometheus")

    monkeypatch.setattr(telemetry_live, "_prometheus_endpoint", _fake_prom_ep)

    # 1. Target resolution: _resolve_server_target intact, multi-ns Model Service, and in-cluster .svc fallback
    multi_task = _make_task(
        "cc000001",
        backend="aiperf",
        endpoint_mode="deployment",
        endpoint_cluster_id="c1",
        endpoint_namespace="ns-a",
    )
    multi_collector = _make_collector(
        tmp_path,
        task=multi_task,
        target_deployments=[
            {"execution_id": "e1", "cluster_id": "c1", "namespace": "ns-a"},
            {"execution_id": "e2", "cluster_id": "c1", "namespace": "ns-b"},
            {"execution_id": "e3", "cluster_id": "c2", "namespace": "ns-other"},
        ],
    )
    assert multi_collector._resolve_server_target() == ("c1", "ns-a")
    assert multi_collector._resolve_server_targets() == ("c1", "ns-a", ["ns-a", "ns-b"])

    svc_task = _make_task(
        "cc000002",
        backend="aiperf",
        endpoint_mode="in-cluster",
        endpoint_cluster_id="c1",
        endpoint_namespace=None,
        endpoint_url="http://router-svc.prod-llm.svc.cluster.local:8000/v1",
    )
    assert _make_collector(tmp_path, task=svc_task)._resolve_server_targets() == ("c1", "prod-llm", ["prod-llm"])

    bad_svc_task = _make_task(
        "cc000003",
        backend="aiperf",
        endpoint_mode="in-cluster",
        endpoint_cluster_id="c1",
        endpoint_namespace=None,
        endpoint_url="http://router-svc.Invalid_NS.svc:8000/v1",
    )
    assert _make_collector(tmp_path, task=bad_svc_task)._resolve_server_targets() == ("c1", None, [])

    # 2. Multi-namespace disaggregated discovery, decode-only client_bridge_matcher, [1m] window,
    #    role_matcher with ns_filter, TPOT/EPP `or` fallbacks, and >= 0 clamping
    discovered_calls: list[str] = []

    async def _discover_multi(ns: str, _cid: str | None) -> dict[str, list[str]]:
        discovered_calls.append(ns)
        if ns == "ns-a":
            return {"epp": ["epp-a"], "prefill": ["prefill-0"], "decode": []}
        return {"epp": [], "prefill": [], "decode": ["decode-0"]}

    monkeypatch.setattr(telemetry_live, "_discover_components", _discover_multi)

    seen_queries: list[str] = []

    async def _fake_query(_client, expr: str):
        seen_queries.append(expr)
        if "vllm:num_requests_running" in expr and "prefill-0" in expr and "decode-0" not in expr:
            return [{"value": [0, "3"]}]
        if "vllm:num_requests_running" in expr and "decode-0" in expr:
            return [{"value": [0, "5"]}]
        if "vllm:num_requests_waiting" in expr:
            return [{"value": [0, "1"]}]
        if "vllm:kv_cache_usage_perc" in expr:
            return [{"value": [0, "0.42"]}]
        if "llm_d_epp_request_total" in expr and "or" in expr:
            return [{"value": [0, "-0.5"]}]  # clamped >= 0.0
        if "vllm:time_to_first_token_seconds" in expr:
            if "prefill-0" in expr and "decode-0" not in expr:
                return [{"value": [0, "30.5"]}]
            return [{"value": [0, "45.5"]}]
        if "vllm:inter_token_latency_seconds" in expr:
            return [{"value": [0, "12.25"]}]
        if "vllm:e2e_request_latency_seconds" in expr:
            if "prefill-0" in expr and "decode-0" not in expr:
                return [{"value": [0, "120.0"]}]
            return [{"value": [0, "300.0"]}]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)
    await multi_collector._poll_prometheus()

    assert discovered_calls == ["ns-a", "ns-b"]
    assert multi_collector._topology_mode == "disaggregated"
    # [1m] rate window used on existing and new queries (no [30s])
    assert all("[30s]" not in q for q in seen_queries)
    prefix_q = next(q for q in seen_queries if "vllm:prefix_cache_hits_total" in q)
    preempt_q = next(q for q in seen_queries if "vllm:num_preemptions_total" in q)
    epp_p90_q = next(q for q in seen_queries if "llm_d_epp_scheduler_e2e_duration_seconds_bucket" in q)
    assert "[1m]" in prefix_q and "[1m]" in preempt_q and "[1m]" in epp_p90_q

    # Multi-ns ns_filter in disaggregated role_matcher and EPP query
    assert 'namespace=~"ns-a|ns-b"' in epp_p90_q
    prefill_run_q = next(
        q
        for q in seen_queries
        if q.startswith("sum(vllm:num_requests_running") and "prefill-0" in q and "decode-0" not in q
    )
    assert 'namespace=~"ns-a|ns-b"' in prefill_run_q

    # engine_matcher covers prefill+decode, while client_bridge_matcher scopes to decode-only
    assert "decode-0" in prefix_q and "prefill-0" in prefix_q
    srv_ttft_q = next(q for q in seen_queries if "vllm:time_to_first_token_seconds_bucket" in q)
    assert "decode-0" in srv_ttft_q and "prefill-0" not in srv_ttft_q
    assert "> 0)" in srv_ttft_q

    # TPOT `or` ITL fallback + `> 0` rate guard
    tpot_q = next(q for q in seen_queries if "vllm:time_per_output_token_seconds_bucket" in q)
    assert "vllm:inter_token_latency_seconds_bucket" in tpot_q
    assert " or " in tpot_q

    # EPP fallback for srv_rps & srv_output_tok_s uses ns_matcher without pod filter
    srv_rps_q = next(q for q in seen_queries if "vllm:request_success_total" in q and "finished_reason" not in q)
    assert 'llm_d_epp_request_total{namespace=~"ns-a|ns-b"}' in srv_rps_q
    srv_tok_q = next(q for q in seen_queries if "vllm:generation_tokens_total" in q)
    assert 'llm_d_epp_request_output_tokens_sum{namespace=~"ns-a|ns-b"}' in srv_tok_q

    latest = multi_collector._server_ring[-1]
    assert latest["srv_rps"] == 0.0  # clamped from -0.5
    assert latest["prefill_ttft_p50_ms"] == 30.5
    assert latest["decode_ttft_p50_ms"] == 45.5
    assert latest["srv_ttft_p50_ms"] == 76.0  # prefill_ttft + decode_ttft
    assert latest["prefill_e2e_p50_ms"] == 120.0
    assert latest["decode_e2e_p50_ms"] == 300.0
    assert latest["srv_e2e_p50_ms"] == 420.0  # prefill_e2e + decode_e2e
    assert latest["srv_tpot_p50_ms"] == 12.25
    assert multi_collector._prometheus_available is True

    # 2b. Disaggregated decode-only fallback when prefill TTFT/E2E queries return empty
    async def _decode_only_query(_client, expr: str):
        if "vllm:time_to_first_token_seconds" in expr and "decode-0" in expr:
            return [{"value": [0, "45.5"]}]
        if "vllm:e2e_request_latency_seconds" in expr and "decode-0" in expr:
            return [{"value": [0, "300.0"]}]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _decode_only_query)
    await multi_collector._poll_prometheus()
    fallback_latest = multi_collector._server_ring[-1]
    assert fallback_latest["prefill_ttft_p50_ms"] is None
    assert fallback_latest["decode_ttft_p50_ms"] == 45.5
    assert fallback_latest["srv_ttft_p50_ms"] == 45.5
    assert fallback_latest["prefill_e2e_p50_ms"] is None
    assert fallback_latest["decode_e2e_p50_ms"] == 300.0
    assert fallback_latest["srv_e2e_p50_ms"] == 300.0
    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)

    # 3. Aggregated mode + empty discovery fallback
    agg_task = _make_task("cc000004", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="agg-ns")
    agg_collector = _make_collector(tmp_path, task=agg_task)
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": [], "prefill": [], "decode": []}),
    )
    seen_queries.clear()
    await agg_collector._poll_prometheus()
    agg_ttft_q = next(q for q in seen_queries if "vllm:time_to_first_token_seconds_bucket" in q)
    assert 'namespace="agg-ns"' in agg_ttft_q
    assert "pod=~" not in agg_ttft_q

    # 4. Transition-gated _had_engine_metrics rollover reset (no 5s storm when ServiceMonitor disabled)
    mono_clock = {"t": 100.0}
    monkeypatch.setattr(telemetry_live.time, "monotonic", lambda: mono_clock["t"])
    disc_count = {"n": 0}

    async def _count_discover(_ns: str, _cid: str | None) -> dict[str, list[str]]:
        disc_count["n"] += 1
        return {"epp": [], "prefill": [], "decode": ["pod-1"]}

    monkeypatch.setattr(telemetry_live, "_discover_components", _count_discover)

    # Disabled ServiceMonitor: engine metrics always None -> discovery runs once in 30s, never storms
    disabled_collector = _make_collector(
        tmp_path,
        task=_make_task("cc000005", endpoint_mode="deployment", endpoint_namespace="ns-dis"),
    )
    monkeypatch.setattr(telemetry_live, "_prom_query", lambda _c, _q: _async_val([]))
    await disabled_collector._poll_prometheus()
    mono_clock["t"] = 105.0
    await disabled_collector._poll_prometheus()
    mono_clock["t"] = 110.0
    await disabled_collector._poll_prometheus()
    assert disc_count["n"] == 1
    assert disabled_collector._had_engine_metrics is False

    # Active -> empty rollover: triggers single-shot reset so next tick re-discovers immediately
    disc_count["n"] = 0
    rollover_collector = _make_collector(
        tmp_path,
        task=_make_task("cc000006", endpoint_mode="deployment", endpoint_namespace="ns-roll"),
    )

    async def _active_query(_client, expr: str):
        if "vllm:num_requests_running" in expr:
            return [{"value": [0, "2"]}]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _active_query)
    mono_clock["t"] = 200.0
    await rollover_collector._poll_prometheus()
    assert disc_count["n"] == 1
    assert rollover_collector._had_engine_metrics is True

    # Pods roll over -> old pod filter yields all-None -> single-shot reset of _last_discovery_mono
    monkeypatch.setattr(telemetry_live, "_prom_query", lambda _c, _q: _async_val([]))
    mono_clock["t"] = 205.0
    await rollover_collector._poll_prometheus()
    assert disc_count["n"] == 1  # still within 30s TTL on that tick
    assert rollover_collector._last_discovery_mono == 0.0
    assert rollover_collector._had_engine_metrics is False

    # Next 5s tick re-discovers immediately, and only once while still empty
    mono_clock["t"] = 210.0
    await rollover_collector._poll_prometheus()
    mono_clock["t"] = 215.0
    await rollover_collector._poll_prometheus()
    assert disc_count["n"] == 2


async def _async_val(val):
    return val


def test_server_fallback_bridges_all_four_surfaces_when_client_records_buffered(tmp_path: Path, monkeypatch):
    task = _make_task(
        "cc000007",
        backend="aiperf",
        endpoint_mode="deployment",
        duration_seconds=100,
        backend_options={"warmup_seconds": 0.0},
    )
    collector = _make_collector(tmp_path, task=task, arrival_schedule=[float(i) for i in range(100)])
    collector.mark_execution_started()
    monkeypatch.setattr(collector, "_elapsed_seconds", lambda: 25.0)

    # Sample 1: EPP-only fallback (no vLLM metrics) -> failed_rps must stay None
    collector._server_ring.append(
        {
            "t": 5.0,
            "epp_running": 4.0,
            "srv_rps": 2.5,
            "srv_output_tok_s": 80.0,
            "srv_failed_rps": None,
            "vllm_running": None,
            "vllm_waiting": None,
        }
    )
    snap_epp = collector.snapshot()
    assert snap_epp["telemetry_status"]["client_telemetry_source"] == "server_fallback"
    assert snap_epp["progress"]["in_flight"] == 4
    assert snap_epp["series"]["client_throughput"][0]["failed_rps"] is None
    assert snap_epp["progress"]["percent"] == 25.0  # elapsed/duration, not 0.0%

    # Sample 2: full vLLM + latency metrics -> failed_rps defaults to 0.0 when None
    collector._server_ring.append(
        {
            "t": 10.0,
            "vllm_running": 6.0,
            "vllm_waiting": 2.0,
            "kv_cache_peak_pct": 55.0,
            "srv_rps": 12.5,
            "srv_failed_rps": None,
            "srv_output_tok_s": 420.5,
            "srv_ttft_p50_ms": 80.0,
            "srv_ttft_p90_ms": 140.0,
            "srv_tpot_p50_ms": 18.0,
            "srv_tpot_p90_ms": 26.0,
            "srv_e2e_p50_ms": 950.0,
            "srv_e2e_p90_ms": 1500.0,
        }
    )
    snap = collector.snapshot()
    assert snap["telemetry_status"]["client_telemetry_source"] == "server_fallback"
    assert snap["progress"]["in_flight"] == 8
    assert snap["progress"]["percent"] == 25.0
    assert snap["progress"]["completed"] == 0
    assert snap["progress"]["successful"] == 0
    assert snap["progress"]["failed"] == 0
    assert snap["kpis"]["total_requests"] == 0
    assert snap["kpis"]["total_output_tokens"] == 0
    assert snap["kpis"]["error_counts"] == {"all": 0, "4xx": 0, "5xx": 0, "timeout": 0, "transport": 0, "unknown": 0}
    assert snap["kpis"]["throughput_rps"] == 12.5
    assert snap["kpis"]["output_tok_per_s"] == 420.5
    assert snap["kpis"]["ttft_p50_ms"] == 80.0
    assert snap["kpis"]["ttft_p90_ms"] == 140.0
    assert snap["kpis"]["tpot_p50_ms"] == 18.0
    assert snap["kpis"]["tpot_p90_ms"] == 26.0

    thr = snap["series"]["client_throughput"]
    lat = snap["series"]["rolling_latency"]
    assert len(thr) == 2
    assert thr[1]["rps"] == 12.5
    assert thr[1]["failed_rps"] == 0.0
    assert thr[1]["output_tok_s"] == 420.5
    assert thr[1]["in_flight"] == 8
    assert len(lat) == 1
    assert lat[0]["ttft_p50_ms"] == 80.0
    assert lat[0]["tpot_p90_ms"] == 26.0
    assert lat[0]["drift_p90_ms"] is None


def test_client_records_authoritative_and_clean_transition_no_double_counting(tmp_path: Path):
    task = _make_task("cc000008", backend="aiperf", endpoint_mode="deployment", duration_seconds=60)
    collector = _make_collector(tmp_path, task=task, arrival_schedule=[0.5, 1.0, 1.5, 2.0])
    collector.mark_execution_started()

    # Phase 1: server_fallback active
    collector._server_ring.append(
        {
            "t": 5.0,
            "vllm_running": 9.0,
            "vllm_waiting": 1.0,
            "srv_rps": 99.0,
            "srv_output_tok_s": 999.0,
            "srv_ttft_p50_ms": 500.0,
            "srv_tpot_p50_ms": 50.0,
        }
    )
    snap_before = collector.snapshot()
    assert snap_before["telemetry_status"]["client_telemetry_source"] == "server_fallback"
    assert snap_before["kpis"]["throughput_rps"] == 99.0
    summary_before = collector.live_summary()
    assert summary_before["telemetry_source"] == "server_fallback"
    assert summary_before["total_requests"] == 0
    assert len(summary_before["completion_timeline"]) == 1
    assert summary_before["throughput_rps"] == 99.0

    # Phase 2: client record arrives -> atomic switch to "client" with zero mixing
    collector._ingest_request_record(
        {
            "request_id": "c-1",
            "s_time": 1000.0,
            "e_time": 2000.0,
            "input_length": 20,
            "output_length": 10,
            "status": 200,
            "first_token_time": 40.0,
            "avg_time_between_tokens": 8.0,
            "total_time": 1000.0,
        }
    )
    snap_after = collector.snapshot()
    assert snap_after["telemetry_status"]["client_telemetry_source"] == "client"
    assert snap_after["progress"]["completed"] == 1
    assert snap_after["progress"]["percent"] == 25.0  # 1 / 4 expected
    assert snap_after["kpis"]["ttft_p50_ms"] == 40.0
    assert snap_after["kpis"]["tpot_p50_ms"] == 8.0
    assert snap_after["kpis"]["throughput_rps"] != 99.0
    assert all(pt["rps"] != 99.0 for pt in snap_after["series"]["client_throughput"])
    assert all(pt["ttft_p50_ms"] != 500.0 for pt in snap_after["series"]["rolling_latency"])
    summary_after = collector.live_summary()
    assert summary_after["telemetry_source"] == "client"
    assert summary_after["total_requests"] == 1
    assert summary_after["ttft"]["p50_ms"] == 40.0
    assert summary_after["throughput_rps"] != 99.0


@pytest.mark.asyncio
async def test_edge_cases_external_mode_prom_unavailable_nan_warmup_and_stopped(tmp_path: Path, monkeypatch):
    # 1. External mode: _server_loop never starts, source stays "none"
    ext_task = _make_task("cc000009", backend="aiperf", endpoint_mode="external", duration_seconds=60)
    ext_collector = _make_collector(tmp_path, task=ext_task)
    await ext_collector.start()
    try:
        assert ext_collector._server_task is None
        assert ext_collector.snapshot()["telemetry_status"]["client_telemetry_source"] == "none"
    finally:
        await ext_collector.stop()

    # 2. Prometheus unavailable / NaN / empty vector -> "none"
    dep_task = _make_task(
        "cc00000a",
        backend="aiperf",
        endpoint_mode="deployment",
        endpoint_namespace="ns-nan",
        duration_seconds=60,
        backend_options={"warmup_seconds": 15.0},
    )
    nan_collector = _make_collector(tmp_path, task=dep_task)
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [])
    monkeypatch.setattr(telemetry_live, "list_resources", lambda *_a, **_kw: _async_val([]))
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": [], "prefill": [], "decode": []}),
    )
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(telemetry_live, "_prometheus_endpoint", lambda _cid: _async_val(SimpleNamespace(source="prom")))
    monkeypatch.setattr(telemetry_live, "_prom_query", lambda _c, _q: _async_val([{"value": [0, "NaN"]}]))
    await nan_collector._poll_prometheus()
    assert nan_collector._prometheus_available is False
    assert nan_collector.snapshot()["telemetry_status"]["client_telemetry_source"] == "none"

    # 3. Warmup phase still plots server_fallback points with numeric t
    nan_collector.mark_execution_started()
    monkeypatch.setattr(nan_collector, "_elapsed_seconds", lambda: 5.0)
    nan_collector._server_ring.append({"t": 5.0, "vllm_running": 2.0, "srv_rps": 3.0, "srv_ttft_p50_ms": 50.0})
    warmup_snap = nan_collector.snapshot()
    assert warmup_snap["live_phase"] == "Warmup"
    assert warmup_snap["telemetry_status"]["client_telemetry_source"] == "server_fallback"
    assert warmup_snap["series"]["client_throughput"][0]["t"] == 5.0

    # 4. Post-stop() snapshot stays valid and drains trailing client file
    await nan_collector.stop()
    assert nan_collector.snapshot()["telemetry_status"]["client_telemetry_source"] == "server_fallback"

    # 5. Pre-collector service.get_live_telemetry() returns client_telemetry_source="none"
    monkeypatch.setattr(service, "load_task", lambda tid: _async_val(dep_task if tid == dep_task.id else None))
    monkeypatch.setattr(service, "resolve_target_deployments", lambda _t: _async_val([]))
    pre_snap = await service.get_live_telemetry(dep_task.id)
    assert pre_snap["telemetry_status"]["client_telemetry_source"] == "none"


def test_live_summary_client_mode_enriches_timelines_percentiles_and_kpis(tmp_path: Path, monkeypatch):
    task = _make_task("dd000001", backend="aiperf", duration_seconds=40)
    collector = _make_collector(tmp_path, task=task, arrival_schedule=[0.5, 1.0, 2.0, 3.0, 6.0, 8.0, 10.0, 12.0])
    collector.mark_execution_started()
    monkeypatch.setattr(collector, "_elapsed_seconds", lambda: 10.0)
    assert collector.has_live_telemetry() is False
    empty_summary = collector.live_summary()
    assert empty_summary["telemetry_source"] == "none"
    assert empty_summary["error_rate_pct"] is None
    assert empty_summary["ttft"] == {"mean_ms": None, "p50_ms": None, "p90_ms": None}

    records = [
        {
            "request_id": "c-1",
            "s_time": 500.0,
            "e_time": 1000.0,
            "input_length": 20,
            "output_length": 25,
            "status": 200,
            "first_token_time": 100.0,
            "avg_time_between_tokens": 10.0,
            "total_time": 500.0,
        },
        {
            "request_id": "c-2",
            "s_time": 1000.0,
            "e_time": 2000.0,
            "input_length": 20,
            "output_length": 50,
            "status": 200,
            "first_token_time": 200.0,
            "avg_time_between_tokens": 20.0,
            "total_time": 1000.0,
        },
        {
            "request_id": "c-3",
            "s_time": 6000.0,
            "e_time": 7000.0,
            "input_length": 20,
            "output_length": 25,
            "status": 502,
            "error": "bad gateway",
            "first_token_time": 300.0,
            "avg_time_between_tokens": 30.0,
            "total_time": 1000.0,
        },
    ]
    for rec in records:
        collector._ingest_request_record(rec)

    assert collector.has_live_telemetry() is True
    summary = collector.live_summary()
    assert summary["telemetry_source"] == "client"
    assert summary["total_requests"] == 3
    assert summary["successful_requests"] == 2
    assert summary["failed_requests"] == 1
    assert summary["error_rate_pct"] == pytest.approx(33.333, abs=1e-3)
    assert summary["throughput_rps"] == pytest.approx(0.3, abs=1e-6)
    assert summary["throughput_tps"] == pytest.approx(10.0, abs=1e-6)
    assert summary["ttft"] == {"mean_ms": 200.0, "p50_ms": 200.0, "p90_ms": 300.0}
    assert summary["tpot"] == {"mean_ms": 20.0, "p50_ms": 20.0, "p90_ms": 30.0}
    assert summary["progress_percent"] == pytest.approx(37.5, abs=1e-2)  # 3 / 8 expected
    assert summary["in_flight"] == 4  # 7 dispatched <= 10.0s minus 3 completed

    comp = summary["completion_timeline"]
    thr = summary["throughput_timeline"]
    assert len(comp) == 2
    assert comp[0]["request_arrival_rps"] == pytest.approx(0.4, abs=1e-3)
    assert comp[0]["request_completion_rps"] == pytest.approx(0.4, abs=1e-3)
    assert comp[0]["failed_rps"] == 0.0
    assert comp[0]["token_throughput_tps"] == pytest.approx(15.0, abs=1e-2)
    assert comp[1]["request_arrival_rps"] == pytest.approx(0.2, abs=1e-3)
    assert comp[1]["request_completion_rps"] == pytest.approx(0.2, abs=1e-3)
    assert comp[1]["failed_rps"] == pytest.approx(0.2, abs=1e-3)
    assert comp[1]["token_throughput_tps"] == pytest.approx(5.0, abs=1e-2)
    assert thr[0]["request_arrival_rps"] == pytest.approx(0.4, abs=1e-3)
    assert thr[0]["failed_rps"] == 0.0
    assert thr[0]["output_tokens"] == 75.0
    assert thr[1]["request_arrival_rps"] == pytest.approx(0.2, abs=1e-3)
    assert thr[1]["failed_rps"] == pytest.approx(0.2, abs=1e-3)
    assert thr[1]["output_tokens"] == 25.0


def test_live_summary_aiperf_server_fallback_populates_timelines_and_kpis(tmp_path: Path, monkeypatch):
    task = _make_task(
        "dd000002",
        backend="aiperf",
        endpoint_mode="deployment",
        duration_seconds=80,
        backend_options={"warmup_seconds": 0.0},
    )
    collector = _make_collector(tmp_path, task=task, arrival_schedule=[float(i) for i in range(100)])
    collector.mark_execution_started()
    monkeypatch.setattr(collector, "_elapsed_seconds", lambda: 20.0)

    # First sample: EPP-only (no vLLM metrics -> failed_rps stays None, error_rate_pct stays None)
    collector._server_ring.append(
        {
            "t": 5.0,
            "epp_running": 3.0,
            "srv_rps": 4.0,
            "srv_output_tok_s": 120.0,
            "srv_failed_rps": None,
        }
    )
    epp_summary = collector.live_summary()
    assert collector.has_live_telemetry() is True
    assert epp_summary["telemetry_source"] == "server_fallback"
    assert epp_summary["error_rate_pct"] is None
    assert epp_summary["in_flight"] == 3
    assert epp_summary["progress_percent"] == 25.0

    # Second sample: vLLM metrics + explicit failed_rps
    collector._server_ring.append(
        {
            "t": 15.0,
            "vllm_running": 6.0,
            "vllm_waiting": 2.0,
            "kv_cache_peak_pct": 62.0,
            "srv_rps": 10.0,
            "srv_failed_rps": 0.5,
            "srv_output_tok_s": 350.0,
            "srv_ttft_p50_ms": 72.0,
            "srv_ttft_p90_ms": 135.0,
            "srv_tpot_p50_ms": 16.5,
            "srv_tpot_p90_ms": 24.0,
        }
    )
    summary = collector.live_summary()
    assert summary["telemetry_source"] == "server_fallback"
    assert summary["total_requests"] == 0
    assert summary["in_flight"] == 8
    assert summary["progress_percent"] == 25.0
    assert summary["throughput_rps"] == 10.0
    assert summary["throughput_tps"] == 350.0
    assert summary["ttft"] == {"mean_ms": 72.0, "p50_ms": 72.0, "p90_ms": 135.0}
    assert summary["tpot"] == {"mean_ms": 16.5, "p50_ms": 16.5, "p90_ms": 24.0}
    assert summary["error_rate_pct"] == 5.0

    comp = summary["completion_timeline"]
    thr = summary["throughput_timeline"]
    err = summary["error_timeline"]
    good = summary["goodput_timeline"]
    assert len(comp) == 2
    # Interval 0: [0.0, 5.0] (dt=5.0), rps=4.0 -> 20.0 completed, +3 in_flight = 23.0 arrived
    assert comp[0]["start_seconds"] == 0.0
    assert comp[0]["end_seconds"] == 5.0
    assert comp[0]["completed_requests"] == 20.0
    assert comp[0]["arrived_requests"] == 23.0
    assert comp[0]["in_flight"] == 3
    assert comp[0]["request_completion_rps"] == 4.0
    assert comp[0]["token_throughput_tps"] == 120.0
    # Interval 1: [5.0, 15.0] (dt=10.0), rps=10.0, failed_rps=0.5 -> 100.0 completed, 95.0 succ, 5.0 failed
    assert comp[1]["start_seconds"] == 5.0
    assert comp[1]["end_seconds"] == 15.0
    assert comp[1]["completed_requests"] == 100.0
    assert comp[1]["successful_requests"] == 95.0
    assert comp[1]["failed_requests"] == 5.0
    assert comp[1]["cumulative_completed"] == 120.0
    assert comp[1]["in_flight"] == 8
    assert thr[1]["output_tokens"] == 3500.0
    assert err[1]["failed_requests"] == 5.0
    assert err[1]["error_rate_percent"] == 5.0
    assert err[1]["cumulative_failures"] == 5.0
    assert good[1]["good_requests"] == 95.0
    assert good[1]["goodput_rps"] == 9.5


@pytest.mark.asyncio
async def test_service_surfaces_aiperf_server_fallback_live_summary_and_progress(tmp_path: Path, monkeypatch):
    task = _make_task(
        "dd000003",
        status="running",
        backend="aiperf",
        endpoint_mode="deployment",
        duration_seconds=100,
    )
    collector = _make_collector(tmp_path, task=task)
    collector.mark_execution_started()
    monkeypatch.setattr(collector, "_elapsed_seconds", lambda: 37.5)
    collector._server_ring.append(
        {
            "t": 10.0,
            "vllm_running": 4.0,
            "vllm_waiting": 1.0,
            "srv_rps": 8.0,
            "srv_output_tok_s": 240.0,
            "srv_ttft_p50_ms": 55.0,
            "srv_tpot_p50_ms": 14.0,
        }
    )

    live_telemetry_registry.register(task.id, collector)
    try:

        async def _fake_load_task(tid: str):
            return task if tid == task.id else None

        def _forbidden_disk_parse(*_args, **_kwargs):
            raise AssertionError("Disk artifact parser must not run when collector has server_fallback telemetry")

        monkeypatch.setattr(service, "load_task", _fake_load_task)
        monkeypatch.setattr(service, "_repository_list_ids", lambda: [task.id])
        monkeypatch.setattr(service, "_legacy_task_ids", lambda: [])
        monkeypatch.setattr(service, "live_summary_from_artifacts", _forbidden_disk_parse)

        direct = await service._live_summary_for_task(task)
        assert direct["telemetry_source"] == "server_fallback"
        assert direct["progress_percent"] == 37.5

        got = await service.get_task(task.id)
        assert got is not None
        assert got.progress_percent == 37.5
        assert got.live_summary is not None
        assert got.live_summary["telemetry_source"] == "server_fallback"
        assert len(got.live_summary["completion_timeline"]) == 1

        page = await service.list_tasks()
        assert page.total == 1
        assert page.tasks[0].progress_percent == 37.5
        assert page.tasks[0].live_summary is not None
        assert page.tasks[0].live_summary["throughput_rps"] == 8.0
        # Original persisted task object was not mutated
        assert task.progress_percent == 0
    finally:
        live_telemetry_registry.unregister(task.id)


def test_server_fallback_early_scrape_arrival_rps_and_failed_rps_are_bounded(tmp_path: Path, monkeypatch):
    for scrape_t in (0.0, 0.05):
        task = _make_task(
            "dd000004",
            backend="aiperf",
            endpoint_mode="deployment",
            duration_seconds=60,
        )
        collector = _make_collector(tmp_path, task=task)
        collector.mark_execution_started()
        monkeypatch.setattr(collector, "_elapsed_seconds", lambda t=scrape_t: max(t, 0.05))
        collector._server_ring.append(
            {
                "t": scrape_t,
                "vllm_running": 8.0,
                "vllm_waiting": 2.0,
                "srv_rps": 3.0,
                "srv_failed_rps": 0.5,
                "srv_output_tok_s": 100.0,
            }
        )
        summary = collector.live_summary()
        comp = summary["completion_timeline"]
        thr = summary["throughput_timeline"]
        assert len(comp) == 1
        assert len(thr) == 1
        # dt is floored by _bucket_width (5.0s) in request_arrival_rps so 10 in_flight -> 3.0 + 10/5.0 = 5.0
        assert comp[0]["request_arrival_rps"] == 5.0
        assert comp[0]["failed_rps"] == 0.5
        assert thr[0]["request_arrival_rps"] == 5.0
        assert thr[0]["failed_rps"] == 0.5


@pytest.mark.asyncio
async def test_poll_prometheus_pci_dra_single_card_on_8card_node_with_403_slices(tmp_path: Path, monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    xpu_profile = get_profile("intel-xpu")
    assert xpu_profile is not None
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [xpu_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": ["epp-0"], "prefill": [], "decode": ["decode-0"]}),
    )

    pods = [
        {
            "metadata": {
                "name": "decode-0",
                "namespace": "ns-xpu",
                "uid": "uid-decode-0",
                "creationTimestamp": "2026-11-26T08:00:00Z",
            },
            "spec": {"nodeName": "smc-18"},
            "status": {"startTime": "2026-11-26T08:00:05Z"},
        }
    ]
    claims = [
        {
            "metadata": {"name": "claim-decode-0", "namespace": "ns-xpu"},
            "status": {
                "reservedFor": [{"resource": "pods", "name": "decode-0", "uid": "uid-decode-0"}],
                "allocation": {
                    "allocationTimestamp": "2026-11-26T08:00:02Z",
                    "devices": {
                        "results": [
                            {
                                "driver": "gpu.intel.com",
                                "pool": "smc-18",
                                "device": "0000-03-00-0-0xe211",
                            }
                        ]
                    },
                },
            },
        }
    ]

    async def _fake_list_resources(kind: str, *, namespace: str | None = None, cluster_id: str | None = None):
        if kind == "pods" and namespace == "ns-xpu":
            return pods
        if kind == "resourceclaims.resource.k8s.io" and namespace == "ns-xpu":
            return claims
        if kind == "resourceslices.resource.k8s.io":
            raise RuntimeError("403 Forbidden: cluster-scoped ResourceSlices require admin")
        return []

    monkeypatch.setattr(telemetry_live, "list_resources", _fake_list_resources)

    sample_ts = datetime(2026, 11, 26, 8, 5, 0, tzinfo=UTC).timestamp()
    all_8_pcis = [f"0000:0{i}:00.0" for i in range(3, 11)]
    seen_queries: list[str] = []

    async def _fake_query(_client, expr: str):
        seen_queries.append(expr)
        if expr.startswith("hw_gpu_utilization_ratio"):
            return [
                {
                    "metric": {
                        "node": "smc-18",
                        "pci_bdf": pci,
                        "hw_gpu_task": "compute-all",
                        "namespace": "intel-xpumd",
                        "pod": "xpumd-smc18",
                    },
                    "value": [sample_ts, "0.9784" if pci == "0000:03:00.0" else "0.0"],
                }
                for pci in all_8_pcis
            ]
        if expr.startswith("hw_power_watts"):
            return [
                {
                    "metric": {
                        "node": "smc-18",
                        "pci_bdf": pci,
                        "namespace": "intel-xpumd",
                        "pod": "xpumd-smc18",
                    },
                    "value": [sample_ts, "135.0" if pci == "0000:03:00.0" else "35.0"],
                }
                for pci in all_8_pcis
            ]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)

    task = _make_task("ee000001", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-xpu")
    collector = _make_collector(tmp_path, task=task)
    await collector._poll_prometheus()

    latest = collector._server_ring[-1]
    assert latest["accelerator_util_pct"] == 97.84
    assert latest["accelerator_power_watts"] == 135.0
    assert collector._accelerator_available is True

    # Verify PCI-filtered unaggregated XPUM queries were issued, never unscoped cluster-wide avg/sum
    util_queries = [q for q in seen_queries if "hw_gpu_utilization_ratio" in q]
    power_queries = [q for q in seen_queries if "hw_power_watts" in q]
    assert len(util_queries) == 1
    assert len(power_queries) == 1
    assert 'pci_bdf=~"(?i)(?:0000:03:00\\\\.0)"' in util_queries[0]
    assert not util_queries[0].startswith("avg(")
    assert 'pci_bdf=~"(?i)(?:0000:03:00\\\\.0)"' in power_queries[0]
    assert not power_queries[0].startswith("sum(")


@pytest.mark.asyncio
async def test_poll_prometheus_pci_dra_multicard_pd_and_excludes_unrelated_pod(tmp_path: Path, monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    xpu_profile = get_profile("intel-xpu")
    assert xpu_profile is not None
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [xpu_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": ["epp-0"], "prefill": ["prefill-0"], "decode": ["decode-0"]}),
    )

    pods = [
        {
            "metadata": {
                "name": name,
                "namespace": "ns-pd",
                "uid": f"uid-{name}",
                "creationTimestamp": "2026-11-26T08:00:00Z",
            },
            "spec": {"nodeName": "smc-18"},
            "status": {"startTime": "2026-11-26T08:00:05Z"},
        }
        for name in ("prefill-0", "decode-0", "benchmark-runner-0")
    ]
    claims = [
        {
            "metadata": {"name": f"claim-{pod_name}", "namespace": "ns-pd"},
            "status": {
                "reservedFor": [{"resource": "pods", "name": pod_name, "uid": f"uid-{pod_name}"}],
                "allocation": {
                    "allocationTimestamp": "2026-11-26T08:00:02Z",
                    "devices": {"results": [{"driver": "gpu.intel.com", "pool": "smc-18-pool", "device": dev_name}]},
                },
            },
        }
        for pod_name, dev_name in (
            ("prefill-0", "gpu-0"),
            ("decode-0", "gpu-1"),
            ("benchmark-runner-0", "gpu-2"),
        )
    ]
    slices = [
        {
            "spec": {
                "driver": "gpu.intel.com",
                "nodeName": "smc-18",
                "pool": {"name": "smc-18-pool"},
                "devices": [
                    {"name": "gpu-0", "attributes": {"pciAddress": {"string": "0000:3a:00.0"}}},
                    {"name": "gpu-1", "attributes": {"pciAddress": {"string": "0000:4a:00.0"}}},
                    {"name": "gpu-2", "attributes": {"pciAddress": {"string": "0000:5a:00.0"}}},
                ],
            }
        }
    ]

    state = {"empty": False}

    async def _fake_list_resources(kind: str, *, namespace: str | None = None, cluster_id: str | None = None):
        if state["empty"]:
            return []
        if kind == "pods" and namespace == "ns-pd":
            return pods
        if kind == "resourceclaims.resource.k8s.io" and namespace == "ns-pd":
            return claims
        if kind == "resourceslices.resource.k8s.io":
            return slices
        return []

    monkeypatch.setattr(telemetry_live, "list_resources", _fake_list_resources)

    sample_ts = datetime(2026, 11, 26, 8, 5, 0, tzinfo=UTC).timestamp()
    util_by_pci = {
        "0000:3A:00.0": "0.80",  # prefill-0 (uppercase hex tested)
        "0000:4a:00.0": "0.60",  # decode-0
        "0000:5a:00.0": "0.99",  # benchmark-runner-0 (must be excluded)
    }
    for idx in range(5):
        util_by_pci[f"0000:8{idx}:00.0"] = "0.0"

    power_by_pci = {
        "0000:3A:00.0": "150.0",
        "0000:4a:00.0": "110.0",
        "0000:5a:00.0": "200.0",
    }
    for idx in range(5):
        power_by_pci[f"0000:8{idx}:00.0"] = "35.0"

    seen_queries: list[str] = []

    async def _fake_query(_client, expr: str):
        seen_queries.append(expr)
        if expr.startswith("hw_gpu_utilization_ratio"):
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": pci, "hw_gpu_task": "compute-all"},
                    "value": [sample_ts, val],
                }
                for pci, val in util_by_pci.items()
            ]
        if expr.startswith("hw_power_watts"):
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": pci},
                    "value": [sample_ts, val],
                }
                for pci, val in power_by_pci.items()
            ]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)

    mono_clock = {"t": 100.0}
    monkeypatch.setattr(telemetry_live.time, "monotonic", lambda: mono_clock["t"])

    task = _make_task("ee000002", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-pd")
    collector = _make_collector(tmp_path, task=task)
    await collector._poll_prometheus()

    latest = collector._server_ring[-1]
    assert latest["accelerator_util_pct"] == 70.0  # avg(80.0, 60.0), NOT 17.5 or benchmark-runner-0
    assert latest["accelerator_power_watts"] == 260.0  # sum(150.0, 110.0), NOT 470.0 or benchmark-runner-0

    util_q = next(q for q in seen_queries if "hw_gpu_utilization_ratio" in q)
    assert "0000:3a:00" in util_q
    assert "0000:4a:00" in util_q
    assert "0000:5a:00" not in util_q  # unrelated benchmark pod excluded from PCI filter

    # Next discovery refresh with terminated pods clears stale _device_allocations -> returns None & no unscoped query
    state["empty"] = True
    seen_queries.clear()
    mono_clock["t"] = 135.0
    await collector._poll_prometheus()
    cleared = collector._server_ring[-1]
    assert collector._device_allocations == {}
    assert cleared.get("accelerator_util_pct") is None
    assert cleared.get("accelerator_power_watts") is None
    assert collector._accelerator_available is False
    assert not any("hw_gpu_utilization_ratio" in q or "hw_power_watts" in q for q in seen_queries)


@pytest.mark.asyncio
async def test_poll_prometheus_direct_profile_pod_scoping_and_zero_allocation_no_unscoped_fallback(
    tmp_path: Path, monkeypatch
):
    from llm_d_lens.hardware.registry import get_profile

    nvidia_profile = get_profile("nvidia")
    xpu_profile = get_profile("intel-xpu")
    assert nvidia_profile is not None and xpu_profile is not None

    # 1. Direct-labelled profile (NVIDIA DCGM) with serving_pods -> filters by namespace AND pod=~"..."
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [nvidia_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": ["epp-0"], "prefill": [], "decode": ["decode-0"]}),
    )
    monkeypatch.setattr(
        telemetry_live,
        "list_resources",
        lambda kind, **_kw: _async_val(
            [
                {
                    "metadata": {"name": "decode-0", "namespace": "ns-nv", "uid": "u1"},
                    "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                }
            ]
            if kind == "pods"
            else []
        ),
    )

    seen_queries: list[str] = []

    async def _nv_query(_client, expr: str):
        seen_queries.append(expr)
        if "DCGM_FI_DEV_GPU_UTIL" in expr:
            return [{"value": [0, "84.25"]}]
        if "DCGM_FI_DEV_POWER_USAGE" in expr:
            return [{"value": [0, "310.5"]}]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _nv_query)

    nv_task = _make_task("ee000003", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-nv")
    nv_collector = _make_collector(tmp_path, task=nv_task)
    await nv_collector._poll_prometheus()

    dcgm_util_q = next(q for q in seen_queries if "DCGM_FI_DEV_GPU_UTIL" in q)
    dcgm_power_q = next(q for q in seen_queries if "DCGM_FI_DEV_POWER_USAGE" in q)
    assert 'namespace="ns-nv"' in dcgm_util_q and 'pod=~"decode-0"' in dcgm_util_q
    assert 'namespace="ns-nv"' in dcgm_power_q and 'pod=~"decode-0"' in dcgm_power_q
    assert nv_collector._server_ring[-1]["accelerator_util_pct"] == 84.25
    assert nv_collector._server_ring[-1]["accelerator_power_watts"] == 310.5

    # 2. PCI-joined profile with zero allocations -> returns None and NEVER emits unscoped cluster-wide PromQL
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [xpu_profile])
    monkeypatch.setattr(telemetry_live, "list_resources", lambda *_a, **_kw: _async_val([]))
    seen_queries.clear()
    empty_collector = _make_collector(
        tmp_path,
        task=_make_task(
            "ee000004", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-empty"
        ),
    )
    # Pre-seed _hw_profiles to ensure even when a profile is known, zero allocations never falls back cluster-wide
    empty_collector._hw_profiles = [xpu_profile]
    await empty_collector._poll_prometheus()

    latest_empty = empty_collector._server_ring[-1]
    assert latest_empty.get("accelerator_util_pct") is None
    assert latest_empty.get("accelerator_power_watts") is None
    assert empty_collector._accelerator_available is False
    assert not any("hw_gpu_utilization_ratio" in q or "hw_power_watts" in q for q in seen_queries)

    # 3. Direct-labelled profile with empty serving_pods -> still namespace-scoped, never cluster-wide
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [nvidia_profile])
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": [], "prefill": [], "decode": []}),
    )
    monkeypatch.setattr(
        telemetry_live,
        "list_resources",
        lambda kind, **_kw: _async_val(
            [
                {
                    "metadata": {"name": "vllm-0", "namespace": "ns-nv-agg", "uid": "u2"},
                    "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                }
            ]
            if kind == "pods"
            else []
        ),
    )
    seen_queries.clear()
    monkeypatch.setattr(telemetry_live, "_prom_query", _nv_query)
    nv_agg_collector = _make_collector(
        tmp_path,
        task=_make_task(
            "ee000005", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-nv-agg"
        ),
    )
    await nv_agg_collector._poll_prometheus()
    agg_dcgm_q = next(q for q in seen_queries if "DCGM_FI_DEV_GPU_UTIL" in q)
    assert 'namespace="ns-nv-agg"' in agg_dcgm_q
    assert "pod=~" not in agg_dcgm_q


@pytest.mark.asyncio
async def test_poll_prometheus_pci_pod_status_plugin_allocation(tmp_path: Path, monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    xpu_profile = get_profile("intel-xpu")
    assert xpu_profile is not None
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [xpu_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": [], "prefill": [], "decode": ["decode-plugin-0"]}),
    )

    pods = [
        {
            "metadata": {
                "name": "decode-plugin-0",
                "namespace": "ns-plugin",
                "uid": "uid-plug-0",
                "creationTimestamp": "2026-11-26T08:00:00Z",
            },
            "spec": {
                "nodeName": "smc-18",
                "containers": [{"resources": {"limits": {"gpu.intel.com/i915": "1"}}}],
            },
            "status": {
                "startTime": "2026-11-26T08:00:05Z",
                "containerStatuses": [
                    {
                        "allocatedResourcesStatus": [
                            {
                                "name": "gpu.intel.com/i915",
                                "resources": [{"resourceID": "0000:06:00.0"}],
                            }
                        ]
                    }
                ],
            },
        }
    ]
    monkeypatch.setattr(
        telemetry_live,
        "list_resources",
        lambda kind, **_kw: _async_val(pods if kind == "pods" else []),
    )

    sample_ts = datetime(2026, 11, 26, 8, 5, 0, tzinfo=UTC).timestamp()

    async def _fake_query(_client, expr: str):
        if expr.startswith("hw_gpu_utilization_ratio"):
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:06:00.0", "hw_gpu_task": "compute-all"},
                    "value": [sample_ts, "0.625"],
                }
            ]
        if expr.startswith("hw_power_watts"):
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:06:00.0"},
                    "value": [sample_ts, "120.0"],
                }
            ]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)

    task = _make_task("ee000006", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-plugin")
    collector = _make_collector(tmp_path, task=task)
    await collector._poll_prometheus()

    latest = collector._server_ring[-1]
    assert latest["accelerator_util_pct"] == 62.5
    assert latest["accelerator_power_watts"] == 120.0
    assert collector._accelerator_available is True


@pytest.mark.asyncio
async def test_poll_prometheus_multi_namespace_direct_profile_regex_no_or(tmp_path: Path, monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    nvidia_profile = get_profile("nvidia")
    assert nvidia_profile is not None
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [nvidia_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )
    monkeypatch.setattr(
        telemetry_live,
        "list_resources",
        lambda kind, **_kw: _async_val(
            [
                {
                    "metadata": {"name": "prefill-0", "namespace": "ns-a", "uid": "u1"},
                    "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                },
                {
                    "metadata": {"name": "decode-0", "namespace": "ns-b", "uid": "u2"},
                    "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                },
            ]
            if kind == "pods"
            else []
        ),
    )

    seen_queries: list[str] = []

    async def _nv_query(_client, expr: str):
        seen_queries.append(expr)
        if "DCGM_FI_DEV_GPU_UTIL" in expr:
            return [{"value": [0, "75.0"]}]
        if "DCGM_FI_DEV_POWER_USAGE" in expr:
            return [{"value": [0, "420.0"]}]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _nv_query)

    # 1. With serving_pods discovered across ns-a and ns-b -> single namespace=~"ns-a|ns-b" & pod=~"..." (no per-ns or)
    async def _discover_two(ns: str, _cid: str | None):
        if ns == "ns-a":
            return {"epp": [], "prefill": ["prefill-0"], "decode": []}
        return {"epp": [], "prefill": [], "decode": ["decode-0"]}

    monkeypatch.setattr(telemetry_live, "_discover_components", _discover_two)
    task = _make_task("ee000007", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-a")
    collector = _make_collector(
        tmp_path,
        task=task,
        target_deployments=[
            {"execution_id": "e1", "cluster_id": "c1", "namespace": "ns-a"},
            {"execution_id": "e2", "cluster_id": "c1", "namespace": "ns-b"},
        ],
    )
    await collector._poll_prometheus()

    util_q = next(q for q in seen_queries if "DCGM_FI_DEV_GPU_UTIL" in q)
    power_q = next(q for q in seen_queries if "DCGM_FI_DEV_POWER_USAGE" in q)
    assert 'namespace=~"ns-a|ns-b"' in util_q and 'pod=~"decode-0|prefill-0"' in util_q
    assert " or " not in util_q
    assert 'namespace=~"ns-a|ns-b"' in power_q and 'pod=~"decode-0|prefill-0"' in power_q
    assert " or " not in power_q
    assert collector._server_ring[-1]["accelerator_util_pct"] == 75.0
    assert collector._server_ring[-1]["accelerator_power_watts"] == 420.0

    # 2. Without serving_pods across ns-a and ns-b -> single namespace=~"ns-a|ns-b" without pod=~ or per-ns or
    seen_queries.clear()
    monkeypatch.setattr(
        telemetry_live,
        "_discover_components",
        lambda _ns, _cid: _async_val({"epp": [], "prefill": [], "decode": []}),
    )
    collector_no_pods = _make_collector(
        tmp_path,
        task=_make_task("ee000008", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-a"),
        target_deployments=[
            {"execution_id": "e1", "cluster_id": "c1", "namespace": "ns-a"},
            {"execution_id": "e2", "cluster_id": "c1", "namespace": "ns-b"},
        ],
    )
    await collector_no_pods._poll_prometheus()

    util_q_no_pod = next(q for q in seen_queries if "DCGM_FI_DEV_GPU_UTIL" in q)
    power_q_no_pod = next(q for q in seen_queries if "DCGM_FI_DEV_POWER_USAGE" in q)
    assert 'namespace=~"ns-a|ns-b"' in util_q_no_pod and "pod=~" not in util_q_no_pod and " or " not in util_q_no_pod
    assert 'namespace=~"ns-a|ns-b"' in power_q_no_pod and "pod=~" not in power_q_no_pod and " or " not in power_q_no_pod


@pytest.mark.asyncio
async def test_poll_prometheus_multi_namespace_pci_per_ns_serving_pod_scoping(tmp_path: Path, monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    xpu_profile = get_profile("intel-xpu")
    assert xpu_profile is not None
    monkeypatch.setattr(telemetry_live, "_hardware_profiles", lambda: [xpu_profile])
    monkeypatch.setattr(telemetry_live, "create_prometheus_client", _fake_prom_cm())
    monkeypatch.setattr(
        telemetry_live,
        "_prometheus_endpoint",
        lambda _cid: _async_val(SimpleNamespace(source="prometheus")),
    )

    # ns-a has serving pod "decode-0"; ns-b has serving pod "prefill-0" AND an unrelated pod ALSO named "decode-0"
    pods_by_ns = {
        "ns-a": [
            {
                "metadata": {
                    "name": "decode-0",
                    "namespace": "ns-a",
                    "uid": "uid-a-decode",
                    "creationTimestamp": "2026-11-26T08:00:00Z",
                },
                "spec": {"nodeName": "smc-18"},
                "status": {"startTime": "2026-11-26T08:00:05Z"},
            }
        ],
        "ns-b": [
            {
                "metadata": {
                    "name": "prefill-0",
                    "namespace": "ns-b",
                    "uid": "uid-b-prefill",
                    "creationTimestamp": "2026-11-26T08:00:00Z",
                },
                "spec": {"nodeName": "smc-18"},
                "status": {"startTime": "2026-11-26T08:00:05Z"},
            },
            {
                "metadata": {
                    "name": "decode-0",
                    "namespace": "ns-b",
                    "uid": "uid-b-unrelated",
                    "creationTimestamp": "2026-11-26T08:00:00Z",
                },
                "spec": {"nodeName": "smc-18"},
                "status": {"startTime": "2026-11-26T08:00:05Z"},
            },
        ],
    }
    claims_by_ns = {
        "ns-a": [
            {
                "metadata": {"name": "claim-a-decode", "namespace": "ns-a"},
                "status": {
                    "reservedFor": [{"resource": "pods", "name": "decode-0", "uid": "uid-a-decode"}],
                    "allocation": {
                        "allocationTimestamp": "2026-11-26T08:00:02Z",
                        "devices": {
                            "results": [{"driver": "gpu.intel.com", "pool": "smc-18", "device": "0000-03-00-0-0xe211"}]
                        },
                    },
                },
            }
        ],
        "ns-b": [
            {
                "metadata": {"name": "claim-b-prefill", "namespace": "ns-b"},
                "status": {
                    "reservedFor": [{"resource": "pods", "name": "prefill-0", "uid": "uid-b-prefill"}],
                    "allocation": {
                        "allocationTimestamp": "2026-11-26T08:00:02Z",
                        "devices": {
                            "results": [{"driver": "gpu.intel.com", "pool": "smc-18", "device": "0000-04-00-0-0xe211"}]
                        },
                    },
                },
            },
            {
                "metadata": {"name": "claim-b-unrelated", "namespace": "ns-b"},
                "status": {
                    "reservedFor": [{"resource": "pods", "name": "decode-0", "uid": "uid-b-unrelated"}],
                    "allocation": {
                        "allocationTimestamp": "2026-11-26T08:00:02Z",
                        "devices": {
                            "results": [{"driver": "gpu.intel.com", "pool": "smc-18", "device": "0000-05-00-0-0xe211"}]
                        },
                    },
                },
            },
        ],
    }

    async def _fake_list_resources(kind: str, *, namespace: str | None = None, cluster_id: str | None = None):
        if kind == "pods" and namespace in pods_by_ns:
            return pods_by_ns[namespace]
        if kind == "resourceclaims.resource.k8s.io" and namespace in claims_by_ns:
            return claims_by_ns[namespace]
        return []

    monkeypatch.setattr(telemetry_live, "list_resources", _fake_list_resources)

    sample_ts = datetime(2026, 11, 26, 8, 5, 0, tzinfo=UTC).timestamp()

    async def _fake_query(_client, expr: str):
        if expr.startswith("hw_gpu_utilization_ratio"):
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:03:00.0", "hw_gpu_task": "compute-all"},
                    "value": [sample_ts, "0.80"],
                },
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:04:00.0", "hw_gpu_task": "compute-all"},
                    "value": [sample_ts, "0.60"],
                },
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:05:00.0", "hw_gpu_task": "compute-all"},
                    "value": [sample_ts, "0.10"],  # unrelated decode-0 in ns-b
                },
            ]
        if expr.startswith("hw_power_watts"):
            return [
                {"metric": {"node": "smc-18", "pci_bdf": "0000:03:00.0"}, "value": [sample_ts, "150.0"]},
                {"metric": {"node": "smc-18", "pci_bdf": "0000:04:00.0"}, "value": [sample_ts, "110.0"]},
                {"metric": {"node": "smc-18", "pci_bdf": "0000:05:00.0"}, "value": [sample_ts, "200.0"]},
            ]
        return []

    monkeypatch.setattr(telemetry_live, "_prom_query", _fake_query)

    # Case 1: both namespaces discover serving pods -> same-named unrelated decode-0 in ns-b is excluded
    async def _discover_both(ns: str, _cid: str | None):
        if ns == "ns-a":
            return {"epp": [], "prefill": [], "decode": ["decode-0"]}
        return {"epp": [], "prefill": ["prefill-0"], "decode": []}

    monkeypatch.setattr(telemetry_live, "_discover_components", _discover_both)
    collector = _make_collector(
        tmp_path,
        task=_make_task("ee000009", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-a"),
        target_deployments=[
            {"execution_id": "e1", "cluster_id": "c1", "namespace": "ns-a"},
            {"execution_id": "e2", "cluster_id": "c1", "namespace": "ns-b"},
        ],
    )
    await collector._poll_prometheus()

    _, allocs = collector._device_allocations["intel-xpu"]
    assert ("smc-18", "0000:03:00.0") in allocs
    assert ("smc-18", "0000:04:00.0") in allocs
    assert ("smc-18", "0000:05:00.0") not in allocs
    assert collector._server_ring[-1]["accelerator_util_pct"] == 70.0
    assert collector._server_ring[-1]["accelerator_power_watts"] == 260.0

    # Case 2: discovery fails for ns-b -> ns-b allocations are preserved rather than wiped out by ns-a's serving pods
    async def _discover_partial_fail(ns: str, _cid: str | None):
        if ns == "ns-a":
            return {"epp": [], "prefill": [], "decode": ["decode-0"]}
        raise RuntimeError("transient k8s error")

    monkeypatch.setattr(telemetry_live, "_discover_components", _discover_partial_fail)
    collector_partial = _make_collector(
        tmp_path,
        task=_make_task("ee00000a", endpoint_mode="deployment", endpoint_cluster_id="c1", endpoint_namespace="ns-a"),
        target_deployments=[
            {"execution_id": "e1", "cluster_id": "c1", "namespace": "ns-a"},
            {"execution_id": "e2", "cluster_id": "c1", "namespace": "ns-b"},
        ],
    )
    await collector_partial._poll_prometheus()

    _, partial_allocs = collector_partial._device_allocations["intel-xpu"]
    assert ("smc-18", "0000:03:00.0") in partial_allocs
    assert ("smc-18", "0000:04:00.0") in partial_allocs
    assert ("smc-18", "0000:05:00.0") in partial_allocs
    assert collector_partial._server_ring[-1]["accelerator_util_pct"] == 50.0  # avg(80, 60, 10)
    assert collector_partial._server_ring[-1]["accelerator_power_watts"] == 460.0  # sum(150, 110, 200)


@pytest.mark.asyncio
async def test_live_summary_for_historical_trace_replayer_task_never_crashes(tmp_path: Path):
    task = _make_task("ff000001", status="running", backend="trace-replayer")
    assert await service._live_summary_for_task(task) == {}

    collector = _make_collector(tmp_path, task=task)
    collector._ingest_request_record(
        {
            "request_id": "hist-1",
            "s_time": 100.0,
            "e_time": 200.0,
            "input_length": 10,
            "output_length": 5,
            "status": 200,
        }
    )
    live_telemetry_registry.register(task.id, collector)
    try:
        summary = await service._live_summary_for_task(task)
        assert summary["total_requests"] == 1
    finally:
        live_telemetry_registry.unregister(task.id)


def test_aiperf_live_collector_filters_phase_kind_warmup_and_uses_usage_token_fallbacks(tmp_path: Path):
    collector = _make_collector(tmp_path, task=_make_task("b2c3d4e6", backend="aiperf"))
    origin_ns = 1_795_597_273_000_000_000

    # Warmup signaled via phase_kind="warmup" even when benchmark_phase="profiling" must be ignored
    collector._ingest_request_record(
        {
            "metadata": {
                "request_id": "warmup-1",
                "benchmark_phase": "profiling",
                "phase_kind": "warmup",
                "request_start_ns": origin_ns,
                "request_end_ns": origin_ns + 500_000_000,
            },
            "metrics": {
                "usage_prompt_tokens": {"value": 999},
                "usage_completion_tokens": {"value": 999},
                "time_to_first_token": {"value": 999.0},
            },
        }
    )
    assert collector.completed_count() == 0

    # Profiling record with usage_prompt_tokens / usage_completion_tokens fallbacks
    collector._ingest_request_record(
        {
            "metadata": {
                "request_id": "prof-1",
                "benchmark_phase": "profiling",
                "phase_kind": "profiling",
                "request_start_ns": origin_ns,
                "request_end_ns": origin_ns + 2_000_000_000,
            },
            "metrics": {
                "usage_prompt_tokens": {"value": 150},
                "usage_completion_tokens": {"value": 35},
                "time_to_first_token": {"value": 120.0},
                "request_latency": {"value": 2000.0},
            },
        }
    )
    # Explicit 0.0 input_sequence_length must NOT fall back to usage_prompt_tokens; missing tokens default to 0.0
    collector._ingest_request_record(
        {
            "metadata": {
                "request_id": "prof-2",
                "benchmark_phase": "profiling",
                "request_start_ns": origin_ns + 2_000_000_000,
                "request_end_ns": origin_ns + 3_000_000_000,
            },
            "metrics": {
                "input_sequence_length": {"value": 0},
                "usage_prompt_tokens": {"value": 500},
                "request_latency": {"value": 1000.0},
            },
        }
    )
    assert collector.completed_count() == 2
    assert collector._total_input_tokens == pytest.approx(150.0)
    assert collector._total_output_tokens == pytest.approx(35.0)
