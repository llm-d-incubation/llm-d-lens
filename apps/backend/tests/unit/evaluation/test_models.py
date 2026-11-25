"""Tests for configurable llm-d-benchmark sources."""

import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from llm_d_lens.evaluation.models import BenchmarkPlan, BenchmarkSource
from llm_d_lens.evaluation.router import (
    _cluster_benchmark_runtime,
    _execution_accelerator_profile,
    _harness_proxy_override,
    _install_helm_proxy_wrapper,
    _prepare_benchmark_storage,
)

evaluate_router = importlib.import_module("llm_d_lens.evaluation.router")


def test_benchmark_plan_normalizes_multiple_baselines_and_legacy_alias():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        baseline_types=["direct-vllm", "router-round-robin", "optimized-baseline"],
    )

    assert plan.baseline_types == ["direct-vllm", "router-neutral", "optimized-baseline"]
    assert plan.baseline_type == "direct-vllm"


def test_benchmark_plan_accepts_optimized_baseline_ablation_arms():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        baseline_types=["kubernetes-service", "load-only", "affinity-only"],
    )
    assert plan.baseline_types == ["kubernetes-service", "load-only", "affinity-only"]


def test_benchmark_plan_disables_all_baselines_when_not_included():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        include_baseline=False,
        baseline_types=["direct-vllm", "optimized-baseline"],
    )

    assert plan.baseline_types == []


def test_benchmark_plan_accepts_named_suite_scenarios_and_keeps_legacy_benchmark():
    plan = BenchmarkPlan(
        id="candidate",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        benchmark={"workload": "legacy.yaml"},
        scenarios=[
            {
                "id": "interactive",
                "name": "Interactive chat",
                "benchmark": {
                    "matrix": [{"isl": 256, "osl": 128}],
                    "concurrency_stages": [{"concurrency": 8, "num_requests": 100}],
                },
                "sla_targets": {"ttft_ms": 500, "tpot_ms": 50, "success_rate_min_percent": 99},
            }
        ],
    )

    assert plan.benchmark.workload == "legacy.yaml"
    assert plan.scenarios[0].benchmark.matrix[0].isl == 256
    assert plan.scenarios[0].sla_targets.ttft_percentile == "p99"


def test_benchmark_plan_rejects_duplicate_suite_scenario_ids():
    with pytest.raises(ValidationError, match="scenario ids must be unique"):
        BenchmarkPlan(
            id="candidate",
            configuration_artifact_id="00000000-0000-4000-8000-000000000001",
            scenarios=[
                {"id": "same", "name": "First"},
                {"id": "same", "name": "Second"},
            ],
        )


def test_benchmark_source_accepts_https_repository_and_version():
    source = BenchmarkSource(
        repository="https://github.com/llm-d/llm-d-benchmark.git",
        revision="v1.2.3",
    )
    assert source.revision == "v1.2.3"


def test_benchmark_source_rejects_credentials_and_unsafe_revision():
    with pytest.raises(ValidationError):
        BenchmarkSource(repository="https://token@example.com/repo.git", revision="main")
    with pytest.raises(ValidationError):
        BenchmarkSource(repository="https://example.com/repo.git", revision="--upload-pack=bad")


def test_cluster_benchmark_runtime_uses_registered_checkout(monkeypatch, tmp_path):
    checkout = tmp_path / "llm-d-benchmark"
    checkout.mkdir()
    monkeypatch.setattr(
        evaluate_router,
        "require_active_session",
        lambda _session_id: SimpleNamespace(server_id="cluster-1"),
    )
    monkeypatch.setattr(
        evaluate_router.cluster_registry,
        "require_cluster",
        lambda cluster_id: SimpleNamespace(
            id=cluster_id,
            name="test-cluster",
            llm_d_lensmark_repo_path=str(checkout),
            llm_d_lensmark_ref="v0.4.0",
        ),
    )

    assert _cluster_benchmark_runtime("a" * 36) == {
        "resolved_repository": str(checkout),
        "ref": "v0.4.0",
        "resolved_from": "cluster-registry",
    }


def test_harness_proxy_environment_preserves_backend_cluster_bypass(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,10.112.228.229,10.233.0.0/18")
    monkeypatch.setenv("no_proxy", "localhost,10.112.0.0/16")
    environment = evaluate_router._harness_proxy_environment(
        {
            "http_proxy": "http://proxy.example:911",
            "https_proxy": "http://proxy.example:911",
            "no_proxy": "intel.com,.intel.com,localhost,127.0.0.1",
        }
    )

    assert environment["NO_PROXY"] == environment["no_proxy"]
    assert environment["NO_PROXY"].split(",") == [
        "intel.com",
        ".intel.com",
        "localhost",
        "127.0.0.1",
        "10.112.228.229",
        "10.233.0.0/18",
        "10.112.0.0/16",
        ".svc",
        ".cluster.local",
    ]
    assert environment["HTTP_PROXY"] == environment["http_proxy"] == "http://proxy.example:911"
    assert environment["HTTPS_PROXY"] == environment["https_proxy"] == "http://proxy.example:911"


@pytest.mark.parametrize("run_no_proxy", [None, "", " , localhost, localhost , "])
def test_harness_proxy_environment_normalizes_empty_and_duplicate_bypass(monkeypatch, run_no_proxy):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    environment = evaluate_router._harness_proxy_environment({"no_proxy": run_no_proxy})

    assert environment["NO_PROXY"] == "localhost,127.0.0.1,.svc,.cluster.local"
    assert environment["NO_PROXY"] == environment["no_proxy"]


def test_harness_proxy_environment_always_bypasses_kubeconfig_api_server(monkeypatch, tmp_path):
    """Regression test: an IP-based API server behind a corporate proxy must
    always be reachable, even when neither the run nor the backend's own
    NO_PROXY happen to include it (see the "warm-up run failed ... ProxyError
    ... Tunnel connection failed: 403 Forbidden" class of bug)."""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    kubeconfig = tmp_path / "kubeconfig.yaml"
    kubeconfig.write_text(
        "clusters:\n- cluster:\n    server: https://10.112.228.229:6443\n  name: cluster\n",
        encoding="utf-8",
    )

    environment = evaluate_router._harness_proxy_environment(
        {"http_proxy": "http://proxy.example:911", "https_proxy": "http://proxy.example:911"},
        kubeconfig_path=str(kubeconfig),
    )

    assert "10.112.228.229" in environment["NO_PROXY"].split(",")


def test_harness_proxy_environment_tolerates_missing_or_invalid_kubeconfig(tmp_path):
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=None) is not None
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=str(tmp_path / "missing.yaml")) is not None
    garbage = tmp_path / "garbage.yaml"
    garbage.write_text("not: [valid, yaml:", encoding="utf-8")
    assert evaluate_router._harness_proxy_environment({}, kubeconfig_path=str(garbage)) is not None


def test_helm_proxy_wrapper_scopes_proxy_to_helm(monkeypatch, tmp_path):
    helm = tmp_path / "system-bin" / "helm"
    helm.parent.mkdir()
    helm.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        'print(json.dumps({k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}))\n',
        encoding="utf-8",
    )
    helm.chmod(0o700)
    environment = {"PATH": str(helm.parent)}

    _install_helm_proxy_wrapper(
        tmp_path / "output",
        environment,
        {"HTTPS_PROXY": "http://proxy.example:911", "NO_PROXY": "localhost"},
    )

    wrapper = tmp_path / "output" / ".bin" / "helm"
    assert wrapper.is_file()
    assert environment["PATH"].split(os.pathsep)[0] == str(wrapper.parent)
    completed = subprocess.run([str(wrapper), "version"], env=environment, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"HTTPS_PROXY": "http://proxy.example:911", "NO_PROXY": "localhost"}
    assert "HTTPS_PROXY" not in environment


def test_harness_proxy_override_injects_proxy_values_directly():
    override = _harness_proxy_override(
        {
            "HTTP_PROXY": "http://proxy.example:911",
            "HTTPS_PROXY": "http://proxy.example:911",
            "NO_PROXY": "localhost,.svc",
            "http_proxy": "ignored-duplicate",
        }
    )

    assert override is not None
    assert json.loads(override.split("=", 1)[1]) == [
        {"name": "HTTP_PROXY", "value": "http://proxy.example:911"},
        {"name": "HTTPS_PROXY", "value": "http://proxy.example:911"},
        {"name": "NO_PROXY", "value": "localhost,.svc"},
    ]


