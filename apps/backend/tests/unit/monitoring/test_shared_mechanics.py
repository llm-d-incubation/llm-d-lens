"""Monitoring adapter contracts without cluster or network access."""

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_d_lens.cluster import registry as cluster_registry
from llm_d_lens.monitoring import prometheus
from llm_d_lens.monitoring.command_output import parse_command_json
from llm_d_lens.monitoring.operation_runtime import installation_environment, run_install_command
from llm_d_lens.monitoring.prometheus import (
    PrometheusEndpoint,
    check_prometheus_health,
    create_prometheus_client,
    invalidate_prometheus_endpoint_cache,
    query_range,
    query_vector,
    resolve_prometheus_endpoint,
)
from llm_d_lens.monitoring.service_links import find_service, service_port


@pytest.fixture(autouse=True)
def _clear_prometheus_cache():
    invalidate_prometheus_endpoint_cache()
    yield
    invalidate_prometheus_endpoint_cache()


def test_command_parser_retains_default_and_domain_failure():
    class QueryError(Exception):
        def __init__(self, code, message, **details):
            self.code, self.details = code, details
            super().__init__(message)

    fallback = []
    assert (
        parse_command_json(
            SimpleNamespace(returncode=1, stdout="bad"), fallback, error_factory=QueryError, code="QUERY"
        )
        is fallback
    )
    with pytest.raises(QueryError) as failure:
        parse_command_json(
            SimpleNamespace(returncode=0, stdout="{bad", argv=["kubectl"]), None, error_factory=QueryError, code="QUERY"
        )
    assert failure.value.code == "QUERY"
    assert failure.value.details == {"retryable": True, "status_code": 502}


def test_service_selection_preserves_preferred_port_and_case_folding():
    service = {"metadata": {"name": "MON-Prometheus"}, "spec": {"ports": [{"port": 8080}, {"port": 9090}]}}
    assert find_service([service], lambda name: name.endswith("prometheus")) is service
    assert service_port(service, 9090) == 9090
    assert service_port(service, 3000) == 8080
    assert service_port({}, 3000) == 3000


@pytest.mark.asyncio
async def test_prometheus_keeps_vector_and_empty_failure_contract():
    client = SimpleNamespace(
        get=AsyncMock(
            return_value=httpx.Response(
                200,
                json={"status": "success", "data": {"result": [{"value": [1, "0"]}]}},
                request=httpx.Request("GET", "http://local/api/v1/query"),
            )
        )
    )
    assert await query_vector(client, "up") == [{"value": [1, "0"]}]
    client.get.assert_awaited_with("/api/v1/query", params={"query": "up"})
    client.get.side_effect = httpx.ConnectError("offline")
    assert await query_vector(client, "up") == []


def test_install_environment_filters_and_overrides_cluster_config(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "omit")
    monkeypatch.setenv("KUBECONFIG", "ambient")
    env = installation_environment("cluster", lambda _: {"KUBECONFIG": "selected"})
    assert env["KUBECONFIG"] == "selected"
    assert "UNRELATED_SECRET" not in env


@pytest.mark.asyncio
async def test_install_transport_kills_and_reaps_on_timeout():
    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                await asyncio.Future()
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

        @property
        def stdout(self):
            async def lines():
                yield b"waiting for pods\n"
                await asyncio.Future()

            return lines()

    process = Process()
    lines = []
    spawn = AsyncMock(return_value=process)
    with pytest.raises(TimeoutError):
        await run_install_command(["installer"], spawn=spawn, env={}, timeout=0.01, on_line=lines.append)
    assert process.killed and process.returncode == -9
    assert lines == ["waiting for pods\n"]


@pytest.mark.asyncio
async def test_resolve_prometheus_endpoint_prefers_direct_url_and_node_port_without_kubectl(monkeypatch):
    calls = []

    def fake_settings(cluster_id):
        calls.append(cluster_id)
        if cluster_id == "c-explicit":
            return {
                "monitoring_namespace": "custom-mon",
                "prometheus_url": "https://prom.example.com/prom",
                "prometheus_url_source": "explicit_url",
            }
        if cluster_id == "c-nodeport":
            return {
                "monitoring_namespace": "llm-d-monitoring",
                "prometheus_url": "http://10.20.30.40:30090",
                "prometheus_url_source": "node_port",
            }
        return None

    monkeypatch.setattr(cluster_registry, "get_cluster_monitoring_settings", fake_settings, raising=False)
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.41", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=endpoint if isinstance(endpoint, str) else endpoint.base_url,
            timeout=timeout,
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, text="Ready")),
        ),
    )
    kubectl_fn = AsyncMock(side_effect=AssertionError("kubectl must not be invoked"))

    explicit = await resolve_prometheus_endpoint("c-explicit", kubectl_fn=kubectl_fn)
    assert explicit == PrometheusEndpoint(
        base_url="https://prom.example.com/prom",
        source="explicit_url",
        namespace="custom-mon",
        local_port=None,
    )
    # Cached hit skips registry lookup within 60s; invalidation clears it.
    assert await resolve_prometheus_endpoint("c-explicit", kubectl_fn=kubectl_fn) == explicit
    assert calls == ["c-explicit"]
    invalidate_prometheus_endpoint_cache("c-explicit")
    assert await resolve_prometheus_endpoint("c-explicit", kubectl_fn=kubectl_fn) == explicit
    assert calls == ["c-explicit", "c-explicit"]

    node_port = await resolve_prometheus_endpoint("c-nodeport", allow_port_forward=False, kubectl_fn=kubectl_fn)
    assert node_port == PrometheusEndpoint(
        base_url="http://10.20.30.40:30090",
        source="node_port",
        namespace="llm-d-monitoring",
        local_port=None,
    )
    kubectl_fn.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_prometheus_endpoint_403_and_port_forward_fallback(monkeypatch):
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: (_ for _ in ()).throw(RuntimeError("db uninitialized")),
        raising=False,
    )

    forbidden_kubectl = AsyncMock(return_value=SimpleNamespace(returncode=1, stdout="Forbidden (403)"))
    assert await resolve_prometheus_endpoint("c-403", kubectl_fn=forbidden_kubectl) is None
    assert forbidden_kubectl.await_count == 1
    # 10s miss cache prevents duplicate kubectl call immediately after.
    assert await resolve_prometheus_endpoint("c-403", kubectl_fn=forbidden_kubectl) is None
    assert forbidden_kubectl.await_count == 1

    # allow_port_forward=False returns None without calling kubectl.
    assert await resolve_prometheus_endpoint("c-pf", allow_port_forward=False, kubectl_fn=forbidden_kubectl) is None
    assert forbidden_kubectl.await_count == 1

    # Priority 3 port-forward succeeds when kubectl lists *-prometheus service.
    ok_kubectl = AsyncMock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {"metadata": {"name": "kube-prom-prometheus"}, "spec": {"ports": [{"port": 9090}]}},
                    ]
                }
            ),
        )
    )
    forward_mock = AsyncMock(return_value=SimpleNamespace(local_port=19090))
    monkeypatch.setattr(prometheus, "ensure_port_forward", forward_mock)

    endpoint = await resolve_prometheus_endpoint("c-pf", kubectl_fn=ok_kubectl)
    assert endpoint == PrometheusEndpoint(
        base_url="http://127.0.0.1:19090",
        source="port_forward",
        namespace="llm-d-monitoring",
        local_port=19090,
    )
    forward_mock.assert_awaited_once_with("llm-d-monitoring", "kube-prom-prometheus", 9090, cluster_id="c-pf")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://169.254.169.254:9090",
        "http://169.254.170.2:9090",
        "http://[fd00:ec2::254]:9090",
        "http://100.100.100.200:9090",
        "http://[::ffff:169.254.169.254]:9090",
        "http://[fe80::1]:9090",
        "http://224.0.0.1:9090",
        "http://0.0.0.0:9090",
        "http://2852039166:9090",
        "http://0xa9fea9fe:9090",
        "http://0251.0376.0251.0376:9090",
        "http://169.254.43518:9090",
        "http://0:9090",
        "http://169.254.169.254.:9090",
        "http://[64:ff9b::169.254.169.254]:9090",
        "http://[64:ff9b:1::169.254.169.254]:9090",
        "http://[2002:a9fe:a9fe::1]:9090",
        "http://[2001:0:a9fe:a9fe::1]:9090",
        "http://user:pass@10.0.0.1:9090",
        "http://10.0.0.1\\foo:9090",
        "http://10.0.0.1:9090?x=1",
    ],
)
async def test_ssrf_blocks_metadata_link_local_multicast_and_unspecified_literals(monkeypatch, blocked_url):
    with pytest.raises(ValueError):
        create_prometheus_client(blocked_url)
    assert await check_prometheus_health(blocked_url) is False

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"prometheus_url": blocked_url, "prometheus_url_source": "explicit_url"},
        raising=False,
    )
    assert await resolve_prometheus_endpoint("c-ssrf", allow_port_forward=False) is None


