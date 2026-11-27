"""Tests for benchmark-window Prometheus observability."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_d_lens.monitoring.profiling import pressure, service
from llm_d_lens.monitoring.profiling.service import _prometheus_clock_offset
from llm_d_lens.monitoring.prometheus import PrometheusEndpoint


@pytest.fixture(autouse=True)
def no_intel_allocations(monkeypatch):
    async def resources(*_args, **_kwargs):
        return []

    async def clock(_client):
        return 0.0

    monkeypatch.setattr(service, "_prometheus_clock_offset", clock)
    monkeypatch.setattr(service, "list_resources", resources)


def test_range_points_sum_series_and_summary_percentiles():
    points = service._range_points(
        [
            {"values": [[10, "1.5"], [20, "2.5"], [30, "NaN"]]},
            {"values": [[10, "3.5"], [20, "4.5"]]},
        ]
    )

    assert points == {10.0: 5.0, 20.0: 7.0}
    assert service._summary(list(points.values())) == {
        "min": 5.0,
        "mean": 6.0,
        "p50": 5.0,
        "p95": 7.0,
        "p99": 7.0,
        "max": 7.0,
    }


def test_cache_config_snapshot_extracts_runtime_token_capacity():
    snapshot = service._cache_config_snapshot(
        [
            {
                "metric": {
                    "pod": "vllm-0",
                    "engine": "0",
                    "block_size": "16",
                    "num_gpu_blocks": "100",
                    "num_cpu_blocks": "25",
                    "kv_cache_size_tokens": "1600",
                },
                "values": [[1, "1"]],
            },
            {
                "metric": {
                    "pod": "vllm-1",
                    "engine": "0",
                    "block_size": "16",
                    "num_gpu_blocks": "100",
                    "num_cpu_blocks": "None",
                    "kv_cache_size_tokens": "1600",
                },
                "values": [[1, "1"]],
            },
        ]
    )

    assert snapshot["hbm_capacity_tokens"] == 3200
    assert snapshot["cpu_capacity_tokens"] == 400
    assert snapshot["effective_capacity_tokens"] == 3600


@pytest.mark.asyncio
async def test_collect_benchmark_observability_builds_aligned_series(monkeypatch):
    monkeypatch.setattr(
        service,
        "_deployment_target",
        lambda execution_id, cluster_id: (object(), "benchmark-ns", "cluster-1"),
    )

    async def fake_port(_cluster_id):
        return 19090

    async def fake_query_range(_client, promql, _start, _end, _step):
        if "external_prefix_cache_hits_total" in promql:
            value = "25"
        elif "prefix_cache_hits_total" in promql:
            value = "80"
        else:
            value = "75" if "kv_cache_usage" in promql else "4"
        metric = {"endpoint_name": "prefill-0"} if "by (endpoint_name" in promql else {"pod": "prefill-0"}
        return [{"metric": metric, "values": [[1000, value], [1005, value]]}]

    async def fake_components(_namespace, _cluster_id):
        return {"epp": ["epp-0"], "prefill": ["prefill-0"], "decode": [], "model-server": []}

    monkeypatch.setattr(service, "_prometheus_local_port", fake_port)
    monkeypatch.setattr(service, "_query_range", fake_query_range)
    monkeypatch.setattr(service, "_discover_components", fake_components)

    result = await service.collect_benchmark_observability(
        "execution-1",
        "2026-09-02T10:00:00Z",
        "2026-09-02T10:01:00Z",
    )

    assert result["status"] == "available"
    assert result["flow_status"] == "available"
    assert result["namespace"] == "benchmark-ns"
    assert len(result["series"]) == 2
    assert result["series"][0]["kv_cache_usage_percent"] == 75.0
    assert result["series"][0]["prefix_cache_hit_percent"] == 80.0
    assert result["series"][0]["external_prefix_cache_hit_percent"] == 25.0
    assert result["summary"]["external_prefix_cache_hit_percent"]["mean"] == 25.0
    assert result["summary"]["queue_depth"]["p99"] == 4.0
    assert result["per_pod"][0]["request_rate_rps"]["mean"] == 4.0
    assert result["per_endpoint"][0]["inflight_token_load"]["mean"] == 4.0
    assert result["derived"]["token_load_cv"] == 0.0
    assert result["derived"]["request_destination_distribution"][0]["percent"] == 100.0
    assert result["derived"]["avoided_prefill_ratio"] == 100.0
    assert result["derived"]["kv_transfer_success_rate"] == 50.0
    assert result["derived"]["matched_blocks_per_lookup"] == 1.0
    assert result["role_series"][0]["prefill_request_rate_rps"] == 4.0
    assert result["evidence"]["kv_transfer_success_rate"]["status"] == "measured"
    assert result["evidence"]["transfer_bandwidth"]["source"] == "vLLM NIXL bytes-transferred histogram"
    assert result["evidence"]["hbm_capacity"]["status"] == "unavailable"
    assert "no finite sample" in result["evidence"]["hbm_capacity"]["reason"]
    assert result["router"]["scheduler_latency_p95_ms"]["mean"] == 4.0
    assert datetime.fromisoformat(result["series"][0]["timestamp"].replace("Z", "+00:00")).tzinfo == UTC


@pytest.mark.asyncio
async def test_collect_benchmark_observability_reports_missing_prometheus(monkeypatch):
    monkeypatch.setattr(
        service,
        "_deployment_target",
        lambda execution_id, cluster_id: (object(), "benchmark-ns", "cluster-1"),
    )

    async def fake_port(_cluster_id):
        return None

    monkeypatch.setattr(service, "_prometheus_local_port", fake_port)

    result = await service.collect_benchmark_observability(
        "execution-1",
        "2026-09-02T10:00:00Z",
        "2026-09-02T10:01:00Z",
    )

    assert result["status"] == "unavailable"
    assert result["series"] == []


@pytest.mark.asyncio
async def test_engine_token_sources_are_collected_separately_from_router_estimates(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "benchmark-ns", "cluster-1"))

    async def port(*_):
        return 19090

    async def components(*_):
        return {"prefill": [], "decode": []}

    observed_queries = []

    async def query(_client, promql, *_):
        observed_queries.append(promql)
        rates = {
            'source="local_compute"': 20,
            'source="local_cache_hit"': 60,
            'source="external_kv_transfer"': 10,
            "vllm:prompt_tokens_recomputed_total": 2,
        }
        for needle, value in rates.items():
            if needle in promql:
                return [{"values": [[1788998400, str(value)], [1788998405, str(value)]]}]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", port)
    monkeypatch.setattr(service, "_discover_components", components)
    monkeypatch.setattr(service, "_query_range", query)
    result = await service.collect_benchmark_observability(
        "execution-1", "2026-09-10T00:00:00Z", "2026-09-10T00:01:00Z"
    )
    for field, value in {
        "engine_prompt_local_compute_tps": 20,
        "engine_prompt_local_cache_tps": 60,
        "engine_prompt_external_tps": 10,
        "engine_prompt_recomputed_tps": 2,
    }.items():
        assert result["summary"][field]["mean"] == value
        assert result["series"][0][field] == value
        assert result["availability"]["metrics"][field] == "available"
        assert "vLLM" in result["metric_sources"][field]
    assert any(
        'vllm:prompt_tokens_by_source_total{namespace="benchmark-ns", source="local_compute"}' in q
        for q in observed_queries
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [384.0, -384.0])
async def test_benchmark_window_uses_prometheus_clock_and_restores_local_timeline(monkeypatch, offset):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench", "cluster"))

    async def port(*_):
        return 19090

    async def clock(_client):
        return offset

    async def components(*_):
        return {"prefill": [], "decode": ["decode-0"]}

    async def query(_client, promql, start, end, step):
        assert service._iso_timestamp(start).timestamp() == 1000 + offset
        assert service._iso_timestamp(end).timestamp() == 1060 + offset
        if "request_success_total" in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000 + offset, "2"], [1005 + offset, "0"]]}]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", port)
    monkeypatch.setattr(service, "_prometheus_clock_offset", clock)
    monkeypatch.setattr(service, "_discover_components", components)
    monkeypatch.setattr(service, "_query_range", query)
    result = await service.collect_benchmark_observability("execution", "1970-01-01T00:16:40Z", "1970-01-01T00:17:40Z")
    assert result["clock_alignment"]["offset_seconds"] == offset
    assert result["flow_status"] == "available"
    assert result["summary"]["request_rate_rps"]["mean"] == 1
    assert result["series"][0]["timestamp"] == "1970-01-01T00:16:40Z"
    assert result["role_series"][0]["timestamp"] == "1970-01-01T00:16:40Z"


@pytest.mark.asyncio
async def test_prometheus_clock_offset_uses_round_trip_midpoint(monkeypatch):
    import httpx

    readings = iter([1000.0, 1002.0])
    from types import SimpleNamespace

    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: next(readings)))

    def respond(request):
        assert request.url.params["query"] == "time()"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "scalar", "result": [1385, "1385"]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://prometheus") as client:
        assert await _prometheus_clock_offset(client) == 384


@pytest.mark.asyncio
async def test_profiling_uses_direct_prometheus_endpoint_before_port_forward(monkeypatch):
    direct = PrometheusEndpoint(base_url="http://10.0.0.5:9090", source="explicit_url")
    resolve_mock = AsyncMock(return_value=direct)
    local_port_mock = AsyncMock(side_effect=AssertionError("port-forward must not run when direct URL exists"))
    monkeypatch.setattr(service, "resolve_prometheus_endpoint", resolve_mock)
    monkeypatch.setattr(service, "_prometheus_local_port", local_port_mock)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://10.0.0.5:9090/")
        if request.url.path == "/api/v1/query":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "7"]}]}})
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404)

    created_urls = []

    def fake_client(endpoint, *, timeout=5.0):
        url = endpoint.base_url if isinstance(endpoint, PrometheusEndpoint) else str(endpoint)
        created_urls.append(url)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(service, "create_prometheus_client", fake_client)
    monkeypatch.setattr(pressure, "create_prometheus_client", fake_client)
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-ns", "cluster-direct"))
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["decode-0"]}),
    )
    monkeypatch.setattr(pressure, "_deployment_target", lambda *_: (object(), "bench-ns", "cluster-direct"))
    monkeypatch.setattr(
        pressure,
        "_discover_components",
        AsyncMock(return_value={"prefill": [], "decode": ["decode-0"]}),
    )

    assert await service.query_prometheus("cluster-direct", "up") == [{"value": [1, "7"]}]
    flow = await service.build_flow_map("exec-direct", "cluster-direct")
    assert flow.prometheus_reachable is True

    pressure.clear_cache()
    p = await pressure.collect_execution_pressure("exec-direct", "cluster-direct", force=True)
    assert p.reason == "insufficient_coverage"

    assert created_urls == ["http://10.0.0.5:9090"] * 3
    assert all(call.kwargs == {"allow_port_forward": False} for call in resolve_mock.await_args_list)
    local_port_mock.assert_not_called()


@pytest.mark.asyncio
async def test_flow_map_route_and_service_forward_monitoring_namespace_override(monkeypatch):
    from fastapi.testclient import TestClient

    from llm_d_lens.api.main import app
    from llm_d_lens.cluster import registry as cluster_registry

    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-ns", "2790b2ae"))
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": []}),
    )
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda cid: {"monitoring_namespace": "llm-d-monitoring-123454"} if cid == "2790b2ae" else None,
        raising=False,
    )

    seen_calls: list[dict] = []

    async def fake_resolve(cluster_id, *, namespace=None, allow_port_forward=True, use_cache=True):
        seen_calls.append(
            {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "allow_port_forward": allow_port_forward,
                "use_cache": use_cache,
            }
        )
        return PrometheusEndpoint(
            base_url="http://10.0.0.5:9090",
            source="explicit_url",
            namespace=namespace or "llm-d-monitoring-123454",
        )

    monkeypatch.setattr(service, "resolve_prometheus_endpoint", fake_resolve)

    # 1. Omitted monitoring_namespace falls back to cluster's monitoring_namespace via _prometheus_endpoint
    res_default = await service.build_flow_map("exec-1", "2790b2ae")
    assert res_default.prometheus_reachable is True
    assert seen_calls == [{"cluster_id": "2790b2ae", "namespace": None, "allow_port_forward": False, "use_cache": True}]

    # 2. Port-forward fallback without override carries cluster.monitoring_namespace on PrometheusEndpoint
    monkeypatch.setattr(
        service,
        "_monitoring_namespace_for_cluster",
        lambda cid: "llm-d-monitoring-123454" if cid == "2790b2ae" else "llm-d-monitoring",
    )
    monkeypatch.setattr(service, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prometheus_local_port", AsyncMock(return_value=41234))
    ep_default = await service._prometheus_endpoint("2790b2ae")
    assert ep_default is not None and ep_default.namespace == "llm-d-monitoring-123454"

    # 3. Route forwards explicit monitoring_namespace override and rejects invalid DNS labels with 422
    seen_calls.clear()
    monkeypatch.setattr(service, "resolve_prometheus_endpoint", fake_resolve)
    with TestClient(app) as client:
        ok_resp = client.get(
            "/api/v1/monitoring/profiling/flow-map"
            "?execution_id=exec-1&cluster_id=2790b2ae&monitoring_namespace=override-mon"
        )
        assert ok_resp.status_code == 200
        assert seen_calls == [
            {"cluster_id": "2790b2ae", "namespace": "override-mon", "allow_port_forward": False, "use_cache": True}
        ]

        bad_resp = client.get(
            "/api/v1/monitoring/profiling/flow-map"
            "?execution_id=exec-1&cluster_id=2790b2ae&monitoring_namespace=Invalid_Name"
        )
        assert bad_resp.status_code == 422


@pytest.mark.asyncio
async def test_prometheus_endpoint_returns_none_cleanly_on_port_forward_or_403_error(monkeypatch):
    from llm_d_lens.infrastructure.kubernetes import PortForwardError

    async def raise_on_pf(_cluster_id, *, namespace=None, allow_port_forward=True, use_cache=True):
        del namespace, use_cache
        if not allow_port_forward:
            return
        raise PortForwardError("Error from server (Forbidden): pods/portforward is forbidden")

    monkeypatch.setattr(service, "resolve_prometheus_endpoint", raise_on_pf)
    assert await service._prometheus_endpoint("c-non-admin") is None
    assert await service._prometheus_endpoint("c-non-admin", namespace="custom-mon") is None

    async def raise_k8s_403(_cluster_id, *, namespace=None, allow_port_forward=True, use_cache=True):
        del namespace, use_cache
        if not allow_port_forward:
            return
        raise RuntimeError("Kubernetes get failed (Forbidden, HTTP 403)")

    monkeypatch.setattr(service, "resolve_prometheus_endpoint", raise_k8s_403)
    assert await service._prometheus_endpoint("c-non-admin") is None


@pytest.mark.asyncio
async def test_discover_components_uncached_falls_back_to_prometheus_http_when_k8s_restricted(monkeypatch):
    service._components_cache.clear()
    monkeypatch.setattr(
        service,
        "list_resources",
        AsyncMock(side_effect=RuntimeError("Kubernetes list failed (Forbidden, HTTP 403)")),
    )
    monkeypatch.setattr(service, "_discover_epp_service", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query == 'up{namespace="ns-restricted"}':
            rows = [
                {"metric": {"pod": "pd-prefill-0"}, "value": [1, "1"]},
                {"metric": {"pod": "pd-decode-0"}, "value": [1, "1"]},
                # EPP pod whose release name contains both "prefill" and "decode" must NOT be classified as serving!
                {
                    "metric": {"service": "ms-prefill-decode-epp", "pod": "ms-prefill-decode-epp-7f8b9"},
                    "value": [1, "1"],
                },
                {"metric": {"pod": "prefill-decode-epp-podonly"}, "value": [1, "1"]},
            ]
        elif query == 'vllm:num_requests_running{namespace="ns-restricted"}':
            rows = [
                {"metric": {"pod": "pd-prefill-0"}, "value": [1, "0"]},
                {"metric": {"pod": "pd-decode-0"}, "value": [1, "2"]},
                {"metric": {"pod": "vllm-single-0"}, "value": [1, "1"]},
            ]
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "data": {"result": rows}})

    monkeypatch.setattr(
        service,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=endpoint.base_url,
            timeout=timeout,
        ),
    )

    discovered = await service._discover_components("ns-restricted", "c-na")
    assert discovered == {
        "epp": ["ms-prefill-decode-epp"],
        "prefill": ["pd-prefill-0"],
        "decode": ["pd-decode-0", "vllm-single-0"],
    }


def test_re2_escape_promql_double_quoted_regex_safety():
    import re

    # Hyphens are NOT escaped (RE2 rejects `\-` outside character classes)
    assert service._re2_escape("vllm-decode-0") == "vllm-decode-0"
    # Dots are PromQL-escaped (`\\.`) so PromQL unescapes `"pod\\.1"` to RE2 `pod\.1`
    assert service._re2_escape("pod.1") == r"pod\.1".replace("\\", "\\\\")
    assert service._re2_escape("pod.1") == "pod\\\\.1"
    # Literal backslashes become 4 backslashes in PromQL (`\\\\` -> RE2 `\\` -> literal `\`)
    assert service._re2_escape(r"pod\slash") == r"pod\\\\slash"
    # Quotes and newlines cannot break out of `pod=~"..."`
    escaped_inj = service._re2_escape('pod"inject}\n\r')
    assert escaped_inj == 'pod\\"inject\\\\}\\n\\r'

    # Simulate PromQL double-quoted string unescaping -> RE2 compilation -> exact match
    def promql_unescape(raw: str) -> str:
        out: list[str] = []
        idx = 0
        escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
        while idx < len(raw):
            if raw[idx] == "\\":
                nxt = raw[idx + 1]
                assert nxt in escapes, f"Invalid PromQL escape: \\{nxt}"
                out.append(escapes[nxt])
                idx += 2
            else:
                assert raw[idx] not in {'"', "\n", "\r"}
                out.append(raw[idx])
                idx += 1
        return "".join(out)

    for candidate in ("vllm-decode-0", "pod.1", r"pod\slash", 'pod"inject}\n\r', "a+b*c?(d)[e]{1}^$"):
        re2_expr = promql_unescape(service._re2_escape(candidate))
        assert re.fullmatch(re2_expr, candidate) is not None
    assert re.fullmatch(promql_unescape(service._re2_escape("pod.1")), "podX1") is None


@pytest.mark.asyncio
async def test_wait_for_deployment_metrics_not_blocked_by_unscraped_epp_on_non_admin(monkeypatch):
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    monkeypatch.setattr(service, "_deployment_target", lambda *_: (None, "ns-na", "c-na"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"prefill": [], "decode": ["decode-0"], "epp": ["vllm-epp"]}),
    )

    async def query(_client, expression):
        if expression.startswith("up{"):
            # Serving pod is up; EPP is unscraped (e.g. non-admin lacked system:auth-delegator)
            return [{"metric": {"pod": "decode-0"}, "value": [0, "1"]}]
        return [{"metric": {"pod": "decode-0"}, "value": [0, "2"]}]

    monkeypatch.setattr(service, "_query", query)
    monkeypatch.setattr(
        service,
        "probe_monitoring_access",
        AsyncMock(return_value=MonitoringAccessResult(reachable=True, access_mode="non_admin", can_manage=False)),
    )
    assert await service.wait_for_deployment_metrics("exec-na", timeout_seconds=0) is True

    # On admin clusters without allow_optional_epp, unscraped EPP still blocks readiness
    monkeypatch.setattr(
        service,
        "probe_monitoring_access",
        AsyncMock(return_value=MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True)),
    )
    assert await service.wait_for_deployment_metrics("exec-na", timeout_seconds=0) is False
    assert await service.wait_for_deployment_metrics("exec-na", timeout_seconds=0, allow_optional_epp=True) is True


@pytest.mark.asyncio
async def test_collect_benchmark_observability_exporter_labelled_gpu_fallback_on_k8s_403(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-na", "c-na"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "list_resources",
        AsyncMock(side_effect=RuntimeError("Kubernetes list failed (Forbidden, HTTP 403)")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["decode-0"]}),
    )

    async def fake_instant_query(_client, promql):
        if "DCGM_FI_DEV_GPU_UTIL" in promql and 'namespace="bench-na"' in promql:
            return [{"metric": {"namespace": "bench-na", "pod": "decode-0"}, "value": [1000, "65"]}]
        return []

    async def fake_range_query(_client, promql, _start, _end, _step):
        if "DCGM_FI_DEV_GPU_UTIL" in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000, "65"], [1005, "75"]]}]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000, "5"], [1005, "5"]]}]
        return []

    monkeypatch.setattr(service, "_query", fake_instant_query)
    monkeypatch.setattr(service, "_query_range", fake_range_query)

    result = await service.collect_benchmark_observability(
        "exec-na",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )
    assert result["status"] == "available"
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 70.0
    assert result["per_pod"][0]["gpu_utilization_percent"]["mean"] == 70.0
    assert result["evidence"]["gpu_xpu_utilization"]["status"] == "measured"


@pytest.mark.asyncio
async def test_kv_offload_cpu_tier_epp_router_and_host_promql_and_trapezoidal_bytes(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-kv", "c-kv"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["ms-epp-0"], "prefill": [], "decode": ["vllm-decode-0"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {"metadata": {"name": "llm-d-lens-sim-job-1"}, "spec": {"nodeName": "client-only-node"}},
                {"metadata": {"name": "trace-replayer-abc"}, "spec": {"nodeName": "client-only-node"}},
                {"metadata": {"name": "ms-epp-0"}, "spec": {"nodeName": "epp-node-2"}},
                {"metadata": {"name": "vllm-decode-0"}, "spec": {"nodeName": "gpu-node-1"}},
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "kv_offload_cpu_cache_usage_perc" in promql:
            return [{"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "42.5"], [1005, "47.5"]]}]
        if "vllm:kv_offload_total_bytes_total" in promql and "GPU_to_" in promql:
            # 2 points 5s apart: 100 and 300 B/s -> trapezoidal integral = 0.5*(100+300)*5 = 1000 B
            # plus 5s..10s: 300 and 100 B/s -> 1000 B => 2000 B total (vs mean*60s = 10000 B!)
            return [{"values": [[1000, "100"], [1005, "300"], [1010, "100"]]}]
        if "vllm:kv_offload_total_bytes_total" in promql and "_to_GPU" in promql:
            return [{"values": [[1000, "50"], [1005, "50"]]}]
        if "vllm:kv_offload_lookup_sync_delay_seconds_bucket" in promql:
            return [{"values": [[1000, "12.5"], [1005, "15.5"]]}]
        if "llm_d_epp_prefix_prompt_tokens_sum" in promql:
            return [{"values": [[1000, "100"], [1005, "100"]]}]
        if "llm_d_epp_prefix_predicted_cached_tokens_sum" in promql:
            return [{"values": [[1000, "60"], [1005, "60"]]}]
        if "llm_d_epp_prefix_indexer_size" in promql:
            return [{"values": [[1000, "256"], [1005, "256"]]}]
        if "node_cpu_seconds_total" in promql:
            return [{"values": [[1000, "55.0"], [1005, "88.0"]]}]
        if "node_hwmon_power_average_watt" in promql:
            return [{"values": [[1000, "200.0"], [1005, "300.0"]]}]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "5"], [1005, "5"]]}]
        return []

    async def fake_instant_query(_client, _promql):
        return []

    monkeypatch.setattr(service, "_query", fake_instant_query)
    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-kv",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert result["nodes"] == ["gpu-node-1"]
    assert result["summary"]["cpu_cache_usage_percent"]["mean"] == 45.0
    assert result["per_pod"][0]["cpu_cache_usage_percent"]["mean"] == 45.0
    assert result["summary"]["kv_offload_lookup_latency_p95_ms"]["mean"] == 14.0
    assert result["derived"]["offloaded_bytes"] == 2000
    assert result["derived"]["restored_bytes"] == 250
    assert result["router"]["index_entry_count"]["mean"] == 256.0
    assert result["derived"]["avoided_prefill_ratio"] == 60.0
    assert result["summary"]["host_cpu_usage_percent"]["max"] == 88.0

    # Verify PromQL details
    assert any("vllm:kv_offload_cpu_cache_usage_perc" in q for q in seen_queries)
    assert any(
        "vllm:kv_offload_total_bytes_total" in q and "vllm:kv_offload_store_bytes_total" in q for q in seen_queries
    )
    assert any("vllm:kv_offload_total_time_total" in q and "vllm:kv_offload_load_time_total" in q for q in seen_queries)
    assert any("llm_d_epp_prefix_indexer_size" in q for q in seen_queries)
    host_qs = [q for q in seen_queries if "node_" in q or "kepler_" in q]
    assert host_qs
    assert all("[30s]" not in q for q in host_qs)
    assert all("client-only-node" not in q for q in host_qs)
    assert all("epp-node-2" not in q for q in host_qs)
    assert any(
        'on(instance) group_left(nodename) max by (instance, nodename) (node_uname_info{nodename=~"(gpu-node-1)(\\\\..+)?"})'
        in q
        for q in host_qs
    )
    assert any('chip!~"(?:pci)?[0-9a-f]{4}:.*"' in q for q in host_qs)

    # Fallback: non-client & non-EPP pods if serving worker pods are not discovered, then all pods
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["ms-epp-0"], "prefill": [], "decode": []}),
    )

    async def non_worker_pods(kind, **_kwargs):
        if kind == "pods":
            return [
                {"metadata": {"name": "ms-epp-0"}, "spec": {"nodeName": "epp-node-2"}},
                {"metadata": {"name": "custom-worker-0"}, "spec": {"nodeName": "worker-node-3"}},
            ]
        return []

    monkeypatch.setattr(service, "list_resources", non_worker_pods)
    res_non_epp = await service.collect_benchmark_observability(
        "exec-kv",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert res_non_epp["nodes"] == ["worker-node-3"]

    # Fallback to all pods if only client pods have nodeName
    async def client_only_pods(kind, **_kwargs):
        if kind == "pods":
            return [{"metadata": {"name": "aiperf-runner-1"}, "spec": {"nodeName": "fallback-node"}}]
        return []

    monkeypatch.setattr(service, "list_resources", client_only_pods)
    seen_queries.clear()
    res_fb = await service.collect_benchmark_observability(
        "exec-kv",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert res_fb["nodes"] == ["fallback-node"]


@pytest.mark.asyncio
async def test_collect_benchmark_observability_preserves_router_series_with_submicrosecond_timestamps(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-router", "c-router"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": [], "decode": ["decode-0"]}),
    )

    async def fake_clock(_client):
        return 0.123456789

    monkeypatch.setattr(service, "_prometheus_clock_offset", fake_clock)

    ts1 = 1795601914.370178872  # ts1 - offset = 1795601914.246722083 (sub-microsecond float)
    ts2 = 1795601919.370178872  # router-only timestamp

    async def fake_range(_client, promql, _start, _end, _step):
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[ts1, "5"]]}]
        if "llm_d_epp_scheduler_e2e_duration_seconds_bucket" in promql and "0.50" in promql:
            return [{"values": [[ts1, "1.2"], [ts2, "1.8"]]}]
        if "llm_d_epp_plugin_duration_seconds_bucket" in promql and "0.50" in promql:
            return [{"values": [[ts1, "0.4"], [ts2, "0.6"]]}]
        if "llm_d_epp_request_total" in promql:
            return [{"values": [[ts1, "10"], [ts2, "12"]]}]
        if "llm_d_epp_request_cached_tokens_sum" in promql:
            return [{"values": [[ts1, "80"], [ts2, "90"]]}]
        if "llm_d_epp_request_input_tokens_sum" in promql:
            return [{"values": [[ts1, "100"], [ts2, "100"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-router",
        "2026-11-25T10:18:30Z",
        "2026-11-25T10:19:00Z",
    )

    assert result["status"] == "available"
    assert len(result["series"]) == 2
    assert result["series"][0]["request_rate_rps"] == 5.0
    assert result["series"][0]["router_scheduler_latency_p50_ms"] == 1.2
    assert result["series"][0]["router_plugin_latency_p50_ms"] == 0.4
    assert result["series"][0]["router_request_rate_rps"] == 10.0
    assert result["series"][0]["router_cached_token_rate_tps"] == 80.0
    assert result["series"][0]["router_input_token_rate_tps"] == 100.0

    # Router-only timestamp still appears in series without polluting summary
    assert "request_rate_rps" not in result["series"][1]
    assert result["series"][1]["router_scheduler_latency_p50_ms"] == 1.8
    assert result["series"][1]["router_plugin_latency_p50_ms"] == 0.6
    assert result["series"][1]["router_request_rate_rps"] == 12.0
    assert result["series"][1]["router_cached_token_rate_tps"] == 90.0
    assert not any(key.startswith("router_") for key in result["summary"])
    assert result["router"]["scheduler_latency_p50_ms"]["mean"] == pytest.approx(1.5)
    assert result["derived"]["avoided_prefill_ratio"] == 85.0
    assert result["role_series"][0]["timestamp"] == result["series"][0]["timestamp"]


@pytest.mark.asyncio
async def test_collect_benchmark_observability_router_only_metrics_produce_series(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-router-only", "c-router"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": [], "decode": []}),
    )

    async def fake_range(_client, promql, _start, _end, _step):
        if "llm_d_epp_request_total" in promql:
            return [{"values": [[1795601914.246722083, "7.5"], [1795601919.246722083, "8.5"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-router-only",
        "2026-11-25T10:18:30Z",
        "2026-11-25T10:19:00Z",
    )

    assert result["status"] == "available"
    assert result["summary"] == {}
    assert [pt["router_request_rate_rps"] for pt in result["series"]] == [7.5, 8.5]


@pytest.mark.asyncio
async def test_collect_benchmark_observability_clamps_percentage_and_ratio_jitter(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-clamp", "c-clamp"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": ["prefill-0"], "decode": ["decode-0"]}),
    )

    async def fake_instant(_client, promql):
        if "DCGM_FI_DEV_GPU_UTIL" in promql and 'namespace="bench-clamp"' in promql:
            return [{"metric": {"namespace": "bench-clamp", "pod": "decode-0"}, "value": [1000, "100.00025"]}]
        return []

    async def fake_range(_client, promql, _start, _end, _step):
        if "DCGM_FI_DEV_GPU_UTIL" in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000, "100.00025421224746"], [1005, "-0.05"]]}]
        if "vllm:kv_cache_usage_perc" in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000, "100.00025"], [1005, "50.0"]]}]
        if "DCGM_FI_DEV_MEM_COPY_UTIL" in promql:
            return [{"metric": {"pod": "decode-0"}, "values": [[1000, "1.00025"], [1005, "0.5"]]}]
        return []

    monkeypatch.setattr(service, "_query", fake_instant)
    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-clamp",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )

    assert result["series"][0]["gpu_utilization_percent"] == 100.0
    assert result["series"][1]["gpu_utilization_percent"] == 0.0
    assert result["series"][0]["kv_cache_usage_percent"] == 100.0
    assert result["summary"]["gpu_utilization_percent"]["max"] == 100.0
    assert result["summary"]["gpu_utilization_percent"]["mean"] == 50.0
    assert result["summary"]["kv_cache_usage_percent"]["max"] == 100.0
    assert result["per_pod"][0]["gpu_utilization_percent"]["max"] == 100.0
    assert result["role_series"][0]["decode_gpu_utilization_percent"] == 100.0
    assert result["derived"]["role_summary"]["decode"]["kv_cache_usage_percent"] == 75.0
    if "gpu_memory_utilization_ratio" in result["summary"]:
        assert result["summary"]["gpu_memory_utilization_ratio"]["max"] == 1.0


def test_extract_cache_config_from_pods_parses_argv_shell_clamping_and_skips_non_serving():
    assert service._extract_cache_config_from_pods([]) == {
        "kv_cache_metrics_enabled": False,
        "kv_cache_metrics_sample": 1.0,
    }

    # Client and EPP pods are ignored even if they carry the flag
    ignored_only = [
        {"metadata": {"name": "llm-d-lens-sim-123"}, "spec": {"containers": [{"args": ["--kv-cache-metrics"]}]}},
        {"metadata": {"name": "trace-replayer-abc"}, "spec": {"containers": [{"args": ["--kv-cache-metrics"]}]}},
        {
            "metadata": {"name": "gateway-epp-0", "labels": {"llm-d.ai/role": "epp"}},
            "spec": {"containers": [{"args": ["--kv-cache-metrics"]}]},
        },
    ]
    assert service._extract_cache_config_from_pods(ignored_only) == {
        "kv_cache_metrics_enabled": False,
        "kv_cache_metrics_sample": 1.0,
    }

    # Default 0.01 when enabled without explicit sample
    assert service._extract_cache_config_from_pods(
        [{"metadata": {"name": "vllm-decode-0"}, "spec": {"containers": [{"args": ["--kv-cache-metrics"]}]}}]
    ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 0.01}

    # Explicit =0.05 and space-separated 0.25
    assert service._extract_cache_config_from_pods(
        [
            {
                "metadata": {"name": "vllm-decode-0"},
                "spec": {"containers": [{"args": ["--kv-cache-metrics", "--kv-cache-metrics-sample=0.05"]}]},
            }
        ]
    ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 0.05}
    assert service._extract_cache_config_from_pods(
        [
            {
                "metadata": {"name": "vllm-decode-0"},
                "spec": {"containers": [{"args": ["--kv-cache-metrics=true", "--kv-cache-metrics-sample", "0.25"]}]},
            }
        ]
    ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 0.25}

    # Shell -c command string
    assert service._extract_cache_config_from_pods(
        [
            {
                "metadata": {"name": "vllm-decode-0"},
                "spec": {
                    "containers": [
                        {
                            "command": ["/bin/sh", "-c"],
                            "args": ["exec vllm serve model --kv-cache-metrics --kv-cache-metrics-sample 0.05"],
                        }
                    ]
                },
            }
        ]
    ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 0.05}

    # Clamping & invalid values
    for bad_sample in ("0", "-0.5", "nan", "inf", "bad"):
        assert service._extract_cache_config_from_pods(
            [
                {
                    "metadata": {"name": "vllm-decode-0"},
                    "spec": {"containers": [{"args": ["--kv-cache-metrics", "--kv-cache-metrics-sample", bad_sample]}]},
                }
            ]
        ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 0.01}

    assert service._extract_cache_config_from_pods(
        [
            {
                "metadata": {"name": "vllm-decode-0"},
                "spec": {"containers": [{"args": ["--kv-cache-metrics", "--kv-cache-metrics-sample=2.5"]}]},
            }
        ]
    ) == {"kv_cache_metrics_enabled": True, "kv_cache_metrics_sample": 1.0}

    # Disabled via --no-kv-cache-metrics or --kv-cache-metrics=false
    for disable_args in (["--kv-cache-metrics", "--no-kv-cache-metrics"], ["--kv-cache-metrics=false"]):
        assert service._extract_cache_config_from_pods(
            [{"metadata": {"name": "vllm-decode-0"}, "spec": {"containers": [{"args": disable_args}]}}]
        ) == {"kv_cache_metrics_enabled": False, "kv_cache_metrics_sample": 1.0}


@pytest.mark.asyncio
async def test_collect_benchmark_observability_scrapes_kv_block_eviction_and_residency_metrics(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-evict", "c-evict"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["vllm-decode-0"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "vllm-decode-0"},
                    "spec": {
                        "containers": [{"args": ["--kv-cache-metrics", "--kv-cache-metrics-sample=0.05"]}],
                    },
                }
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "rate(vllm:kv_block_idle_before_evict_seconds_count" in promql:
            # sample_rate=0.05 -> scaled by 20x: [60.0, 120.0, 90.0]
            return [{"values": [[1000, "3.0"], [1005, "6.0"], [1010, "4.5"]]}]
        if "increase(vllm:kv_block_idle_before_evict_seconds_count" in promql:
            # final finite value 5.0 / 0.05 = 100
            return [{"values": [[1000, "2.0"], [1005, "4.0"], [1010, "5.0"]]}]
        if "vllm:kv_block_lifetime_seconds_bucket" in promql:
            if "0.50" in promql:
                return [{"values": [[1000, "10.0"], [1005, "14.0"]]}]
            if "0.90" in promql:
                return [{"values": [[1000, "30.0"], [1005, "34.0"]]}]
            if "0.99" in promql:
                return [{"values": [[1000, "60.0"], [1005, "64.0"]]}]
        if "vllm:kv_block_idle_before_evict_seconds_bucket" in promql:
            if "0.50" in promql:
                return [{"values": [[1000, "2.0"], [1005, "4.0"]]}]
            if "0.90" in promql:
                return [{"values": [[1000, "6.0"], [1005, "8.0"]]}]
            if "0.99" in promql:
                return [{"values": [[1000, "12.0"], [1005, "14.0"]]}]
        if "vllm:kv_block_reuse_gap_seconds_bucket" in promql:
            if "0.50" in promql:
                return [{"values": [[1000, "1.0"], [1005, "3.0"]]}]
            if "0.90" in promql:
                return [{"values": [[1000, "4.0"], [1005, "6.0"]]}]
            if "0.99" in promql:
                return [{"values": [[1000, "9.0"], [1005, "11.0"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-evict",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert result["cache_config"]["kv_cache_metrics_enabled"] is True
    assert result["cache_config"]["kv_cache_metrics_sample"] == 0.05
    assert [pt["kv_blocks_evicted_per_minute"] for pt in result["series"]] == [60.0, 120.0, 90.0]
    assert result["summary"]["kv_blocks_evicted_per_minute"]["mean"] == 90.0
    assert result["summary"]["kv_blocks_evicted_per_minute"]["min"] == 60.0
    assert result["summary"]["kv_blocks_evicted_per_minute"]["max"] == 120.0
    assert result["summary"]["kv_blocks_evicted_per_minute"]["latest"] == 90.0
    assert result["derived"]["kv_blocks_evicted_total"] == 100

    assert result["summary"]["kv_block_lifetime_seconds"] == {"p50": 12.0, "p90": 32.0, "p99": 62.0}
    assert result["summary"]["kv_block_idle_before_evict_seconds"] == {"p50": 3.0, "p90": 7.0, "p99": 13.0}
    assert result["summary"]["kv_block_reuse_gap_seconds"] == {"p50": 2.0, "p90": 5.0, "p99": 10.0}
    assert result["series"][0]["kv_block_lifetime_p50_seconds"] == 10.0
    assert result["series"][0]["kv_block_idle_before_evict_p90_seconds"] == 6.0
    assert result["series"][0]["kv_block_reuse_gap_p99_seconds"] == 9.0

    avail = result["availability"]["metrics"]
    assert avail["kv_blocks_evicted_per_minute"] == "available"
    assert avail["kv_block_lifetime_seconds"] == "available"
    assert avail["kv_block_idle_before_evict_seconds"] == "available"
    assert avail["kv_block_reuse_gap_seconds"] == "available"
    assert any('le="+Inf"' in q and "vllm:kv_block_lifetime_seconds_bucket" in q for q in seen_queries)


@pytest.mark.asyncio
async def test_collect_benchmark_observability_kv_block_fallback_and_nan_guards(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-evict-fb", "c-evict"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["vllm-decode-0"]}),
    )

    async def fake_range(_client, promql, _start, _end, _step):
        # Pods absent -> sample_rate = 1.0; increase() empty -> trapezoidal integration fallback
        if "rate(vllm:kv_block_idle_before_evict_seconds_count" in promql:
            # 60/min = 1/s and 180/min = 3/s over 5s -> 0.5*(1+3)*5 = 10 blocks
            return [{"values": [[1000, "60.0"], [1005, "180.0"]]}]
        if "vllm:kv_block_lifetime_seconds_bucket" in promql:
            return [{"values": [[1000, "NaN"], [1005, "Inf"]]}]
        if "vllm:kv_block_idle_before_evict_seconds_bucket" in promql:
            return [{"values": [[1000, "-0.5"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_benchmark_observability(
        "exec-evict-fb",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert result["cache_config"]["kv_cache_metrics_enabled"] is False
    assert result["cache_config"]["kv_cache_metrics_sample"] == 1.0
    assert result["derived"]["kv_blocks_evicted_total"] == 10
    assert "kv_block_lifetime_seconds" not in result["summary"]
    assert "kv_block_idle_before_evict_seconds" not in result["summary"]
    assert "kv_block_reuse_gap_seconds" not in result["summary"]
    assert "kv_block_lifetime_p50_seconds" not in result["series"][0]
    assert "kv_block_idle_before_evict_p50_seconds" not in result["series"][0]

    avail = result["availability"]["metrics"]
    assert avail["kv_blocks_evicted_per_minute"] == "available"
    assert avail["kv_block_lifetime_seconds"] == "unavailable"
    assert avail["kv_block_idle_before_evict_seconds"] == "unavailable"
    assert avail["kv_block_reuse_gap_seconds"] == "unavailable"

    # When eviction rate is also absent, availability reports unavailable
    monkeypatch.setattr(service, "_query_range", AsyncMock(return_value=[]))
    empty_res = await service.collect_benchmark_observability(
        "exec-evict-empty",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert empty_res["availability"]["metrics"]["kv_blocks_evicted_per_minute"] == "unavailable"


@pytest.mark.asyncio
async def test_collect_benchmark_observability_scrapes_prompt_token_histograms_and_role_sources(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-tok", "c-tok"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )

    # 1. Disaggregated mode with client/EPP pods excluded from prompt_tokens_by_role_source
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(
            return_value={
                "epp": ["gateway-epp-0"],
                "prefill": ["vllm-prefill-0"],
                "decode": ["vllm-decode-0"],
                "model-server": [],
            }
        ),
    )

    seen_queries: list[str] = []

    async def disagg_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "vllm:request_prompt_tokens_sum" in promql:
            return [{"values": [[1000, "12000"], [1005, "24000"]]}]
        if "vllm:request_prompt_tokens_count" in promql:
            return [{"values": [[1000, "10"], [1005, "20"]]}]
        if "vllm:request_prefill_kv_computed_tokens_sum" in promql:
            return [{"values": [[1000, "7200"], [1005, "14400"]]}]
        if "vllm:request_prefill_kv_computed_tokens_count" in promql:
            return [{"values": [[1000, "10"], [1005, "20"]]}]
        if "vllm:request_prompt_tokens_bucket" in promql:
            val = "1100" if "0.50" in promql else "2400"
            return [{"values": [[1000, val], [1005, val]]}]
        if "vllm:request_prefill_kv_computed_tokens_bucket" in promql:
            val = "650" if "0.50" in promql else "1500"
            return [{"values": [[1000, val], [1005, val]]}]
        if "sum by (pod, source) (increase(vllm:prompt_tokens_by_source_total" in promql:
            return [
                {"metric": {"pod": "vllm-prefill-0", "source": "local_cache_hit"}, "values": [[1005, "8000"]]},
                {"metric": {"pod": "vllm-prefill-0", "source": "external_kv_transfer"}, "values": [[1005, "1000"]]},
                {"metric": {"pod": "vllm-prefill-0", "source": "local_compute"}, "values": [[1005, "11000"]]},
                {"metric": {"pod": "vllm-decode-0", "source": "local_cache_hit"}, "values": [[1005, "4000"]]},
                {"metric": {"pod": "vllm-decode-0", "source": "local_compute"}, "values": [[1005, "3000"]]},
                # Client job pod & EPP pod must be strictly excluded
                {"metric": {"pod": "llm-d-lens-sim-job-1", "source": "local_compute"}, "values": [[1005, "99999"]]},
                {"metric": {"pod": "gateway-epp-0", "source": "local_cache_hit"}, "values": [[1005, "88888"]]},
            ]
        return []

    monkeypatch.setattr(service, "_query_range", disagg_range)
    res_disagg = await service.collect_benchmark_observability(
        "exec-tok-disagg",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert res_disagg["topology_mode"] == "disaggregated"
    ppr = res_disagg["derived"]["prompt_tokens_per_request"]
    assert ppr == {
        "mean_prompt_tokens": 1200.0,
        "mean_cached_tokens": 480.0,
        "mean_computed_tokens": 720.0,
        "cached_pct": 40.0,
        "prompt_p50": 1100.0,
        "prompt_p90": 2400.0,
        "computed_p50": 650.0,
        "computed_p90": 1500.0,
        "source": "vllm_histogram",
        "role_scope": "prefill",
    }
    prompt_token_queries_seen = [
        q for q in seen_queries if ("vllm:request_prompt_tokens" in q or "vllm:request_prefill_kv_computed_tokens" in q)
    ]
    assert len(prompt_token_queries_seen) == 8
    assert all('pod=~"vllm-prefill-0"' in q for q in prompt_token_queries_seen)
    assert res_disagg["derived"]["prompt_tokens_by_role_source"] == [
        {
            "role": "Prefill",
            "local_cache_tokens": 8000,
            "external_kv_tokens": 1000,
            "local_compute_tokens": 11000,
            "total_tokens": 20000,
        },
        {
            "role": "Decode",
            "local_cache_tokens": 4000,
            "external_kv_tokens": 0,
            "local_compute_tokens": 3000,
            "total_tokens": 7000,
        },
    ]
    assert res_disagg["availability"]["metrics"]["prompt_tokens_per_request"] == "available"
    assert res_disagg["availability"]["metrics"]["prompt_tokens_by_role_source"] == "available"
    assert "prompt_tokens_per_request" in res_disagg["metric_sources"]
    assert "prompt_tokens_by_role_source" in res_disagg["metric_sources"]
    assert any(
        'sum by (pod, source) (increase(vllm:prompt_tokens_by_source_total{namespace="bench-tok"}' in q
        for q in seen_queries
    )

    # 2. Aggregated mode aggregates workers under role="Worker" and still excludes client/EPP
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(
            return_value={"epp": ["gateway-epp-0"], "prefill": [], "decode": ["vllm-worker-0"], "model-server": []}
        ),
    )

    async def agg_range(_client, promql, _start, _end, _step):
        if "sum by (pod, source) (increase(vllm:prompt_tokens_by_source_total" in promql:
            return [
                {"metric": {"pod": "vllm-worker-0", "source": "local_cache_hit"}, "values": [[1005, "5000"]]},
                {"metric": {"pod": "vllm-worker-0", "source": "local_compute"}, "values": [[1005, "15000"]]},
                {"metric": {"pod": "trace-replayer-xyz", "source": "local_compute"}, "values": [[1005, "50000"]]},
            ]
        return []

    monkeypatch.setattr(service, "_query_range", agg_range)
    res_agg = await service.collect_benchmark_observability(
        "exec-tok-agg",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert res_agg["topology_mode"] == "aggregated"
    assert "prompt_tokens_per_request" not in res_agg["derived"]
    assert res_agg["derived"]["prompt_tokens_by_role_source"] == [
        {
            "role": "Worker",
            "local_cache_tokens": 5000,
            "external_kv_tokens": 0,
            "local_compute_tokens": 15000,
            "total_tokens": 20000,
        }
    ]
    assert res_agg["availability"]["metrics"]["prompt_tokens_per_request"] == "unavailable"
    assert res_agg["availability"]["metrics"]["prompt_tokens_by_role_source"] == "available"


@pytest.mark.asyncio
async def test_collect_namespace_observability_per_node_multi_node_join_and_fallbacks(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(
            return_value={
                "epp": ["ms-epp-0"],
                "prefill": ["vllm-prefill-0"],
                "decode": ["vllm-decode-0", "vllm-decode-1"],
                "model-server": [],
            }
        ),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "vllm-prefill-0", "labels": {"llm-d.ai/role": "prefill"}},
                    "spec": {
                        "nodeName": "node-a",
                        "containers": [{"name": "vllm", "resources": {"limits": {"nvidia.com/gpu": "2"}}}],
                    },
                },
                {
                    "metadata": {"name": "vllm-decode-0", "labels": {"llm-d.ai/role": "decode"}},
                    "spec": {
                        "nodeName": "node-b",
                        "containers": [{"name": "vllm", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
                    },
                },
                {
                    "metadata": {"name": "vllm-decode-1", "labels": {"llm-d.ai/role": "decode"}},
                    "spec": {
                        "nodeName": "node-b",
                        "containers": [{"name": "vllm", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
                    },
                },
                {
                    "metadata": {"name": "ms-epp-0", "labels": {"llm-d.ai/role": "epp"}},
                    "spec": {"nodeName": "epp-node", "containers": []},
                },
                {
                    "metadata": {"name": "llm-d-lens-sim-job-1"},
                    "spec": {"nodeName": "client-node", "containers": []},
                },
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        is_per_node = "by (nodename)" in promql

        # Global host queries
        if not is_per_node:
            if "node_cpu_seconds_total" in promql:
                return [{"values": [[1000, "40.0"], [1005, "60.0"]]}]
            if "node_memory_MemTotal_bytes" in promql:
                return [{"values": [[1000, "2000.0"], [1005, "2000.0"]]}]
            if "node_memory_MemAvailable_bytes" in promql:
                return [{"values": [[1000, "1200.0"], [1005, "800.0"]]}]
            if "node_filesystem_avail_bytes" in promql:
                return [{"values": [[1000, "5000.0"], [1005, "5000.0"]]}]
            if "node_filesystem_size_bytes" in promql:
                return [{"values": [[1000, "10000.0"], [1005, "10000.0"]]}]
            if "node_network_receive_bytes_total" in promql:
                return [{"values": [[1000, "1500.0"], [1005, "2500.0"]]}]
            if "node_network_transmit_bytes_total" in promql:
                return [{"values": [[1000, "800.0"], [1005, "1200.0"]]}]
            if "node_hwmon_power_average_watt" in promql:
                return [{"values": [[1000, "600.0"], [1005, "700.0"]]}]

        # Per-node queries (test nodename from node_uname_info and instance:port normalization)
        if is_per_node:
            if "node_cpu_seconds_total" in promql:
                return [
                    {"metric": {"nodename": "node-a"}, "values": [[1000, "105.0"], [1005, "50.0"]]},
                    {"metric": {"instance": "node-b:9100"}, "values": [[1000, "30.0"], [1005, "40.0"]]},
                ]
            if "node_memory_MemTotal_bytes" in promql:
                return [
                    {"metric": {"nodename": "node-a"}, "values": [[1000, "1000.0"], [1005, "1000.0"]]},
                    {"metric": {"node": "node-b"}, "values": [[1000, "1000.0"], [1005, "1000.0"]]},
                ]
            if "node_memory_MemAvailable_bytes" in promql:
                return [
                    {"metric": {"nodename": "node-a"}, "values": [[1000, "600.0"], [1005, "400.0"]]},
                    {"metric": {"node": "node-b"}, "values": [[1000, "600.0"], [1005, "400.0"]]},
                ]
            if "node_disk_read_bytes_total" in promql:
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "100.0"], [1005, "200.0"]]}]
            if "node_disk_written_bytes_total" in promql:
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "50.0"], [1005, "150.0"]]}]
            if "node_filesystem_avail_bytes" in promql:
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "2500.0"]]}]
            if "node_filesystem_size_bytes" in promql:
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "5000.0"]]}]
            if "node_network_receive_bytes_total" in promql:
                # Only node-a has host network; node-b falls back to pod container network
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "1000.0"], [1005, "2000.0"]]}]
            if "node_network_transmit_bytes_total" in promql:
                return [{"metric": {"nodename": "node-a"}, "values": [[1000, "500.0"], [1005, "700.0"]]}]
            if "node_hwmon_power_average_watt" in promql:
                return [
                    {"metric": {"nodename": "node-a"}, "values": [[1000, "320.0"], [1005, "360.0"]]},
                    {"metric": {"node": "node-b"}, "values": [[1000, "280.0"], [1005, "340.0"]]},
                ]

        # Pod container network fallback for node-b
        if "container_network_receive_bytes_total" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "300.0"], [1005, "400.0"]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, "200.0"], [1005, "300.0"]]},
                {"metric": {"pod": "ms-epp-0"}, "values": [[1000, "9999.0"]]},
            ]
        if "container_network_transmit_bytes_total" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "100.0"], [1005, "150.0"]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, "150.0"], [1005, "250.0"]]},
            ]

        # Direct NVIDIA DCGM pod metrics
        if "DCGM_FI_DEV_GPU_UTIL" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "vllm-prefill-0"}, "values": [[1000, "80.0"], [1005, "102.0"]]},
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "40.0"], [1005, "60.0"]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, "60.0"], [1005, "80.0"]]},
            ]
        if "DCGM_FI_DEV_POWER_USAGE" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "vllm-prefill-0"}, "values": [[1000, "300.0"], [1005, "350.0"]]},
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "150.0"], [1005, "200.0"]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, "170.0"], [1005, "220.0"]]},
            ]
        if "DCGM_FI_DEV_GPU_TEMP" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "vllm-prefill-0"}, "values": [[1000, "65.0"], [1005, "71.0"]]},
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "55.0"], [1005, "59.0"]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, "65.0"], [1005, "69.0"]]},
            ]
        if "DCGM_FI_DEV_FB_USED" in promql and "by (pod)" in promql:
            mib = 1024 * 1024
            return [
                {"metric": {"pod": "vllm-prefill-0"}, "values": [[1000, str(20 * mib)], [1005, str(20 * mib)]]},
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, str(10 * mib)], [1005, str(12 * mib)]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, str(14 * mib)], [1005, str(16 * mib)]]},
            ]
        if "DCGM_FI_DEV_FB_FREE" in promql and "by (pod)" in promql:
            mib = 1024 * 1024
            return [
                {"metric": {"pod": "vllm-prefill-0"}, "values": [[1000, str(60 * mib)], [1005, str(60 * mib)]]},
                {"metric": {"pod": "vllm-decode-0"}, "values": [[1000, str(30 * mib)], [1005, str(28 * mib)]]},
                {"metric": {"pod": "vllm-decode-1"}, "values": [[1000, str(26 * mib)], [1005, str(24 * mib)]]},
            ]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "vllm-decode-0"}, "values": [[1000, "5"], [1005, "5"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_namespace_observability(
        "c-multi",
        "ns-multi",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )

    assert result["nodes"] == ["node-a", "node-b"]
    # Global host derivations
    assert result["summary"]["host_filesystem_size_bytes"]["mean"] == 10000.0
    assert result["summary"]["host_network_receive_bytes_per_second"]["mean"] == 2000.0
    assert result["summary"]["host_network_transmit_bytes_per_second"]["mean"] == 1000.0
    assert result["summary"]["host_memory_used_bytes"]["mean"] == 1000.0
    assert result["summary"]["host_memory_usage_percent"]["mean"] == 50.0
    assert result["summary"]["host_memory_usage_percent"]["max"] == 60.0
    assert result["series"][0]["host_memory_used_bytes"] == 800.0
    assert result["series"][1]["host_memory_usage_percent"] == 60.0

    # Per-pod includes node for serving pods
    pod_map = {p["pod"]: p for p in result["per_pod"]}
    assert pod_map["vllm-prefill-0"]["node"] == "node-a"
    assert pod_map["vllm-decode-0"]["node"] == "node-b"
    assert pod_map["vllm-decode-1"]["node"] == "node-b"

    # Per-node checks
    assert [n["node"] for n in result["per_node"]] == ["node-a", "node-b"]
    node_a, node_b = result["per_node"]

    assert node_a["roles"] == ["prefill"]
    assert node_a["pods"] == ["vllm-prefill-0"]
    assert node_a["pod_count"] == 1
    assert node_a["allocated_accelerators"] == 2
    assert node_a["cpu_usage_percent"] == {"mean": 75.0, "max": 100.0}
    assert node_a["memory_used_bytes"] == {"mean": 500.0, "max": 600.0}
    assert node_a["memory_available_bytes"] == {"mean": 500.0, "min": 400.0}
    assert node_a["memory_total_bytes"] == 1000.0
    assert node_a["memory_usage_percent"] == {"mean": 50.0, "max": 60.0}
    assert node_a["filesystem_avail_bytes"] == 2500.0
    assert node_a["filesystem_size_bytes"] == 5000.0
    assert node_a["network_receive_bytes_per_second"] == {"mean": 1500.0, "max": 2000.0}
    assert node_a["network_transmit_bytes_per_second"] == {"mean": 600.0, "max": 700.0}
    assert node_a["host_power_watts"] == {"mean": 340.0, "max": 360.0}
    assert node_a["gpu_utilization_percent"] == {"mean": 90.0, "max": 100.0}
    assert node_a["gpu_power_watts"] == {"mean": 325.0, "max": 350.0}
    assert node_a["gpu_temperature_celsius"] == {"mean": 68.0, "max": 71.0}
    mib = 1024.0 * 1024.0
    assert node_a["gpu_memory_used_bytes"] == {"mean": 20.0 * mib, "max": 20.0 * mib}
    assert node_a["gpu_memory_total_bytes"] == pytest.approx(80.0 * mib)

    assert node_b["roles"] == ["decode"]
    assert node_b["pods"] == ["vllm-decode-0", "vllm-decode-1"]
    assert node_b["pod_count"] == 2
    assert node_b["allocated_accelerators"] == 2
    assert node_b["cpu_usage_percent"] == {"mean": 35.0, "max": 40.0}
    # Pod container network fallback summed across decode-0 + decode-1
    assert node_b["network_receive_bytes_per_second"] == {"mean": 600.0, "max": 700.0}
    assert node_b["network_transmit_bytes_per_second"] == {"mean": 325.0, "max": 400.0}
    # GPU power summed across pods: (150+170)=320, (200+220)=420
    assert node_b["gpu_power_watts"] == {"mean": 370.0, "max": 420.0}
    # GPU util averaged across pods per ts: (40+60)/2=50, (60+80)/2=70
    assert node_b["gpu_utilization_percent"] == {"mean": 60.0, "max": 70.0}
    assert node_b["gpu_temperature_celsius"] == {"mean": 62.0, "max": 69.0}
    assert node_b["gpu_memory_used_bytes"] == {"mean": 26.0 * mib, "max": 28.0 * mib}
    assert node_b["gpu_memory_total_bytes"] == pytest.approx(80.0 * mib)

    # Per-node series populated
    assert len(result["per_node_series"]["node-a"]) == 2
    assert result["per_node_series"]["node-a"][0]["cpu_usage_percent"] == 100.0
    assert result["per_node_series"]["node-b"][1]["network_receive_bytes_per_second"] == 700.0

    # PromQL verification
    per_node_qs = [q for q in seen_queries if "by (nodename)" in q]
    assert per_node_qs
    assert any(
        'max by (instance, nodename) (node_uname_info{nodename=~"(node-a|node-b)(\\\\..+)?"})' in q for q in per_node_qs
    )
    assert any('chip!~"(?:pci)?[0-9a-f]{4}:.*"' in q for q in per_node_qs)
    assert any(
        'device!~"lo|veth.*|cali.*|flannel.*|cilium.*|cni.*|docker.*|virbr.*|br-.*|kube-ipvs.*|tunl.*|vxlan.*"' in q
        for q in per_node_qs
    )


@pytest.mark.asyncio
async def test_collect_namespace_observability_global_network_fallback_and_series_downsampling(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["worker-0"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [{"metadata": {"name": "worker-0"}, "spec": {"nodeName": "single-node", "containers": []}}]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    long_vals_rx = [[1000 + i * 5, str(100 + i)] for i in range(300)]
    long_vals_tx = [[1000 + i * 5, str(50 + i)] for i in range(300)]

    async def fake_range(_client, promql, _start, _end, _step):
        # No node_network_* available -> both global & per-node fall back to pod container network
        if "container_network_receive_bytes_total" in promql:
            if "by (pod)" in promql:
                return [{"metric": {"pod": "worker-0"}, "values": long_vals_rx}]
            return [{"values": long_vals_rx}]
        if "container_network_transmit_bytes_total" in promql:
            if "by (pod)" in promql:
                return [{"metric": {"pod": "worker-0"}, "values": long_vals_tx}]
            return [{"values": long_vals_tx}]
        if "node_cpu_seconds_total" in promql and "by (nodename)" in promql:
            return [{"metric": {"nodename": "single-node"}, "values": [[1000 + i * 5, "25.0"] for i in range(300)]}]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "3.0"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_namespace_observability(
        "c-single",
        "ns-single",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:41:40Z",
    )

    assert result["topology_mode"] == "aggregated"
    assert result["summary"]["host_network_receive_bytes_per_second"]["mean"] == pytest.approx(249.5)
    assert result["summary"]["host_network_transmit_bytes_per_second"]["mean"] == pytest.approx(199.5)
    assert result["availability"]["metrics"]["host_network_receive_bytes_per_second"] == "available"
    assert result["per_node"][0]["roles"] == ["worker"]
    node_series = result["per_node_series"]["single-node"]
    assert len(node_series) == 240
    assert node_series[0]["timestamp"] == "1970-01-01T00:16:40Z"
    assert node_series[-1]["timestamp"] == datetime.fromtimestamp(1000 + 299 * 5, UTC).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.asyncio
async def test_collect_benchmark_observability_stage_histograms_use_1m_lookback(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["worker-0"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [{"metadata": {"name": "worker-0"}, "spec": {"nodeName": "gpu-node-1", "containers": []}}]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    await service.collect_namespace_observability(
        "c-stage",
        "ns-stage",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    stage_metrics = (
        "vllm:request_queue_time_seconds_bucket",
        "vllm:request_prefill_time_seconds_bucket",
        "vllm:request_decode_time_seconds_bucket",
    )
    for metric in stage_metrics:
        qs = [q for q in seen_queries if metric in q]
        assert len(qs) == 3, f"Expected P50, P90, and P99 queries for {metric}, got {len(qs)}"
        assert all("[1m]" in q for q in qs), f"Expected [1m] lookback on {metric}: {qs}"
        assert all("[30s]" not in q for q in qs), f"Unexpected [30s] lookback on {metric}: {qs}"


def test_cache_config_snapshot_extracts_multitier_byte_capacities_and_flags():
    gib = 1024**3
    snapshot = service._cache_config_snapshot(
        [
            {
                "metric": {
                    "pod": "vllm-0",
                    "engine": "0",
                    "kv_cache_memory_bytes": str(16 * gib),
                    "cpu_kvcache_space_bytes": str(4 * gib),
                    "cpu_offload_gb": "99",  # ignored when cpu_kvcache_space_bytes > 0
                    "disk_kvcache_space_bytes": str(10 * gib),
                },
                "values": [[1, "1"]],
            },
            {
                "metric": {
                    "pod": "vllm-1",
                    "engine": "0",
                    "kv_cache_memory_bytes": str(16 * gib),
                    "cpu_offload_gb": "2",
                    "nvme_kvcache_space_bytes": str(8 * gib),
                },
                "values": [[1, "1"]],
            },
            {
                "metric": {
                    "pod": "vllm-2",
                    "engine": "0",
                    "swap_space_bytes": str(1 * gib),
                    "local_disk_bytes": str(2 * gib),
                },
                "values": [[1, "1"]],
            },
        ]
    )

    assert snapshot["hbm_capacity_bytes"] == 32 * gib
    assert snapshot["cpu_capacity_bytes"] == 7 * gib
    assert snapshot["nvme_capacity_bytes"] == 20 * gib
    assert snapshot["disk_capacity_bytes"] == 20 * gib
    assert snapshot["host_kv_configured"] is True
    assert snapshot["nvme_kv_configured"] is True

    # Token-only CPU cache still sets host_kv_configured without nvme_kv_configured
    token_only = service._cache_config_snapshot(
        [{"metric": {"pod": "vllm-0", "engine": "0", "block_size": "16", "num_cpu_blocks": "10"}}]
    )
    assert token_only["host_kv_configured"] is True
    assert "nvme_kv_configured" not in token_only


def test_extract_cache_config_from_pods_parses_kv_transfer_config_and_lmcache_env():
    gib = 1024**3
    pods = [
        {
            "metadata": {"name": "vllm-decode-0"},
            "spec": {
                "containers": [
                    {
                        "command": ["/bin/sh", "-c"],
                        "args": [
                            "exec vllm serve model --kv-cache-metrics --kv-cache-metrics-sample 0.05 "
                            '--kv-transfer-config \'{"kv_connector": "MultiConnector", "kv_connector_extra_config": '
                            '{"connectors": [{"kv_connector": "NixlConnector"}, '
                            '{"kv_connector": "OffloadingConnector", "kv_connector_extra_config": '
                            '{"cpu_bytes_to_use": 4294967296, "nvme_bytes_to_use": 8589934592}}]}}\''
                        ],
                    }
                ]
            },
        },
        {
            "metadata": {"name": "vllm-decode-1"},
            "spec": {
                "containers": [
                    {
                        "args": [
                            '--kv-transfer-config={"kv_connector": "OffloadingConnector", '
                            '"kv_connector_extra_config": {"disk_bytes_to_use": 2147483648}}',
                            "--kv-transfer-config={not-valid-json",
                        ],
                        "env": [
                            {"name": "LMCACHE_MAX_LOCAL_CPU_SIZE", "value": "2.5"},
                            {"name": "LMCACHE_LOCAL_DISK", "value": "/nvme/lmcache"},
                            {"name": "LMCACHE_MAX_LOCAL_DISK_SIZE", "value": "4"},
                        ],
                    }
                ]
            },
        },
        {
            "metadata": {"name": "llm-d-lens-sim-runner"},
            "spec": {
                "containers": [
                    {
                        "env": [
                            {"name": "LMCACHE_MAX_LOCAL_CPU_SIZE", "value": "100"},
                            {"name": "LMCACHE_MAX_LOCAL_DISK_SIZE", "value": "100"},
                        ]
                    }
                ]
            },
        },
    ]

    extracted = service._extract_cache_config_from_pods(pods)
    assert extracted["kv_cache_metrics_enabled"] is True
    assert extracted["kv_cache_metrics_sample"] == 0.05
    assert extracted["cpu_capacity_bytes"] == 4294967296 + int(2.5 * gib)
    assert extracted["nvme_capacity_bytes"] == 8589934592 + 2147483648 + int(4 * gib)
    assert extracted["disk_capacity_bytes"] == extracted["nvme_capacity_bytes"]
    assert extracted["host_kv_configured"] is True
    assert extracted["nvme_kv_configured"] is True

    # LMCACHE_LOCAL_DISK alone marks nvme_kv_configured without nvme_capacity_bytes
    disk_flag_only = service._extract_cache_config_from_pods(
        [
            {
                "metadata": {"name": "vllm-worker-0"},
                "spec": {"containers": [{"env": [{"name": "LMCACHE_LOCAL_DISK", "value": "file:///nvme"}]}]},
            }
        ]
    )
    assert disk_flag_only["nvme_kv_configured"] is True
    assert "nvme_capacity_bytes" not in disk_flag_only
    assert disk_flag_only["kv_cache_metrics_enabled"] is False
    assert disk_flag_only["kv_cache_metrics_sample"] == 1.0


@pytest.mark.asyncio
async def test_collect_observability_multitier_queries_clamping_and_cache_config_merge(monkeypatch):
    assert service.clamp_bounded_metric("disk_cache_usage_percent", 108.5) == 100.0
    assert service.clamp_bounded_metric("disk_cache_usage_percent", -2.5) == 0.0

    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["worker-0"]}),
    )

    gib = 1024**3

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0"},
                    "spec": {
                        "nodeName": "gpu-node-1",
                        "containers": [
                            {
                                "args": [
                                    "--kv-transfer-config",
                                    '{"kv_connector": "LMCacheConnectorV1", "kv_connector_extra_config": {"cpu_offload": true}}',
                                ],
                                "env": [
                                    {"name": "LMCACHE_LOCAL_DISK", "value": "/nvme/cache"},
                                    {"name": "LMCACHE_MAX_LOCAL_DISK_SIZE", "value": "10"},
                                ],
                            }
                        ],
                    },
                }
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)

    async def fake_instant(_client, _promql):
        return []

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "vllm:cache_config_info" in promql:
            return [
                {
                    "metric": {
                        "pod": "worker-0",
                        "engine": "0",
                        "kv_cache_memory_bytes": str(24 * gib),
                        "cpu_kvcache_space_bytes": str(8 * gib),
                    },
                    "values": [[1000, "1"]],
                }
            ]
        if "lmcache:local_cpu_usage_perc" in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "32.0"], [1005, "48.0"]]}]
        if "lmcache:local_disk_usage_perc" in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "105.0"], [1005, "-5.0"]]}]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "5"], [1005, "5"]]}]
        return []

    monkeypatch.setattr(service, "_query", fake_instant)
    monkeypatch.setattr(service, "_query_range", fake_range)

    result = await service.collect_namespace_observability(
        "c-tier",
        "ns-tier",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert any(
        "lmcache:local_cpu_usage_perc" in q and "lmcache:local_cpu_cache_bytes" in q and "by (pod)" not in q
        for q in seen_queries
    )
    assert any(
        "lmcache:local_cpu_usage_perc" in q and "lmcache:local_cpu_max_bytes" in q and "by (pod)" in q
        for q in seen_queries
    )
    assert any(
        "vllm:disk_cache_usage_perc" in q
        and "vllm:kv_offload_disk_cache_usage_perc" in q
        and "lmcache:local_disk_usage_perc" in q
        and "lmcache:local_disk_cache_bytes" in q
        and "by (pod)" not in q
        for q in seen_queries
    )
    assert any(
        "vllm:disk_cache_usage_perc" in q and "lmcache:local_disk_max_bytes" in q and "by (pod)" in q
        for q in seen_queries
    )

    assert result["summary"]["cpu_cache_usage_percent"]["mean"] == 40.0
    assert result["summary"]["disk_cache_usage_percent"]["max"] == 100.0
    assert result["summary"]["disk_cache_usage_percent"]["mean"] == 50.0
    assert result["per_pod"][0]["disk_cache_usage_percent"]["max"] == 100.0

    cfg = result["cache_config"]
    assert cfg["hbm_capacity_bytes"] == 24 * gib
    assert cfg["cpu_capacity_bytes"] == 8 * gib
    assert cfg["nvme_capacity_bytes"] == 10 * gib
    assert cfg["disk_capacity_bytes"] == 10 * gib
    assert cfg["host_kv_configured"] is True
    assert cfg["nvme_kv_configured"] is True


@pytest.mark.asyncio
async def test_profile_driven_accelerator_model_extraction_dra_and_direct_and_node_fallbacks(monkeypatch):
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "bench-hw", "c-hw"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["worker-0"]}),
    )

    # 1. DRA profile (intel-xpu style): hw_gpu_info exposes hw_model with trademark markers, filtered to allocation
    async def fake_dra_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0", "uid": "pod-uid-0", "namespace": "bench-hw"},
                    "spec": {"nodeName": "xpu-node-1", "containers": []},
                }
            ]
        if kind == "resourceclaims.resource.k8s.io":
            return [
                {
                    "metadata": {"name": "claim-0", "namespace": "bench-hw"},
                    "status": {
                        "reservedFor": [{"resource": "pods", "name": "worker-0", "uid": "pod-uid-0"}],
                        "allocation": {
                            "allocationTimestamp": "1970-01-01T00:00:00Z",
                            "devices": {
                                "results": [
                                    {
                                        "driver": "gpu.intel.com",
                                        "pool": "xpu-node-1",
                                        "device": "0000-03-00-0-0x0000",
                                    }
                                ]
                            },
                        },
                    },
                }
            ]
        if kind == "resourceslices.resource.k8s.io":
            return [
                {
                    "spec": {
                        "driver": "gpu.intel.com",
                        "nodeName": "xpu-node-1",
                        "pool": {"name": "xpu-node-1"},
                        "devices": [
                            {
                                "name": "0000-03-00-0-0x0000",
                                "attributes": {"pciAddress": {"string": "0000:03:00.0"}},
                            }
                        ],
                    }
                }
            ]
        return []

    async def fake_dra_instant(_client, promql):
        if "hw_gpu_info" in promql:
            return [
                {
                    "metric": {
                        "node": "xpu-node-1",
                        "pci_bdf": "0000:03:00.0",
                        "hw_model": "Intel(R) Arc(TM) Pro B60 Graphics",
                    },
                    "value": [1000, "1"],
                },
                {
                    "metric": {
                        "node": "other-node",
                        "pci_bdf": "0000:99:00.0",
                        "hw_model": "Unrelated Other GPU",
                    },
                    "value": [1000, "1"],
                },
            ]
        return []

    async def fake_dra_range(_client, promql, _start, _end, _step):
        if "hw_gpu_utilization_ratio" in promql:
            return [
                {
                    "metric": {"node": "xpu-node-1", "pci_bdf": "0000:03:00.0"},
                    "values": [[1000, "0.5"]],
                }
            ]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "2"]]}]
        return []

    monkeypatch.setattr(service, "list_resources", fake_dra_resources)
    monkeypatch.setattr(service, "_query", fake_dra_instant)
    monkeypatch.setattr(service, "_query_range", fake_dra_range)

    dra_res = await service.collect_benchmark_observability(
        "exec-dra",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert dra_res["accelerator_summary"] == "Intel Arc Pro B60 Graphics"
    assert dra_res["hardware_profiles"][0]["models"] == ["Intel Arc Pro B60 Graphics"]
    assert dra_res["hardware_profiles"][0]["allocated_accelerators"] == 1

    # 2. Direct profile (nvidia style): raw device metric series exposes modelName, pod requests give count
    async def fake_direct_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0", "namespace": "bench-hw"},
                    "spec": {
                        "nodeName": "gpu-node-1",
                        "containers": [{"name": "modelserver", "resources": {"requests": {"nvidia.com/gpu": "2"}}}],
                    },
                }
            ]
        return []

    async def fake_direct_instant(_client, promql):
        if "DCGM_FI_DEV_GPU_UTIL" in promql:
            return [
                {
                    "metric": {
                        "namespace": "bench-hw",
                        "pod": "worker-0",
                        "Hostname": "gpu-node-1",
                        "modelName": "NVIDIA H100 80GB HBM3",
                    },
                    "value": [1000, "70"],
                }
            ]
        return []

    async def fake_direct_range(_client, promql, _start, _end, _step):
        if "DCGM_FI_DEV_GPU_UTIL" in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "70"]]}]
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {"pod": "worker-0"}, "values": [[1000, "2"]]}]
        return []

    monkeypatch.setattr(service, "list_resources", fake_direct_resources)
    monkeypatch.setattr(service, "_query", fake_direct_instant)
    monkeypatch.setattr(service, "_query_range", fake_direct_range)

    direct_res = await service.collect_benchmark_observability(
        "exec-nvidia",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert direct_res["accelerator_summary"] == "NVIDIA H100 80GB HBM3"
    assert direct_res["hardware_profiles"][0]["models"] == ["NVIDIA H100 80GB HBM3"]
    assert direct_res["hardware_profiles"][0]["allocated_accelerators"] == 2

    # 3. Node label fallback (K8s node labels via planning.model_label_keys and KSM model_label_patterns)
    async def fake_node_label_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0", "namespace": "bench-hw"},
                    "spec": {
                        "nodeName": "gpu-node-1",
                        "containers": [{"name": "modelserver", "resources": {"requests": {"nvidia.com/gpu": "4"}}}],
                    },
                }
            ]
        if kind == "nodes":
            return [
                {
                    "metadata": {
                        "name": "gpu-node-1",
                        "labels": {
                            "nvidia.com/gpu.product": "NVIDIA A100-SXM4-80GB",
                            "nvidia.com/gpu.family": "ampere",
                        },
                    }
                }
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_node_label_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    node_label_res = await service.collect_benchmark_observability(
        "exec-node-labels",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert node_label_res["accelerator_summary"] == "NVIDIA A100-SXM4-80GB"
    assert node_label_res["hardware_profiles"][0]["models"] == ["NVIDIA A100-SXM4-80GB"]
    assert node_label_res["hardware_profiles"][0]["allocated_accelerators"] == 4

    # 4. Fallback to profile display_name when no model label or node label is exposed
    monkeypatch.setattr(service, "list_resources", fake_direct_resources)
    display_fallback_res = await service.collect_benchmark_observability(
        "exec-fallback",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert display_fallback_res["accelerator_summary"] == "NVIDIA GPU"
    assert display_fallback_res["hardware_profiles"][0]["models"] == []

    # 5. Synthetic custom profile (proving zero vendor hardcoding)
    from llm_d_lens.hardware.models import HardwareProfile

    custom_profile = HardwareProfile.from_dict(
        {
            "id": "acme-npu",
            "display_name": "Acme NPU",
            "vendor": "acme",
            "accelerator_keys": ["acme"],
            "request_model": "extended-resource",
            "resource_prefixes": ["acme.example/npu"],
            "telemetry": {
                "provider_id": "acme_npu",
                "allocation_join": "extended-resource",
                "device_metrics": {"device_info": "acme_npu_info"},
                "device_metric_sources": {
                    "utilization": {"metric": "acme_npu_util", "unit": "percent", "output_unit": "percent"}
                },
                "label_schema": {"namespace": "ns", "pod": "pod", "model": "npu_card"},
            },
            "deployment": {
                "overlay_root": "guides/{guide}",
                "arch": "npu",
                "resource_name": "acme.example/npu",
                "modes": {"plugin": {"request_model": "extended-resource", "resource_names": ["acme.example/npu"]}},
            },
        }
    )
    monkeypatch.setattr(service, "_hardware_profiles", lambda: [custom_profile])

    async def fake_custom_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "worker-0", "namespace": "bench-hw"},
                    "spec": {
                        "nodeName": "npu-node-1",
                        "containers": [{"name": "modelserver", "resources": {"requests": {"acme.example/npu": "2"}}}],
                    },
                }
            ]
        return []

    async def fake_custom_instant(_client, promql):
        if "acme_npu_info" in promql:
            return [{"metric": {"ns": "bench-hw", "pod": "worker-0", "npu_card": "Acme(TM) X900"}, "value": [1, "1"]}]
        return []

    monkeypatch.setattr(service, "list_resources", fake_custom_resources)
    monkeypatch.setattr(service, "_query", fake_custom_instant)

    custom_res = await service.collect_benchmark_observability(
        "exec-custom",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert custom_res["accelerator_summary"] == "Acme X900"
    assert custom_res["hardware_profiles"][0]["models"] == ["Acme X900"]
    assert custom_res["hardware_profiles"][0]["allocated_accelerators"] == 2


@pytest.mark.asyncio
async def test_classify_window_pods_and_historical_pod_recovery(monkeypatch):
    # 1. Direct _classify_window_pods: preserves K8s llm-d.ai/role, excludes EPP/client, disjoint sets
    rows = [
        {"metric": {"pod": "model-a-0"}},  # unlabeled in Prom, K8s says prefill
        {"metric": {"pod": "model-b-0", "llm_d_ai_role": "decode"}},
        {"metric": {"pod": "model-a-0", "role": "decode"}},  # prefill wins -> disjoint
        {"metric": {"pod": "gaie-epp-0", "component": "epp"}},
        {"metric": {"pod": "llm-d-lens-sim-abc"}},
        {"metric": {"pod": "worker-custom", "role": "epp"}},
    ]
    p_pods, d_pods = service._classify_window_pods(rows, k8s_roles={"model-a-0": "prefill"})
    assert p_pods == ["model-a-0"]
    assert d_pods == ["model-b-0"]
    assert set(p_pods).isdisjoint(set(d_pods))

    # 2. Historical pod recovery when K8s list_resources returns empty and _discover_components is empty
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": []}),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "count by (pod, llm_d_ai_role, role, component) (vllm:num_requests_running" in promql:
            return []  # force fallback to request_success_total
        if "count by (pod, llm_d_ai_role, role, component) (vllm:request_success_total" in promql:
            return [
                {"metric": {"pod": "hist-p-0", "llm_d_ai_role": "prefill"}, "values": [[1000, "1"]]},
                {"metric": {"pod": "hist-d-0", "llm_d_ai_role": "decode"}, "values": [[1000, "1"]]},
                {"metric": {"pod": "hist-epp-0", "component": "epp"}, "values": [[1000, "1"]]},
            ]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    res = await service.collect_namespace_observability(
        "c-hist",
        "ns-hist",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert res["topology_mode"] == "disaggregated"
    assert any('pod=~"hist-p-0"' in q and "vllm:request_prefill_time_seconds" in q for q in seen_queries)
    assert any('pod=~"hist-d-0"' in q and "vllm:request_decode_time_seconds" in q for q in seen_queries)


@pytest.mark.asyncio
async def test_hist_mean_ms_and_exact_mean_queries(monkeypatch):
    q_expr = service._hist_mean_ms("vllm:request_queue_time_seconds", '{namespace="ns-m"}', "1m")
    assert "vllm:request_queue_time_seconds_sum" in q_expr
    assert "vllm:request_queue_time_seconds_count" in q_expr
    assert "* 1000" in q_expr
    assert "> 0)" in q_expr

    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": [], "decode": ["worker-0"]}),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "vllm:time_to_first_token_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "120.0"]]}]
        if "vllm:e2e_request_latency_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "950.0"]]}]
        if "vllm:time_per_output_token_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "22.5"]]}]
        if "vllm:request_queue_time_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "14.0"]]}]
        if "vllm:request_prefill_time_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "90.0"]]}]
        if "vllm:request_decode_time_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "830.0"]]}]
        if "vllm:nixl_xfer_time_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "110.0"]]}]
        if "histogram_quantile(0.90" in promql and "vllm:nixl_xfer_time_seconds_bucket" in promql:
            return [{"metric": {}, "values": [[1000, "180.0"]]}]
        if "llm_d_epp_scheduler_e2e_duration_seconds_sum" in promql:
            return [{"metric": {}, "values": [[1000, "1.8"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    res = await service.collect_namespace_observability(
        "c-mean",
        "ns-mean",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    summary = res["summary"]
    assert summary["vllm_ttft_mean_ms"]["mean"] == pytest.approx(120.0)
    assert summary["vllm_e2e_mean_ms"]["mean"] == pytest.approx(950.0)
    assert summary["vllm_tpot_mean_ms"]["mean"] == pytest.approx(22.5)
    assert summary["vllm_queue_time_mean_ms"]["mean"] == pytest.approx(14.0)
    assert summary["vllm_prefill_time_mean_ms"]["mean"] == pytest.approx(90.0)
    assert summary["vllm_decode_time_mean_ms"]["mean"] == pytest.approx(830.0)
    assert summary["nixl_transfer_latency_mean_ms"]["mean"] == pytest.approx(110.0)
    assert summary["nixl_transfer_latency_p90_ms"]["mean"] == pytest.approx(180.0)
    assert res["router"]["scheduler_latency_mean_ms"]["mean"] == pytest.approx(1.8)
    assert res["metric_sources"]["vllm_stage_latencies"] == "aggregated_namespace"
    assert any("vllm:nixl_xfer_time_seconds_count" in q and "> 0)" in q for q in seen_queries)
    assert any("llm_d_epp_scheduler_e2e_duration_seconds_count" in q and "> 0)" in q for q in seen_queries)


@pytest.mark.asyncio
async def test_role_separated_pd_observability_series_summary_and_undiluted_canonical(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": ["p-0"], "decode": ["d-0"]}),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    async def fake_range(_client, promql, _start, _end, _step):
        is_p = 'pod=~"p-0"' in promql
        is_d = 'pod=~"d-0"' in promql

        if "llm_d_epp_pd_decision_total" in promql:
            # t=1000: pd=8, r=10 -> alpha=0.8; t=1005: no router_rps -> alpha=1.0
            return [{"metric": {}, "values": [[1000, "8.0"], [1005, "5.0"]]}]
        if "llm_d_epp_request_total" in promql:
            return [{"metric": {}, "values": [[1000, "10.0"]]}]
        if "vllm:num_requests_waiting" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "p-0"}, "values": [[1000, "0"], [1005, "0"]]},
                {"metric": {"pod": "d-0"}, "values": [[1000, "4"], [1005, "2"]]},
            ]
        if "vllm:num_requests_running" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "p-0"}, "values": [[1000, "2"], [1005, "1"]]},
                {"metric": {"pod": "d-0"}, "values": [[1000, "10"], [1005, "8"]]},
            ]

        if is_p:
            if "vllm:request_queue_time_seconds_bucket" in promql:
                val = "150.0" if "0.50" in promql else "270.0"
                return [{"metric": {}, "values": [[1000, val], [1005, val]]}]
            if "vllm:request_queue_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "2.0"], [1005, "2.0"]]}]
            if "vllm:request_prefill_time_seconds_bucket" in promql:
                val = "300.0" if "0.50" in promql else "500.0"
                return [{"metric": {}, "values": [[1000, val], [1005, val]]}]
            if "vllm:request_prefill_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "320.0"], [1005, "320.0"]]}]
            if "vllm:request_decode_time_seconds_bucket" in promql:
                return [{"metric": {}, "values": [[1000, "1.0"], [1005, "1.0"]]}]
            if "vllm:time_to_first_token_seconds_bucket" in promql and "0.50" in promql:
                return [{"metric": {}, "values": [[1000, "400.0"], [1005, "400.0"]]}]
            if "vllm:e2e_request_latency_seconds_bucket" in promql and "0.50" in promql:
                return [{"metric": {}, "values": [[1000, "410.0"], [1005, "410.0"]]}]
        elif is_d:
            if "vllm:request_queue_time_seconds_bucket" in promql:
                val = "300.0" if "0.50" in promql else "600.0"
                return [{"metric": {}, "values": [[1000, val], [1005, val]]}]
            if "vllm:request_queue_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "40.0"], [1005, "40.0"]]}]
            if "vllm:request_prefill_time_seconds_bucket" in promql:
                return [{"metric": {}, "values": [[1000, "2.0"], [1005, "2.0"]]}]
            if "vllm:request_decode_time_seconds_bucket" in promql:
                val = "9000.0" if "0.50" in promql else "14000.0"
                return [{"metric": {}, "values": [[1000, val], [1005, val]]}]
            if "vllm:request_decode_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "9500.0"], [1005, "9500.0"]]}]
            if "vllm:time_per_output_token_seconds_bucket" in promql and "0.50" in promql:
                return [{"metric": {}, "values": [[1000, "25.0"], [1005, "25.0"]]}]
            if "vllm:time_to_first_token_seconds_bucket" in promql and "0.50" in promql:
                return [{"metric": {}, "values": [[1000, "500.0"], [1005, "500.0"]]}]
            if "vllm:e2e_request_latency_seconds_bucket" in promql and "0.50" in promql:
                return [{"metric": {}, "values": [[1000, "9500.0"], [1005, "9500.0"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    res = await service.collect_namespace_observability(
        "c-pd",
        "ns-pd",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert res["metric_sources"]["vllm_stage_latencies"] == "role_separated"

    # Raw quantiles preserved in role_series and summary_by_role (no floor clamping there)
    assert res["role_series"][0]["prefill_vllm_queue_time_p50_ms"] == pytest.approx(150.0)
    assert res["role_series"][0]["prefill_vllm_queue_time_p90_ms"] == pytest.approx(270.0)
    assert res["role_series"][0]["decode_vllm_decode_time_p50_ms"] == pytest.approx(9000.0)

    sbr = res["summary_by_role"]
    assert sbr["prefill"]["vllm_queue_time_p50_ms"]["mean"] == pytest.approx(150.0)
    assert sbr["prefill"]["vllm_queue_time_p90_ms"]["mean"] == pytest.approx(270.0)
    assert sbr["prefill"]["waiting_requests"]["mean"] == pytest.approx(0.0)
    assert sbr["prefill"]["running_requests"]["mean"] == pytest.approx(1.5)
    assert sbr["decode"]["waiting_requests"]["mean"] == pytest.approx(3.0)
    assert sbr["decode"]["waiting_requests"]["max"] == pytest.approx(4.0)
    assert sbr["decode"]["running_requests"]["mean"] == pytest.approx(9.0)
    assert sbr["decode"]["vllm_tpot_p50_ms"]["mean"] == pytest.approx(25.0)
    assert "vllm_tpot_p50_ms" not in sbr["prefill"]

    # derived.role_summary includes role latency means
    assert res["derived"]["role_summary"]["prefill"]["vllm_prefill_time_p50_ms"] == pytest.approx(300.0)
    assert res["derived"]["role_summary"]["decode"]["vllm_decode_time_p50_ms"] == pytest.approx(9000.0)

    # Canonical series: un-diluted prefill/decode/tpot + alpha(t)-weighted TTFT/E2E/queue with _effective_role_queue
    pt0, pt1 = res["series"][0], res["series"][1]
    assert pt0["vllm_prefill_time_p50_ms"] == pytest.approx(300.0)
    assert pt0["vllm_decode_time_p50_ms"] == pytest.approx(9000.0)
    assert pt0["vllm_tpot_p50_ms"] == pytest.approx(25.0)
    # t=1000: alpha = 8/10 = 0.8
    assert pt0["vllm_ttft_p50_ms"] == pytest.approx(0.8 * 400.0 + 500.0)
    assert pt0["vllm_e2e_p50_ms"] == pytest.approx(0.8 * 410.0 + 9500.0)
    # prefill queue mean=2.0 < 25 and p50=150 <= 160, p90/p99=270 <= 305 -> _effective_role_queue = 2.0
    assert pt0["vllm_queue_time_p50_ms"] == pytest.approx(0.8 * 2.0 + 300.0)
    assert pt0["vllm_queue_time_p90_ms"] == pytest.approx(0.8 * 2.0 + 600.0)
    assert pt0["vllm_queue_time_p99_ms"] == pytest.approx(0.8 * 2.0 + 600.0)
    # t=1005: no router_rps -> alpha = 1.0
    assert pt1["vllm_ttft_p50_ms"] == pytest.approx(400.0 + 500.0)
    assert pt1["vllm_queue_time_p50_ms"] == pytest.approx(2.0 + 300.0)


@pytest.mark.asyncio
async def test_pd_canonical_role_separated_metrics_kv_max_hbm_capacity_and_endpoint_cv(monkeypatch):
    """Verify A2, A3, B1, B2, B3, and B4 for 3P+1D disaggregated serving."""
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "ns-pd-canon", "c-pd-canon"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(
            return_value={
                "epp": ["epp-0"],
                "prefill": ["p-0", "p-1", "p-2"],
                "decode": ["d-0"],
            }
        ),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    async def fake_range(_client, promql, _start, _end, _step):
        is_p = 'pod=~"p-0|p-1|p-2"' in promql
        is_d = 'pod=~"d-0"' in promql

        if "vllm:cache_config_info" in promql:
            return [
                {
                    "metric": {"pod": "p-0", "kv_cache_size_tokens": "173376", "block_size": "16"},
                    "values": [[1000, "1"]],
                },
                {
                    "metric": {"pod": "p-1", "kv_cache_size_tokens": "173376", "block_size": "16"},
                    "values": [[1000, "1"]],
                },
                {
                    "metric": {"pod": "p-2", "kv_cache_size_tokens": "173376", "block_size": "16"},
                    "values": [[1000, "1"]],
                },
                {
                    "metric": {"pod": "d-0", "kv_cache_size_tokens": "173376", "block_size": "16"},
                    "values": [[1000, "1"]],
                },
            ]
        if "by (pod)" in promql:
            if "vllm:request_success_total" in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "0.3"], [1005, "0.3"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "0.25"], [1005, "0.25"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "0.25"], [1005, "0.25"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "0.75"], [1005, "0.75"]]},
                ]
            if "vllm:prompt_tokens_total" in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "2000"], [1005, "2000"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "1800"], [1005, "1800"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "1712.5"], [1005, "1712.5"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "5426"], [1005, "5426"]]},
                ]
            if "vllm:generation_tokens_total" in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "0.3"], [1005, "0.3"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "0.25"], [1005, "0.25"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "0.25"], [1005, "0.25"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "374.3"], [1005, "374.3"]]},
                ]
            if "vllm:kv_cache_usage_perc" in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "2.0"], [1005, "3.0"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "1.5"], [1005, "2.0"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "1.5"], [1005, "2.5"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "46.0"], [1005, "97.5"]]},
                ]
            if 'source="local_cache_hit"' in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "1000"], [1005, "1000"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "800"], [1005, "800"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "750.8"], [1005, "750.8"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "2750"], [1005, "2750"]]},
                ]
            if 'source="local_compute"' in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "1000"], [1005, "1000"]]},
                    {"metric": {"pod": "p-1"}, "values": [[1000, "1000"], [1005, "1000"]]},
                    {"metric": {"pod": "p-2"}, "values": [[1000, "964.4"], [1005, "964.4"]]},
                ]
            if 'source="external_kv_transfer"' in promql:
                return [
                    {"metric": {"pod": "d-0"}, "values": [[1000, "2676"], [1005, "2676"]]},
                ]
            if "vllm:prompt_tokens_recomputed_total" in promql:
                return [
                    {"metric": {"pod": "p-0"}, "values": [[1000, "40"], [1005, "40"]]},
                    {"metric": {"pod": "d-0"}, "values": [[1000, "999"], [1005, "999"]]},
                ]
        if is_p:
            if "vllm:prefix_cache_hits_total" in promql:
                return [{"metric": {}, "values": [[1000, "46.18"], [1005, "46.18"]]}]
            if "vllm:external_prefix_cache_hits_total" in promql:
                return [{"metric": {}, "values": [[1000, "0.0"], [1005, "0.0"]]}]
        if is_d:
            if "vllm:prefix_cache_hits_total" in promql:
                return [{"metric": {}, "values": [[1000, "50.5"], [1005, "50.5"]]}]
            if "vllm:external_prefix_cache_hits_total" in promql:
                return [{"metric": {}, "values": [[1000, "49.5"], [1005, "49.5"]]}]
        if "llm_d_epp_inflight_tokens" in promql:
            return [
                {"metric": {"endpoint_name": "p-0-rank-0"}, "values": [[1000, "4211.2"]]},
                {"metric": {"endpoint_name": "p-1-rank-0"}, "values": [[1000, "3127.17"]]},
                {"metric": {"endpoint_name": "p-2-rank-0"}, "values": [[1000, "8284.69"]]},
                {"metric": {"endpoint_name": "d-0-rank-0"}, "values": [[1000, "10000.0"]]},
                {"metric": {"endpoint_name": "d-0-rank-1"}, "values": [[1000, "8886.4"]]},
            ]
        # Corrupted pooled namespace-wide fallbacks that MUST be overridden
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [{"metric": {}, "values": [[1000, "1.55"], [1005, "1.55"]]}]
        if "vllm:kv_cache_usage_perc" in promql:
            return [{"metric": {}, "values": [[1000, "12.75"], [1005, "28.2"]]}]
        if "vllm:prompt_tokens_total" in promql:
            return [{"metric": {}, "values": [[1000, "10938.5"], [1005, "10938.5"]]}]
        if "vllm:generation_tokens_total" in promql:
            return [{"metric": {}, "values": [[1000, "375.1"], [1005, "375.1"]]}]
        if 'source="local_cache_hit"' in promql:
            return [{"metric": {}, "values": [[1000, "5300.8"], [1005, "5300.8"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)

    res = await service.collect_benchmark_observability(
        "exec-pd-canon",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    # B1: Canonical top-level request & token rates
    assert res["metric_sources"]["pd_canonical_metrics"] == "role_separated"
    assert res["summary"]["request_rate_rps"]["mean"] == pytest.approx(0.75)
    assert res["summary"]["input_token_rate_tps"]["mean"] == pytest.approx(5512.5)
    assert res["summary"]["output_token_rate_tps"]["mean"] == pytest.approx(374.3)

    # B2: Max KV across serving pods (97.5% Decode not masked as 28.2%) + per-role HBM capacity
    assert res["summary"]["kv_cache_usage_percent"]["max"] == pytest.approx(97.5)
    assert res["summary"]["kv_cache_usage_percent"]["mean"] == pytest.approx((46.0 + 97.5) / 2.0)
    assert res["cache_config"]["prefill_hbm_capacity_tokens"] == 520128
    assert res["cache_config"]["decode_hbm_capacity_tokens"] == 173376
    assert res["derived"]["prefill_hbm_capacity_tokens"] == 520128
    assert res["derived"]["decode_hbm_capacity_tokens"] == 173376

    # A3: Prefill-scoped prompt sources & hit rate; Decode-scoped external KV
    assert res["summary"]["engine_prompt_local_cache_tps"]["mean"] == pytest.approx(2550.8)
    assert res["summary"]["engine_prompt_local_compute_tps"]["mean"] == pytest.approx(2964.4)
    assert res["summary"]["engine_prompt_recomputed_tps"]["mean"] == pytest.approx(40.0)
    assert res["summary"]["engine_prompt_external_tps"]["mean"] == pytest.approx(2676.0)
    assert res["summary"]["prefix_cache_hit_percent"]["mean"] == pytest.approx(46.18)
    assert res["summary"]["external_prefix_cache_hit_percent"]["mean"] == pytest.approx(49.5)

    # role_series & summary_by_role contain B1, B2, A3 per-role stats
    assert res["role_series"][0]["prefill_kv_cache_usage_max_percent"] == pytest.approx(2.0)
    assert res["role_series"][1]["decode_kv_cache_usage_max_percent"] == pytest.approx(97.5)
    sbr = res["summary_by_role"]
    assert sbr["prefill"]["input_token_rate_tps"]["mean"] == pytest.approx(5512.5)
    assert sbr["prefill"]["prefix_cache_hit_percent"]["mean"] == pytest.approx(46.18)
    assert sbr["prefill"]["engine_prompt_local_cache_tps"]["mean"] == pytest.approx(2550.8)
    assert sbr["decode"]["output_token_rate_tps"]["mean"] == pytest.approx(374.3)
    assert sbr["decode"]["kv_cache_usage_max_percent"]["max"] == pytest.approx(97.5)
    assert sbr["decode"]["engine_prompt_external_tps"]["mean"] == pytest.approx(2676.0)

    # B3: Per-role 100% normalized endpoint distributions with -rank-N inflight_token_load joined
    p_dist = res["derived"]["prefill_endpoint_distribution"]
    d_dist = res["derived"]["decode_endpoint_distribution"]
    assert sum(row["percent"] for row in p_dist) == pytest.approx(100.0)
    assert sum(row["percent"] for row in d_dist) == pytest.approx(100.0)
    assert d_dist == [
        {
            "pod": "d-0",
            "role": "decode",
            "request_rate_rps": 0.75,
            "percent": 100.0,
            "inflight_token_load": 18886.4,
        }
    ]
    p_by_pod = {row["pod"]: row for row in p_dist}
    assert p_by_pod["p-0"]["inflight_token_load"] == pytest.approx(4211.2)
    assert p_by_pod["p-0"]["percent"] == pytest.approx(37.5)

    # B4: Intra-role token_load_cv (3 Prefill pods -> 0.42629, 1 Decode pod -> ignored)
    assert res["derived"]["token_load_cv"] == pytest.approx(0.42629, abs=1e-4)


@pytest.mark.asyncio
async def test_pd_single_replica_per_role_token_load_cv_is_zero(monkeypatch):
    """Verify B4: 1P+1D disaggregated run yields token_load_cv == 0.0 (not cross-role skew)."""
    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "ns-1p1d", "c-1p1d"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": ["p-0"], "decode": ["d-0"]}),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    async def fake_range(_client, promql, _start, _end, _step):
        if "llm_d_epp_inflight_tokens" in promql:
            return [
                {"metric": {"endpoint_name": "p-0-rank-0"}, "values": [[1000, "100.0"]]},
                {"metric": {"endpoint_name": "d-0-rank-0"}, "values": [[1000, "18000.0"]]},
            ]
        if "vllm:request_success_total" in promql and "by (pod)" in promql:
            return [
                {"metric": {"pod": "p-0"}, "values": [[1000, "1.0"]]},
                {"metric": {"pod": "d-0"}, "values": [[1000, "1.0"]]},
            ]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)
    res = await service.collect_benchmark_observability(
        "exec-1p1d",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )
    assert res["derived"]["token_load_cv"] == 0.0


@pytest.mark.asyncio
async def test_p99_stage_queries_router_p99_and_effective_role_queue_cap(monkeypatch):
    """Verify native P99 stage/router queries, _effective_role_queue 305ms P99/max cap, and no 0.01/1.0 quantiles."""
    assert service._effective_role_queue(297.0, 1.5, "p99") == pytest.approx(1.5)
    assert service._effective_role_queue(297.0, 1.5, "max") == pytest.approx(1.5)
    assert service._effective_role_queue(310.0, 1.5, "p99") == pytest.approx(310.0)
    assert service._effective_role_queue(297.0, 30.0, "p99") == pytest.approx(297.0)

    monkeypatch.setattr(service, "_deployment_target", lambda *_: (object(), "ns-p99", "c-p99"))
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["epp-0"], "prefill": ["p-0"], "decode": ["d-0"]}),
    )
    monkeypatch.setattr(service, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        seen_queries.append(promql)
        if "llm_d_epp_scheduler_e2e_duration_seconds_bucket" in promql and "0.99" in promql:
            return [{"metric": {}, "values": [[1000, "2.5"], [1005, "5.0"]]}]
        if 'pod=~"p-0"' in promql:
            if "vllm:request_queue_time_seconds_bucket" in promql and "0.99" in promql:
                return [{"metric": {}, "values": [[1000, "297.0"], [1005, "297.0"]]}]
            if "vllm:request_queue_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "1.5"], [1005, "1.5"]]}]
            if "vllm:request_prefill_time_seconds_bucket" in promql and "0.99" in promql:
                return [{"metric": {}, "values": [[1000, "800.0"], [1005, "900.0"]]}]
        if 'pod=~"d-0"' in promql:
            if "vllm:request_queue_time_seconds_bucket" in promql and "0.99" in promql:
                return [{"metric": {}, "values": [[1000, "450.0"], [1005, "550.0"]]}]
            if "vllm:request_queue_time_seconds_sum" in promql:
                return [{"metric": {}, "values": [[1000, "60.0"], [1005, "80.0"]]}]
            if "vllm:request_decode_time_seconds_bucket" in promql and "0.99" in promql:
                return [{"metric": {}, "values": [[1000, "12000.0"], [1005, "13000.0"]]}]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)
    res = await service.collect_benchmark_observability(
        "exec-p99",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:40Z",
    )

    assert not any("histogram_quantile(0.01" in q or "histogram_quantile(1.0" in q for q in seen_queries)
    assert res["router"]["scheduler_latency_p99_ms"]["min"] == pytest.approx(2.5)
    assert res["router"]["scheduler_latency_p99_ms"]["max"] == pytest.approx(5.0)
    assert res["summary"]["vllm_prefill_time_p99_ms"]["min"] == pytest.approx(800.0)
    assert res["summary"]["vllm_prefill_time_p99_ms"]["max"] == pytest.approx(900.0)
    assert res["summary"]["vllm_decode_time_p99_ms"]["p99"] == pytest.approx(13000.0)
    assert res["summary_by_role"]["prefill"]["vllm_queue_time_p99_ms"]["mean"] == pytest.approx(297.0)
    assert res["series"][0]["vllm_queue_time_p99_ms"] == pytest.approx(451.5)
    assert res["series"][1]["vllm_queue_time_p99_ms"] == pytest.approx(551.5)


@pytest.mark.asyncio
async def test_node_alias_resolution_fqdn_ip_ipv6_ambiguous_and_dedup(monkeypatch):
    # 1. Alias resolution: FQDN<->shortname both directions, ambiguous shortname guard, IPv4/IPv6, priority dedup
    node_names = ["smc-18", "fqdn-node.lab.local", "worker-1.zone-a", "worker-1.zone-b"]
    pods = [
        {"metadata": {"name": "p0"}, "spec": {"nodeName": "smc-18"}, "status": {"hostIP": "10.0.1.18"}},
        {
            "metadata": {"name": "p1"},
            "spec": {"nodeName": "fqdn-node.lab.local"},
            "status": {"hostIPs": [{"ip": "fd00::19"}]},
        },
    ]
    nodes = [
        {
            "metadata": {"name": "worker-1.zone-a"},
            "status": {
                "addresses": [
                    {"type": "InternalIP", "address": "10.0.1.21"},
                    {"type": "Hostname", "address": "w1a-alias.lab.local"},
                ]
            },
        },
        {
            "metadata": {"name": "worker-1.zone-b"},
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.1.22"}]},
        },
    ]
    aliases = service._build_node_aliases(node_names, pods, nodes)
    assert service._resolve_canonical_node("smc-18.lab.local", aliases) == ("smc-18", 3)
    assert service._resolve_canonical_node("fqdn-node:9100", aliases) == ("fqdn-node.lab.local", 3)
    assert service._resolve_canonical_node("[fd00::19]:9100", aliases) == ("fqdn-node.lab.local", 4)
    assert service._resolve_canonical_node("fd00::19", aliases) == ("fqdn-node.lab.local", 4)
    assert service._resolve_canonical_node("10.0.1.18:9100", aliases) == ("smc-18", 4)
    assert service._resolve_canonical_node("w1a-alias:9100", aliases) == ("worker-1.zone-a", 3)
    # Ambiguous shortname "worker-1" must NOT resolve to either zone-a or zone-b
    assert service._resolve_canonical_node("worker-1", aliases) == (None, 99)
    assert service._resolve_canonical_node("10.0.1.22:9100", aliases) == ("worker-1.zone-b", 4)

    # Priority-based deduplication avoids double-counting node="smc-18" and instance="10.0.1.18:9100"
    deduped = service._normalize_node_series(
        {
            "smc-18": {1000.0: 55.0},
            "10.0.1.18:9100": {1000.0: 55.0, 1005.0: 65.0},
        },
        node_names,
        aliases=aliases,
    )
    assert deduped["smc-18"] == {1000.0: 55.0, 1005.0: 65.0}

    # PromQL IPv6-safe label_replace wrapper
    promql_ip = service._extract_instance_host_promql("sum by (instance) (up)", "internal_ip")
    assert '"^(.*)$"' in promql_ip
    assert '"^([^:\\\\[]+)(?::[0-9]+)?$"' in promql_ip
    assert '"^\\\\[([^\\\\]]+)\\\\](?::[0-9]+)?$"' in promql_ip

    # 2. End-to-end: partial IP (1 of 2 nodes) skips partial IP in host_queries and falls back to per_node_vectors
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["w-0", "w-1"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "w-0"},
                    "spec": {"nodeName": "smc-18", "containers": []},
                    "status": {"hostIP": "10.0.1.18"},
                },
                {
                    "metadata": {"name": "w-1"},
                    "spec": {"nodeName": "smc-19", "containers": []},
                    "status": {},
                },
            ]
        if kind == "nodes":
            return [
                {
                    "metadata": {"name": "smc-19"},
                    "status": {"addresses": [{"type": "Hostname", "address": "smc-19.lab.local"}]},
                }
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    seen_host_queries: list[str] = []
    seen_per_node_queries: list[str] = []

    async def fake_range(_client, promql, _start, _end, _step):
        is_per_node = "by (nodename)" in promql or "by (nodename, device)" in promql
        if not is_per_node:
            if "node_" in promql or "ipmi_" in promql or "kepler_" in promql:
                seen_host_queries.append(promql)
            # Simulate host_queries returning empty (e.g., partial-IP host_inst_sel guard prevented partial sum)
            return []
        seen_per_node_queries.append(promql)
        if "node_cpu_seconds_total" in promql:
            return [
                {"metric": {"nodename": "smc-18"}, "values": [[1000, "40.0"]]},
                {"metric": {"instance": "10.0.1.18:9100"}, "values": [[1000, "40.0"]]},
                {"metric": {"nodename": "smc-19.lab.local"}, "values": [[1000, "80.0"]]},
            ]
        if "node_memory_MemTotal_bytes" in promql:
            return [
                {"metric": {"instance": "10.0.1.18:9100"}, "values": [[1000, "1000.0"]]},
                {"metric": {"nodename": "smc-19.lab.local"}, "values": [[1000, "2000.0"]]},
            ]
        if "node_memory_MemAvailable_bytes" in promql:
            return [
                {"metric": {"instance": "10.0.1.18:9100"}, "values": [[1000, "400.0"]]},
                {"metric": {"nodename": "smc-19.lab.local"}, "values": [[1000, "1100.0"]]},
            ]
        if "node_disk_read_bytes_total" in promql:
            return [
                {"metric": {"nodename": "smc-18"}, "values": [[1000, "100.0"]]},
                {"metric": {"nodename": "smc-19"}, "values": [[1000, "200.0"]]},
            ]
        if "node_network_receive_bytes_total" in promql and "by (nodename)" in promql:
            return [
                {"metric": {"instance": "10.0.1.18:9100"}, "values": [[1000, "500.0"]]},
                {"metric": {"nodename": "smc-19"}, "values": [[1000, "700.0"]]},
            ]
        if "node_hwmon_power_average_watt" in promql:
            return [
                {"metric": {"nodename": "smc-18"}, "values": [[1000, "300.0"]]},
                {"metric": {"nodename": "smc-19"}, "values": [[1000, "450.0"]]},
            ]
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)
    res = await service.collect_namespace_observability(
        "c-alias",
        "ns-alias",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )

    assert seen_host_queries
    assert all(r"10\\.0\\.1\\.18" not in q for q in seen_host_queries)
    assert any(r"10\\.0\\.1\\.18" in q for q in seen_per_node_queries)
    assert any("kube_node_info" in q for q in seen_per_node_queries)
    # host_points fallback populated all 4 chart families cleanly without double-counting smc-18
    assert res["summary"]["host_cpu_usage_percent"]["mean"] == pytest.approx(60.0)
    assert res["summary"]["host_memory_total_bytes"]["mean"] == pytest.approx(3000.0)
    assert res["summary"]["host_memory_used_bytes"]["mean"] == pytest.approx(1500.0)
    assert res["summary"]["host_memory_usage_percent"]["mean"] == pytest.approx(50.0)
    assert res["summary"]["host_disk_read_bytes_per_second"]["mean"] == pytest.approx(300.0)
    assert res["summary"]["host_network_receive_bytes_per_second"]["mean"] == pytest.approx(1200.0)
    assert res["summary"]["host_power_watts"]["mean"] == pytest.approx(750.0)


@pytest.mark.asyncio
async def test_per_node_nic_collection_ranking_cap_and_exclusion(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": [], "decode": ["w-a", "w-b"]}),
    )

    async def fake_list_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {"metadata": {"name": "w-a"}, "spec": {"nodeName": "node-a", "containers": []}},
                {"metadata": {"name": "w-b"}, "spec": {"nodeName": "node-b", "containers": []}},
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_list_resources)
    monkeypatch.setattr(service, "_query", AsyncMock(return_value=[]))

    # Build 18 physical/fabric/VLAN interfaces + virtual interfaces on node-a
    extra_devs = [f"eth{i}" for i in range(15)]
    virtual_devs = [
        "lo",
        "veth12",
        "cali123",
        "flannel.1",
        "cilium_host",
        "cni0",
        "docker0",
        "virbr0",
        "br-abc",
        "kube-ipvs0",
        "tunl0",
        "vxlan.calico",
    ]

    async def fake_range(_client, promql, _start, _end, _step):
        if "by (nodename, device)" in promql:
            is_rx = "node_network_receive_bytes_total" in promql
            rows = []
            # node-a has ib0 (highest), eno1.100 (VLAN), mlx5_0, plus 15 ethX + virtual devices
            rows.append(
                {
                    "metric": {"nodename": "node-a", "device": "ib0"},
                    "values": [[1000, "2000.0" if is_rx else "1000.0"], [1005, "4000.0" if is_rx else "2000.0"]],
                }
            )
            # Duplicate IP alias for ib0 on node-a must not double-count
            rows.append(
                {
                    "metric": {"node": "node-a", "device": "eno1.100"},
                    "values": [[1000, "500.0" if is_rx else "250.0"], [1005, "700.0" if is_rx else "350.0"]],
                }
            )
            rows.append(
                {
                    "metric": {"nodename": "node-a", "device": "mlx5_0"},
                    "values": [[1000, "300.0" if is_rx else "150.0"], [1005, "300.0" if is_rx else "150.0"]],
                }
            )
            for idx, dev in enumerate(extra_devs):
                val = str(100.0 + idx)
                rows.append({"metric": {"nodename": "node-a", "device": dev}, "values": [[1000, val], [1005, val]]})
            for vdev in virtual_devs:
                rows.append({"metric": {"nodename": "node-a", "device": vdev}, "values": [[1000, "999999.0"]]})
            # node-b has ib0 + ens1f0
            rows.append(
                {
                    "metric": {"nodename": "node-b", "device": "ib0"},
                    "values": [[1000, "1000.0" if is_rx else "500.0"], [1005, "1500.0" if is_rx else "750.0"]],
                }
            )
            rows.append(
                {
                    "metric": {"nodename": "node-b", "device": "ens1f0"},
                    "values": [[1000, "200.0" if is_rx else "100.0"], [1005, "200.0" if is_rx else "100.0"]],
                }
            )
            return rows
        return []

    monkeypatch.setattr(service, "_query_range", fake_range)
    res = await service.collect_namespace_observability(
        "c-nic",
        "ns-nic",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )

    assert res["availability"]["metrics"]["per_node.network_interfaces"] == "available"
    node_a, node_b = res["per_node"]
    # node-a capped at 16 NICs, top ranked first, zero virtual devices
    assert len(node_a["network_interfaces"]) == 16
    assert [n["device"] for n in node_a["network_interfaces"][:3]] == ["ib0", "eno1.100", "mlx5_0"]
    assert all(n["device"] not in set(virtual_devs) for n in node_a["network_interfaces"])
    assert node_a["network_interfaces"][0]["rx_bytes_per_second"] == {"mean": 3000.0, "max": 4000.0}
    assert node_a["network_interfaces"][0]["tx_bytes_per_second"] == {"mean": 1500.0, "max": 2000.0}
    assert [n["device"] for n in node_b["network_interfaces"]] == ["ib0", "ens1f0"]

    # per_node_series attaches network_interfaces map
    assert res["per_node_series"]["node-a"][0]["network_interfaces"]["ib0"] == {"rx": 2000.0, "tx": 1000.0}
    assert res["per_node_series"]["node-a"][0]["network_interfaces"]["eno1.100"] == {"rx": 500.0, "tx": 250.0}

    # Cluster-aggregate series sums across nodes per (device, timestamp) and caps at 16
    assert len(res["series"]) == 2
    assert res["series"][0]["network_interfaces"]["ib0"] == {"rx": 3000.0, "tx": 1500.0}
    assert res["series"][1]["network_interfaces"]["ib0"] == {"rx": 5500.0, "tx": 2750.0}
    assert res["series"][0]["network_interfaces"]["eno1.100"] == {"rx": 500.0, "tx": 250.0}
    assert res["series"][0]["network_interfaces"]["ens1f0"] == {"rx": 200.0, "tx": 100.0}
    assert len(res["series"][0]["network_interfaces"]) <= 16


def test_k8s_quantity_parsing_and_profile_resource_matching():
    from llm_d_lens.hardware.models import HardwareProfile

    # CPU cores
    assert service._parse_k8s_cpu_cores("500m") == 0.5
    assert service._parse_k8s_cpu_cores("4") == 4
    assert service._parse_k8s_cpu_cores("1500m") == 1.5
    assert service._parse_k8s_cpu_cores("1e3m") == 1
    assert service._parse_k8s_cpu_cores("250000000n") == 0.25
    assert service._parse_k8s_cpu_cores("500000u") == 0.5
    assert service._parse_k8s_cpu_cores(4) == 4
    assert service._parse_k8s_cpu_cores("1.25") == 1.25

    # Memory bytes
    assert service._parse_k8s_memory_bytes("16Gi") == 16 * 1024**3
    assert service._parse_k8s_memory_bytes("64G") == 64 * 10**9
    assert service._parse_k8s_memory_bytes("512Mi") == 512 * 1024**2
    assert service._parse_k8s_memory_bytes("1024Ki") == 1024**2
    assert service._parse_k8s_memory_bytes("68719476736") == 68719476736
    assert service._parse_k8s_memory_bytes("1e6") == 1_000_000

    # Device counts
    assert service._parse_k8s_device_count("2") == 2
    assert service._parse_k8s_device_count(4) == 4
    assert service._parse_k8s_device_count("0") == 0

    # Invalid / negative / non-finite / bool / oversized inputs
    for bad in (
        None,
        True,
        False,
        "",
        "   ",
        "-5",
        "-500m",
        "NaN",
        "Inf",
        "-Inf",
        "not-a-qty",
        {"raw": "1"},
        "1" * 65,
    ):
        assert service._parse_k8s_quantity(bad) is None
        assert service._parse_k8s_cpu_cores(bad) is None
        assert service._parse_k8s_memory_bytes(bad) is None
        assert service._parse_k8s_device_count(bad) is None

    # Control-char / ANSI CSI hygiene on node spec strings
    assert service._sanitize_k8s_spec_str("\x1b[31mUbuntu\x1b[0m\t24.04\r\nLTS\x00\x7f\x85") == "Ubuntu 24.04 LTS"
    assert service._sanitize_k8s_spec_str("\x1b[31m\x00\n\t\x1b[0m") is None

    # Profile-driven extended resource matching (non-NVIDIA, prefix + monitor suffix exclusion)
    custom_profile = HardwareProfile.from_dict(
        {
            "id": "acme-npu",
            "display_name": "Acme NPU",
            "vendor": "acme",
            "request_model": "extended-resource",
            "resource_prefixes": ["acme.example/npu"],
            "monitor_resource_suffixes": ["monitoring"],
            "deployment": {
                "overlay_root": "guides/{guide}",
                "arch": "npu",
                "resource_name": "acme.example/npu",
                "modes": {"plugin": {"request_model": "extended-resource", "resource_names": ["acme.example/npu"]}},
            },
        }
    )
    assert service._profile_matches_resource_key(custom_profile, "acme.example/npu") is True
    assert service._profile_matches_resource_key(custom_profile, "acme.example/npu-mig-1g") is True
    assert service._profile_matches_resource_key(custom_profile, "acme.example/npu/shard") is True
    assert service._profile_matches_resource_key(custom_profile, "acme.example/npu_monitoring") is False
    assert service._profile_matches_resource_key(custom_profile, "acme.example/npu/monitoring") is False
    assert service._profile_matches_resource_key(custom_profile, "other.io/gpu") is False
    assert service._any_profile_matches_resource_key([custom_profile], "acme.example/npu-mig-1g") is True
    assert service._any_profile_matches_resource_key([custom_profile], "cpu") is False
    assert (
        service._profile_allocated_accelerators(
            custom_profile,
            [
                {
                    "metadata": {"name": "w-0"},
                    "spec": {
                        "containers": [
                            {
                                "resources": {
                                    "requests": {
                                        "acme.example/npu-mig-1g": "2",
                                        "acme.example/npu_monitoring": "5",
                                    }
                                }
                            }
                        ]
                    },
                }
            ],
            None,
            {},
        )
        == 2
    )


@pytest.mark.asyncio
async def test_collect_observability_node_pod_hardware_specs_isolation_and_redaction(monkeypatch):
    import json

    from llm_d_lens.hardware.models import HardwareProfile

    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": ["ms-epp-0"], "prefill": ["prefill-0"], "decode": ["decode-0"]}),
    )

    custom_npu = HardwareProfile.from_dict(
        {
            "id": "acme-npu",
            "display_name": "Acme NPU",
            "vendor": "acme",
            "request_model": "extended-resource",
            "resource_prefixes": ["acme.example/npu"],
            "monitor_resource_suffixes": ["monitoring"],
            "telemetry": {
                "provider_id": "acme_npu",
                "allocation_join": "extended-resource",
                "device_metrics": {"device_info": "acme_npu_info"},
                "device_metric_sources": {
                    "utilization": {"metric": "acme_npu_util", "unit": "percent", "output_unit": "percent"}
                },
                "label_schema": {"namespace": "ns", "pod": "pod", "node": "Hostname", "model": "npu_card"},
            },
            "deployment": {
                "overlay_root": "guides/{guide}",
                "arch": "npu",
                "resource_name": "acme.example/npu",
                "modes": {"plugin": {"request_model": "extended-resource", "resource_names": ["acme.example/npu"]}},
            },
        }
    )
    dra_xpu = HardwareProfile.from_dict(
        {
            "id": "intel-xpu",
            "display_name": "Intel GPU",
            "vendor": "intel",
            "request_model": "dra",
            "device_classes": ["gpu.intel.com"],
            "resource_prefixes": ["gpu.intel.com/"],
            "telemetry": {
                "provider_id": "intel_gpu",
                "allocation_join": "dra",
                "device_metrics": {"device_info": "hw_gpu_info"},
                "device_metric_sources": {
                    "utilization": {
                        "metric": "hw_gpu_utilization_ratio",
                        "unit": "ratio",
                        "scale": 100,
                        "output_unit": "percent",
                    }
                },
                "label_schema": {"node": "node", "pci": "pci_bdf", "name": "hw_model"},
                "allocation": {"drivers": ["gpu.intel.com"], "pci_attributes": ["pciAddress"], "node_labels": ["node"]},
            },
            "deployment": {
                "overlay_root": "guides/{guide}",
                "arch": "xpu",
                "device_class": "gpu.intel.com",
                "modes": {"dra": {"request_model": "dra", "device_class": "gpu.intel.com"}},
            },
        }
    )
    monkeypatch.setattr(service, "_hardware_profiles", lambda: [custom_npu, dra_xpu])

    long_os_image = "Ubuntu 24.04 LTS " + ("x" * 200)

    async def fake_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {
                        "name": "prefill-0",
                        "namespace": "bench-hw",
                        "labels": {"llm-d.ai/role": "prefill"},
                        "annotations": {"secret-token": "TOP_SECRET_POD_ANN"},
                    },
                    "spec": {
                        "nodeName": "node-a",
                        "containers": [
                            {
                                "name": "modelserver",
                                "env": [{"name": "API_KEY", "value": "SECRET_ENV_VAL"}],
                                "args": ["--secret-arg=123"],
                                "command": ["/bin/sh"],
                                "resources": {
                                    "requests": {
                                        "cpu": "4",
                                        "memory": "16Gi",
                                        "acme.example/npu": "2",
                                        "acme.example/npu_monitoring": "99",
                                    },
                                    "limits": {
                                        "cpu": "8",
                                        "memory": "32Gi",
                                        "acme.example/npu": "2",
                                    },
                                },
                            },
                            {
                                "name": "sidecar",
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "512Mi"},
                                },
                            },
                        ],
                    },
                },
                {
                    "metadata": {"name": "decode-0", "uid": "uid-dec-0", "namespace": "bench-hw"},
                    "spec": {
                        "nodeName": "node-b",
                        "containers": [
                            {
                                "name": "modelserver",
                                "resources": {"requests": {"cpu": "2", "memory": "8Gi"}},
                            }
                        ],
                    },
                },
                {
                    "metadata": {
                        "name": "ms-epp-0",
                        "namespace": "bench-hw",
                        "labels": {"llm-d.ai/role": "epp"},
                    },
                    "spec": {
                        "nodeName": "node-a",
                        "containers": [
                            {
                                "name": "epp",
                                "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                            }
                        ],
                    },
                },
            ]
        if kind == "nodes":
            return [
                {
                    "metadata": {
                        "name": "node-a",
                        "annotations": {"node-secret": "TOP_SECRET_NODE_ANN"},
                    },
                    "status": {
                        "addresses": [{"type": "InternalIP", "address": "10.99.88.77"}],
                        "capacity": {"cpu": "64", "memory": "512Gi", "acme.example/npu-mig": "8"},
                        "allocatable": {"cpu": "62500m", "memory": "500Gi", "acme.example/npu-mig": "6"},
                        "nodeInfo": {
                            "architecture": "amd64",
                            "osImage": long_os_image,
                            "kernelVersion": "6.5.0-44-generic",
                            "kubeletVersion": "v1.31.2",
                            "containerRuntimeVersion": "containerd://1.7.13",
                        },
                    },
                },
                {
                    "metadata": {
                        "name": "node-b",
                        "labels": {"kubernetes.io/arch": "arm64"},
                    },
                    "status": {
                        "addresses": [{"type": "InternalIP", "address": "10.99.88.78"}],
                        "capacity": {"cpu": "32", "memory": "256G"},
                        "allocatable": {"cpu": "31", "memory": "250G"},
                        "nodeInfo": {
                            "osImage": "Debian GNU/Linux 12",
                            "kernelVersion": "6.1.0",
                            "kubeletVersion": "v1.31.2",
                            "containerRuntimeVersion": "cri-o://1.30.0",
                        },
                    },
                },
            ]
        if kind == "resourceclaims.resource.k8s.io":
            return [
                {
                    "metadata": {"name": "claim-dec-0", "namespace": "bench-hw"},
                    "status": {
                        "reservedFor": [{"resource": "pods", "name": "decode-0", "uid": "uid-dec-0"}],
                        "allocation": {
                            "devices": {
                                "results": [
                                    {"driver": "gpu.intel.com", "pool": "node-b", "device": "dev-0"},
                                    {"driver": "gpu.intel.com", "pool": "node-b", "device": "dev-1"},
                                ]
                            }
                        },
                    },
                }
            ]
        if kind == "resourceslices.resource.k8s.io":
            return [
                {
                    "spec": {
                        "driver": "gpu.intel.com",
                        "nodeName": "node-b",
                        "pool": {"name": "node-b"},
                        "devices": [
                            {"name": "dev-0", "attributes": {"pciAddress": {"string": "0000:01:00.0"}}},
                            {"name": "dev-1", "attributes": {"pciAddress": {"string": "0000:02:00.0"}}},
                            {"name": "dev-2", "attributes": {"pciAddress": {"string": "0000:03:00.0"}}},
                            {"name": "dev-3", "attributes": {"pciAddress": {"string": "0000:04:00.0"}}},
                        ],
                    }
                }
            ]
        return []

    async def fake_instant(_client, promql):
        if "acme_npu_info" in promql:
            return [
                {"metric": {"ns": "bench-hw", "pod": "prefill-0", "Hostname": "node-a", "npu_card": "Acme(TM) X900"}}
            ]
        if "hw_gpu_info" in promql:
            return [
                {
                    "metric": {
                        "node": "node-b",
                        "pci_bdf": "0000:01:00.0",
                        "hw_model": "Intel(R) Arc(TM) Pro B60",
                    },
                    "value": [1000, "1"],
                },
                {
                    "metric": {
                        "node": "other-node",
                        "pci_bdf": "0000:99:00.0",
                        "hw_model": "Unrelated Leak Card",
                    },
                    "value": [1000, "1"],
                },
            ]
        return []

    async def fake_range(_client, promql, _start, _end, _step):
        if "vllm:request_success_total" in promql and "finished_reason" not in promql:
            return [
                {"metric": {"pod": "prefill-0"}, "values": [[1000, "2"]]},
                {"metric": {"pod": "decode-0"}, "values": [[1000, "2"]]},
                {"metric": {"pod": "ms-epp-0"}, "values": [[1000, "4"]]},
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_resources)
    monkeypatch.setattr(service, "_query", fake_instant)
    monkeypatch.setattr(service, "_query_range", fake_range)

    res = await service.collect_namespace_observability(
        "c-hw",
        "bench-hw",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )

    node_map = {n["node"]: n for n in res["per_node"]}
    node_a = node_map["node-a"]
    node_b = node_map["node-b"]

    assert node_a["cpu_capacity_cores"] == 64
    assert node_a["cpu_allocatable_cores"] == 62.5
    assert node_a["memory_capacity_bytes"] == 512 * 1024**3
    assert node_a["accelerator_capacity"] == 8
    assert node_a["accelerator_allocatable"] == 6
    assert node_a["allocated_accelerators"] == 2
    assert node_a["accelerator_model"] == "Acme X900"
    assert node_a["architecture"] == "amd64"
    assert len(node_a["os_image"]) == 160
    assert node_a["kernel_version"] == "6.5.0-44-generic"
    assert node_a["kubelet_version"] == "v1.31.2"
    assert node_a["container_runtime"] == "containerd://1.7.13"

    assert node_b["cpu_capacity_cores"] == 32
    assert node_b["cpu_allocatable_cores"] == 31
    assert node_b["memory_capacity_bytes"] == 256 * 10**9
    assert node_b["accelerator_capacity"] == 4
    assert "accelerator_allocatable" not in node_b
    assert node_b["allocated_accelerators"] == 2
    assert node_b["accelerator_model"] == "Intel Arc Pro B60"
    assert node_b["architecture"] == "arm64"
    assert "Acme X900" not in node_b["accelerator_model"]
    assert "Unrelated Leak Card" not in node_b["accelerator_model"]

    pod_map = {p["pod"]: p for p in res["per_pod"]}
    prefill = pod_map["prefill-0"]
    decode = pod_map["decode-0"]
    epp = pod_map["ms-epp-0"]

    assert prefill["allocated_accelerators"] == 2
    assert prefill["cpu_request_cores"] == 4.5
    assert prefill["cpu_limit_cores"] == 8
    assert prefill["memory_request_bytes"] == 16 * 1024**3 + 512 * 1024**2
    assert prefill["memory_limit_bytes"] == 32 * 1024**3
    assert decode["allocated_accelerators"] == 2
    assert decode["cpu_request_cores"] == 2
    assert decode["memory_request_bytes"] == 8 * 1024**3
    assert "allocated_accelerators" not in epp

    serialized = json.dumps(res)
    for forbidden in (
        "10.99.88.77",
        "10.99.88.78",
        "TOP_SECRET_POD_ANN",
        "TOP_SECRET_NODE_ANN",
        "SECRET_ENV_VAL",
        "--secret-arg",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_per_node_accelerator_model_never_leaks_to_unlabeled_sibling_node(monkeypatch):
    monkeypatch.setattr(
        service,
        "_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.9:30090", source="explicit_url")),
    )
    monkeypatch.setattr(
        service,
        "_discover_components",
        AsyncMock(return_value={"epp": [], "prefill": ["w-a"], "decode": ["w-b"]}),
    )

    async def fake_resources(kind, **_kwargs):
        if kind == "pods":
            return [
                {
                    "metadata": {"name": "w-a", "namespace": "bench-iso"},
                    "spec": {
                        "nodeName": "node-a",
                        "containers": [{"name": "c", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
                    },
                },
                {
                    "metadata": {"name": "w-b", "namespace": "bench-iso"},
                    "spec": {
                        "nodeName": "node-b",
                        "containers": [{"name": "c", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
                    },
                },
            ]
        return []

    async def fake_instant(_client, promql):
        if "DCGM_FI_DEV_GPU_UTIL" in promql:
            # Unscoped sample (no Hostname/pod) + node-a-only sample: neither may leak to node-b
            return [
                {
                    "metric": {"namespace": "bench-iso", "pod": "w-a", "Hostname": "node-a", "modelName": "Card A"},
                    "value": [1000, "50"],
                },
                {
                    "metric": {"namespace": "bench-iso", "modelName": "Global Unscoped Card"},
                    "value": [1000, "50"],
                },
            ]
        return []

    monkeypatch.setattr(service, "list_resources", fake_resources)
    monkeypatch.setattr(service, "_query", fake_instant)
    monkeypatch.setattr(service, "_query_range", AsyncMock(return_value=[]))

    res = await service.collect_namespace_observability(
        "c-iso",
        "bench-iso",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:17:00Z",
    )
    node_map = {n["node"]: n for n in res["per_node"]}
    assert node_map["node-a"]["accelerator_model"] == "Card A"
    assert "accelerator_model" not in node_map["node-b"]