def test_accelerator_profile_is_derived_from_deployment_manifest():
    execution = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                provider_ref="optimized-baseline",
                content=json.dumps(
                    {
                        "officialGuide": {
                            "renderedManifest": "deviceClassName: gpu.intel.com\nnetworkDeviceClassName: dra.net\n"
                        }
                    }
                ),
            )
        ]
    )

    assert _execution_accelerator_profile(execution) == "intel-xpu"
    with pytest.raises(ValueError, match="does not match"):
        _execution_accelerator_profile(execution, "custom.xpu.example")


def test_accelerator_profile_is_derived_for_generated_xpu_baseline():
    execution = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                provider_ref="baseline-vllm",
                content=json.dumps({"runtime": {"image": "ghcr.io/llm-d/llm-d-xpu:v0.8.0"}}),
            )
        ]
    )

    assert _execution_accelerator_profile(execution) == "intel-xpu"


@pytest.mark.asyncio
async def test_benchmark_storage_uses_cluster_default(monkeypatch):
    async def fake_kubectl(_environment, *arguments, stdin=None):
        assert arguments == ("get", "storageclass", "-o", "json")
        assert stdin is None
        return {
            "items": [
                {"metadata": {"name": "shared", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}}
            ]
        }

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    assert await _prepare_benchmark_storage("run", "namespace", {}, None) == ("shared", None)


