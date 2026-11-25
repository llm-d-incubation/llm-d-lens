"""Tests for the `kubernetes-service` baseline: reuses a sibling Guide case's already-ready
deployment execution (no second deployment) and hits the Guide's registered baseline
endpoint -- the only way to match a Guide's own "vs a stock Kubernetes Service" benchmark
methodology (currently precise-prefix-cache-routing) without provisioning a second stack.
"""

import importlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from llm_d_lens.deploy.contracts import DeployableConfiguration
from llm_d_lens.evaluation.models import BenchmarkPlan, EvaluationCreateRequest
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum

router = importlib.import_module("llm_d_lens.evaluation.router")


def test_kubernetes_service_is_an_accepted_baseline_type():
    content = {
        "model": {"name": "Qwen/Qwen3-30B-A3B"},
        "decode": {"replicaCount": 8, "tensorParallelSize": 4},
        "runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.9.0"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="precise-prefix-cache-routing",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    artifact_id = "00000000-0000-4000-8000-000000000001"
    request = EvaluationCreateRequest(
        cluster_session_id="00000000-0000-4000-8000-000000000002",
        benchmark_plans=[
            {
                "id": "candidate",
                "configuration_artifact_id": artifact_id,
                "baseline_types": ["kubernetes-service"],
            }
        ],
    )

    cases = router._evaluation_cases(request, {artifact_id: SimpleNamespace(deployable_configuration=configuration)})

    assert cases[0]["kind"] == "guide"
    assert cases[0]["baseline_case_ids"] == ["baseline-1"]
    assert cases[1]["kind"] == "baseline"
    assert cases[1]["baseline_type"] == "kubernetes-service"
    assert cases[1]["dependent_guide_case_id"] == "guide-1"


def test_kubernetes_service_baseline_is_not_deduplicated_across_candidates():
    content = {
        "model": {"name": "Qwen/Qwen3-30B-A3B"},
        "decode": {"replicaCount": 8, "tensorParallelSize": 4},
        "runtime": {"image": "image:test"},
    }
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="precise-prefix-cache-routing",
        format="helm",
        content=content,
        checksum=configuration_checksum(content),
    )
    artifact_ids = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]
    request = EvaluationCreateRequest(
        cluster_session_id="00000000-0000-4000-8000-000000000003",
        benchmark_plans=[
            {
                "id": f"candidate-{index}",
                "configuration_artifact_id": artifact_id,
                "baseline_types": ["kubernetes-service"],
            }
            for index, artifact_id in enumerate(artifact_ids, start=1)
        ],
    )

    cases = router._evaluation_cases(
        request,
        {artifact_id: SimpleNamespace(deployable_configuration=configuration) for artifact_id in artifact_ids},
    )
    ordered = router._ordered_evaluation_cases(cases)

    assert [case["id"] for case in ordered] == ["guide-1", "baseline-1", "guide-2", "baseline-2"]
    assert ordered[1]["dependent_guide_case_id"] == "guide-1"
    assert ordered[3]["dependent_guide_case_id"] == "guide-2"


def test_baseline_type_rejects_unknown_values():
    with pytest.raises(ValidationError):
        BenchmarkPlan(
            configuration_artifact_id="00000000-0000-4000-8000-000000000001",
            baseline_types=["not-a-real-baseline-type"],
        )


def _workflow(guide_case: dict, baseline_case: dict) -> dict:
    guide_case.setdefault("baseline_case_ids", [baseline_case["id"]])
    return {
        "id": "workflow-1",
        "cluster_session_id": "session-1",
        "runtime": {"http_proxy": "", "https_proxy": "", "no_proxy": ""},
        "cases": [guide_case, baseline_case],
    }


def _baseline_case(**overrides) -> dict:
    case = {
        "id": "baseline-1",
        "kind": "baseline",
        "status": "queued",
        "baseline_type": "kubernetes-service",
        "benchmark": {
            "harness": "inference-perf",
            "workload": "guide_precise-prefix-cache-routing_1.yaml",
            "parallelism": 1,
            "wait_timeout_seconds": 60,
            "warmup_requests": 0,
        },
    }
    case.update(overrides)
    return case