@pytest.mark.asyncio
async def test_ssrf_dns_guard_blocks_metadata_resolution_and_allows_gaierror_for_mock_transport(monkeypatch):
    loop = asyncio.get_running_loop()

    async def rebind_to_metadata(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(loop, "getaddrinfo", rebind_to_metadata)
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"prometheus_url": "http://rebind.internal:9090", "prometheus_url_source": "explicit_url"},
        raising=False,
    )
    assert await resolve_prometheus_endpoint("c-rebind", allow_port_forward=False) is None
    assert await check_prometheus_health("http://rebind.internal:9090") is False

    # gaierror passes through so MockTransport tests on synthetic hostnames work cleanly.
    async def raise_gaierror(host, port, *_args, **_kwargs):
        raise socket.gaierror("synthetic test host")

    monkeypatch.setattr(loop, "getaddrinfo", raise_gaierror)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(404)
        if request.url.path == "/api/v1/query" and request.url.params.get("query") == "1":
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"values": [[1, "42"]]}]}})
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://prom.mock.local:9090",
        trust_env=False,
        follow_redirects=False,
    ) as client:
        assert await check_prometheus_health("http://prom.mock.local:9090", client=client) is True
        assert await query_range(client, "up", "1", "2", 5) == [{"values": [[1, "42"]]}]


@pytest.mark.asyncio
async def test_create_prometheus_client_disables_redirects_and_env_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8888")
    client = create_prometheus_client("http://127.0.0.1:9090")
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect_handler),
        base_url="http://127.0.0.1:9090",
        trust_env=False,
        follow_redirects=False,
    ) as redirect_client:
        assert await check_prometheus_health("http://127.0.0.1:9090", client=redirect_client) is False
        assert await query_vector(redirect_client, "up") == []
        assert await query_range(redirect_client, "up", "1", "2", 5) == []


@pytest.mark.asyncio
async def test_create_prometheus_client_per_request_hook_blocks_dns_rebinding_on_queries(monkeypatch):
    loop = asyncio.get_running_loop()
    resolved_ip = "10.0.0.5"

    async def dynamic_getaddrinfo(host, port, *_args, **_kwargs):
        if resolved_ip == "GAIERROR":
            raise socket.gaierror("synthetic test host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved_ip, port))]

    monkeypatch.setattr(loop, "getaddrinfo", dynamic_getaddrinfo)
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"prometheus_url": "http://rebind-cached.internal:9090", "prometheus_url_source": "explicit_url"},
        raising=False,
    )

    endpoint = await resolve_prometheus_endpoint("c-rebind-hook", allow_port_forward=False)
    assert endpoint is not None

    # DNS rebinds to metadata IP while endpoint remains cached for 60s.
    resolved_ip = "169.254.169.254"
    dialed = []

    def handler(request: httpx.Request) -> httpx.Response:
        dialed.append(str(request.url))
        return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "9"]}]}})

    client = create_prometheus_client(endpoint)
    client._transport = httpx.MockTransport(handler)
    try:
        assert await query_vector(client, "up") == []
        assert await query_range(client, "up", "1", "2", 5) == []
        assert dialed == []

        # gaierror passes through to httpx so MockTransport works cleanly.
        resolved_ip = "GAIERROR"
        assert await query_vector(client, 'up{pod=~"a\\\\.b"}') == [{"value": [1, "9"]}]
        assert len(dialed) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_prometheus_endpoint_rejects_invalid_namespace_and_service_name(monkeypatch):
    kubectl_fn = AsyncMock()
    forward_mock = AsyncMock()
    monkeypatch.setattr(prometheus, "ensure_port_forward", forward_mock)

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_namespace": "--kubeconfig=/tmp/evil"},
        raising=False,
    )
    assert await resolve_prometheus_endpoint("c-bad-ns", kubectl_fn=kubectl_fn) is None
    kubectl_fn.assert_not_called()

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_namespace": "llm-d-monitoring"},
        raising=False,
    )
    bad_service_kubectl = AsyncMock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {"metadata": {"name": "-evil-flag-prometheus"}, "spec": {"ports": [{"port": 9090}]}},
                    ]
                }
            ),
        )
    )
    assert await resolve_prometheus_endpoint("c-bad-svc", kubectl_fn=bad_service_kubectl) is None
    forward_mock.assert_not_called()