@pytest.mark.asyncio
async def test_benchmark_storage_falls_back_without_storage_class_or_target_node(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append((arguments, stdin))
        return {"items": []} if arguments[0] == "get" else {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    monkeypatch.delenv("PRISM_K8S_TARGET_NODE", raising=False)

    storage_class, volume = await _prepare_benchmark_storage(
        "00000000-0000-4000-8000-000000000002", "deployment-namespace", {}, None
    )

    manifest = json.loads(calls[1][1])
    persistent_volume = manifest["items"][1]
    assert storage_class == "prism-benchmark-local"
    assert volume == "prism-benchmark-00000000-0000-4000-8000-000000000002"
    assert "nodeAffinity" not in persistent_volume["spec"]


@pytest.mark.asyncio
async def test_benchmark_storage_creates_prebound_target_node_volume(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append((arguments, stdin))
        return {"items": []} if arguments[0] == "get" else {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "smc-18")

    storage_class, volume = await _prepare_benchmark_storage(
        "00000000-0000-4000-8000-000000000003", "deployment-namespace", {}, None
    )

    manifest = json.loads(calls[1][1])
    persistent_volume = manifest["items"][1]
    assert storage_class == "prism-benchmark-local"
    assert volume == "prism-benchmark-00000000-0000-4000-8000-000000000003"
    assert persistent_volume["spec"]["claimRef"] == {"namespace": "deployment-namespace", "name": "workload-pvc"}
    assert persistent_volume["spec"]["accessModes"] == ["ReadWriteOnce", "ReadWriteMany"]
    assert persistent_volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0][
        "values"
    ] == ["smc-18"]


@pytest.mark.asyncio
async def test_benchmark_storage_cleanup_deletes_volume_when_claim_cleanup_fails(monkeypatch):
    calls = []

    async def fake_kubectl(_environment, *arguments, stdin=None):
        calls.append(arguments)
        if arguments[1] == "pvc":
            raise RuntimeError("namespace is gone")
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)

    await evaluate_router._cleanup_benchmark_storage("deleted-namespace", "benchmark-volume", {})

    assert calls[-1][:3] == ("delete", "persistentvolume", "benchmark-volume")


def test_plan_can_select_all_routing_combinations_without_full_guide():
    plan = BenchmarkPlan(
        id="ablation",
        configuration_artifact_id="00000000-0000-4000-8000-000000000001",
        include_configuration=False,
        baseline_types=["router-neutral", "affinity-only", "load-only", "direct-vllm"],
    )
    assert plan.include_configuration is False
    assert len(plan.baseline_types) == 4


def test_plan_rejects_empty_selection_and_missing_same_pod_candidate():
    for extra in ({"include_baseline": False}, {"baseline_types": ["kubernetes-service"]}):
        with pytest.raises(ValidationError):
            BenchmarkPlan(
                id="empty",
                configuration_artifact_id="00000000-0000-4000-8000-000000000001",
                include_configuration=False,
                **extra,
            )


@pytest.mark.parametrize("value", [0, -1, 513, 1.5, True, "32", None])
def test_harness_memory_rejects_invalid_budgets(value):
    from llm_d_lens.evaluation.models import BenchmarkSpec

    for model, kwargs in [
        (BenchmarkSpec, {}),
        (evaluate_router.EvaluateRunRequest, {"deployment_execution_id": "exec"}),
    ]:
        with pytest.raises(ValidationError):
            model(**kwargs, harness_memory_gib=value)


def test_harness_memory_defaults_and_explicit_budget():
    from llm_d_lens.evaluation.models import BenchmarkSpec

    assert BenchmarkSpec().harness_memory_gib == 8
    assert evaluate_router.EvaluateRunRequest(deployment_execution_id="exec").harness_memory_gib == 8
    assert BenchmarkSpec(harness_memory_gib=64).model_dump()["harness_memory_gib"] == 64


@pytest.mark.parametrize("separate_limit", [False, True])
def test_harness_budget_overrides_xpu_request_and_limit(tmp_path, separate_limit):
    import yaml
    from jinja2 import Template

    # Both generations of the upstream Pod template must yield a valid request
    # <= limit, including when the XPU overlay explicitly sets memoryLimit=8Gi.
    limit = (
        "harness.resources.memoryLimit | default(harness.resources.memory, true)"
        if separate_limit
        else "harness.resources.memory"
    )
    source = (
        "resources:\n  requests:\n    memory: {{ harness.resources.memory }}\n  limits:\n    memory: {{ "
        + limit
        + " }}\n"
    )
    template = tmp_path / "config/templates/jinja/20_harness_pod.yaml.j2"
    template.parent.mkdir(parents=True)
    template.write_text(source)
    values = {"harness": {"resources": {"memory": "8Gi", "memoryLimit": "8Gi"}}}
    for override in evaluate_router._harness_resource_overrides({"harness_memory_gib": 64}, tmp_path):
        key, value = override.split("=", 1)
        values["harness"]["resources"][key.split(".")[-1]] = value
    rendered = yaml.safe_load(Template(source).render(**values))["resources"]
    assert rendered["requests"]["memory"] == "64Gi"
    assert rendered["limits"]["memory"] == "64Gi"


@pytest.mark.parametrize(
    "message",
    [
        "modelserver (terminated: OOMKilled, exit_code=137)",
        "inference-perf-test/harness (terminated: Error, exit_code=137)",
        "Connection refused",
    ],
)
def test_harness_oom_diagnostic_does_not_misclassify_other_failures(message):
    assert evaluate_router._benchmark_failure_message(message, {}) == message


@pytest.mark.parametrize("profile", ["nvidia", "intel-xpu", "custom.device"])
def test_benchmark_request_accepts_profile_identifiers(profile):
    from llm_d_lens.evaluation.models import EvaluateRunRequest

    request = EvaluateRunRequest(
        deployment_execution_id="00000000-0000-4000-8000-000000000001",
        accelerator_profile=profile,
    )
    assert request.accelerator_profile == profile


@pytest.mark.asyncio
async def test_benchmark_storage_non_admin_403_falls_back_to_prometheus_and_namespace_pvcs(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint

    async def forbidden_storageclass(_environment, *arguments, stdin=None):
        del stdin
        if arguments[:2] == ("get", "storageclass"):
            raise RuntimeError(
                "Error from server (Forbidden): storageclasses.storage.k8s.io is forbidden: "
                'User "dev" cannot list resource "storageclasses" at the cluster scope'
            )
        if arguments[:2] == ("get", "pvc"):
            return {
                "items": [
                    {"spec": {"storageClassName": "bad,harness.image=evil"}},
                    {"spec": {"storageClassName": "ns-nfs-rwx"}},
                ]
            }
        raise AssertionError(f"Unexpected cluster mutation: {arguments}")

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", forbidden_storageclass)

    # 1. Prometheus HTTP API discovers default StorageClass via kube_storageclass_annotations (ignoring invalid names)
    monkeypatch.setattr(
        evaluate_router,
        "resolve_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.15:30090", source="explicit_url")),
    )

    def prom_sc_handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if 'annotation_storageclass_kubernetes_io_is_default_class="true"' in query:
            rows = [
                {"metric": {"storageclass": "Upper_Invalid,set=1"}, "value": [1, "1"]},
                {"metric": {"storageclass": "prom-default-rwx"}, "value": [1, "1"]},
            ]
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "data": {"result": rows}})

    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            transport=httpx.MockTransport(prom_sc_handler),
            base_url=endpoint.base_url,
            timeout=timeout,
        ),
    )

    assert await _prepare_benchmark_storage("run-na", "ns-bench", {}, None, cluster_id="c-na") == (
        "prom-default-rwx",
        None,
    )

    # 2. When Prometheus has no storageclass series, falls back to namespace PVC storageClassName
    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(200, json={"status": "success", "data": {"result": []}})
            ),
            base_url=endpoint.base_url,
            timeout=timeout,
        ),
    )
    assert await _prepare_benchmark_storage("run-na", "ns-bench", {}, None, cluster_id="c-na") == (
        "ns-nfs-rwx",
        None,
    )

    # 3. When both Prometheus and namespace PVCs are unavailable/empty, relies on cluster DefaultStorageClass ("", None)
    async def all_forbidden(_environment, *arguments, stdin=None):
        del arguments, stdin
        raise RuntimeError("Error from server (Forbidden): HTTP 403")

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", all_forbidden)
    monkeypatch.setattr(evaluate_router, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    assert await _prepare_benchmark_storage("run-na", "ns-bench", {}, None, cluster_id="c-na") == ("", None)


def _create_synthetic_benchmark_tree(root, *, version: str) -> None:
    (root / "llmdbenchmark" / "interface").mkdir(parents=True, exist_ok=True)
    (root / "llmdbenchmark" / "run" / "steps").mkdir(parents=True, exist_ok=True)
    (root / "config" / "templates" / "jinja").mkdir(parents=True, exist_ok=True)
    (root / "config" / "templates" / "values" / "overlays").mkdir(parents=True, exist_ok=True)
    (root / "config" / "templates" / "values" / "defaults.yaml").write_text(
        "storage:\n"
        "  workloadPvc:\n"
        "    name: workload-pvc\n"
        "    size: 20Gi\n"
        "    accessModes:\n"
        "      - ReadWriteMany\n"
        "images:\n"
        "  benchmark:\n"
        "    repository: ghcr.io/llm-d/llm-d-benchmark\n"
        "    tag: v1.0.0\n"
        "    pullPolicy: IfNotPresent\n"
        "dataAccess:\n"
        "  rsyncPort: 20873\n"
        "labels:\n"
        "  app: llm-d-benchmark-harness\n",
        encoding="utf-8",
    )
    (root / "llmdbenchmark" / "interface" / "run.py").write_text('parser.add_argument("--step")\n', encoding="utf-8")
    if version == "v1.0.0":
        (root / "llmdbenchmark" / "cli.py").write_text(
            '"--non-admin" "--no-pvc" "--no-prism"\n',
            encoding="utf-8",
        )
        (root / "config" / "templates" / "jinja" / "20_harness_pod.yaml.j2").write_text(
            "{% if no_pvc | default(false) %}{% endif %}\n",
            encoding="utf-8",
        )
        (root / "llmdbenchmark" / "run" / "steps" / "step_02b_deploy_prism.py").write_text("", encoding="utf-8")
    else:
        (root / "llmdbenchmark" / "cli.py").write_text('"--non-admin"\n', encoding="utf-8")
        (root / "config" / "templates" / "jinja" / "20_harness_pod.yaml.j2").write_text(
            "volumes:\n  - name: requests\n",
            encoding="utf-8",
        )


def test_benchmark_capability_helpers_detect_supported_flags(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert evaluate_router._benchmark_supports_no_pvc(bare) is False
    assert evaluate_router._benchmark_supports_no_prism(bare) is False
    assert evaluate_router._benchmark_supports_non_admin(bare) is False
    assert evaluate_router._benchmark_supports_step_filter(bare) is False

    v080 = tmp_path / "v080"
    _create_synthetic_benchmark_tree(v080, version="v0.8.0")
    assert evaluate_router._benchmark_supports_no_pvc(v080) is False
    assert evaluate_router._benchmark_supports_no_prism(v080) is False
    assert evaluate_router._benchmark_supports_non_admin(v080) is True
    assert evaluate_router._benchmark_supports_step_filter(v080) is True

    v100 = tmp_path / "v100"
    _create_synthetic_benchmark_tree(v100, version="v1.0.0")
    assert evaluate_router._benchmark_supports_no_pvc(v100) is True
    assert evaluate_router._benchmark_supports_no_prism(v100) is True
    assert evaluate_router._benchmark_supports_non_admin(v100) is True
    assert evaluate_router._benchmark_supports_step_filter(v100) is True


@pytest.mark.asyncio
async def test_can_get_namespace_handles_allowed_forbidden_and_other_errors(monkeypatch):
    async def allowed(_env, *args, stdin=None):
        del args, stdin
        return {"metadata": {"name": "ns"}}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", allowed)
    assert await evaluate_router._can_get_namespace("ns", {}) is True

    async def forbidden(_env, *args, stdin=None):
        del args, stdin
        raise RuntimeError('Error from server (Forbidden): namespaces "ns" is forbidden')

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", forbidden)
    assert await evaluate_router._can_get_namespace("ns", {}) is False

    async def other_error(_env, *args, stdin=None):
        del args, stdin
        raise RuntimeError("Error from server (NotFound): namespaces not found")

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", other_error)
    assert await evaluate_router._can_get_namespace("ns", {}) is True


def test_cluster_resource_overrides_covers_all_hardware_modes_and_custom_profile():
    from llm_d_lens.hardware.models import HardwareProfile
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key

    expected_clears = {
        "vllmCommon.networkResource=",
        "vllmCommon.networkNr=",
        "affinity.nodeSelector={}",
        "decode.acceleratorType.labelValue=",
        "prefill.acceleratorType.labelValue=",
        "standalone.acceleratorType.labelValue=",
    }

    def assert_valid_overrides(overrides: list[str], expected: set[str]) -> dict[str, str]:
        assert expected_clears.issubset(overrides)
        assert expected.issubset(overrides)
        parsed = dict(item.split("=", 1) for item in overrides)
        assert "auto" not in parsed.values()
        return parsed

    # 1. Intel XPU DRA
    xpu_profile = resolve_by_accelerator_key("intel-xpu")
    xpu_dra_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps({"officialGuide": {"renderedManifest": "deviceClassName: gpu.intel.com\n"}})
            )
        ]
    )
    xpu_dra_req = evaluate_router._execution_resource_request(xpu_dra_exec, xpu_profile)
    assert_valid_overrides(
        evaluate_router._cluster_resource_overrides("intel-xpu", xpu_profile, resource_request=xpu_dra_req),
        {"accelerator.profile=intel-xpu", "accelerator.resource=", "accelerator.draDriver=gpu.intel.com"},
    )

    # 2. Intel XPU device-plugin
    xpu_plugin_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps(
                    {"officialGuide": {"renderedManifest": "resources:\n  requests:\n    gpu.intel.com/xe: 1\n"}}
                )
            )
        ]
    )
    xpu_plugin_req = evaluate_router._execution_resource_request(xpu_plugin_exec, xpu_profile)
    parsed_xpu_plugin = assert_valid_overrides(
        evaluate_router._cluster_resource_overrides("intel-xpu", xpu_profile, resource_request=xpu_plugin_req),
        {"accelerator.profile=intel-xpu", "accelerator.resource=gpu.intel.com/xe"},
    )
    assert "accelerator.draDriver" not in parsed_xpu_plugin

    # 3. NVIDIA device-plugin
    nvidia_profile = resolve_by_accelerator_key("nvidia")
    nvidia_plugin_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps(
                    {"officialGuide": {"renderedManifest": "resources:\n  limits:\n    nvidia.com/gpu: 1\n"}}
                )
            )
        ]
    )
    nvidia_plugin_req = evaluate_router._execution_resource_request(nvidia_plugin_exec, nvidia_profile)
    parsed_nv_plugin = assert_valid_overrides(
        evaluate_router._cluster_resource_overrides("nvidia", nvidia_profile, resource_request=nvidia_plugin_req),
        {"accelerator.profile=nvidia", "accelerator.resource=nvidia.com/gpu"},
    )
    assert "accelerator.draDriver" not in parsed_nv_plugin

    # 4. NVIDIA DRA
    nvidia_dra_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps({"officialGuide": {"renderedManifest": "deviceClassName: gpu.nvidia.com\n"}})
            )
        ]
    )
    nvidia_dra_req = evaluate_router._execution_resource_request(nvidia_dra_exec, nvidia_profile)
    assert_valid_overrides(
        evaluate_router._cluster_resource_overrides("nvidia", nvidia_profile, resource_request=nvidia_dra_req),
        {"accelerator.profile=nvidia", "accelerator.resource=", "accelerator.draDriver=gpu.nvidia.com"},
    )

    # 5. Synthetic custom profile (proves zero vendor branches)
    custom_profile = HardwareProfile.from_dict(
        {
            "id": "custom-accel",
            "display_name": "Custom Accel",
            "vendor": "example",
            "accelerator_keys": ["custom-accel"],
            "benchmark_profile": "custom-accel",
            "upstream_variant": "custom",
            "request_model": "extended-resource",
            "resource_prefixes": ["vendor.example/"],
            "access_modes": ["plugin"],
            "driver": {"installer": "helm", "modes": {"plugin": {}}},
            "presence": {"node_label_selector": {"vendor.example/present": "true"}},
            "deployment": {
                "resource_name": "vendor.example/accel",
                "modes": {
                    "plugin": {
                        "request_model": "extended-resource",
                        "resource_names": ["vendor.example/accel"],
                    }
                },
            },
        }
    )
    custom_req = evaluate_router._execution_resource_request(
        SimpleNamespace(configuration_artifacts=[]), custom_profile
    )
    assert_valid_overrides(
        evaluate_router._cluster_resource_overrides("custom-accel", custom_profile, resource_request=custom_req),
        {"accelerator.profile=custom-accel", "accelerator.resource=vendor.example/accel"},
    )

    # 6. Unknown / CPU (None)
    assert evaluate_router._execution_resource_request(SimpleNamespace(configuration_artifacts=[]), None) is None
    parsed_cpu = assert_valid_overrides(
        evaluate_router._cluster_resource_overrides(None, None),
        {"accelerator.profile=", "accelerator.resource="},
    )
    assert "accelerator.draDriver" not in parsed_cpu