@pytest.mark.asyncio
async def test_shared_pods_baseline_fails_clearly_without_a_ready_sibling_guide_case(monkeypatch):
    saved = []
    monkeypatch.setattr(router, "_save", saved.append)
    guide_case = {"id": "guide-1", "kind": "guide", "status": "queued"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert baseline_case["status"] == "failed"
    assert "no ready Guide deployment" in baseline_case["error"]


@pytest.mark.asyncio
async def test_shared_pods_baseline_fails_clearly_when_guide_has_no_baseline_endpoint(monkeypatch):
    saved = []
    monkeypatch.setattr(router, "_save", saved.append)
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url=None),
        namespace="ns",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert baseline_case["status"] == "failed"
    assert "kubernetes-service baseline endpoint" in baseline_case["error"]


@pytest.mark.asyncio
async def test_shared_pods_baseline_reuses_guide_execution_and_hits_baseline_url(monkeypatch):
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url="http://guide-baseline.local"),
        namespace="llmd-precise-prefix-cache-routing-run",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(router, "_save", lambda _record: None)
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", lambda *_args, **_kwargs: _async_none())

    captured_requests = []

    async def fake_create_run(request):
        captured_requests.append(request)
        return {"id": "benchmark-1"}

    async def fake_wait_for_benchmark(_benchmark_id):
        return {"status": "succeeded", "metrics": {"throughput_tps": 42}, "rate_stage_results": [{"rate": 1.0}]}

    monkeypatch.setattr(router, "create_run", fake_create_run)
    monkeypatch.setattr(router, "_wait_for_benchmark", fake_wait_for_benchmark)

    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    baseline_case["benchmark"]["harness_memory_gib"] = 64
    workflow = _workflow(guide_case, baseline_case)

    await router._execute_shared_pods_baseline_case(workflow, baseline_case)

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.deployment_execution_id == "exec-1"
    assert request.harness_memory_gib == 64
    assert request.use_baseline_endpoint is True
    assert baseline_case["status"] == "succeeded"
    assert baseline_case["execution_id"] == "exec-1"
    assert baseline_case["endpoint"] == "http://guide-baseline.local"
    assert baseline_case["endpoint_kind"] == "kubernetes-service"
    assert baseline_case["metrics"] == {"throughput_tps": 42}
    assert baseline_case["rate_stage_results"] == [{"rate": 1.0}]
    # Never owns a deployment -- nothing here should ever be cleaned up.
    assert "deployment_run_id" not in baseline_case or baseline_case.get("deployment_run_id") is None