def test_service_links_auth_vs_forbidden_predicates():
    from llm_d_lens.monitoring.service_links import is_auth_error, is_forbidden_error

    assert is_auth_error("error: You must be logged in to the server (Unauthorized)") is True
    assert is_auth_error("HTTP 401 Unauthorized") is True
    assert is_auth_error("x509: certificate has expired") is True
    assert is_auth_error("Forbidden: HTTP 403") is False

    # Auth errors never masquerade as 403 forbidden
    assert is_forbidden_error("Unauthorized (HTTP 401)") is False
    assert is_forbidden_error("Error from server (Forbidden): servicemonitors is forbidden") is True
    assert is_forbidden_error("Kubernetes get failed (Forbidden, HTTP 403)") is True
    assert is_forbidden_error('cannot create resource "servicemonitors" in API group "monitoring.coreos.com"') is True
    # Bare port/ID numbers like "401" or "403" in unrelated text must not false-positive
    assert is_auth_error("port 40100 connection refused") is False
    assert is_forbidden_error("port 40300 connection refused") is False


@pytest.mark.asyncio
async def test_probe_monitoring_access_ssar_admin_non_admin_401_and_cache(monkeypatch):
    from llm_d_lens.monitoring.service_links import (
        invalidate_monitoring_access_cache,
        probe_monitoring_access,
    )

    invalidate_monitoring_access_cache()

    def _ssar_response(items, *, allowed_fn, consecutive_docs=False):
        docs = []
        for item in items:
            attrs = (item.get("spec") or {}).get("resourceAttributes") or {}
            docs.append(
                {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectAccessReview",
                    "spec": {"resourceAttributes": attrs},
                    "status": {"allowed": allowed_fn(attrs)},
                }
            )
        if consecutive_docs:
            return "\n".join(json.dumps(doc) for doc in docs)
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": docs})

    class FakeSsarRunner:
        def __init__(self, mode: str, *, consecutive_docs: bool = False):
            self.mode = mode
            self.consecutive_docs = consecutive_docs
            self.calls = 0

        async def run(self, argv, *, input=None, timeout=15, **_kwargs):  # noqa: A002 - matches runner interface
            del timeout
            if argv[:3] == ["kubectl", "config", "current-context"]:
                return SimpleNamespace(returncode=0, stdout="ctx-1\n", stderr="")
            self.calls += 1
            if self.mode == "401":
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="error: You must be logged in to the server (Unauthorized)"
                )
            if self.mode == "403_ssar":
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error from server (Forbidden): selfsubjectaccessreviews is forbidden",
                )
            payload = json.loads(input or "{}")
            items = payload.get("items") or []
            if self.mode == "admin":
                out = _ssar_response(items, allowed_fn=lambda _a: True, consecutive_docs=self.consecutive_docs)
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            if self.mode == "non_admin":
                # Only allow pods list, deny clusterrolebindings/namespaces/crds
                out = _ssar_response(
                    items,
                    allowed_fn=lambda a: a.get("resource") == "pods",
                    consecutive_docs=self.consecutive_docs,
                )
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="connection refused")

    admin_res = await probe_monitoring_access(
        "c-ssar", scope="cluster_stack", runner=FakeSsarRunner("admin", consecutive_docs=True)
    )
    assert admin_res.reachable is True
    assert admin_res.access_mode == "admin"
    assert admin_res.can_manage is True

    non_admin_res = await probe_monitoring_access("c-ssar", scope="accelerator", runner=FakeSsarRunner("non_admin"))
    assert non_admin_res.reachable is True
    assert non_admin_res.access_mode == "non_admin"
    assert non_admin_res.can_manage is False

    unauth_res = await probe_monitoring_access("c-ssar", scope="cluster_stack", runner=FakeSsarRunner("401"))
    assert unauth_res.reachable is False
    assert unauth_res.access_mode is None
    assert unauth_res.can_manage is False
    assert unauth_res.auth_error is True

    ssar_denied_res = await probe_monitoring_access("c-ssar", scope="cluster_stack", runner=FakeSsarRunner("403_ssar"))
    assert ssar_denied_res.reachable is True
    assert ssar_denied_res.access_mode == "non_admin"
    assert ssar_denied_res.can_manage is False


@pytest.mark.asyncio
async def test_check_http_endpoint_reachable_and_direct_links(monkeypatch):
    from llm_d_lens.monitoring import service_links

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    def make_transport(handler):
        def factory(url, *, timeout=3.0):
            del timeout
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url=url,
                trust_env=False,
                follow_redirects=False,
            )

        return factory

    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        make_transport(lambda req: httpx.Response(200) if req.url.path == "/api/health" else httpx.Response(404)),
    )
    assert await service_links.check_http_endpoint_reachable("http://10.20.30.40:30300") is True

    # Redirect to relative /login counts as reachable; cross-origin redirect does not
    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        make_transport(
            lambda req: (
                httpx.Response(404)
                if req.url.path == "/api/health"
                else httpx.Response(302, headers={"Location": "/login"})
            )
        ),
    )
    assert await service_links.check_http_endpoint_reachable("http://10.20.30.40:30300") is True

    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        make_transport(
            lambda req: (
                httpx.Response(404)
                if req.url.path == "/api/health"
                else httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
            )
        ),
    )
    assert await service_links.check_http_endpoint_reachable("http://10.20.30.40:30300") is False
    assert await service_links.check_http_endpoint_reachable("http://169.254.169.254:30300") is False

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_namespace": "obs-mon",
            "prometheus_url": "http://10.20.30.40:30090",
            "grafana_url": "https://grafana.example.com/grafana",
        },
        raising=False,
    )
    prom_link, graf_link = service_links.resolve_direct_monitoring_links(
        "c-links",
        namespace="obs-mon",
        grafana_dashboard_path="/d/abc/dash",
    )
    assert prom_link is not None and prom_link.available is True and prom_link.url == "http://10.20.30.40:30090"
    assert graf_link is not None and graf_link.available is True
    assert graf_link.url == "https://grafana.example.com/grafana"
    assert graf_link.dashboard_path == "/d/abc/dash"