@pytest.mark.asyncio
async def test_discover_storage_class_via_prometheus_filters_no_provisioner_and_prefers_immediate(monkeypatch):
    import httpx

    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint

    monkeypatch.setattr(
        evaluate_router,
        "resolve_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://10.0.0.15:30090", source="explicit_url")),
    )

    def make_client(info_rows, default_rows=None):
        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("query", "")
            if query == "kube_storageclass_info":
                rows = info_rows
            elif default_rows and 'annotation_storageclass_kubernetes_io_is_default_class="true"' in query:
                rows = default_rows
            else:
                rows = []
            return httpx.Response(200, json={"status": "success", "data": {"result": rows}})

        return lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=endpoint.base_url,
            timeout=timeout,
        )

    # 1. Skips no-provisioner SCs (even if annotated default) and prefers Immediate over WaitForFirstConsumer
    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        make_client(
            info_rows=[
                {
                    "metric": {
                        "storageclass": "monitoring-local",
                        "provisioner": "kubernetes.io/no-provisioner",
                        "volume_binding_mode": "WaitForFirstConsumer",
                    },
                    "value": [1, "1"],
                },
                {
                    "metric": {
                        "storageclass": "local-path",
                        "provisioner": "rancher.io/local-path",
                        "volume_binding_mode": "WaitForFirstConsumer",
                    },
                    "value": [1, "1"],
                },
                {
                    "metric": {
                        "storageclass": "fast-nfs",
                        "provisioner": "nfs.csi.k8s.io",
                        "volume_binding_mode": "Immediate",
                    },
                    "value": [1, "1"],
                },
            ],
            default_rows=[{"metric": {"storageclass": "monitoring-local"}, "value": [1, "1"]}],
        ),
    )
    assert await evaluate_router._discover_storage_class_via_prometheus("c-1", "ns-1") == "fast-nfs"

    # 2. Falls back to WaitForFirstConsumer dynamic SC when no Immediate dynamic SC exists
    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        make_client(
            info_rows=[
                {
                    "metric": {
                        "storageclass": "monitoring-local",
                        "provisioner": "kubernetes.io/no-provisioner",
                        "volume_binding_mode": "Immediate",
                    },
                    "value": [1, "1"],
                },
                {
                    "metric": {
                        "storageclass": "local-path",
                        "provisioner": "rancher.io/local-path",
                        "volume_binding_mode": "WaitForFirstConsumer",
                    },
                    "value": [1, "1"],
                },
            ]
        ),
    )
    assert await evaluate_router._discover_storage_class_via_prometheus("c-1", "ns-1") == "local-path"

    # 3. Returns None when only kubernetes.io/no-provisioner SCs exist
    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        make_client(
            info_rows=[
                {
                    "metric": {
                        "storageclass": "monitoring-local",
                        "provisioner": "kubernetes.io/no-provisioner",
                        "volume_binding_mode": "WaitForFirstConsumer",
                    },
                    "value": [1, "1"],
                }
            ]
        ),
    )
    assert await evaluate_router._discover_storage_class_via_prometheus("c-1", "ns-1") is None


