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