@pytest.mark.asyncio
async def test_probe_accelerator_metrics_via_prometheus_profile_driven():
    from llm_d_lens.hardware.registry import get_profile
    from llm_d_lens.monitoring.service_links import probe_accelerator_metrics_via_prometheus

    profile = get_profile("nvidia")

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if "count by (Hostname)" in query:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "3"]}]}})
        if "DCGM_FI_DEV_GPU_UTIL" in query:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "12"]}]}})
        if "DCGM_FI_DEV_FB_USED" in query:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "6"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    async with httpx.AsyncClient(
        base_url="http://prom.mock.local:9090",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await probe_accelerator_metrics_via_prometheus(client, profile)

    assert result["detected"] is True
    assert result["series_count"] == 12  # max across metrics, not sum
    assert result["node_count"] == 3
    assert "DCGM_FI_DEV_GPU_UTIL" in result["metrics_found"]


@pytest.mark.asyncio
async def test_deployment_monitoring_non_admin_status_and_403_admin_handoff(monkeypatch):
    import importlib
    import subprocess

    from fastapi.testclient import TestClient

    from llm_d_lens.api.main import app
    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.auth.permissions import BUILTIN_ROLE_PERMISSIONS
    from llm_d_lens.monitoring.deployment import service as deploy_svc
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    deploy_router = importlib.import_module("llm_d_lens.monitoring.deployment.router")

    exec_ctx = SimpleNamespace(execution_id="exec-1", cluster_id="c-1", namespace="ns-deploy")
    exec_ctx_2 = SimpleNamespace(execution_id="exec-2", cluster_id="c-2", namespace="ns-deploy-2")
    monkeypatch.setattr(
        deploy_svc,
        "get_execution_context",
        lambda eid: exec_ctx_2 if eid == "exec-2" else exec_ctx,
    )
    monkeypatch.setattr(deploy_svc, "list_execution_contexts", lambda cluster_id=None: [exec_ctx])

    # Non-admin status: Prometheus HTTP ready, servicemonitors/podmonitors return 403, marker says enabled
    monkeypatch.setattr(
        deploy_svc,
        "probe_monitoring_access",
        AsyncMock(return_value=MonitoringAccessResult(reachable=True, access_mode="non_admin", can_manage=False)),
    )
    monkeypatch.setattr(
        deploy_svc,
        "resolve_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.5:30090", source="explicit_url")),
    )
    monkeypatch.setattr(deploy_svc, "check_prometheus_health", AsyncMock(return_value=True))

    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        if resource == "configmaps":
            return [{"metadata": {"name": deploy_svc.MARKER_NAME}, "data": {"state": "enabled"}}]
        if resource == "services":
            return [{"metadata": {"name": "vllm-epp", "labels": {"app.kubernetes.io/name": "vllm-epp"}}}]
        if resource == "pods":
            return [{"spec": {"serviceAccountName": "vllm-epp-sa"}}]
        return []

    monkeypatch.setattr(deploy_svc, "list_resources", fake_list_resources)

    apply_inputs: list[str] = []

    class ForbiddenRunner:
        async def run(self, argv, *, input=None, timeout=30):  # noqa: A002 - matches runner interface
            del timeout
            if argv[:2] == ["kubectl", "get"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="Kubernetes get failed (Forbidden, HTTP 403)")
            if argv[:2] == ["kubectl", "apply"]:
                apply_inputs.append(input or "")
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "Error from server (Forbidden): clusterrolebindings.rbac.authorization.k8s.io is forbidden "
                        '(config file "/var/lib/lens/kubeconfig_cache/c-1.kubeconfig", Bearer secret-tok-123)'
                    ),
                )
            if argv[:2] == ["kubectl", "delete"]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error from server (Forbidden): servicemonitors is forbidden",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy_svc, "scoped_runner", lambda _cid: ForbiddenRunner())

    cluster_status = await deploy_svc.get_cluster_status("c-1")
    assert cluster_status["cluster_reachable"] is True
    assert cluster_status["access_mode"] == "non_admin"
    assert cluster_status["can_manage"] is False
    assert cluster_status["stack_ready"] is True
    assert cluster_status["items"][0]["status"] == "enabled"
    assert cluster_status["items"][0]["rbac_restricted"] is True

    # 403 during install raises DeploymentMonitoringError("admin_handoff_required", status_code=403, extra=...)
    # AND privileged kubectl apply input must NOT contain ConfigMap/llm-d-prism-monitoring!
    with pytest.raises(deploy_svc.DeploymentMonitoringError) as handoff_err:
        await deploy_svc.install("exec-1")
    assert handoff_err.value.code == "admin_handoff_required"
    assert handoff_err.value.status_code == 403
    assert apply_inputs
    assert deploy_svc.MARKER_NAME not in apply_inputs[0]
    assert "kind: ConfigMap" not in apply_inputs[0]
    assert "secret-tok-123" not in handoff_err.value.extra["reason"]
    assert "kubeconfig_cache" not in handoff_err.value.extra["reason"]
    assert "[REDACTED]" in handoff_err.value.extra["reason"]
    assert "[REDACTED_KUBECONFIG_PATH]" in handoff_err.value.extra["reason"]
    script_payload = handoff_err.value.extra["admin_script"]
    assert script_payload["action"] == "install"
    assert script_payload["namespace"] == "ns-deploy"
    proc = subprocess.run(["bash", "-n"], input=script_payload["script"], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr

    # Untrusted/overlong labels in epp_service are filtered out in generated script
    filtered_script = deploy_svc.render_deployment_monitoring_admin_script(
        "enable",
        namespace="ns-deploy",
        epp_service={
            "name": "vllm-epp",
            "labels": {
                "app.kubernetes.io/name": "vllm-epp",
                "bad/key/extra": "ok",
                "too-long": "a" * 64,
            },
        },
    )
    assert "vllm-epp" in filtered_script
    assert "too-long" not in filtered_script
    assert "bad/key/extra" not in filtered_script

    # Dynamic EPP discovery when epp_service is None (e.g. non-admin got 403 listing services)
    dynamic_script = deploy_svc.render_deployment_monitoring_admin_script(
        "enable", namespace="ns-deploy", epp_service=None
    )
    proc_dyn = subprocess.run(["bash", "-n"], input=dynamic_script, text=True, capture_output=True, check=False)
    assert proc_dyn.returncode == 0, proc_dyn.stderr
    assert "tr -cd 'A-Za-z0-9._-'" in dynamic_script
    assert "llm-d-prism-epp-auth-delegator-ns-deploy" in dynamic_script
    assert "llm-d-prism-metrics-reader" in dynamic_script

    disable_script = deploy_svc.render_deployment_monitoring_admin_script("disable", namespace="ns-deploy")
    assert (
        subprocess.run(["bash", "-n"], input=disable_script, text=True, capture_output=True, check=False).returncode
        == 0
    )
    assert "state: disabled" in disable_script

    # Router returns 403 with admin_script in detail AND enforces cluster/permission scope unconditionally
    allowed_principal = Principal(
        user_id="u1",
        username="u1",
        bindings=(
            RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c-1"),
            RoleBinding(role="viewer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c-2"),
        ),
        role_permissions={
            "maintainer": BUILTIN_ROLE_PERMISSIONS["maintainer"],
            "viewer": frozenset({"monitoring:deployment:read"}),
        },
    )
    denied_principal = Principal(
        user_id="u2",
        username="u2",
        bindings=(RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c-other"),),
        role_permissions={"maintainer": BUILTIN_ROLE_PERMISSIONS["maintainer"]},
    )

    with TestClient(app) as client:
        monkeypatch.setattr(deploy_router, "current_principal", lambda _req: denied_principal)
        assert client.get("/api/v1/monitoring/deployments?cluster_id=c-1").status_code == 403
        # Closing ?cluster_id= omission IDOR: execution belongs to c-1, denied_principal only has c-other
        assert client.get("/api/v1/monitoring/deployments/exec-1").status_code == 403
        assert client.post("/api/v1/monitoring/deployments/exec-1/install").status_code == 403

        # Cross-cluster role escalation rejected: viewer on c-2 has read, not action
        monkeypatch.setattr(deploy_router, "current_principal", lambda _req: allowed_principal)
        assert client.post("/api/v1/monitoring/deployments/exec-2/install").status_code == 403
        assert client.post("/api/v1/monitoring/deployments/exec-2/install?cluster_id=c-2").status_code == 403

        # Maintainer on c-1 accessing exec-1 without ?cluster_id= succeeds (hits 403 admin_handoff_required from cluster)
        res = client.post("/api/v1/monitoring/deployments/exec-1/install")
        assert res.status_code == 403
        detail = res.json()["detail"]
        assert detail["code"] == "admin_handoff_required"
        assert detail["admin_script"]["filename"] == "lens-deployment-monitoring-install-ns-deploy.sh"

    # 401 is never masked as non_admin
    monkeypatch.setattr(
        deploy_svc,
        "probe_monitoring_access",
        AsyncMock(
            return_value=MonitoringAccessResult(
                reachable=False,
                access_mode=None,
                can_manage=False,
                auth_error=True,
                error="Unauthorized (HTTP 401)",
            )
        ),
    )
    with pytest.raises(deploy_svc.DeploymentMonitoringError) as unauth_err:
        await deploy_svc.disable("exec-1")
    assert unauth_err.value.status_code == 401
    assert unauth_err.value.code == "cluster_unauthorized"


@pytest.mark.asyncio
async def test_deployment_and_service_links_honor_custom_cluster_monitoring_namespace(monkeypatch):
    import yaml

    from llm_d_lens.monitoring import service_links
    from llm_d_lens.monitoring.deployment import service as deploy_svc
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    service_links.invalidate_monitoring_access_cache()
    exec_ctx = SimpleNamespace(execution_id="exec-custom", cluster_id="2790b2ae", namespace="ns-workload")
    monkeypatch.setattr(deploy_svc, "get_execution_context", lambda _eid: exec_ctx)
    monkeypatch.setattr(deploy_svc, "list_execution_contexts", lambda cluster_id=None: [exec_ctx])
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda cid: (
            {"monitoring_mode": "managed", "monitoring_namespace": "llm-d-monitoring-123454"}
            if cid == "2790b2ae"
            else None
        ),
        raising=False,
    )
    monkeypatch.setattr(
        deploy_svc,
        "get_cluster_monitoring_settings",
        cluster_registry.get_cluster_monitoring_settings,
    )

    # 1. probe_monitoring_access with namespace=None resolves cluster's monitoring_namespace
    seen_ssar_namespaces: list[str] = []

    class SsarSpyRunner:
        async def run(self, argv, *, input=None, timeout=15, **_kwargs):  # noqa: A002
            del argv, timeout
            payload = json.loads(input or "{}")
            docs = []
            for item in payload.get("items") or []:
                attrs = (item.get("spec") or {}).get("resourceAttributes") or {}
                if attrs.get("namespace"):
                    seen_ssar_namespaces.append(attrs["namespace"])
                docs.append({"spec": {"resourceAttributes": attrs}, "status": {"allowed": True}})
            return SimpleNamespace(returncode=0, stdout=json.dumps({"kind": "List", "items": docs}), stderr="")

    access_res = await service_links.probe_monitoring_access(
        "2790b2ae", scope="cluster_stack", runner=SsarSpyRunner(), use_cache=False
    )
    assert access_res.access_mode == "admin"
    assert seen_ssar_namespaces == ["llm-d-monitoring-123454"]

    # 2. deployment/service.py passes custom monitoring_namespace across access probes, pod checks,
    #    Prometheus SA discovery, and metrics-reader ClusterRoleBinding subject namespace
    probed_namespaces: list[str | None] = []

    async def spy_probe_access(_cid, *, scope="cluster_stack", namespace=None, **_kwargs):
        del scope
        probed_namespaces.append(namespace)
        return MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")

    queried_resources: list[tuple[str, str | None]] = []

    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        del selector, cluster_id
        queried_resources.append((resource, namespace))
        if resource == "pods" and namespace == "llm-d-monitoring-123454":
            return [{"status": {"phase": "Running"}}]
        if resource == "pods" and namespace == "ns-workload":
            return [{"spec": {"serviceAccountName": "epp-sa"}}]
        if resource == "services" and namespace == "ns-workload":
            return [{"metadata": {"name": "vllm-epp", "labels": {"app.kubernetes.io/name": "vllm-epp"}}}]
        if resource == "prometheuses" and namespace == "llm-d-monitoring-123454":
            return [{"spec": {"serviceAccountName": "custom-prom-sa"}}]
        if resource == "configmaps":
            return [{"metadata": {"name": deploy_svc.MARKER_NAME}, "data": {"state": "enabled"}}]
        return []

    applied_docs: list[dict] = []

    class ApplySpyRunner:
        async def run(self, argv, *, input=None, timeout=30):  # noqa: A002
            del timeout
            if argv[:2] == ["kubectl", "apply"] and input:
                for doc in yaml.safe_load_all(input):
                    if isinstance(doc, dict):
                        applied_docs.append(doc)
            if argv[:2] == ["kubectl", "get"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy_svc, "probe_monitoring_access", spy_probe_access)
    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    monkeypatch.setattr(deploy_svc, "list_resources", fake_list_resources)
    monkeypatch.setattr(deploy_svc, "scoped_runner", lambda _cid: ApplySpyRunner())

    installed = await deploy_svc.install("exec-custom")
    assert installed["stack_ready"] is True
    assert all(ns == "llm-d-monitoring-123454" for ns in probed_namespaces)
    assert ("pods", "llm-d-monitoring-123454") in queried_resources
    assert ("prometheuses", "llm-d-monitoring-123454") in queried_resources

    crbs = [
        d
        for d in applied_docs
        if d.get("kind") == "ClusterRoleBinding"
        and d.get("metadata", {}).get("name") == deploy_svc.METRICS_READER_BINDING
    ]
    assert len(crbs) == 1
    assert crbs[0]["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "custom-prom-sa",
            "namespace": "llm-d-monitoring-123454",
        }
    ]

    # 3. resolve_prometheus_endpoint strips padded explicit namespace
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"prometheus_url": "http://10.0.0.5:30090", "monitoring_namespace": "llm-d-monitoring"},
        raising=False,
    )
    monkeypatch.setattr(prometheus, "check_prometheus_health", AsyncMock(return_value=True))
    ep_padded = await resolve_prometheus_endpoint("c-pad", namespace="  padded-mon  ", use_cache=False)
    assert ep_padded is not None and ep_padded.namespace == "padded-mon"