@pytest.mark.asyncio
async def test_prepare_non_admin_harness_pvc_creates_pvc_and_pod_before_waiting(monkeypatch, tmp_path):
    root = tmp_path / "bench"
    _create_synthetic_benchmark_tree(root, version="v1.0.0")
    calls = []
    pod_polls = 0

    async def fake_kubectl(_env, *arguments, stdin=None):
        nonlocal pod_polls
        calls.append((arguments, json.loads(stdin.decode()) if stdin else None))
        if arguments[:3] == ("get", "pvc", "workload-pvc"):
            raise RuntimeError('Error from server (NotFound): persistentvolumeclaims "workload-pvc" not found')
        if arguments[:3] == ("get", "pod", "access-to-harness-data-workload-pvc"):
            pod_polls += 1
            if pod_polls == 1:
                return {"status": {"phase": "Pending", "conditions": []}}
            return {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)

    await evaluate_router._prepare_non_admin_harness_pvc(
        "ns-bench",
        "local-path",
        root,
        {},
        wait_timeout=5,
    )

    assert calls[0][0][:3] == ("get", "pvc", "workload-pvc")
    # PVC created with ReadWriteOnce forced for local-path
    assert calls[1][0][:2] == ("apply", "--filename=-")
    pvc_manifest = calls[1][1]
    assert pvc_manifest["kind"] == "PersistentVolumeClaim"
    assert pvc_manifest["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc_manifest["spec"]["storageClassName"] == "local-path"
    # Data access Pod created BEFORE polling readiness
    assert calls[2][0][:2] == ("apply", "--filename=-")
    pod_manifest = calls[2][1]
    assert pod_manifest["kind"] == "Pod"
    assert pod_manifest["metadata"]["name"] == "access-to-harness-data-workload-pvc"
    assert calls[3][0][:3] == ("get", "pod", "access-to-harness-data-workload-pvc")
    assert pod_polls == 2


@pytest.mark.asyncio
async def test_prepare_non_admin_harness_pvc_reuses_bound_pvc_and_recreates_immutable_pod(monkeypatch, tmp_path):
    root = tmp_path / "bench"
    _create_synthetic_benchmark_tree(root, version="v1.0.0")
    calls = []
    pod_apply_attempts = 0

    async def fake_kubectl(_env, *arguments, stdin=None):
        nonlocal pod_apply_attempts
        payload = json.loads(stdin.decode()) if stdin else None
        calls.append((arguments, payload))
        if arguments[:3] == ("get", "pvc", "workload-pvc"):
            return {"metadata": {"name": "workload-pvc"}, "status": {"phase": "Bound"}}
        if arguments[0] == "apply" and payload and payload.get("kind") == "Pod":
            pod_apply_attempts += 1
            if pod_apply_attempts == 1:
                raise RuntimeError("Pod updates may not change fields other than spec.containers[*].image")
            return {}
        if arguments[:3] == ("get", "pod", "access-to-harness-data-workload-pvc"):
            return {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)

    await evaluate_router._prepare_non_admin_harness_pvc("ns-bench", "auto", root, {}, wait_timeout=5)

    # Bound PVC was reused (no PVC apply)
    assert not any(payload and payload.get("kind") == "PersistentVolumeClaim" for _, payload in calls)
    # Immutable pod triggered delete + recreate before polling
    assert [c[0][:3] for c in calls] == [
        ("get", "pvc", "workload-pvc"),
        ("apply", "--filename=-", "--output=json"),
        ("delete", "pod", "access-to-harness-data-workload-pvc"),
        ("apply", "--filename=-", "--output=json"),
        ("get", "pod", "access-to-harness-data-workload-pvc"),
    ]


@pytest.mark.asyncio
async def test_cleanup_benchmark_storage_cleans_non_admin_pvc_resources_without_pv(monkeypatch):
    calls = []

    async def fake_kubectl(_env, *arguments, stdin=None):
        del stdin
        calls.append(arguments)
        if arguments[1] == "pod":
            raise RuntimeError("pod delete hiccup")
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)

    await evaluate_router._cleanup_benchmark_storage("ns-bench", None, {}, cleanup_pvc_resources=True)

    assert [c[:3] for c in calls] == [
        ("delete", "pod", "access-to-harness-data-workload-pvc"),
        ("delete", "pvc", "workload-pvc"),
    ]
    assert all("--ignore-not-found=true" in c for c in calls)


def _patch_execute_owned_dependencies(monkeypatch, tmp_path, run, benchmark_root):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(evaluate_router, "_get", lambda _kind, _id: run)
    monkeypatch.setattr(evaluate_router, "_save", lambda _record: None)
    monkeypatch.setattr(evaluate_router, "_results_root", tmp_path)
    execution = SimpleNamespace(
        status=evaluate_router.DeploymentStatus.READY,
        endpoint=SimpleNamespace(url="http://endpoint.local"),
        namespace="ns-bench",
        provenance={},
        configuration_artifacts=[],
    )
    monkeypatch.setattr(evaluate_router, "_store", SimpleNamespace(get_execution=lambda _id: execution))
    monkeypatch.setattr(evaluate_router, "_execution_model", lambda _execution: "Qwen/Qwen3-0.6B")
    monkeypatch.setattr(evaluate_router, "_execution_accelerator_profile", lambda _execution, _profile: None)
    monkeypatch.setattr(evaluate_router, "_harness_proxy_environment", lambda _run, **_kwargs: {})
    monkeypatch.setattr(evaluate_router, "_harness_proxy_override", lambda _env: None)
    monkeypatch.setattr(evaluate_router, "_install_helm_proxy_wrapper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evaluate_router, "wait_for_deployment_metrics", AsyncMock(return_value=True))
    monkeypatch.setattr(evaluate_router, "capture_benchmark_context", AsyncMock(return_value=None))
    monkeypatch.setattr(evaluate_router, "verify_benchmark_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        evaluate_router,
        "collect_benchmark_observability",
        AsyncMock(
            return_value={
                "status": "unavailable",
                "reason": "No test telemetry",
                "window": {},
                "summary": {},
                "series": [],
            }
        ),
    )
    monkeypatch.setattr(
        evaluate_router.KVTraceCollector, "discover", AsyncMock(side_effect=ValueError("No test probe"))
    )

    async def fake_prepare_runtime(_source):
        return "llmdbenchmark", benchmark_root

    monkeypatch.setattr(evaluate_router, "_prepare_benchmark_runtime", fake_prepare_runtime)

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_stream(process, *_args, **_kwargs):
        return await process.communicate()

    monkeypatch.setattr(evaluate_router, "_stream_benchmark_process", fake_stream)
    commands = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        del kwargs
        commands.append(list(args))
        return _FakeProcess()

    monkeypatch.setattr(evaluate_router.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return commands


@pytest.mark.asyncio
async def test_execute_owned_uses_no_pvc_and_step_filter_on_v1_checkout(monkeypatch, tmp_path):
    v100 = tmp_path / "v100"
    _create_synthetic_benchmark_tree(v100, version="v1.0.0")
    run = {
        "id": "run-v1-no-pvc",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
    }
    commands = _patch_execute_owned_dependencies(monkeypatch, tmp_path, run, v100)

    storage_called = False

    async def unexpected_storage(*_args, **_kwargs):
        nonlocal storage_called
        storage_called = True
        return "standard", None

    cleanup_calls = []

    async def fake_cleanup(*args, **kwargs):
        cleanup_calls.append((args, kwargs))

    monkeypatch.setattr(evaluate_router, "_prepare_benchmark_storage", unexpected_storage)
    monkeypatch.setattr(evaluate_router, "_cleanup_benchmark_storage", fake_cleanup)

    await evaluate_router._execute(run["id"])

    assert run["status"] == "succeeded"
    assert storage_called is False
    cmd = commands[0]
    assert "--non-admin" in cmd
    assert "--no-pvc" in cmd
    assert "--no-prism" in cmd
    step_idx = cmd.index("--step")
    assert cmd[step_idx + 1] == "0,1,3-20"
    assert not any("storage.workloadPvc.storageClassName=" in item for item in cmd)
    assert "accelerator.profile=" in cmd
    assert "affinity.nodeSelector={}" in cmd
    assert "--no-pvc" in run["command"]
    assert "--no-prism" in run["command"]
    assert "--step" in run["command"]
    assert cleanup_calls and cleanup_calls[0][1] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v1.0.0", "v0.8.0"])
async def test_execute_owned_non_admin_pvc_fallback_runs_pre_step_and_cleans_up(monkeypatch, tmp_path, version):
    from unittest.mock import AsyncMock

    root = tmp_path / f"bench-{version}"
    _create_synthetic_benchmark_tree(root, version=version)
    run = {
        "id": f"run-pvc-{version}",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        **({"storage_class_name": "custom-rwx"} if version == "v1.0.0" else {}),
    }
    commands = _patch_execute_owned_dependencies(monkeypatch, tmp_path, run, root)

    storage_calls = []

    async def fake_storage(*args, **_kwargs):
        storage_calls.append(args)
        return args[3] or "local-path", None

    pvc_prep_calls = []

    async def fake_non_admin_pvc(*args, **kwargs):
        pvc_prep_calls.append((args, kwargs))

    cleanup_calls = []

    async def fake_cleanup(*args, **kwargs):
        cleanup_calls.append((args, kwargs))

    monkeypatch.setattr(evaluate_router, "_prepare_benchmark_storage", fake_storage)
    monkeypatch.setattr(evaluate_router, "_can_get_namespace", AsyncMock(return_value=False))
    monkeypatch.setattr(evaluate_router, "_prepare_non_admin_harness_pvc", fake_non_admin_pvc)
    monkeypatch.setattr(evaluate_router, "_cleanup_benchmark_storage", fake_cleanup)

    await evaluate_router._execute(run["id"])

    assert run["status"] == "succeeded"
    assert len(storage_calls) == 1
    assert len(pvc_prep_calls) == 1
    cmd = commands[0]
    assert "--non-admin" in cmd
    assert "--no-pvc" not in cmd
    assert ("--no-prism" in cmd) is (version == "v1.0.0")
    step_idx = cmd.index("--step")
    assert cmd[step_idx + 1] == "0,1,3-20"
    expected_sc = "custom-rwx" if version == "v1.0.0" else "local-path"
    assert f"storage.workloadPvc.storageClassName={expected_sc}" in cmd
    assert "accelerator.profile=" in cmd
    assert "affinity.nodeSelector={}" in cmd
    assert cleanup_calls == [(("ns-bench", None, cleanup_calls[0][0][2]), {"cleanup_pvc_resources": True})]


@pytest.mark.asyncio
async def test_cluster_resource_overrides_and_storage_reject_injected_identifiers():
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key

    xpu_profile = resolve_by_accelerator_key("intel-xpu")
    nvidia_profile = resolve_by_accelerator_key("nvidia")

    # Crafted comma-injected device_class in hardware_request is rejected by _execution_resource_request
    # and falls back to the profile's trusted deployment_mode.
    injected_dra_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps(
                    {
                        "hardware_request": {
                            "access_mode": "dra",
                            "request_model": "dra",
                            "device_class": "gpu.intel.com,images.benchmark.repository=attacker.io/evil",
                        }
                    }
                )
            )
        ]
    )
    req = evaluate_router._execution_resource_request(injected_dra_exec, xpu_profile)
    assert req is not None
    assert req.get("device_class") == "gpu.intel.com"

    # Crafted comma-injected extended resource key in renderedManifest is rejected by _execution_resource_request.
    injected_res_exec = SimpleNamespace(
        configuration_artifacts=[
            SimpleNamespace(
                content=json.dumps(
                    {
                        "officialGuide": {
                            "renderedManifest": (
                                "resources:\n  requests:\n"
                                '    "nvidia.com/gpu,images.benchmark.repository=attacker.io/evil": 1\n'
                            )
                        }
                    }
                )
            )
        ]
    )
    res_req = evaluate_router._execution_resource_request(injected_res_exec, nvidia_profile)
    assert res_req is not None
    assert "attacker.io" not in str(res_req.get("resource_name") or "")

    # Direct calls to _cluster_resource_overrides reject malformed identifiers.
    with pytest.raises(ValueError, match="accelerator_profile"):
        evaluate_router._cluster_resource_overrides("intel-xpu,harness.image=evil", xpu_profile)
    with pytest.raises(ValueError, match="device_class"):
        evaluate_router._cluster_resource_overrides(
            "intel-xpu",
            xpu_profile,
            resource_request={"request_model": "dra", "device_class": "gpu.intel.com,set=1"},
        )
    with pytest.raises(ValueError, match="device_class"):
        evaluate_router._cluster_resource_overrides(
            "intel-xpu",
            xpu_profile,
            resource_request={"request_model": "dra", "device_class": "other.vendor.io"},
        )
    with pytest.raises(ValueError, match="resource name"):
        evaluate_router._cluster_resource_overrides(
            "nvidia",
            nvidia_profile,
            resource_request={"request_model": "extended-resource", "resource_name": "nvidia.com/gpu,set=1"},
        )

    # Configured storage class is validated before returning.
    with pytest.raises(ValueError, match="storage class"):
        await _prepare_benchmark_storage("run-inj", "ns-bench", {}, "bad,harness.image=evil")