@pytest.mark.asyncio
async def test_shared_pods_baseline_raises_when_the_benchmark_fails(monkeypatch):
    execution = SimpleNamespace(
        status=router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://guide.local", baseline_url="http://guide-baseline.local"),
        namespace="ns",
    )
    monkeypatch.setattr(router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(router, "_save", lambda _record: None)
    monkeypatch.setattr(router, "_kubernetes_resource_snapshot", lambda *_args, **_kwargs: _async_none())

    async def fake_create_run(_request):
        return {"id": "benchmark-1"}

    monkeypatch.setattr(router, "create_run", fake_create_run)

    async def fake_wait_for_benchmark(_benchmark_id):
        return {"status": "failed", "error": "boom"}

    monkeypatch.setattr(router, "_wait_for_benchmark", fake_wait_for_benchmark)

    guide_case = {"id": "guide-1", "kind": "guide", "status": "succeeded", "execution_id": "exec-1"}
    baseline_case = _baseline_case()
    workflow = _workflow(guide_case, baseline_case)

    with pytest.raises(ValueError, match="boom"):
        await router._execute_shared_pods_baseline_case(workflow, baseline_case)


async def _async_none():
    return None


def _envoy_yaml(*, tls: bool) -> str:
    return "static_resources:\n  clusters:\n" + (
        "  - name: ext_proc\n    transport_socket:\n      name: envoy.transport_sockets.tls\n"
        "    health_checks:\n    - tls_options: {}\n"
        if tls
        else "  - name: ext_proc\n"
    )


def _router_deployment(
    *, name="optimized-baseline-epp", secure_serving="false", envoy_cm="envoy", proxy_enabled=True, available_replicas=1
):
    epp_args = ["--pool-name", "optimized-baseline", "--grpc-health-port", "9003"]
    if secure_serving is not None:
        epp_args.append(f"--secure-serving={secure_serving}")
    containers = [
        {
            "name": "epp",
            "args": epp_args,
            "ports": [
                {"name": "grpc", "containerPort": 9002, "protocol": "TCP"},
                {"name": "metrics", "containerPort": 9090, "protocol": "TCP"},
            ],
        }
    ]
    volumes = []
    if proxy_enabled:
        containers.append(
            {
                "name": "envoy-proxy",
                "image": "envoyproxy/envoy:v1.31",
                "ports": [{"name": "http", "containerPort": 8081, "protocol": "TCP"}],
                "volumeMounts": [{"name": "proxy-cfg", "mountPath": "/etc/envoy"}],
            }
        )
        volumes.append({"name": "proxy-cfg", "configMap": {"name": envoy_cm}})
    return {
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": containers, "volumes": volumes},
            },
        },
        "status": {"availableReplicas": available_replicas},
    }


def _modelserver_deployment(
    *, name="optimized-baseline-epp-model-servers", ports=None, args=None, available_replicas=1
):
    container = {"name": "modelserver", "command": ["vllm", "serve"], "args": args or ["Qwen/Qwen3-0.6B"]}
    if ports is not None:
        container["ports"] = ports
    return {
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"llm-d.ai/role": "decode", "app": "ms"}},
                "spec": {"containers": [container]},
            },
        },
        "status": {"availableReplicas": available_replicas},
    }


def test_parse_in_cluster_service_url_and_envoy_epp_mismatch():
    assert router._parse_in_cluster_service_url("http://svc-a.ns-1.svc.cluster.local:8080", "def") == (
        "svc-a",
        "ns-1",
        8080,
    )
    assert router._parse_in_cluster_service_url("https://svc-b.ns-2.svc", "def") == ("svc-b", "ns-2", 443)
    assert router._parse_in_cluster_service_url("http://endpoint.local:8000", "def") is None
    assert router._parse_in_cluster_service_url("http://svc.--kubeconfig=/etc/foo.svc:80", "def") is None
    assert router._parse_in_cluster_service_url("http://--server=evil.ns-1.svc:80", "def") is None

    dep_plaintext_epp = _router_deployment(secure_serving="false")
    assert router._detect_envoy_epp_mismatch(dep_plaintext_epp, _envoy_yaml(tls=True)) == (
        "envoy_ext_proc_tls_mismatch_epp_plaintext"
    )
    assert router._detect_envoy_epp_mismatch(dep_plaintext_epp, _envoy_yaml(tls=False)) is None

    dep_tls_epp = _router_deployment(secure_serving=None)
    assert router._detect_envoy_epp_mismatch(dep_tls_epp, _envoy_yaml(tls=False)) == (
        "envoy_ext_proc_plaintext_mismatch_epp_tls"
    )
    assert router._detect_envoy_epp_mismatch(dep_tls_epp, _envoy_yaml(tls=True)) is None