@pytest.mark.asyncio
async def test_resolve_prometheus_endpoint_stale_url_nodeport_refresh_and_port_forward_fallback(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *_args, **_kwargs):
        if host == "rebind-node.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.99", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    updated_calls: list[tuple[str, dict, bool]] = []

    def spy_update(cluster_id: str, payload: dict, *, invalidate_execution: bool = False):
        updated_calls.append((cluster_id, dict(payload), invalidate_execution))
        return SimpleNamespace(id=cluster_id)

    monkeypatch.setattr(cluster_registry, "update_cluster_monitoring", spy_update, raising=False)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None, raising=False)
    monkeypatch.setattr(cluster_registry, "read_kubeconfig", lambda _cid: None, raising=False)

    # 1. Stale managed URL fails to connect -> discovers healthy NodePort on custom namespace & refreshes metadata
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "managed",
            "monitoring_namespace": "custom-mon",
            "prometheus_url": "http://10.20.30.40:30090",
            "grafana_url": "http://10.20.30.40:30300",
            "prometheus_node_port": 30090,
            "grafana_node_port": 30300,
            "prometheus_url_source": "node_port",
            "monitoring_host": "10.20.30.40",
        },
        raising=False,
    )

    probed_urls: list[str] = []

    def handler_nodeport(request: httpx.Request) -> httpx.Response:
        url = f"{request.url.scheme}://{request.url.netloc.decode() if isinstance(request.url.netloc, bytes) else request.url.netloc}"
        probed_urls.append(url)
        if "10.20.30.40:30090" in url:
            raise httpx.ConnectError("connection refused", request=request)
        if "10.20.30.50:31090" in url and request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready")
        return httpx.Response(503)

    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=endpoint if isinstance(endpoint, str) else endpoint.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [prometheus._ssrf_request_hook]},
            transport=httpx.MockTransport(handler_nodeport),
        ),
    )

    async def kubectl_nodeport(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        if argv[:2] == ["get", "services"]:
            assert argv[3] == "custom-mon"
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"type": "NodePort", "ports": [{"port": 9090, "nodePort": 31090}]},
                            }
                        ]
                    }
                ),
            )
        if argv[:2] == ["get", "nodes"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "10.20.30.50"}]}}]}
                ),
            )
        raise AssertionError(f"Unexpected kubectl call: {argv}")

    forward_fail = AsyncMock(
        side_effect=AssertionError("ensure_port_forward must not be called when NodePort is healthy")
    )
    monkeypatch.setattr(prometheus, "ensure_port_forward", forward_fail)

    ep_refreshed = await resolve_prometheus_endpoint("c-stale-np", allow_port_forward=True, kubectl_fn=kubectl_nodeport)
    assert ep_refreshed == PrometheusEndpoint(
        base_url="http://10.20.30.50:31090",
        source="node_port",
        namespace="custom-mon",
        local_port=None,
    )
    assert len(updated_calls) == 1
    assert updated_calls[0][0] == "c-stale-np"
    assert updated_calls[0][1]["prometheusUrl"] == "http://10.20.30.50:31090"
    assert updated_calls[0][1]["prometheusNodePort"] == 31090
    assert updated_calls[0][1]["namespace"] == "custom-mon"
    assert updated_calls[0][1]["mode"] == "managed"
    assert updated_calls[0][1]["grafanaUrl"] == "http://10.20.30.40:30300"
    assert updated_calls[0][2] is False

    # 2. Stale managed URL -> ClusterIP service (no NodePort) -> port-forward fallback + stale metadata cleanup
    updated_calls.clear()
    forward_ok = AsyncMock(return_value=SimpleNamespace(local_port=19091))
    monkeypatch.setattr(prometheus, "ensure_port_forward", forward_ok)

    async def kubectl_clusterip(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        assert argv[:4] == ["get", "services", "-n", "custom-mon"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                            "spec": {"type": "ClusterIP", "ports": [{"port": 9090}]},
                        }
                    ]
                }
            ),
        )

    ep_pf = await resolve_prometheus_endpoint("c-clusterip", use_cache=False, kubectl_fn=kubectl_clusterip)
    assert ep_pf == PrometheusEndpoint(
        base_url="http://127.0.0.1:19091",
        source="port_forward",
        namespace="custom-mon",
        local_port=19091,
    )
    forward_ok.assert_awaited_once_with(
        "custom-mon", "llmd-kube-prometheus-stack-prometheus", 9090, cluster_id="c-clusterip"
    )
    assert len(updated_calls) == 1
    assert updated_calls[0][1]["prometheusUrl"] is None
    assert updated_calls[0][1]["prometheusNodePort"] is None
    assert updated_calls[0][1]["namespace"] == "custom-mon"
    assert updated_calls[0][1]["mode"] == "managed"
    assert updated_calls[0][1]["grafanaUrl"] == "http://10.20.30.40:30300"
    assert updated_calls[0][2] is False

    # 3. Stale managed URL -> NodePort present in K8s but unreachable (TimeoutException) -> port-forward + cleanup
    updated_calls.clear()
    forward_ok.reset_mock()

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=endpoint if isinstance(endpoint, str) else endpoint.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [prometheus._ssrf_request_hook]},
            transport=httpx.MockTransport(handler_timeout),
        ),
    )
    ep_timeout_pf = await resolve_prometheus_endpoint("c-np-timeout", use_cache=False, kubectl_fn=kubectl_nodeport)
    assert ep_timeout_pf is not None and ep_timeout_pf.source == "port_forward"
    assert len(updated_calls) == 1
    assert updated_calls[0][1]["prometheusUrl"] is None
    assert updated_calls[0][1]["prometheusNodePort"] is None

    # 4. Existing-mode explicit user URL -> fallback works, NEVER overwrites user metadata
    updated_calls.clear()
    forward_ok.reset_mock()
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "existing",
            "monitoring_namespace": "obs-existing",
            "prometheus_url": "https://user-prom.example.com",
            "prometheus_url_source": "explicit_url",
        },
        raising=False,
    )

    async def kubectl_existing_clusterip(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        assert argv[:4] == ["get", "services", "-n", "obs-existing"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "ext-prometheus"},
                            "spec": {"type": "ClusterIP", "ports": [{"port": 9090}]},
                        }
                    ]
                }
            ),
        )

    ep_existing_pf = await resolve_prometheus_endpoint(
        "c-existing-user", use_cache=False, kubectl_fn=kubectl_existing_clusterip
    )
    assert ep_existing_pf == PrometheusEndpoint(
        base_url="http://127.0.0.1:19091",
        source="port_forward",
        namespace="obs-existing",
        local_port=19091,
    )
    assert updated_calls == []

    # 5. SSRF / blocked-IP rejection preserved on discovered NodePort URLs + DB-less guard
    probed_urls.clear()
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "existing",
            "monitoring_namespace": "custom-mon",
            "prometheus_url": "http://10.20.30.40:30090",
            "prometheus_url_source": "auto_discovered",
            "auto_discovered": True,
        },
        raising=False,
    )
    monkeypatch.setattr(
        cluster_registry,
        "update_cluster_monitoring",
        lambda *_a, **_kw: (_ for _ in ()).throw(SQLAlchemyError("no db")),
        raising=False,
    )

    async def kubectl_ssrf_nodes(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        if argv[:2] == ["get", "services"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"type": "NodePort", "ports": [{"port": 9090, "nodePort": 31090}]},
                            }
                        ]
                    }
                ),
            )
        if argv[:2] == ["get", "nodes"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {"status": {"addresses": [{"type": "InternalIP", "address": "169.254.169.254"}]}},
                            {"status": {"addresses": [{"type": "ExternalIP", "address": "rebind-node.internal"}]}},
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected kubectl call: {argv}")

    monkeypatch.setattr(
        prometheus,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=endpoint if isinstance(endpoint, str) else endpoint.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [prometheus._ssrf_request_hook]},
            transport=httpx.MockTransport(handler_nodeport),
        ),
    )

    ep_ssrf_fallback = await resolve_prometheus_endpoint(
        "c-ssrf-np", use_cache=False, kubectl_fn=kubectl_ssrf_nodes, skip_direct_url=True
    )
    assert ep_ssrf_fallback is not None and ep_ssrf_fallback.source == "port_forward"
    assert not any("169.254.169.254" in u or "rebind-node.internal" in u for u in probed_urls)