@pytest.mark.asyncio
async def test_non_admin_pvc_validates_namespace_and_execute_redacts_error_credentials(monkeypatch, tmp_path):
    v100 = tmp_path / "v100"
    _create_synthetic_benchmark_tree(v100, version="v1.0.0")

    with pytest.raises(ValueError, match="namespace"):
        await evaluate_router._prepare_non_admin_harness_pvc("--bad-ns", "local-path", v100, {})

    kubectl_calls = []

    async def fake_kubectl(_env, *args, stdin=None):
        del stdin
        kubectl_calls.append(args)
        return {}

    monkeypatch.setattr(evaluate_router, "_benchmark_kubectl_json", fake_kubectl)
    await evaluate_router._cleanup_benchmark_storage("--bad-ns", None, {}, cleanup_pvc_resources=True)
    assert kubectl_calls == []

    run = {
        "id": "run-redact",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "cluster_session_id": "sess-1",
    }
    _patch_execute_owned_dependencies(monkeypatch, tmp_path, run, v100)

    async def fail_with_secrets(*_args, **_kwargs):
        raise RuntimeError(
            "kubectl failed with Bearer secret-bearer-123 HF_TOKEN=hf_AbCdEfGhIjKlMnOpQrStUvWxYz "
            'OPENAI_API_KEY=sk-secret-value config file "/tmp/cluster.kubeconfig"'
        )

    monkeypatch.setattr(evaluate_router, "capture_benchmark_context", fail_with_secrets)
    monkeypatch.setattr(evaluate_router, "_kubernetes_resource_snapshot", fail_with_secrets)
    monkeypatch.setattr(
        evaluate_router,
        "_prepare_non_admin_harness_pvc",
        AsyncMock(side_effect=fail_with_secrets),
    )

    # Trigger failure inside _execute_owned (before observability_started_at, and also test snapshot error path)
    await evaluate_router._execute(run["id"])
    assert run["status"] == "failed"
    assert "secret-bearer-123" not in run["error"]
    assert "hf_AbCdEfGhIjKlMnOpQrStUvWxYz" not in run["error"]
    assert "sk-secret-value" not in run["error"]
    assert "/tmp/cluster.kubeconfig" not in run["error"]
    assert "[REDACTED]" in run["error"]

    # Also verify resource_snapshot_error redaction when observability_started_at is set
    run2 = {
        "id": "run-redact-snap",
        "deployment_execution_id": "exec-1",
        "specification_file": "guides/optimized-baseline",
        "harness": "inference-perf",
        "workload": "sanity_random.yaml",
        "parallelism": 1,
        "wait_timeout_seconds": 60,
        "cluster_session_id": "sess-1",
    }
    _patch_execute_owned_dependencies(monkeypatch, tmp_path, run2, v100)
    monkeypatch.setattr(
        evaluate_router.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=fail_with_secrets),
    )
    await evaluate_router._execute(run2["id"])
    assert run2["status"] == "failed"
    assert "hf_AbCdEfGhIjKlMnOpQrStUvWxYz" not in run2["resource_snapshot_error"]
    assert "sk-secret-value" not in run2["resource_snapshot_error"]
    assert "secret-bearer-123" not in run2["resource_snapshot_error"]
    assert "[REDACTED]" in run2["resource_snapshot_error"]


