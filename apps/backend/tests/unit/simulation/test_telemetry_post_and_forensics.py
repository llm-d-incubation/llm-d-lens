"""Unit tests for post-run telemetry, hardware/DRA profiling, redaction, correlation, and forensics."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.hardware.registry import get_profile
from llm_d_lens.hardware.telemetry import DEVICE_RESULT_KEYS
from llm_d_lens.monitoring.profiling import service as profiling_service
from llm_d_lens.monitoring.prometheus import PrometheusEndpoint
from llm_d_lens.simulation import telemetry_post
from llm_d_lens.simulation.backends.analytics import latency_heatmap
from llm_d_lens.simulation.forensics import (
    ForensicsAnalyzer,
    _reconcile_pd_engine_overhead,
    analyze_forensics,
)
from llm_d_lens.simulation.models import (
    ArtifactRecord,
    SimulationConfig,
    SimulationDataset,
    SimulationLatencyHeatmap,
    SimulationPrompt,
    SimulationTask,
    SimulationTrace,
)
from llm_d_lens.simulation.telemetry_live import redact_secret_text


def _make_task(
    task_id: str = "b1c2d3e4",
    *,
    endpoint_mode: str = "external",
    endpoint_cluster_id: str | None = None,
    endpoint_namespace: str | None = None,
    endpoint_deployment_execution_id: str | None = None,
    endpoint_deployment_name: str | None = None,
    endpoint_service: str | None = None,
    model_service_group_id: str | None = None,
    trace_path: str = "trace.jsonl",
    trace_format: str = "bailian_trace",
    duration_seconds: int = 60,
    backend: str = "aiperf",
    backend_options: dict | None = None,
) -> SimulationTask:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"Task {task_id}",
        description="",
        scenario="chat",
        status="completed",
        task_dir=f"/tmp/{task_id}",
        progress_percent=100,
        progress_message="",
        logs=[],
        endpoint_url="http://127.0.0.1:8000",
        endpoint_mode=endpoint_mode,
        endpoint_cluster_id=endpoint_cluster_id,
        endpoint_namespace=endpoint_namespace,
        endpoint_deployment_execution_id=endpoint_deployment_execution_id,
        endpoint_deployment_name=endpoint_deployment_name,
        endpoint_service=endpoint_service,
        model_service_group_id=model_service_group_id,
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
                path=trace_path,
                format=trace_format,
                start_seconds=0.0,
                end_seconds=float(duration_seconds),
                synthesis_speedup_ratio=1.0,
                fixed_schedule=False,
                timeout_seconds=60.0,
            ),
        ),
        created_at=now,
        updated_at=now,
    )


def test_hardware_profile_nvidia_and_direct_profiles():
    nvidia = get_profile("nvidia")
    intel = get_profile("intel-xpu")
    assert nvidia is not None
    assert intel is not None
    assert nvidia.telemetry is not None
    sources = nvidia.telemetry.device_metric_sources
    assert "framebuffer_free" in sources
    assert "power" in sources
    assert "temperature" in sources
    assert DEVICE_RESULT_KEYS["framebuffer_free"] == "gpu_framebuffer_free_bytes"

    # NVIDIA DRA has allocation_join="dra" (via mode) or "none" at top-level; simulate DRA mode override.
    nvidia_dra = replace(
        nvidia,
        telemetry=replace(nvidia.telemetry, allocation_join="dra"),
    )
    direct = profiling_service._direct_profiles([nvidia_dra, intel])
    direct_ids = {p.id for p in direct}
    # NVIDIA DRA has no 'pci' label in label_schema -> uses direct queries.
    assert "nvidia" in direct_ids
    # Intel XPUM DRA has 'pci' label in label_schema -> handled by aggregate_xpum, excluded from direct.
    assert "intel-xpu" not in direct_ids


def test_role_from_prom_labels_flow_map_compatibility():
    assert (
        profiling_service._role_from_prom_labels({"llm_d_ai_role": "prefill"}, "pod-0", is_vllm_metric=True)
        == "prefill"
    )
    assert (
        profiling_service._role_from_prom_labels({"llm-d.ai/role": "decode"}, "pod-1", is_vllm_metric=True) == "decode"
    )
    assert profiling_service._role_from_prom_labels({}, "my-epp-pod", is_vllm_metric=True) is None
    assert profiling_service._role_from_prom_labels({}, "vllm-worker-0", is_vllm_metric=True) == "decode"


@pytest.mark.asyncio
async def test_collect_namespace_observability_unavailable_and_non_admin(monkeypatch):
    async def _no_prom(_cid):
        return None

    monkeypatch.setattr(profiling_service, "_prometheus_endpoint", _no_prom)
    unavail = await profiling_service.collect_namespace_observability(
        "c1", "ns1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"
    )
    assert unavail["status"] == "unavailable"
    assert unavail["series"] == []

    # Non-Admin / K8s list_resources forbidden still degrades gracefully with Prometheus available.
    async def _prom_ep(_cid):
        return PrometheusEndpoint(base_url="http://prom:9090", source="explicit_url")

    async def _forbidden_list(*_args, **_kwargs):
        raise PermissionError("403 Forbidden")

    async def _no_discover(*_args, **_kwargs):
        return {"epp": [], "prefill": [], "decode": [], "model-server": []}

    @asynccontextmanager
    async def _fake_client(*_args, **_kwargs):
        yield object()

    async def _fake_query_range(_client, query: str, *_args, **_kwargs):
        if "vllm:request_success_total" in query:
            return [{"metric": {"pod": "worker-0"}, "values": [[1767225600, "2.0"]]}]
        return []

    async def _clock(_client):
        return 0.0

    monkeypatch.setattr(profiling_service, "_prometheus_endpoint", _prom_ep)
    monkeypatch.setattr(profiling_service, "list_resources", _forbidden_list)
    monkeypatch.setattr(profiling_service, "_discover_components", _no_discover)
    monkeypatch.setattr(profiling_service, "create_prometheus_client", _fake_client)
    monkeypatch.setattr(profiling_service, "_query_range", _fake_query_range)
    monkeypatch.setattr(profiling_service, "_prometheus_clock_offset", _clock)

    res = await profiling_service.collect_namespace_observability(
        "c1", "ns1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"
    )
    assert res["status"] == "available"
    assert res["topology_mode"] == "aggregated"
    assert len(res["series"]) >= 1


@pytest.mark.asyncio
async def test_topology_mode_aggregated_vs_disaggregated_and_vram_derivation(monkeypatch):
    nvidia = get_profile("nvidia")
    assert nvidia is not None and nvidia.telemetry is not None
    # Simulate DRA mode where nvidia has framebuffer_used + framebuffer_free, and no dedicated vram source.
    sources_no_vram = {k: v for k, v in nvidia.telemetry.device_metric_sources.items() if k != "vram"}
    nvidia_dra = replace(
        nvidia,
        telemetry=replace(
            nvidia.telemetry,
            allocation_join="dra",
            device_metric_sources=sources_no_vram,
        ),
    )

    async def _prom_ep(_cid):
        return PrometheusEndpoint(base_url="http://prom:9090", source="explicit_url")

    async def _clock(_client):
        return 0.0

    @asynccontextmanager
    async def _fake_client(*_args, **_kwargs):
        yield object()

    async def _fake_query_range(_client, query: str, *_args, **_kwargs):
        if "DCGM_FI_DEV_FB_USED" in query:
            return [{"metric": {"pod": "worker-0"}, "values": [[1767225600, str(1024 * 1024 * 10)]]}]
        if "DCGM_FI_DEV_FB_FREE" in query:
            return [{"metric": {"pod": "worker-0"}, "values": [[1767225600, str(1024 * 1024 * 30)]]}]
        if "vllm:num_requests_running" in query:
            return [{"metric": {"pod": "worker-0"}, "values": [[1767225600, "3"]]}]
        return []

    async def _fake_query(*_args, **_kwargs):
        return []

    monkeypatch.setattr(profiling_service, "_prometheus_endpoint", _prom_ep)
    monkeypatch.setattr(profiling_service, "_prometheus_clock_offset", _clock)
    monkeypatch.setattr(profiling_service, "create_prometheus_client", _fake_client)
    monkeypatch.setattr(profiling_service, "_query", _fake_query)
    monkeypatch.setattr(profiling_service, "_query_range", _fake_query_range)
    monkeypatch.setattr(profiling_service, "_hardware_profiles", lambda: [nvidia_dra])

    # 1. Aggregated: workers normalized to role="worker"
    async def _agg_discover(*_args, **_kwargs):
        return {"epp": [], "prefill": [], "decode": ["worker-0"], "model-server": []}

    async def _agg_resources(kind: str, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0", "labels": {}},
                    "spec": {"containers": [{"name": "vllm", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                }
            ]
        return []

    monkeypatch.setattr(profiling_service, "_discover_components", _agg_discover)
    monkeypatch.setattr(profiling_service, "list_resources", _agg_resources)

    agg = await profiling_service.collect_namespace_observability(
        "c1", "ns1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z"
    )
    assert agg["topology_mode"] == "aggregated"
    assert agg["per_pod"][0]["role"] == "worker"
    # Derived total VRAM = framebuffer_used + framebuffer_free (40 MiB in bytes)
    assert agg["summary"]["gpu_memory_total_bytes"]["mean"] == pytest.approx(1024 * 1024 * 40.0)

    # 2. Disaggregated: prefill/decode pods retain their roles
    async def _disagg_discover(*_args, **_kwargs):
        return {"epp": [], "prefill": ["worker-0"], "decode": [], "model-server": []}

    monkeypatch.setattr(profiling_service, "_discover_components", _disagg_discover)
    disagg = await profiling_service.collect_namespace_observability(
        "c1", "ns1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z"
    )
    assert disagg["topology_mode"] == "disaggregated"
    assert disagg["per_pod"][0]["role"] == "prefill"


def test_compute_energy_wh_trapezoidal_and_gap_skipping():
    # Two contiguous 5s intervals: [0..5] avg 100W -> 500 Ws; [5..10] avg 200W -> 1000 Ws.
    # Gap from 10 -> 30 (dt=20 > 3*5=15) is skipped.
    # [30..35] avg 150W -> 750 Ws. Total = 2250 Ws = 0.625 Wh.
    samples = [
        (35.0, 100.0),
        (0.0, 100.0),
        (5.0, 100.0),
        (10.0, 300.0),
        (30.0, 200.0),
    ]
    assert telemetry_post.compute_energy_wh(samples, step_seconds=5.0) == pytest.approx(2250.0 / 3600.0, rel=1e-6)
    assert telemetry_post.compute_energy_wh([(0.0, 100.0)], step_seconds=5.0) is None
    assert telemetry_post.compute_energy_wh([(0.0, 100.0), (50.0, 100.0)], step_seconds=5.0) is None


@pytest.mark.asyncio
async def test_resolve_target_deployments_across_all_modes(monkeypatch):
    # External without group/exec -> empty
    ext_task = _make_task(endpoint_mode="external")
    assert await telemetry_post.resolve_target_deployments(ext_task) == []

    # Fake deploy.executions and model_service.service
    from llm_d_lens.deploy import executions as deploy_execs
    from llm_d_lens.model_service import service as ms_service

    fake_ctxs = {
        "exec-dep": SimpleNamespace(
            execution_id="exec-dep", display_name="Dep A", namespace="ns-a", cluster_id="c-a", status="running"
        ),
        "exec-ms": SimpleNamespace(
            execution_id="exec-ms", display_name="MS Member", namespace="ns-ms", cluster_id="c-ms", status="ready"
        ),
        "exec-ic": SimpleNamespace(
            execution_id="exec-ic", display_name="InCluster Pod", namespace="ns-ic", cluster_id="c-ic", status="running"
        ),
    }

    monkeypatch.setattr(deploy_execs, "find_execution_context", lambda eid: fake_ctxs.get(eid))
    monkeypatch.setattr(deploy_execs, "list_execution_contexts", lambda cluster_id="": list(fake_ctxs.values()))

    class FakeMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="Group 1")

        def list_members(self, gid):
            return [
                SimpleNamespace(
                    execution_id="exec-ms",
                    cluster_id="c-ms",
                    target_namespace="ns-ms",
                    target_service="svc-ms",
                    status="ready",
                ),
                SimpleNamespace(
                    execution_id="exec-other-cluster",
                    cluster_id="other-c",
                    target_namespace="ns-other",
                    target_service="svc-other",
                    status="ready",
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: FakeMS())

    # 1. deployment mode
    dep_task = _make_task(endpoint_mode="deployment", endpoint_deployment_execution_id="exec-dep")
    dep_res = await telemetry_post.resolve_target_deployments(dep_task)
    assert [d["execution_id"] for d in dep_res] == ["exec-dep"]

    # 2. model_service_group_id mode (filters by endpoint_cluster_id)
    ms_task = _make_task(
        endpoint_mode="deployment",
        model_service_group_id="grp-1",
        endpoint_cluster_id="c-ms",
    )
    ms_res = await telemetry_post.resolve_target_deployments(ms_task)
    assert [d["execution_id"] for d in ms_res] == ["exec-ms"]

    # 3. in-cluster mode
    ic_task = _make_task(endpoint_mode="in-cluster", endpoint_namespace="ns-ic", endpoint_cluster_id="c-ic")
    ic_res = await telemetry_post.resolve_target_deployments(ic_task)
    assert [d["execution_id"] for d in ic_res] == ["exec-ic"]


def test_correlate_trace_sessions_bailian_mooncake_and_path_safety(tmp_path: Path, monkeypatch):
    trace_root = tmp_path / "traces"
    trace_root.mkdir()
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: trace_root))

    # 1. Bailian trace with unsorted timestamps, BASETIME alignment, drift, and hash_ids
    bailian_file = trace_root / "bailian.jsonl"
    bailian_entries = [
        {"timestamp": 100.0, "input_length": 120, "output_length": 20, "chat_id": "c1", "turn": 1, "hash_ids": [1, 2]},
        {
            "timestamp": 105.0,
            "input_length": 200,
            "output_length": 30,
            "chat_id": "c1",
            "turn": 2,
            "hash_ids": [1, 2, 3],
        },
        {
            "timestamp": 102.0,
            "input_length": 80,
            "output_length": 10,
            "chat_id": "c2",
            "parent_chat_id": "c1",
            "turn": 1,
            "hash_ids": [1],
        },
    ]
    # Write intentionally unsorted (105s before 102s)
    bailian_file.write_text(
        "\n".join(json.dumps(bailian_entries[i]) for i in (0, 2, 1)) + "\n",
        encoding="utf-8",
    )

    task = _make_task(trace_path="bailian.jsonl", trace_format="bailian_trace")
    task.task_dir = str(tmp_path / "data" / "simulation" / "tasks" / task.id)
    # Write stdout.log with BASETIME
    task_dir = Path(task.task_dir) / "artifacts"
    task_dir.mkdir(parents=True)
    basetime_epoch = 1767225600.0  # 2026-01-01T00:00:00Z
    (task_dir / "stdout.log").write_text("2026-01-01T00:00:00Z INFO started\n", encoding="utf-8")

    # Records with s_time_drift_ms shifting arrived back to scheduled_ms within ±5ms
    records = [
        ArtifactRecord(
            request_id="r1",
            successful=True,
            arrived=basetime_epoch + 0.002,
            completed=basetime_epoch + 0.5,
            ttft_ms=40.0,
            input_tokens=120,
            output_tokens=20,
            s_time_drift_ms=2.0,
        ),
        ArtifactRecord(
            request_id="r2",
            successful=True,
            arrived=basetime_epoch + 2.004,
            completed=basetime_epoch + 2.4,
            ttft_ms=30.0,
            input_tokens=80,
            output_tokens=10,
            s_time_drift_ms=2.0,
        ),
        ArtifactRecord(
            request_id="r3",
            successful=True,
            arrived=basetime_epoch + 5.010,
            completed=basetime_epoch + 5.8,
            ttft_ms=20.0,
            input_tokens=200,
            output_tokens=30,
            s_time_drift_ms=10.0,
        ),
    ]

    res = telemetry_post.correlate_trace_sessions(task, records)
    assert res["available"] is True
    assert res["correlation"]["matched_count"] == 3
    assert res["correlation"]["total_count"] == 3
    assert res["correlation"]["matched_ratio"] == 1.0
    assert len(res["lanes"]) == 1
    assert res["session_ids"] == ["c1"]
    assert res["turn_distribution"] == [{"turns": 3, "sessions": 1}]
    assert res["tree_depth_distribution"] == [{"depth": 2, "sessions": 1}]
    assert len(res["lanes"][0]) == 5  # compact 5-tuple: [idx, start_s, end_s, cache_cls, ttft_ms]

    # 2. Mooncake / sessionless trace -> honest available: False
    mooncake_file = trace_root / "mooncake.jsonl"
    mooncake_file.write_text(
        json.dumps({"timestamp": 1000, "input_length": 64, "output_length": 16, "hash_ids": [10, 11]}) + "\n",
        encoding="utf-8",
    )
    mc_task = _make_task(trace_path="mooncake.jsonl", trace_format="mooncake_trace")
    mc_res = telemetry_post.correlate_trace_sessions(mc_task, records)
    assert mc_res["available"] is False
    assert mc_res["correlation"]["matched_count"] == 0

    # 3. Symlink & outside-trace_root rejection
    outside = tmp_path / "secret.jsonl"
    outside.write_text(json.dumps({"timestamp": 1, "chat_id": "x"}) + "\n", encoding="utf-8")
    symlink_trace = trace_root / "evil_link.jsonl"
    symlink_trace.symlink_to(outside)

    sym_task = _make_task(trace_path="evil_link.jsonl", trace_format="bailian_trace")
    assert telemetry_post.correlate_trace_sessions(sym_task, records)["available"] is False

    traversal_task = _make_task(trace_path="../secret.jsonl", trace_format="bailian_trace")
    assert telemetry_post.correlate_trace_sessions(traversal_task, records)["available"] is False


def test_secret_redaction_and_safe_key_preservation():
    # Pre-truncation redaction with small max_len
    long_secret = "HF_TOKEN=" + ("a" * 80)
    truncated = redact_secret_text(long_secret, max_len=25)
    assert truncated is not None
    assert "aaaa" not in truncated
    assert "[REDACTED]" in truncated
    assert len(truncated) <= 25

    # max_len=0 does not truncate
    full_redacted = redact_secret_text("Authorization: Bearer sk-1234567890abcdef", max_len=0)
    assert full_redacted == "Authorization: Bearer [REDACTED]"

    # --api-key and single-token URL userinfo
    assert "--api-key [REDACTED]" in (redact_secret_text("vllm --api-key mysecret123") or "")
    assert redact_secret_text("https://tok123@example.com/v1") == "https://[REDACTED]@example.com/v1"

    # Manifest payload redaction preserves input_token_rate_tps while redacting sensitive nested dicts
    payload = {
        "input_token_rate_tps": 1234.5,
        "api_tokens": {"primary": "secret-val", "nested": ["tok-1"]},
        "kubeconfig": {"clusters": [{"server": "https://u:p@k8s.local"}]},
        "args": ["--api-key", "secret-cli"],
    }
    redacted = telemetry_post.redact_manifest_payload(payload)
    assert redacted["input_token_rate_tps"] == 1234.5
    assert redacted["api_tokens"]["primary"] == "<redacted>"
    assert redacted["api_tokens"]["nested"] == ["<redacted>"]
    assert redacted["kubeconfig"]["clusters"][0]["server"] == "<redacted>"


def test_forensics_all_8_verdict_rules_and_sections():
    base_client = {
        "warmup_seconds": 0.0,
        "duration_seconds": 60.0,
        "origin_epoch": 1000.0,
        "buckets": [{"mid_seconds": 5.0, "failed_requests": 0}],
        "windows": {
            "full": {
                "request_count": 100,
                "successful_requests": 100,
                "error_rate_pct": 0.0,
                "slo_attainment_pct": 99.0,
                "goodput_rps": 20.0,
                "throughput_rps": 20.0,
                "ttft_ms": {"p50": 100.0, "p90": 200.0},
                "tpot_ms": {"p50": 10.0, "p90": 20.0},
                "e2e_ms": {"p50": 300.0, "p90": 800.0},
                "drift_ms": {"p50": 5.0, "p90": 20.0},
            }
        },
    }
    base_server = {
        "status": "available",
        "topology_mode": "aggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "kv_cache_usage_percent": 30.0,
                "queue_depth": 0.0,
                "running_requests": 4.0,
                "vllm_preemptions_total": 0.0,
            }
        ],
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "queue_depth": {"mean": 0.0},
            "gpu_utilization_percent": {"mean": 75.0},
            "vllm_queue_time_p50_ms": {"mean": 10.0},
            "vllm_queue_time_p90_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 80.0},
            "vllm_prefill_time_p90_ms": {"mean": 150.0},
            "vllm_decode_time_p90_ms": {"mean": 100.0},
            "vllm_ttft_p90_ms": {"mean": 200.0},
            "prefix_cache_hit_percent": {"mean": 80.0},
            "input_token_rate_tps": {"mean": 500.0},
        },
        "router": {"scheduler_latency_p50_ms": {"mean": 2.0}, "scheduler_latency_p90_ms": {"mean": 5.0}},
        "derived": {"avoided_prefill_ratio": 82.0},
        "per_pod": [
            {"pod": "worker-0", "role": "worker", "request_rate_rps": {"mean": 10.0}},
            {"pod": "worker-1", "role": "worker", "request_rate_rps": {"mean": 10.0}},
        ],
    }

    task = _make_task()

    # 8. healthy
    healthy = analyze_forensics(task, client_series=base_client, server_telemetry=base_server)
    assert healthy["verdict"]["code"] == "healthy"
    assert healthy["telemetry_scope"] == "full"
    assert healthy["timeline_buckets"]
    assert healthy["ttft_decomposition"]["available"] is True
    assert healthy["routing_vs_cache"]["available"] is True
    assert healthy["pod_skew"]["available"] is True

    # 7. routing_cache_miss (pod_skew_ratio >= 2.0)
    srv7 = json.loads(json.dumps(base_server))
    srv7["per_pod"] = [
        {"pod": "worker-0", "role": "worker", "request_rate_rps": {"mean": 25.0}},
        {"pod": "worker-1", "role": "worker", "request_rate_rps": {"mean": 1.0}},
        {"pod": "worker-2", "role": "worker", "request_rate_rps": {"mean": 1.0}},
    ]
    assert (
        analyze_forensics(task, client_series=base_client, server_telemetry=srv7)["verdict"]["code"]
        == "routing_cache_miss"
    )

    # 6. decode_bound (tpot_p90 > 100ms, ttft_ok)
    cli6 = json.loads(json.dumps(base_client))
    cli6["windows"]["full"]["tpot_ms"]["p90"] = 150.0
    assert analyze_forensics(task, client_series=cli6, server_telemetry=srv7)["verdict"]["code"] == "decode_bound"

    # 5. prefill_bound (ttft_p90 > 2000ms and ttft/e2e >= 0.5)
    cli5 = json.loads(json.dumps(cli6))
    cli5["windows"]["full"]["ttft_ms"]["p90"] = 2500.0
    cli5["windows"]["full"]["e2e_ms"]["p90"] = 3000.0
    assert analyze_forensics(task, client_series=cli5, server_telemetry=srv7)["verdict"]["code"] == "prefill_bound"

    # 4. queue_bound (waiting_sustained_frac >= 0.5)
    srv4 = json.loads(json.dumps(srv7))
    srv4["series"][0]["queue_depth"] = 5.0
    assert analyze_forensics(task, client_series=cli5, server_telemetry=srv4)["verdict"]["code"] == "queue_bound"

    # 3. kv_cache_eviction (kv_high_frac >= 0.20)
    srv3 = json.loads(json.dumps(srv4))
    srv3["series"][0]["kv_cache_usage_percent"] = 95.0
    assert analyze_forensics(task, client_series=cli5, server_telemetry=srv3)["verdict"]["code"] == "kv_cache_eviction"

    # 2. client_schedule_drift (drift_p90 > 500ms & accel_util < 60%)
    cli2 = json.loads(json.dumps(cli5))
    cli2["windows"]["full"]["drift_ms"]["p90"] = 800.0
    srv2 = json.loads(json.dumps(srv3))
    srv2["summary"]["gpu_utilization_percent"]["mean"] = 40.0
    assert (
        analyze_forensics(task, client_series=cli2, server_telemetry=srv2)["verdict"]["code"] == "client_schedule_drift"
    )

    # 1. error_saturation (err_rate >= 5%)
    cli1 = json.loads(json.dumps(cli2))
    cli1["windows"]["full"]["error_rate_pct"] = 12.0
    assert analyze_forensics(task, client_series=cli1, server_telemetry=srv2)["verdict"]["code"] == "error_saturation"

    # Legacy & partial fallbacks
    partial_res = ForensicsAnalyzer().analyze(task, client_series=base_client, server_telemetry=None, partial=True)
    assert partial_res["telemetry_scope"] == "partial"
    legacy_res = ForensicsAnalyzer().analyze(task, client_series=base_client, server_telemetry=None, legacy=True)
    assert legacy_res["telemetry_scope"] == "client_only_legacy"


def test_build_client_series_completion_bucketing_burst_arrivals_and_schema_v2():
    # Match bc8de4e8's burst-arrival shape: 46 requests arrive in 0..10s (33 in 0..5s, 13 in 5..10s),
    # while completions span buckets 1..6 (5..32s: [6, 5, 13, 16, 5, 1]).
    task = _make_task("bc8de4e8", duration_seconds=32)
    origin = 1795597273.0
    comp_counts = [0, 6, 5, 13, 16, 5, 1]
    records: list[ArtifactRecord] = []
    arr_times = [origin + 0.1 * i for i in range(33)] + [origin + 5.1 + 0.3 * i for i in range(13)]
    comp_times: list[float] = []
    for b_idx, cnt in enumerate(comp_counts):
        for j in range(cnt):
            if b_idx == 6:
                comp_times.append(origin + 31.966)
            else:
                comp_times.append(origin + b_idx * 5.0 + 0.5 + 0.2 * j)

    for idx, (arr_t, comp_t) in enumerate(zip(arr_times, comp_times, strict=True)):
        e2e_ms = (comp_t - arr_t) * 1000.0
        records.append(
            ArtifactRecord(
                request_id=f"req-{idx}",
                arrived=arr_t,
                completed=comp_t,
                successful=True,
                input_tokens=200 + idx * 10,
                output_tokens=64,
                requested_output_tokens=64,
                ttft_ms=150.0 + idx * 5.0,
                tpot_ms=20.0 + (idx % 5),
                latency_ms=e2e_ms,
                s_time_drift_ms=12.0 + idx,
            )
        )

    cs = telemetry_post.build_client_series(task, records)
    assert cs["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION == 15
    buckets = cs["buckets"]
    # Trailing partial bucket [30, 31.966] (width 1.966s < 2.5s) merges into [25, 31.966]
    assert len(buckets) == 6

    # Bucket 0 (0..5s): 33 arrivals, 0 completions -> ttft/tpot/e2e are None, drift_ms is populated
    assert buckets[0]["arrived_requests"] == 33
    assert buckets[0]["completed_requests"] == 0
    assert buckets[0]["ttft"] == {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None}
    assert buckets[0]["tpot"] == {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None}
    assert buckets[0]["e2e"] == {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None}
    assert buckets[0]["drift_ms"]["p50"] is not None

    # Buckets 1..5 (5..31.966s): all have populated ttft/tpot/e2e matching completed_requests;
    # buckets 2..5 have no arrivals so drift_ms stays None.
    merged_comp_counts = [0, 6, 5, 13, 16, 6]
    for idx in range(1, 6):
        assert buckets[idx]["completed_requests"] == merged_comp_counts[idx]
        assert buckets[idx]["ttft"]["p50"] is not None
        assert buckets[idx]["tpot"]["p50"] is not None
        assert buckets[idx]["e2e"]["p50"] is not None
    assert buckets[5]["start_seconds"] == 25
    assert buckets[5]["end_seconds"] == 31.966
    for idx in range(2, 6):
        assert buckets[idx]["arrived_requests"] == 0
        assert buckets[idx]["drift_ms"] == {
            "min": None,
            "p50": None,
            "mean": None,
            "p90": None,
            "p99": None,
            "max": None,
        }

    # Heatmaps use observation/completion time and span into the later completion window
    assert max(c["end_seconds"] for c in cs["heatmap"]["ttft"]["cells"]) > 25.0
    assert max(c["end_seconds"] for c in cs["heatmap"]["tpot"]["cells"]) > 25.0

    # Forensics runs cleanly when bucket 0 has completed=0 and latency stats None
    forensics = analyze_forensics(task, client_series=cs)
    assert forensics["verdict"]["code"] in {"healthy", "prefill_bound", "decode_bound"}

    # Fallback: when completed is None, latency buckets fall back to arr_rel
    fallback_rec = ArtifactRecord(
        request_id="req-fallback",
        arrived=origin + 1.0,
        completed=None,
        successful=False,
        issue_failed=True,
        ttft_ms=500.0,
        tpot_ms=None,
        latency_ms=1000.0,
    )
    cs_fb = telemetry_post.build_client_series(task, [fallback_rec])
    assert cs_fb["buckets"][0]["ttft"]["p50"] == 500.0
    assert cs_fb["buckets"][0]["e2e"]["p50"] == 1000.0


@pytest.mark.asyncio
async def test_service_get_task_telemetry_auto_rederives_stale_v1_and_respects_warmup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from llm_d_lens.simulation import service

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        task_id = "bc8de4e8"
        from llm_d_lens.simulation.models import SimulationResult

        task = _make_task(task_id, duration_seconds=32)
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 6, "successful_requests": 6, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        backend_dir = task_dir / "artifacts" / "aiperf"
        report_dir = task_dir / "artifacts" / "report"
        backend_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        # Write burst-arrival / delayed-completion profile_export.jsonl (arrivals in 0..8s, completions in 6..28s)
        origin_ns = 1_795_597_273_000_000_000
        req_lines = [
            {
                "metadata": {
                    "request_id": f"r-{i}",
                    "benchmark_phase": "profiling",
                    "request_start_ns": origin_ns + (500 + i * 1000) * 1_000_000,
                    "request_end_ns": origin_ns + (12500 + i * 2500) * 1_000_000,
                    "credit_issued_ns": origin_ns + (495 + i * 1000) * 1_000_000,
                    "status_code": 200,
                },
                "metrics": {
                    "input_sequence_length": {"value": 128},
                    "output_sequence_length": {"value": 32},
                    "time_to_first_token": {"value": 200.0 + i * 10.0},
                    "inter_token_latency": {"value": 25.0},
                    "request_latency": {"value": 12000.0 + i * 1500.0},
                },
            }
            for i in range(6)
        ]
        (backend_dir / "profile_export.jsonl").write_text(
            "\n".join(json.dumps(line) for line in req_lines) + "\n",
            encoding="utf-8",
        )

        # Write stale v1 client-series.json (no schema_version) + forensics.json + index.html
        stale_cs = {
            "warmup_seconds": 0.0,
            "duration_seconds": 32.0,
            "bucket_seconds": 5,
            "buckets": [
                {
                    "index": 0,
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "mid_seconds": 2.5,
                    "ttft": {"p50": 200.0, "p90": 240.0, "p99": 240.0, "mean": 220.0},
                },
                {
                    "index": 4,
                    "start_seconds": 20,
                    "end_seconds": 25,
                    "mid_seconds": 22.5,
                    "ttft": {"p50": None, "p90": None, "p99": None, "mean": None},
                },
            ],
            "windows": {"full": {"request_count": 6, "successful_requests": 6, "error_rate_pct": 0.0}},
        }
        (report_dir / "client-series.json").write_text(json.dumps(stale_cs), encoding="utf-8")
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 1, "verdict": {"code": "healthy"}, "telemetry_scope": "client_only"}),
            encoding="utf-8",
        )
        (report_dir / "index.html").write_text("<html>stale</html>", encoding="utf-8")
        await service.save_task(task)

        # 1. Custom warmup_seconds re-derives in memory WITHOUT overwriting persisted v1 files
        custom_tel = await service.get_task_telemetry(task_id, warmup_seconds=5.0)
        assert custom_tel["client_series"]["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        assert custom_tel["client_series"]["warmup_seconds"] == 5.0
        persisted_before = json.loads((report_dir / "client-series.json").read_text(encoding="utf-8"))
        assert persisted_before.get("schema_version") is None
        assert (report_dir / "index.html").read_text(encoding="utf-8") == "<html>stale</html>"

        # 2. Default warmup_seconds=None re-derives AND persists updated v2 files + refreshes index.html
        default_tel = await service.get_task_telemetry(task_id)
        cs_v2 = default_tel["client_series"]
        assert cs_v2["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        assert cs_v2["buckets"][0]["completed_requests"] == 0
        assert cs_v2["buckets"][0]["ttft"]["p50"] is None
        assert cs_v2["buckets"][0]["drift_ms"]["p50"] == 5.0
        assert cs_v2["buckets"][4]["completed_requests"] > 0
        assert cs_v2["buckets"][4]["ttft"]["p50"] is not None

        persisted_after = json.loads((report_dir / "client-series.json").read_text(encoding="utf-8"))
        assert persisted_after["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        assert persisted_after["buckets"][4]["ttft"]["p50"] is not None
        persisted_forensics = json.loads((report_dir / "forensics.json").read_text(encoding="utf-8"))
        assert persisted_forensics["telemetry_scope"] == "client_only"
        assert persisted_forensics["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION == 8
        assert (report_dir / "index.html").read_text(encoding="utf-8") != "<html>stale</html>"

        # 3. Read-only failure while persisting stale refresh is tolerated gracefully
        (report_dir / "client-series.json").write_text(json.dumps(stale_cs), encoding="utf-8")

        def _deny_write(*_args, **_kwargs):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(service, "persist_refreshed_report_artifacts", _deny_write)
        ro_tel = await service.get_task_telemetry(task_id)
        assert ro_tel["client_series"]["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        assert ro_tel["client_series"]["buckets"][4]["ttft"]["p50"] is not None
    finally:
        service.reset_service_state()


def test_forensics_rejects_bogus_queue_bound_on_zero_waiting_samples(tmp_path: Path) -> None:
    """Prometheus le='0.3' histogram interpolation with 0% waiting samples must not trigger queue_bound."""
    task = _make_task("bc8de401").model_copy(update={"task_dir": str(tmp_path)})
    client_series = {
        "schema_version": telemetry_post.REPORT_SCHEMA_VERSION,
        "warmup_seconds": 1.0,
        "duration_seconds": 32.0,
        "origin_epoch": 1700000000.0,
        "buckets": [],
        "windows": {
            "profiling": {
                "request_count": 46,
                "successful_requests": 46,
                "error_rate_pct": 0.0,
                "slo_attainment_pct": 100.0,
                "throughput_rps": 1.44,
                "goodput_rps": 1.44,
                "ttft_ms": {"p50": 225.7, "p90": 911.9},
                "tpot_ms": {"p90": 18.0},
                "e2e_ms": {"p90": 1400.0},
                "drift_ms": {"p90": 2.1},
            }
        },
    }
    server_telemetry = {
        "status": "available",
        "available": True,
        "topology_mode": "aggregated",
        "step_seconds": 5,
        "summary": {
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 150.0},
            "vllm_queue_time_p90_ms": {"mean": 270.0},
            "vllm_prefill_time_p50_ms": {"mean": 154.5},
            "vllm_prefill_time_p90_ms": {"mean": 278.2},
            "vllm_ttft_p90_ms": {"mean": 421.0},
            "vllm_preemptions_per_second": {"mean": 0.0},
            "kv_cache_usage_percent": {"mean": 20.6, "max": 40.0},
            "gpu_utilization_percent": {"mean": 76.3},
        },
        "series": [
            {
                "timestamp": "2023-11-14T22:13:22Z",
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 15.0,
                "running_requests": 4.0,
                "vllm_preemptions_per_second": 0.0,
            },
            {
                "timestamp": "2023-11-14T22:13:27Z",
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 35.0,
                "running_requests": 12.0,
                "vllm_preemptions_per_second": 0.0,
            },
        ],
    }

    forensics = analyze_forensics(task, client_series=client_series, server_telemetry=server_telemetry)
    assert forensics["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION == 8
    assert forensics["verdict"]["code"] == "healthy"
    assert not any(f.get("code") == "queue_wait" and f.get("value") == "0%" for f in forensics["findings"])
    q_rule = next(r for r in forensics["rules_evaluated"] if r["code"] == "queue_bound")
    assert q_rule["triggered"] is False


def test_sanitize_server_telemetry_and_forensics_timeline_exclude_client_epp_and_phantom_prefill(
    tmp_path: Path,
) -> None:
    """Client pods, EPP misclassification, phantom prefill, and 5s timeline sawtooths are all cleaned."""
    task = _make_task("bc8de402").model_copy(update={"task_dir": str(tmp_path)})
    raw_server = {
        "status": "available",
        "available": True,
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "router": {"source": "epp"},
        "per_pod": [
            {
                "pod": "llm-d-lens-sim-7df77121-d60bc0",
                "role": "model-server",
                "request_rate_rps": {"mean": 0.0},
                "running_requests": {"mean": 0.0},
            },
            {
                "pod": "optimized-baseline-epp-5c569b9f79-rrkvd",
                "role": "model-server",
                "request_rate_rps": {"mean": 1.4},
                "running_requests": {"mean": 2.0},
            },
            {
                "pod": "optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq",
                "role": "decode",
                "request_rate_rps": {"mean": 0.6},
                "running_requests": {"mean": 20.1},
                "kv_cache_usage_percent": {"max": 35.0},
                "prefix_cache_hit_percent": {"mean": 16.5},
            },
        ],
        "derived": {
            "request_destination_distribution": [
                {"pod": "llm-d-lens-sim-7df77121-d60bc0", "request_rate_rps": 1.0, "percent": 50.0},
                {"pod": "optimized-baseline-epp-5c569b9f79-rrkvd", "request_rate_rps": 0.4, "percent": 20.0},
                {
                    "pod": "optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq",
                    "request_rate_rps": 0.6,
                    "percent": 30.0,
                },
            ],
            "role_summary": {"decode": {"running_requests": 20.1}},
        },
        "summary": {
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "kv_cache_usage_percent": {"mean": 20.0, "max": 35.0},
        },
        "series": [
            {
                "timestamp": datetime.fromtimestamp(1700000000.0 + offset, tz=UTC).isoformat().replace("+00:00", "Z"),
                "running_requests": 20.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 25.0,
                "prefix_cache_hit_percent": 15.0,
                "vllm_preemptions_per_second": 0.0,
            }
            for offset in (2.0, 7.0, 12.0, 17.0, 22.0, 27.0)
        ],
        "role_series": [
            {
                "timestamp": datetime.fromtimestamp(1700000000.0 + offset, tz=UTC).isoformat().replace("+00:00", "Z"),
                "decode_running_requests": 20.0,
                "decode_waiting_requests": 0.0,
                "decode_kv_cache_usage_percent": 25.0,
            }
            for offset in (2.0, 7.0, 12.0, 17.0, 22.0, 27.0)
        ],
    }

    cleaned = telemetry_post.sanitize_server_telemetry(raw_server)
    assert cleaned is not None
    assert cleaned["topology_mode"] == "aggregated"
    pod_roles = {p["pod"]: p["role"] for p in cleaned["per_pod"]}
    assert "llm-d-lens-sim-7df77121-d60bc0" not in pod_roles
    assert pod_roles["optimized-baseline-epp-5c569b9f79-rrkvd"] == "epp"
    assert pod_roles["optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq"] == "worker"
    dist = cleaned["derived"]["request_destination_distribution"]
    assert [d["pod"] for d in dist] == ["optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq"]
    assert dist[0]["percent"] == 100.0

    client_series = {
        "schema_version": 2,
        "warmup_seconds": 1.0,
        "duration_seconds": 32.0,
        "origin_epoch": 1700000000.0,
        "buckets": [],
        "windows": {
            "profiling": {
                "request_count": 20,
                "successful_requests": 20,
                "error_rate_pct": 0.0,
                "slo_attainment_pct": 100.0,
            }
        },
    }
    forensics = analyze_forensics(task, client_series=client_series, server_telemetry=raw_server)
    skew_pods = [p["pod"] for p in forensics["pod_skew"]["pods"]]
    assert skew_pods == ["optimized-baseline-xpu-vllm-decode-545f64b875-ldpkq"]
    assert forensics["pod_skew"]["topology_mode"] == "aggregated"

    timeline = forensics["timeline_buckets"]
    roles_in_timeline = {cell["role"] for cell in timeline}
    assert "prefill" not in roles_in_timeline
    assert "worker" in roles_in_timeline
    worker_cells = [c for c in timeline if c["role"] == "worker"]
    assert len(worker_cells) == 6
    assert all((c["end_seconds"] - c["start_seconds"]) >= 5.0 for c in worker_cells)
    assert all(c["running"] == 20.0 for c in worker_cells)


def test_sanitize_server_telemetry_clamps_percentage_and_ratio_jitter() -> None:
    raw = {
        "schema_version": 3,
        "status": "available",
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "gpu_utilization_percent": 100.00025421224746,
                "gpu_memory_bandwidth_utilization_percent": 100.00025,
                "gpu_memory_utilization_ratio": 1.00025,
                "kv_cache_usage_percent": -0.01,
            }
        ],
        "summary": {
            "gpu_utilization_percent": {"mean": 100.00025, "max": 100.00025421224746},
            "gpu_memory_utilization_ratio": {"mean": 1.0002, "max": 1.00025},
        },
        "per_pod": [
            {
                "pod": "decode-0",
                "role": "decode",
                "gpu_utilization_percent": {"mean": 100.00025, "max": 100.00025421224746},
            }
        ],
        "role_series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "decode_gpu_utilization_percent": 100.00025421224746,
            }
        ],
        "derived": {
            "role_summary": {
                "decode": {"gpu_utilization_percent": 100.00025},
            }
        },
        "system": {"cpu_avg_pct": 100.00025, "cpu_peak_pct": 100.00025},
    }
    cleaned = telemetry_post.sanitize_server_telemetry(raw)
    assert cleaned is not None
    assert cleaned["series"][0]["gpu_utilization_percent"] == 100.0
    assert cleaned["series"][0]["gpu_memory_bandwidth_utilization_percent"] == 100.0
    assert cleaned["series"][0]["gpu_memory_utilization_ratio"] == 1.0
    assert cleaned["series"][0]["kv_cache_usage_percent"] == 0.0
    assert cleaned["summary"]["gpu_utilization_percent"] == {"mean": 100.0, "max": 100.0}
    assert cleaned["summary"]["gpu_memory_utilization_ratio"] == {"mean": 1.0, "max": 1.0}
    assert cleaned["per_pod"][0]["gpu_utilization_percent"] == {"mean": 100.0, "max": 100.0}
    assert cleaned["role_series"][0]["decode_gpu_utilization_percent"] == 100.0
    assert cleaned["derived"]["role_summary"]["decode"]["gpu_utilization_percent"] == 100.0
    assert cleaned["system"] == {"cpu_avg_pct": 100.0, "cpu_peak_pct": 100.0}


def test_build_client_series_honors_backend_warmup_window_span_and_aiperf_drift(tmp_path: Path) -> None:
    """AIPerf credit_issued_ns drift, drift_histogram, arrived_rps, warmup_seconds=None, and span rate work."""
    from llm_d_lens.simulation.backends import get_backend

    task_dir = tmp_path / "sim-aiperf"
    backend_dir = task_dir / "artifacts" / "aiperf"
    backend_dir.mkdir(parents=True, exist_ok=True)
    task = _make_task("bc8de403")
    task = task.model_copy(
        update={
            "task_dir": str(task_dir),
            "simulation": task.simulation.model_copy(
                update={
                    "backend": "aiperf",
                    "duration_seconds": 32,
                    "backend_options": {"warmup_seconds": 1},
                }
            ),
        }
    )

    origin_ns = 1_700_000_000_000_000_000
    rows = [
        # Warmup arrival at 0.2s completing at 1.5s, 2ms schedule drift
        {
            "metadata": {
                "request_id": "req-0",
                "benchmark_phase": "profiling",
                "credit_issued_ns": origin_ns + 200_000_000,
                "request_start_ns": origin_ns + 202_000_000,
                "request_end_ns": origin_ns + 1_500_000_000,
            },
            "metrics": {
                "time_to_first_token": {"value": 150.0, "unit": "ms"},
                "inter_token_latency": {"value": 15.0, "unit": "ms"},
                "request_latency": {"value": 1298.0, "unit": "ms"},
                "input_sequence_length": {"value": 128},
                "output_sequence_length": {"value": 64},
            },
        },
        # Burst arrivals at 2.0s..2.2s taking ~24s to complete; drifts 20ms and 80ms
        {
            "metadata": {
                "request_id": "req-1",
                "benchmark_phase": "profiling",
                "credit_issued_ns": origin_ns + 2_000_000_000,
                "request_start_ns": origin_ns + 2_020_000_000,
                "request_end_ns": origin_ns + 26_020_000_000,
            },
            "metrics": {
                "time_to_first_token": {"value": 250.0, "unit": "ms"},
                "inter_token_latency": {"value": 20.0, "unit": "ms"},
                "request_latency": {"value": 24000.0, "unit": "ms"},
                "input_sequence_length": {"value": 256},
                "output_sequence_length": {"value": 128},
            },
        },
        {
            "metadata": {
                "request_id": "req-2",
                "benchmark_phase": "profiling",
                "credit_issued_ns": origin_ns + 2_120_000_000,
                "request_start_ns": origin_ns + 2_200_000_000,
                "request_end_ns": origin_ns + 26_200_000_000,
            },
            "metrics": {
                "time_to_first_token": {"value": 300.0, "unit": "ms"},
                "inter_token_latency": {"value": 22.0, "unit": "ms"},
                "request_latency": {"value": 24000.0, "unit": "ms"},
                "input_sequence_length": {"value": 256},
                "output_sequence_length": {"value": 128},
            },
        },
    ]
    (backend_dir / "profile_export.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    records = get_backend("aiperf").artifact_records(task, tolerate_incomplete=True)
    assert [round(r.s_time_drift_ms or 0.0, 2) for r in records] == [2.0, 20.0, 80.0]

    cs = telemetry_post.build_client_series(task, records)
    assert cs["schema_version"] == 15
    assert cs["warmup_seconds"] == 1.0
    prof = cs["windows"]["profiling"]
    assert prof["request_count"] == 2
    # Active span runs from arrival (2.0s) through completion (26.2s) -> 24.2s, never inflated to ~10 req/s
    assert prof["throughput_rps"] < 0.2
    assert all("arrived_rps" in b for b in cs["buckets"])
    assert cs["buckets"][0]["arrived_rps"] > 0.0
    drift_hist = {item["label"]: item["count"] for item in cs["drift_histogram"]}
    assert drift_hist["<10ms"] == 1
    assert drift_hist["10-50ms"] == 1
    assert drift_hist["50-250ms"] == 1
    assert sum(drift_hist.values()) == 3


def test_build_client_series_dispatch_duration_and_fidelity_series():
    task = _make_task("d150a7c4", duration_seconds=20)
    origin = 1_750_000_000.0
    # 20 arrivals over 0..19.674s, draining until 49.881s (30s grace period)
    schedule = [round(i * (19.674 / 19.0), 3) for i in range(20)]
    records = [
        ArtifactRecord(
            request_id=f"r-{i}",
            arrived=origin + sched_t + 0.015,
            completed=origin + (49.881 if i == 19 else sched_t + 5.0),
            successful=True,
            input_tokens=100,
            output_tokens=20,
            ttft_ms=100.0,
            tpot_ms=15.0,
            latency_ms=5000.0,
            s_time_drift_ms=15.0 + i,
        )
        for i, sched_t in enumerate(schedule)
    ]

    cs = telemetry_post.build_client_series(task, records, warmup_seconds=2.0, arrival_schedule=schedule)
    assert cs["duration_seconds"] == pytest.approx(49.866, abs=0.001)
    assert cs["dispatch_duration_seconds"] == pytest.approx(19.674, abs=0.001)
    fid = cs["fidelity_series"]
    assert 1 <= len(fid) <= 40
    assert len(fid) == 20
    assert fid[0]["start_seconds"] == 0.0
    assert fid[0]["end_seconds"] == 1.0
    assert fid[0]["is_warmup"] is True
    assert fid[2]["is_warmup"] is False
    assert fid[-1]["end_seconds"] == cs["dispatch_duration_seconds"]
    assert all(b["target_arrival_rps"] is not None for b in fid)
    assert sum(b["arrived_requests"] for b in fid) == 20
    assert fid[0]["drift_ms"]["mean"] is not None
    assert fid[0]["drift_ms"]["p50"] is not None
    assert fid[0]["drift_ms"]["p90"] is not None
    assert fid[0]["drift_ms"]["p99"] is not None

    # Without arrival_schedule, target_arrival_rps is None
    cs_no_sched = telemetry_post.build_client_series(task, records, arrival_schedule=None)
    assert all(b["target_arrival_rps"] is None for b in cs_no_sched["fidelity_series"])


def test_enrich_server_telemetry_stamps_v3_system_peak_and_offload_totals():
    raw_srv = {
        "status": "available",
        "step_seconds": 5,
        "nodes": ["node-a"],
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "host_power_watts": 200.0,
                "kv_offload_bytes_per_second": 1000.0,
                "kv_restore_bytes_per_second": 500.0,
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "host_power_watts": 300.0,
                "kv_offload_bytes_per_second": 3000.0,
                "kv_restore_bytes_per_second": 500.0,
            },
        ],
        "summary": {
            "host_cpu_usage_percent": {"mean": 45.0, "max": 82.5},
            "host_memory_total_bytes": {"mean": 64e9},
            "host_memory_available_bytes": {"mean": 32e9},
            "host_filesystem_avail_bytes": {"mean": 100e9},
            "host_disk_read_bytes_per_second": {"mean": 1e6},
            "host_disk_written_bytes_per_second": {"mean": 2e6},
            "host_power_watts": {"mean": 250.0, "max": 300.0},
            "kv_offload_bytes_per_second": {"mean": 2000.0},
            "kv_restore_bytes_per_second": {"mean": 500.0},
        },
    }
    enriched = telemetry_post._enrich_server_telemetry(
        raw_srv, {"duration_seconds": 60.0, "windows": {"full": {"total_output_tokens": 1000}}}
    )
    assert enriched["schema_version"] == telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11
    assert enriched["system"]["available"] is True
    assert enriched["system"]["cpu_avg_pct"] == 45.0
    assert enriched["system"]["cpu_peak_pct"] == 82.5
    assert enriched["system"]["host_power_watts"] == 250.0
    assert enriched["system"]["host_peak_power_watts"] == 300.0
    assert enriched["energy"]["host_avg_power_watts"] == 250.0
    assert enriched["energy"]["host_peak_power_watts"] == 300.0
    assert enriched["energy"]["host_energy_wh"] == pytest.approx((250.0 * 5.0) / 3600.0, rel=1e-5)
    assert enriched["derived"]["offloaded_bytes"] == 10000
    assert enriched["derived"]["restored_bytes"] == 2500


@pytest.mark.asyncio
async def test_service_get_task_telemetry_refreshes_stale_v2_server_telemetry_and_grace_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from pydantic import ValidationError

    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult, SimulationTaskCreateRequest, SimulationTaskRerunRequest

    # 1. Request model validation & create_task wiring for grace_period_seconds
    with pytest.raises(ValidationError):
        SimulationTaskCreateRequest(
            scenario="chat",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="ds",
            trace_path="t.jsonl",
            grace_period_seconds=-1.0,
        )
    with pytest.raises(ValidationError):
        SimulationTaskCreateRequest(
            scenario="chat",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="ds",
            trace_path="t.jsonl",
            grace_period_seconds=3601.0,
        )
    rerun_req = SimulationTaskRerunRequest(grace_period_seconds=45.0)
    assert rerun_req.grace_period_seconds == 45.0

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        # 2. Stale v2 server-telemetry.json triggers capture_server_telemetry over exact saved window
        task_id = "c42812b3"
        task = _make_task(
            task_id,
            duration_seconds=20,
            endpoint_mode="in-cluster",
            endpoint_cluster_id="c-1",
            endpoint_namespace="ns-1",
        )
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 2, "successful_requests": 2, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        backend_dir = task_dir / "artifacts" / "aiperf"
        report_dir = task_dir / "artifacts" / "report"
        backend_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        origin_ns = 1_795_597_273_000_000_000
        req_lines = [
            {
                "metadata": {
                    "request_id": f"r-{i}",
                    "benchmark_phase": "profiling",
                    "request_start_ns": origin_ns + (100 + i * 1000) * 1_000_000,
                    "request_end_ns": origin_ns + (2100 + i * 1000) * 1_000_000,
                    "credit_issued_ns": origin_ns + (97 + i * 1000) * 1_000_000,
                    "status_code": 200,
                },
                "metrics": {
                    "input_sequence_length": {"value": 64},
                    "output_sequence_length": {"value": 16},
                    "time_to_first_token": {"value": 120.0},
                    "inter_token_latency": {"value": 15.0},
                    "request_latency": {"value": 2000.0},
                },
            }
            for i in range(2)
        ]
        (backend_dir / "profile_export.jsonl").write_text(
            "\n".join(json.dumps(line) for line in req_lines) + "\n",
            encoding="utf-8",
        )

        saved_v2_srv = {
            "schema_version": 2,
            "status": "available",
            "step_seconds": 5,
            "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:20Z"},
            "nodes": ["node-1"],
            "series": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "host_cpu_usage_percent": 30.0,
                    "kv_offload_bytes_per_second": 400.0,
                },
                {
                    "timestamp": "2026-01-01T00:00:05Z",
                    "host_cpu_usage_percent": 50.0,
                    "kv_offload_bytes_per_second": 600.0,
                },
            ],
            "summary": {
                "host_cpu_usage_percent": {"mean": 40.0, "max": 50.0},
                "kv_offload_bytes_per_second": {"mean": 500.0},
            },
        }
        (report_dir / "server-telemetry.json").write_text(json.dumps(saved_v2_srv), encoding="utf-8")
        (report_dir / "client-series.json").write_text(
            json.dumps({"schema_version": 2, "buckets": [], "windows": {"full": {"request_count": 2}}}),
            encoding="utf-8",
        )
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 2, "verdict": {"code": "healthy"}, "telemetry_scope": "full"}),
            encoding="utf-8",
        )
        (report_dir / "index.html").write_text("<html>stale</html>", encoding="utf-8")
        await service.save_task(task)

        # Case A: Prometheus returns empty series -> preserves saved non-empty series and re-enriches to v4
        captured_calls: list[tuple[str, str, str | None, str]] = []

        async def empty_prom(cluster_id, namespace, start_iso, end_iso):
            captured_calls.append((start_iso, end_iso, cluster_id, namespace))
            return {
                "status": "empty",
                "window": {"start": start_iso, "end": end_iso},
                "series": [],
                "summary": {},
            }

        monkeypatch.setattr(telemetry_post, "collect_namespace_observability", empty_prom)
        tel_a = await service.get_task_telemetry(task_id)
        assert captured_calls == [("2026-01-01T00:00:00Z", "2026-01-01T00:00:20Z", "c-1", "ns-1")]
        assert tel_a["server_telemetry"]["schema_version"] == 11
        assert len(tel_a["server_telemetry"]["series"]) == 2
        assert tel_a["server_telemetry"]["system"]["cpu_peak_pct"] == 50.0
        assert tel_a["server_telemetry"]["derived"]["offloaded_bytes"] == 2500
        assert tel_a["client_series"]["schema_version"] == 15
        assert tel_a["forensics"]["schema_version"] == 8

        # Case B: Re-seed v2 and return fresh Prometheus series -> updates persisted server-telemetry.json
        (report_dir / "server-telemetry.json").write_text(json.dumps(saved_v2_srv), encoding="utf-8")

        async def fresh_prom(cluster_id, namespace, start_iso, end_iso):
            return {
                "status": "available",
                "step_seconds": 5,
                "window": {"start": start_iso, "end": end_iso},
                "nodes": ["node-1"],
                "series": [
                    {"timestamp": "2026-01-01T00:00:00Z", "cpu_cache_usage_percent": 15.0},
                    {"timestamp": "2026-01-01T00:00:05Z", "cpu_cache_usage_percent": 25.0},
                ],
                "summary": {"cpu_cache_usage_percent": {"mean": 20.0, "max": 25.0}},
            }

        monkeypatch.setattr(telemetry_post, "collect_namespace_observability", fresh_prom)
        tel_b = await service.get_task_telemetry(task_id)
        assert tel_b["server_telemetry"]["schema_version"] == 11
        assert tel_b["server_telemetry"]["summary"]["cpu_cache_usage_percent"]["mean"] == 20.0
        persisted_srv = json.loads((report_dir / "server-telemetry.json").read_text(encoding="utf-8"))
        assert persisted_srv["schema_version"] == 11
        assert persisted_srv["summary"]["cpu_cache_usage_percent"]["mean"] == 20.0

        # Incomplete tasks must skip Prometheus re-query in refresh_persisted_server_telemetry
        incomplete_calls: list[tuple[str, str]] = []

        async def fail_if_called(_cid, _ns, start_iso, end_iso):
            incomplete_calls.append((start_iso, end_iso))
            return {}

        monkeypatch.setattr(telemetry_post, "collect_namespace_observability", fail_if_called)
        incomplete_task = task.model_copy(update={"artifacts_incomplete": True})
        res_inc = await telemetry_post.refresh_persisted_server_telemetry(
            incomplete_task, saved_v2_srv, tel_b["client_series"]
        )
        assert incomplete_calls == []
        assert res_inc["schema_version"] == 11

        # 3. create_task wires request.grace_period_seconds and rerun_task supports override
        from unittest.mock import AsyncMock

        monkeypatch.setattr(service, "discover_models", AsyncMock(return_value={"models": ["m"]}))
        monkeypatch.setattr(service, "_schedule_task", lambda _t: None)
        monkeypatch.setattr(
            service,
            "list_backends",
            lambda: [
                SimpleNamespace(
                    name="aiperf",
                    scenarios=[SimpleNamespace(name="chat")],
                    capabilities={
                        "trace_formats": ["bailian_trace"],
                        "supports_trace_range": False,
                        "trace_format_capabilities": {"bailian_trace": {"supports_scale_factor": True}},
                    },
                )
            ],
        )
        monkeypatch.setattr(
            service.trace_registry,
            "get",
            lambda _name: {"trace_format": "bailian_trace", "scenario": "chat", "filename": "t.jsonl"},
        )
        monkeypatch.setattr(
            service.trace_registry,
            "get_type",
            lambda _fmt: SimpleNamespace(externally_managed=False),
        )
        monkeypatch.setattr(
            service,
            "get_backend",
            lambda _name: SimpleNamespace(validate=AsyncMock(return_value=None)),
        )
        create_req = SimulationTaskCreateRequest(
            scenario="chat",
            endpoint_url="http://127.0.0.1:8000",
            model_name="m",
            trace_dataset="ds",
            trace_path="t.jsonl",
            grace_period_seconds=12.5,
        )
        created = await service.create_task(create_req)
        assert created.simulation.grace_period_seconds == 12.5
        created.status = "completed"
        await service.save_task(created)
        reran = await service.rerun_task(created.id, override=SimulationTaskRerunRequest(grace_period_seconds=45.0))
        assert reran.simulation.grace_period_seconds == 45.0
    finally:
        service.reset_service_state()


def _base_forensics_client(
    *,
    ttft_p90: float = 200.0,
    tpot_p90: float = 20.0,
    e2e_p90: float = 800.0,
    drift_p90: float = 10.0,
) -> dict:
    return {
        "schema_version": telemetry_post.REPORT_SCHEMA_VERSION,
        "warmup_seconds": 0.0,
        "duration_seconds": 60.0,
        "origin_epoch": 1000.0,
        "buckets": [],
        "windows": {
            "full": {
                "request_count": 100,
                "successful_requests": 100,
                "error_rate_pct": 0.0,
                "slo_attainment_pct": 99.0,
                "goodput_rps": 20.0,
                "throughput_rps": 20.0,
                "ttft_ms": {"p50": ttft_p90 / 2.0, "p90": ttft_p90},
                "tpot_ms": {"p50": tpot_p90 / 2.0, "p90": tpot_p90},
                "e2e_ms": {"p50": e2e_p90 / 2.0, "p90": e2e_p90},
                "drift_ms": {"p50": 5.0, "p90": drift_p90},
            }
        },
    }


def _finding(forensics: dict, code: str) -> dict:
    return next(f for f in forensics["findings"] if f["code"] == code)


def test_forensics_kv_pressure_context_aware_recommendations() -> None:
    task = _make_task("aa000001")
    client = _base_forensics_client()

    # 1. CPU KV offload active via cpu_cache_usage_percent (<85% peak), gpu_mem_util=0.95 (>=0.92), 1 replica
    srv_active_high_util = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "kv_cache_usage_percent": 94.0,
                "queue_depth": 0.0,
                "vllm_preemptions_total": 0.0,
            }
        ],
        "summary": {
            "kv_cache_usage_percent": {"mean": 88.0, "max": 94.0},
            "cpu_cache_usage_percent": {"mean": 30.0, "max": 45.2},
        },
        "availability": {"metrics": {"cpu_cache_usage_percent": "available"}},
        "cache_config": {"engines": ["bad-entry", {"gpu_memory_utilization": 0.95}]},
        "per_pod": [
            {"pod": "llm-d-lens-sim-job-1", "role": "worker"},
            {"pod": "my-epp-0", "role": "epp"},
            {"pod": "vllm-worker-0", "role": "worker"},
        ],
    }
    res1 = analyze_forensics(task, client_series=client, server_telemetry=srv_active_high_util)
    assert res1["schema_version"] == 8
    rec1 = _finding(res1, "kv_pressure")["recommendation"]
    assert "enable cpu kv offload" not in rec1.lower()
    assert "CPU KV offload is active (peak 45.2%)" in rec1
    assert "cpu_offload_gb" not in rec1
    assert "--gpu-memory-utilization" not in rec1
    assert "(currently 1 replica)" in rec1
    assert "max_num_seqs" in rec1

    # 2. CPU KV offload active via derived.offloaded_bytes & peak >= 85%, gpu_mem_util=0.90 (<0.92), 2 replicas
    srv_active_high_cpu = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 92.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 90.0, "max": 92.0},
            "cpu_cache_usage_percent": {"mean": 80.0, "max": 88.5},
        },
        "derived": {"offloaded_bytes": 1024.0},
        "cache_config": {"engines": [{"gpu_memory_utilization": 0.90}]},
        "per_pod": [
            {"pod": "vllm-worker-0", "role": "worker"},
            {"pod": "vllm-worker-1", "role": "worker"},
        ],
    }
    res2 = analyze_forensics(task, client_series=client, server_telemetry=srv_active_high_cpu)
    rec2 = _finding(res2, "kv_pressure")["recommendation"]
    assert "enable cpu kv offload" not in rec2.lower()
    assert "CPU KV offload is active (peak 88.5%)" in rec2
    assert "cpu_offload_gb / --kv-offloading-size" in rec2
    assert "--gpu-memory-utilization (currently 0.90)" in rec2
    assert "currently 1 replica" not in rec2

    # 3. CPU KV offload active via cache_config.cpu_capacity_tokens only (cpu_kv_peak_pct is None) & gpu_mem_util=NaN
    srv_cap_only = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 93.0, "queue_depth": 0.0}],
        "summary": {"kv_cache_usage_percent": {"mean": 90.0, "max": 93.0}},
        "cache_config": {
            "cpu_capacity_tokens": 50000,
            "engines": [{"gpu_memory_utilization": "NaN"}],
        },
        "per_pod": [{"pod": "vllm-worker-0", "role": "worker"}],
    }
    res3 = analyze_forensics(task, client_series=client, server_telemetry=srv_cap_only)
    rec3 = _finding(res3, "kv_pressure")["recommendation"]
    assert "enable cpu kv offload" not in rec3.lower()
    assert "CPU KV offload is active." in rec3
    assert "Raise --gpu-memory-utilization" in rec3
    assert "(currently " not in rec3.split("scale")[0]

    # 4. CPU KV offload inactive, gpu_mem_util=0.85, 1 replica -> recommends enabling CPU KV offload
    srv_inactive = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 95.0, "queue_depth": 0.0}],
        "summary": {"kv_cache_usage_percent": {"mean": 92.0, "max": 95.0}},
        "cache_config": {"engines": [{"gpu_memory_utilization": 0.85}]},
        "per_pod": [{"pod": "vllm-worker-0", "role": "worker"}],
    }
    res4 = analyze_forensics(task, client_series=client, server_telemetry=srv_inactive)
    rec4 = _finding(res4, "kv_pressure")["recommendation"]
    assert "enable CPU KV offload" in rec4
    assert "--gpu-memory-utilization (currently 0.85)" in rec4
    assert "(currently 1 replica)" in rec4

    # 5. Null / NaN / non-dict summary entries must NOT falsely trigger cpu_kv_offload_active
    srv_null_stats = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 95.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 92.0, "max": 95.0},
            "cpu_cache_usage_percent": {"mean": None, "max": float("nan")},
            "kv_offload_bytes_per_second": "unavailable",
        },
        "per_pod": [{"pod": "vllm-worker-0", "role": "worker"}],
    }
    res5 = analyze_forensics(task, client_series=client, server_telemetry=srv_null_stats)
    rec5 = _finding(res5, "kv_pressure")["recommendation"]
    assert "CPU KV offload is active" not in rec5
    assert "enable CPU KV offload" in rec5


def test_forensics_queue_wait_single_vs_multi_replica_and_router_fallback() -> None:
    task = _make_task("aa000002")
    client = _base_forensics_client()
    base_srv = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 40.0, "queue_depth": 5.0}],
        "summary": {"kv_cache_usage_percent": {"mean": 40.0, "max": 40.0}, "queue_depth": {"mean": 5.0, "max": 5.0}},
    }

    # 1. Single serving pod (plus client & EPP pods) -> never mentions EPP load balancing
    srv_one = {
        **base_srv,
        "per_pod": [
            {"pod": "trace-replayer-abc", "role": "worker"},
            {"pod": "gateway-epp-0", "role": "epp"},
            {"pod": "vllm-worker-0", "role": "worker"},
        ],
    }
    rec_one = _finding(analyze_forensics(task, client_series=client, server_telemetry=srv_one), "queue_wait")[
        "recommendation"
    ]
    assert "epp" not in rec_one.lower()
    assert "currently 1" in rec_one

    # 2. Multiple serving pods -> mentions EPP load balancing
    srv_two = {
        **base_srv,
        "per_pod": [
            {"pod": "vllm-worker-0", "role": "worker"},
            {"pod": "vllm-worker-1", "role": "worker"},
        ],
    }
    rec_two = _finding(analyze_forensics(task, client_series=client, server_telemetry=srv_two), "queue_wait")[
        "recommendation"
    ]
    assert "EPP load balancing" in rec_two

    # 3. Empty per_pod with router.ready_endpoints fallback = 1 (mean-only) vs 3
    srv_router_one = {**base_srv, "per_pod": [], "router": {"ready_endpoints": {"mean": 1.0}}}
    rec_r1 = _finding(analyze_forensics(task, client_series=client, server_telemetry=srv_router_one), "queue_wait")[
        "recommendation"
    ]
    assert "epp" not in rec_r1.lower()
    assert "currently 1" in rec_r1

    srv_router_three = {**base_srv, "per_pod": [], "router": {"ready_endpoints": {"max": 3.0}}}
    rec_r3 = _finding(analyze_forensics(task, client_series=client, server_telemetry=srv_router_three), "queue_wait")[
        "recommendation"
    ]
    assert "EPP load balancing" in rec_r3


def test_forensics_high_ttft_tpot_and_schedule_drift_context_awareness() -> None:
    client = _base_forensics_client(ttft_p90=2500.0, tpot_p90=150.0, e2e_p90=4000.0, drift_p90=600.0)
    aiperf_task = _make_task("aa000003", backend="aiperf")
    historical_replayer_task = _make_task("aa000004", backend="trace-replayer")

    # 1. Prefix caching active + aggregated
    srv_pc_agg = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "prefix_cache_hit_percent": {"mean": 55.0, "max": 70.0},
            "gpu_utilization_percent": {"mean": 40.0},
        },
        "per_pod": [{"pod": "vllm-worker-0", "role": "worker"}],
    }
    res_pc_agg = analyze_forensics(aiperf_task, client_series=client, server_telemetry=srv_pc_agg)
    ttft_rec_1 = _finding(res_pc_agg, "high_ttft")["recommendation"]
    assert "enable prefix caching" not in ttft_rec_1.lower()
    assert "Prefix caching is active" in ttft_rec_1
    assert "P/D disaggregation" in ttft_rec_1
    drift_aiperf = _finding(res_pc_agg, "schedule_drift")["recommendation"]
    assert "AIPerf" in drift_aiperf
    assert "num_producer" not in drift_aiperf

    # 2. Prefix caching active (via engine_prompt_local_cache_tps) + disaggregated
    srv_pc_disagg = {
        "status": "available",
        "topology_mode": "disaggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "engine_prompt_local_cache_tps": {"mean": 250.0, "max": 400.0},
            "gpu_utilization_percent": {"mean": 40.0},
        },
        "per_pod": [
            {"pod": "vllm-prefill-0", "role": "prefill"},
            {"pod": "vllm-decode-0", "role": "decode"},
        ],
    }
    res_pc_disagg = analyze_forensics(historical_replayer_task, client_series=client, server_telemetry=srv_pc_disagg)
    ttft_rec_2 = _finding(res_pc_disagg, "high_ttft")["recommendation"]
    assert "enable prefix caching" not in ttft_rec_2.lower()
    assert "Prefix caching and P/D disaggregation are active" in ttft_rec_2
    assert "evaluate P/D disaggregation" not in ttft_rec_2
    tpot_rec_disagg = _finding(res_pc_disagg, "high_tpot")["recommendation"]
    assert "decode replicas" in tpot_rec_disagg
    drift_historical = _finding(res_pc_disagg, "schedule_drift")["recommendation"]
    assert "AIPerf" in drift_historical
    assert "num_producer" not in drift_historical

    # 3. Prefix caching inactive + disaggregated vs aggregated
    srv_nopc_disagg = {
        "status": "available",
        "topology_mode": "disaggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {"kv_cache_usage_percent": {"mean": 30.0, "max": 30.0}},
        "per_pod": [
            {"pod": "vllm-prefill-0", "role": "prefill"},
            {"pod": "vllm-decode-0", "role": "decode"},
        ],
    }
    ttft_rec_3 = _finding(
        analyze_forensics(aiperf_task, client_series=client, server_telemetry=srv_nopc_disagg), "high_ttft"
    )["recommendation"]
    assert "Enable prefix caching" in ttft_rec_3
    assert "scale prefill replicas" in ttft_rec_3
    assert "evaluate P/D disaggregation" not in ttft_rec_3

    srv_nopc_agg = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {"kv_cache_usage_percent": {"mean": 30.0, "max": 30.0}},
        "per_pod": [{"pod": "vllm-worker-0", "role": "worker"}],
    }
    ttft_rec_4 = _finding(
        analyze_forensics(aiperf_task, client_series=client, server_telemetry=srv_nopc_agg), "high_ttft"
    )["recommendation"]
    assert "Enable prefix caching" in ttft_rec_4
    assert "evaluate P/D disaggregation" in ttft_rec_4


@pytest.mark.asyncio
async def test_service_get_task_telemetry_refreshes_stale_v3_forensics_when_records_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        task_id = "f0e1d2c3"
        task = _make_task(task_id, duration_seconds=30)
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 0, "successful_requests": 0, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        report_dir = task_dir / "artifacts" / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "client-series.json").write_text(
            json.dumps(
                {
                    "schema_version": telemetry_post.CLIENT_SERIES_SCHEMA_VERSION,
                    "warmup_seconds": 0.0,
                    "duration_seconds": 30.0,
                    "buckets": [],
                    "windows": {"full": {"request_count": 0, "successful_requests": 0, "error_rate_pct": 0.0}},
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "server-telemetry.json").write_text(
            json.dumps(
                {
                    "schema_version": telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION,
                    "status": "unavailable",
                    "series": [],
                    "summary": {},
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 3, "verdict": {"code": "healthy"}}),
            encoding="utf-8",
        )
        await service.save_task(task)

        tel = await service.get_task_telemetry(task_id)
        assert tel["forensics"]["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION == 8
        persisted = json.loads((report_dir / "forensics.json").read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 8
    finally:
        service.reset_service_state()


@pytest.mark.asyncio
async def test_server_telemetry_v5_preserves_kv_block_metrics_and_integrates_evictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11

    raw_srv = {
        "schema_version": 4,
        "status": "available",
        "step_seconds": 5,
        "cache_config": {
            "source": "vllm:cache_config_info",
            "engines": [],
            "kv_cache_metrics_enabled": True,
            "kv_cache_metrics_sample": 0.05,
        },
        "series": [
            {"timestamp": "2026-01-01T00:00:00Z", "kv_blocks_evicted_per_minute": 60.0},
            {"timestamp": "2026-01-01T00:00:05Z", "kv_blocks_evicted_per_minute": 180.0},
            {"timestamp": "2026-01-01T00:00:10Z", "kv_blocks_evicted_per_minute": 120.0},
        ],
        "summary": {
            "kv_blocks_evicted_per_minute": {"mean": 120.0, "max": 180.0},
            "kv_block_lifetime_seconds": {"p50": 10.0, "p90": 30.0, "p99": 60.0},
            "kv_block_idle_before_evict_seconds": {"p50": 2.0, "p90": 6.0, "p99": 12.0},
            "kv_block_reuse_gap_seconds": {"p50": 1.0, "p90": 4.0, "p99": 9.0},
        },
    }

    cleaned = telemetry_post.sanitize_server_telemetry(raw_srv)
    assert cleaned is not None
    assert cleaned["cache_config"]["kv_cache_metrics_enabled"] is True
    assert cleaned["cache_config"]["kv_cache_metrics_sample"] == 0.05
    assert cleaned["summary"]["kv_block_lifetime_seconds"] == {"p50": 10.0, "p90": 30.0, "p99": 60.0}

    enriched = telemetry_post._enrich_server_telemetry(cleaned, {"duration_seconds": 10.0})
    assert enriched["schema_version"] == 11
    # 0..5s: 0.5*(1+3)*5 = 10; 5..10s: 0.5*(3+2)*5 = 12.5 => 22.5 -> round(22.5) == 22
    assert enriched["derived"]["kv_blocks_evicted_total"] == 22
    assert enriched["summary"]["kv_blocks_evicted_per_minute"]["min"] == 60.0
    assert enriched["summary"]["kv_blocks_evicted_per_minute"]["latest"] == 120.0

    # Fallback to mean_rate * duration_s / 60 when series is empty
    summary_only = telemetry_post._enrich_server_telemetry(
        {
            "status": "available",
            "series": [],
            "summary": {"kv_blocks_evicted_per_minute": {"mean": 120.0, "min": 60.0, "latest": 90.0}},
        },
        {"duration_seconds": 30.0},
    )
    assert summary_only["derived"]["kv_blocks_evicted_total"] == 60
    assert summary_only["summary"]["kv_blocks_evicted_per_minute"]["min"] == 60.0
    assert summary_only["summary"]["kv_blocks_evicted_per_minute"]["latest"] == 90.0

    # capture_server_telemetry preserves cache_config when Prometheus has aged out
    task = _make_task("aa112233", endpoint_mode="in-cluster", endpoint_cluster_id="c-1", endpoint_namespace="ns-1")
    monkeypatch.setattr(
        telemetry_post,
        "collect_namespace_observability",
        AsyncMock(return_value={"status": "empty", "series": [], "summary": {}}),
    )
    fallback_res = await telemetry_post.capture_server_telemetry(
        task,
        start_iso="2026-01-01T00:00:00Z",
        end_iso="2026-01-01T00:00:10Z",
        client_series={"duration_seconds": 10.0},
        existing_server_telemetry=enriched,
    )
    assert fallback_res["schema_version"] == 11
    assert fallback_res["cache_config"]["kv_cache_metrics_enabled"] is True
    assert fallback_res["cache_config"]["kv_cache_metrics_sample"] == 0.05
    assert fallback_res["derived"]["kv_blocks_evicted_total"] == 22


def test_token_flow_schema_v4_v6_sanitization_redaction_and_session_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert telemetry_post.REPORT_SCHEMA_VERSION == 15
    assert telemetry_post.CLIENT_SERIES_SCHEMA_VERSION == 15
    assert telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11

    # 1. redact_manifest_payload must not redact token_flow leaves
    redacted = telemetry_post.redact_manifest_payload(
        {
            "token_flow": {
                "sent_tokens": 1000,
                "cached_tokens": 400,
                "api_token": "secret-123",
            }
        }
    )
    assert redacted["token_flow"]["sent_tokens"] == 1000
    assert redacted["token_flow"]["cached_tokens"] == 400
    assert redacted["token_flow"]["api_token"] == "<redacted>"  # noqa: S105

    # 2. sanitize_server_telemetry normalizes phantom disaggregated prompt_tokens_by_role_source
    cleaned = telemetry_post.sanitize_server_telemetry(
        {
            "topology_mode": "disaggregated",
            "per_pod": [{"pod": "vllm-worker-0", "role": "decode"}],
            "derived": {
                "prompt_tokens_by_role_source": [
                    {
                        "role": "Decode",
                        "local_cache_tokens": 1000,
                        "external_kv_tokens": 200,
                        "local_compute_tokens": 800,
                        "total_tokens": 2000,
                    },
                    {
                        "role": "model-server",
                        "local_cache_tokens": 500,
                        "external_kv_tokens": 0,
                        "local_compute_tokens": 500,
                        "total_tokens": 1000,
                    },
                    {
                        "role": "epp",
                        "local_cache_tokens": 9999,
                        "external_kv_tokens": 0,
                        "local_compute_tokens": 0,
                        "total_tokens": 9999,
                    },
                    {
                        "role": "llm-d-lens-sim-0",
                        "local_cache_tokens": 0,
                        "external_kv_tokens": 0,
                        "local_compute_tokens": 7777,
                        "total_tokens": 7777,
                    },
                ]
            },
        }
    )
    assert cleaned is not None
    assert cleaned["topology_mode"] == "aggregated"
    assert cleaned["derived"]["prompt_tokens_by_role_source"] == [
        {
            "role": "Worker",
            "local_cache_tokens": 1500,
            "external_kv_tokens": 200,
            "local_compute_tokens": 1300,
            "total_tokens": 3000,
        }
    ]

    # 3. correlate_trace_sessions computes reusable_prefix_tokens for hash_ids without session_ids, and for multi-turn chat_id without hash_ids
    from llm_d_lens.simulation.traces import BaseTrace

    monkeypatch.setattr(BaseTrace, "trace_root", classmethod(lambda _cls: tmp_path))
    hash_trace = tmp_path / "mooncake.jsonl"
    hash_trace.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": 0, "input_length": 1000, "output_length": 50, "hash_ids": [1, 2, 3, 4]}),
                json.dumps({"timestamp": 1000, "input_length": 1000, "output_length": 50, "hash_ids": [1, 2, 5, 6]}),
            ]
        ),
        encoding="utf-8",
    )
    t_hash = _make_task("a1a2b3c4", trace_path=hash_trace.name, trace_format="mooncake_trace")
    ss_hash = telemetry_post.correlate_trace_sessions(t_hash, [])
    assert ss_hash["available"] is False
    assert ss_hash["reusable_prefix_tokens"] == 500
    assert ss_hash["theoretical_prefix_overlap_pct"] == 25.0

    chat_trace = tmp_path / "bailian.jsonl"
    chat_trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 1.0,
                        "input_length": 200,
                        "output_length": 50,
                        "chat_id": "c1",
                        "parent_chat_id": None,
                        "turn": 1,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 2.0,
                        "input_length": 350,
                        "output_length": 50,
                        "chat_id": "c1",
                        "parent_chat_id": None,
                        "turn": 2,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 3.0,
                        "input_length": 300,
                        "output_length": 40,
                        "chat_id": "c1",
                        "parent_chat_id": None,
                        "turn": 3,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    t_chat = _make_task("c1a2b3c4", trace_path=chat_trace.name, trace_format="bailian_trace")
    ss_chat = telemetry_post.correlate_trace_sessions(t_chat, [])
    assert ss_chat["available"] is True
    # Turn 1: prior=0 -> 0; Turn 2: min(350, 250)=250; Turn 3: min(300, 400)=300 => 550
    assert ss_chat["reusable_prefix_tokens"] == 550

    # 4. build_client_series computes lightweight token_flow (with optional duck-typed session_id/cached_tokens)
    duck_records = [
        SimpleNamespace(
            arrived=1.0,
            completed=2.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r1",
            input_tokens=200.0,
            output_tokens=50.0,
            requested_output_tokens=50.0,
            latency_ms=1000.0,
            ttft_ms=100.0,
            tpot_ms=15.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=50.0,
            failed_issue=False,
            session_id="s1",
            turn=1,
            cached_tokens=0,
        ),
        SimpleNamespace(
            arrived=3.0,
            completed=4.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r2",
            input_tokens=350.0,
            output_tokens=50.0,
            requested_output_tokens=50.0,
            latency_ms=1000.0,
            ttft_ms=80.0,
            tpot_ms=15.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=50.0,
            failed_issue=False,
            session_id="s1",
            turn=2,
            cached_tokens=180,
        ),
    ]
    cs_duck = telemetry_post.build_client_series(t_chat, duck_records)
    assert cs_duck["token_flow"] == {
        "sent_tokens": 550,
        "client_cached_tokens": 180,
        "raw_session_reusable_tokens": 250,
    }


def test_enrich_server_telemetry_token_flow_invariants_and_fallbacks() -> None:
    # 1. Integrated engine prompt rates + sessions_summary + fallback prompt_tokens_per_request + disaggregated role split
    srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "engine_prompt_local_cache_tps": 400.0,
                "engine_prompt_external_tps": 100.0,
                "engine_prompt_local_compute_tps": 500.0,
                "engine_prompt_recomputed_tps": 50.0,
            },
            {
                "timestamp": "2026-01-01T00:00:10Z",
                "engine_prompt_local_cache_tps": 400.0,
                "engine_prompt_external_tps": 100.0,
                "engine_prompt_local_compute_tps": 500.0,
                "engine_prompt_recomputed_tps": 50.0,
            },
        ],
        "summary": {
            "engine_prompt_local_cache_tps": {"mean": 400.0},
            "engine_prompt_external_tps": {"mean": 100.0},
            "engine_prompt_local_compute_tps": {"mean": 500.0},
            "engine_prompt_recomputed_tps": {"mean": 50.0},
        },
        "derived": {
            "role_summary": {
                "prefill": {"input_token_rate_tps": 750.0},
                "decode": {"input_token_rate_tps": 250.0},
            }
        },
    }
    client = {
        "duration_seconds": 10.0,
        "windows": {
            "full": {
                "total_input_tokens": 10000,
                "total_output_tokens": 1000,
                "input_tokens": {"avg": 1000.0, "p50": 900.0, "p90": 1800.0},
            }
        },
    }
    enriched = telemetry_post._enrich_server_telemetry(
        srv,
        client,
        sessions_summary={"reusable_prefix_tokens": 7000},
    )
    tf = enriched["derived"]["token_flow"]
    assert tf["sent_tokens"] == 10000
    assert tf["cached_tokens"] == 5000
    assert tf["theoretical_cached_tokens"] == 7000
    assert tf["theoretical_ceiling_pct"] == 70.0
    assert tf["recomputed_tokens"] == 2000
    assert tf["cold_new_tokens"] == 3000
    assert tf["cached_tokens"] + tf["recomputed_tokens"] + tf["cold_new_tokens"] == tf["sent_tokens"]
    assert tf["theoretical_cached_tokens"] >= tf["cached_tokens"]

    ppr = enriched["derived"]["prompt_tokens_per_request"]
    assert ppr == {
        "mean_prompt_tokens": 1000.0,
        "mean_cached_tokens": 500.0,
        "mean_computed_tokens": 500.0,
        "cached_pct": 50.0,
        "prompt_p50": 900.0,
        "prompt_p90": 1800.0,
        "computed_p50": None,
        "computed_p90": None,
        "source": "client_fallback",
    }

    role_rows = enriched["derived"]["prompt_tokens_by_role_source"]
    assert role_rows == [
        {
            "role": "Prefill",
            "local_cache_tokens": 3000,
            "external_kv_tokens": 750,
            "local_compute_tokens": 3750,
            "total_tokens": 7500,
        },
        {
            "role": "Decode",
            "local_cache_tokens": 1000,
            "external_kv_tokens": 250,
            "local_compute_tokens": 1250,
            "total_tokens": 2500,
        },
    ]

    # 2. Preserves vllm_histogram and backfills missing prompt_p50/prompt_p90; theoretical=None when raw_reusable==0 and no higher avoided_prefill_ratio
    srv_hist = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [],
        "summary": {"prefix_cache_hit_percent": {"mean": 30.0}},
        "derived": {
            "avoided_prefill_ratio": 25.0,
            "prompt_tokens_per_request": {
                "mean_prompt_tokens": 1200.0,
                "mean_cached_tokens": 360.0,
                "mean_computed_tokens": 840.0,
                "cached_pct": 30.0,
                "prompt_p50": None,
                "prompt_p90": None,
                "computed_p50": 600.0,
                "computed_p90": 1400.0,
                "source": "vllm_histogram",
            },
        },
    }
    enriched_hist = telemetry_post._enrich_server_telemetry(
        srv_hist, client, sessions_summary={"reusable_prefix_tokens": 0}
    )
    tf_hist = enriched_hist["derived"]["token_flow"]
    assert tf_hist["cached_tokens"] == 3000
    assert tf_hist["theoretical_cached_tokens"] is None
    assert tf_hist["theoretical_ceiling_pct"] is None
    assert tf_hist["recomputed_tokens"] == 0
    assert tf_hist["cold_new_tokens"] == 7000
    assert enriched_hist["derived"]["prompt_tokens_per_request"]["source"] == "vllm_histogram"
    assert enriched_hist["derived"]["prompt_tokens_per_request"]["prompt_p50"] == 900.0
    assert enriched_hist["derived"]["prompt_tokens_per_request"]["prompt_p90"] == 1800.0


def test_build_client_series_per_bucket_reusable_prefix_and_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.simulation.traces import BaseTrace

    # 1. Redaction preserves per-bucket reusable prefix & token_flow fields (including client_cached_tokens / raw_session_reusable_tokens) while redacting auth tokens
    redacted = telemetry_post.redact_manifest_payload(
        {
            "buckets": [
                {
                    "input_tokens_total": 1000,
                    "reusable_prefix_tokens": 400,
                    "reusable_prefix_pct": 40.0,
                }
            ],
            "token_flow": {
                "sent_tokens": 1000,
                "client_cached_tokens": 300,
                "raw_session_reusable_tokens": 400,
            },
            "secrets": {
                "client_token": "sec-1",
                "session_token": "sec-2",
                "api_token": "sec-3",
                "access_token": "sec-4",
                "refresh_token": "sec-5",
                "bearer_token": "sec-6",
                "hf_token": "sec-7",
            },
        }
    )
    assert redacted["buckets"][0] == {
        "input_tokens_total": 1000,
        "reusable_prefix_tokens": 400,
        "reusable_prefix_pct": 40.0,
    }
    assert redacted["token_flow"] == {
        "sent_tokens": 1000,
        "client_cached_tokens": 300,
        "raw_session_reusable_tokens": 400,
    }
    for auth_key in (
        "client_token",
        "session_token",
        "api_token",
        "access_token",
        "refresh_token",
        "bearer_token",
        "hf_token",
    ):
        assert redacted["secrets"][auth_key] == "<redacted>"

    # 1b. sanitize_server_telemetry preserves disaggregated topology when prompt_tokens_by_role_source has Prefill > 0
    disagg_clean = telemetry_post.sanitize_server_telemetry(
        {
            "topology_mode": "disaggregated",
            "derived": {
                "prompt_tokens_by_role_source": [
                    {
                        "role": "Prefill",
                        "local_cache_tokens": 1000,
                        "external_kv_tokens": 0,
                        "local_compute_tokens": 2000,
                        "total_tokens": 3000,
                    },
                    {
                        "role": "Decode",
                        "local_cache_tokens": 500,
                        "external_kv_tokens": 200,
                        "local_compute_tokens": 300,
                        "total_tokens": 1000,
                    },
                ]
            },
        }
    )
    assert disagg_clean is not None
    assert disagg_clean["topology_mode"] == "disaggregated"
    assert [r["role"] for r in disagg_clean["derived"]["prompt_tokens_by_role_source"]] == ["Prefill", "Decode"]

    # 2. Duck-typed session records: Turn-1 bucket = 0%, Turn-2 bucket > 0%, empty bucket = None
    task_duck = _make_task("d1a2b3c4", duration_seconds=15, trace_path="missing.jsonl")
    duck_records = [
        SimpleNamespace(
            arrived=1.0,
            completed=2.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r1",
            input_tokens=400.0,
            output_tokens=100.0,
            requested_output_tokens=100.0,
            latency_ms=1000.0,
            ttft_ms=120.0,
            tpot_ms=15.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=100.0,
            failed_issue=False,
            chat_id="chat-1",
            turn=1,
        ),
        SimpleNamespace(
            arrived=6.0,
            completed=13.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r2",
            input_tokens=600.0,
            output_tokens=50.0,
            requested_output_tokens=50.0,
            latency_ms=7000.0,
            ttft_ms=90.0,
            tpot_ms=15.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=50.0,
            failed_issue=False,
            chat_id="chat-1",
            turn=2,
            cached_tokens=150,
        ),
    ]
    cs_duck = telemetry_post.build_client_series(task_duck, duck_records)
    assert len(cs_duck["buckets"]) == 3
    # Bucket 0 (0..5s): Turn 1 cold -> 0% reusable
    assert cs_duck["buckets"][0]["input_tokens_total"] == 400
    assert cs_duck["buckets"][0]["reusable_prefix_tokens"] == 0
    assert cs_duck["buckets"][0]["reusable_prefix_pct"] == 0.0
    # Bucket 1 (5..10s): Turn 2 -> prior 500 of 600 reusable = 83.33%
    assert cs_duck["buckets"][1]["input_tokens_total"] == 600
    assert cs_duck["buckets"][1]["reusable_prefix_tokens"] == 500
    assert cs_duck["buckets"][1]["reusable_prefix_pct"] == 83.33
    # Bucket 2 (10..13s): no arrivals -> None pct
    assert cs_duck["buckets"][2]["input_tokens_total"] == 0
    assert cs_duck["buckets"][2]["reusable_prefix_tokens"] == 0
    assert cs_duck["buckets"][2]["reusable_prefix_pct"] is None
    # Redacting full build_client_series output preserves token_flow + per-bucket fields
    redacted_cs = telemetry_post.redact_manifest_payload(cs_duck)
    assert redacted_cs["token_flow"] == {
        "sent_tokens": 1000,
        "client_cached_tokens": 150,
        "raw_session_reusable_tokens": 500,
    }
    assert redacted_cs["buckets"][1]["reusable_prefix_tokens"] == 500
    assert redacted_cs["buckets"][1]["reusable_prefix_pct"] == 83.33

    # 3. Trace fallback when ArtifactRecord lacks session IDs (Bailian trace on disk)
    monkeypatch.setattr(BaseTrace, "trace_root", classmethod(lambda _cls: tmp_path))
    trace_file = tmp_path / "bailian_buckets.jsonl"
    trace_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 1.0,
                        "input_length": 200,
                        "output_length": 50,
                        "chat_id": "c1",
                        "parent_chat_id": None,
                        "turn": 1,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 7.0,
                        "input_length": 500,
                        "output_length": 50,
                        "chat_id": "c1",
                        "parent_chat_id": None,
                        "turn": 2,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    task_trace = _make_task("e1a2b3c4", duration_seconds=15, trace_path=trace_file.name, trace_format="bailian_trace")
    origin = 1_767_225_600.0
    trace_records = [
        ArtifactRecord(
            request_id="tr-1",
            arrived=origin + 1.0,
            completed=origin + 2.0,
            successful=True,
            input_tokens=200.0,
            output_tokens=50.0,
        ),
        ArtifactRecord(
            request_id="tr-2",
            arrived=origin + 7.0,
            completed=origin + 8.5,
            successful=True,
            input_tokens=500.0,
            output_tokens=50.0,
        ),
    ]
    cs_trace = telemetry_post.build_client_series(task_trace, trace_records)
    assert cs_trace["buckets"][0]["input_tokens_total"] == 200
    assert cs_trace["buckets"][0]["reusable_prefix_tokens"] == 0
    assert cs_trace["buckets"][0]["reusable_prefix_pct"] == 0.0
    assert cs_trace["buckets"][1]["input_tokens_total"] == 500
    assert cs_trace["buckets"][1]["reusable_prefix_tokens"] == 250
    assert cs_trace["buckets"][1]["reusable_prefix_pct"] == 50.0

    # 4. When records lack session IDs and trace is missing -> reusable_prefix_pct stays None
    cs_missing = telemetry_post.build_client_series(task_duck, trace_records)
    assert cs_missing["buckets"][0]["input_tokens_total"] == 200
    assert cs_missing["buckets"][0]["reusable_prefix_tokens"] is None
    assert cs_missing["buckets"][0]["reusable_prefix_pct"] is None


def test_per_node_sanitization_clamping_and_role_normalization() -> None:
    raw = {
        "schema_version": 7,
        "status": "available",
        "topology_mode": "disaggregated",
        "per_pod": [{"pod": "vllm-decode-0", "role": "decode"}],
        "per_node": [
            {
                "node": "node-a",
                "roles": ["decode", "model-server", "epp", "client"],
                "pods": ["vllm-decode-0", "ms-epp-0", "llm-d-lens-sim-123"],
                "pod_count": 3,
                "cpu_usage_percent": {"mean": 104.5, "max": -2.0},
                "memory_usage_percent": {"mean": 100.2, "max": 101.0},
                "gpu_utilization_percent": {"mean": -0.5, "max": 100.004},
            }
        ],
        "per_node_series": {
            "node-a": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cpu_usage_percent": 105.0,
                    "memory_usage_percent": -1.5,
                    "gpu_utilization_percent": 100.02,
                }
            ]
        },
        "system": {
            "cpu_avg_pct": 101.0,
            "cpu_peak_pct": -1.0,
            "mem_usage_pct": 102.0,
            "mem_peak_pct": -5.0,
        },
    }

    cleaned = telemetry_post.sanitize_server_telemetry(raw)
    assert cleaned is not None
    assert cleaned["topology_mode"] == "aggregated"
    node_a = cleaned["per_node"][0]
    assert node_a["roles"] == ["worker"]
    assert node_a["pods"] == ["vllm-decode-0"]
    assert node_a["pod_count"] == 1
    assert node_a["cpu_usage_percent"] == {"mean": 100.0, "max": 0.0}
    assert node_a["memory_usage_percent"] == {"mean": 100.0, "max": 100.0}
    assert node_a["gpu_utilization_percent"] == {"mean": 0.0, "max": 100.0}
    assert cleaned["per_node_series"]["node-a"][0]["cpu_usage_percent"] == 100.0
    assert cleaned["per_node_series"]["node-a"][0]["memory_usage_percent"] == 0.0
    assert cleaned["per_node_series"]["node-a"][0]["gpu_utilization_percent"] == 100.0
    assert cleaned["system"] == {
        "cpu_avg_pct": 100.0,
        "cpu_peak_pct": 0.0,
        "mem_usage_pct": 100.0,
        "mem_peak_pct": 0.0,
    }


@pytest.mark.asyncio
async def test_single_node_saved_run_backfill_and_system_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11

    long_series = [
        {
            "timestamp": datetime.fromtimestamp(1767225600 + i * 5, tz=UTC).isoformat().replace("+00:00", "Z"),
            "host_cpu_usage_percent": 40.0 + (i % 10),
            "host_memory_total_bytes": 1000.0,
            "host_memory_available_bytes": 400.0 if i % 2 == 0 else 200.0,
            "host_disk_read_bytes_per_second": 100.0,
            "host_disk_written_bytes_per_second": 50.0,
            "network_receive_bytes_per_second": 300.0,
            "network_transmit_bytes_per_second": 150.0,
            "host_power_watts": 250.0,
            "gpu_power_watts": 180.0,
            "gpu_utilization_percent": 85.0,
        }
        for i in range(300)
    ]

    legacy_single = {
        "schema_version": 6,
        "status": "available",
        "step_seconds": 5,
        "topology_mode": "aggregated",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:25:00Z"},
        "nodes": ["gpu-node-1"],
        "per_pod": [
            {"pod": "vllm-worker-0", "role": "worker", "gpu_utilization_percent": {"mean": 85.0, "max": 95.0}},
            {"pod": "ms-epp-0", "role": "epp"},
            {"pod": "llm-d-lens-sim-job-1", "role": "worker"},
        ],
        "device_telemetry": {
            "devices": [
                {"node": "gpu-node-1", "pci_bdf": "0000:01:00.0", "pod": "vllm-worker-0"},
                {"node": "gpu-node-1", "pci_bdf": "0000:02:00.0", "pod": "vllm-worker-0"},
            ]
        },
        "series": long_series,
        "summary": {
            "host_cpu_usage_percent": {"mean": 44.5, "max": 49.0},
            "host_memory_total_bytes": {"mean": 1000.0},
            "host_memory_available_bytes": {"mean": 300.0},
            "host_filesystem_avail_bytes": {"mean": 5000.0},
            "host_filesystem_size_bytes": {"mean": 10000.0},
            "host_disk_read_bytes_per_second": {"mean": 100.0, "max": 120.0},
            "host_disk_written_bytes_per_second": {"mean": 50.0, "max": 80.0},
            "network_receive_bytes_per_second": {"mean": 300.0, "max": 450.0},
            "network_transmit_bytes_per_second": {"mean": 150.0, "max": 220.0},
            "host_power_watts": {"mean": 250.0, "max": 280.0},
            "gpu_power_watts": {"mean": 180.0, "max": 210.0},
            "gpu_temperature_celsius": {"mean": 62.0, "max": 68.0},
            "gpu_utilization_percent": {"mean": 85.0, "max": 95.0},
            "gpu_framebuffer_used_bytes": {"mean": 16e9, "max": 20e9},
            "gpu_memory_total_bytes": {"mean": 80e9},
        },
    }

    task = _make_task(
        "11223344",
        endpoint_mode="in-cluster",
        endpoint_cluster_id="c-1",
        endpoint_namespace="ns-1",
    )
    monkeypatch.setattr(
        telemetry_post,
        "collect_namespace_observability",
        AsyncMock(return_value={"status": "empty", "series": [], "summary": {}}),
    )

    refreshed = await telemetry_post.refresh_persisted_server_telemetry(
        task, legacy_single, {"duration_seconds": 1500.0, "windows": {"full": {"total_output_tokens": 5000}}}
    )
    assert refreshed["schema_version"] == 11

    sys_info = refreshed["system"]
    assert sys_info["available"] is True
    assert sys_info["mem_used_bytes"] == 700.0
    assert sys_info["mem_usage_pct"] == 70.0
    assert sys_info["mem_peak_pct"] == 80.0
    assert sys_info["mem_avail_min_bytes"] == 200.0
    assert sys_info["disk_total_bytes"] == 10000.0
    assert sys_info["disk_read_peak_bps"] == 120.0
    assert sys_info["disk_write_peak_bps"] == 80.0
    assert sys_info["net_rx_bps"] == 300.0
    assert sys_info["net_rx_peak_bps"] == 450.0
    assert sys_info["net_tx_bps"] == 150.0
    assert sys_info["net_tx_peak_bps"] == 220.0
    assert sys_info["gpu_power_avg_watts"] == 180.0
    assert sys_info["gpu_power_peak_watts"] == 210.0
    assert sys_info["gpu_temp_peak_c"] == 68.0

    assert len(refreshed["per_node"]) == 1
    node = refreshed["per_node"][0]
    assert node["node"] == "gpu-node-1"
    assert node["roles"] == ["worker"]
    assert node["pods"] == ["vllm-worker-0"]
    assert node["pod_count"] == 1
    assert node["allocated_accelerators"] == 2
    assert node["cpu_usage_percent"] == {"mean": 44.5, "max": 49.0}
    assert node["memory_used_bytes"] == {"mean": 700.0, "max": 800.0}
    assert node["memory_available_bytes"] == {"mean": 300.0, "min": 200.0}
    assert node["memory_total_bytes"] == 1000.0
    assert node["memory_usage_percent"] == {"mean": 70.0, "max": 80.0}
    assert node["filesystem_avail_bytes"] == 5000.0
    assert node["filesystem_size_bytes"] == 10000.0
    assert node["network_receive_bytes_per_second"] == {"mean": 300.0, "max": 450.0}
    assert node["network_transmit_bytes_per_second"] == {"mean": 150.0, "max": 220.0}
    assert node["host_power_watts"] == {"mean": 250.0, "max": 280.0}
    assert node["gpu_utilization_percent"] == {"mean": 85.0, "max": 95.0}
    assert node["gpu_power_watts"] == {"mean": 180.0, "max": 210.0}
    assert node["gpu_temperature_celsius"] == {"mean": 62.0, "max": 68.0}
    assert node["gpu_memory_used_bytes"] == {"mean": 16e9, "max": 20e9}
    assert node["gpu_memory_total_bytes"] == 80e9

    node_series = refreshed["per_node_series"]["gpu-node-1"]
    assert len(node_series) == 240
    assert node_series[0]["cpu_usage_percent"] == 40.0
    assert node_series[0]["memory_available_bytes"] == 400.0
    assert node_series[0]["memory_used_bytes"] == 600.0
    assert node_series[0]["memory_usage_percent"] == 60.0
    assert node_series[0]["network_receive_bytes_per_second"] == 300.0
    assert node_series[0]["network_transmit_bytes_per_second"] == 150.0
    assert node_series[0]["host_power_watts"] == 250.0
    assert node_series[0]["gpu_power_watts"] == 180.0
    assert node_series[0]["gpu_utilization_percent"] == 85.0

    # Exact 0.0 readings in single-node backfill must be preserved, never coerced to None
    zero_srv = {
        "schema_version": 6,
        "status": "available",
        "nodes": ["zero-node"],
        "series": [],
        "summary": {
            "host_filesystem_avail_bytes": {"mean": 0.0},
            "host_filesystem_size_bytes": {"mean": 0.0},
            "host_power_watts": {"mean": 0.0, "max": 0.0},
            "gpu_power_watts": {"mean": 0.0, "max": 0.0},
            "gpu_memory_total_bytes": {"mean": 0.0},
        },
    }
    zero_enriched = telemetry_post._enrich_server_telemetry(zero_srv, {"duration_seconds": 10.0})
    zero_node = zero_enriched["per_node"][0]
    assert zero_node["filesystem_avail_bytes"] == 0.0
    assert zero_node["filesystem_size_bytes"] == 0.0
    assert zero_node["host_power_watts"] == {"mean": 0.0, "max": 0.0}
    assert zero_node["gpu_power_watts"] == {"mean": 0.0, "max": 0.0}
    assert zero_node["gpu_memory_total_bytes"] == 0.0


@pytest.mark.asyncio
async def test_multi_node_legacy_saved_run_backfill_leaves_host_metrics_none(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_multi = {
        "schema_version": 6,
        "status": "available",
        "step_seconds": 5,
        "topology_mode": "disaggregated",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:01:00Z"},
        "nodes": ["node-a", "node-b"],
        "per_pod": [
            {
                "pod": "vllm-prefill-0",
                "role": "prefill",
                "node": "node-a",
                "gpu_utilization_percent": {"mean": 80.0, "max": 90.0},
                "gpu_power_watts": {"mean": 300.0, "max": 350.0},
                "gpu_temperature_celsius": {"mean": 65.0, "max": 70.0},
                "gpu_framebuffer_used_bytes": {"mean": 20e9, "max": 24e9},
                "gpu_memory_total_bytes": {"mean": 80e9},
            },
            {
                "pod": "vllm-decode-0",
                "role": "decode",
                "node": "node-b",
                "gpu_utilization_percent": {"mean": 50.0, "max": 60.0},
                "gpu_power_watts": {"mean": 200.0, "max": 240.0},
                "gpu_temperature_celsius": {"mean": 58.0, "max": 62.0},
                "gpu_framebuffer_used_bytes": {"mean": 30e9, "max": 32e9},
                "gpu_memory_total_bytes": {"mean": 80e9},
            },
            {"pod": "ms-epp-0", "role": "epp", "node": "node-a"},
        ],
        "device_telemetry": {
            "devices": [
                {"node": "node-a", "pci_bdf": "0000:01:00.0", "pod": "vllm-prefill-0"},
                {"node": "node-b", "pci_bdf": "0000:02:00.0", "pod": "vllm-decode-0"},
                {"node": "node-b", "pci_bdf": "0000:03:00.0", "pod": "vllm-decode-0"},
            ]
        },
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "host_cpu_usage_percent": 75.0}],
        "summary": {
            "host_cpu_usage_percent": {"mean": 75.0, "max": 85.0},
            "host_memory_total_bytes": {"mean": 2000.0},
            "host_memory_available_bytes": {"mean": 800.0},
            "host_power_watts": {"mean": 900.0, "max": 950.0},
        },
    }

    task = _make_task(
        "22334455",
        endpoint_mode="in-cluster",
        endpoint_cluster_id="c-1",
        endpoint_namespace="ns-1",
    )
    monkeypatch.setattr(
        telemetry_post,
        "collect_namespace_observability",
        AsyncMock(return_value={"status": "unavailable", "series": [], "summary": {}}),
    )

    refreshed = await telemetry_post.refresh_persisted_server_telemetry(task, legacy_multi, {"duration_seconds": 60.0})
    assert refreshed["schema_version"] == 11
    assert refreshed["per_node_series"] == {}
    assert [n["node"] for n in refreshed["per_node"]] == ["node-a", "node-b"]

    node_a, node_b = refreshed["per_node"]
    assert node_a["roles"] == ["prefill"]
    assert node_a["pods"] == ["vllm-prefill-0"]
    assert node_a["pod_count"] == 1
    assert node_a["allocated_accelerators"] == 1
    assert node_a["gpu_utilization_percent"] == {"mean": 80.0, "max": 90.0}
    assert node_a["gpu_power_watts"] == {"mean": 300.0, "max": 350.0}
    assert node_a["gpu_temperature_celsius"] == {"mean": 65.0, "max": 70.0}
    assert node_a["gpu_memory_used_bytes"] == {"mean": 20e9, "max": 24e9}
    assert node_a["gpu_memory_total_bytes"] == 80e9

    assert node_b["roles"] == ["decode"]
    assert node_b["pods"] == ["vllm-decode-0"]
    assert node_b["pod_count"] == 1
    assert node_b["allocated_accelerators"] == 2
    assert node_b["gpu_utilization_percent"] == {"mean": 50.0, "max": 60.0}

    # Host metrics must NEVER attribute cluster-wide host sums to a single node in multi-node legacy artifacts
    host_fields = (
        "cpu_usage_percent",
        "memory_used_bytes",
        "memory_available_bytes",
        "memory_total_bytes",
        "memory_usage_percent",
        "disk_read_bytes_per_second",
        "disk_written_bytes_per_second",
        "filesystem_avail_bytes",
        "filesystem_size_bytes",
        "network_receive_bytes_per_second",
        "network_transmit_bytes_per_second",
        "host_power_watts",
    )
    for node_entry in (node_a, node_b):
        for field in host_fields:
            assert node_entry[field] is None


def test_slot_lanes_greedy_packing_tiers_sampling_and_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_client_series packs concurrent requests into reusable slots with 3-tier cache-hit classification."""
    assert telemetry_post.REPORT_SCHEMA_VERSION == 15
    assert telemetry_post.CLIENT_SERIES_SCHEMA_VERSION == 15
    assert telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11

    # 1. Greedy slot packing + reuse + 3-tier boundary classification + redaction safety
    task = _make_task("55a1b2c3", duration_seconds=10)
    boundary_records = [
        # [0.0, 2.0] -> slot 0, 9.9% -> cold
        SimpleNamespace(
            arrived=0.0,
            completed=2.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r0",
            input_tokens=1000.0,
            output_tokens=50.0,
            requested_output_tokens=50.0,
            latency_ms=2000.0,
            ttft_ms=110.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=50.0,
            failed_issue=False,
            session_id="sess-a",
            turn=1,
            cached_tokens=99,
        ),
        # [0.5, 3.0] -> slot 1, 10.0% -> partial
        SimpleNamespace(
            arrived=0.5,
            completed=3.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r1",
            input_tokens=1000.0,
            output_tokens=60.0,
            requested_output_tokens=60.0,
            latency_ms=2500.0,
            ttft_ms=90.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=60.0,
            failed_issue=False,
            session_id="sess-b",
            turn=1,
            cached_tokens=100,
        ),
        # [2.0, 4.0] -> reuses slot 0 (free at 2.0), 69.9% -> partial
        SimpleNamespace(
            arrived=2.0,
            completed=4.0,
            successful=True,
            status_code=200,
            error=None,
            request_id="r2",
            input_tokens=1000.0,
            output_tokens=70.0,
            requested_output_tokens=70.0,
            latency_ms=2000.0,
            ttft_ms=70.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=70.0,
            failed_issue=False,
            session_id="sess-a",
            turn=2,
            cached_tokens=699,
        ),
        # [2.5, 3.5] -> opens slot 2 (slot 0 free 4.0, slot 1 free 3.0), 70.0% -> warm
        SimpleNamespace(
            arrived=2.5,
            completed=3.5,
            successful=True,
            status_code=200,
            error=None,
            request_id="r3",
            input_tokens=1000.0,
            output_tokens=40.0,
            requested_output_tokens=40.0,
            latency_ms=1000.0,
            ttft_ms=35.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=40.0,
            failed_issue=False,
            session_id="sess-c",
            turn=1,
            cached_tokens=700,
        ),
        # [3.1, 4.5] -> reuses slot 1 (free at 3.0), clamped 120% -> 100.0% -> warm
        SimpleNamespace(
            arrived=3.1,
            completed=4.5,
            successful=True,
            status_code=200,
            error=None,
            request_id="r4",
            input_tokens=1000.0,
            output_tokens=80.0,
            requested_output_tokens=80.0,
            latency_ms=1400.0,
            ttft_ms=25.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=80.0,
            failed_issue=False,
            session_id="sess-b",
            turn=2,
            cached_tokens=1200,
        ),
        # Incomplete record (completed=None) must be excluded from slot_lanes
        SimpleNamespace(
            arrived=1.0,
            completed=None,
            successful=False,
            status_code=504,
            error="timeout",
            request_id="r-drop",
            input_tokens=500.0,
            output_tokens=0.0,
            requested_output_tokens=50.0,
            latency_ms=None,
            ttft_ms=None,
            tpot_ms=None,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category="timeout",
            rate_tokens=0.0,
            failed_issue=True,
            session_id="sess-drop",
            turn=1,
            cached_tokens=None,
        ),
    ]
    cs = telemetry_post.build_client_series(task, boundary_records)
    sl = cs["slot_lanes"]
    assert sl["peak_slots"] == 3
    assert sl["total_requests"] == 5
    assert sl["session_count"] == 3
    assert sl["sample_stride"] == 1
    assert sl["tier_counts"] == {"cold": 1, "partial": 2, "warm": 2}
    assert [(it["slot"], it["cache_hit_pct"], it["tier"], it["session_id"], it["turn"]) for it in sl["items"]] == [
        (0, 9.9, "cold", "sess-a", 1),
        (1, 10.0, "partial", "sess-b", 1),
        (0, 69.9, "partial", "sess-a", 2),
        (2, 70.0, "warm", "sess-c", 1),
        (1, 100.0, "warm", "sess-b", 2),
    ]
    assert sl["items"][0]["input_tokens"] == 1000
    assert sl["items"][0]["output_tokens"] == 50
    assert sl["items"][0]["ttft_ms"] == 110.0

    # Redaction must preserve every field inside slot_lanes
    redacted_cs = telemetry_post.redact_manifest_payload(cs)
    assert redacted_cs["slot_lanes"] == sl

    # 2. Trace-slice overlap fallback when ArtifactRecord lacks cached_tokens / session_id
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: tmp_path))
    trace_file = tmp_path / "slot_trace.jsonl"
    trace_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 1.0,
                        "input_length": 400,
                        "output_length": 50,
                        "chat_id": "c1",
                        "turn": 1,
                        "hash_ids": [1, 2, 3, 4],
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 3.0,
                        "input_length": 500,
                        "output_length": 50,
                        "chat_id": "c1",
                        "turn": 2,
                        "hash_ids": [1, 2, 3, 4, 5],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    trace_task = _make_task("55a1b2c4", duration_seconds=10, trace_path=trace_file.name, trace_format="bailian_trace")
    origin = 1_767_225_600.0
    trace_records = [
        ArtifactRecord(
            request_id="t1",
            arrived=origin + 1.0,
            completed=origin + 2.0,
            successful=True,
            input_tokens=400.0,
            output_tokens=50.0,
            ttft_ms=85.0,
        ),
        ArtifactRecord(
            request_id="t2",
            arrived=origin + 3.0,
            completed=origin + 4.0,
            successful=True,
            input_tokens=500.0,
            output_tokens=50.0,
            ttft_ms=30.0,
        ),
    ]
    cs_trace = telemetry_post.build_client_series(trace_task, trace_records)
    assert cs_trace["slot_lanes"]["peak_slots"] == 1
    assert cs_trace["slot_lanes"]["session_count"] == 1
    assert cs_trace["slot_lanes"]["tier_counts"] == {"cold": 1, "partial": 0, "warm": 1}
    assert [
        (it["cache_hit_pct"], it["tier"], it["session_id"], it["turn"]) for it in cs_trace["slot_lanes"]["items"]
    ] == [
        (0.0, "cold", "c1", 1),
        (80.0, "warm", "c1", 2),
    ]

    # 3. Stride sampling when valid requests > 2000 preserves exact aggregate metrics
    many_records = [
        SimpleNamespace(
            arrived=float(i) * 0.1,
            completed=float(i) * 0.1 + 0.35,
            successful=True,
            status_code=200,
            error=None,
            request_id=f"m-{i}",
            input_tokens=100.0,
            output_tokens=20.0,
            requested_output_tokens=20.0,
            latency_ms=350.0,
            ttft_ms=40.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            s_time_drift_ms=0.0,
            error_category=None,
            rate_tokens=20.0,
            failed_issue=False,
            session_id=f"s-{i % 10}",
            turn=(i // 10) + 1,
            cached_tokens=80 if i % 3 == 0 else (30 if i % 3 == 1 else 0),
        )
        for i in range(2500)
    ]
    cs_many = telemetry_post.build_client_series(task, many_records)
    sl_many = cs_many["slot_lanes"]
    assert sl_many["total_requests"] == 2500
    assert sl_many["sample_stride"] == 2
    assert len(sl_many["items"]) == 2000
    sampled_session_counts: dict[str, int] = {}
    for item in sl_many["items"]:
        sid = str(item["session_id"])
        sampled_session_counts[sid] = sampled_session_counts.get(sid, 0) + 1
    assert len(sampled_session_counts) == 8
    assert all(count == 250 for count in sampled_session_counts.values())
    assert "s-0" in sampled_session_counts and "s-9" in sampled_session_counts
    assert sl_many["peak_slots"] == 4
    assert sl_many["session_count"] == 10
    assert sum(sl_many["tier_counts"].values()) == 2500
    assert sl_many["tier_counts"] == {"cold": 833, "partial": 833, "warm": 834}


def test_build_heatmap_grid_fine_grained_bins_and_cell_percentiles() -> None:
    # 1. 20s / 46-request run -> time_bins == 40, len_bins == 28, and per-cell p50/p90/max stats
    points_46 = [(i * (20.0 / 46.0), float(100 + i * 20), 20.0 + i * 2.5) for i in range(46)]
    # Add 4 points in the very first cell (t<0.5, seq=100) to verify p50_ms / p90_ms / max_ms
    points_multi = [(0.01, 100.0, 20.0), (0.02, 100.0, 40.0), (0.03, 100.0, 80.0), (0.04, 100.0, 120.0), *points_46[2:]]
    grid_46 = telemetry_post._build_heatmap_grid(points_46, duration_seconds=20.0)
    assert grid_46 is not None
    assert grid_46["time_bins"] == 40
    assert grid_46["len_bins"] == 28
    assert sum(c["count"] for c in grid_46["cells"]) == 46
    for cell in grid_46["cells"]:
        assert cell["seq_end"] >= cell["seq_start"] + 1
        assert "p50_ms" in cell
        assert "p90_ms" in cell
        assert "max_ms" in cell
        assert cell["max_ms"] >= cell["p90_ms"] >= cell["p50_ms"]

    grid_multi = telemetry_post._build_heatmap_grid(points_multi, duration_seconds=20.0)
    assert grid_multi is not None
    first_cell = next(c for c in grid_multi["cells"] if c["t_idx"] == 0 and c["l_idx"] == 0)
    assert first_cell["count"] == 4
    assert first_cell["avg_ms"] == 65.0
    assert first_cell["p50_ms"] == 40.0
    assert first_cell["p90_ms"] == 120.0
    assert first_cell["max_ms"] == 120.0

    # 2. < 1s short run + narrow token span (< 15 tokens) avoids empty oversubscribed bins
    short_points = [(0.1 * i, float(100 + i), 10.0 + i) for i in range(5)]
    short_grid = telemetry_post._build_heatmap_grid(short_points, duration_seconds=0.5)
    assert short_grid is not None
    assert short_grid["time_bins"] == 5
    assert short_grid["len_bins"] == 5
    assert all(c["seq_end"] == c["seq_start"] + 1 for c in short_grid["cells"])

    # 3. Single constant token length -> len_bins == 1 with non-empty [512, 513) interval
    const_points = [(float(i), 512.0, 15.0 + i) for i in range(5)]
    const_grid = telemetry_post._build_heatmap_grid(const_points, duration_seconds=5.0)
    assert const_grid is not None
    assert const_grid["time_bins"] == 24
    assert const_grid["len_bins"] == 1
    assert all(c["seq_start"] == 512 and c["seq_end"] == 513 for c in const_grid["cells"])

    # 4. 2-point regression check: never more sequence bins than data points
    two_grid = telemetry_post._build_heatmap_grid([(0.1, 128.0, 50.0), (0.75, 256.0, 100.0)], duration_seconds=2.0)
    assert two_grid is not None
    assert two_grid["len_bins"] == 2
    assert {c["seq_start"] for c in two_grid["cells"]} == {128, 192}

    # 5. Sub-millisecond/zero duration, dense <1s run, 1-point, and float token edge cases
    zero_dur_points = [(0.0, float(100 + i), 10.0 + i) for i in range(48)]
    zero_dur_grid = telemetry_post._build_heatmap_grid(zero_dur_points, duration_seconds=0.0)
    assert zero_dur_grid is not None
    assert zero_dur_grid["time_bins"] == 48
    assert all(c["end_seconds"] > c["start_seconds"] and c["end_seconds"] > 0 for c in zero_dur_grid["cells"])

    dense_short_points = [(i * (0.2 / 48.0), float(100 + i), 12.0 + i) for i in range(48)]
    dense_short_grid = telemetry_post._build_heatmap_grid(dense_short_points, duration_seconds=0.2)
    assert dense_short_grid is not None
    assert dense_short_grid["time_bins"] == 48
    assert all(c["end_seconds"] > c["start_seconds"] and c["end_seconds"] > 0 for c in dense_short_grid["cells"])

    one_grid = telemetry_post._build_heatmap_grid([(0.5, 100.0, 25.0)], duration_seconds=0.5)
    assert one_grid is not None
    assert one_grid["time_bins"] == 1
    assert one_grid["len_bins"] == 1
    assert one_grid["cells"][0]["end_seconds"] > one_grid["cells"][0]["start_seconds"]
    assert one_grid["cells"][0]["end_seconds"] > 0
    assert one_grid["cells"][0]["seq_end"] >= one_grid["cells"][0]["seq_start"] + 1

    float_tok_grid = telemetry_post._build_heatmap_grid(
        [(0.1, 10.2, 12.0), (0.3, 10.8, 15.0), (0.5, 25.4, 20.0)],
        duration_seconds=1.0,
    )
    assert float_tok_grid is not None
    assert all(
        c["end_seconds"] > c["start_seconds"] and c["end_seconds"] > 0 and c["seq_end"] >= c["seq_start"] + 1
        for c in float_tok_grid["cells"]
    )


def test_analytics_latency_heatmap_fine_grained_bins_and_model_validation() -> None:
    # 1. 20s / 46-request run -> time_bin_count == 40, sequence_length_bin_count == 28, valid model
    records_46 = [(0.1 + i * (19.9 / 45.0), float(128 + i * 16), 25.0 + i) for i in range(46)]
    heatmap = latency_heatmap(
        records_46,
        metric="ttft",
        sequence_length="input",
        sequence_length_source="observed",
    )
    assert isinstance(heatmap, SimulationLatencyHeatmap)
    assert heatmap.time_bin_count == 40
    assert heatmap.sequence_length_bin_count == 28
    assert sum(c.request_count for c in heatmap.cells) == 46
    for cell in heatmap.cells:
        assert cell.sequence_length_end >= cell.sequence_length_start + 1
        assert set(cell.model_dump().keys()) == {
            "start_seconds",
            "end_seconds",
            "sequence_length_start",
            "sequence_length_end",
            "request_count",
            "average_metric_ms",
        }

    # 2. < 1s short run and single constant token length
    short_heatmap = latency_heatmap(
        [(0.1, 256.0, 12.0), (0.2, 256.0, 14.0), (0.3, 256.0, 16.0), (0.4, 256.0, 18.0)],
        metric="tpot",
        sequence_length="output",
        sequence_length_source="observed",
    )
    assert isinstance(short_heatmap, SimulationLatencyHeatmap)
    assert short_heatmap.time_bin_count == 4
    assert short_heatmap.sequence_length_bin_count == 1
    assert all(c.sequence_length_start == 256 and c.sequence_length_end == 257 for c in short_heatmap.cells)

    # 3. 2-point regression check: sequence_length_bin_count == 2 -> {128, 192}
    two_heatmap = latency_heatmap(
        [(0.1, 128.0, 50.0), (0.75, 256.0, 100.0)],
        metric="ttft",
        sequence_length="input",
        sequence_length_source="observed",
    )
    assert isinstance(two_heatmap, SimulationLatencyHeatmap)
    assert two_heatmap.sequence_length_bin_count == 2
    assert {c.sequence_length_start for c in two_heatmap.cells} == {128, 192}


def test_forensics_stage_latency_breakdown_server_client_zero_guards_and_series_cap() -> None:
    task = _make_task("51b00001")
    client = _base_forensics_client(ttft_p90=300.0, tpot_p90=25.0, e2e_p90=1200.0)
    client["windows"]["full"]["ttft_ms"] = {"p50": 175.0, "p90": 420.0, "avg": 210.0}
    client["windows"]["full"]["e2e_ms"] = {"p50": 995.0, "p90": 1600.0, "avg": 1100.0}

    # 1. Server telemetry: non-double-counted gateway_overhead_ms, P90 distinct from P50, mean client_fallback, <= 120-pt series
    srv = {
        "status": "available",
        "topology_mode": "aggregated",
        "step_seconds": 5,
        "window": {"start": "1970-01-01T00:16:40Z"},
        "router": {
            "scheduler_latency_p50_ms": {"mean": 2.4},
            "scheduler_latency_p90_ms": {"p90": 6.0},
        },
        "summary": {
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
            "vllm_queue_time_p50_ms": {"mean": 18.0},
            "vllm_prefill_time_p50_ms": {"mean": 140.0},
            "vllm_decode_time_p50_ms": {"mean": 820.0},
            "vllm_queue_time_p90_ms": {"mean": 200.0},
            "vllm_prefill_time_p90_ms": {"mean": 160.0},
            "vllm_decode_time_p90_ms": {"mean": 1100.0},
            "vllm_ttft_p50_ms": {"mean": 160.0},
            "vllm_e2e_p50_ms": {"mean": 980.0},
        },
        "series": [
            {
                "timestamp": datetime.fromtimestamp(1000 + i * 5, tz=UTC).isoformat().replace("+00:00", "Z"),
                "router_scheduler_latency_p50_ms": 2.4,
                "vllm_queue_time_p50_ms": 18.0,
                "vllm_prefill_time_p50_ms": 140.0,
                "vllm_decode_time_p50_ms": 2000.0 if i == 75 else 820.0,
                "router_scheduler_latency_p90_ms": 6.0,
                "vllm_queue_time_p90_ms": 200.0,
                "vllm_prefill_time_p90_ms": 160.0,
                "vllm_decode_time_p90_ms": 3000.0 if i == 75 else 1100.0,
                "kv_cache_usage_percent": 30.0,
                "queue_depth": 0.0,
            }
            for i in range(180)
        ],
    }
    res = analyze_forensics(task, client_series=client, server_telemetry=srv)
    assert res["schema_version"] == 8
    slb = res["stage_latency_breakdown"]
    assert slb["available"] is True
    assert slb["source"] == "server_telemetry"
    assert slb["has_p90_series"] is True
    assert slb["has_epp_series"] is True
    assert slb["primary_slice"] == "p50"
    assert slb["dominant_e2e_stage"] == "decode_time"
    assert slb["dominant_ttft_stage"] == "prefill_time"

    p50 = slb["p50"]
    # engine_overhead_ms = 160.0 - (18.0 + 140.0) = 2.0; gateway_overhead_ms = 175.0 - (2.4 + 160.0) = 12.6
    assert p50["epp_scheduling_ms"] == pytest.approx(2.4)
    assert p50["engine_overhead_ms"] == pytest.approx(2.0)
    assert p50["gateway_overhead_ms"] == pytest.approx(12.6)
    assert p50["ttft_stage_ms"] == pytest.approx(175.0)
    assert p50["total_stage_ms"] == pytest.approx(995.0)
    assert p50["share_of_e2e_pct"]["decode_time"] == pytest.approx(82.4)
    assert p50["share_of_ttft_pct"]["prefill_time"] == pytest.approx(80.0)

    # mean slice uses client_fallback when server mean histograms are absent
    assert slb["mean"]["available"] is True
    assert slb["mean"]["source"] == "client_fallback"
    assert slb["mean"]["ttft_stage_ms"] == pytest.approx(210.0)
    assert slb["mean"]["decode_time_ms"] == pytest.approx(890.0)

    # Series compacted to <= 120 points preserving start, end, and peak point (t = 75 * 5 = 375.0)
    assert len(slb["series"]) == 120
    assert slb["series"][0]["t"] == 0.0
    assert slb["series"][-1]["t"] == 895.0
    assert any(pt["t"] == 375.0 for pt in slb["series"])

    # Healthy verdict keeps healthy finding first, adds stage_dominance, and caps verdict.evidence <= 4 with Stage Breakdown
    assert res["verdict"]["code"] == "healthy"
    assert res["findings"][0]["code"] == "healthy"
    dom_finding = _finding(res, "stage_dominance")
    assert dom_finding["tab"] == "serving"
    assert dom_finding["anchor"] == "s-latency"
    assert len(res["verdict"]["evidence"]) <= 4

    # 2. Server P50-only must NOT copy P50 into P90
    srv_p50_only = {
        "status": "available",
        "topology_mode": "aggregated",
        "summary": {
            "vllm_queue_time_p50_ms": {"mean": 10.0},
            "vllm_prefill_time_p50_ms": {"mean": 80.0},
            "vllm_decode_time_p50_ms": {"mean": 300.0},
        },
        "series": [],
    }
    res_p50 = analyze_forensics(task, client_series=client, server_telemetry=srv_p50_only)
    assert res_p50["stage_latency_breakdown"]["p50"]["available"] is True
    assert res_p50["stage_latency_breakdown"]["p90"]["available"] is False

    # 3. Client-fallback mode + decode > 50% vs decode <= 50% + bucket ttft/e2e keys
    client_fb = {
        "schema_version": telemetry_post.REPORT_SCHEMA_VERSION,
        "warmup_seconds": 0.0,
        "duration_seconds": 20.0,
        "origin_epoch": 1000.0,
        "buckets": [
            {"mid_seconds": 5.0, "ttft": {"p50": 100.0, "p90": 200.0}, "e2e": {"p50": 500.0, "p90": 900.0}},
            {"mid_seconds": 15.0, "ttft_ms": {"p50": 150.0, "p90": 250.0}, "e2e_ms": {"p50": 650.0, "p90": 1000.0}},
        ],
        "windows": {
            "full": {
                "request_count": 20,
                "successful_requests": 20,
                "error_rate_pct": 0.0,
                "slo_attainment_pct": 100.0,
                "ttft_ms": {"p50": 100.0, "p90": 200.0},
                "tpot_ms": {"p90": 15.0},
                "e2e_ms": {"p50": 600.0, "p90": 1000.0},
            }
        },
    }
    res_fb = analyze_forensics(task, client_series=client_fb, server_telemetry=None)
    slb_fb = res_fb["stage_latency_breakdown"]
    assert slb_fb["source"] == "client_fallback"
    assert slb_fb["dominant_e2e_stage"] == "decode_time"
    assert slb_fb["dominant_ttft_stage"] is None
    assert slb_fb["p50"]["decode_time_ms"] == pytest.approx(500.0)
    assert slb_fb["p50"]["share_of_e2e_pct"]["ttft_unattributed"] == pytest.approx(16.7)
    assert len(slb_fb["series"]) == 2
    assert slb_fb["series"][1]["decode_ms"] == pytest.approx(500.0)
    dom_fb = _finding(res_fb, "stage_dominance")
    assert dom_fb["tab"] == "bottlenecks"
    assert dom_fb["anchor"] == "b-stage-breakdown"
    assert dom_fb["evidence"] == ["Client TTFT: 100 ms (16.7%)", "Decode Time: 500 ms (83.3%)"]

    # 4. Zero/null denominator guard & unavailable mode
    zero_client = {
        "windows": {
            "full": {
                "request_count": 1,
                "successful_requests": 1,
                "ttft_ms": {"p50": 0.0},
                "e2e_ms": {"p50": 0.0},
            }
        }
    }
    res_zero = analyze_forensics(task, client_series=zero_client, server_telemetry=None)
    assert res_zero["stage_latency_breakdown"]["p50"]["total_stage_ms"] == 0.0
    assert all(v is None for v in res_zero["stage_latency_breakdown"]["p50"]["share_of_e2e_pct"].values())

    res_unavail = analyze_forensics(task, client_series={"windows": {"full": {}}}, server_telemetry=None)
    assert res_unavail["stage_latency_breakdown"]["available"] is False
    assert res_unavail["stage_latency_breakdown"]["source"] == "unavailable"
    assert res_unavail["stage_latency_breakdown"]["series"] == []

    res_e2e_only = analyze_forensics(
        task,
        client_series={"windows": {"full": {"e2e_ms": {"p50": 500.0}}}, "buckets": [{"mid_seconds": 5.0}]},
        server_telemetry=None,
    )
    assert res_e2e_only["stage_latency_breakdown"]["available"] is False
    assert res_e2e_only["stage_latency_breakdown"]["source"] == "unavailable"
    assert res_e2e_only["stage_latency_breakdown"]["series"] == []

    # 5. Malformed/non-finite step_seconds & boolean values never raise or emit NaN/Inf JSON
    for bad_step in ("5s", float("inf"), float("nan"), True):
        srv_bad = {
            "status": "available",
            "step_seconds": bad_step,
            "summary": {
                "vllm_queue_time_p50_ms": {"mean": True},
                "vllm_prefill_time_p50_ms": {"mean": 120.0},
                "vllm_decode_time_p50_ms": {"mean": 480.0},
            },
            "series": [
                {"vllm_prefill_time_p50_ms": 120.0, "vllm_decode_time_p50_ms": 480.0},
                {"vllm_prefill_time_p50_ms": 130.0, "vllm_decode_time_p50_ms": 490.0},
            ],
        }
        res_bad = analyze_forensics(task, client_series=client, server_telemetry=srv_bad)
        assert res_bad["stage_latency_breakdown"]["p50"]["queue_time_ms"] is None
        assert res_bad["stage_latency_breakdown"]["series"][1]["t"] == 5.0
        json.dumps(res_bad["stage_latency_breakdown"], allow_nan=False)


def test_forensics_peak_window_shift_enriched_findings_and_evidence_cap() -> None:
    task = _make_task("51b00002")
    client = _base_forensics_client(ttft_p90=2500.0, tpot_p90=140.0, e2e_p90=4000.0)
    client["windows"]["full"]["ttft_ms"] = {"p50": 220.0, "p90": 2500.0}
    client["windows"]["full"]["e2e_ms"] = {"p50": 1000.0, "p90": 4000.0}

    # 1. Steady-state saturation spike hidden by low P50 queue baseline -> peak_window_shift + stage_shift finding
    srv_shift = {
        "status": "available",
        "topology_mode": "aggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "summary": {
            "queue_depth": {"mean": 0.1, "max": 2.0},
            "kv_cache_usage_percent": {"mean": 40.0, "max": 50.0},
            "vllm_queue_time_p50_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 140.0},
            "vllm_decode_time_p50_ms": {"mean": 780.0},
            "vllm_queue_time_p90_ms": {"mean": 120.0},
            "vllm_prefill_time_p90_ms": {"mean": 220.0},
            "vllm_decode_time_p90_ms": {"mean": 900.0},
        },
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "vllm_queue_time_p50_ms": 20.0,
                "vllm_prefill_time_p50_ms": 140.0,
                "vllm_decode_time_p50_ms": 780.0,
                "vllm_queue_time_p90_ms": 40.0,
                "vllm_prefill_time_p90_ms": 180.0,
                "vllm_decode_time_p90_ms": 850.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 35.0,
            },
            {
                "timestamp": "1970-01-01T00:17:05Z",
                "vllm_queue_time_p50_ms": 25.0,
                "vllm_prefill_time_p50_ms": 150.0,
                "vllm_decode_time_p50_ms": 780.0,
                "vllm_queue_time_p90_ms": 1900.0,
                "vllm_prefill_time_p90_ms": 220.0,
                "vllm_decode_time_p90_ms": 900.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 45.0,
            },
        ],
    }
    res_shift = analyze_forensics(task, client_series=client, server_telemetry=srv_shift)
    shift = res_shift["stage_latency_breakdown"]["peak_window_shift"]
    assert shift["detected"] is True
    assert shift["peak_stage"] == "queue_time"
    assert shift["summary"]
    shift_finding = _finding(res_shift, "stage_shift")
    assert shift_finding["severity"] == "warning"
    assert shift_finding["tab"] == "bottlenecks"
    assert len(res_shift["verdict"]["evidence"]) <= 4

    # 2. high_ttft Gateway / EPP Overhead Dominated disambiguation
    client_gw = _base_forensics_client(ttft_p90=2600.0, tpot_p90=30.0, e2e_p90=3200.0)
    client_gw["windows"]["full"]["ttft_ms"] = {"p50": 2100.0, "p90": 2600.0}
    client_gw["windows"]["full"]["e2e_ms"] = {"p50": 2700.0, "p90": 3200.0}
    srv_gw = {
        "status": "available",
        "topology_mode": "aggregated",
        "router": {"scheduler_latency_p50_ms": {"mean": 1200.0}},
        "summary": {
            "vllm_queue_time_p50_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 150.0},
            "vllm_decode_time_p50_ms": {"mean": 500.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
        },
        "series": [],
    }
    res_gw = analyze_forensics(task, client_series=client_gw, server_telemetry=srv_gw)
    gw_finding = _finding(res_gw, "high_ttft")
    assert "EPP Scheduling Dominated" in gw_finding["title"]
    assert "EPP scheduler" in gw_finding["recommendation"]

    # 3. 4-item verdict.evidence cap preserves Stage Breakdown bullet when findings[:4] has >= 4 non-stage findings
    client_many = _base_forensics_client(ttft_p90=2500.0, tpot_p90=140.0, e2e_p90=4000.0, drift_p90=600.0)
    client_many["windows"]["full"]["error_rate_pct"] = 2.0
    client_many["windows"]["full"]["ttft_ms"] = {"p50": 400.0, "p90": 2500.0}
    client_many["windows"]["full"]["e2e_ms"] = {"p50": 2000.0, "p90": 4000.0}
    srv_many = {
        "status": "available",
        "topology_mode": "aggregated",
        "summary": {
            "vllm_queue_time_p50_ms": {"mean": 30.0},
            "vllm_prefill_time_p50_ms": {"mean": 250.0},
            "vllm_decode_time_p50_ms": {"mean": 1500.0},
            "kv_cache_usage_percent": {"mean": 90.0, "max": 92.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
        },
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 92.0, "queue_depth": 0.0}],
    }
    res_many = analyze_forensics(task, client_series=client_many, server_telemetry=srv_many)
    assert len(res_many["verdict"]["evidence"]) == 4
    assert any(item.startswith("Stage Breakdown (") for item in res_many["verdict"]["evidence"])


def test_forensics_ec80a90a_decode_dominant_regression_and_r5_r6_arbitration() -> None:
    # Exact ec80a90a / d527cf07 regression: Decode accounts for ~97% of E2E while TTFT P90
    # barely breaches 1000 ms SLO (+2.1%) and engine Prefill Time P90 stays within budget.
    task = _make_task(
        "ec80a90a",
        backend_options={"ttft_slo": 1.0, "tpot_slo": 0.05, "error_rate_slo": 5.0},
    )
    client_ec80 = _base_forensics_client(ttft_p90=1021.35, tpot_p90=51.22, e2e_p90=29139.15)
    client_ec80["windows"]["full"]["slo_attainment_pct"] = 78.5
    client_ec80["windows"]["full"]["ttft_ms"] = {"p50": 258.26, "p90": 1021.35}
    client_ec80["windows"]["full"]["tpot_ms"] = {"p50": 35.0, "p90": 51.22}
    client_ec80["windows"]["full"]["e2e_ms"] = {"p50": 14521.6, "p90": 29139.15}

    srv_ec80 = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "kv_cache_usage_percent": 45.0,
                "queue_depth": 0.0,
                "gpu_utilization_percent": 95.4,
                "vllm_preemptions_total": 0.0,
            }
        ],
        "summary": {
            "kv_cache_usage_percent": {"mean": 45.0, "max": 45.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "gpu_utilization_percent": {"mean": 95.4},
            "vllm_queue_time_p50_ms": {"mean": 150.0},
            "vllm_prefill_time_p50_ms": {"mean": 225.8},
            "vllm_decode_time_p50_ms": {"mean": 14145.8},
            "vllm_queue_time_p90_ms": {"mean": 270.0},
            "vllm_prefill_time_p90_ms": {"mean": 744.7},
            "vllm_decode_time_p90_ms": {"mean": 28117.8},
        },
    }

    res_ec80 = analyze_forensics(task, client_series=client_ec80, server_telemetry=srv_ec80)
    assert res_ec80["schema_version"] == 8
    assert res_ec80["verdict"]["code"] == "decode_bound"
    assert res_ec80["verdict"]["label"] == "Decode Bound"
    assert res_ec80["verdict"]["primary_stage"] == "Decode Generation"
    r5_ec80 = next(r for r in res_ec80["rules_evaluated"] if r["code"] == "prefill_bound")
    r6_ec80 = next(r for r in res_ec80["rules_evaluated"] if r["code"] == "decode_bound")
    assert r5_ec80["triggered"] is False
    assert "within budget" in r5_ec80["reason"]
    assert r6_ec80["triggered"] is True
    assert _finding(res_ec80, "high_ttft")["metric"] == "ttft_p90_ms"

    # (a1) Genuine prefill_bound: Prefill dominates TTFT & E2E while TPOT is healthy
    client_prefill = _base_forensics_client(ttft_p90=2400.0, tpot_p90=30.0, e2e_p90=3000.0)
    client_prefill["windows"]["full"]["slo_attainment_pct"] = 60.0
    client_prefill["windows"]["full"]["ttft_ms"] = {"p50": 1500.0, "p90": 2400.0}
    client_prefill["windows"]["full"]["e2e_ms"] = {"p50": 2000.0, "p90": 3000.0}
    srv_prefill = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 1450.0},
            "vllm_decode_time_p50_ms": {"mean": 500.0},
            "vllm_queue_time_p90_ms": {"mean": 30.0},
            "vllm_prefill_time_p90_ms": {"mean": 2300.0},
            "vllm_decode_time_p90_ms": {"mean": 600.0},
        },
    }
    res_prefill = analyze_forensics(task, client_series=client_prefill, server_telemetry=srv_prefill)
    assert res_prefill["verdict"]["code"] == "prefill_bound"

    # (a2) Severe TTFT (>2.5x SLO) vs mild TPOT (<1.15x SLO) still picks prefill_bound even when decode >= 65%
    client_sev_ttft = _base_forensics_client(ttft_p90=2800.0, tpot_p90=54.0, e2e_p90=10000.0)
    client_sev_ttft["windows"]["full"]["slo_attainment_pct"] = 55.0
    client_sev_ttft["windows"]["full"]["ttft_ms"] = {"p50": 1800.0, "p90": 2800.0}
    client_sev_ttft["windows"]["full"]["e2e_ms"] = {"p50": 6000.0, "p90": 10000.0}
    srv_sev_ttft = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 1600.0},
            "vllm_decode_time_p50_ms": {"mean": 4000.0},
            "vllm_queue_time_p90_ms": {"mean": 50.0},
            "vllm_prefill_time_p90_ms": {"mean": 2500.0},
            "vllm_decode_time_p90_ms": {"mean": 7200.0},
        },
    }
    res_sev_ttft = analyze_forensics(task, client_series=client_sev_ttft, server_telemetry=srv_sev_ttft)
    assert res_sev_ttft["verdict"]["code"] == "prefill_bound"

    # (b) Both TTFT & TPOT breach with decode >= 65% and >= 2x prefill (70% decode / 20% prefill) picks decode_bound + high_ttft
    client_both = _base_forensics_client(ttft_p90=1600.0, tpot_p90=75.0, e2e_p90=6000.0)
    client_both["windows"]["full"]["slo_attainment_pct"] = 50.0
    client_both["windows"]["full"]["ttft_ms"] = {"p50": 900.0, "p90": 1600.0}
    client_both["windows"]["full"]["e2e_ms"] = {"p50": 3500.0, "p90": 6000.0}
    srv_both = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 40.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 40.0, "max": 40.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 100.0},
            "vllm_prefill_time_p50_ms": {"mean": 700.0},
            "vllm_decode_time_p50_ms": {"mean": 2500.0},
            "vllm_queue_time_p90_ms": {"mean": 600.0},
            "vllm_prefill_time_p90_ms": {"mean": 1200.0},
            "vllm_decode_time_p90_ms": {"mean": 4200.0},
        },
    }
    res_both = analyze_forensics(task, client_series=client_both, server_telemetry=srv_both)
    assert res_both["verdict"]["code"] == "decode_bound"
    assert res_both["verdict"]["label"] == "Decode Bound"
    assert _finding(res_both, "high_ttft")["metric"] == "ttft_p90_ms"

    # (c) Overwhelming decode dominance (>= 85% E2E, prefill <= 10%, prefill_p90 <= ttft_slo, attainment < 95%, TPOT healthy)
    client_sat = _base_forensics_client(ttft_p90=1150.0, tpot_p90=42.0, e2e_p90=18000.0)
    client_sat["windows"]["full"]["slo_attainment_pct"] = 82.0
    client_sat["windows"]["full"]["ttft_ms"] = {"p50": 350.0, "p90": 1150.0}
    client_sat["windows"]["full"]["e2e_ms"] = {"p50": 9000.0, "p90": 18000.0}
    srv_sat = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 50.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 50.0, "max": 50.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 50.0},
            "vllm_prefill_time_p50_ms": {"mean": 300.0},
            "vllm_decode_time_p50_ms": {"mean": 8650.0},
            "vllm_queue_time_p90_ms": {"mean": 200.0},
            "vllm_prefill_time_p90_ms": {"mean": 850.0},
            "vllm_decode_time_p90_ms": {"mean": 16800.0},
        },
    }
    res_sat = analyze_forensics(task, client_series=client_sat, server_telemetry=srv_sat)
    assert res_sat["verdict"]["code"] == "decode_bound"
    r5_sat = next(r for r in res_sat["rules_evaluated"] if r["code"] == "prefill_bound")
    r6_sat = next(r for r in res_sat["rules_evaluated"] if r["code"] == "decode_bound")
    assert r5_sat["triggered"] is False
    assert r6_sat["triggered"] is True
    assert "saturation" in r6_sat["reason"].lower()
    assert "42.0 ms/tok" in r6_sat["reason"]
    assert "indicates decode throughput" not in r6_sat["reason"]

    # (d) Gateway/EPP overhead dominating TTFT (>= 45%) with engine prefill within budget does not trigger r5
    client_gw_gate = _base_forensics_client(ttft_p90=2600.0, tpot_p90=30.0, e2e_p90=3200.0)
    client_gw_gate["windows"]["full"]["ttft_ms"] = {"p50": 2100.0, "p90": 2600.0}
    client_gw_gate["windows"]["full"]["e2e_ms"] = {"p50": 2700.0, "p90": 3200.0}
    srv_gw_gate = {
        "status": "available",
        "topology_mode": "aggregated",
        "router": {"scheduler_latency_p50_ms": {"mean": 1200.0}},
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 30.0, "queue_depth": 0.0}],
        "summary": {
            "vllm_queue_time_p50_ms": {"mean": 20.0},
            "vllm_prefill_time_p50_ms": {"mean": 150.0},
            "vllm_decode_time_p50_ms": {"mean": 500.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 30.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
        },
    }
    res_gw_gate = analyze_forensics(task, client_series=client_gw_gate, server_telemetry=srv_gw_gate)
    r5_gw = next(r for r in res_gw_gate["rules_evaluated"] if r["code"] == "prefill_bound")
    assert r5_gw["triggered"] is False
    assert "EPP Scheduling" in r5_gw["reason"]

    # (e) Long-output genuine prefill_bound (vllm_prefill_p90 > ttft_slo_ms, decode_e2e_pct >= 85%, healthy tpot_p90)
    client_long_out_prefill = _base_forensics_client(ttft_p90=2800.0, tpot_p90=25.0, e2e_p90=22800.0)
    client_long_out_prefill["windows"]["full"]["slo_attainment_pct"] = 40.0
    client_long_out_prefill["windows"]["full"]["ttft_ms"] = {"p50": 1500.0, "p90": 2800.0}
    client_long_out_prefill["windows"]["full"]["e2e_ms"] = {"p50": 12000.0, "p90": 22800.0}
    srv_long_out_prefill = {
        "status": "available",
        "topology_mode": "aggregated",
        "series": [{"timestamp": "1970-01-01T00:16:45Z", "kv_cache_usage_percent": 40.0, "queue_depth": 0.0}],
        "summary": {
            "kv_cache_usage_percent": {"mean": 40.0, "max": 40.0},
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "vllm_queue_time_p50_ms": {"mean": 100.0},
            "vllm_prefill_time_p50_ms": {"mean": 1400.0},
            "vllm_decode_time_p50_ms": {"mean": 10500.0},
            "vllm_queue_time_p90_ms": {"mean": 200.0},
            "vllm_prefill_time_p90_ms": {"mean": 2600.0},
            "vllm_decode_time_p90_ms": {"mean": 20000.0},
        },
    }
    res_long_out_prefill = analyze_forensics(
        task, client_series=client_long_out_prefill, server_telemetry=srv_long_out_prefill
    )
    assert res_long_out_prefill["verdict"]["code"] == "prefill_bound"
    r5_lop = next(r for r in res_long_out_prefill["rules_evaluated"] if r["code"] == "prefill_bound")
    r6_lop = next(r for r in res_long_out_prefill["rules_evaluated"] if r["code"] == "decode_bound")
    assert r5_lop["triggered"] is True
    assert r6_lop["triggered"] is False

    # (f) tpot_p90=None under overwhelming decode dominance handles None cleanly without mentioning TPOT
    client_sat_no_tpot = json.loads(json.dumps(client_sat))
    client_sat_no_tpot["windows"]["full"]["tpot_ms"] = {"p50": None, "p90": None}
    res_sat_no_tpot = analyze_forensics(task, client_series=client_sat_no_tpot, server_telemetry=srv_sat)
    assert res_sat_no_tpot["verdict"]["code"] == "decode_bound"
    r6_no_tpot = next(r for r in res_sat_no_tpot["rules_evaluated"] if r["code"] == "decode_bound")
    assert r6_no_tpot["triggered"] is True
    assert "TPOT" not in r6_no_tpot["reason"]

    # (g) Client-only (server_telemetry=None, ttft_ratio < 0.15) arbitration picks decode_bound when both r5 and r6 trigger
    client_only_arb = _base_forensics_client(ttft_p90=1500.0, tpot_p90=70.0, e2e_p90=15000.0)
    client_only_arb["windows"]["full"]["slo_attainment_pct"] = 60.0
    res_client_only = analyze_forensics(task, client_series=client_only_arb, server_telemetry=None)
    assert res_client_only["verdict"]["code"] == "decode_bound"
    r5_co = next(r for r in res_client_only["rules_evaluated"] if r["code"] == "prefill_bound")
    r6_co = next(r for r in res_client_only["rules_evaluated"] if r["code"] == "decode_bound")
    assert r5_co["triggered"] is True
    assert r6_co["triggered"] is True


def test_bailian_root_chain_resolution_and_executed_turn_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bailian parent_chat_id chains (-1/'-1'/None/0/cycle/out-of-slice) resolve root_chat_id and turn_distribution."""
    trace_root = tmp_path / "traces"
    trace_root.mkdir()
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: trace_root))

    trace_file = trace_root / "bailian_chains.jsonl"
    # Session 0 (chat_id=0, parent=-1) -> 3 turns (0 -> 10 -> 20), missing explicit turn on 20
    # Session 1 (chat_id=1, parent="-1") -> 1 turn executed (second turn 11 unexecuted in partial run)
    # Cycle guard (chat_id=90 -> parent 91, 91 -> parent 90)
    # Out-of-slice parent (chat_id=30, parent=999 at t=50s when start_seconds=5s)
    entries = [
        {
            "timestamp": 0.0,
            "input_length": 100,
            "output_length": 20,
            "chat_id": 0,
            "parent_chat_id": -1,
            "turn": 1,
            "hash_ids": [1, 2],
        },
        {
            "timestamp": 2.0,
            "input_length": 150,
            "output_length": 25,
            "chat_id": 1,
            "parent_chat_id": "-1",
            "turn": 1,
            "hash_ids": [9],
        },
        {
            "timestamp": 10.0,
            "input_length": 200,
            "output_length": 30,
            "chat_id": 10,
            "parent_chat_id": 0,
            "turn": 2,
            "hash_ids": [1, 2, 3],
        },
        {
            "timestamp": 20.0,
            "input_length": 300,
            "output_length": 40,
            "chat_id": 20,
            "parent_chat_id": 10,
            "hash_ids": [1, 2, 3, 4],
        },
        {
            "timestamp": 30.0,
            "input_length": 250,
            "output_length": 20,
            "chat_id": 11,
            "parent_chat_id": 1,
            "turn": 2,
            "hash_ids": [9, 10],
        },
        {
            "timestamp": 40.0,
            "input_length": 180,
            "output_length": 15,
            "chat_id": 90,
            "parent_chat_id": 91,
            "hash_ids": [7],
        },
        {
            "timestamp": 41.0,
            "input_length": 185,
            "output_length": 15,
            "chat_id": 91,
            "parent_chat_id": 90,
            "hash_ids": [7, 8],
        },
        {
            "timestamp": 50.0,
            "input_length": 190,
            "output_length": 15,
            "chat_id": 30,
            "parent_chat_id": 999,
            "hash_ids": [5],
        },
    ]
    trace_file.write_text("\n".join(json.dumps(row) for row in entries) + "\n", encoding="utf-8")

    task = _make_task("b1a2c3d4", trace_path="bailian_chains.jsonl", trace_format="bailian_trace", duration_seconds=60)
    task.task_dir = str(tmp_path / "task_dir")

    # Verify _load_trace_slice chain resolution (including cycle and out-of-slice fallback)
    loaded = telemetry_post._load_trace_slice(task, trace_file)
    assert loaded is not None
    slice_items, has_sessions, has_hashes, _ = loaded
    assert has_sessions is True and has_hashes is True
    by_cid = {it["chat_id"]: it for it in slice_items}
    assert by_cid["0"]["root_chat_id"] == "0"
    assert by_cid["10"]["root_chat_id"] == "0"
    assert by_cid["20"]["root_chat_id"] == "0"
    assert by_cid["20"]["turn"] == 3  # derived from chain depth when explicit turn missing
    assert by_cid["1"]["root_chat_id"] == "1"
    assert by_cid["11"]["root_chat_id"] == "1"
    assert by_cid["90"]["root_chat_id"] in {"90", "91"}
    assert by_cid["30"]["root_chat_id"] == "999"

    # Simulate partial execution: only session 0 (all 3 turns) + session 1 (turn 1 only) executed
    origin = 1_767_225_600.0
    records = [
        ArtifactRecord(
            request_id="r0",
            arrived=origin + 0.0,
            completed=origin + 1.0,
            successful=True,
            ttft_ms=100.0,
            input_tokens=100.0,
            output_tokens=20.0,
        ),
        ArtifactRecord(
            request_id="r1",
            arrived=origin + 2.0,
            completed=origin + 3.0,
            successful=True,
            ttft_ms=95.0,
            input_tokens=150.0,
            output_tokens=25.0,
        ),
        ArtifactRecord(
            request_id="r2",
            arrived=origin + 10.0,
            completed=origin + 11.0,
            successful=True,
            ttft_ms=45.0,
            input_tokens=200.0,
            output_tokens=30.0,
        ),
        ArtifactRecord(
            request_id="r3",
            arrived=origin + 20.0,
            completed=origin + 21.0,
            successful=True,
            ttft_ms=30.0,
            input_tokens=300.0,
            output_tokens=40.0,
        ),
    ]
    res = telemetry_post.correlate_trace_sessions(task, records, time_origin=origin)
    assert res["available"] is True
    assert res["correlation"]["matched_count"] == 4
    assert res["session_ids"] == ["0", "1"]
    assert res["turn_distribution"] == [{"turns": 1, "sessions": 1}, {"turns": 3, "sessions": 1}]
    assert res["tree_depth_distribution"] == [{"depth": 1, "sessions": 1}, {"depth": 3, "sessions": 1}]


def test_aiperf_sidecar_matching_execution_order_overlap_and_tier2_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIPerf profile_export sidecar + execution-order hash_ids populate slot_lanes & cache_buckets; Tier-2 handles tokenizer delta."""
    from llm_d_lens.simulation.backends import get_backend

    trace_root = tmp_path / "traces"
    trace_root.mkdir()
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: trace_root))

    # Trace includes an unexecuted row between turn 1 and turn 2 to verify execution-order overlap ignores unexecuted items
    trace_file = trace_root / "aiperf_bailian.jsonl"
    trace_rows = [
        {
            "timestamp": 0.0,
            "input_length": 200,
            "output_length": 40,
            "chat_id": 0,
            "parent_chat_id": -1,
            "turn": 1,
            "hash_ids": [1, 2, 3, 4],
        },
        {
            "timestamp": 1.0,
            "input_length": 500,
            "output_length": 40,
            "chat_id": 99,
            "parent_chat_id": -1,
            "turn": 1,
            "hash_ids": [10, 11, 12, 13],
        },
        {
            "timestamp": 4.0,
            "input_length": 300,
            "output_length": 50,
            "chat_id": 100,
            "parent_chat_id": 0,
            "turn": 2,
            "hash_ids": [1, 2, 10, 11, 12, 13, 14, 15, 16, 17],
        },
        {
            "timestamp": 8.0,
            "input_length": 400,
            "output_length": 60,
            "chat_id": 101,
            "parent_chat_id": 100,
            "turn": 3,
            "hash_ids": [1, 2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
        },
        {
            "timestamp": 12.0,
            "input_length": 450,
            "output_length": 20,
            "chat_id": 102,
            "parent_chat_id": 101,
            "turn": 4,
            "hash_ids": [1, 2],
        },
    ]
    trace_file.write_text("\n".join(json.dumps(r) for r in trace_rows) + "\n", encoding="utf-8")

    task = _make_task("a1b2c3d4", trace_path="aiperf_bailian.jsonl", trace_format="bailian_trace", duration_seconds=60)
    task = task.model_copy(
        update={
            "task_dir": str(tmp_path / "aiperf_task"),
            "simulation": task.simulation.model_copy(update={"backend": "aiperf"}),
        }
    )
    aiperf_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    aiperf_dir.mkdir(parents=True)

    origin_ns = 1_767_225_600_000_000_000
    # Note: conversation_id="0" + turn_index=0..3, with tokenizer length delta on turn 2 & 3, and missing ISL on 400 error turn 4
    sidecar_rows = [
        {
            "metadata": {
                "conversation_id": "0",
                "turn_index": 0,
                "request_start_ns": origin_ns,
                "request_end_ns": origin_ns + 500_000_000,
                "benchmark_phase": "profiling",
            },
            "metrics": {
                "input_sequence_length": {"value": 200},
                "output_sequence_length": {"value": 40},
                "time_to_first_token": {"value": 120.0},
                "request_latency": {"value": 500.0},
            },
            "prompt": "SENSITIVE_PROMPT_SHOULD_NOT_BE_READ",
        },
        {
            "metadata": {
                "conversation_id": "0",
                "turn_index": 1,
                "request_start_ns": origin_ns + 2_000_000_000,
                "request_end_ns": origin_ns + 2_600_000_000,
                "benchmark_phase": "profiling",
            },
            "metrics": {
                "input_sequence_length": {"value": 540},
                "output_sequence_length": {"value": 50},
                "time_to_first_token": {"value": 80.0},
                "request_latency": {"value": 600.0},
            },
        },
        {
            "metadata": {
                "conversation_id": "0",
                "turn_index": 2,
                "request_start_ns": origin_ns + 5_000_000_000,
                "request_end_ns": origin_ns + 5_500_000_000,
                "benchmark_phase": "profiling",
            },
            "metrics": {
                "input_sequence_length": {"value": 990},
                "output_sequence_length": {"value": 60},
                "time_to_first_token": {"value": 30.0},
                "request_latency": {"value": 500.0},
            },
        },
        {
            "metadata": {
                "conversation_id": "0",
                "turn_index": 3,
                "request_start_ns": origin_ns + 9_000_000_000,
                "request_end_ns": origin_ns + 9_050_000_000,
                "benchmark_phase": "profiling",
            },
            "metrics": {"error_isl": {"value": 49000}},
            "error": {"code": 400, "message": "context exceeded"},
        },
    ]
    (aiperf_dir / "profile_export.jsonl").write_text(
        "\n".join(json.dumps(r) for r in sidecar_rows) + "\n", encoding="utf-8"
    )

    records = get_backend("aiperf").artifact_records(task, tolerate_incomplete=True)
    assert len(records) == 4

    ss = telemetry_post.correlate_trace_sessions(task, records, time_origin=origin_ns / 1e9)
    assert ss["correlation"]["matched_count"] == 4
    assert ss["correlation"]["matched_ratio"] == 1.0
    assert ss["session_ids"] == ["0"]
    assert ss["turn_distribution"] == [{"turns": 4, "sessions": 1}]
    # Execution-order overlaps: turn1=0.0 (cold), turn2=2/10=0.2 (partial, since row 99 wasn't executed!), turn3=10/12=0.833 (warm)
    buckets_by_name = {b["bucket"]: b["count"] for b in ss["cache_buckets"]}
    assert buckets_by_name == {"cold": 1, "partial": 1, "warm": 1}

    cs = telemetry_post.build_client_series(task, records)
    sl = cs["slot_lanes"]
    assert sl["session_count"] == 1
    assert sl["total_requests"] == 4
    assert sl["tier_counts"] == {"cold": 1, "partial": 1, "warm": 2}
    assert all(it["session_id"] == "0" for it in sl["items"])
    assert [it["turn"] for it in sl["items"]] == [1, 2, 3, 4]
    assert "SENSITIVE_PROMPT" not in json.dumps(cs)

    # Symlink sidecar file must be rejected safely
    outside_sidecar = tmp_path / "outside_export.jsonl"
    outside_sidecar.write_text(json.dumps(sidecar_rows[0]) + "\n", encoding="utf-8")
    (aiperf_dir / "profile_export.jsonl").unlink()
    (aiperf_dir / "profile_export.jsonl").symlink_to(outside_sidecar)
    assert telemetry_post._extract_sidecar_request_metadata(task, records) == {}

    # Tier-2 relaxed fallback when conversation_id is absent (tokenizer framing delta <= max(16, 8%) and ±2000ms window)
    tier2_trace = trace_root / "tier2_relaxed.jsonl"
    tier2_trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 0.0,
                        "input_length": 200,
                        "output_length": 20,
                        "chat_id": "s1",
                        "turn": 1,
                        "hash_ids": [1, 2],
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 5.0,
                        "input_length": 400,
                        "output_length": 30,
                        "chat_id": "s1",
                        "turn": 2,
                        "hash_ids": [1, 2, 3, 4],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    t2_task = _make_task(
        "deadbeef", trace_path="tier2_relaxed.jsonl", trace_format="bailian_trace", duration_seconds=20
    )
    t2_origin = 1_767_225_600.0
    t2_records = [
        # +10 token delta (within max(16, 0.08*200)=16) and +800ms schedule offset (within ±2000ms)
        ArtifactRecord(
            request_id="t2-0",
            arrived=t2_origin + 0.8,
            completed=t2_origin + 1.5,
            successful=True,
            ttft_ms=50.0,
            input_tokens=210.0,
            output_tokens=20.0,
        ),
        # +25 token delta (within int(0.08*400)=32) and +1200ms offset
        ArtifactRecord(
            request_id="t2-1",
            arrived=t2_origin + 6.2,
            completed=t2_origin + 7.0,
            successful=True,
            ttft_ms=25.0,
            input_tokens=425.0,
            output_tokens=30.0,
        ),
    ]
    t2_res = telemetry_post.correlate_trace_sessions(t2_task, t2_records, time_origin=t2_origin)
    assert t2_res["available"] is True
    assert t2_res["correlation"]["matched_count"] == 2
    assert t2_res["turn_distribution"] == [{"turns": 2, "sessions": 1}]


def test_review_edge_cases_uuid_fallback_sidecar_cached_and_failed_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify UUID sidecar Tier-2 fallback, sidecar in_diff guard, session_id=0 + cached_tokens in slot_lanes, and failed hash gating."""
    trace_root = tmp_path / "traces"
    trace_root.mkdir()
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: trace_root))

    # 1. UUID conversation_id sidecar not in trace -> falls through to Tier 2; failed first request does not warm seen_hashes
    uuid_trace = trace_root / "uuid_fallback.jsonl"
    uuid_trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 0.0,
                        "input_length": 200,
                        "output_length": 20,
                        "chat_id": "c1",
                        "turn": 1,
                        "hash_ids": [1, 2],
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 2.0,
                        "input_length": 300,
                        "output_length": 30,
                        "chat_id": "c1",
                        "turn": 2,
                        "hash_ids": [1, 2, 3, 4],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = _make_task("c0ffee01", trace_path="uuid_fallback.jsonl", trace_format="bailian_trace", duration_seconds=20)
    task = task.model_copy(
        update={
            "task_dir": str(tmp_path / "uuid_task"),
            "simulation": task.simulation.model_copy(update={"backend": "aiperf"}),
        }
    )
    aiperf_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    aiperf_dir.mkdir(parents=True)
    origin_ns = 1_767_225_600_000_000_000
    (aiperf_dir / "profile_export.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "metadata": {
                            "conversation_id": "uuid-not-in-trace-1",
                            "turn_index": 0,
                            "request_start_ns": origin_ns,
                        },
                        "metrics": {"input_sequence_length": {"value": 200}, "cached_tokens": {"value": 0}},
                    }
                ),
                json.dumps(
                    {
                        "metadata": {
                            "conversation_id": "uuid-not-in-trace-1",
                            "turn_index": 1,
                            "request_start_ns": origin_ns + 2_000_000_000,
                        },
                        "metrics": {"input_sequence_length": {"value": 300}, "cached_tokens": {"value": 240}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    records = [
        # First request failed (400/500) -> must NOT warm seen_hashes for the second request
        ArtifactRecord(
            request_id="u0",
            arrived=origin_ns / 1e9,
            completed=origin_ns / 1e9 + 0.5,
            successful=False,
            issue_failed=True,
            status_code=500,
            ttft_ms=10.0,
            input_tokens=200.0,
            output_tokens=0.0,
        ),
        ArtifactRecord(
            request_id="u1",
            arrived=origin_ns / 1e9 + 2.0,
            completed=origin_ns / 1e9 + 2.8,
            successful=True,
            ttft_ms=80.0,
            input_tokens=300.0,
            output_tokens=30.0,
        ),
    ]
    res = telemetry_post.correlate_trace_sessions(task, records, time_origin=origin_ns / 1e9)
    assert res["correlation"]["matched_count"] == 2
    assert res["theoretical_prefix_overlap_pct"] == 0.0  # failed request did not warm seen_hashes

    # 2. Sidecar timestamp alignment rejects mismatched input_tokens (> 1.0) and populates slot_lanes from sidecar cached_tokens
    sidecar_only_task = _make_task(
        "c0ffee02", trace_path="missing_trace.jsonl", trace_format="bailian_trace", duration_seconds=20
    )
    sidecar_only_task = sidecar_only_task.model_copy(
        update={
            "task_dir": str(tmp_path / "sidecar_task"),
            "simulation": sidecar_only_task.simulation.model_copy(update={"backend": "trace-replayer"}),
        }
    )
    tr_dir = Path(sidecar_only_task.task_dir) / "artifacts" / "trace-replayer"
    tr_dir.mkdir(parents=True)
    (tr_dir / "requests.jsonl").write_text(
        json.dumps({"s_time": 1000.0, "input_tokens": 500, "conversation_id": 0, "turn": 2, "cached_tokens": 400})
        + "\n",
        encoding="utf-8",
    )
    # Record r_wrong arrives at s_time 1000.5ms but has input_tokens=999 -> must NOT steal the sidecar row for input_tokens=500
    tr_epoch = 1_767_225_600.0
    r_anchor = ArtifactRecord(
        request_id=None,
        arrived=tr_epoch,
        completed=tr_epoch + 0.2,
        successful=True,
        input_tokens=100.0,
        output_tokens=10.0,
    )
    r_wrong = ArtifactRecord(
        request_id=None,
        arrived=tr_epoch + 1.0005,
        completed=tr_epoch + 1.5,
        successful=True,
        input_tokens=999.0,
        output_tokens=10.0,
    )
    r_right = ArtifactRecord(
        request_id=None,
        arrived=tr_epoch + 1.0,
        completed=tr_epoch + 1.5,
        successful=True,
        input_tokens=500.0,
        output_tokens=10.0,
    )
    meta_map = telemetry_post._extract_sidecar_request_metadata(sidecar_only_task, [r_anchor, r_wrong, r_right])
    assert 1 not in meta_map
    assert meta_map[2]["conversation_id"] == "0"
    assert meta_map[2]["cached_tokens"] == 400.0

    # Relative arrived records with stdout.log ISO basetime must still match relative s_time
    (tr_dir / "stdout.log").write_text("2026-01-01T00:00:00Z INFO started\n", encoding="utf-8")
    rel_rec = ArtifactRecord(
        request_id=None, arrived=1.0, completed=1.5, successful=True, input_tokens=500.0, output_tokens=10.0
    )
    rel_meta = telemetry_post._extract_sidecar_request_metadata(sidecar_only_task, [rel_rec])
    assert rel_meta[0]["conversation_id"] == "0"
    assert rel_meta[0]["cached_tokens"] == 400.0

    # Integer session_id=0 on duck-typed record + sidecar cached_tokens in slot_lanes
    duck_zero = SimpleNamespace(
        arrived=tr_epoch,
        completed=tr_epoch + 0.5,
        successful=True,
        status_code=200,
        error=None,
        request_id="dz0",
        input_tokens=500.0,
        output_tokens=20.0,
        requested_output_tokens=20.0,
        latency_ms=500.0,
        ttft_ms=50.0,
        tpot_ms=10.0,
        tpot_is_measured=True,
        s_time_drift_ms=0.0,
        error_category=None,
        rate_tokens=20.0,
        failed_issue=False,
        session_id=0,
        turn=1,
    )
    cs_sidecar = telemetry_post.build_client_series(sidecar_only_task, [duck_zero, r_right])
    assert [it["session_id"] for it in cs_sidecar["slot_lanes"]["items"]] == ["0", "0"]
    assert cs_sidecar["slot_lanes"]["items"][1]["cache_hit_pct"] == 80.0
    assert cs_sidecar["slot_lanes"]["items"][1]["tier"] == "warm"


@pytest.mark.asyncio
async def test_resolve_target_deployments_filters_standby_and_disabled_model_service_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.deploy import executions as deploy_execs
    from llm_d_lens.model_service import service as ms_service

    fake_ctxs = {
        "exec-active": SimpleNamespace(
            execution_id="exec-active",
            display_name="Active Member",
            namespace="ns-active",
            cluster_id="c-1",
            status="ready",
        ),
        "exec-standby": SimpleNamespace(
            execution_id="exec-standby",
            display_name="Standby Member",
            namespace="ns-standby",
            cluster_id="c-1",
            status="ready",
        ),
        "exec-disabled": SimpleNamespace(
            execution_id="exec-disabled",
            display_name="Disabled Member",
            namespace="ns-disabled",
            cluster_id="c-1",
            status="ready",
        ),
    }
    monkeypatch.setattr(deploy_execs, "find_execution_context", lambda eid: fake_ctxs.get(eid))
    monkeypatch.setattr(deploy_execs, "list_execution_contexts", lambda cluster_id="": [])

    class MixedMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="Group Mixed")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-active",
                    cluster_id="c-1",
                    target_namespace="ns-active",
                    target_service="svc-active",
                    status="active",
                    weight=100,
                ),
                SimpleNamespace(
                    execution_id="exec-standby",
                    cluster_id="c-1",
                    target_namespace="ns-standby",
                    target_service="svc-standby",
                    status="active",
                    weight=0,
                ),
                SimpleNamespace(
                    execution_id="exec-disabled",
                    cluster_id="c-1",
                    target_namespace="ns-disabled",
                    target_service="svc-disabled",
                    status="disabled",
                    weight=50,
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: MixedMS())
    task = _make_task(endpoint_mode="external", model_service_group_id="grp-mixed", endpoint_cluster_id="c-1")
    res = await telemetry_post.resolve_target_deployments(task)
    assert [d["execution_id"] for d in res] == ["exec-active"]

    # Fallback when all non-disabled members have weight=0 or weight=None -> keep active non-disabled members only
    class ZeroWeightMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="Group Zero")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-active",
                    cluster_id="c-1",
                    target_namespace="ns-active",
                    target_service="svc-active",
                    status="active",
                    weight=0,
                ),
                SimpleNamespace(
                    execution_id="exec-standby",
                    cluster_id="c-1",
                    target_namespace="ns-standby",
                    target_service="svc-standby",
                    status="active",
                    weight=None,
                ),
                SimpleNamespace(
                    execution_id="exec-disabled",
                    cluster_id="c-1",
                    target_namespace="ns-disabled",
                    target_service="svc-disabled",
                    enabled=False,
                    weight=100,
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: ZeroWeightMS())
    fallback_res = await telemetry_post.resolve_target_deployments(task)
    assert [d["execution_id"] for d in fallback_res] == ["exec-active", "exec-standby"]


@pytest.mark.asyncio
async def test_external_model_service_captures_and_refreshes_server_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.deploy import executions as deploy_execs
    from llm_d_lens.model_service import service as ms_service

    fake_ctx = SimpleNamespace(
        execution_id="exec-ms-1",
        display_name="MS Worker",
        namespace="ns-ms-1",
        cluster_id="c-ms",
        status="running",
    )
    monkeypatch.setattr(deploy_execs, "find_execution_context", lambda eid: fake_ctx if eid == "exec-ms-1" else None)
    monkeypatch.setattr(deploy_execs, "list_execution_contexts", lambda cluster_id="": [])

    class SingleMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="External Gateway Group")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-ms-1",
                    cluster_id="c-ms",
                    target_namespace="ns-ms-1",
                    target_service="svc-ms-1",
                    status="active",
                    weight=100,
                )
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: SingleMS())

    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_collect_benchmark(exec_id, start_iso, end_iso, cluster_id=None):
        calls.append((exec_id, start_iso, end_iso, cluster_id))
        return {
            "status": "available",
            "step_seconds": 5,
            "window": {"start": start_iso, "end": end_iso},
            "nodes": ["node-ms-1"],
            "namespace": "ns-ms-1",
            "series": [
                {"timestamp": start_iso, "gpu_utilization_percent": 72.0, "gpu_power_watts": 240.0},
                {"timestamp": end_iso, "gpu_utilization_percent": 76.0, "gpu_power_watts": 260.0},
            ],
            "summary": {
                "gpu_utilization_percent": {"mean": 74.0, "max": 76.0},
                "gpu_power_watts": {"mean": 250.0, "max": 260.0},
            },
            "per_pod": [{"pod": "vllm-ms-1", "role": "worker", "node": "node-ms-1"}],
        }

    monkeypatch.setattr(telemetry_post, "collect_benchmark_observability", fake_collect_benchmark)
    monkeypatch.setattr(telemetry_post, "build_grafana_links", lambda *_args, **_kwargs: {})

    task = _make_task(
        "e17a0001",
        endpoint_mode="external",
        model_service_group_id="grp-ext",
        endpoint_cluster_id="c-ms",
        duration_seconds=10,
    )
    origin = 1_767_225_600.0
    records = [
        ArtifactRecord(
            request_id="r-1",
            arrived=origin,
            completed=origin + 1.0,
            successful=True,
            ttft_ms=50.0,
            tpot_ms=10.0,
            input_tokens=100.0,
            output_tokens=20.0,
        ),
        ArtifactRecord(
            request_id="r-2",
            arrived=origin + 9.0,
            completed=origin + 10.0,
            successful=True,
            ttft_ms=60.0,
            tpot_ms=12.0,
            input_tokens=120.0,
            output_tokens=24.0,
        ),
    ]

    post_tel = await telemetry_post.collect_post_run_telemetry(task, records, time_origin=origin)
    assert post_tel["server_telemetry"]["status"] == "available"
    assert post_tel["server_telemetry"]["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(74.0)
    assert post_tel["capabilities"]["server_telemetry"] is True
    assert len(calls) == 1
    assert calls[0][0] == "exec-ms-1"
    assert calls[0][3] == "c-ms"

    # refresh_persisted_server_telemetry also re-queries Prometheus for external ModelService tasks
    calls.clear()
    stale_srv = {
        "schema_version": 2,
        "status": "available",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:10Z"},
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "gpu_utilization_percent": 10.0}],
        "summary": {"gpu_utilization_percent": {"mean": 10.0}},
    }
    refreshed = await telemetry_post.refresh_persisted_server_telemetry(task, stale_srv, post_tel["client_series"])
    assert refreshed["status"] == "available"
    assert refreshed["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(74.0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_capture_server_telemetry_merges_multi_namespace_model_service_and_skips_standby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.deploy import executions as deploy_execs
    from llm_d_lens.model_service import service as ms_service

    fake_ctxs = {
        "exec-a": SimpleNamespace(
            execution_id="exec-a", display_name="Member A", namespace="ns-a", cluster_id="c-1", status="ready"
        ),
        "exec-b": SimpleNamespace(
            execution_id="exec-b", display_name="Member B", namespace="ns-b", cluster_id="c-1", status="ready"
        ),
        "exec-standby": SimpleNamespace(
            execution_id="exec-standby",
            display_name="Standby",
            namespace="ns-standby",
            cluster_id="c-1",
            status="ready",
        ),
    }
    monkeypatch.setattr(deploy_execs, "find_execution_context", lambda eid: fake_ctxs.get(eid))
    monkeypatch.setattr(deploy_execs, "list_execution_contexts", lambda cluster_id="": [])

    class MultiNamespaceMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="Canary MS")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-a",
                    cluster_id="c-1",
                    target_namespace="ns-a",
                    target_service="svc-a",
                    status="active",
                    weight=70,
                ),
                SimpleNamespace(
                    execution_id="exec-b",
                    cluster_id="c-1",
                    target_namespace="ns-b",
                    target_service="svc-b",
                    status="active",
                    weight=30,
                ),
                SimpleNamespace(
                    execution_id="exec-standby",
                    cluster_id="c-1",
                    target_namespace="ns-standby",
                    target_service="svc-standby",
                    status="active",
                    weight=0,
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: MultiNamespaceMS())

    queried_execs: list[str] = []

    async def fake_collect_benchmark(exec_id, start_iso, end_iso, cluster_id=None):
        queried_execs.append(exec_id)
        if exec_id == "exec-a":
            return {
                "status": "available",
                "step_seconds": 5,
                "window": {"start": start_iso, "end": end_iso},
                "namespace": "ns-a",
                "nodes": ["node-1"],
                "series": [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "gpu_utilization_percent": 80.0,
                        "gpu_power_watts": 300.0,
                        "request_rate_rps": 6.0,
                    },
                    {
                        "timestamp": "2026-01-01T00:00:05Z",
                        "gpu_utilization_percent": 90.0,
                        "gpu_power_watts": 340.0,
                        "request_rate_rps": 8.0,
                    },
                ],
                "summary": {
                    "gpu_utilization_percent": {"mean": 85.0, "max": 90.0},
                    "gpu_power_watts": {"mean": 320.0, "max": 340.0},
                    "request_rate_rps": {"mean": 7.0, "max": 8.0},
                },
                "per_pod": [{"pod": "vllm-a-0", "role": "worker", "node": "node-1"}],
                "pods": ["vllm-a-0"],
                "warnings": ["warn-a"],
            }
        if exec_id == "exec-b":
            return {
                "status": "available",
                "step_seconds": 5,
                "window": {"start": start_iso, "end": end_iso},
                "namespace": "ns-b",
                "nodes": ["node-2"],
                "series": [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "gpu_utilization_percent": 40.0,
                        "gpu_power_watts": 200.0,
                        "request_rate_rps": 4.0,
                    },
                    {
                        "timestamp": "2026-01-01T00:00:05Z",
                        "gpu_utilization_percent": 50.0,
                        "gpu_power_watts": 220.0,
                        "request_rate_rps": 2.0,
                    },
                ],
                "summary": {
                    "gpu_utilization_percent": {"mean": 45.0, "max": 50.0},
                    "gpu_power_watts": {"mean": 210.0, "max": 220.0},
                    "request_rate_rps": {"mean": 3.0, "max": 4.0},
                },
                "per_pod": [{"pod": "vllm-b-0", "role": "worker", "node": "node-2"}],
                "pods": ["vllm-b-0"],
                "warnings": ["warn-b"],
            }
        raise AssertionError(f"Standby deployment {exec_id} must not be queried")

    monkeypatch.setattr(telemetry_post, "collect_benchmark_observability", fake_collect_benchmark)
    monkeypatch.setattr(telemetry_post, "build_grafana_links", lambda *_args, **_kwargs: {})

    task = _make_task(
        "e17a0002",
        endpoint_mode="external",
        model_service_group_id="grp-canary",
        endpoint_cluster_id="c-1",
        duration_seconds=10,
    )
    resolved = await telemetry_post.resolve_target_deployments(task)
    assert [d["execution_id"] for d in resolved] == ["exec-a", "exec-b"]

    merged = await telemetry_post.capture_server_telemetry(
        task,
        start_iso="2026-01-01T00:00:00Z",
        end_iso="2026-01-01T00:00:10Z",
        target_deployments=resolved,
    )
    assert sorted(queried_execs) == ["exec-a", "exec-b"]
    assert merged["status"] == "available"
    assert len(merged["series"]) == 2
    assert merged["series"][0]["gpu_utilization_percent"] == pytest.approx(60.0)
    assert merged["series"][1]["gpu_utilization_percent"] == pytest.approx(70.0)
    assert merged["series"][0]["gpu_power_watts"] == pytest.approx(500.0)
    assert merged["series"][1]["gpu_power_watts"] == pytest.approx(560.0)
    assert merged["series"][0]["request_rate_rps"] == pytest.approx(10.0)
    assert merged["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(65.0)
    assert merged["summary"]["gpu_utilization_percent"]["min"] == pytest.approx(60.0)
    assert merged["summary"]["gpu_utilization_percent"]["max"] == pytest.approx(70.0)
    assert merged["summary"]["gpu_power_watts"]["mean"] == pytest.approx(530.0)
    assert merged["summary"]["gpu_power_watts"]["max"] == pytest.approx(560.0)
    assert [p["pod"] for p in merged["per_pod"]] == ["vllm-a-0", "vllm-b-0"]
    assert merged["pods"] == ["vllm-a-0", "vllm-b-0"]
    assert merged["nodes"] == ["node-1", "node-2"]
    assert merged["warnings"] == ["warn-a", "warn-b"]

    # Weighted GPU utilization by card count (2 cards @ 80% in ns-a, 1 card @ 20% in ns-b -> 60%)
    # and end-to-end collect_post_run_telemetry integration.
    async def fake_collect_weighted(exec_id, start_iso, end_iso, cluster_id=None):
        if exec_id == "exec-a":
            return {
                "status": "available",
                "step_seconds": 5,
                "window": {"start": start_iso, "end": end_iso},
                "namespace": "ns-a",
                "nodes": ["node-1"],
                "hardware_profiles": [{"id": "prof", "allocated_accelerators": 2, "models": ["Accel-A"]}],
                "series": [
                    {
                        "timestamp": start_iso,
                        "gpu_utilization_percent": 80.0,
                        "gpu_power_watts": 400.0,
                        "gpu_framebuffer_used_bytes": 32e9,
                        "gpu_memory_total_bytes": 64e9,
                    }
                ],
                "summary": {"gpu_utilization_percent": {"mean": 80.0}},
                "per_pod": [{"pod": "vllm-a-0", "role": "worker", "node": "node-1"}],
            }
        if exec_id == "exec-b":
            return {
                "status": "available",
                "step_seconds": 5,
                "window": {"start": start_iso, "end": end_iso},
                "namespace": "ns-b",
                "nodes": ["node-2"],
                "hardware_profiles": [{"id": "prof", "allocated_accelerators": 1, "models": ["Accel-A"]}],
                "series": [
                    {
                        "timestamp": start_iso,
                        "gpu_utilization_percent": 20.0,
                        "gpu_power_watts": 150.0,
                        "gpu_framebuffer_used_bytes": 16e9,
                        "gpu_memory_total_bytes": 32e9,
                    }
                ],
                "summary": {"gpu_utilization_percent": {"mean": 20.0}},
                "per_pod": [{"pod": "vllm-b-0", "role": "worker", "node": "node-2"}],
            }
        raise AssertionError(f"Unexpected exec_id {exec_id}")

    monkeypatch.setattr(telemetry_post, "collect_benchmark_observability", fake_collect_weighted)
    origin = 1_767_225_600.0
    records = [
        ArtifactRecord(
            request_id="w1",
            arrived=origin,
            completed=origin + 1.0,
            successful=True,
            input_tokens=10.0,
            output_tokens=5.0,
        ),
        ArtifactRecord(
            request_id="w2",
            arrived=origin + 4.0,
            completed=origin + 5.0,
            successful=True,
            input_tokens=10.0,
            output_tokens=5.0,
        ),
    ]
    post = await telemetry_post.collect_post_run_telemetry(task, records, time_origin=origin)
    srv = post["server_telemetry"]
    assert srv["status"] == "available"
    assert srv["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(60.0)
    assert srv["summary"]["gpu_power_watts"]["mean"] == pytest.approx(550.0)
    assert srv["summary"]["gpu_framebuffer_used_bytes"]["mean"] == pytest.approx(48e9)
    assert srv["summary"]["gpu_memory_total_bytes"]["mean"] == pytest.approx(96e9)
    assert [d["execution_id"] for d in post["target_deployments"]] == ["exec-a", "exec-b"]


@pytest.mark.asyncio
async def test_resolve_target_deployments_edge_weights_unhealthy_and_all_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.deploy import executions as deploy_execs
    from llm_d_lens.model_service import service as ms_service

    monkeypatch.setattr(deploy_execs, "find_execution_context", lambda _eid: None)
    monkeypatch.setattr(deploy_execs, "list_execution_contexts", lambda cluster_id="": [])

    class EdgeWeightMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="Edge Weights")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-bool",
                    cluster_id="c-1",
                    target_namespace="ns-bool",
                    target_service="svc-bool",
                    status="active",
                    weight=True,
                ),
                SimpleNamespace(
                    execution_id="exec-neg",
                    cluster_id="c-1",
                    target_namespace="ns-neg",
                    target_service="svc-neg",
                    status="active",
                    weight=-5,
                ),
                SimpleNamespace(
                    execution_id="exec-tw",
                    cluster_id="c-1",
                    target_namespace="ns-tw",
                    target_service="svc-tw",
                    status="active",
                    traffic_weight=40,
                ),
                SimpleNamespace(
                    execution_id="exec-unhealthy",
                    cluster_id="c-1",
                    target_namespace="ns-unhealthy",
                    target_service="svc-unhealthy",
                    status="unhealthy",
                    weight=60,
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: EdgeWeightMS())
    task = _make_task(endpoint_mode="external", model_service_group_id="grp-edge", endpoint_cluster_id="c-1")
    res = await telemetry_post.resolve_target_deployments(task)
    # bool weight=True and negative weight=-5 are excluded when positive traffic_weight / weight > 0 exist;
    # unhealthy with positive weight is kept.
    assert [d["execution_id"] for d in res] == ["exec-tw", "exec-unhealthy"]

    class AllDisabledMS:
        def get_group(self, gid):
            return SimpleNamespace(id=gid, name="All Disabled")

        def list_members(self, _gid):
            return [
                SimpleNamespace(
                    execution_id="exec-dis-1",
                    cluster_id="c-1",
                    target_namespace="ns-1",
                    target_service="svc-1",
                    status="disabled",
                    weight=50,
                ),
                SimpleNamespace(
                    execution_id="exec-dis-2",
                    cluster_id="c-1",
                    target_namespace="ns-2",
                    target_service="svc-2",
                    enabled=False,
                    weight=50,
                ),
            ]

    monkeypatch.setattr(ms_service, "default_service", lambda: AllDisabledMS())
    fallback_res = await telemetry_post.resolve_target_deployments(task)
    assert [d["execution_id"] for d in fallback_res] == ["exec-dis-1", "exec-dis-2"]


@pytest.mark.asyncio
async def test_pure_external_url_task_skips_prometheus_in_post_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_spy = AsyncMock(side_effect=AssertionError("capture_server_telemetry must not be called"))
    monkeypatch.setattr(telemetry_post, "capture_server_telemetry", capture_spy)
    monkeypatch.setattr(telemetry_post, "build_grafana_links", lambda *_args, **_kwargs: {})

    pure_external_task = _make_task(
        "e17a0003",
        endpoint_mode="external",
        endpoint_cluster_id="c-1",
        endpoint_namespace="ns-should-not-query",
        model_service_group_id=None,
        endpoint_deployment_execution_id=None,
        duration_seconds=10,
    )
    origin = 1_767_225_600.0
    records = [
        ArtifactRecord(
            request_id="ext-1",
            arrived=origin,
            completed=origin + 1.0,
            successful=True,
            input_tokens=32.0,
            output_tokens=16.0,
        )
    ]
    post = await telemetry_post.collect_post_run_telemetry(pure_external_task, records, time_origin=origin)
    assert post["target_deployments"] == []
    assert post["server_telemetry"]["status"] == "unavailable"
    assert capture_spy.await_count == 0

    saved_srv = {
        "schema_version": 2,
        "status": "unavailable",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:10Z"},
        "series": [],
        "summary": {},
    }
    refreshed = await telemetry_post.refresh_persisted_server_telemetry(
        pure_external_task, saved_srv, post["client_series"]
    )
    assert refreshed["status"] == "unavailable"
    assert capture_spy.await_count == 0


@pytest.mark.asyncio
async def test_merge_observability_shared_and_partial_overlap_nodes_mixed_weights_and_partial_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ts0 = "2026-01-01T00:00:00Z"
    # 1. Partial node overlap: ns-a on [smc-18], ns-b on [smc-18, smc-19].
    # Host RAM/CPU must be deduplicated on smc-18 while GPU power is summed and GPU util is card-weighted.
    snap_a = {
        "status": "available",
        "step_seconds": 5,
        "window": {"start": ts0, "end": "2026-01-01T00:00:10Z"},
        "namespace": "ns-a",
        "nodes": ["smc-18"],
        "hardware_profiles": [{"id": "prof", "allocated_accelerators": 2, "models": ["Accel-A"]}],
        "series": [
            {
                "timestamp": ts0,
                "gpu_utilization_percent": 80.0,
                "gpu_power_watts": 300.0,
                "host_cpu_usage_percent": 40.0,
                "host_memory_total_bytes": 256.0,
                "host_memory_used_bytes": 100.0,
                "host_memory_available_bytes": 156.0,
                "host_memory_usage_percent": 39.0625,
                "host_power_watts": 500.0,
            }
        ],
        "summary": {},
        "per_pod": [
            {"pod": "vllm-a-0", "role": "worker", "node": "smc-18"},
            {"pod": "vllm-a-1", "role": "worker", "node": "smc-18"},
        ],
        "per_node": [
            {
                "node": "smc-18",
                "roles": ["worker"],
                "pod_count": 2,
                "pods": ["vllm-a-0", "vllm-a-1"],
                "allocated_accelerators": 2,
                "memory_total_bytes": 256.0,
                "filesystem_avail_bytes": 1000.0,
                "filesystem_size_bytes": 2000.0,
                "cpu_usage_percent": {"mean": 40.0, "max": 40.0},
                "gpu_utilization_percent": {"mean": 80.0, "max": 80.0},
                "gpu_power_watts": {"mean": 300.0, "max": 300.0},
            }
        ],
        "per_node_series": {
            "smc-18": [
                {
                    "timestamp": ts0,
                    "cpu_usage_percent": 40.0,
                    "memory_used_bytes": 100.0,
                    "memory_available_bytes": 156.0,
                    "memory_usage_percent": 39.0625,
                    "host_power_watts": 500.0,
                    "gpu_power_watts": 300.0,
                    "gpu_utilization_percent": 80.0,
                }
            ]
        },
        "per_endpoint": [{"endpoint": "vllm-svc", "inflight_token_load": {"mean": 10.0}}],
    }
    snap_b = {
        "status": "available",
        "step_seconds": 5,
        "window": {"start": ts0, "end": "2026-01-01T00:00:10Z"},
        "namespace": "ns-b",
        "nodes": ["smc-18", "smc-19"],
        "hardware_profiles": [{"id": "prof", "allocated_accelerators": 2, "models": ["Accel-A"]}],
        "series": [
            {
                "timestamp": ts0,
                "gpu_utilization_percent": 40.0,
                "gpu_power_watts": 250.0,
                "host_cpu_usage_percent": 50.0,
                "host_memory_total_bytes": 512.0,
                "host_memory_used_bytes": 200.0,
                "host_memory_available_bytes": 312.0,
                "host_memory_usage_percent": 39.0625,
                "host_power_watts": 900.0,
            }
        ],
        "summary": {},
        "per_pod": [{"pod": "vllm-b-0", "role": "worker", "node": "smc-19"}],
        "per_node": [
            {
                "node": "smc-18",
                "roles": ["worker"],
                "pod_count": 1,
                "pods": ["vllm-b-18"],
                "allocated_accelerators": 1,
                "memory_total_bytes": 256.0,
                "filesystem_avail_bytes": 1000.0,
                "filesystem_size_bytes": 2000.0,
                "cpu_usage_percent": {"mean": 40.0, "max": 40.0},
                "gpu_utilization_percent": {"mean": 50.0, "max": 50.0},
                "gpu_power_watts": {"mean": 150.0, "max": 150.0},
            },
            {
                "node": "smc-19",
                "roles": ["worker"],
                "pod_count": 1,
                "pods": ["vllm-b-0"],
                "allocated_accelerators": 1,
                "memory_total_bytes": 256.0,
                "filesystem_avail_bytes": 1500.0,
                "filesystem_size_bytes": 2000.0,
                "cpu_usage_percent": {"mean": 60.0, "max": 60.0},
                "gpu_utilization_percent": {"mean": 30.0, "max": 30.0},
                "gpu_power_watts": {"mean": 100.0, "max": 100.0},
            },
        ],
        "per_node_series": {
            "smc-18": [
                {
                    "timestamp": ts0,
                    "cpu_usage_percent": 40.0,
                    "memory_used_bytes": 100.0,
                    "memory_available_bytes": 156.0,
                    "memory_usage_percent": 39.0625,
                    "host_power_watts": 500.0,
                    "gpu_power_watts": 150.0,
                    "gpu_utilization_percent": 50.0,
                }
            ],
            "smc-19": [
                {
                    "timestamp": ts0,
                    "cpu_usage_percent": 60.0,
                    "memory_used_bytes": 100.0,
                    "memory_available_bytes": 156.0,
                    "memory_usage_percent": 39.0625,
                    "host_power_watts": 400.0,
                    "gpu_power_watts": 100.0,
                    "gpu_utilization_percent": 30.0,
                }
            ],
        },
        "per_endpoint": [{"endpoint": "vllm-svc", "inflight_token_load": {"mean": 20.0}}],
    }

    merged = telemetry_post._merge_observability_snapshots(
        [snap_a, snap_b],
        start_iso=ts0,
        end_iso="2026-01-01T00:00:10Z",
    )
    pt0 = merged["series"][0]
    assert pt0["host_memory_total_bytes"] == pytest.approx(512.0)
    assert pt0["host_memory_used_bytes"] == pytest.approx(200.0)
    assert pt0["host_memory_available_bytes"] == pytest.approx(312.0)
    assert pt0["host_power_watts"] == pytest.approx(900.0)
    assert pt0["host_cpu_usage_percent"] == pytest.approx(50.0)
    assert pt0["host_memory_usage_percent"] == pytest.approx(200.0 / 512.0 * 100.0)
    assert pt0["gpu_power_watts"] == pytest.approx(550.0)
    assert pt0["gpu_utilization_percent"] == pytest.approx(60.0)

    # per_node and per_node_series for shared node smc-18 sum GPU power and card-weight GPU util (2@80 + 1@50 -> 70)
    smc18_node = next(n for n in merged["per_node"] if n["node"] == "smc-18")
    assert smc18_node["allocated_accelerators"] == 3
    assert smc18_node["gpu_power_watts"]["mean"] == pytest.approx(450.0)
    assert smc18_node["gpu_utilization_percent"]["mean"] == pytest.approx(70.0)
    smc18_series = merged["per_node_series"]["smc-18"][0]
    assert smc18_series["host_power_watts"] == pytest.approx(500.0)
    assert smc18_series["gpu_power_watts"] == pytest.approx(450.0)
    assert smc18_series["gpu_utilization_percent"] == pytest.approx(70.0)
    # Same-named endpoints across different namespaces are both kept with namespace tags
    assert [(e["namespace"], e["endpoint"]) for e in merged["per_endpoint"]] == [
        ("ns-a", "vllm-svc"),
        ("ns-b", "vllm-svc"),
    ]

    # 2. Mixed card-weight metadata fallback: snap_with_cards (4 cards, 1 pod) vs snap_no_cards (no cards, 1 pod)
    # Must fall back to pod weights (1 vs 1 -> mean 60.0), NOT mix 4 cards with 1 pod (72.0).
    snap_with_cards = {
        "status": "available",
        "namespace": "ns-a",
        "hardware_profiles": [{"id": "prof", "allocated_accelerators": 4}],
        "per_pod": [{"pod": "vllm-a-0", "role": "worker"}],
        "series": [{"timestamp": ts0, "gpu_utilization_percent": 80.0}],
    }
    snap_no_cards = {
        "status": "available",
        "namespace": "ns-b",
        "per_pod": [{"pod": "vllm-b-0", "role": "worker"}],
        "series": [{"timestamp": ts0, "gpu_utilization_percent": 40.0}],
    }
    mixed_merged = telemetry_post._merge_observability_snapshots(
        [snap_with_cards, snap_no_cards],
        start_iso=ts0,
        end_iso="2026-01-01T00:00:10Z",
    )
    assert mixed_merged["series"][0]["gpu_utilization_percent"] == pytest.approx(60.0)

    # 3. Partial failure: 1 of 2 namespaces times out / returns unavailable while other succeeds
    task = _make_task("e17a0004", endpoint_mode="external", endpoint_cluster_id="c-1")
    targets = [
        {"execution_id": "exec-ok", "namespace": "ns-ok", "cluster_id": "c-1"},
        {"execution_id": "exec-timeout", "namespace": "ns-timeout", "cluster_id": "c-1"},
    ]

    async def partial_timeout_collect(exec_id, start_iso, end_iso, cluster_id=None):
        if exec_id == "exec-ok":
            return {
                "status": "available",
                "namespace": "ns-ok",
                "window": {"start": start_iso, "end": end_iso},
                "series": [{"timestamp": start_iso, "gpu_utilization_percent": 75.0}],
                "summary": {"gpu_utilization_percent": {"mean": 75.0}},
            }
        raise TimeoutError("timed out")

    monkeypatch.setattr(telemetry_post, "collect_benchmark_observability", partial_timeout_collect)
    res_partial = await telemetry_post.capture_server_telemetry(
        task,
        start_iso=ts0,
        end_iso="2026-01-01T00:00:10Z",
        target_deployments=targets,
    )
    assert res_partial["status"] == "available"
    assert res_partial["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(75.0)
    assert any("ns-timeout" in w and "timed out" in w for w in res_partial["warnings"])

    async def partial_unavail_collect(exec_id, start_iso, end_iso, cluster_id=None):
        if exec_id == "exec-ok":
            return {
                "status": "available",
                "namespace": "ns-ok",
                "window": {"start": start_iso, "end": end_iso},
                "series": [{"timestamp": start_iso, "gpu_utilization_percent": 65.0}],
                "summary": {"gpu_utilization_percent": {"mean": 65.0}},
            }
        return {
            "status": "unavailable",
            "namespace": "ns-unavail",
            "reason": "Prometheus unreachable Bearer secret-xyz",
            "series": [],
            "summary": {},
        }

    monkeypatch.setattr(telemetry_post, "collect_benchmark_observability", partial_unavail_collect)
    res_unavail_partial = await telemetry_post.capture_server_telemetry(
        task,
        start_iso=ts0,
        end_iso="2026-01-01T00:00:10Z",
        target_deployments=targets,
    )
    assert res_unavail_partial["status"] == "available"
    assert res_unavail_partial["summary"]["gpu_utilization_percent"]["mean"] == pytest.approx(65.0)
    assert any("ns-unavail" in w and "secret-xyz" not in w for w in res_unavail_partial["warnings"])


def test_sanitize_and_merge_role_separated_observability_snapshots() -> None:
    # 1. sanitize_server_telemetry clamps summary_by_role and drops prefill on aggregated downgrade
    raw_agg = {
        "schema_version": 8,
        "status": "available",
        "topology_mode": "aggregated",
        "summary": {"vllm_decode_time_p50_ms": {"mean": 400.0}},
        "summary_by_role": {
            "prefill": {"vllm_prefill_time_p50_ms": {"mean": 100.0}},
            "decode": {
                "kv_cache_usage_percent": {"mean": -5.0, "max": 105.0},
                "vllm_decode_time_p50_ms": {"p50": 400.0, "p99": float("nan")},
                "waiting_requests": {"mean": 0.0, "max": 2.0},
            },
        },
        "series": [],
    }
    cleaned = telemetry_post.sanitize_server_telemetry(raw_agg)
    assert cleaned is not None
    assert "prefill" not in cleaned["summary_by_role"]
    assert cleaned["summary_by_role"]["decode"]["kv_cache_usage_percent"]["mean"] == 0.0
    assert cleaned["summary_by_role"]["decode"]["kv_cache_usage_percent"]["max"] == 100.0
    assert cleaned["summary_by_role"]["decode"]["waiting_requests"]["mean"] == 0.0

    # 2. _merge_observability_snapshots merges role_series, summary_by_role, derived.role_summary, metric_sources
    ts0 = "2026-01-01T00:00:00Z"
    snap_p = {
        "status": "available",
        "namespace": "ns-p",
        "topology_mode": "disaggregated",
        "window": {"start": ts0, "end": "2026-01-01T00:01:00Z"},
        "step_seconds": 5,
        "series": [{"timestamp": ts0, "vllm_prefill_time_p50_ms": 220.0}],
        "summary": {"vllm_prefill_time_p50_ms": {"mean": 220.0}},
        "role_series": [{"timestamp": ts0, "prefill_vllm_prefill_time_p50_ms": 220.0, "prefill_waiting_requests": 0.0}],
        "summary_by_role": {
            "prefill": {
                "vllm_prefill_time_p50_ms": {"mean": 220.0, "p50": 220.0},
                "waiting_requests": {"mean": 0.0, "max": 0.0},
            }
        },
        "derived": {"role_summary": {"prefill": {"vllm_prefill_time_p50_ms": 220.0}}},
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
    }
    snap_d = {
        "status": "available",
        "namespace": "ns-d",
        "topology_mode": "disaggregated",
        "window": {"start": ts0, "end": "2026-01-01T00:01:00Z"},
        "step_seconds": 5,
        "series": [{"timestamp": ts0, "vllm_decode_time_p50_ms": 3500.0}],
        "summary": {"vllm_decode_time_p50_ms": {"mean": 3500.0}},
        "role_series": [{"timestamp": ts0, "decode_vllm_decode_time_p50_ms": 3500.0, "decode_waiting_requests": 3.0}],
        "summary_by_role": {
            "decode": {
                "vllm_decode_time_p50_ms": {"mean": 3500.0, "p50": 3500.0},
                "waiting_requests": {"mean": 3.0, "max": 3.0},
            }
        },
        "derived": {"role_summary": {"decode": {"vllm_decode_time_p50_ms": 3500.0}}},
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
    }
    merged = telemetry_post._merge_observability_snapshots(
        [snap_p, snap_d], start_iso=ts0, end_iso="2026-01-01T00:01:00Z"
    )
    assert merged["metric_sources"]["vllm_stage_latencies"] == "role_separated"
    assert merged["summary_by_role"]["prefill"]["vllm_prefill_time_p50_ms"]["mean"] == pytest.approx(220.0)
    assert merged["summary_by_role"]["decode"]["vllm_decode_time_p50_ms"]["mean"] == pytest.approx(3500.0)
    assert merged["derived"]["role_summary"]["prefill"]["vllm_prefill_time_p50_ms"] == pytest.approx(220.0)
    assert merged["derived"]["role_summary"]["decode"]["vllm_decode_time_p50_ms"] == pytest.approx(3500.0)
    assert len(merged["role_series"]) == 1
    assert merged["role_series"][0]["prefill_vllm_prefill_time_p50_ms"] == pytest.approx(220.0)
    assert merged["role_series"][0]["decode_vllm_decode_time_p50_ms"] == pytest.approx(3500.0)


def test_forensics_role_separated_pd_zero_nixl_double_count_mean_slice_and_10s_guard() -> None:
    task = _make_task("a1b2c3d4")
    client = _base_forensics_client(ttft_p90=14000.0, tpot_p90=40.0, e2e_p90=22000.0)
    client["windows"]["full"]["ttft_ms"] = {"mean": 2100.0, "p50": 1800.0, "p90": 14000.0}
    client["windows"]["full"]["e2e_ms"] = {"mean": 10500.0, "p50": 9200.0, "p90": 22000.0}

    srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "metric_sources": {"vllm_stage_latencies": "role_separated"},
        "router": {
            "scheduler_latency_p50_ms": {"mean": 1.0},
            "scheduler_latency_p90_ms": {"mean": 3.0},
            "scheduler_latency_mean_ms": {"mean": 1.5},
        },
        "summary": {
            "queue_depth": {"mean": 0.5, "max": 2.0},
            "kv_cache_usage_percent": {"mean": 45.0, "max": 55.0},
            "vllm_queue_time_p50_ms": {"mean": 1200.0},
            "vllm_queue_time_p90_ms": {"mean": 2500.0},
            "vllm_queue_time_mean_ms": {"mean": 1300.0},
            "vllm_prefill_time_p50_ms": {"mean": 450.0},
            "vllm_prefill_time_p90_ms": {"mean": 700.0},
            "vllm_prefill_time_mean_ms": {"mean": 500.0},
            "vllm_decode_time_p50_ms": {"mean": 7400.0},
            "vllm_decode_time_p90_ms": {"mean": 11000.0},
            "vllm_decode_time_mean_ms": {"mean": 8400.0},
            "nixl_transfer_latency_p50_ms": {"mean": 1150.0},
            "nixl_transfer_latency_p90_ms": {"mean": 2400.0},
            "nixl_transfer_latency_mean_ms": {"mean": 1200.0},
            # p90 hits 10s vLLM bucket ceiling (>= 9999.0), while mean is exact _sum/_count
            "vllm_ttft_p50_ms": {"mean": 1700.0},
            "vllm_ttft_p90_ms": {"mean": 10000.0},
            "vllm_ttft_mean_ms": {"mean": 1950.0},
        },
        "summary_by_role": {
            "prefill": {
                "vllm_queue_time_p50_ms": {"mean": 150.0},
                "vllm_queue_time_p90_ms": {"mean": 270.0},
                "vllm_queue_time_mean_ms": {"mean": 2.0},
                "vllm_prefill_time_p50_ms": {"mean": 450.0},
                "vllm_prefill_time_p90_ms": {"mean": 700.0},
                "vllm_prefill_time_mean_ms": {"mean": 500.0},
                "vllm_ttft_p90_ms": {"mean": 700.0},
                "waiting_requests": {"mean": 0.0, "max": 0.0},
                "running_requests": {"mean": 2.0, "max": 4.0},
            },
            "decode": {
                "vllm_queue_time_p50_ms": {"mean": 1200.0},
                "vllm_queue_time_p90_ms": {"mean": 2500.0},
                "vllm_queue_time_mean_ms": {"mean": 1300.0},
                "vllm_decode_time_p50_ms": {"mean": 7400.0},
                "vllm_decode_time_p90_ms": {"mean": 11000.0},
                "vllm_decode_time_mean_ms": {"mean": 8400.0},
                "vllm_ttft_p90_ms": {"mean": 10000.0},
                "waiting_requests": {"mean": 1.5, "max": 3.0},
                "running_requests": {"mean": 8.0, "max": 12.0},
            },
        },
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "router_scheduler_latency_p50_ms": 1.0,
                "router_scheduler_latency_p90_ms": 3.0,
                "vllm_queue_time_p50_ms": 1200.0,
                "vllm_prefill_time_p50_ms": 450.0,
                "vllm_decode_time_p50_ms": 7400.0,
                "vllm_queue_time_p90_ms": 2500.0,
                "vllm_prefill_time_p90_ms": 700.0,
                "vllm_decode_time_p90_ms": 11000.0,
                "nixl_transfer_latency_p50_ms": 1150.0,
                "nixl_transfer_latency_p90_ms": 2400.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 45.0,
            },
            {
                "timestamp": "1970-01-01T00:17:05Z",
                "router_scheduler_latency_p50_ms": 1.0,
                "router_scheduler_latency_p90_ms": 3.0,
                "vllm_queue_time_p50_ms": 1200.0,
                "vllm_prefill_time_p50_ms": 450.0,
                "vllm_decode_time_p50_ms": 7400.0,
                # NIXL spike only (2400 -> 2400ms NIXL, 2500 -> 2500ms queue): net queue = 100ms, NOT peak shift!
                "vllm_queue_time_p90_ms": 2500.0,
                "vllm_prefill_time_p90_ms": 700.0,
                "vllm_decode_time_p90_ms": 11000.0,
                "nixl_transfer_latency_p50_ms": 1150.0,
                "nixl_transfer_latency_p90_ms": 2400.0,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 45.0,
            },
        ],
    }

    res = analyze_forensics(task, client_series=client, server_telemetry=srv)
    slb = res["stage_latency_breakdown"]
    assert slb["source"] == "server_telemetry"

    # Zero NIXL double-counting across p50, p90, mean
    p50 = slb["p50"]
    assert p50["kv_transfer_ms"] == pytest.approx(1150.0)
    assert p50["queue_time_ms"] == pytest.approx(52.0)  # prefill_eff(2) + (1200 - 1150)
    assert p50["prefill_time_ms"] == pytest.approx(450.0)
    # server_ttft = 1700.0; engine_sum = 52 + 450 + 1150 = 1652 -> engine_overhead = 48.0
    assert p50["engine_overhead_ms"] == pytest.approx(48.0)
    # client_ttft = 1800.0; gateway_overhead = 1800 - (1.0 + 1700.0) = 99.0
    assert p50["gateway_overhead_ms"] == pytest.approx(99.0)
    assert p50["reconciliation"]["quantile_non_additive"] is True
    assert p50["reconciliation"]["client_e2e_ms"] == pytest.approx(9200.0)

    # p90 hits 10s TTFT ceiling -> server_ttft_clamped=True, gateway_overhead_ms=None, whereas mean is NOT clamped!
    p90 = slb["p90"]
    assert p90["kv_transfer_ms"] == pytest.approx(2400.0)
    assert p90["queue_time_ms"] == pytest.approx(102.0)  # prefill_eff(2) + (2500 - 2400)
    assert p90["server_ttft_clamped"] is True
    assert p90["gateway_overhead_ms"] is None
    assert p90["caveat"] is not None

    mean_sl = slb["mean"]
    assert mean_sl["available"] is True
    assert mean_sl["source"] == "server_telemetry"
    assert mean_sl["server_ttft_clamped"] is False
    assert mean_sl["kv_transfer_ms"] == pytest.approx(1200.0)
    assert mean_sl["queue_time_ms"] == pytest.approx(102.0)  # prefill(2) + (1300 - 1200)
    assert mean_sl["prefill_time_ms"] == pytest.approx(500.0)
    assert mean_sl["engine_overhead_ms"] == pytest.approx(148.0)  # 1950 - (102 + 500 + 1200)
    assert mean_sl["gateway_overhead_ms"] == pytest.approx(148.5)  # 2100 - (1.5 + 1950.0)

    # Per-timestamp series subtracts NIXL from queue without double-counting
    assert slb["series"][0]["queue_ms"] == pytest.approx(50.0)
    assert slb["series"][0]["kv_transfer_ms"] == pytest.approx(1150.0)
    assert slb["series"][0]["queue_p90_ms"] == pytest.approx(100.0)
    assert slb["series"][0]["kv_transfer_p90_ms"] == pytest.approx(2400.0)
    assert slb["peak_window_shift"]["detected"] is False

    # Rule 4 uses NIXL-subtracted queue (100ms), not raw 2500ms
    r4 = next(r for r in res["rules_evaluated"] if r["code"] == "queue_bound")
    assert r4["triggered"] is False

    # role_breakdown preserves waiting_mean == 0.0, running_*, queue_raw_*, queue_excl_kv_*
    rb = slb["role_breakdown"]
    assert rb["prefill"]["waiting_mean"] == 0.0
    assert rb["prefill"]["running_mean"] == pytest.approx(2.0)
    assert rb["decode"]["queue_raw_p50_ms"] == pytest.approx(1200.0)
    assert rb["decode"]["queue_excl_kv_p50_ms"] == pytest.approx(50.0)
    assert rb["decode"]["waiting_mean"] == pytest.approx(1.5)
    assert rb["decode"]["running_mean"] == pytest.approx(8.0)


def test_forensics_legacy_pooled_pd_fallback_and_unified_ttft_decomposition() -> None:
    task = _make_task("e5f6a7b8")
    client = _base_forensics_client(ttft_p90=2800.0, tpot_p90=25.0, e2e_p90=12000.0)
    client["windows"]["full"]["ttft_ms"] = {"mean": 2200.0, "p50": 2000.0, "p90": 2800.0}
    client["windows"]["full"]["e2e_ms"] = {"mean": 10500.0, "p50": 10000.0, "p90": 12000.0}

    srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "metric_sources": {"vllm_stage_latencies": "pooled_namespace_legacy"},
        "per_pod": [{"pod": "p-0", "role": "prefill"}, {"pod": "d-0", "role": "decode"}],
        "router": {"scheduler_latency_p50_ms": {"mean": 0.2}},
        "summary": {
            "queue_depth": {"mean": 1.0, "max": 6.0},
            "kv_cache_usage_percent": {"mean": 35.0, "max": 42.0},
            # Pooled histograms that must be ignored in pd_pooled_fallback
            "vllm_queue_time_p50_ms": {"mean": 555.0},
            "vllm_prefill_time_p50_ms": {"mean": 360.0},
            "vllm_decode_time_p50_ms": {"mean": 825.0},
            "vllm_queue_time_p90_ms": {"mean": 1900.0},
            "vllm_prefill_time_p90_ms": {"mean": 600.0},
            "vllm_decode_time_p90_ms": {"mean": 1400.0},
            # NIXL has p50 and p95, intentionally NO p90 -> must NOT substitute p95 for p90!
            "nixl_transfer_latency_p50_ms": {"mean": 1200.0},
            "nixl_transfer_latency_p95_ms": {"mean": 2400.0},
        },
        "series": [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "router_scheduler_latency_p50_ms": 0.2,
                "nixl_transfer_latency_p50_ms": 1200.0,
                "queue_depth": 2.0,
                "kv_cache_usage_percent": 35.0,
            }
        ],
    }

    res = analyze_forensics(task, client_series=client, server_telemetry=srv)
    slb = res["stage_latency_breakdown"]
    assert slb["source"] == "pd_pooled_fallback"
    assert slb["caveat"]
    assert slb["dominant_ttft_stage"] != "gateway_overhead"
    assert slb["p50"]["kv_transfer_ms"] == pytest.approx(1200.0)
    assert slb["p50"]["total_stage_ms"] == pytest.approx(10000.0)
    # p90 must NOT substitute p95 (2400.0) for missing p90
    assert slb["p90"].get("kv_transfer_ms") is None
    assert slb["p90"]["total_stage_ms"] == pytest.approx(12000.0)

    # Rule 4 omits pooled ms
    r4 = next(r for r in res["rules_evaluated"] if r["code"] == "queue_bound")
    assert "555" not in r4["reason"] and "1900" not in r4["reason"]

    # KV Transfer (NIXL) Dominated finding emitted
    ttft_finding = _finding(res, "high_ttft")
    assert "KV Transfer (NIXL) Dominated" in ttft_finding["title"]

    # Unified ttft_decomposition matches stage_latency_breakdown
    td = res["ttft_decomposition"]
    assert td["source"] == slb["source"]
    assert td["caveat"] == slb["caveat"]
    assert td["p50"]["kv_transfer_ms"] == pytest.approx(slb["p50"]["kv_transfer_ms"])
    assert td["p50"]["epp_scheduling_ms"] == pytest.approx(slb["p50"]["epp_scheduling_ms"])


def _pd_base_server(
    *,
    role_points: list[dict] | None = None,
    series_points: list[dict] | None = None,
    summary: dict | None = None,
    summary_by_role: dict | None = None,
    derived_role_summary: dict | None = None,
    per_pod: list[dict] | None = None,
    metric_sources: dict | None = None,
) -> dict:
    return {
        "status": "available",
        "topology_mode": "disaggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "step_seconds": 5,
        "metric_sources": metric_sources or {},
        "per_pod": per_pod
        if per_pod is not None
        else [
            {"pod": "p-0", "role": "prefill", "request_rate_rps": {"mean": 1.0}},
            {"pod": "d-0", "role": "decode", "request_rate_rps": {"mean": 1.0}},
        ],
        "summary": summary
        if summary is not None
        else {
            "queue_depth": {"mean": 1.0, "max": 4.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
        },
        "summary_by_role": summary_by_role or {},
        "derived": {"role_summary": derived_role_summary} if derived_role_summary else {},
        "series": series_points
        if series_points is not None
        else [
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "queue_depth": 1.0,
                "kv_cache_usage_percent": 30.0,
                "vllm_preemptions_per_second": 0.0,
            }
        ],
        "role_series": role_points or [],
    }


def test_forensics_pd_queue_role_attribution_decode_prefill_both_diluted_and_fallback() -> None:
    task = _make_task("a9700001", backend_options={"ttft_slo": 1.0, "tpot_slo": 0.05})
    client = _base_forensics_client(ttft_p90=800.0, tpot_p90=25.0, e2e_p90=2500.0)
    client["windows"]["full"]["ttft_ms"] = {"mean": 500.0, "p50": 450.0, "p90": 800.0}
    client["windows"]["full"]["e2e_ms"] = {"mean": 2000.0, "p50": 1800.0, "p90": 2500.0}

    # 1. Decode-only queueing (>= 50% -> queue_bound verdict + queue_wait finding)
    srv_decode_q = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_waiting_requests": 0.0,
                "decode_waiting_requests": 2.0 if i < 6 else 0.0,
            }
            for i in range(10)
        ],
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 2.0 if i < 6 else 0.0,
                "kv_cache_usage_percent": 35.0,
            }
            for i in range(10)
        ],
    )
    res_d = analyze_forensics(task, client_series=client, server_telemetry=srv_decode_q)
    assert res_d["verdict"]["code"] == "queue_bound"
    assert res_d["verdict"]["role"] == "decode"
    assert res_d["verdict"]["label"] == "Decode (D) Queue / Admission Bound"
    assert res_d["verdict"]["primary_stage"] == "Decode (D) Engine Queue"
    r4_d = next(r for r in res_d["rules_evaluated"] if r["code"] == "queue_bound")
    assert r4_d["observed"]["queue_role"] == "decode"
    assert "Decode (D)" in r4_d["reason"]
    q_d = _finding(res_d, "queue_wait")
    assert q_d["role"] == "decode"
    assert q_d["title"] == "Sustained Decode (D) vLLM request queueing"
    assert q_d["value"] == "60%"
    assert "Decode (D)" in q_d["description"] and "Prefill (P)" in q_d["description"]
    assert any("Decode waiting:" in ev for ev in q_d["evidence"])
    assert any("Prefill waiting:" in ev for ev in q_d["evidence"])
    assert "Scale Decode (D) replicas (currently 1 Decode replica)" in q_d["recommendation"]
    assert "Prefill (P) is not the queue bottleneck" in q_d["recommendation"]

    # 2. Prefill-only queueing with role-separated latency
    srv_prefill_q = _pd_base_server(
        metric_sources={"vllm_stage_latencies": "role_separated"},
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_waiting_requests": 3.0 if i < 6 else 0.0,
                "decode_waiting_requests": 0.0,
                "prefill_vllm_queue_time_p90_ms": 1400.0,
                "prefill_vllm_queue_time_mean_ms": 950.0,
                "prefill_vllm_prefill_time_p90_ms": 300.0,
                "decode_vllm_queue_time_p90_ms": 270.0,
                "decode_vllm_queue_time_mean_ms": 2.0,
                "decode_vllm_decode_time_p90_ms": 1200.0,
            }
            for i in range(10)
        ],
        summary={
            "queue_depth": {"mean": 1.5, "max": 3.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
            "nixl_transfer_latency_p90_ms": {"mean": 50.0},
        },
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 3.0 if i < 6 else 0.0,
                "kv_cache_usage_percent": 30.0,
                "nixl_transfer_latency_p90_ms": 50.0,
            }
            for i in range(10)
        ],
    )
    res_p = analyze_forensics(task, client_series=client, server_telemetry=srv_prefill_q)
    assert res_p["verdict"]["code"] == "queue_bound"
    assert res_p["verdict"]["role"] == "prefill"
    assert res_p["verdict"]["label"] == "Prefill (P) Queue / Admission Bound"
    assert res_p["verdict"]["primary_stage"] == "Prefill (P) Engine Queue"
    q_p = _finding(res_p, "queue_wait")
    assert q_p["role"] == "prefill"
    assert q_p["title"] == "Sustained Prefill (P) vLLM request queueing"
    assert "Prefill (P)" in q_p["recommendation"]
    assert "Decode (D) is not the queue bottleneck" in q_p["recommendation"]

    # 3. Both P+D queueing
    srv_both_q = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_waiting_requests": 2.0 if i < 5 else 0.0,
                "decode_waiting_requests": 2.5 if i < 6 else 0.0,
            }
            for i in range(10)
        ],
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 4.5 if i < 5 else 0.0,
                "kv_cache_usage_percent": 35.0,
            }
            for i in range(10)
        ],
    )
    res_b = analyze_forensics(task, client_series=client, server_telemetry=srv_both_q)
    assert res_b["verdict"]["code"] == "queue_bound"
    assert res_b["verdict"]["role"] == "both"
    assert res_b["verdict"]["label"] == "Prefill & Decode (P/D) Queue / Admission Bound"
    q_b = _finding(res_b, "queue_wait")
    assert q_b["role"] == "both"
    assert q_b["title"] == "Sustained Prefill & Decode (P/D) vLLM request queueing"
    assert "both Prefill (P) and Decode (D)" in q_b["recommendation"]

    # 4. Diluted pooled waiting (0.10) while Decode role_series shows 60% sustained queueing
    srv_diluted = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_waiting_requests": 0.0,
                "decode_waiting_requests": 2.0 if i < 6 else 0.0,
            }
            for i in range(10)
        ],
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 1.0 if i == 0 else 0.0,
                "kv_cache_usage_percent": 30.0,
            }
            for i in range(10)
        ],
        summary={"queue_depth": {"mean": 0.4, "max": 1.0}, "kv_cache_usage_percent": {"mean": 30.0, "max": 35.0}},
    )
    res_dil = analyze_forensics(task, client_series=client, server_telemetry=srv_diluted)
    assert res_dil["verdict"]["code"] == "queue_bound"
    assert res_dil["verdict"]["role"] == "decode"
    q_dil = _finding(res_dil, "queue_wait")
    assert q_dil["role"] == "decode"
    assert q_dil["value"] == "60%"

    # 5. Missing per-role waiting/queue data in P/D falls back to generic wording without 'role'
    srv_no_role_q = _pd_base_server(
        role_points=[],
        per_pod=[{"pod": "p-0", "role": "prefill"}, {"pod": "d-0", "role": "decode"}],
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 3.0 if i < 6 else 0.0,
                "kv_cache_usage_percent": 30.0,
            }
            for i in range(10)
        ],
    )
    res_fallback = analyze_forensics(task, client_series=client, server_telemetry=srv_no_role_q)
    assert res_fallback["verdict"]["code"] == "queue_bound"
    assert "role" not in res_fallback["verdict"]
    q_fb = _finding(res_fallback, "queue_wait")
    assert q_fb["title"] == "Sustained vLLM request queueing"
    assert "role" not in q_fb


def test_forensics_pd_kv_pressure_ttft_tpot_skew_and_aggregated_neutrality() -> None:
    task = _make_task("a9700002", backend_options={"ttft_slo": 1.0, "tpot_slo": 0.05})
    client = _base_forensics_client(ttft_p90=2400.0, tpot_p90=140.0, e2e_p90=6000.0)

    # 1. Decode-only KV pressure diluted in pooled series (55%) but 95% + preemptions on Decode
    srv_d_kv = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_kv_cache_usage_percent": 15.0,
                "decode_kv_cache_usage_percent": 96.0 if i < 4 else 60.0,
                "prefill_preemptions_per_second": 0.0,
                "decode_preemptions_per_second": 0.5,
                "prefill_waiting_requests": 0.0,
                "decode_waiting_requests": 0.0,
            }
            for i in range(10)
        ],
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "kv_cache_usage_percent": 55.0,
                "queue_depth": 0.0,
                "vllm_preemptions_per_second": 0.0,
            }
            for i in range(10)
        ],
        summary={"kv_cache_usage_percent": {"mean": 50.0, "max": 55.0}, "queue_depth": {"mean": 0.0, "max": 0.0}},
    )
    res_d_kv = analyze_forensics(task, client_series=client, server_telemetry=srv_d_kv)
    assert res_d_kv["verdict"]["code"] == "kv_cache_eviction"
    assert res_d_kv["verdict"]["role"] == "decode"
    assert res_d_kv["verdict"]["label"] == "Decode (D) KV Cache & Eviction Bound"
    kv_d = _finding(res_d_kv, "kv_pressure")
    assert kv_d["role"] == "decode"
    assert kv_d["title"] == "High Decode (D) KV cache usage or request preemptions"
    assert "Prefill (P) KV cache is not the bottleneck" in kv_d["recommendation"]

    # 2. Prefill-only KV pressure
    srv_p_kv = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_kv_cache_usage_percent": 93.0 if i < 4 else 50.0,
                "decode_kv_cache_usage_percent": 25.0,
                "prefill_waiting_requests": 0.0,
                "decode_waiting_requests": 0.0,
            }
            for i in range(10)
        ],
        series_points=[{"timestamp": "1970-01-01T00:16:45Z", "queue_depth": 0.0, "kv_cache_usage_percent": 50.0}],
        summary={"kv_cache_usage_percent": {"mean": 50.0, "max": 60.0}, "queue_depth": {"mean": 0.0, "max": 0.0}},
    )
    res_p_kv = analyze_forensics(task, client_series=client, server_telemetry=srv_p_kv)
    kv_p = _finding(res_p_kv, "kv_pressure")
    assert kv_p["role"] == "prefill"
    assert "Decode (D) KV cache is not the bottleneck" in kv_p["recommendation"]

    # 3. Role-separated P/D high_ttft (prefill) + high_tpot (decode) + intra-role decode skew
    srv_pd_skew = _pd_base_server(
        metric_sources={"vllm_stage_latencies": "role_separated"},
        per_pod=[
            {"pod": "p-0", "role": "prefill", "request_rate_rps": {"mean": 2.0}},
            {"pod": "p-1", "role": "prefill", "request_rate_rps": {"mean": 2.0}},
            {"pod": "d-0", "role": "decode", "request_rate_rps": {"mean": 6.0}},
            {"pod": "d-1", "role": "decode", "request_rate_rps": {"mean": 0.5}},
            {"pod": "d-2", "role": "decode", "request_rate_rps": {"mean": 0.5}},
        ],
        series_points=[
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 40.0,
            }
        ],
        role_points=[
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "prefill_vllm_queue_time_p90_ms": 270.0,
                "prefill_vllm_queue_time_mean_ms": 2.0,
                "prefill_vllm_prefill_time_p90_ms": 1800.0,
                "decode_vllm_queue_time_p90_ms": 270.0,
                "decode_vllm_queue_time_mean_ms": 3.0,
                "decode_vllm_decode_time_p90_ms": 3600.0,
                "decode_running_requests": 6.0,
                "decode_gpu_utilization_percent": 82.0,
            }
        ],
        summary={"kv_cache_usage_percent": {"mean": 40.0, "max": 45.0}, "queue_depth": {"mean": 0.0, "max": 0.0}},
    )
    res_pd = analyze_forensics(task, client_series=client, server_telemetry=srv_pd_skew)
    ttft_pd = _finding(res_pd, "high_ttft")
    assert ttft_pd["role"] == "prefill"
    assert "Prefill (P)" in ttft_pd["title"]
    tpot_pd = _finding(res_pd, "high_tpot")
    assert tpot_pd["role"] == "decode"
    assert "Decode (D)" in tpot_pd["title"]
    skew_pd = _finding(res_pd, "pod_skew")
    assert skew_pd["role"] == "decode"
    assert "Decode (D)" in skew_pd["title"]

    # 4. Aggregated run must keep generic wording and omit 'role' everywhere
    srv_agg = {
        "status": "available",
        "topology_mode": "aggregated",
        "per_pod": [{"pod": "worker-0", "role": "worker", "request_rate_rps": {"mean": 1.0}}],
        "summary": {
            "queue_depth": {"mean": 2.0, "max": 5.0},
            "kv_cache_usage_percent": {"mean": 88.0, "max": 93.0},
        },
        "series": [
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 2.0 if i < 6 else 0.0,
                "kv_cache_usage_percent": 92.0 if i < 5 else 60.0,
            }
            for i in range(10)
        ],
    }
    res_agg = analyze_forensics(task, client_series=client, server_telemetry=srv_agg)
    assert "role" not in res_agg["verdict"]
    for finding in res_agg["findings"]:
        assert "role" not in finding
        assert "Prefill (P)" not in finding["title"]
        assert "Decode (D)" not in finding["title"]
    assert _finding(res_agg, "queue_wait")["title"] == "Sustained vLLM request queueing"


def test_service_ai_evidence_pack_preserves_finding_role_and_prompt_mentions_pd_roles() -> None:
    from llm_d_lens.simulation import service

    assert "Prefill (P)" in service._AI_ANALYSIS_SYSTEM_PROMPT
    assert "Decode (D)" in service._AI_ANALYSIS_SYSTEM_PROMPT
    assert "Prefill & Decode (P/D)" in service._AI_ANALYSIS_SYSTEM_PROMPT
    assert "forensics.findings[*].role" in service._AI_ANALYSIS_SYSTEM_PROMPT

    task = _make_task("a9700003", backend_options={"ttft_slo": 1.0, "tpot_slo": 0.05})
    client = _base_forensics_client(ttft_p90=800.0, tpot_p90=25.0, e2e_p90=2500.0)
    srv = _pd_base_server(
        role_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "prefill_waiting_requests": 0.0,
                "decode_waiting_requests": 2.0,
            }
            for i in range(6)
        ],
        series_points=[
            {"timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z", "queue_depth": 2.0, "kv_cache_usage_percent": 35.0}
            for i in range(6)
        ],
    )
    forensics = analyze_forensics(task, client_series=client, server_telemetry=srv)
    pack, _ = service._build_telemetry_evidence_pack(
        task,
        {"client_series": client, "server_telemetry": srv, "forensics": forensics},
    )
    assert pack["forensics"]["verdict"]["role"] == "decode"
    q_pack_finding = next(f for f in pack["forensics"]["findings"] if f["code"] == "queue_wait")
    assert q_pack_finding["role"] == "decode"
    serialized = json.loads(service._serialize_evidence_pack(pack))
    q_ser_finding = next(f for f in serialized["forensics"]["findings"] if f["code"] == "queue_wait")
    assert q_ser_finding["role"] == "decode"


def test_forensics_pd_blip_cumulative_preemptions_and_derived_role_summary_fallback() -> None:
    task = _make_task("a9700004", backend_options={"ttft_slo": 1.0, "tpot_slo": 0.05})
    client = _base_forensics_client(ttft_p90=3500.0, tpot_p90=25.0, e2e_p90=8000.0)
    client["windows"]["full"]["ttft_ms"] = {"mean": 2500.0, "p50": 2200.0, "p90": 3500.0}
    client["windows"]["full"]["e2e_ms"] = {"mean": 6500.0, "p50": 6000.0, "p90": 8000.0}

    # 1. Single 1/20-sample decode blip in pd_pooled_fallback must NOT override Prefill (P) TTFT attribution
    for p_gpu in (60.0, 92.0):
        srv_blip = _pd_base_server(
            metric_sources={"vllm_stage_latencies": "pooled_namespace_legacy"},
            role_points=[
                {
                    "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                    "prefill_waiting_requests": 0.0,
                    "decode_waiting_requests": 1.0 if i == 0 else 0.0,
                    "prefill_gpu_utilization_percent": p_gpu,
                    "decode_gpu_utilization_percent": 40.0,
                }
                for i in range(20)
            ],
            series_points=[
                {
                    "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                    "queue_depth": 0.0,
                    "kv_cache_usage_percent": 30.0,
                    "nixl_transfer_latency_p50_ms": 200.0,
                    "nixl_transfer_latency_p90_ms": 300.0,
                }
                for i in range(20)
            ],
            summary={
                "queue_depth": {"mean": 0.05, "max": 1.0},
                "kv_cache_usage_percent": {"mean": 30.0, "max": 35.0},
                "nixl_transfer_latency_p50_ms": {"mean": 200.0},
                "nixl_transfer_latency_p90_ms": {"mean": 300.0},
            },
        )
        res_blip = analyze_forensics(task, client_series=client, server_telemetry=srv_blip)
        assert res_blip["verdict"]["code"] == "prefill_bound"
        assert res_blip["verdict"]["role"] == "prefill"
        assert res_blip["verdict"]["label"] == "Prefill (P) Compute Bound"
        ttft_blip = _finding(res_blip, "high_ttft")
        assert ttft_blip["role"] == "prefill"
        assert "Address Decode (D) queueing first" not in ttft_blip["recommendation"]

    # 2. Unchanged cumulative per_pod vllm_preemptions_total (min=12, max=12) must yield 0 in-run preemptions
    srv_cum = _pd_base_server(
        role_points=[],
        per_pod=[
            {"pod": "p-0", "role": "prefill", "request_rate_rps": {"mean": 1.0}},
            {
                "pod": "d-0",
                "role": "decode",
                "request_rate_rps": {"mean": 1.0},
                "vllm_preemptions_total": {"min": 12.0, "max": 12.0},
                "kv_cache_usage_percent": {"mean": 30.0, "max": 35.0},
            },
        ],
        series_points=[
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 30.0,
                "vllm_preemptions_total": 12.0,
            },
            {
                "timestamp": "1970-01-01T00:16:50Z",
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 30.0,
                "vllm_preemptions_total": 12.0,
            },
        ],
        summary={
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 35.0},
            "vllm_preemptions_per_second": {"mean": 0.0},
        },
    )
    res_cum = analyze_forensics(task, client_series=client, server_telemetry=srv_cum)
    assert res_cum["verdict"]["code"] != "kv_cache_eviction"
    assert not any(f["code"] == "kv_pressure" for f in res_cum["findings"])
    r3_cum = next(r for r in res_cum["rules_evaluated"] if r["code"] == "kv_cache_eviction")
    assert r3_cum["triggered"] is False
    assert r3_cum["observed"]["total_preemptions"] == 0.0

    # 3. Missing role_series with derived.role_summary present still attributes queue_wait and keeps 0.0 GPU util
    srv_drv = _pd_base_server(
        role_points=[],
        derived_role_summary={
            "prefill": {"waiting_requests": 0.05, "running_requests": 0.4, "gpu_utilization_percent": 0.0},
            "decode": {"waiting_requests": 2.4, "running_requests": 8.0, "gpu_utilization_percent": 85.0},
        },
        series_points=[
            {
                "timestamp": f"1970-01-01T00:16:{45 + i * 5:02d}Z",
                "queue_depth": 2.0 if i < 6 else 0.0,
                "kv_cache_usage_percent": 35.0,
            }
            for i in range(10)
        ],
        summary={"queue_depth": {"mean": 1.2, "max": 4.0}, "kv_cache_usage_percent": {"mean": 35.0, "max": 40.0}},
    )
    res_drv = analyze_forensics(task, client_series=client, server_telemetry=srv_drv)
    assert res_drv["verdict"]["code"] == "queue_bound"
    assert res_drv["verdict"]["role"] == "decode"
    q_drv = _finding(res_drv, "queue_wait")
    assert q_drv["role"] == "decode"
    assert q_drv["title"] == "Sustained Decode (D) vLLM request queueing"
    ctx_drv = ForensicsAnalyzer()._build_pd_role_context(
        is_disaggregated=True,
        has_role_separated_latency=False,
        is_legacy_pooled_pd=True,
        role_breakdown=res_drv["stage_latency_breakdown"]["role_breakdown"],
        steady_role_points=[],
        summary_by_role={},
        derived_role_summary=srv_drv["derived"]["role_summary"],
        engine_pods=[],
        components={},
        duration_seconds=60.0,
    )
    assert ctx_drv is not None
    assert ctx_drv["prefill"]["gpu_util_mean"] == 0.0


@pytest.mark.asyncio
async def test_service_get_task_telemetry_handles_historical_trace_replayer_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        # 1. Persisted client-series.json + stale forensics.json -> serves/refreshes cleanly with zero 500s
        task_id = "ab12ef34"
        task = _make_task(task_id, backend="trace-replayer", duration_seconds=30)
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="trace-replayer",
            backend_version="0.2.0",
            summary={"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        report_dir = task_dir / "artifacts" / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "client-series.json").write_text(
            json.dumps(
                {
                    "schema_version": telemetry_post.CLIENT_SERIES_SCHEMA_VERSION,
                    "warmup_seconds": 0.0,
                    "duration_seconds": 30.0,
                    "buckets": [
                        {
                            "index": 0,
                            "start_seconds": 0,
                            "end_seconds": 5,
                            "mid_seconds": 2.5,
                            "completed_requests": 1,
                        }
                    ],
                    "windows": {"full": {"request_count": 1, "successful_requests": 1, "error_rate_pct": 0.0}},
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": 3, "verdict": {"code": "healthy"}}),
            encoding="utf-8",
        )
        await service.save_task(task)

        persisted_tel = await service.get_task_telemetry(task_id)
        assert persisted_tel["client_series"]["windows"]["full"]["request_count"] == 1
        assert persisted_tel["forensics"]["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION

        # 2. warmup_seconds override hits else: branch when backend is unregistered -> zero 500s
        override_tel = await service.get_task_telemetry(task_id, warmup_seconds=5.0)
        assert override_tel["client_series"]["warmup_seconds"] == 5.0

        # 3. Missing client-series.json hits else: branch when backend is unregistered -> zero 500s
        task_missing_id = "ab12ef35"
        task_missing = _make_task(task_missing_id, backend="trace-replayer", duration_seconds=30)
        task_missing.task_dir = str(service.task_root() / task_missing_id)
        await service.save_task(task_missing)
        missing_tel = await service.get_task_telemetry(task_missing_id)
        assert missing_tel["client_series"]["schema_version"] == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        assert missing_tel["forensics"]["schema_version"] == telemetry_post.FORENSICS_SCHEMA_VERSION
    finally:
        service.reset_service_state()


def test_enrich_server_telemetry_pd_token_flow_uses_prefill_only_and_preserves_aggregated() -> None:
    """A1: Disaggregated token_flow uses Prefill role avoided compute (never P+D summed rates), aggregated preserved."""
    # 1. P/D run with P+D summed rates in series/summary AND explicit prompt_tokens_by_role_source (e23b5732 shape)
    pd_srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "engine_prompt_local_cache_tps": 6000.0,
                "engine_prompt_external_tps": 3000.0,
                "engine_prompt_local_compute_tps": 3500.0,
            },
            {
                "timestamp": "2026-01-01T00:02:20Z",
                "engine_prompt_local_cache_tps": 6000.0,
                "engine_prompt_external_tps": 3000.0,
                "engine_prompt_local_compute_tps": 3500.0,
            },
        ],
        "summary": {
            "engine_prompt_local_cache_tps": {"mean": 6000.0},
            "engine_prompt_external_tps": {"mean": 3000.0},
            "engine_prompt_local_compute_tps": {"mean": 3500.0},
        },
        "derived": {
            "prompt_tokens_by_role_source": [
                {
                    "role": "Prefill",
                    "local_cache_tokens": 392673,
                    "external_kv_tokens": 0,
                    "local_compute_tokens": 457688,
                    "total_tokens": 850361,
                },
                {
                    "role": "Decode",
                    "local_cache_tokens": 423295,
                    "external_kv_tokens": 419959,
                    "local_compute_tokens": 0,
                    "total_tokens": 843254,
                },
            ],
        },
    }
    pd_client = {
        "duration_seconds": 140.0,
        "windows": {"full": {"total_input_tokens": 843008, "total_output_tokens": 10000}},
        "token_flow": {"sent_tokens": 843008, "raw_session_reusable_tokens": 439199},
    }
    enriched_pd = telemetry_post._enrich_server_telemetry(pd_srv, pd_client)
    tf_pd = enriched_pd["derived"]["token_flow"]
    assert tf_pd["sent_tokens"] == 843008
    # Prefill ratio 392673 / 850361 = 46.177% -> 389278 cached, never 100% clamped
    assert tf_pd["cached_tokens"] == 389278
    assert tf_pd["cached_pct"] == pytest.approx(46.18, abs=0.01)
    assert tf_pd["recomputed_tokens"] + tf_pd["cold_new_tokens"] == 843008 - 389278
    assert tf_pd["cached_tokens"] + tf_pd["recomputed_tokens"] + tf_pd["cold_new_tokens"] == tf_pd["sent_tokens"]
    assert tf_pd["cold_new_tokens"] > 0

    # 2. Disaggregated Decode-only degraded rows (missing Prefill row) with non-zero Decode series/summary
    #    must NOT leak Decode KV rates into cached_tokens; falls back to client_cached_tokens
    decode_only_srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "engine_prompt_local_cache_tps": 8000.0,
                "engine_prompt_external_tps": 8000.0,
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "engine_prompt_local_cache_tps": 8000.0,
                "engine_prompt_external_tps": 8000.0,
            },
        ],
        "summary": {
            "engine_prompt_local_cache_tps": {"mean": 8000.0},
            "engine_prompt_external_tps": {"mean": 8000.0},
        },
        "derived": {
            "prompt_tokens_by_role_source": [
                {
                    "role": "Decode",
                    "local_cache_tokens": 400000,
                    "external_kv_tokens": 400000,
                    "local_compute_tokens": 0,
                    "total_tokens": 800000,
                }
            ]
        },
    }
    decode_only_client = {
        "duration_seconds": 60.0,
        "windows": {"full": {"total_input_tokens": 800000}},
        "token_flow": {"sent_tokens": 800000, "client_cached_tokens": 200000},
    }
    tf_dec_only = telemetry_post._enrich_server_telemetry(decode_only_srv, decode_only_client)["derived"]["token_flow"]
    assert tf_dec_only["cached_tokens"] == 200000
    assert tf_dec_only["cold_new_tokens"] == 600000

    # 2b. Disaggregated Prefill total_tokens == 0 + Decode row + non-zero Decode rates -> cached_tokens == 0
    zero_prefill_srv = {
        "status": "available",
        "topology_mode": "disaggregated",
        "step_seconds": 5,
        "series": decode_only_srv["series"],
        "summary": decode_only_srv["summary"],
        "derived": {
            "prompt_tokens_by_role_source": [
                {
                    "role": "Prefill",
                    "local_cache_tokens": 0,
                    "external_kv_tokens": 0,
                    "local_compute_tokens": 0,
                    "total_tokens": 0,
                },
                *decode_only_srv["derived"]["prompt_tokens_by_role_source"],
            ]
        },
    }
    tf_zero_prefill = telemetry_post._enrich_server_telemetry(zero_prefill_srv, decode_only_client)["derived"][
        "token_flow"
    ]
    assert tf_zero_prefill["cached_tokens"] == 0
    assert tf_zero_prefill["cold_new_tokens"] == 800000

    # 3. Aggregated Worker run preserves integrated rate behavior
    agg_srv = {
        "status": "available",
        "topology_mode": "aggregated",
        "step_seconds": 5,
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "engine_prompt_local_cache_tps": 3000.0,
                "engine_prompt_external_tps": 0.0,
                "engine_prompt_local_compute_tps": 2500.0,
            },
            {
                "timestamp": "2026-01-01T00:00:10Z",
                "engine_prompt_local_cache_tps": 3000.0,
                "engine_prompt_external_tps": 0.0,
                "engine_prompt_local_compute_tps": 2500.0,
            },
        ],
        "summary": {
            "engine_prompt_local_cache_tps": {"mean": 3000.0},
            "engine_prompt_external_tps": {"mean": 0.0},
            "engine_prompt_local_compute_tps": {"mean": 2500.0},
        },
        "derived": {
            "prompt_tokens_by_role_source": [
                {
                    "role": "Worker",
                    "local_cache_tokens": 35000,
                    "external_kv_tokens": 0,
                    "local_compute_tokens": 28000,
                    "total_tokens": 63000,
                }
            ]
        },
    }
    agg_client = {"duration_seconds": 10.0, "windows": {"full": {"total_input_tokens": 60000}}}
    tf_agg = telemetry_post._enrich_server_telemetry(agg_srv, agg_client)["derived"]["token_flow"]
    assert tf_agg["cached_tokens"] == 30000
    assert tf_agg["cold_new_tokens"] == 30000


def test_unified_prefix_cache_thresholds_between_slot_lanes_and_cache_buckets() -> None:
    """B5: _classify_cache_bucket and _classify_cache_hit_tier share <10% / 10-70% / >=70% boundaries."""
    cases = [
        (0.0, "cold", 0),
        (0.05, "cold", 0),
        (0.099, "cold", 0),
        (0.10, "partial", 1),
        (0.59, "partial", 1),
        (0.60, "partial", 1),
        (0.699, "partial", 1),
        (0.70, "warm", 2),
        (1.0, "warm", 2),
    ]
    for ratio, expected_tier, expected_code in cases:
        assert telemetry_post._classify_cache_hit_tier(ratio * 100.0) == expected_tier
        assert telemetry_post._classify_cache_bucket(ratio) == (expected_tier, expected_code)


def test_build_client_series_merges_trailing_partial_buckets_and_preserves_counts() -> None:
    """B6: Trailing partial bucket <0.5*bucket_width merges into preceding bucket for buckets & fidelity_series."""
    origin = 1_750_000_000.0
    # duration=140.189s (bucket_seconds=5 -> trailing [140, 140.189] width=0.189s < 2.5s)
    # dispatch_duration=117.2s (fid_bucket_seconds=3 -> trailing [117, 117.2] width=0.2s < 1.5s)
    task = _make_task("b6000001", duration_seconds=140)
    records = [
        ArtifactRecord(
            request_id="r-early",
            arrived=origin + 0.0,
            completed=origin + 138.0,
            successful=True,
            input_tokens=100,
            output_tokens=200,
            requested_output_tokens=200,
            ttft_ms=100.0,
            tpot_ms=10.0,
            latency_ms=138000.0,
            s_time_drift_ms=5.0,
        ),
        ArtifactRecord(
            request_id="r-tail-dispatch",
            arrived=origin + 117.1,
            completed=origin + 139.0,
            successful=True,
            input_tokens=100,
            output_tokens=200,
            requested_output_tokens=200,
            ttft_ms=110.0,
            tpot_ms=11.0,
            latency_ms=21900.0,
            s_time_drift_ms=10.0,
        ),
        ArtifactRecord(
            request_id="r-tail-complete",
            arrived=origin + 110.0,
            completed=origin + 140.189,
            successful=True,
            input_tokens=100,
            output_tokens=600,
            requested_output_tokens=600,
            ttft_ms=120.0,
            tpot_ms=12.0,
            latency_ms=30189.0,
            s_time_drift_ms=15.0,
        ),
    ]
    schedule = [0.0, 110.0, 117.1]
    cs = telemetry_post.build_client_series(task, records, arrival_schedule=schedule)

    buckets = cs["buckets"]
    assert len(buckets) == 28  # 29th [140, 140.189] merged into 28th [135, 140.189]
    assert len(cs["concurrency_series"]) == len(buckets)
    last_b = buckets[-1]
    assert last_b["start_seconds"] == 135
    assert last_b["end_seconds"] == 140.189
    assert last_b["mid_seconds"] == round(135 + (140.189 - 135) / 2.0, 3)
    assert last_b["completed_requests"] == 3
    assert last_b["throughput_rps"] == pytest.approx(3 / 5.189, rel=1e-3)
    assert last_b["output_tok_per_s"] == pytest.approx(1000 / 5.189, rel=1e-3)
    assert sum(b["arrived_requests"] for b in buckets) == cs["windows"]["full"]["request_count"] == 3
    assert sum(b["completed_requests"] for b in buckets) == 3

    fid = cs["fidelity_series"]
    # 117.1s with max 40 buckets -> fid_bucket_seconds=3, 40th [117, 117.1] width=0.1s < 1.5s merged into [114, 117.1]
    assert len(fid) == 39
    assert fid[-1]["start_seconds"] == 114
    assert fid[-1]["end_seconds"] == 117.1
    assert fid[-1]["arrived_requests"] == 1
    assert fid[-1]["arrived_rps"] == pytest.approx(1 / 3.1, rel=1e-3)
    assert sum(f["arrived_requests"] for f in fid) == 3

    # Non-merge check when trailing width >= 0.5 * bucket_seconds (13.0s -> [10, 13.0] width=3.0 >= 2.5)
    cs_nomerge = telemetry_post.build_client_series(
        _make_task("b6000002", duration_seconds=13),
        [
            ArtifactRecord(
                request_id="r1",
                arrived=origin + 0.0,
                completed=origin + 13.0,
                successful=True,
                input_tokens=50,
                output_tokens=10,
                ttft_ms=50.0,
                tpot_ms=5.0,
                latency_ms=13000.0,
            )
        ],
    )
    assert len(cs_nomerge["buckets"]) == 3
    assert cs_nomerge["buckets"][-1]["start_seconds"] == 10
    assert cs_nomerge["buckets"][-1]["end_seconds"] == 13.0


def test_build_client_series_per_window_cdf_and_observed_tpot_heatmap() -> None:
    """B7b & B10: windows.profiling.cdf & windows.full.cdf match percentiles; tpot heatmap uses observed output_tokens."""
    origin = 1_750_000_000.0
    task = _make_task("b7b10001", duration_seconds=20, backend_options={"warmup_seconds": 5.0})
    records = [
        # Warmup request (t=1.0 < 5.0) with fast TTFT=100ms, requested_output_tokens=17940 vs observed=80
        ArtifactRecord(
            request_id="w1",
            arrived=origin + 1.0,
            completed=origin + 2.0,
            successful=True,
            input_tokens=200,
            output_tokens=80,
            requested_output_tokens=17940,
            ttft_ms=100.0,
            tpot_ms=10.0,
            latency_ms=1000.0,
            s_time_drift_ms=2.0,
        ),
        # Profiling requests (t >= 5.0) with higher TTFT=1000..3000ms
        ArtifactRecord(
            request_id="p1",
            arrived=origin + 6.0,
            completed=origin + 8.0,
            successful=True,
            input_tokens=300,
            output_tokens=120,
            requested_output_tokens=17940,
            ttft_ms=1000.0,
            tpot_ms=20.0,
            latency_ms=2000.0,
            s_time_drift_ms=8.0,
        ),
        ArtifactRecord(
            request_id="p2",
            arrived=origin + 10.0,
            completed=origin + 15.0,
            successful=True,
            input_tokens=400,
            output_tokens=400,
            requested_output_tokens=17940,
            ttft_ms=3000.0,
            tpot_ms=30.0,
            latency_ms=5000.0,
            s_time_drift_ms=16.0,
        ),
    ]
    cs = telemetry_post.build_client_series(task, records)
    assert (
        cs["schema_version"]
        == telemetry_post.CLIENT_SERIES_SCHEMA_VERSION
        == telemetry_post.REPORT_SCHEMA_VERSION
        == 15
    )

    # B7b: Per-window CDFs
    full_cdf = cs["windows"]["full"]["cdf"]
    prof_cdf = cs["windows"]["profiling"]["cdf"]
    assert full_cdf == cs["cdf"]
    for c in (full_cdf, prof_cdf):
        assert set(c.keys()) == {"quantiles", "ttft_ms", "tpot_ms", "e2e_ms", "drift_ms"}
    assert prof_cdf["ttft_ms"][50] == pytest.approx(cs["windows"]["profiling"]["ttft_ms"]["p50"], abs=1e-2)
    assert prof_cdf["ttft_ms"][90] == pytest.approx(cs["windows"]["profiling"]["ttft_ms"]["p90"], abs=1e-2)
    assert prof_cdf["ttft_ms"][50] == 1000.0
    assert prof_cdf["ttft_ms"][90] == 3000.0
    assert full_cdf["ttft_ms"][50] == 1000.0
    assert full_cdf["ttft_ms"][0] == 100.0
    assert prof_cdf["ttft_ms"][0] == 1000.0

    # When warmup=0, windows.profiling.cdf matches windows.full.cdf
    cs_nowu = telemetry_post.build_client_series(_make_task("b7b10002", duration_seconds=20), records)
    assert cs_nowu["windows"]["profiling"]["cdf"] == cs_nowu["windows"]["full"]["cdf"] == cs_nowu["cdf"]

    # B10: Heatmap metadata & observed output_tokens preference
    ttft_hm = cs["heatmap"]["ttft"]
    tpot_hm = cs["heatmap"]["tpot"]
    assert ttft_hm["sequence_length"] == "input"
    assert ttft_hm["sequence_length_source"] == "observed"
    assert tpot_hm["sequence_length"] == "output"
    assert tpot_hm["sequence_length_source"] == "observed"
    assert max(c["seq_end"] for c in tpot_hm["cells"]) <= 401

    # Fallback to requested_output_tokens only when output_tokens is None
    req_only_recs = [
        ArtifactRecord(
            request_id="req-only",
            arrived=origin + 1.0,
            completed=origin + 2.0,
            successful=True,
            input_tokens=200,
            output_tokens=None,
            requested_output_tokens=2048,
            ttft_ms=100.0,
            tpot_ms=12.0,
            latency_ms=1000.0,
        )
    ]
    cs_req = telemetry_post.build_client_series(_make_task("b1000001", duration_seconds=10), req_only_recs)
    assert cs_req["heatmap"]["tpot"]["sequence_length"] == "output"
    assert cs_req["heatmap"]["tpot"]["sequence_length_source"] == "requested"
    assert cs_req["heatmap"]["tpot"]["cells"][0]["seq_start"] == 2048


def test_reconcile_pd_engine_overhead_guards_and_conservation() -> None:
    """t107: Pure helper guards, zero/negative handling, and rounding-penny conservation."""
    # 1. Unclamped 777f0072 mean -> exact 0.000 ms distortion
    res_777f = _reconcile_pd_engine_overhead(
        1393.004,
        False,
        2662.008,
        306.223,
        1456.887,
        1663.796,
        1169.691,
        54.47,
    )
    assert res_777f == (54.47, 898.899, 439.635)
    assert round(sum(res_777f), 3) == 1393.004

    # 2. Client-clamped e0a7cde7 mean -> proportional conservation to 3 decimals
    res_e0a7_mean = _reconcile_pd_engine_overhead(
        775.877,
        False,
        1141.335,
        0.019,
        680.037,
        1040.473,
        614.656,
        26.966,
    )
    assert res_e0a7_mean == (23.585, 403.447, 348.845)
    assert round(sum(res_e0a7_mean), 3) == 775.877

    # 3. 10s-clamped or non-finite/None engine_overhead_ms -> (None, None, None)
    assert _reconcile_pd_engine_overhead(1200.0, True, 1141.0, 0.02, 680.0, 10000.0, 614.0, 27.0) == (None, None, None)
    for bad_eo in (None, float("nan"), float("inf"), True):
        assert _reconcile_pd_engine_overhead(bad_eo, False, 1141.0, 0.02, 680.0, 1040.0, 614.0, 27.0) == (
            None,
            None,
            None,
        )

    # 4. Any of the 5 required role inputs missing / non-finite / bool -> (None, None, None)
    base_args = [1141.0, 0.02, 680.0, 1040.0, 614.0]
    for idx in range(5):
        for bad_val in (None, float("nan"), float("-inf"), True):
            mutated = list(base_args)
            mutated[idx] = bad_val
            assert _reconcile_pd_engine_overhead(500.0, False, *mutated, 27.0) == (None, None, None)

    # Optional d_pre=None falls back to raw_d_pre=0.0 when the other 5 inputs are valid
    d_pre_none = _reconcile_pd_engine_overhead(100.0, False, 200.0, 10.0, 140.0, 150.0, 100.0, None)
    assert d_pre_none == (0.0, 50.0, 50.0)

    # 5. Negative non-additive quantile where all raw components clamp to 0 -> (None, None, None)
    assert _reconcile_pd_engine_overhead(120.0, False, 500.0, 200.0, 400.0, 400.0, 500.0, -5.0) == (None, None, None)

    # 6. Zero or <=0 engine_overhead_ms -> (0.0, 0.0, 0.0)
    assert _reconcile_pd_engine_overhead(0.0, False, 500.0, 10.0, 400.0, 400.0, 100.0, 10.0) == (0.0, 0.0, 0.0)
    assert _reconcile_pd_engine_overhead(0.0004, False, 500.0, 10.0, 400.0, 400.0, 100.0, 10.0) == (0.0, 0.0, 0.0)

    # 7. Rounding-penny conservation: 0.0 raw component stays strictly 0.0; penny goes to argmax
    # raw_d_pre = 0.0, raw_p_oh = 100.0, raw_d_oh = 200.0, target = 10.001
    # unadjusted round: [0.0, 3.334, 6.667] -> sum 10.001, try target = 10.000: [0.0, 3.333, 6.667] -> sum 10.000
    # target = 0.005 with raw=(0.0, 1.0, 2.0): [round(0), round(0.001667)=0.002, round(0.003333)=0.003] -> sum=0.005
    # target = 0.004 with raw=(0.0, 1.0, 1.0): [0.0, 0.002, 0.002]; target=0.003 with raw=(0.0, 1.0, 2.0):
    # [0.0, 0.001, 0.002]; target = 0.005 with raw=(0.0, 1.0, 1.0): [0.0, 0.003, 0.003] -> sum=0.006 -> drift=-0.001 -> argmax(1)
    penny_res = _reconcile_pd_engine_overhead(
        0.0054,
        False,
        101.0,
        0.0,
        100.0,
        101.0,
        100.0,
        0.0,
    )
    assert penny_res[0] == 0.0
    assert round(sum(penny_res), 3) == 0.005

    # Argmax receives drift when non-zero raw components tie/differ
    penny_asym = _reconcile_pd_engine_overhead(
        10.004,
        False,
        101.0,
        0.0,
        100.0,
        102.0,
        100.0,
        0.0,
    )
    # raw=(0.0, 1.0, 2.0), target=10.004 -> vals=[0.0, 3.335, 6.669] -> sum=10.004
    assert penny_asym[0] == 0.0
    assert round(sum(penny_asym), 3) == 10.004

    # target=0.008 with raw=(0.0, 1.0, 3.0) -> 0.002 + 0.006 = 0.008;
    # target=10.001 with raw=(0.0, 3.0, 7.0) -> 3.000 + 7.001;
    # target=1.001 with raw=(0.0, 1.0, 2.0) -> [0.0, 0.334, 0.667];
    # target=2.002 with raw=(0.0, 1.0, 2.0) -> [0.0, 0.667, 1.335];
    # target=0.007 with raw=(0.0, 1.0, 2.0) -> [0.0, 0.002, 0.005]
    # target=0.001 with raw=(0.0, 1.0, 2.0) -> [0.0, 0.0, 0.001] (raw_d_pre stays 0.0, argmax=2 gets 0.001)
    penny_tiny = _reconcile_pd_engine_overhead(
        0.001,
        False,
        101.0,
        0.0,
        100.0,
        102.0,
        100.0,
        0.0,
    )
    assert penny_tiny == (0.0, 0.0, 0.001)
    assert round(sum(penny_tiny), 3) == 0.001


def test_forensics_pd_engine_overhead_decomposition_and_fallbacks() -> None:
    """t107: End-to-end P/D sub-stage reconciliation across e0a7cde7/777f0072, 10s-clamp, aggregated, and pooled."""
    # 1. P/D role-separated: e0a7cde7 mean (client-clamped), p90 (_effective_role_queue floor bypass), p50
    task = _make_task("e0a7cde7")
    client = _base_forensics_client(ttft_p90=5701.829, tpot_p90=25.0, e2e_p90=14000.0)
    client["windows"]["full"]["ttft_ms"] = {"mean": 2070.811, "p50": 1525.0, "p90": 5701.829}
    client["windows"]["full"]["e2e_ms"] = {"mean": 8000.0, "p50": 7000.0, "p90": 14000.0}

    srv_e0a7 = _pd_base_server(
        metric_sources={"vllm_stage_latencies": "role_separated"},
        summary={
            "queue_depth": {"mean": 0.0, "max": 0.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
            "nixl_transfer_latency_mean_ms": {"mean": 614.656},
            "nixl_transfer_latency_p50_ms": {"mean": 393.896},
            "nixl_transfer_latency_p90_ms": {"mean": 1777.468},
            "vllm_decode_time_mean_ms": {"mean": 5900.0},
            "vllm_decode_time_p50_ms": {"mean": 5400.0},
            "vllm_decode_time_p90_ms": {"mean": 8200.0},
            "vllm_ttft_p90_ms": {"mean": 5585.881},
        },
        summary_by_role={
            "prefill": {
                "vllm_queue_time_mean_ms": {"mean": 0.019},
                "vllm_queue_time_p50_ms": {"mean": 150.0},
                "vllm_queue_time_p90_ms": {"mean": 270.0},
                "vllm_prefill_time_mean_ms": {"mean": 680.037},
                "vllm_prefill_time_p50_ms": {"mean": 366.359},
                "vllm_prefill_time_p90_ms": {"mean": 2143.608},
                "vllm_ttft_mean_ms": {"mean": 1141.335},
                "vllm_ttft_p50_ms": {"mean": 877.253},
                "vllm_ttft_p90_ms": {"mean": 2787.885},
            },
            "decode": {
                "vllm_queue_time_mean_ms": {"mean": 614.656},
                "vllm_queue_time_p50_ms": {"mean": 393.896},
                "vllm_queue_time_p90_ms": {"mean": 1777.468},
                "vllm_prefill_time_mean_ms": {"mean": 26.966},
                "vllm_prefill_time_p50_ms": {"mean": 150.0},
                "vllm_prefill_time_p90_ms": {"mean": 270.0},
                "vllm_decode_time_mean_ms": {"mean": 5900.0},
                "vllm_decode_time_p50_ms": {"mean": 5400.0},
                "vllm_decode_time_p90_ms": {"mean": 8200.0},
                "vllm_ttft_mean_ms": {"mean": 1040.473},
                "vllm_ttft_p50_ms": {"mean": 794.254},
                "vllm_ttft_p90_ms": {"mean": 2797.997},
            },
        },
        series_points=[
            {
                "timestamp": "1970-01-01T00:16:45Z",
                "router_scheduler_latency_mean_ms": 0.222,
                "router_scheduler_latency_p50_ms": 0.2,
                "router_scheduler_latency_p90_ms": 0.446,
                "queue_depth": 0.0,
                "kv_cache_usage_percent": 30.0,
            }
        ],
    )
    srv_e0a7["router"] = {
        "scheduler_latency_mean_ms": {"mean": 0.222},
        "scheduler_latency_p50_ms": {"mean": 0.2},
        "scheduler_latency_p90_ms": {"mean": 0.446},
    }

    res = analyze_forensics(task, client_series=client, server_telemetry=srv_e0a7)
    assert res["schema_version"] == 8
    slb = res["stage_latency_breakdown"]
    td = res["ttft_decomposition"]

    # P90 unclamped: _effective_role_queue bypasses 270ms floor -> p_q_eff=0.019, engine_overhead=1664.786
    p90 = slb["p90"]
    assert p90["server_ttft_clamped"] is False
    assert p90["engine_overhead_ms"] == pytest.approx(1664.786)
    assert p90["gateway_overhead_ms"] == pytest.approx(115.502)
    assert p90["decode_first_step_ms"] == pytest.approx(270.0)
    assert p90["prefill_engine_overhead_ms"] == pytest.approx(644.258)
    assert p90["decode_handoff_overhead_ms"] == pytest.approx(750.528)
    assert round(
        p90["decode_first_step_ms"] + p90["prefill_engine_overhead_ms"] + p90["decode_handoff_overhead_ms"],
        3,
    ) == pytest.approx(p90["engine_overhead_ms"])

    # Mean client-clamped: proportional scaling conserves sum == engine_overhead_ms (775.877)
    mean_sl = slb["mean"]
    assert mean_sl["engine_overhead_ms"] == pytest.approx(775.877)
    assert mean_sl["decode_first_step_ms"] == pytest.approx(23.585)
    assert mean_sl["prefill_engine_overhead_ms"] == pytest.approx(403.447)
    assert mean_sl["decode_handoff_overhead_ms"] == pytest.approx(348.845)
    assert round(
        mean_sl["decode_first_step_ms"] + mean_sl["prefill_engine_overhead_ms"] + mean_sl["decode_handoff_overhead_ms"],
        3,
    ) == pytest.approx(mean_sl["engine_overhead_ms"])

    # Propagated identically onto ttft_decomposition.{p50, p90, mean}
    for slice_key in ("p50", "p90", "mean"):
        for field in ("decode_first_step_ms", "prefill_engine_overhead_ms", "decode_handoff_overhead_ms"):
            assert td[slice_key][field] == slb[slice_key][field]

    # 2. P/D unclamped mean (777f0072 shape): zero-distortion attribution
    client_777f = _base_forensics_client(ttft_p90=8600.0, tpot_p90=30.0, e2e_p90=16000.0)
    client_777f["windows"]["full"]["ttft_ms"] = {"mean": 4500.0, "p50": 3900.0, "p90": 8600.0}
    client_777f["windows"]["full"]["e2e_ms"] = {"mean": 12000.0, "p50": 10000.0, "p90": 16000.0}
    srv_777f = _pd_base_server(
        metric_sources={"vllm_stage_latencies": "role_separated"},
        summary={
            "queue_depth": {"mean": 1.0, "max": 4.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
            "nixl_transfer_latency_mean_ms": {"mean": 1169.691},
            "nixl_transfer_latency_p50_ms": {"mean": 803.007},
            "nixl_transfer_latency_p90_ms": {"mean": 3043.432},
            "vllm_ttft_mean_ms": {"mean": 4325.805},
        },
        summary_by_role={
            "prefill": {
                "vllm_queue_time_mean_ms": {"mean": 306.223},
                "vllm_queue_time_p50_ms": {"mean": 214.943},
                "vllm_prefill_time_mean_ms": {"mean": 1456.887},
                "vllm_prefill_time_p50_ms": {"mean": 1203.703},
                "vllm_ttft_mean_ms": {"mean": 2662.008},
                "vllm_ttft_p50_ms": {"mean": 2383.781},
            },
            "decode": {
                "vllm_queue_time_mean_ms": {"mean": 1169.691},
                "vllm_queue_time_p50_ms": {"mean": 803.007},
                "vllm_prefill_time_mean_ms": {"mean": 54.47},
                "vllm_prefill_time_p50_ms": {"mean": 151.793},
                "vllm_decode_time_mean_ms": {"mean": 6000.0},
                "vllm_decode_time_p50_ms": {"mean": 5000.0},
                "vllm_ttft_mean_ms": {"mean": 1663.796},
                "vllm_ttft_p50_ms": {"mean": 1382.764},
            },
        },
    )
    res_777f = analyze_forensics(_make_task("777f0072"), client_series=client_777f, server_telemetry=srv_777f)
    mean_777f = res_777f["stage_latency_breakdown"]["mean"]
    assert mean_777f["engine_overhead_ms"] == pytest.approx(1393.004)
    assert mean_777f["decode_first_step_ms"] == pytest.approx(54.47)
    assert mean_777f["prefill_engine_overhead_ms"] == pytest.approx(898.899)
    assert mean_777f["decode_handoff_overhead_ms"] == pytest.approx(439.635)

    # 3. 10s-clamped P90 P/D slice -> server_ttft_clamped=True -> all 3 sub-fields None on P90 (mean stays populated)
    srv_clamped = json.loads(json.dumps(srv_e0a7))
    srv_clamped["summary_by_role"]["decode"]["vllm_ttft_p90_ms"] = {"mean": 10000.0}
    client_clamped = json.loads(json.dumps(client))
    client_clamped["windows"]["full"]["ttft_ms"]["p90"] = 14000.0
    res_clamped = analyze_forensics(_make_task("e0a7c10a"), client_series=client_clamped, server_telemetry=srv_clamped)
    p90_clamped = res_clamped["stage_latency_breakdown"]["p90"]
    assert p90_clamped["server_ttft_clamped"] is True
    assert p90_clamped["decode_first_step_ms"] is None
    assert p90_clamped["prefill_engine_overhead_ms"] is None
    assert p90_clamped["decode_handoff_overhead_ms"] is None
    assert res_clamped["ttft_decomposition"]["p90"]["decode_first_step_ms"] is None
    assert res_clamped["stage_latency_breakdown"]["mean"]["decode_first_step_ms"] == pytest.approx(23.585)

    # 4. Aggregated run -> all 3 sub-fields are None across p50, p90, mean
    srv_agg = {
        "status": "available",
        "topology_mode": "aggregated",
        "summary": {
            "vllm_queue_time_p50_ms": {"mean": 15.0},
            "vllm_prefill_time_p50_ms": {"mean": 120.0},
            "vllm_decode_time_p50_ms": {"mean": 700.0},
            "vllm_ttft_p50_ms": {"mean": 180.0},
            "vllm_queue_time_p90_ms": {"mean": 40.0},
            "vllm_prefill_time_p90_ms": {"mean": 200.0},
            "vllm_decode_time_p90_ms": {"mean": 900.0},
            "vllm_ttft_p90_ms": {"mean": 300.0},
            "vllm_queue_time_mean_ms": {"mean": 18.0},
            "vllm_prefill_time_mean_ms": {"mean": 140.0},
            "vllm_decode_time_mean_ms": {"mean": 750.0},
            "vllm_ttft_mean_ms": {"mean": 210.0},
        },
        "series": [],
    }
    res_agg = analyze_forensics(_make_task("a9910701"), client_series=client, server_telemetry=srv_agg)
    for slice_key in ("p50", "p90", "mean"):
        for field in ("decode_first_step_ms", "prefill_engine_overhead_ms", "decode_handoff_overhead_ms"):
            assert res_agg["stage_latency_breakdown"][slice_key][field] is None
            assert res_agg["ttft_decomposition"][slice_key][field] is None

    # 5. pd_pooled_fallback -> all 3 sub-fields are None
    srv_pooled = {
        "status": "available",
        "topology_mode": "disaggregated",
        "metric_sources": {"vllm_stage_latencies": "pooled_namespace_legacy"},
        "per_pod": [{"pod": "p-0", "role": "prefill"}, {"pod": "d-0", "role": "decode"}],
        "summary": {"nixl_transfer_latency_p50_ms": {"mean": 300.0}},
        "series": [],
    }
    res_pooled = analyze_forensics(_make_task("f0011071"), client_series=client, server_telemetry=srv_pooled)
    assert res_pooled["stage_latency_breakdown"]["source"] == "pd_pooled_fallback"
    for slice_key in ("p50", "p90", "mean"):
        for field in ("decode_first_step_ms", "prefill_engine_overhead_ms", "decode_handoff_overhead_ms"):
            assert res_pooled["stage_latency_breakdown"][slice_key][field] is None
            assert res_pooled["ttft_decomposition"][slice_key][field] is None


def test_aiperf_sidecar_phase_kind_warmup_usage_fallbacks_checkpoints_and_mid_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify _parse_sidecar_line warmup phase_kind, usage_* fallbacks, checkpoints exclusion, and mid-trajectory correlation."""
    trace_root = tmp_path / "traces"
    trace_root.mkdir()
    monkeypatch.setattr(telemetry_post.BaseTrace, "trace_root", classmethod(lambda _cls: trace_root))

    # 1. _parse_sidecar_line filters phase_kind="warmup" and extracts usage_prompt_cache_read_tokens / usage_prompt_tokens
    assert (
        telemetry_post._parse_sidecar_line(
            {
                "metadata": {
                    "conversation_id": "c0",
                    "turn_index": 0,
                    "benchmark_phase": "profiling",
                    "phase_kind": "warmup",
                },
                "metrics": {"usage_prompt_tokens": {"value": 100}},
            }
        )
        is None
    )
    parsed = telemetry_post._parse_sidecar_line(
        {
            "request_id": "req-mid-1",
            "metadata": {
                "conversation_id": "c0",
                "turn_index": 2,
                "benchmark_phase": "profiling",
                "phase_kind": "profiling",
                "request_start_ns": 1_767_225_600_000_000_000,
            },
            "metrics": {
                "usage_prompt_tokens": {"value": 320},
                "usage_prompt_cache_read_tokens": {"value": 240},
            },
        }
    )
    assert parsed is not None
    assert parsed["turn"] == 3
    assert parsed["cached_tokens"] == 240.0
    assert parsed["input_sequence_length"] == 320.0

    # 2. _extract_sidecar_request_metadata skips checkpoints/ and picks shallowest non-checkpoint file
    trace_file = trace_root / "mid_traj.jsonl"
    trace_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 0.0,
                        "input_length": 100,
                        "output_length": 20,
                        "chat_id": "c0",
                        "turn": 1,
                        "hash_ids": [1, 2],
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 2.0,
                        "input_length": 200,
                        "output_length": 20,
                        "chat_id": "c0",
                        "turn": 2,
                        "hash_ids": [1, 2, 3, 4],
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 4.0,
                        "input_length": 320,
                        "output_length": 30,
                        "chat_id": "c0",
                        "turn": 3,
                        "hash_ids": [1, 2, 3, 4, 5, 6],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = _make_task("e105000c", trace_path="mid_traj.jsonl", trace_format="bailian_trace", duration_seconds=20)
    task = task.model_copy(
        update={
            "task_dir": str(tmp_path / "mid_task"),
            "simulation": task.simulation.model_copy(update={"backend": "aiperf"}),
        }
    )
    aiperf_dir = Path(task.task_dir) / "artifacts" / "aiperf"
    (aiperf_dir / "checkpoints").mkdir(parents=True)
    (aiperf_dir / "run_01").mkdir(parents=True)
    origin_ns = 1_767_225_600_000_000_000

    # Decoy in checkpoints/ must be ignored
    (aiperf_dir / "checkpoints" / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "request_id": "req-mid-1",
                "metadata": {"conversation_id": "wrong", "turn_index": 0, "request_start_ns": origin_ns},
                "metrics": {"usage_prompt_tokens": {"value": 9999}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Valid nested non-checkpoint sidecar starting at turn_index=2 (no turn==1)
    (aiperf_dir / "run_01" / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "request_id": "req-mid-1",
                "metadata": {
                    "conversation_id": "c0",
                    "turn_index": 2,
                    "benchmark_phase": "profiling",
                    "phase_kind": "profiling",
                    "request_start_ns": origin_ns,
                },
                "metrics": {"usage_prompt_tokens": {"value": 320}, "usage_prompt_cache_read_tokens": {"value": 240}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recs = [
        ArtifactRecord(
            request_id="req-mid-1",
            arrived=origin_ns / 1e9,
            completed=origin_ns / 1e9 + 0.5,
            successful=True,
            ttft_ms=65.0,
            input_tokens=320.0,
            output_tokens=30.0,
        )
    ]
    extracted = telemetry_post._extract_sidecar_request_metadata(task, recs)
    assert extracted[0]["conversation_id"] == "c0"
    assert extracted[0]["turn"] == 3
    assert extracted[0]["cached_tokens"] == 240.0

    # 3. Mid-trajectory correlation without turn == 1 succeeds safely
    ss = telemetry_post.correlate_trace_sessions(task, recs, time_origin=origin_ns / 1e9)
    assert ss["available"] is True
    assert ss["correlation"]["matched_count"] == 1
    assert ss["followup_turn_stats"]["first_turn_ttft_p50_ms"] is None
    assert ss["followup_turn_stats"]["followup_regression_pct"] is None


def test_forensics_six_slice_ttft_decomposition_new_historical_guards_and_pd_splits() -> None:
    """Verify min, p50, mean, p90, p99, max TTFT decomposition across native P99, historical fallback, guards, and P/D."""
    # 1. New run with native P99 histograms and warmup_seconds > 0 (window-P99 & window-max aggregation)
    task = _make_task("a1130001")
    task = task.model_copy(
        update={"simulation": task.simulation.model_copy(update={"backend_options": {"warmup_seconds": 10.0}})}
    )
    client = _base_forensics_client(ttft_p90=600.0, tpot_p90=20.0, e2e_p90=1600.0)
    client["warmup_seconds"] = 10.0
    client["windows"]["profiling"] = {
        "request_count": 50,
        "successful_requests": 50,
        "error_rate_pct": 0.0,
        "slo_attainment_pct": 100.0,
        "ttft_ms": {"min": 65.0, "p50": 210.0, "avg": 280.0, "p90": 600.0, "p99": 1200.0, "max": 1500.0},
        "tpot_ms": {"p90": 20.0},
        "e2e_ms": {"min": 200.0, "p50": 900.0, "avg": 1100.0, "p90": 1600.0, "p99": 2500.0, "max": 3000.0},
    }
    srv_native = {
        "status": "available",
        "topology_mode": "aggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "router": {
            "scheduler_latency_p50_ms": {"mean": 1.0},
            "scheduler_latency_p90_ms": {"mean": 2.0},
            "scheduler_latency_p99_ms": {"mean": 4.0, "p99": 4.0, "max": 5.0},
            "scheduler_latency_mean_ms": {"mean": 1.2},
        },
        "summary": {},
        "series": [
            {
                "timestamp": "1970-01-01T00:16:42Z",  # inside 10s warmup -> ignored
                "router_scheduler_latency_p99_ms": 99.0,
                "vllm_queue_time_p99_ms": 9999.0,
                "vllm_prefill_time_p99_ms": 9999.0,
                "vllm_decode_time_p99_ms": 9999.0,
                "vllm_ttft_p99_ms": 9999.0,
                "vllm_e2e_p99_ms": 9999.0,
            },
            {
                "timestamp": "1970-01-01T00:16:55Z",
                "router_scheduler_latency_p50_ms": 1.0,
                "router_scheduler_latency_p90_ms": 2.0,
                "router_scheduler_latency_p99_ms": 3.0,
                "router_scheduler_latency_mean_ms": 1.2,
                "vllm_queue_time_p50_ms": 20.0,
                "vllm_queue_time_p90_ms": 50.0,
                "vllm_queue_time_p99_ms": 100.0,
                "vllm_queue_time_mean_ms": 30.0,
                "vllm_prefill_time_p50_ms": 170.0,
                "vllm_prefill_time_p90_ms": 500.0,
                "vllm_prefill_time_p99_ms": 800.0,
                "vllm_prefill_time_mean_ms": 220.0,
                "vllm_decode_time_p50_ms": 690.0,
                "vllm_decode_time_p90_ms": 1000.0,
                "vllm_decode_time_p99_ms": 1200.0,
                "vllm_decode_time_mean_ms": 800.0,
                "vllm_ttft_p50_ms": 200.0,
                "vllm_ttft_p90_ms": 580.0,
                "vllm_ttft_p99_ms": 1150.0,
                "vllm_ttft_mean_ms": 265.0,
                "vllm_e2e_p50_ms": 890.0,
                "vllm_e2e_p90_ms": 1580.0,
                "vllm_e2e_p99_ms": 2400.0,
                "vllm_e2e_mean_ms": 1070.0,
            },
            {
                "timestamp": "1970-01-01T00:17:05Z",
                "router_scheduler_latency_p50_ms": 1.0,
                "router_scheduler_latency_p90_ms": 2.0,
                "router_scheduler_latency_p99_ms": 5.0,
                "router_scheduler_latency_mean_ms": 1.2,
                "vllm_queue_time_p50_ms": 20.0,
                "vllm_queue_time_p90_ms": 50.0,
                "vllm_queue_time_p99_ms": 300.0,
                "vllm_queue_time_mean_ms": 30.0,
                "vllm_prefill_time_p50_ms": 170.0,
                "vllm_prefill_time_p90_ms": 500.0,
                "vllm_prefill_time_p99_ms": 850.0,
                "vllm_prefill_time_mean_ms": 220.0,
                "vllm_decode_time_p50_ms": 690.0,
                "vllm_decode_time_p90_ms": 1000.0,
                "vllm_decode_time_p99_ms": 1400.0,
                "vllm_decode_time_mean_ms": 800.0,
                "vllm_ttft_p50_ms": 200.0,
                "vllm_ttft_p90_ms": 580.0,
                "vllm_ttft_p99_ms": 1450.0,
                "vllm_ttft_mean_ms": 265.0,
                "vllm_e2e_p50_ms": 890.0,
                "vllm_e2e_p90_ms": 1580.0,
                "vllm_e2e_p99_ms": 2900.0,
                "vllm_e2e_mean_ms": 1070.0,
            },
        ],
    }
    res = analyze_forensics(task, client_series=client, server_telemetry=srv_native)
    slb = res["stage_latency_breakdown"]
    td = res["ttft_decomposition"]
    assert slb["caveat"] is None  # slice_note must NOT hoist into top-level caveat
    for key in ("min", "p50", "mean", "p90", "p99", "max"):
        assert slb[key]["available"] is True
        assert td[key]["available"] is True
    assert td["client_ttft_min_ms"] == pytest.approx(65.0)
    assert td["client_ttft_mean_ms"] == pytest.approx(280.0)
    assert td["client_ttft_p99_ms"] == pytest.approx(1200.0)
    assert td["client_ttft_max_ms"] == pytest.approx(1500.0)

    # Min: exact client min, no fabricated stage splits
    assert td["min"]["ttft_unattributed_ms"] == pytest.approx(65.0)
    assert td["min"]["epp_scheduling_ms"] is None
    assert td["min"]["queue_time_ms"] is None
    assert td["min"]["prefill_time_ms"] is None
    assert "single-request minimums" in (td["min"]["slice_note"] or "")

    # P99 uses window-P99 (300 + 850 + 5 = 1155 <= 1200) and native P99 has no slice_note
    assert td["p99"]["queue_time_ms"] == pytest.approx(300.0)
    assert td["p99"]["prefill_time_ms"] == pytest.approx(850.0)
    assert td["p99"]["epp_scheduling_ms"] == pytest.approx(5.0)
    assert td["p99"]["engine_overhead_ms"] == pytest.approx(45.0)
    assert td["p99"]["gateway_overhead_ms"] == pytest.approx(0.0)
    assert td["p99"]["slice_note"] is None

    # Max uses window-max, sets slice_note
    assert td["max"]["ttft_stage_ms"] == pytest.approx(1500.0)
    assert "per-request max" in (td["max"]["slice_note"] or "")

    # 2. Historical schema-6 run: missing *_mean_ms & *_p99_ms, plus unbounded-tail guard on max
    srv_hist = {
        "status": "available",
        "topology_mode": "aggregated",
        "window": {"start": "1970-01-01T00:16:40Z"},
        "router": {"scheduler_latency_p95_ms": {"mean": 3.0}},
        "summary": {},
        "series": [
            {
                "timestamp": "1970-01-01T00:16:55Z",
                "router_scheduler_latency_p95_ms": 2.0,
                "vllm_queue_time_p50_ms": 15.0,
                "vllm_queue_time_p90_ms": 100.0,
                "vllm_prefill_time_p50_ms": 150.0,
                "vllm_prefill_time_p90_ms": 300.0,
                "vllm_decode_time_p50_ms": 600.0,
                "vllm_decode_time_p90_ms": 900.0,
                "vllm_ttft_p99_ms": 600.0,
            },
            {
                "timestamp": "1970-01-01T00:17:05Z",
                "router_scheduler_latency_p95_ms": 4.0,
                "vllm_queue_time_p50_ms": 15.0,
                "vllm_queue_time_p90_ms": 400.0,
                "vllm_prefill_time_p50_ms": 150.0,
                "vllm_prefill_time_p90_ms": 700.0,
                "vllm_decode_time_p50_ms": 600.0,
                "vllm_decode_time_p90_ms": 1100.0,
                "vllm_ttft_p99_ms": 1200.0,
            },
        ],
    }
    client_hist = json.loads(json.dumps(client))
    client_hist["windows"]["profiling"]["ttft_ms"]["max"] = 2500.0  # > 1200 + 500 -> unbounded tail
    res_hist = analyze_forensics(task, client_series=client_hist, server_telemetry=srv_hist)
    td_hist = res_hist["ttft_decomposition"]
    assert res_hist["stage_latency_breakdown"]["mean"]["source"] == "client_fallback"
    assert td_hist["mean"]["available"] is True
    assert td_hist["mean"]["ttft_unattributed_ms"] == pytest.approx(280.0)
    assert "Mean TTFT" in (td_hist["mean"]["slice_note"] or "")
    assert "peak-window P90" in (td_hist["p99"]["slice_note"] or "")
    assert td_hist["p99"]["queue_time_ms"] == pytest.approx(400.0)
    assert td_hist["p99"]["prefill_time_ms"] == pytest.approx(700.0)
    assert td_hist["max"]["gateway_overhead_ms"] is None
    assert td_hist["max"]["engine_overhead_ms"] == pytest.approx(2500.0 - (4.0 + 400.0 + 700.0))

    # 3. Non-additive over-allocation guard on p99 when epp + queue + prefill > client_ttft_p99
    client_over = json.loads(json.dumps(client))
    client_over["windows"]["profiling"]["ttft_ms"]["p99"] = 1000.0  # < 5 + 300 + 850 = 1155
    res_over = analyze_forensics(task, client_series=client_over, server_telemetry=srv_native)
    p99_over = res_over["ttft_decomposition"]["p99"]
    assert p99_over["ttft_stage_ms"] == pytest.approx(1000.0)
    assert p99_over["engine_overhead_ms"] == pytest.approx(0.0)
    assert p99_over["gateway_overhead_ms"] == pytest.approx(0.0)

    # 4. P99 10s clamping triggers top-level engine-neutral caveat even when P90 is unclamped, and suppresses P/D sub-split
    client_clamp = _base_forensics_client(ttft_p90=3000.0, tpot_p90=25.0, e2e_p90=12000.0)
    client_clamp["windows"]["full"]["ttft_ms"] = {
        "min": 150.0,
        "p50": 800.0,
        "avg": 1500.0,
        "p90": 3000.0,
        "p99": 12000.0,
        "max": 14000.0,
    }
    client_clamp["windows"]["full"]["e2e_ms"] = {
        "min": 500.0,
        "p50": 5000.0,
        "avg": 7000.0,
        "p90": 12000.0,
        "p99": 20000.0,
        "max": 22000.0,
    }
    srv_pd_p99 = _pd_base_server(
        metric_sources={"vllm_stage_latencies": "role_separated"},
        summary={
            "queue_depth": {"mean": 1.0, "max": 4.0},
            "kv_cache_usage_percent": {"mean": 30.0, "max": 40.0},
            "nixl_transfer_latency_mean_ms": {"mean": 500.0},
            "nixl_transfer_latency_p50_ms": {"mean": 400.0},
            "nixl_transfer_latency_p90_ms": {"mean": 1000.0},
            "nixl_transfer_latency_p99_ms": {"mean": 1800.0, "p99": 1800.0, "max": 2000.0},
            "vllm_ttft_mean_ms": {"mean": 1400.0},
            "vllm_ttft_p50_ms": {"mean": 780.0},
            "vllm_ttft_p90_ms": {"mean": 2900.0},
            "vllm_ttft_p99_ms": {"mean": 9900.0, "p99": 9900.0, "max": 10000.0},
        },
        summary_by_role={
            "prefill": {
                "vllm_queue_time_mean_ms": {"mean": 2.0},
                "vllm_queue_time_p50_ms": {"mean": 150.0},
                "vllm_queue_time_p90_ms": {"mean": 270.0},
                "vllm_queue_time_p99_ms": {"mean": 297.0, "p99": 297.0, "max": 297.0},
                "vllm_prefill_time_mean_ms": {"mean": 300.0},
                "vllm_prefill_time_p50_ms": {"mean": 250.0},
                "vllm_prefill_time_p90_ms": {"mean": 800.0},
                "vllm_prefill_time_p99_ms": {"mean": 2000.0, "p99": 2000.0, "max": 2200.0},
                "vllm_ttft_mean_ms": {"mean": 500.0},
                "vllm_ttft_p50_ms": {"mean": 350.0},
                "vllm_ttft_p90_ms": {"mean": 1200.0},
                "vllm_ttft_p99_ms": {"mean": 3000.0, "p99": 3000.0, "max": 3500.0},
            },
            "decode": {
                "vllm_queue_time_mean_ms": {"mean": 500.0},
                "vllm_queue_time_p50_ms": {"mean": 400.0},
                "vllm_queue_time_p90_ms": {"mean": 1100.0},
                "vllm_queue_time_p99_ms": {"mean": 2200.0, "p99": 2200.0, "max": 2500.0},
                "vllm_prefill_time_mean_ms": {"mean": 20.0},
                "vllm_prefill_time_p50_ms": {"mean": 20.0},
                "vllm_prefill_time_p90_ms": {"mean": 100.0},
                "vllm_prefill_time_p99_ms": {"mean": 200.0, "p99": 200.0, "max": 250.0},
                "vllm_decode_time_mean_ms": {"mean": 5500.0},
                "vllm_decode_time_p50_ms": {"mean": 4000.0},
                "vllm_decode_time_p90_ms": {"mean": 8000.0},
                "vllm_decode_time_p99_ms": {"mean": 10000.0, "p99": 10000.0, "max": 11000.0},
                "vllm_ttft_mean_ms": {"mean": 900.0},
                "vllm_ttft_p50_ms": {"mean": 430.0},
                "vllm_ttft_p90_ms": {"mean": 1700.0},
                "vllm_ttft_p99_ms": {"mean": 9900.0, "p99": 9900.0, "max": 9900.0},
            },
        },
    )
    res_pd_clamp = analyze_forensics(_make_task("a1130d01"), client_series=client_clamp, server_telemetry=srv_pd_p99)
    slb_pd = res_pd_clamp["stage_latency_breakdown"]
    assert slb_pd["p90"]["server_ttft_clamped"] is False
    assert slb_pd["p99"]["server_ttft_clamped"] is True
    assert slb_pd["p99"]["gateway_overhead_ms"] is None
    assert slb_pd["p99"]["decode_first_step_ms"] is None
    assert slb_pd["caveat"] is not None and "vLLM" not in slb_pd["caveat"]
    assert slb_pd["role_breakdown"]["prefill"]["queue_p99_ms"] == pytest.approx(297.0)
    assert slb_pd["role_breakdown"]["decode"]["queue_excl_kv_p99_ms"] == pytest.approx(400.0)

    # Unclamped PD-role P99 and bounded Max cleanly sub-split engine overhead and bypass 297ms prefill queue floor
    srv_pd_ok = json.loads(json.dumps(srv_pd_p99))
    srv_pd_ok["summary"]["vllm_ttft_p99_ms"] = {"mean": 6000.0, "p99": 6000.0, "max": 7000.0}
    srv_pd_ok["summary_by_role"]["decode"]["vllm_ttft_p99_ms"] = {"mean": 3000.0, "p99": 3000.0, "max": 3500.0}
    client_pd_ok = json.loads(json.dumps(client_clamp))
    client_pd_ok["windows"]["full"]["ttft_ms"]["p99"] = 6100.0
    client_pd_ok["windows"]["full"]["ttft_ms"]["max"] = 7200.0  # <= 7000 + 500 -> bounded!
    res_pd_ok = analyze_forensics(_make_task("a1130d02"), client_series=client_pd_ok, server_telemetry=srv_pd_ok)
    p99_pd = res_pd_ok["ttft_decomposition"]["p99"]
    max_pd = res_pd_ok["ttft_decomposition"]["max"]
    assert p99_pd["queue_time_ms"] == pytest.approx(402.0)  # prefill_eff(2.0) + (2200 - 1800)
    assert p99_pd["decode_first_step_ms"] is not None
    assert max_pd["decode_first_step_ms"] is not None
    assert max_pd["gateway_overhead_ms"] == pytest.approx(200.0)


def test_bucket_stats_include_min_and_max_for_ttft_tpot_e2e() -> None:
    """Bucket ttft/tpot/e2e/drift_ms expose all 6 stats (min, p50, mean, p90, p99, max) with expected ordering."""
    assert telemetry_post._compact_bucket_stats([]) == {
        "min": None,
        "p50": None,
        "mean": None,
        "p90": None,
        "p99": None,
        "max": None,
    }
    assert telemetry_post._compact_bucket_stats([10.1234, 20.5678, 30.9999]) == {
        "min": 10.123,
        "p50": 20.568,
        "mean": 20.564,
        "p90": 31.0,
        "p99": 31.0,
        "max": 31.0,
    }

    origin = 1_795_597_000.0
    task = _make_task("01140001", duration_seconds=10)
    records = [
        ArtifactRecord(
            request_id=f"r-{i}",
            arrived=origin + 0.5 + i * 0.2,
            completed=origin + 1.0 + i * 0.3,
            successful=True,
            input_tokens=100,
            output_tokens=20,
            requested_output_tokens=20,
            ttft_ms=ttft,
            tpot_ms=tpot,
            latency_ms=e2e,
            s_time_drift_ms=drift,
        )
        for i, (ttft, tpot, e2e, drift) in enumerate(
            [
                (120.125, 10.5, 330.25, 2.0),
                (210.5, 15.25, 515.5, 5.0),
                (350.0, 22.0, 790.0, 8.0),
                (480.75, 28.5, 1050.75, 11.0),
                (990.333, 45.125, 1892.833, 19.5),
            ]
        )
    ]
    cs = telemetry_post.build_client_series(task, records)
    assert cs["schema_version"] == telemetry_post.REPORT_SCHEMA_VERSION == 15
    b0 = cs["buckets"][0]
    for metric_key, expected_min, expected_max in (
        ("ttft", 120.125, 990.333),
        ("tpot", 10.5, 45.125),
        ("e2e", 330.25, 1892.833),
        ("drift_ms", 2.0, 19.5),
    ):
        stats = b0[metric_key]
        assert set(stats.keys()) == {"min", "p50", "mean", "p90", "p99", "max"}
        assert stats["min"] == pytest.approx(expected_min)
        assert stats["max"] == pytest.approx(expected_max)
        assert stats["min"] <= stats["p50"] <= stats["p90"] <= stats["p99"] <= stats["max"]
        assert stats["min"] <= stats["mean"] <= stats["max"]


@pytest.mark.asyncio
async def test_service_get_task_telemetry_rebuilds_stale_v10_client_series_to_v11(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale v10 client-series.json (lacking min/max) is rebuilt to v11 when profile_export.jsonl exists."""
    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        task_id = "0114a101"
        task = _make_task(task_id, duration_seconds=10)
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 3, "successful_requests": 3, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        backend_dir = task_dir / "artifacts" / "aiperf"
        report_dir = task_dir / "artifacts" / "report"
        backend_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        origin_ns = 1_795_597_273_000_000_000
        req_lines = [
            {
                "metadata": {
                    "request_id": f"r-{i}",
                    "benchmark_phase": "profiling",
                    "request_start_ns": origin_ns + (200 + i * 300) * 1_000_000,
                    "request_end_ns": origin_ns + (1200 + i * 500) * 1_000_000,
                    "credit_issued_ns": origin_ns + (195 + i * 300) * 1_000_000,
                    "status_code": 200,
                },
                "metrics": {
                    "input_sequence_length": {"value": 128},
                    "output_sequence_length": {"value": 32},
                    "time_to_first_token": {"value": 100.0 + i * 150.0},
                    "inter_token_latency": {"value": 15.0 + i * 10.0},
                    "request_latency": {"value": 600.0 + i * 400.0},
                },
            }
            for i in range(3)
        ]
        (backend_dir / "profile_export.jsonl").write_text(
            "\n".join(json.dumps(line) for line in req_lines) + "\n",
            encoding="utf-8",
        )

        stale_v10 = {
            "schema_version": 10,
            "warmup_seconds": 0.0,
            "duration_seconds": 10.0,
            "bucket_seconds": 5,
            "buckets": [
                {
                    "index": 0,
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "mid_seconds": 2.5,
                    "ttft": {"p50": 250.0, "p90": 400.0, "p99": 400.0, "mean": 250.0},
                    "tpot": {"p50": 25.0, "p90": 35.0, "p99": 35.0, "mean": 25.0},
                    "e2e": {"p50": 1000.0, "p90": 1400.0, "p99": 1400.0, "mean": 1000.0},
                }
            ],
            "windows": {"full": {"request_count": 3, "successful_requests": 3, "error_rate_pct": 0.0}},
        }
        (report_dir / "client-series.json").write_text(json.dumps(stale_v10), encoding="utf-8")
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": telemetry_post.FORENSICS_SCHEMA_VERSION, "verdict": {"code": "healthy"}}),
            encoding="utf-8",
        )
        await service.save_task(task)

        tel = await service.get_task_telemetry(task_id)
        cs_v11 = tel["client_series"]
        assert cs_v11["schema_version"] == 15
        b0 = cs_v11["buckets"][0]
        assert b0["ttft"]["min"] == 100.0
        assert b0["ttft"]["max"] == 400.0
        assert b0["tpot"]["min"] == 15.0
        assert b0["tpot"]["max"] == 35.0
        assert b0["e2e"]["min"] == 600.0
        assert b0["e2e"]["max"] == 1400.0

        # When profile_export.jsonl is absent, legacy v10 client-series.json is preserved gracefully
        (backend_dir / "profile_export.jsonl").unlink()
        (report_dir / "client-series.json").write_text(json.dumps(stale_v10), encoding="utf-8")
        tel_fallback = await service.get_task_telemetry(task_id)
        assert tel_fallback["client_series"]["buckets"][0]["ttft"]["p50"] == 250.0
    finally:
        service.reset_service_state()


def test_workload_profile_edge_cases_zero_one_two_zero_tokens_failed_and_missing_ts() -> None:
    """Workload builder handles 0/1/2 requests, ISL=0/OSL=0, all-failed, and missing timestamps safely."""
    # 0 requests
    task_0 = _make_task("a0000000", duration_seconds=10)
    cs_0 = telemetry_post.build_client_series(task_0, [])
    assert cs_0["schema_version"] == 15
    assert all(b["in_flight_avg"] is None and b["in_flight_max"] is None for b in cs_0["buckets"])
    w0 = cs_0["workload"]
    assert w0["fallback_to_full"] is True
    assert w0["fallback_synthesized"] is False
    f0 = w0["windows"]["full"]
    assert f0["summary"]["counts"] == {"completed": 0, "failed": 0, "total": 0}
    assert f0["summary"]["isl"]["min"] is None
    assert f0["summary"]["osl"]["mean"] is None
    assert f0["summary"]["osl_isl_ratio"]["sample_count"] == 0
    assert f0["summary"]["concurrency"]["mean"] is None
    assert f0["summary"]["concurrency"]["peak"] is None
    assert f0["summary"]["arrivals"]["mean_arrival_rps"] is None
    assert f0["isl_histogram"] == []
    assert f0["isl_cdf"] == []
    assert f0["concurrency_residency"] == []
    assert f0["multi_turn"]["available"] is False
    assert f0["multi_turn"]["is_multi_turn"] is False

    # 1 request -> cv=0.0, single-bin 100% histogram, inter_arrival_cv=None
    r_one = ArtifactRecord(
        request_id="r-1",
        arrived=1.0,
        completed=3.0,
        successful=True,
        input_tokens=512,
        output_tokens=128,
    )
    cs_1 = telemetry_post.build_client_series(_make_task("a0000001", duration_seconds=10), [r_one])
    f1 = cs_1["workload"]["windows"]["full"]
    assert f1["summary"]["isl"]["min"] == f1["summary"]["isl"]["max"] == 512
    assert f1["summary"]["isl"]["cv"] == 0.0
    assert f1["summary"]["osl"]["cv"] == 0.0
    assert f1["summary"]["arrivals"]["inter_arrival_cv"] is None
    assert len(f1["isl_histogram"]) == 1
    assert f1["isl_histogram"][0]["share_pct"] == 100.0
    assert f1["isl_cdf"][0]["value"] == 512
    assert f1["isl_cdf"][-1]["value"] == 512

    # 2 requests + ISL=0 + OSL=0 + all failed + missing timestamps
    r_zero_isl = ArtifactRecord(
        request_id="r-z1",
        arrived=None,
        completed=None,
        successful=False,
        error="upstream 500",
        input_tokens=0,
        output_tokens=40,
    )
    r_zero_osl = ArtifactRecord(
        request_id="r-z2",
        arrived=None,
        completed=None,
        successful=False,
        error="cancelled",
        input_tokens=100,
        output_tokens=0,
    )
    cs_2 = telemetry_post.build_client_series(_make_task("a0000002", duration_seconds=10), [r_zero_isl, r_zero_osl])
    f2 = cs_2["workload"]["windows"]["full"]
    assert f2["summary"]["counts"] == {"completed": 0, "failed": 2, "total": 2}
    assert f2["summary"]["isl"]["min"] == 0
    assert f2["summary"]["osl"]["min"] == 0
    assert f2["summary"]["osl_isl_ratio"]["sample_count"] == 1
    assert f2["summary"]["osl_isl_ratio"]["excluded_zero_isl_count"] == 1
    assert f2["summary"]["osl_isl_ratio"]["regimes"]["prefill_heavy"]["count"] == 1
    # Missing timestamps -> concurrency scalars and bucket sweep metrics MUST be None (not 0)
    assert f2["summary"]["concurrency"]["mean"] is None
    assert f2["summary"]["concurrency"]["peak"] is None
    assert f2["concurrency_residency"] == []
    assert all(b["in_flight_avg"] is None and b["in_flight_max"] is None for b in cs_2["buckets"])

    # All-zero ISL -> cv is None and ratio sample_count is 0
    cs_zero_only = telemetry_post.build_client_series(
        _make_task("a0000003", duration_seconds=10),
        [
            ArtifactRecord(
                request_id="r0", arrived=1.0, completed=2.0, successful=True, input_tokens=0, output_tokens=10
            )
        ],
    )
    fz = cs_zero_only["workload"]["windows"]["full"]
    assert fz["summary"]["isl"]["cv"] is None
    assert fz["summary"]["osl_isl_ratio"]["sample_count"] == 0
    assert fz["summary"]["osl_isl_ratio"]["regimes"]["balanced"]["share_pct"] is None


def test_workload_concurrency_sweep_half_open_boundaries_point_intervals_and_idle_steady() -> None:
    """Event sweep enforces half-open [start_s, end_s), point intervals, warmup carry-over, and idle vs missing."""
    # Case A: Half-open boundary & back-to-back non-overlapping requests [0, 5] and [5, 10]
    task_a = _make_task("ac000001", duration_seconds=10, backend_options={"warmup_seconds": 5.0})
    recs_a = [
        ArtifactRecord(
            request_id="a0", arrived=0.0, completed=5.0, successful=True, input_tokens=100, output_tokens=20
        ),
        ArtifactRecord(
            request_id="a1", arrived=5.0, completed=10.0, successful=True, input_tokens=100, output_tokens=20
        ),
    ]
    cs_a = telemetry_post.build_client_series(task_a, recs_a)
    assert len(cs_a["buckets"]) == 2
    assert cs_a["buckets"][0]["in_flight_avg"] == 1.0
    assert cs_a["buckets"][0]["in_flight_max"] == 1
    assert cs_a["buckets"][1]["in_flight_avg"] == 1.0
    assert cs_a["buckets"][1]["in_flight_max"] == 1
    full_a = cs_a["workload"]["windows"]["full"]
    assert full_a["summary"]["concurrency"]["mean"] == 1.0
    assert full_a["summary"]["concurrency"]["peak"] == 1
    assert full_a["summary"]["concurrency"]["p50"] == 1

    # Case B: Bucket 0 empty when request is strictly [5.0, 10.0]; profiling steady-state idle when request is [0.0, 4.0]
    task_b = _make_task("ac000002", duration_seconds=10, backend_options={"warmup_seconds": 5.0})
    cs_b_second = telemetry_post.build_client_series(
        task_b,
        [
            ArtifactRecord(
                request_id="b1", arrived=5.0, completed=10.0, successful=True, input_tokens=100, output_tokens=20
            )
        ],
    )
    assert cs_b_second["buckets"][0]["in_flight_avg"] == 0.0
    assert cs_b_second["buckets"][0]["in_flight_max"] == 0
    assert cs_b_second["buckets"][1]["in_flight_avg"] == 1.0
    assert cs_b_second["buckets"][1]["in_flight_max"] == 1

    cs_b_idle_steady = telemetry_post.build_client_series(
        task_b,
        [
            ArtifactRecord(
                request_id="b0", arrived=0.0, completed=4.0, successful=True, input_tokens=100, output_tokens=20
            )
        ],
        arrival_schedule=[10.0],
    )
    prof_idle = cs_b_idle_steady["workload"]["windows"]["profiling"]
    assert prof_idle["summary"]["concurrency"]["mean"] == 0.0
    assert prof_idle["summary"]["concurrency"]["peak"] == 0
    assert prof_idle["summary"]["concurrency"]["p50"] == 0
    assert prof_idle["summary"]["concurrency"]["active_duration_seconds"] == 0.0
    assert prof_idle["concurrency_residency"] == [
        {"min_level": 0, "max_level": 0, "label": "0 (idle)", "duration_seconds": 5.0, "share_pct": 100.0}
    ]

    # Case C: Warmup-born request [2.0, 8.0] carries into profiling [5.0, 10.0] -> mean 0.6, peak 1
    cs_carry = telemetry_post.build_client_series(
        task_b,
        [
            ArtifactRecord(
                request_id="c0", arrived=2.0, completed=8.0, successful=True, input_tokens=200, output_tokens=50
            )
        ],
        arrival_schedule=[10.0],
    )
    prof_carry = cs_carry["workload"]["windows"]["profiling"]
    assert prof_carry["summary"]["concurrency"]["mean"] == pytest.approx(0.6, abs=1e-4)
    assert prof_carry["summary"]["concurrency"]["peak"] == 1
    assert prof_carry["summary"]["concurrency"]["active_duration_seconds"] == pytest.approx(3.0, abs=1e-3)

    # Case D: Zero-duration point intervals at t=2.0 (2 concurrent) -> peak=2, mean=0.0, 100% idle residency
    cs_pt = telemetry_post.build_client_series(
        _make_task("ac000003", duration_seconds=10),
        [
            ArtifactRecord(
                request_id="p0", arrived=2.0, completed=2.0, successful=True, input_tokens=50, output_tokens=10
            ),
            ArtifactRecord(
                request_id="p1", arrived=2.0, completed=2.0, successful=True, input_tokens=60, output_tokens=10
            ),
        ],
    )
    assert cs_pt["buckets"][0]["in_flight_avg"] == 0.0
    assert cs_pt["buckets"][0]["in_flight_max"] == 2
    f_pt = cs_pt["workload"]["windows"]["full"]
    assert f_pt["summary"]["concurrency"]["mean"] == 0.0
    assert f_pt["summary"]["concurrency"]["peak"] == 2
    assert f_pt["summary"]["concurrency"]["p50"] == 0
    assert f_pt["concurrency_residency"][0]["label"] == "0 (idle)"
    assert f_pt["concurrency_residency"][0]["share_pct"] == 100.0


def test_workload_distributions_multiturn_prefix_clamping_and_redaction() -> None:
    """Adaptive histograms, ratio regimes, multi-turn profiling consistency, clamping <= 100%, and redaction safety."""
    task = _make_task("ae000001", duration_seconds=20, backend_options={"warmup_seconds": 5.0})
    records: list[SimpleNamespace] = [
        # Warmup Turn 1 of session s1 (4-turn session whose turns 2-4 are in profiling)
        SimpleNamespace(
            request_id="m-0",
            arrived=1.0,
            completed=4.0,
            successful=True,
            issue_failed=False,
            failed_issue=False,
            input_tokens=128,
            output_tokens=64,
            requested_output_tokens=64,
            rate_tokens=64,
            ttft_ms=50.0,
            tpot_ms=10.0,
            tpot_is_measured=True,
            latency_ms=690.0,
            s_time_drift_ms=1.0,
            session_id="s1",
            turn=1,
            cached_tokens=0,
        ),
        # Profiling Turn 2 of s1: over-reported cached_tokens=9999 must clamp to input_tokens=512 (<=100%)
        SimpleNamespace(
            request_id="m-1",
            arrived=6.0,
            completed=9.0,
            successful=True,
            issue_failed=False,
            failed_issue=False,
            input_tokens=512,
            output_tokens=512,  # balanced (ratio 1.0)
            requested_output_tokens=512,
            rate_tokens=512,
            ttft_ms=60.0,
            tpot_ms=12.0,
            tpot_is_measured=True,
            latency_ms=1000.0,
            s_time_drift_ms=2.0,
            session_id="s1",
            turn=2,
            cached_tokens=9999,
        ),
        # Profiling Turn 3 of s1: decode_heavy (ratio > 1.0)
        SimpleNamespace(
            request_id="m-2",
            arrived=8.0,
            completed=12.0,
            successful=True,
            issue_failed=False,
            failed_issue=False,
            input_tokens=2048,
            output_tokens=4096,
            requested_output_tokens=4096,
            rate_tokens=4096,
            ttft_ms=90.0,
            tpot_ms=15.0,
            tpot_is_measured=True,
            latency_ms=1500.0,
            s_time_drift_ms=3.0,
            session_id="s1",
            turn=3,
            cached_tokens=1024,
        ),
        # Profiling Turn 4 of s1: prefill_heavy (ratio < 0.25), heavy-tail ISL 32768
        SimpleNamespace(
            request_id="m-3",
            arrived=11.0,
            completed=16.0,
            successful=True,
            issue_failed=False,
            failed_issue=False,
            input_tokens=32768,
            output_tokens=256,
            requested_output_tokens=256,
            rate_tokens=256,
            ttft_ms=200.0,
            tpot_ms=20.0,
            tpot_is_measured=True,
            latency_ms=2000.0,
            s_time_drift_ms=4.0,
            session_id="s1",
            turn=4,
            cached_tokens=2000,
        ),
    ]
    # Add high concurrency burst of 16 requests in session s2 (single turn) to exercise >12 peak residency binning
    for idx in range(16):
        records.append(
            SimpleNamespace(
                request_id=f"c-{idx}",
                arrived=13.0,
                completed=15.0,
                successful=True,
                issue_failed=False,
                failed_issue=False,
                input_tokens=256,
                output_tokens=64,  # ratio 0.25 -> balanced
                requested_output_tokens=64,
                rate_tokens=64,
                ttft_ms=40.0,
                tpot_ms=10.0,
                tpot_is_measured=True,
                latency_ms=500.0,
                s_time_drift_ms=1.0,
                session_id=f"single-{idx}",
                turn=1,
                cached_tokens=0,
            )
        )

    cs = telemetry_post.build_client_series(task, records)
    prof = cs["workload"]["windows"]["profiling"]
    full = cs["workload"]["windows"]["full"]
    assert cs["workload"]["fallback_to_full"] is False

    # Heavy-tail ISL histogram uses clean power-of-2 labels (<= 12 bins)
    assert 1 <= len(full["isl_histogram"]) <= 12
    assert any("K" in b["label"] for b in full["isl_histogram"])

    # Regime shares sum cleanly
    regimes = prof["summary"]["osl_isl_ratio"]["regimes"]
    assert regimes["prefill_heavy"]["count"] == 1
    assert regimes["balanced"]["count"] == 17  # m-1 (1.0) + 16 single-* (0.25)
    assert regimes["decode_heavy"]["count"] == 1

    # High-concurrency residency binning (> 12 peak -> <= 12 bins)
    assert prof["summary"]["concurrency"]["peak"] == 17
    assert len(prof["concurrency_residency"]) <= 12

    # Profiling multi-turn consistency: s1 had Turn 1 in warmup, Turns 2-4 in profiling -> still 4 turns
    mt = prof["multi_turn"]
    assert mt["is_multi_turn"] is True
    assert mt["max_turns"] == 4
    s1_dist = {item["turns"]: item["sessions"] for item in mt["turns_distribution"]}
    assert s1_dist == {1: 16, 4: 1}
    assert [item["label"] for item in mt["isl_by_turn"]] == ["Turn 1", "Turn 2", "Turn 3", "Turn 4"]
    assert [item["request_count"] for item in mt["isl_by_turn"]] == [16, 1, 1, 1]

    # Reusable prefix clamping <= 100%
    assert mt["reusable_prefix_tokens"] <= prof["summary"]["total_input_tokens"]
    assert mt["reusable_prefix_pct"] <= 100.0
    assert all(
        item["reusable_prefix_pct"] is None or item["reusable_prefix_pct"] <= 100.0 for item in mt["isl_by_turn"]
    )

    # Redactability: every key under workload passes redact_manifest_payload unchanged
    redacted_workload = telemetry_post.redact_manifest_payload(cs["workload"])
    assert redacted_workload == cs["workload"]

    # Single-turn trace -> informative non-multi-turn state
    cs_single = telemetry_post.build_client_series(
        _make_task("ae000002", duration_seconds=10),
        [
            ArtifactRecord(
                request_id="s-0", arrived=1.0, completed=2.0, successful=True, input_tokens=100, output_tokens=50
            ),
            ArtifactRecord(
                request_id="s-1", arrived=3.0, completed=4.0, successful=True, input_tokens=200, output_tokens=80
            ),
        ],
    )
    mt_single = cs_single["workload"]["windows"]["full"]["multi_turn"]
    assert mt_single["is_multi_turn"] is False
    assert mt_single["isl_by_turn"] == []
    assert mt_single["reason"]


@pytest.mark.asyncio
async def test_service_workload_section_and_legacy_fallback_preserves_schema_and_buckets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy v10/v11 runs without profile_export.jsonl synthesize workload safely across telemetry and artifacts."""
    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        task_id = "0114b202"
        task = _make_task(task_id, duration_seconds=20, backend_options={"warmup_seconds": 5.0})
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 10, "successful_requests": 10, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        report_dir = task_dir / "artifacts" / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        legacy_cs = {
            "schema_version": 10,
            "warmup_seconds": 5.0,
            "duration_seconds": 20.0,
            "bucket_seconds": 5,
            "buckets": [
                {
                    "index": 0,
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "mid_seconds": 2.5,
                    "is_warmup": True,
                    "arrived_requests": 3,
                    "arrived_rps": 0.6,
                    "completed_requests": 2,
                    "throughput_rps": 0.4,
                    "in_flight_concurrency": 2,
                    "reusable_prefix_tokens": 500,
                },
                {
                    "index": 1,
                    "start_seconds": 5,
                    "end_seconds": 20,
                    "mid_seconds": 12.5,
                    "is_warmup": False,
                    "arrived_requests": 7,
                    "arrived_rps": 0.4667,
                    "completed_requests": 8,
                    "throughput_rps": 0.5333,
                    "in_flight_concurrency": 4,
                    "reusable_prefix_tokens": 99999,
                },
            ],
            "windows": {
                "full": {
                    "request_count": 10,
                    "successful_requests": 10,
                    "failed_requests": 0,
                    "duration_seconds": 20.0,
                    "total_input_tokens": 4000,
                    "total_output_tokens": 1000,
                    "input_tokens": {
                        "avg": 400.0,
                        "min": 100.0,
                        "p50": 350.0,
                        "p90": 700.0,
                        "p99": 800.0,
                        "max": 800.0,
                    },
                    "output_tokens": {"avg": 100.0, "min": 20.0, "p50": 90.0, "p90": 180.0, "p99": 200.0, "max": 200.0},
                },
                "profiling": {
                    "request_count": 7,
                    "successful_requests": 7,
                    "failed_requests": 0,
                    "duration_seconds": 15.0,
                    "total_input_tokens": 2800,
                    "total_output_tokens": 700,
                    "input_tokens": {
                        "avg": 400.0,
                        "min": 100.0,
                        "p50": 350.0,
                        "p90": 700.0,
                        "p99": 800.0,
                        "max": 800.0,
                    },
                    "output_tokens": {"avg": 100.0, "min": 20.0, "p50": 90.0, "p90": 180.0, "p99": 200.0, "max": 200.0},
                },
            },
            "prompt_length_buckets": [{"label": "<512", "count": 8}, {"label": "512-2K", "count": 2}],
        }
        legacy_sessions = {
            "available": True,
            "turn_distribution": [{"turns": 3, "sessions": 2}, {"turns": 12, "sessions": 1}],
            "reusable_prefix_tokens": 3500,
        }
        (report_dir / "client-series.json").write_text(json.dumps(legacy_cs), encoding="utf-8")
        (report_dir / "sessions-summary.json").write_text(json.dumps(legacy_sessions), encoding="utf-8")
        (report_dir / "forensics.json").write_text(
            json.dumps({"schema_version": telemetry_post.FORENSICS_SCHEMA_VERSION, "verdict": {"code": "healthy"}}),
            encoding="utf-8",
        )
        await service.save_task(task)

        # section="workload" routes client_series + sessions_summary and preserves schema_version=10
        tel = await service.get_task_telemetry(task_id, section="workload")
        assert tel["section"] == "workload"
        cs_out = tel["client_series"]
        assert cs_out["schema_version"] == 10
        assert cs_out["buckets"] == legacy_cs["buckets"]
        assert tel["sessions_summary"] == legacy_sessions

        wl = cs_out["workload"]
        assert wl["fallback_synthesized"] is True
        assert wl["fallback_to_full"] is False

        # Full window maps legacy stats, does NOT fabricate ratios, and folds >=10 turns
        w_full = wl["windows"]["full"]
        assert w_full["summary"]["isl"]["mean"] == 400
        assert w_full["summary"]["osl_isl_ratio"]["mean"] is None
        assert w_full["summary"]["osl_isl_ratio"]["sample_count"] == 0
        assert w_full["multi_turn"]["is_multi_turn"] is True
        assert w_full["multi_turn"]["reusable_prefix_tokens"] == 3500
        assert any(item["label"] == "10+ turns" for item in w_full["multi_turn"]["turns_distribution"])

        # Non-fallback profiling MUST NOT leak full-run sessions_summary.reusable_prefix_tokens;
        # instead sums non-warmup buckets[].reusable_prefix_tokens clamped <= profiling total_input_tokens
        w_prof = wl["windows"]["profiling"]
        assert w_prof["multi_turn"]["available"] is False
        assert w_prof["multi_turn"]["reason"] == "Per-window session breakdown requires raw request export."
        assert w_prof["multi_turn"]["reusable_prefix_tokens"] == 2800
        assert w_prof["multi_turn"]["reusable_prefix_pct"] == 100.0

        # resolve_task_artifact refreshes persisted client-series.json with synthesized workload while keeping schema_version=10
        cs_path, _, _ = await service.resolve_task_artifact(task, "client_series")
        persisted = json.loads(cs_path.read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 10
        assert persisted["workload"]["fallback_synthesized"] is True
    finally:
        service.reset_service_state()


def test_v10_nic_sanitization_and_system_enrichment() -> None:
    # 1. sanitize_server_telemetry validates, clamps/drops invalid NIC samples, and caps at 16
    many_nics = [
        {
            "device": f"eth{i}",
            "rx_bytes_per_second": {"mean": float(100 + i), "max": float(200 + i)},
            "tx_bytes_per_second": {"mean": float(50 + i), "max": float(100 + i)},
        }
        for i in range(18)
    ]
    many_nics.append({"device": "", "rx_bytes_per_second": {"mean": 999.0, "max": 999.0}})
    many_nics.append({"device": "bad-neg", "rx_bytes_per_second": {"mean": -5.0, "max": float("nan")}})

    point_nics = {f"eth{i}": {"rx": float(100 + i), "tx": float(50 + i)} for i in range(18)}
    point_nics["bad-neg"] = {"rx": -1.0, "tx": float("inf")}
    point_nics[" "] = {"rx": 10.0}

    raw = {
        "schema_version": 10,
        "status": "available",
        "nodes": ["node-a", "node-b"],
        "per_node": [
            {"node": "node-a", "network_interfaces": many_nics},
            {
                "node": "node-b",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 500.0, "max": 900.0},
                        "tx_bytes_per_second": {"mean": 200.0, "max": 400.0},
                    }
                ],
            },
        ],
        "per_node_series": {
            "node-a": [{"timestamp": "2026-01-01T00:00:00Z", "network_interfaces": point_nics}],
        },
        "series": [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "network_interfaces": {
                    "ib0": {"rx": 100.0, "tx": 50.0},
                    "eno1": {"rx": 20.0, "tx": 10.0},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "network_interfaces": {
                    "ib0": {"rx": 300.0, "tx": 150.0},
                    "eno1": {"rx": 40.0, "tx": 20.0},
                },
            },
        ],
        "summary": {
            "host_network_receive_bytes_per_second": {"mean": 230.0, "max": 340.0},
            "host_network_transmit_bytes_per_second": {"mean": 115.0, "max": 170.0},
        },
        "system": {
            "network_interfaces": [
                {"device": "ib0", "rx_bytes_per_second": {"mean": 200.0, "max": 300.0}, "node_count": 2},
                {"device": "bad-only", "rx_bytes_per_second": {"mean": -1.0}},
            ]
        },
    }

    cleaned = telemetry_post.sanitize_server_telemetry(raw)
    assert cleaned is not None
    assert len(cleaned["per_node"][0]["network_interfaces"]) == 16
    assert all(n["device"] not in {"", "bad-neg"} for n in cleaned["per_node"][0]["network_interfaces"])
    assert len(cleaned["per_node_series"]["node-a"][0]["network_interfaces"]) == 16
    assert "bad-neg" not in cleaned["per_node_series"]["node-a"][0]["network_interfaces"]
    assert cleaned["system"]["network_interfaces"] == [
        {"device": "ib0", "rx_bytes_per_second": {"mean": 200.0, "max": 300.0}, "node_count": 2}
    ]

    # 2. _enrich_server_telemetry computes system.network_interfaces from timestamp-summed series (concurrent peak)
    # Use staggered node peaks so sum(node_max) = 900+900 = 1800, whereas concurrent peak = 300
    staggered_srv = {
        "schema_version": 10,
        "status": "available",
        "nodes": ["node-a", "node-b"],
        "per_node": [
            {
                "node": "node-a",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 100.0, "max": 900.0},
                        "tx_bytes_per_second": {"mean": 50.0, "max": 400.0},
                    }
                ],
            },
            {
                "node": "node-b",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 100.0, "max": 900.0},
                        "tx_bytes_per_second": {"mean": 50.0, "max": 400.0},
                    }
                ],
            },
        ],
        "series": cleaned["series"],
        "summary": raw["summary"],
    }
    enriched = telemetry_post._enrich_server_telemetry(staggered_srv, {"duration_seconds": 10.0})
    assert enriched["schema_version"] == 11
    sys_nics = enriched["system"]["network_interfaces"]
    assert [n["device"] for n in sys_nics] == ["ib0", "eno1"]
    assert sys_nics[0] == {
        "device": "ib0",
        "rx_bytes_per_second": {"mean": 200.0, "max": 300.0},
        "tx_bytes_per_second": {"mean": 100.0, "max": 150.0},
        "node_count": 2,
    }

    # 3. Fallback from per_node_series when series lacks network_interfaces
    no_series_nic_srv = {
        "schema_version": 10,
        "status": "available",
        "nodes": ["node-a", "node-b"],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "host_cpu_usage_percent": 10.0}],
        "per_node_series": {
            "node-a": [
                {"timestamp": "2026-01-01T00:00:00Z", "network_interfaces": {"ib0": {"rx": 100.0, "tx": 50.0}}},
                {"timestamp": "2026-01-01T00:00:05Z", "network_interfaces": {"ib0": {"rx": 200.0, "tx": 80.0}}},
            ],
            "node-b": [
                {"timestamp": "2026-01-01T00:00:00Z", "network_interfaces": {"ib0": {"rx": 300.0, "tx": 100.0}}},
                {"timestamp": "2026-01-01T00:00:05Z", "network_interfaces": {"ib0": {"rx": 100.0, "tx": 20.0}}},
            ],
        },
        "summary": {"host_cpu_usage_percent": {"mean": 10.0, "max": 10.0}},
    }
    enriched_fallback = telemetry_post._enrich_server_telemetry(no_series_nic_srv, {"duration_seconds": 10.0})
    assert enriched_fallback["system"]["network_interfaces"] == [
        {
            "device": "ib0",
            "rx_bytes_per_second": {"mean": 350.0, "max": 400.0},
            "tx_bytes_per_second": {"mean": 125.0, "max": 150.0},
            "node_count": 2,
        }
    ]

    # 4. Legacy v<=9 upgrade without per-NIC data omits network_interfaces cleanly
    legacy_v9 = {
        "schema_version": 9,
        "status": "available",
        "nodes": ["node-a"],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "host_network_receive_bytes_per_second": 500.0}],
        "summary": {"host_network_receive_bytes_per_second": {"mean": 500.0, "max": 500.0}},
    }
    upgraded = telemetry_post._enrich_server_telemetry(legacy_v9, {"duration_seconds": 10.0})
    assert upgraded["schema_version"] == 11
    assert "network_interfaces" not in upgraded["system"]
    assert "network_interfaces" not in upgraded["per_node"][0]


def test_multi_snapshot_merge_preserves_and_dedups_per_nic() -> None:
    ts0 = "2026-01-01T00:00:00Z"
    # 1. Overlapping node smc-18 + additional node smc-19: must NOT double-count smc-18's NICs
    snap_a = {
        "status": "available",
        "namespace": "ns-a",
        "nodes": ["smc-18"],
        "series": [
            {
                "timestamp": ts0,
                "host_network_receive_bytes_per_second": 1000.0,
                "network_interfaces": {"ib0": {"rx": 800.0, "tx": 400.0}, "eno1": {"rx": 200.0, "tx": 100.0}},
            }
        ],
        "per_node": [
            {
                "node": "smc-18",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 800.0, "max": 800.0},
                        "tx_bytes_per_second": {"mean": 400.0, "max": 400.0},
                    },
                    {
                        "device": "eno1",
                        "rx_bytes_per_second": {"mean": 200.0, "max": 200.0},
                        "tx_bytes_per_second": {"mean": 100.0, "max": 100.0},
                    },
                ],
            }
        ],
        "per_node_series": {
            "smc-18": [
                {
                    "timestamp": ts0,
                    "network_receive_bytes_per_second": 1000.0,
                    "network_interfaces": {"ib0": {"rx": 800.0, "tx": 400.0}, "eno1": {"rx": 200.0, "tx": 100.0}},
                }
            ]
        },
    }
    snap_b = {
        "status": "available",
        "namespace": "ns-b",
        "nodes": ["smc-18", "smc-19"],
        "series": [
            {
                "timestamp": ts0,
                "host_network_receive_bytes_per_second": 1500.0,
                "network_interfaces": {"ib0": {"rx": 1200.0, "tx": 600.0}, "eno1": {"rx": 300.0, "tx": 150.0}},
            }
        ],
        "per_node": [
            {
                "node": "smc-18",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 800.0, "max": 800.0},
                        "tx_bytes_per_second": {"mean": 400.0, "max": 400.0},
                    }
                ],
            },
            {
                "node": "smc-19",
                "network_interfaces": [
                    {
                        "device": "ib0",
                        "rx_bytes_per_second": {"mean": 400.0, "max": 400.0},
                        "tx_bytes_per_second": {"mean": 200.0, "max": 200.0},
                    },
                    {
                        "device": "eno1",
                        "rx_bytes_per_second": {"mean": 100.0, "max": 100.0},
                        "tx_bytes_per_second": {"mean": 50.0, "max": 50.0},
                    },
                ],
            },
        ],
        "per_node_series": {
            "smc-18": [
                {
                    "timestamp": ts0,
                    "network_receive_bytes_per_second": 1000.0,
                    "network_interfaces": {"ib0": {"rx": 800.0, "tx": 400.0}, "eno1": {"rx": 200.0, "tx": 100.0}},
                }
            ],
            "smc-19": [
                {
                    "timestamp": ts0,
                    "network_receive_bytes_per_second": 500.0,
                    "network_interfaces": {"ib0": {"rx": 400.0, "tx": 200.0}, "eno1": {"rx": 100.0, "tx": 50.0}},
                }
            ],
        },
    }

    merged_overlap = telemetry_post._merge_observability_snapshots(
        [snap_a, snap_b], start_iso=ts0, end_iso="2026-01-01T00:00:10Z"
    )
    smc18 = next(n for n in merged_overlap["per_node"] if n["node"] == "smc-18")
    assert [n["device"] for n in smc18["network_interfaces"]] == ["ib0", "eno1"]
    assert merged_overlap["per_node_series"]["smc-18"][0]["network_interfaces"] == {
        "ib0": {"rx": 800.0, "tx": 400.0},
        "eno1": {"rx": 200.0, "tx": 100.0},
    }
    # series sums smc-18 (once) + smc-19 (once) = 1200 ib0 rx, 300 eno1 rx
    assert merged_overlap["series"][0]["network_interfaces"] == {
        "ib0": {"rx": 1200.0, "tx": 600.0},
        "eno1": {"rx": 300.0, "tx": 150.0},
    }

    # 2. Overlapping node without complete per_node_series coverage averages rather than double-counting
    partial_a = {
        "status": "available",
        "namespace": "ns-a",
        "nodes": ["smc-18"],
        "series": [{"timestamp": ts0, "network_interfaces": {"ib0": {"rx": 800.0, "tx": 400.0}}}],
    }
    partial_b = {
        "status": "available",
        "namespace": "ns-b",
        "nodes": ["smc-18", "smc-19"],
        "series": [{"timestamp": ts0, "network_interfaces": {"ib0": {"rx": 1000.0, "tx": 500.0}}}],
    }
    merged_partial = telemetry_post._merge_observability_snapshots(
        [partial_a, partial_b], start_iso=ts0, end_iso="2026-01-01T00:00:10Z"
    )
    assert merged_partial["series"][0]["network_interfaces"]["ib0"] == {"rx": 900.0, "tx": 450.0}

    # 3. Disjoint nodes sum per-NIC rates across snapshots and cap globally at 16
    disjoint_a = {
        "status": "available",
        "namespace": "ns-a",
        "nodes": ["node-a"],
        "series": [
            {
                "timestamp": ts0,
                "network_interfaces": {
                    "ib0": {"rx": 500.0, "tx": 250.0},
                    **{f"eth{i}": {"rx": float(10 + i), "tx": 5.0} for i in range(12)},
                },
            }
        ],
    }
    disjoint_b = {
        "status": "available",
        "namespace": "ns-b",
        "nodes": ["node-b"],
        "series": [
            {
                "timestamp": ts0,
                "network_interfaces": {
                    "ib0": {"rx": 700.0, "tx": 350.0},
                    **{f"ens{i}": {"rx": float(20 + i), "tx": 5.0} for i in range(10)},
                },
            }
        ],
    }
    merged_disjoint = telemetry_post._merge_observability_snapshots(
        [disjoint_a, disjoint_b], start_iso=ts0, end_iso="2026-01-01T00:00:10Z"
    )
    disjoint_nics = merged_disjoint["series"][0]["network_interfaces"]
    assert disjoint_nics["ib0"] == {"rx": 1200.0, "tx": 600.0}
    assert len(disjoint_nics) == 16


def test_workload_request_points_extraction_sampling_redaction_and_fallback() -> None:
    """Workload request_points extracts per-request fields, samples uniformly >2000, is redaction-safe, and has empty fallback."""
    task = _make_task("f0000001", duration_seconds=40, backend_options={"warmup_seconds": 5.0})
    records = [
        # Out-of-order arrived timestamps + completed-only fallback + failed request + missing tokens (skipped)
        ArtifactRecord(
            request_id="r-late",
            arrived=12.0,
            completed=15.0,
            successful=True,
            input_tokens=1024,
            output_tokens=256,
            requested_output_tokens=512,
            ttft_ms=85.125,
            tpot_ms=11.2346,
        ),
        ArtifactRecord(
            request_id="r-warmup",
            arrived=2.0,
            completed=4.0,
            successful=True,
            input_tokens=128,
            output_tokens=64,
            requested_output_tokens=64,
            ttft_ms=20.0,
            tpot_ms=5.0,
        ),
        ArtifactRecord(
            request_id="r-failed",
            arrived=None,
            completed=9.0,
            successful=False,
            issue_failed=True,
            input_tokens=0,
            output_tokens=10,
            requested_output_tokens=128,
            ttft_ms=None,
            tpot_ms=None,
        ),
        ArtifactRecord(
            request_id="r-notokens",
            arrived=10.0,
            completed=11.0,
            successful=False,
            input_tokens=None,
            output_tokens=None,
        ),
    ]
    cs = telemetry_post.build_client_series(task, records)
    assert cs["schema_version"] == telemetry_post.REPORT_SCHEMA_VERSION == 15
    full = cs["workload"]["windows"]["full"]
    prof = cs["workload"]["windows"]["profiling"]

    # r-notokens is excluded; remaining 3 points sorted chronologically by t
    assert full["request_points_total"] == 3
    assert full["request_points_sampled"] == 3
    assert [p["t"] for p in full["request_points"]] == [2.0, 9.0, 12.0]
    assert full["request_points"][1] == {
        "t": 9.0,
        "isl": 0,
        "osl": 10,
        "req_osl": 128,
        "ttft_ms": None,
        "tpot_ms": None,
        "ok": False,
    }
    assert full["request_points"][2] == {
        "t": 12.0,
        "isl": 1024,
        "osl": 256,
        "req_osl": 512,
        "ttft_ms": 85.12,
        "tpot_ms": 11.235,
        "ok": True,
    }
    # Profiling excludes r-warmup (arrived=2.0 < warmup=5.0)
    assert prof["request_points_total"] == 2
    assert prof["request_points_sampled"] == 2
    assert [p["t"] for p in prof["request_points"]] == [9.0, 12.0]

    # Every key in request_points and workload passes _is_sensitive_key(k) is False and redact_manifest_payload unchanged
    for k in ("request_points", "request_points_total", "request_points_sampled"):
        assert telemetry_post._is_sensitive_key(k) is False
    for k in full["request_points"][0]:
        assert telemetry_post._is_sensitive_key(k) is False
    assert telemetry_post.redact_manifest_payload(cs["workload"]) == cs["workload"]

    # Deterministic uniform downsampling when N > 2000 (preserves endpoints, exact 2000 points)
    task_big = _make_task("f0000002", duration_seconds=3000)
    big_records = [
        ArtifactRecord(
            request_id=f"big-{i}",
            arrived=float(2500 - i),  # reverse arrival order to verify chronological sort before sampling
            completed=float(2501 - i),
            successful=True,
            input_tokens=100 + (2500 - i),
            output_tokens=50 + ((2500 - i) % 200),
        )
        for i in range(2500)
    ]
    cs_big_a = telemetry_post.build_client_series(task_big, big_records)
    cs_big_b = telemetry_post.build_client_series(task_big, big_records)
    big_full = cs_big_a["workload"]["windows"]["full"]
    assert big_full["request_points_total"] == 2500
    assert big_full["request_points_sampled"] == 2000
    assert len(big_full["request_points"]) == 2000
    assert big_full["request_points"][0]["t"] == 1.0
    assert big_full["request_points"][-1]["t"] == 2500.0
    assert big_full["request_points"] == cs_big_b["workload"]["windows"]["full"]["request_points"]

    # Fallback synthesis emits empty request_points and zero counts
    fb = telemetry_post.synthesize_workload_fallback({"duration_seconds": 10.0, "windows": {"full": {}}})
    for w_key in ("profiling", "full"):
        w_fb = fb["windows"][w_key]
        assert w_fb["request_points"] == []
        assert w_fb["request_points_total"] == 0
        assert w_fb["request_points_sampled"] == 0


def test_build_heatmap_grid_per_request_scatter_sampling_and_edge_cases() -> None:
    """t125: _build_heatmap_grid emits sorted per-request points, true min/max, deterministic sampling, and optional cells."""
    # Empty or invalid-only inputs -> None
    assert telemetry_post._build_heatmap_grid([], duration_seconds=10.0) is None
    assert (
        telemetry_post._build_heatmap_grid(
            [(-0.1, 100.0, 20.0), (1.0, float("nan"), 20.0), (1.0, 100.0, -5.0)],
            duration_seconds=10.0,
        )
        is None
    )

    # 1-point edge case (including include_cells=False)
    one = telemetry_post._build_heatmap_grid(
        [(2.5, 512.0, 42.1234)],
        duration_seconds=1.0,
        window_start_seconds=2.0,
        include_cells=False,
    )
    assert one is not None
    assert one["points"] == [{"t": 2.5, "seq": 512, "ms": 42.123}]
    assert one["points_total"] == 1
    assert one["points_sampled"] == 1
    assert one["duration_seconds"] == 2.5
    assert one["window_start_seconds"] == 2.0
    assert one["min_ms"] == 42.123
    assert one["max_ms"] == 42.123
    assert "cells" not in one
    assert "time_bins" not in one
    assert "len_bins" not in one

    # True per-request min_ms/max_ms even when points share a cell (cell avg=50.0, true min=10.0, max=90.0)
    shared_cell = telemetry_post._build_heatmap_grid(
        [(0.02, 100.0, 90.0), (0.01, 100.0, 10.0), (-1.0, 100.0, 1.0)],
        duration_seconds=10.0,
    )
    assert shared_cell is not None
    assert shared_cell["points"] == [
        {"t": 0.01, "seq": 100, "ms": 10.0},
        {"t": 0.02, "seq": 100, "ms": 90.0},
    ]
    assert shared_cell["points_total"] == 2
    assert shared_cell["points_sampled"] == 2
    assert shared_cell["min_ms"] == 10.0
    assert shared_cell["max_ms"] == 90.0
    assert shared_cell["cells"][0]["avg_ms"] == 50.0

    # Deterministic uniform downsampling at >2000 points preserves endpoints and full-window min_ms/max_ms
    # Reverse input order; place extreme min (1.5ms) and max (9999.5ms) at odd indices likely skipped by sampling.
    raw_2500 = []
    for i in range(2500):
        idx = 2499 - i
        val = 1.5 if idx == 1 else (9999.5 if idx == 3 else 50.0 + (idx % 100))
        raw_2500.append((float(idx), float(100 + (idx % 300)), val))
    grid_a = telemetry_post._build_heatmap_grid(raw_2500, duration_seconds=2499.0, include_cells=False)
    grid_b = telemetry_post._build_heatmap_grid(raw_2500, duration_seconds=2499.0, include_cells=False)
    assert grid_a is not None and grid_b is not None
    assert grid_a["points_total"] == 2500
    assert grid_a["points_sampled"] == 2000
    assert len(grid_a["points"]) == 2000
    assert grid_a["points"][0]["t"] == 0.0
    assert grid_a["points"][-1]["t"] == 2499.0
    assert grid_a["points"] == grid_b["points"]
    assert [p["t"] for p in grid_a["points"]] == sorted(p["t"] for p in grid_a["points"])
    assert grid_a["min_ms"] == 1.5
    assert grid_a["max_ms"] == 9999.5


def test_build_client_series_window_scoped_latency_scatter() -> None:
    """t125: build_client_series scopes windows.{full,profiling}.heatmap, handles empty steady state, and requested OSL."""
    origin = 1_750_000_000.0
    task = _make_task("d1250001", duration_seconds=20, backend_options={"warmup_seconds": 5.0})
    records = [
        # Warmup request (arrived=1.0 < 5.0, completed=2.0) with extreme low TTFT=50ms and low TPOT=5ms
        ArtifactRecord(
            request_id="w1",
            arrived=origin + 1.0,
            completed=origin + 2.0,
            successful=True,
            input_tokens=128,
            output_tokens=64,
            requested_output_tokens=512,
            ttft_ms=50.0,
            tpot_ms=5.0,
            tpot_is_measured=True,
            latency_ms=800.0,
        ),
        # Profiling request 1 (arrived=6.0 >= 5.0, completed=8.0) with non-streaming tpot_is_measured=False
        ArtifactRecord(
            request_id="p1",
            arrived=origin + 6.0,
            completed=origin + 8.0,
            successful=True,
            input_tokens=256,
            output_tokens=None,
            requested_output_tokens=512,
            ttft_ms=1000.0,
            tpot_ms=20.0,
            tpot_is_measured=False,
            latency_ms=2000.0,
        ),
        # Profiling request 2 (arrived=10.0 >= 5.0, completed=15.0)
        ArtifactRecord(
            request_id="p2",
            arrived=origin + 10.0,
            completed=origin + 15.0,
            successful=True,
            input_tokens=512,
            output_tokens=None,
            requested_output_tokens=1024,
            ttft_ms=3000.0,
            tpot_ms=30.0,
            tpot_is_measured=False,
            latency_ms=5000.0,
        ),
    ]
    cs = telemetry_post.build_client_series(task, records)
    assert cs["schema_version"] == telemetry_post.REPORT_SCHEMA_VERSION == 15

    # Top-level cs["heatmap"] retains legacy cells alongside points
    top_ttft = cs["heatmap"]["ttft"]
    top_tpot = cs["heatmap"]["tpot"]
    assert "cells" in top_ttft and "cells" in top_tpot
    assert top_ttft["window_start_seconds"] == 0.0
    assert top_ttft["points_total"] == 3
    assert top_ttft["min_ms"] == 50.0
    assert top_ttft["max_ms"] == 3000.0

    # windows.full.heatmap omits cells and matches full-run scatter points
    full_hm = cs["windows"]["full"]["heatmap"]
    assert "cells" not in full_hm["ttft"] and "cells" not in full_hm["tpot"]
    assert full_hm["ttft"]["points"] == top_ttft["points"]
    assert full_hm["ttft"]["window_start_seconds"] == 0.0
    assert full_hm["tpot"]["points_total"] == 3
    # Full window has 1 observed (w1) vs 2 requested (p1, p2) -> "requested"
    assert full_hm["tpot"]["sequence_length_source"] == "requested"

    # windows.profiling.heatmap excludes warmup w1 (t=1.0) and sets window_start_seconds=5.0
    prof_hm = cs["windows"]["profiling"]["heatmap"]
    assert "cells" not in prof_hm["ttft"] and "cells" not in prof_hm["tpot"]
    assert prof_hm["ttft"]["window_start_seconds"] == 5.0
    assert prof_hm["ttft"]["duration_seconds"] == cs["duration_seconds"]
    assert prof_hm["ttft"]["points"] == [
        {"t": 7.0, "seq": 256, "ms": 1000.0},
        {"t": 14.0, "seq": 512, "ms": 3000.0},
    ]
    assert prof_hm["ttft"]["points_total"] == 2
    assert prof_hm["ttft"]["min_ms"] == 1000.0
    assert prof_hm["ttft"]["max_ms"] == 3000.0
    # Profiling TPOT keeps tpot_is_measured=False requests and falls back to requested_output_tokens
    assert prof_hm["tpot"]["sequence_length"] == "output"
    assert prof_hm["tpot"]["sequence_length_source"] == "requested"
    assert prof_hm["tpot"]["points"] == [
        {"t": 7.0, "seq": 512, "ms": 20.0},
        {"t": 14.0, "seq": 1024, "ms": 30.0},
    ]
    assert prof_hm["tpot"]["min_ms"] == 20.0
    assert prof_hm["tpot"]["max_ms"] == 30.0

    # Warmup=0 -> windows.profiling.heatmap equals windows.full.heatmap as an independent copy
    cs_nowu = telemetry_post.build_client_series(_make_task("d1250002", duration_seconds=20), records)
    assert cs_nowu["windows"]["profiling"]["heatmap"] == cs_nowu["windows"]["full"]["heatmap"]
    assert cs_nowu["windows"]["profiling"]["heatmap"] is not cs_nowu["windows"]["full"]["heatmap"]

    # Empty steady-state window (all requests arrived during warmup while span exceeds warmup) -> profiling heatmap metrics are None
    task_empty_steady = _make_task("d1250003", duration_seconds=20, backend_options={"warmup_seconds": 10.0})
    warmup_only_recs = [
        ArtifactRecord(
            request_id="wu-only",
            arrived=origin + 2.0,
            completed=origin + 14.0,
            successful=True,
            input_tokens=100,
            output_tokens=50,
            ttft_ms=80.0,
            tpot_ms=10.0,
            latency_ms=12000.0,
        )
    ]
    cs_empty_steady = telemetry_post.build_client_series(task_empty_steady, warmup_only_recs)
    assert cs_empty_steady["cold_start_comparison"]["mode"] == "configured_warmup"
    assert cs_empty_steady["windows"]["full"]["heatmap"]["ttft"] is not None
    assert cs_empty_steady["windows"]["full"]["heatmap"]["tpot"] is not None
    assert cs_empty_steady["windows"]["profiling"]["heatmap"] == {"ttft": None, "tpot": None}


def test_sample_slot_lane_items_preserves_multi_turn_sessions_and_edge_cases() -> None:
    """_sample_slot_lane_items keeps selected multi-turn sessions intact and handles all edge cases deterministically."""
    # 1. max_items <= 0 and len(items) <= max_items
    small_items = [
        {"slot": 0, "start_s": 0.0, "end_s": 0.5, "session_id": "s-0", "turn": 1},
        {"slot": 1, "start_s": 0.1, "end_s": 0.6, "session_id": "s-0", "turn": 2},
    ]
    assert telemetry_post._sample_slot_lane_items(small_items, 0) == ([], 1)
    assert telemetry_post._sample_slot_lane_items(small_items, -5) == ([], 1)
    passthrough, passthrough_stride = telemetry_post._sample_slot_lane_items(small_items, 2)
    assert passthrough == small_items
    assert passthrough is not small_items
    assert passthrough_stride == 1

    # 2. Sessionless-only downsampling preserves first and last item and stride >= 2
    sessionless = [
        {"slot": idx % 4, "start_s": float(idx), "end_s": float(idx) + 0.25, "session_id": None if idx % 2 else "  "}
        for idx in range(50)
    ]
    sl_only, sl_stride = telemetry_post._sample_slot_lane_items(sessionless, 10)
    assert len(sl_only) == 10
    assert sl_only[0] == sessionless[0]
    assert sl_only[-1] == sessionless[-1]
    assert sl_stride == max(2, round(50 / 10))
    assert [(it["start_s"], it["end_s"], it["slot"]) for it in sl_only] == sorted(
        (it["start_s"], it["end_s"], it["slot"]) for it in sl_only
    )

    # 3. Multi-turn sessions only: whole sessions preserved 100%, first & last kept, chronological order
    multi_turn_items = [
        {
            "slot": turn % 3,
            "start_s": float(sess_idx * 10 + turn),
            "end_s": float(sess_idx * 10 + turn) + 0.5,
            "session_id": f"sess-{sess_idx:02d}",
            "turn": turn + 1,
        }
        for sess_idx in range(12)
        for turn in range(4)
    ]
    sampled_sessions, sess_stride = telemetry_post._sample_slot_lane_items(multi_turn_items, 20)
    assert len(sampled_sessions) == 20
    assert sess_stride == max(2, round(48 / 20))
    counts_by_sid: dict[str, int] = {}
    for item in sampled_sessions:
        sid = str(item["session_id"])
        counts_by_sid[sid] = counts_by_sid.get(sid, 0) + 1
    assert len(counts_by_sid) == 5
    assert all(count == 4 for count in counts_by_sid.values())
    assert "sess-00" in counts_by_sid and "sess-11" in counts_by_sid
    assert [(it["start_s"], it["end_s"], it["slot"]) for it in sampled_sessions] == sorted(
        (it["start_s"], it["end_s"], it["slot"]) for it in sampled_sessions
    )

    # 4. Mixed sessioned + sessionless items: proportional budget, whole sessions, sessionless endpoints & chronological order
    mixed_items: list[dict[str, object]] = []
    for idx in range(10):
        # 10 sessions x 3 turns = 30 sessioned items
        for turn in range(3):
            mixed_items.append(
                {
                    "slot": turn,
                    "start_s": float(idx * 5 + turn * 0.5),
                    "end_s": float(idx * 5 + turn * 0.5 + 0.2),
                    "session_id": f"mix-{idx}",
                    "turn": turn + 1,
                }
            )
        # 30 sessionless items interleaved across time
        for extra in range(3):
            mixed_items.append(
                {
                    "slot": 3 + extra,
                    "start_s": float(idx * 5 + extra * 0.6 + 0.1),
                    "end_s": float(idx * 5 + extra * 0.6 + 0.3),
                    "session_id": None,
                    "turn": None,
                }
            )
    mixed_items.sort(key=lambda it: (float(it["start_s"]), float(it["end_s"]), int(it["slot"])))
    mixed_sampled, mixed_stride = telemetry_post._sample_slot_lane_items(mixed_items, 20)
    assert len(mixed_sampled) <= 20
    assert mixed_stride == max(2, round(len(mixed_items) / len(mixed_sampled)))
    mixed_sessioned = [it for it in mixed_sampled if it.get("session_id")]
    mixed_sessionless = [it for it in mixed_sampled if not it.get("session_id")]
    assert len(mixed_sessioned) > 0 and len(mixed_sessionless) >= 2
    mixed_sid_counts: dict[str, int] = {}
    for it in mixed_sessioned:
        sid = str(it["session_id"])
        mixed_sid_counts[sid] = mixed_sid_counts.get(sid, 0) + 1
    assert all(count == 3 for count in mixed_sid_counts.values())
    all_sessionless = [it for it in mixed_items if not it.get("session_id")]
    assert mixed_sessionless[0] == all_sessionless[0]
    assert mixed_sessionless[-1] == all_sessionless[-1]
    assert [(it["start_s"], it["end_s"], it["slot"]) for it in mixed_sampled] == sorted(
        (it["start_s"], it["end_s"], it["slot"]) for it in mixed_sampled
    )

    # 5. Edge case: single huge session > max_items (0 whole sessions fit) -> uniform sample within first session
    huge_session = [
        {"slot": idx % 2, "start_s": float(idx), "end_s": float(idx) + 0.4, "session_id": "huge-0", "turn": idx + 1}
        for idx in range(50)
    ]
    huge_sampled, huge_stride = telemetry_post._sample_slot_lane_items(huge_session, 12)
    assert len(huge_sampled) == 12
    assert huge_stride == max(2, round(50 / 12))
    assert huge_sampled[0] == huge_session[0]
    assert huge_sampled[-1] == huge_session[-1]
    assert [it["turn"] for it in huge_sampled] == sorted(it["turn"] for it in huge_sampled)

    # 6. Mixed workload where every session exceeds proportional session_budget but fits within max_items:
    #    keep the first fitting session 100% whole and fill the remaining budget with sessionless items.
    sparse_session_mixed: list[dict[str, object]] = []
    for sess_idx in range(2):
        for turn in range(15):
            sparse_session_mixed.append(
                {
                    "slot": sess_idx,
                    "start_s": float(sess_idx * 20 + turn),
                    "end_s": float(sess_idx * 20 + turn) + 0.3,
                    "session_id": f"sparse-{sess_idx}",
                    "turn": turn + 1,
                }
            )
    for idx in range(10000):
        sparse_session_mixed.append(
            {
                "slot": 2 + (idx % 4),
                "start_s": 100.0 + float(idx) * 0.1,
                "end_s": 100.0 + float(idx) * 0.1 + 0.05,
                "session_id": None,
                "turn": None,
            }
        )
    sparse_sampled, sparse_stride = telemetry_post._sample_slot_lane_items(sparse_session_mixed, 2000)
    assert len(sparse_sampled) == 2000
    assert sparse_stride == max(2, round(10030 / 2000))
    sparse_sessioned = [it for it in sparse_sampled if it.get("session_id")]
    sparse_sessionless = [it for it in sparse_sampled if not it.get("session_id")]
    assert len(sparse_sessioned) == 15
    assert {it["session_id"] for it in sparse_sessioned} == {"sparse-0"}
    assert [it["turn"] for it in sparse_sessioned] == list(range(1, 16))
    assert len(sparse_sessionless) == 1985
    assert [(it["start_s"], it["end_s"], it["slot"]) for it in sparse_sampled] == sorted(
        (it["start_s"], it["end_s"], it["slot"]) for it in sparse_sampled
    )


def test_build_slot_lanes_preserves_complete_multi_turn_sessions_over_cap() -> None:
    """_build_slot_lanes keeps 100% of turns for every selected session when total_requests > _MAX_SLOT_LANES_ITEMS."""
    total_sessions = 600
    turns_per_session = 4
    origin = 1_700_000_000.0
    records: list[ArtifactRecord] = []
    record_session_id: dict[int, str] = {}
    record_turn: dict[int, int] = {}
    per_record_cached: dict[int, float] = {}

    seq = 0
    for turn in range(1, turns_per_session + 1):
        for sess_idx in range(total_sessions):
            arr = origin + float(turn * 1000 + sess_idx)
            records.append(
                ArtifactRecord(
                    arrived=arr,
                    completed=arr + 0.5,
                    successful=True,
                    input_tokens=100,
                    output_tokens=20,
                    ttft_ms=25.0,
                    request_id=f"r-{seq}",
                )
            )
            record_session_id[seq] = f"session-{sess_idx:03d}"
            record_turn[seq] = turn
            per_record_cached[seq] = 80.0 if turn > 1 else 0.0
            seq += 1

    sl = telemetry_post._build_slot_lanes(
        records,
        origin=origin,
        per_record_reusable={},
        per_record_overlap_pct={},
        record_session_id=record_session_id,
        record_turn=record_turn,
        per_record_cached=per_record_cached,
    )
    assert sl["total_requests"] == total_sessions * turns_per_session == 2400
    assert sl["session_count"] == total_sessions
    assert len(sl["items"]) <= telemetry_post._MAX_SLOT_LANES_ITEMS
    assert len(sl["items"]) == telemetry_post._MAX_SLOT_LANES_ITEMS
    assert sl["sample_stride"] >= 2

    turns_by_session: dict[str, list[int]] = {}
    for item in sl["items"]:
        sid = str(item["session_id"])
        turns_by_session.setdefault(sid, []).append(int(item["turn"]))

    assert len(turns_by_session) == telemetry_post._MAX_SLOT_LANES_ITEMS // turns_per_session
    assert "session-000" in turns_by_session
    assert f"session-{total_sessions - 1:03d}" in turns_by_session
    assert all(sorted(turns) == [1, 2, 3, 4] for turns in turns_by_session.values())
    assert [(it["start_s"], it["end_s"], it["slot"]) for it in sl["items"]] == sorted(
        (it["start_s"], it["end_s"], it["slot"]) for it in sl["items"]
    )


def test_v11_hardware_spec_sanitization_merge_and_v10_compat() -> None:
    assert telemetry_post.SERVER_TELEMETRY_SCHEMA_VERSION == 11

    long_os = "Ubuntu 24.04 " + ("z" * 200)
    raw_v11 = {
        "schema_version": 11,
        "status": "available",
        "nodes": ["node-a", "node-zero"],
        "per_node": [
            {
                "node": "node-a",
                "roles": ["prefill"],
                "pods": ["prefill-0"],
                "pod_count": 1,
                "allocated_accelerators": 4,
                "accelerator_capacity": 8,
                "accelerator_allocatable": 6,
                "cpu_capacity_cores": 64,
                "cpu_allocatable_cores": 62.5,
                "memory_capacity_bytes": 512 * 1024**3,
                "accelerator_model": "  Acme X900  ",
                "architecture": "amd64",
                "os_image": long_os,
                "kernel_version": "6.5.0",
                "kubelet_version": "v1.31.2",
                "container_runtime": "containerd://1.7.13",
            },
            {
                "node": "node-zero",
                "roles": ["worker"],
                "pods": ["w-0"],
                "pod_count": 1,
                "allocated_accelerators": 0,
                "accelerator_capacity": -2,
                "accelerator_allocatable": True,
                "cpu_capacity_cores": float("nan"),
                "cpu_allocatable_cores": False,
                "memory_capacity_bytes": -100,
                "accelerator_model": {"raw": "bad"},
                "architecture": "   ",
                "os_image": float("inf"),
            },
        ],
        "per_pod": [
            {
                "pod": "prefill-0",
                "role": "prefill",
                "node": "node-a",
                "allocated_accelerators": 4,
                "cpu_request_cores": 4.5,
                "cpu_limit_cores": 8,
                "memory_request_bytes": 16 * 1024**3,
                "memory_limit_bytes": 32 * 1024**3,
            },
            {
                "pod": "w-0",
                "role": "worker",
                "node": "node-zero",
                "allocated_accelerators": False,
                "cpu_request_cores": -1.0,
                "cpu_limit_cores": float("inf"),
                "memory_request_bytes": True,
            },
        ],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 10.0}],
        "summary": {"request_rate_rps": {"mean": 10.0, "max": 10.0}},
    }

    cleaned = telemetry_post.sanitize_server_telemetry(raw_v11)
    assert cleaned is not None
    node_a, node_zero = cleaned["per_node"]
    assert node_a["allocated_accelerators"] == 4
    assert node_a["accelerator_capacity"] == 8
    assert node_a["accelerator_allocatable"] == 6
    assert node_a["cpu_capacity_cores"] == 64
    assert node_a["cpu_allocatable_cores"] == 62.5
    assert node_a["memory_capacity_bytes"] == 512 * 1024**3
    assert node_a["accelerator_model"] == "Acme X900"
    assert node_a["architecture"] == "amd64"
    assert len(node_a["os_image"]) == 160
    assert node_a["kernel_version"] == "6.5.0"
    assert node_a["kubelet_version"] == "v1.31.2"
    assert node_a["container_runtime"] == "containerd://1.7.13"

    assert node_zero["allocated_accelerators"] == 0
    for dropped in (
        "accelerator_capacity",
        "accelerator_allocatable",
        "cpu_capacity_cores",
        "cpu_allocatable_cores",
        "memory_capacity_bytes",
        "accelerator_model",
        "architecture",
        "os_image",
    ):
        assert dropped not in node_zero

    pod_a, pod_zero = cleaned["per_pod"]
    assert pod_a["allocated_accelerators"] == 4
    assert pod_a["cpu_request_cores"] == 4.5
    assert pod_a["cpu_limit_cores"] == 8
    assert pod_a["memory_request_bytes"] == 16 * 1024**3
    assert pod_a["memory_limit_bytes"] == 32 * 1024**3
    for dropped in ("allocated_accelerators", "cpu_request_cores", "cpu_limit_cores", "memory_request_bytes"):
        assert dropped not in pod_zero

    # Multi-snapshot merge preserves per_node/per_pod specs, deduplicates accelerator_model, sums cache hierarchy
    snap_1 = {
        "status": "available",
        "namespace": "ns-p",
        "nodes": ["node-a"],
        "per_node": [
            {
                "node": "node-a",
                "roles": ["prefill"],
                "pods": ["prefill-0"],
                "pod_count": 1,
                "allocated_accelerators": 2,
                "accelerator_capacity": 8,
                "accelerator_allocatable": 8,
                "cpu_capacity_cores": 64,
                "cpu_allocatable_cores": 62.5,
                "memory_capacity_bytes": 512 * 1024**3,
                "accelerator_model": "Card A",
                "architecture": "amd64",
                "os_image": "Ubuntu 24.04",
                "kernel_version": "6.5.0",
                "kubelet_version": "v1.31.2",
                "container_runtime": "containerd://1.7.13",
            }
        ],
        "per_pod": [
            {
                "pod": "prefill-0",
                "role": "prefill",
                "node": "node-a",
                "allocated_accelerators": 2,
                "cpu_request_cores": 4,
                "memory_request_bytes": 16 * 1024**3,
            }
        ],
        "cache_config": {
            "engines": [{"pod": "prefill-0"}],
            "hbm_capacity_tokens": 1000,
            "hbm_capacity_bytes": 10 * 1024**3,
            "prefill_hbm_capacity_tokens": 1000,
            "cpu_capacity_tokens": 2000,
            "cpu_capacity_bytes": 32 * 1024**3,
            "effective_capacity_tokens": 3000,
            "host_kv_configured": True,
        },
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 5.0}],
        "summary": {"request_rate_rps": {"mean": 5.0}},
    }
    snap_2 = {
        "status": "available",
        "namespace": "ns-d",
        "nodes": ["node-a"],
        "per_node": [
            {
                "node": "node-a",
                "roles": ["decode"],
                "pods": ["decode-0"],
                "pod_count": 1,
                "allocated_accelerators": 4,
                "accelerator_capacity": 8,
                "cpu_capacity_cores": 64,
                "accelerator_model": "Card B, Card A",
            }
        ],
        "per_pod": [
            {
                "pod": "decode-0",
                "role": "decode",
                "node": "node-a",
                "allocated_accelerators": 4,
                "cpu_request_cores": 6,
                "memory_limit_bytes": 64 * 1024**3,
            }
        ],
        "cache_config": {
            "engines": [{"pod": "decode-0"}],
            "hbm_capacity_tokens": 2000,
            "hbm_capacity_bytes": 20 * 1024**3,
            "decode_hbm_capacity_tokens": 2000,
            "nvme_capacity_bytes": 100 * 1024**3,
            "disk_capacity_bytes": 100 * 1024**3,
            "effective_capacity_tokens": 2000,
            "nvme_kv_configured": True,
        },
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 8.0}],
        "summary": {"request_rate_rps": {"mean": 8.0}},
    }

    merged = telemetry_post._merge_observability_snapshots(
        [snap_1, snap_2],
        start_iso="2026-01-01T00:00:00Z",
        end_iso="2026-01-01T00:01:00Z",
    )
    assert merged is not None
    m_node = merged["per_node"][0]
    assert m_node["allocated_accelerators"] == 6
    assert m_node["accelerator_capacity"] == 8
    assert m_node["accelerator_allocatable"] == 8
    assert m_node["cpu_capacity_cores"] == 64
    assert m_node["cpu_allocatable_cores"] == 62.5
    assert m_node["memory_capacity_bytes"] == 512 * 1024**3
    assert m_node["accelerator_model"] == "Card A, Card B"
    assert m_node["architecture"] == "amd64"
    assert m_node["os_image"] == "Ubuntu 24.04"
    assert m_node["kernel_version"] == "6.5.0"
    assert m_node["kubelet_version"] == "v1.31.2"
    assert m_node["container_runtime"] == "containerd://1.7.13"

    m_pods = {p["pod"]: p for p in merged["per_pod"]}
    assert m_pods["prefill-0"]["allocated_accelerators"] == 2
    assert m_pods["prefill-0"]["cpu_request_cores"] == 4
    assert m_pods["decode-0"]["allocated_accelerators"] == 4
    assert m_pods["decode-0"]["memory_limit_bytes"] == 64 * 1024**3

    m_cc = merged["cache_config"]
    assert m_cc["hbm_capacity_tokens"] == 3000
    assert m_cc["hbm_capacity_bytes"] == 30 * 1024**3
    assert m_cc["prefill_hbm_capacity_tokens"] == 1000
    assert m_cc["decode_hbm_capacity_tokens"] == 2000
    assert m_cc["cpu_capacity_tokens"] == 2000
    assert m_cc["cpu_capacity_bytes"] == 32 * 1024**3
    assert m_cc["nvme_capacity_bytes"] == 100 * 1024**3
    assert m_cc["disk_capacity_bytes"] == 100 * 1024**3
    assert m_cc["effective_capacity_tokens"] == 5000
    assert m_cc["host_kv_configured"] is True
    assert m_cc["nvme_kv_configured"] is True

    # v10 backwards compatibility: missing v11 spec fields stay absent without fabrication
    legacy_v10 = {
        "schema_version": 10,
        "status": "available",
        "nodes": ["legacy-node"],
        "per_node": [
            {
                "node": "legacy-node",
                "roles": ["worker"],
                "pods": ["w-0"],
                "pod_count": 1,
                "allocated_accelerators": 1,
                "memory_total_bytes": 128 * 1024**3,
            }
        ],
        "per_pod": [{"pod": "w-0", "role": "worker", "node": "legacy-node"}],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 2.0}],
        "summary": {"request_rate_rps": {"mean": 2.0}},
    }
    upgraded_v10 = telemetry_post._enrich_server_telemetry(
        telemetry_post.sanitize_server_telemetry(legacy_v10),
        {"duration_seconds": 10.0},
    )
    assert upgraded_v10["schema_version"] == 11
    assert upgraded_v10["per_node"][0]["memory_total_bytes"] == 128 * 1024**3
    assert "cpu_capacity_cores" not in upgraded_v10["per_node"][0]
    assert "accelerator_model" not in upgraded_v10["per_node"][0]
    assert "allocated_accelerators" not in upgraded_v10["per_pod"][0]

    # Security hygiene: ANSI/control chars stripped, secrets redacted in spec strings, disallowed K8s keys dropped
    dirty_v11 = {
        "schema_version": 11,
        "status": "available",
        "nodes": ["node-sec"],
        "per_node": [
            {
                "node": "node-sec",
                "roles": ["worker"],
                "pods": ["p-sec"],
                "pod_count": 1,
                "os_image": "\x1b[31mUbuntu\x1b[0m\t24.04\nBearer secret-tok-123\x00\x85",
                "accelerator_model": "https://user:pass@host.internal/card sk-secretkey999",
                "addresses": [{"type": "InternalIP", "address": "10.0.0.5"}],
                "annotations": {"secret": "val"},
                "api_token": "secret-token",
                "status": {"raw": "object"},
                "metadata": {"uid": "abc"},
            }
        ],
        "per_pod": [
            {
                "pod": "p-sec",
                "role": "worker",
                "node": "node-sec",
                "env": [{"name": "API_KEY", "value": "secret"}],
                "envFrom": [{"secretRef": {"name": "s"}}],
                "args": ["--api-key=secret"],
                "command": ["/bin/sh"],
                "volumeMounts": [{"name": "v"}],
                "volumes": [{"name": "v"}],
                "imagePullSecrets": [{"name": "pull"}],
                "annotations": {"a": "b"},
                "spec": {"raw": "x"},
                "api_token": "secret-token",
            }
        ],
        "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 1.0}],
        "summary": {"request_rate_rps": {"mean": 1.0}},
    }
    cleaned_sec = telemetry_post.sanitize_server_telemetry(dirty_v11)
    assert cleaned_sec is not None
    sec_node = cleaned_sec["per_node"][0]
    assert "\x1b" not in sec_node["os_image"]
    assert "\x00" not in sec_node["os_image"]
    assert "\x85" not in sec_node["os_image"]
    assert "\n" not in sec_node["os_image"]
    assert "secret-tok-123" not in sec_node["os_image"]
    assert sec_node["os_image"] == "Ubuntu 24.04 Bearer [REDACTED]"
    assert "user:pass" not in sec_node["accelerator_model"]
    assert "sk-secretkey999" not in sec_node["accelerator_model"]
    for dropped in ("addresses", "annotations", "api_token", "status", "metadata"):
        assert dropped not in sec_node

    sec_pod = cleaned_sec["per_pod"][0]
    for dropped in (
        "env",
        "envFrom",
        "args",
        "command",
        "volumeMounts",
        "volumes",
        "imagePullSecrets",
        "annotations",
        "spec",
        "api_token",
    ):
        assert dropped not in sec_pod


@pytest.mark.asyncio
async def test_v11_section_hardware_and_workload_service_and_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from llm_d_lens.api.main import app
    from llm_d_lens.simulation import service
    from llm_d_lens.simulation.models import SimulationResult

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    service.reset_service_state()
    try:
        task_id = "ab112233"
        task = _make_task(
            task_id,
            duration_seconds=20,
            endpoint_mode="in-cluster",
            endpoint_cluster_id="c-1",
            endpoint_namespace="ns-1",
        )
        task_dir = service.task_root() / task_id
        task.task_dir = str(task_dir)
        task.result = SimulationResult(
            run_id=task_id,
            backend="aiperf",
            summary={"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
            per_request=[],
            backend_metrics={},
            warnings=[],
            artifacts=[],
        )
        report_dir = task_dir / "artifacts" / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "server-telemetry.json").write_text(
            json.dumps(
                {
                    "schema_version": 11,
                    "status": "available",
                    "nodes": ["node-1"],
                    "per_node": [
                        {
                            "node": "node-1",
                            "roles": ["worker"],
                            "pods": ["w-0"],
                            "pod_count": 1,
                            "allocated_accelerators": 2,
                            "cpu_capacity_cores": 32,
                        }
                    ],
                    "series": [{"timestamp": "2026-01-01T00:00:00Z", "request_rate_rps": 1.0}],
                    "summary": {"request_rate_rps": {"mean": 1.0}},
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "client-series.json").write_text(
            json.dumps(
                {
                    "schema_version": telemetry_post.CLIENT_SERIES_SCHEMA_VERSION,
                    "buckets": [],
                    "windows": {"full": {"request_count": 1}},
                    "workload": {"fallback_synthesized": False},
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "forensics.json").write_text(
            json.dumps(
                {
                    "schema_version": telemetry_post.FORENSICS_SCHEMA_VERSION,
                    "verdict": {"code": "healthy"},
                    "telemetry_scope": "full",
                }
            ),
            encoding="utf-8",
        )
        (report_dir / "sessions-summary.json").write_text(json.dumps({"available": False}), encoding="utf-8")
        (report_dir / "run-manifest.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        await service.save_task(task)

        hw_payload = await service.get_task_telemetry(task_id, section="hardware")
        assert hw_payload["section"] == "hardware"
        assert hw_payload["server_telemetry"]["per_node"][0]["cpu_capacity_cores"] == 32
        assert hw_payload["run_manifest"] == {"schema_version": 1}
        for omitted in ("forensics", "client_series", "sessions_summary", "grafana_links"):
            assert omitted not in hw_payload

        with TestClient(app) as client:
            hw_resp = client.get(f"/api/v1/simulation/tasks/{task_id}/telemetry?section=hardware")
            assert hw_resp.status_code == 200
            hw_json = hw_resp.json()
            assert hw_json["section"] == "hardware"
            assert hw_json["server_telemetry"]["per_node"][0]["cpu_capacity_cores"] == 32
            assert hw_json["run_manifest"] == {"schema_version": 1}

            wl_resp = client.get(f"/api/v1/simulation/tasks/{task_id}/telemetry?section=workload")
            assert wl_resp.status_code == 200
            assert wl_resp.json()["section"] == "workload"
            assert "client_series" in wl_resp.json()
    finally:
        service.reset_service_state()