@pytest.mark.asyncio
async def test_check_target_service_health_covers_failures_and_healthy_cases():
    svc_router = {
        "metadata": {"name": "optimized-baseline-epp"},
        "spec": {"selector": {"app": "optimized-baseline-epp"}, "ports": [{"port": 80, "targetPort": "http"}]},
    }
    dep_mismatch = _router_deployment(secure_serving="false", envoy_cm="custom-envoy-cm")
    assert (
        await router._check_target_service_health(
            "missing", 80, [svc_router], [dep_mismatch], lambda _n: {"data": {"envoy.yaml": _envoy_yaml(tls=False)}}
        )
        == "service_not_found"
    )
    assert (
        await router._check_target_service_health(
            "optimized-baseline-epp", 8443, [svc_router], [dep_mismatch], lambda _n: None
        )
        == "service_port_not_found"
    )
    dep_zero = _router_deployment(available_replicas=0)
    assert (
        await router._check_target_service_health(
            "optimized-baseline-epp", 80, [svc_router], [dep_zero], lambda _n: None
        )
        == "backing_deployment_unavailable"
    )
    dep_no_proxy = _router_deployment(proxy_enabled=False)
    svc_no_proxy = {
        "metadata": {"name": "optimized-baseline-epp"},
        "spec": {"selector": {"app": "optimized-baseline-epp"}, "ports": [{"port": 80, "targetPort": 8081}]},
    }
    assert (
        await router._check_target_service_health(
            "optimized-baseline-epp", 80, [svc_no_proxy], [dep_no_proxy], lambda _n: None
        )
        == "proxy_target_port_not_listening"
    )
    assert (
        await router._check_target_service_health(
            "optimized-baseline-epp",
            80,
            [svc_router],
            [dep_mismatch],
            lambda name: {"data": {"envoy.yaml": _envoy_yaml(tls=True)}} if name == "custom-envoy-cm" else None,
        )
        == "envoy_ext_proc_tls_mismatch_epp_plaintext"
    )
    assert (
        await router._check_target_service_health(
            "optimized-baseline-epp",
            80,
            [svc_router],
            [dep_mismatch],
            lambda name: {"data": {"envoy.yaml": _envoy_yaml(tls=False)}} if name == "custom-envoy-cm" else None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_discover_endpoints_excludes_epp_and_shadow_ports_and_supports_pods_and_inferred_ports():
    services = [
        {
            "metadata": {"name": "optimized-baseline-ip-5790737e"},
            "spec": {"selector": {"llm-d.ai/role": "decode"}, "ports": [{"port": 54321, "targetPort": 54321}]},
        },
        {
            "metadata": {"name": "ms-headless"},
            "spec": {"clusterIP": "None", "selector": {"llm-d.ai/role": "decode"}, "ports": [{"port": 8088}]},
        },
        {
            "metadata": {"name": "ms-lws-model-servers"},
            "spec": {"selector": {"lws": "worker"}, "ports": [{"port": 8000, "targetPort": "modelserver"}]},
        },
        {
            "metadata": {"name": "epp-grpc-only"},
            "spec": {"selector": {"app": "epp-only"}, "ports": [{"port": 9002}, {"port": 9090}]},
        },
    ]
    deployments = [
        _modelserver_deployment(args=["Qwen/Qwen3-0.6B", "--port=8088"], ports=None),
        _router_deployment(name="epp-only", proxy_enabled=False),
    ]
    deployments[1]["spec"]["template"]["metadata"]["labels"] = {"app": "epp-only"}
    pods = [
        {
            "metadata": {"name": "lws-pod-0", "labels": {"lws": "worker"}},
            "spec": {
                "containers": [{"name": "modelserver", "ports": [{"name": "modelserver", "containerPort": 8000}]}]
            },
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
    ]

    router_url, direct_url = router._discover_namespace_inference_endpoints(services, deployments, "ns-1", pods=pods)
    assert router_url is None
    assert direct_url == "http://ms-lws-model-servers.ns-1.svc.cluster.local:8000"
    assert (
        await router._check_target_service_health("ms-lws-model-servers", 8000, services, [], None, pods=pods) is None
    )

    unready_pods = [
        {
            "metadata": {"name": "lws-pod-0", "labels": {"lws": "worker"}},
            "spec": {"containers": [{"name": "modelserver"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]},
        }
    ]
    assert (
        await router._check_target_service_health("ms-lws-model-servers", 8000, services, [], None, pods=unready_pods)
        == "backing_deployment_unavailable"
    )


@pytest.mark.asyncio
async def test_resolve_benchmark_endpoint_keeps_healthy_router_and_falls_back_on_tls_mismatch(monkeypatch):
    ns = "ns-e20cf405"
    services = [
        {
            "metadata": {"name": "optimized-baseline-epp"},
            "spec": {"selector": {"app": "optimized-baseline-epp"}, "ports": [{"port": 80, "targetPort": 8081}]},
        },
        {
            "metadata": {"name": "optimized-baseline-ip-5790737e"},
            "spec": {"selector": {"llm-d.ai/role": "decode"}, "ports": [{"port": 54321, "targetPort": 54321}]},
        },
        {
            "metadata": {"name": "optimized-baseline-epp-model-servers"},
            "spec": {"selector": {"llm-d.ai/role": "decode"}, "ports": [{"port": 8000}]},
        },
    ]
    ms_dep = _modelserver_deployment()

    def setup_k8s(*, envoy_tls: bool):
        dep = _router_deployment(secure_serving="false", envoy_cm="envoy")

        async def fake_k8s_json(args, _env):
            if args[:2] == ["get", "services"]:
                return {"items": services}
            if args[:2] == ["get", "deployments"]:
                return {"items": [dep, ms_dep]}
            if args[:2] == ["get", "pods"]:
                return {"items": []}
            if args[:3] == ["get", "configmap", "envoy"]:
                return {"data": {"envoy.yaml": _envoy_yaml(tls=envoy_tls)}}
            return None

        monkeypatch.setattr(router, "_eval_k8s_json", fake_k8s_json)

    execution = SimpleNamespace(
        namespace=ns,
        endpoint=SimpleNamespace(url=f"http://optimized-baseline-epp.{ns}.svc.cluster.local:80", baseline_url=None),
        provenance={},
        artifact=None,
    )

    # Healthy plaintext Envoy + EPP -> benchmarks :80 router directly (no fallback).
    setup_k8s(envoy_tls=False)
    run_ok: dict = {}
    resolved_ok = await router._resolve_benchmark_endpoint(run_ok, execution, {})
    assert resolved_ok == f"http://optimized-baseline-epp.{ns}.svc.cluster.local:80"
    assert "endpoint_fallback" not in run_ok

    # Broken e20cf405 Envoy TLS vs EPP plaintext -> cleanly falls back to *-model-servers:8000 without mutating K8s.
    setup_k8s(envoy_tls=True)
    run_bad: dict = {}
    resolved_bad = await router._resolve_benchmark_endpoint(run_bad, execution, {})
    assert resolved_bad == f"http://optimized-baseline-epp-model-servers.{ns}.svc.cluster.local:8000"
    assert run_bad["endpoint_fallback"] == {
        "original_endpoint": f"http://optimized-baseline-epp.{ns}.svc.cluster.local:80",
        "resolved_endpoint": f"http://optimized-baseline-epp-model-servers.{ns}.svc.cluster.local:8000",
        "reason": "envoy_ext_proc_tls_mismatch_epp_plaintext",
    }

    # Loopback 127.0.0.1 with healthy router -> resolves to discovered router_url; with broken router -> falls back to direct.
    setup_k8s(envoy_tls=False)
    exec_loopback = SimpleNamespace(
        namespace=ns,
        endpoint=SimpleNamespace(url="http://127.0.0.1:8080", baseline_url=None),
        provenance={},
        artifact=None,
    )
    run_lb_ok: dict = {}
    assert (
        await router._resolve_benchmark_endpoint(run_lb_ok, exec_loopback, {})
        == f"http://optimized-baseline-epp.{ns}.svc.cluster.local:80"
    )
    assert "endpoint_fallback" not in run_lb_ok

    setup_k8s(envoy_tls=True)
    run_lb_bad: dict = {}
    assert (
        await router._resolve_benchmark_endpoint(run_lb_bad, exec_loopback, {})
        == f"http://optimized-baseline-epp-model-servers.{ns}.svc.cluster.local:8000"
    )
    assert run_lb_bad["endpoint_fallback"]["reason"] == "envoy_ext_proc_tls_mismatch_epp_plaintext"