@pytest.mark.asyncio
async def test_eval_k8s_helpers_short_circuit_without_real_cluster_target(monkeypatch, tmp_path):
    import os

    from llm_d_lens.infrastructure.kubernetes import commands as k8s_commands
    from llm_d_lens.infrastructure.process import CommandResult

    sdk_calls = []
    spawn_calls = []

    async def fake_sdk(argv, **_kwargs):
        sdk_calls.append(argv)
        return CommandResult(tuple(argv), 0, "{}", "")

    async def fake_spawn(argv, **_kwargs):
        spawn_calls.append(argv)
        raise AssertionError("spawn should not be called in this branch")

    monkeypatch.setattr(k8s_commands, "execute_sdk_command", fake_sdk)
    monkeypatch.setattr(k8s_commands, "sdk_enabled", lambda: True)
    monkeypatch.setattr(k8s_commands, "sdk_writes_enabled", lambda: True)
    monkeypatch.setattr(evaluate_router, "_process_spawn", fake_spawn)

    # Empty env and non-existent KUBECONFIG paths short-circuit cleanly.
    for env in (
        {},
        {"KUBECONFIG": "/does/not/exist.yaml"},
        {"KUBECONFIG": f"/missing/a.yaml{os.pathsep}/missing/b.yaml"},
    ):
        assert await evaluate_router._eval_k8s_json(["get", "services", "-n", "ns"], env) is None
        assert await evaluate_router._eval_k8s_apply({"kind": "ConfigMap"}, env) is False
        assert await evaluate_router._eval_k8s_delete("configmap", "cm-a", "ns", env) is False
    assert sdk_calls == []
    assert spawn_calls == []

    # Multi-entry KUBECONFIG with at least one existing file enables SDK calls.
    kc_file = tmp_path / "cluster.yaml"
    kc_file.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    multi_env = {"KUBECONFIG": f"/missing/a.yaml{os.pathsep}{kc_file}"}
    assert await evaluate_router._eval_k8s_json(["get", "services", "-n", "ns"], multi_env) == {}
    assert await evaluate_router._eval_k8s_delete("configmap", "cm-a", "ns", multi_env) is True
    assert len(sdk_calls) == 2

    # KUBERNETES_SERVICE_HOST also enables K8s operations; SDK failures never fall back to CLI.
    async def failing_sdk(argv, **_kwargs):
        sdk_calls.append(argv)
        return CommandResult(tuple(argv), 1, "", "Forbidden")

    monkeypatch.setattr(k8s_commands, "execute_sdk_command", failing_sdk)
    assert (
        await evaluate_router._eval_k8s_json(["get", "pods", "-n", "ns"], {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
        is None
    )
    assert (
        await evaluate_router._eval_k8s_delete("configmap", "cm-a", "ns", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
        is False
    )
    assert spawn_calls == []

    # _eval_k8s_apply (which falls through to _process_spawn) must never re-inject stripped os.environ proxies.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:911")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:911")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    spawn_envs = []

    class _FakeApplyProc:
        returncode = 0

        async def communicate(self, _stdin=None):
            return b"{}", b""

        async def wait(self):
            return 0

    async def capture_spawn(argv, **kwargs):
        spawn_envs.append((argv, kwargs["env"]))
        return _FakeApplyProc()

    monkeypatch.setattr(k8s_commands, "execute_sdk_command", AsyncMock(return_value=None))
    monkeypatch.setattr(evaluate_router, "_process_spawn", capture_spawn)
    assert await evaluate_router._eval_k8s_apply({"kind": "ConfigMap"}, {"KUBECONFIG": str(kc_file)}) is True
    assert len(spawn_envs) == 1
    assert not any(k.lower() in {"http_proxy", "https_proxy", "all_proxy"} for k in spawn_envs[0][1])


@pytest.mark.asyncio
async def test_resolve_standup_accelerator_model_uses_hardware_profiles_and_prometheus(monkeypatch):
    import httpx

    from llm_d_lens.hardware.models import DeviceMetricSource, HardwareProfile, TelemetryContribution
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key
    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint

    xpu_profile = resolve_by_accelerator_key("intel-xpu")
    nvidia_profile = resolve_by_accelerator_key("nvidia")

    seen_queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        seen_queries.append(query)
        if query == "hw_gpu_info":
            rows = [
                {"metric": {"hw_model": "Other GPU", "node": "other-node"}, "value": [1, "1"]},
                {"metric": {"hw_model": "Intel(R) Arc(TM) Pro B60 Graphics", "node": "smc-22"}, "value": [1, "1"]},
            ]
        elif query == "DCGM_FI_DEV_GPU_UTIL":
            rows = [{"metric": {"modelName": "NVIDIA H100 80GB HBM3", "Hostname": "gpu-node-1"}, "value": [1, "0"]}]
        elif query == 'custom_gpu_metric{job="custom-exp"}':
            rows = [{"metric": {"gpu_model": "Custom-Accel-X1", "node": "c-node"}, "value": [1, "1"]}]
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "data": {"result": rows}})

    monkeypatch.setattr(
        evaluate_router,
        "resolve_prometheus_endpoint",
        AsyncMock(return_value=PrometheusEndpoint(base_url="http://prom.local:9090", source="explicit_url")),
    )
    monkeypatch.setattr(
        evaluate_router,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=endpoint.base_url, timeout=timeout
        ),
    )

    xpu_pods = [
        {"spec": {"nodeName": "smc-22", "containers": [{"name": "modelserver"}]}},
    ]
    assert (
        await evaluate_router._resolve_standup_accelerator_model(
            xpu_profile,
            cluster_id="c-1",
            namespace="ns-1",
            environment={},
            deployments=[],
            pods=xpu_pods,
        )
        == "Intel(R) Arc(TM) Pro B60 Graphics"
    )
    assert (
        await evaluate_router._resolve_standup_accelerator_model(
            nvidia_profile,
            cluster_id="c-1",
            namespace="ns-1",
            environment={},
            deployments=[],
            pods=[{"spec": {"nodeName": "gpu-node-1", "containers": [{"name": "modelserver"}]}}],
        )
        == "NVIDIA H100 80GB HBM3"
    )

    # Verify N1: telemetry fallback queries source.metric + _selector(source.match).
    custom_profile = HardwareProfile(
        id="custom-accel",
        display_name="Custom Accel",
        vendor="custom",
        request_model="extended-resource",
        deployment=xpu_profile.deployment,
        telemetry=TelemetryContribution(
            device_metrics={},
            device_metric_sources={
                "utilization": DeviceMetricSource(metric="custom_gpu_metric", match={"job": "custom-exp"})
            },
            label_schema={"model": "gpu_model", "node": "node"},
        ),
    )
    assert (
        await evaluate_router._resolve_standup_accelerator_model(
            custom_profile,
            cluster_id="c-1",
            namespace="ns-1",
            environment={},
            deployments=[],
            pods=[],
        )
        == "Custom-Accel-X1"
    )
    assert 'custom_gpu_metric{job="custom-exp"}' in seen_queries

    # Without Prometheus or profile -> profile display_name, then neutral "unknown" (never list_node).
    monkeypatch.setattr(evaluate_router, "resolve_prometheus_endpoint", AsyncMock(return_value=None))
    assert (
        await evaluate_router._resolve_standup_accelerator_model(
            xpu_profile, cluster_id="c-1", namespace="ns-1", environment={}, deployments=[], pods=[]
        )
        == "Intel GPU"
    )
    assert (
        await evaluate_router._resolve_standup_accelerator_model(
            None, cluster_id="c-1", namespace="ns-1", environment={}, deployments=[], pods=[]
        )
        == "unknown"
    )


def test_build_standup_parameters_data_strictly_stringified_and_extracts_topology():
    manifest = """apiVersion: leaderworkerset.x-k8s.io/v1
kind: LeaderWorkerSet
metadata:
  name: pd-decode
spec:
  replicas: 2
  leaderWorkerTemplate:
    size: 2
    workerTemplate:
      metadata:
        labels:
          llm-d.ai/role: decode
      spec:
        containers:
        - name: modelserver
          command: [vllm, serve]
          args: [Qwen/Qwen3-30B, --tensor-parallel-size=4, --data-parallel-size=2, --data-parallel-size-local=1]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pd-prefill
spec:
  replicas: 3
  template:
    metadata:
      labels:
        llm-d.ai/role: prefill
    spec:
      containers:
      - name: modelserver
        command: [vllm, serve]
        args: [Qwen/Qwen3-30B, -tp, "2"]
"""
    execution = SimpleNamespace(
        configuration_artifacts=[SimpleNamespace(content=json.dumps({"officialGuide": {"renderedManifest": manifest}}))]
    )
    data = evaluate_router._build_standup_parameters_data(
        execution,
        model_name="Qwen/Qwen3-30B",
        namespace="ns-pd",
        accelerator_model="Intel(R) Arc(TM) Pro B60 Graphics",
        deployments=[],
    )
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())
    assert data["model_name"] == "Qwen/Qwen3-30B"
    assert data["namespace"] == "ns-pd"
    assert data["accelerator_model"] == "Intel(R) Arc(TM) Pro B60 Graphics"
    assert data["decode_replicas"] == "2"
    assert data["decode_tensor_parallelism"] == "4"
    assert data["decode_data_parallelism"] == "2"
    assert data["decode_data_local_parallelism"] == "1"
    assert data["decode_workers_parallelism"] == "2"
    assert data["prefill_replicas"] == "3"
    assert data["prefill_tensor_parallelism"] == "2"
    assert data["multinode_enabled"] == "true"