@pytest.mark.asyncio
async def test_resolve_prometheus_endpoint_validation_cache_invalidation_transient_preserve_and_bounded_probes(
    monkeypatch,
):
    from llm_d_lens.infrastructure.kubernetes import PortForwardError

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(_host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.99", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: None, raising=False)
    monkeypatch.setattr(cluster_registry, "read_kubeconfig", lambda _cid: None, raising=False)

    updated_calls: list[tuple[str, dict, bool]] = []

    def spy_update(cluster_id: str, payload: dict, *, invalidate_execution: bool = False):
        updated_calls.append((cluster_id, dict(payload), invalidate_execution))
        return SimpleNamespace(id=cluster_id)

    monkeypatch.setattr(cluster_registry, "update_cluster_monitoring", spy_update, raising=False)

    # 1. Colliding grafanaNodePort is cleared before normalization; invalid plan aborts without writing raw dict.
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "managed",
            "monitoring_namespace": "custom-mon",
            "prometheus_url": "http://10.20.30.40:30090",
            "grafana_url": "http://10.20.30.40:31090",
            "prometheus_node_port": 30090,
            "grafana_node_port": 31090,
            "auto_discovered": ["grafanaUrl", "grafanaNodePort"],
        },
        raising=False,
    )

    prometheus._update_cluster_monitoring_metadata(
        "c-collide",
        cluster_registry.get_cluster_monitoring_settings("c-collide"),
        prometheus_url="http://10.20.30.50:31090",
        prometheus_node_port=31090,
        host="10.20.30.50",
    )
    assert len(updated_calls) == 1
    assert updated_calls[0][1]["prometheusNodePort"] == 31090
    assert updated_calls[0][1]["grafanaNodePort"] is None
    assert updated_calls[0][1]["grafanaUrl"] is None

    updated_calls.clear()
    prometheus._update_cluster_monitoring_metadata(
        "c-invalid",
        {
            "monitoring_mode": "managed",
            "monitoring_namespace": "custom-mon",
            "grafana_url": "ftp://invalid-scheme.example.com",
        },
        prometheus_url="http://10.20.30.50:31090",
        prometheus_node_port=31090,
        host="10.20.30.50",
    )
    assert updated_calls == []

    # 2. _update_cluster_monitoring_metadata invalidates (cluster_id, True) cache too even when can_cache=False
    prometheus._ENDPOINT_CACHE[("c-cache-inv", True)] = (
        PrometheusEndpoint(base_url="http://10.20.30.40:30090", source="node_port", namespace="custom-mon"),
        1e12,
    )
    prometheus._update_cluster_monitoring_metadata(
        "c-cache-inv",
        {"monitoring_mode": "managed", "monitoring_namespace": "custom-mon"},
        prometheus_url=None,
        prometheus_node_port=None,
        clear_stale=True,
    )
    assert ("c-cache-inv", True) not in prometheus._ENDPOINT_CACHE

    # 3. Service with NodePort + failing probe + failing port-forward preserves managed NodePort metadata
    updated_calls.clear()
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "managed",
            "monitoring_namespace": "custom-mon",
            "prometheus_url": "http://10.20.30.50:31090",
            "prometheus_node_port": 31090,
        },
        raising=False,
    )
    monkeypatch.setattr(prometheus, "check_prometheus_health", AsyncMock(return_value=False))
    monkeypatch.setattr(prometheus, "ensure_port_forward", AsyncMock(side_effect=PortForwardError("pod restarting")))

    async def kubectl_many_nodes(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        if argv[:2] == ["get", "services"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"type": "NodePort", "ports": [{"port": 9090, "nodePort": 31090}]},
                            }
                        ]
                    }
                ),
            )
        if argv[:2] == ["get", "nodes"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {"status": {"addresses": [{"type": "InternalIP", "address": f"10.20.30.{i}"}]}}
                            for i in range(1, 11)
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected kubectl call: {argv}")

    probed_targets: list[str] = []

    async def spy_probe(url: str, *, timeout: float = 3.0) -> bool:
        del timeout
        probed_targets.append(url)
        return False

    monkeypatch.setattr(prometheus, "_probe_direct_url", spy_probe)
    res_transient = await resolve_prometheus_endpoint("c-transient", use_cache=False, kubectl_fn=kubectl_many_nodes)
    assert res_transient is None
    assert updated_calls == []
    # 4. Multi-node probing is bounded to _MAX_NODE_PORT_PROBE_HOSTS (never probes all 10 nodes)
    nodeport_probes = [u for u in probed_targets if u != "http://10.20.30.50:31090"]
    assert len(nodeport_probes) <= prometheus._MAX_NODE_PORT_PROBE_HOSTS

    # 5. Security: malicious node IP ("10.0.0.5/p"), "%" zone-ID authority, unrelated existing-mode
    #    autoDiscovered=["host"], and PortForwardError credential redaction.
    with pytest.raises(ValueError):
        create_prometheus_client("http://10.0.0.5%2F:9090")
    with pytest.raises(ValueError):
        create_prometheus_client("http://[fe80::1%eth0]:9090")

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "existing",
            "monitoring_namespace": "obs-existing",
            "prometheus_url": "https://user-prom.example.com",
            "prometheus_url_source": "explicit_url",
            "auto_discovered": ["host"],
        },
        raising=False,
    )
    assert (
        prometheus._should_update_cluster_monitoring(cluster_registry.get_cluster_monitoring_settings("c-ex")) is False
    )

    from llm_d_lens.db.dao.cluster import ClusterDao

    monkeypatch.setattr(
        ClusterDao,
        "get_provisioning_plan",
        lambda _self, _cid: {
            "monitoring": {
                "mode": "existing",
                "namespace": "obs-existing",
                "prometheusUrl": "https://user-prom.example.com",
                "host": "10.20.30.40",
                "autoDiscovered": ["host"],
            }
        },
    )
    updated_calls.clear()
    prometheus._update_cluster_monitoring_metadata(
        "c-ex-db",
        {
            "monitoring_mode": "existing",
            "monitoring_namespace": "obs-existing",
            "prometheus_url": "https://user-prom.example.com",
            "prometheus_url_source": "node_port",
            "auto_discovered": ["host"],
        },
        prometheus_url=None,
        prometheus_node_port=None,
        clear_stale=True,
    )
    assert updated_calls == []

    # Malicious node IP "10.0.0.5/p" must NEVER be turned into http://10.0.0.5/p:31090 (port 80)
    probed_targets.clear()

    async def kubectl_malicious_node(argv, *, cluster_id=None, timeout=10):
        del cluster_id, timeout
        if argv[:2] == ["get", "services"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"type": "NodePort", "ports": [{"port": 9090, "nodePort": 31090}]},
                            }
                        ]
                    }
                ),
            )
        if argv[:2] == ["get", "nodes"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.5/p"}]}},
                            {"status": {"addresses": [{"type": "ExternalIP", "address": "fe80::1%eth0"}]}},
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected kubectl call: {argv}")

    monkeypatch.setattr(
        prometheus,
        "ensure_port_forward",
        AsyncMock(side_effect=PortForwardError('failed with Bearer super-secret-jwt "/tmp/kubeconfig_cache/c1"')),
    )
    res_malicious = await resolve_prometheus_endpoint(
        "c-mal-node", use_cache=False, kubectl_fn=kubectl_malicious_node, skip_direct_url=True
    )
    assert res_malicious is None
    assert probed_targets == []


@pytest.mark.asyncio
async def test_central_stack_ready_non_admin_retry_fallback_and_admin_isolation(monkeypatch):
    from llm_d_lens.monitoring.deployment import service as deploy_svc

    # Preserve benchmark and metrics-reader RBAC contract exports
    assert deploy_svc.METRICS_READER_ROLE == "llm-d-prism-metrics-reader"
    assert deploy_svc.METRICS_READER_BINDING == "llm-d-prism-metrics-reader"
    assert deploy_svc.AUTH_DELEGATOR_ROLE == "system:auth-delegator"
    assert "for_benchmark" in deploy_svc.install.__kwdefaults__
    assert "for_benchmark" in deploy_svc.enable.__kwdefaults__

    monkeypatch.setattr(deploy_svc.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_namespace": "lens-monitoring"},
        raising=False,
    )

    # 1. Transient check_prometheus_health blip recovers on 0.2s retry with timeout=2.0
    ep_direct = PrometheusEndpoint(base_url="http://10.0.0.5:30900", source="explicit_url", namespace="lens-monitoring")
    health_calls: list[float | None] = []

    async def flaky_health(ep, *, timeout=3.0):
        assert ep is ep_direct
        health_calls.append(timeout)
        if len(health_calls) == 1:
            raise httpx.ConnectError("transient blip")
        return True

    resolve_mock = AsyncMock(return_value=ep_direct)
    list_resources_fail = AsyncMock(side_effect=AssertionError("non_admin must never list pods"))
    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", resolve_mock)
    monkeypatch.setattr(deploy_svc, "check_prometheus_health", flaky_health)
    monkeypatch.setattr(deploy_svc, "list_resources", list_resources_fail)

    assert await deploy_svc._central_stack_ready("c-blip", access_mode="non_admin") is True
    assert health_calls == [2.0, 2.0]
    assert resolve_mock.await_count == 1

    # 2. 1-arg check_prometheus_health test double (raises TypeError on timeout=) falls back cleanly
    async def one_arg_health(_ep):
        return True

    monkeypatch.setattr(deploy_svc, "check_prometheus_health", one_arg_health)
    assert await deploy_svc._central_stack_ready("c-stub", access_mode="non_admin") is True

    # 3. Non-admin initial None -> falls back to allow_port_forward=True, use_cache=True, skip_direct_url=False
    ep_nodeport = PrometheusEndpoint(base_url="http://10.0.0.6:30900", source="node_port", namespace="lens-monitoring")
    resolve_calls: list[tuple[bool, bool, bool]] = []

    async def fallback_resolve(cluster_id, *, allow_port_forward=True, use_cache=True, skip_direct_url=False):
        assert cluster_id == "c-fallback"
        resolve_calls.append((allow_port_forward, use_cache, skip_direct_url))
        if not allow_port_forward:
            return None
        return ep_nodeport

    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", fallback_resolve)
    monkeypatch.setattr(deploy_svc, "check_prometheus_health", AsyncMock(return_value=True))
    assert await deploy_svc._central_stack_ready("c-fallback", access_mode="non_admin") is True
    assert resolve_calls == [(False, False, False), (True, True, False)]

    # 4. Non-admin unhealthy direct endpoint -> passes skip_direct_url=True and probes distinct fallback
    resolve_calls.clear()
    ep_stale = PrometheusEndpoint(base_url="http://10.0.0.5:30900", source="explicit_url", namespace="lens-monitoring")
    ep_pf = PrometheusEndpoint(
        base_url="http://127.0.0.1:19090", source="port_forward", namespace="lens-monitoring", local_port=19090
    )

    async def stale_then_pf(cluster_id, *, allow_port_forward=True, use_cache=True, skip_direct_url=False):
        assert cluster_id == "c-stale"
        resolve_calls.append((allow_port_forward, use_cache, skip_direct_url))
        return ep_stale if not allow_port_forward else ep_pf

    async def health_by_url(ep, *, timeout=3.0):
        del timeout
        return ep.base_url == ep_pf.base_url

    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", stale_then_pf)
    monkeypatch.setattr(deploy_svc, "check_prometheus_health", health_by_url)
    assert await deploy_svc._central_stack_ready("c-stale", access_mode="non_admin") is True
    assert resolve_calls == [(False, False, False), (True, True, True)]

    # 5. Genuinely unreachable on non_admin returns False without calling list_resources("pods", ...)
    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    assert await deploy_svc._central_stack_ready("c-down", access_mode="non_admin") is False

    # 6. Admin path leaves allow_port_forward=True untouched and falls back to pod listing
    admin_resolve_calls: list[bool] = []

    async def admin_resolve(_cid, *, allow_port_forward=True, use_cache=True):
        del use_cache
        admin_resolve_calls.append(allow_port_forward)
        return

    admin_list = AsyncMock(return_value=[{"status": {"phase": "Running"}}])
    monkeypatch.setattr(deploy_svc, "resolve_prometheus_endpoint", admin_resolve)
    monkeypatch.setattr(deploy_svc, "list_resources", admin_list)
    assert await deploy_svc._central_stack_ready("c-admin", access_mode="admin") is True
    assert admin_resolve_calls == [False]
    admin_list.assert_awaited_once_with("pods", namespace="lens-monitoring", cluster_id="c-admin")