@pytest.mark.asyncio
async def test_standup_parameters_configmap_preserves_user_cm_and_refcounts_concurrent_runs(monkeypatch, tmp_path):
    kc = tmp_path / "kubeconfig"
    kc.write_text("kind: Config\n", encoding="utf-8")
    env = {"KUBECONFIG": str(kc)}
    evaluate_router._standup_configmap_refs.clear()

    applied_manifests = []
    deleted = []
    existing_cm: dict | None = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": evaluate_router._STANDUP_CM_NAME, "namespace": "ns-user", "labels": {"user": "keep"}},
        "data": {"custom": "value"},
    }

    async def fake_k8s_json(args, _env):
        ns = args[args.index("-n") + 1]
        if args[:3] == ["get", "configmap", evaluate_router._STANDUP_CM_NAME]:
            if ns == "ns-user":
                return existing_cm
            return applied_manifests[-1] if (ns == "ns-managed" and applied_manifests) else None
        return None

    async def fake_k8s_apply(manifest, _env):
        applied_manifests.append(manifest)
        return True

    async def fake_k8s_delete(resource, name, ns, _env):
        deleted.append((resource, name, ns))
        return True

    monkeypatch.setattr(evaluate_router, "_eval_k8s_json", fake_k8s_json)
    monkeypatch.setattr(evaluate_router, "_eval_k8s_apply", fake_k8s_apply)
    monkeypatch.setattr(evaluate_router, "_eval_k8s_delete", fake_k8s_delete)
    monkeypatch.setattr(evaluate_router, "_resolve_standup_accelerator_model", AsyncMock(return_value="XPU"))

    execution = SimpleNamespace(configuration_artifacts=[])

    # Pre-existing user ConfigMap without managed label -> preserved untouched, never deleted.
    managed_user = await evaluate_router._prepare_standup_parameters_configmap(
        {},
        execution,
        model_name="m",
        namespace="ns-user",
        environment=env,
        selected_hardware=None,
        cluster_id=None,
        deployments=[],
        pods=[],
    )
    assert managed_user is False
    assert applied_manifests == []

    # Concurrent managed runs refcount cleanly: first cleanup decrements 2->1 (no delete), second 1->0 (deletes).
    assert (
        await evaluate_router._prepare_standup_parameters_configmap(
            {},
            execution,
            model_name="m",
            namespace="ns-managed",
            environment=env,
            selected_hardware=None,
            cluster_id=None,
            deployments=[],
            pods=[],
        )
        is True
    )
    assert len(applied_manifests) == 1
    assert all(isinstance(v, str) for v in applied_manifests[0]["data"].values())
    assert (
        await evaluate_router._prepare_standup_parameters_configmap(
            {},
            execution,
            model_name="m",
            namespace="ns-managed",
            environment=env,
            selected_hardware=None,
            cluster_id=None,
            deployments=[],
            pods=[],
        )
        is True
    )
    ref_key = (str(kc), "ns-managed")
    assert evaluate_router._standup_configmap_refs[ref_key] == 2

    # Concurrent run on a second cluster with the SAME namespace name keeps an independent refcount.
    kc_b = tmp_path / "kubeconfig-b"
    kc_b.write_text("kind: Config\n", encoding="utf-8")
    env_b = {"KUBECONFIG": str(kc_b)}
    assert (
        await evaluate_router._prepare_standup_parameters_configmap(
            {},
            execution,
            model_name="m",
            namespace="ns-managed",
            environment=env_b,
            selected_hardware=None,
            cluster_id=None,
            deployments=[],
            pods=[],
        )
        is True
    )
    assert evaluate_router._standup_configmap_refs[(str(kc_b), "ns-managed")] == 1
    await evaluate_router._cleanup_standup_parameters_configmap("ns-managed", env_b)
    assert deleted == [("configmap", evaluate_router._STANDUP_CM_NAME, "ns-managed")]
    assert (str(kc_b), "ns-managed") not in evaluate_router._standup_configmap_refs
    assert evaluate_router._standup_configmap_refs[ref_key] == 2
    deleted.clear()

    await evaluate_router._cleanup_standup_parameters_configmap("ns-managed", env)
    assert deleted == []
    assert evaluate_router._standup_configmap_refs[ref_key] == 1

    await evaluate_router._cleanup_standup_parameters_configmap("ns-managed", env)
    assert deleted == [("configmap", evaluate_router._STANDUP_CM_NAME, "ns-managed")]
    assert ref_key not in evaluate_router._standup_configmap_refs


@pytest.mark.asyncio
async def test_gaie_presets_config_b64_and_harness_env_override(tmp_path):
    import base64

    import yaml

    epp_yaml = (
        "apiVersion: inference.networking.x-k8s.io/v1alpha1\n"
        "kind: EndpointPickerConfig\n"
        "plugins:\n"
        "- type: prefix-cache-scorer\n"
    )
    manifest = yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "epp-dep"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "epp", "args": ["--config-file=/config/custom-epp.yaml"]}],
                            "volumes": [{"name": "cfg", "configMap": {"name": "epp-cm"}}],
                        }
                    }
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "epp-cm"},
                "data": {"custom-epp.yaml": epp_yaml},
            },
        ]
    )
    exec_with_epp = SimpleNamespace(
        configuration_artifacts=[SimpleNamespace(content=json.dumps({"officialGuide": {"renderedManifest": manifest}}))]
    )
    b64 = await evaluate_router._resolve_gaie_presets_config_b64(exec_with_epp, "ns", {})
    decoded = yaml.safe_load(base64.b64decode(b64).decode("utf-8"))
    assert decoded["plugins"] == [{"type": "prefix-cache-scorer"}]

    # Fallback when no EndpointPickerConfig exists -> plugins: []\n -> cGx1Z2luczogW10K
    b64_default = await evaluate_router._resolve_gaie_presets_config_b64(
        SimpleNamespace(configuration_artifacts=[]), "ns", {}
    )
    assert b64_default == "cGx1Z2luczogW10K"
    assert yaml.safe_load(base64.b64decode(b64_default).decode("utf-8")) == {"plugins": []}

    # _harness_proxy_override forwards LLMDBENCH_VLLM_MODELSERVICE_GAIE_PRESETS_CONFIG.
    override = _harness_proxy_override({"LLMDBENCH_VLLM_MODELSERVICE_GAIE_PRESETS_CONFIG": b64})
    assert override is not None
    assert json.loads(override.split("=", 1)[1]) == [
        {"name": "LLMDBENCH_VLLM_MODELSERVICE_GAIE_PRESETS_CONFIG", "value": b64}
    ]


@pytest.mark.asyncio
async def test_k8s_discovery_and_gaie_reject_flag_like_names_and_invalid_namespaces(monkeypatch, tmp_path):
    kc = tmp_path / "kubeconfig"
    kc.write_text("kind: Config\n", encoding="utf-8")
    env = {"KUBECONFIG": str(kc)}
    k8s_calls = []

    async def spy_k8s_json(args, _env):
        k8s_calls.append(args)

    monkeypatch.setattr(evaluate_router, "_eval_k8s_json", spy_k8s_json)

    # Flag-like configMap.name in deployment volumes is rejected before _eval_k8s_json.
    evil_dep = {
        "metadata": {"name": "evil-dep"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "epp",
                            "args": ["--pool-name=p"],
                            "volumeMounts": [{"name": "v1", "mountPath": "/cfg"}],
                        },
                        {
                            "name": "envoy-proxy",
                            "volumeMounts": [{"name": "v1", "mountPath": "/etc/envoy"}],
                        },
                    ],
                    "volumes": [{"name": "v1", "configMap": {"name": "--server=https://evil.example"}}],
                }
            }
        },
    }
    assert evaluate_router._envoy_configmap_names(evil_dep) == []
    assert (
        await evaluate_router._resolve_gaie_presets_config_b64(
            SimpleNamespace(configuration_artifacts=[]), "--kubeconfig=/etc/passwd", env, deployments=[evil_dep]
        )
        == "cGx1Z2luczogW10K"
    )
    assert await evaluate_router._fetch_namespace_resources("--all-namespaces", env) == ([], [], [], False)
    assert k8s_calls == []
