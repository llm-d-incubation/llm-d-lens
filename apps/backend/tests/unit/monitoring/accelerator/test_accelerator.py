"""Tests for accelerator observability discovery, preflight and API contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.infrastructure import kubernetes as kubernetes_module
from llm_d_lens.infrastructure.process import CommandResult
from llm_d_lens.monitoring.accelerator import intel_gpu
from llm_d_lens.monitoring.accelerator.intel_gpu import (
    IntelGpuProvider,
    _availability,
    _compute_engine_dashboard,
)
from llm_d_lens.monitoring.accelerator.models import (
    AcceleratorInstallRequest,
    AcceleratorStatusResponse,
    GpuAccess,
)
from llm_d_lens.monitoring.accelerator.nvidia_gpu import NvidiaGpuProvider
from llm_d_lens.monitoring.accelerator.operations import (
    AcceleratorOperationManager,
    AcceleratorOperationStore,
)
from llm_d_lens.monitoring.accelerator.service import (
    _find_service,
    _grafana_dashboard_path,
    _service_port,
    _slugify,
    get_links,
    get_status,
    validate_namespace,
)
from llm_d_lens.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ClusterSummary,
    HelmReleaseSummary,
)


@pytest.fixture(autouse=True)
def _use_cli_kubernetes_backend(monkeypatch):
    monkeypatch.setenv("PRISM_KUBERNETES_BACKEND", "cli")


def _result(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


def _patch_runner(monkeypatch, runner: FakeRunner) -> FakeRunner:
    """Route the provider's scoped runner (used by kubernetes/helm helpers) to a fake."""

    def factory(cluster_id=None):
        del cluster_id
        return runner

    monkeypatch.setattr(kubernetes_module, "scoped_runner", factory)
    monkeypatch.setattr(intel_gpu, "scoped_runner", factory)
    return runner


class FakeRunner:
    def __init__(
        self,
        *,
        installed: bool,
        access_mode: GpuAccess = "dra",
        gpu_nodes: bool = True,
        degraded: bool = False,
    ) -> None:
        self.installed = installed
        self.access_mode = access_mode
        self.gpu_nodes = gpu_nodes
        self.degraded = degraded
        self.labeled_nodes: list[str] = []

    def executable(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
        del timeout
        command = " ".join(argv)
        if command == "kubectl config current-context":
            return _result(argv, "kind-test\n")
        if command == "kubectl cluster-info":
            return _result(argv, "Kubernetes control plane is running")
        if command.startswith("helm status xpumd"):
            if not self.installed:
                return _result(argv, stderr="Error: release: not found", returncode=1)
            return _result(argv, json.dumps({"version": 2, "info": {"status": "deployed"}}))
        if command.startswith("helm list"):
            if not self.installed:
                return _result(argv, json.dumps([]))
            return _result(argv, json.dumps([{"chart": "xpumd-2.1.0"}]))
        if command.startswith("helm get values"):
            return _result(argv, json.dumps({"gpuAccess": self.access_mode}))
        if "app.kubernetes.io/name=xpumd" in command and "daemonsets,services" in command:
            items = []
            if self.installed:
                items = [self._daemonset("xpumd", ready=True)]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get servicemonitors"):
            items = []
            if self.installed and not self.degraded:
                items = [
                    {
                        "kind": "ServiceMonitor",
                        "metadata": {"name": "xpumd", "labels": {"app.kubernetes.io/name": "xpumd"}},
                    }
                ]
            return _result(argv, json.dumps({"items": items}))
        if "grafana_dashboard=1" in command:
            items = []
            if self.installed and not self.degraded:
                items = [{"kind": "ConfigMap", "metadata": {"name": "xpumd-dashboard"}}]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get nodes"):
            items = []
            if self.gpu_nodes:
                items = [
                    {
                        "metadata": {"name": "gpu-node-1"},
                        "status": {"allocatable": {"gpu.intel.com/monitoring": "1"}},
                    }
                ]
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get resourceslices"):
            items = [{"spec": {"driver": "gpu.intel.com", "nodeName": "gpu-node-1"}}] if self.gpu_nodes else []
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl label node"):
            self.labeled_nodes.append(argv[3])
            return _result(argv, "node/gpu-node-1 labeled\n")
        if command == "kubectl get daemonsets --all-namespaces -o json":
            return _result(argv, json.dumps({"items": self._access_daemonsets()}))
        if command.startswith("kubectl get namespace"):
            return _result(argv, stderr="not found", returncode=1)
        raise AssertionError(f"Unexpected command: {command}")

    def _access_daemonsets(self) -> list[dict]:
        if self.access_mode == "dra":
            return [
                {
                    "kind": "DaemonSet",
                    "metadata": {"name": "intel-gpu-resource-driver", "namespace": "intel-gpu-resource-driver"},
                    "status": {"desiredNumberScheduled": 1, "numberReady": 1},
                }
            ]
        return [
            {
                "kind": "DaemonSet",
                "metadata": {
                    "name": "intel-gpu-plugin",
                    "namespace": "intel-gpu-plugin",
                    "labels": {"app": "intel-gpu-plugin"},
                },
                "spec": {"template": {"spec": {"containers": [{"args": ["-enable-monitoring"]}]}}},
                "status": {"desiredNumberScheduled": 1, "numberReady": 1},
            }
        ]

    @staticmethod
    def _daemonset(name: str, *, ready: bool) -> dict:
        return {
            "kind": "DaemonSet",
            "metadata": {"name": name},
            "status": {"desiredNumberScheduled": 1, "numberReady": 1 if ready else 0},
        }


@pytest.mark.asyncio
async def test_discovery_reports_absent(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=False))
    status = await provider.discover_status("intel-xpumd", None)

    assert status.status == "absent"
    assert status.cluster_reachable is True
    assert status.context == "kind-test"
    assert status.release.name == "xpumd"
    assert status.access_modes[0].mode == "dra"
    assert status.access_modes[0].available is True


@pytest.mark.asyncio
async def test_discovery_reports_ready(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=True))
    status = await provider.discover_status("intel-xpumd", None)

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["xpumd"].status == "ready"
    assert components["prometheus_service_monitor"].status == "ready"
    assert components["grafana_dashboard"].status == "ready"
    assert components["gpu_nodes"].status == "ready"
    assert status.access_mode == "dra"


@pytest.mark.asyncio
async def test_access_mode_detection_plugin(monkeypatch):
    provider = IntelGpuProvider()
    _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin"))
    status = await provider.discover_status("intel-xpumd", None)

    plugin = next(mode for mode in status.access_modes if mode.mode == "plugin")
    assert plugin.detected is True
    assert plugin.available is True


def test_plugin_requires_enable_monitoring_flag():
    plugin_daemonset = {
        "kind": "DaemonSet",
        "metadata": {"name": "intel-gpu-plugin", "namespace": "intel-gpu-plugin"},
        "spec": {"template": {"spec": {"containers": [{"name": "intel-gpu-plugin"}]}}},
        "status": {"desiredNumberScheduled": 1, "numberReady": 1},
    }

    availability = _availability("plugin", [plugin_daemonset])

    assert availability.detected is True
    assert availability.available is False
    assert "-enable-monitoring" in availability.message


@pytest.mark.asyncio
async def test_preflight_blocks_on_missing_monitoring_stack(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="failed"),
        status="degraded",
        message="degraded",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False))
    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["monitoring_stack"].passed is False
    assert result.allowed is False


@pytest.mark.asyncio
async def test_preflight_gpu_nodes_presence_check_is_blocking(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False, gpu_nodes=True))
    result = await provider.preflight(
        AcceleratorInstallRequest(),
        None,
        monitoring_status=monitoring,
    )

    checks = {check.name: check for check in result.checks}
    # Hardware presence is a blocking gate; the access-mode label stays a warning.
    assert checks["gpu_nodes"].blocking is True
    assert checks["access_label"].passed is False
    assert checks["access_label"].blocking is False
    assert result.status == "absent"
    assert result.command_preview[0] == "helm"


@pytest.mark.asyncio
async def test_intel_hardware_presence_probe_detects_label_dra_or_extended_resource(monkeypatch):
    from llm_d_lens.monitoring.accelerator import intel_gpu

    present = {"label": False, "dra": False, "extended": False}

    async def fake_list_resources(kind: str, **kwargs):
        if kind == "nodes" and kwargs.get("selector"):
            return [{"metadata": {"name": "n"}}] if present["label"] else []
        if kind == "resourceslices":
            return [{"spec": {"driver": "gpu.intel.com"}}] if present["dra"] else []
        if kind == "nodes":
            if present["extended"]:
                return [{"status": {"allocatable": {"gpu.intel.com/xe": "8"}}}]
            return []
        return []

    monkeypatch.setattr(intel_gpu, "list_resources", fake_list_resources)
    assert await intel_gpu._intel_hardware_present(None) is False
    for signal in ("label", "dra", "extended"):
        present[signal] = True
        assert await intel_gpu._intel_hardware_present(None) is True
        present[signal] = False


def test_extended_resource_node_names_requires_exact_resource_domain():
    from llm_d_lens.monitoring.accelerator.intel_gpu import _extended_resource_node_names

    nodes = [
        {"metadata": {"name": "valid"}, "status": {"allocatable": {"gpu.intel.com/xe": "8"}}},
        {"metadata": {"name": "invalid"}, "status": {"allocatable": {"gpu.intel.com.evil/xe": "8"}}},
    ]
    assert _extended_resource_node_names(nodes) == {"valid"}


@pytest.mark.asyncio
async def test_preflight_access_label_only_for_dra(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin", gpu_nodes=False))
    result = await provider.preflight(
        AcceleratorInstallRequest(access_mode="plugin"),
        None,
        monitoring_status=monitoring,
    )

    check_names = {check.name for check in result.checks}
    assert "access_label" not in check_names
    assert "gpu_nodes" in check_names  # gpu_nodes is shown for plugin too


@pytest.mark.asyncio
async def test_prepare_nodes_labels_gpu_nodes_for_dra(monkeypatch):
    provider = IntelGpuProvider()
    runner = _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="dra", gpu_nodes=True))
    await provider.prepare_nodes(None, access_mode="dra")

    assert runner.labeled_nodes == ["gpu-node-1"]


@pytest.mark.asyncio
async def test_prepare_nodes_labels_gpu_nodes_for_plugin(monkeypatch):
    provider = IntelGpuProvider()
    runner = _patch_runner(monkeypatch, FakeRunner(installed=False, access_mode="plugin", gpu_nodes=True))
    await provider.prepare_nodes(None, access_mode="plugin")

    assert runner.labeled_nodes == ["gpu-node-1"]


@pytest.mark.asyncio
async def test_preflight_allows_reconcile_when_degraded(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=True, degraded=True))
    status = await provider.discover_status("intel-xpumd", None)
    assert status.status == "degraded"

    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["stack_state"].passed is True
    assert result.allowed is True
    assert result.status == "degraded"
    assert result.command_preview[:3] == ["helm", "upgrade", "--install"]


@pytest.mark.asyncio
async def test_preflight_allows_reconcile_when_ready(monkeypatch):
    provider = IntelGpuProvider()
    monitoring = ClusterStackStatusResponse(
        cluster=ClusterSummary(reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(name="llmd", status="deployed"),
        status="ready",
        message="ready",
        observed_at=datetime.now(UTC),
    )
    _patch_runner(monkeypatch, FakeRunner(installed=True))
    status = await provider.discover_status("intel-xpumd", None)
    assert status.status == "ready"

    result = await provider.preflight(AcceleratorInstallRequest(), None, monitoring_status=monitoring)

    checks = {check.name: check for check in result.checks}
    assert checks["stack_state"].passed is True
    assert result.allowed is True
    assert result.status == "ready"
    assert result.command_preview[:3] == ["helm", "upgrade", "--install"]


def test_install_argv_wires_prometheus_exporter():
    provider = IntelGpuProvider()
    argv = provider.install_argv(
        AcceleratorInstallRequest(),
        monitoring_namespace="llm-d-monitoring",
        monitoring_release="llmd",
    )

    assert "config.service.pipelines.metrics.exporters={intel_xpu_info,prometheus}" in argv


def test_observability_link_service_discovery():
    services = [
        {"metadata": {"name": "llmd-grafana"}, "spec": {"ports": [{"port": 80}]}},
        {
            "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
            "spec": {"ports": [{"port": 9090}, {"port": 8080}]},
        },
        {"metadata": {"name": "llmd-prometheus-node-exporter"}, "spec": {"ports": [{"port": 9100}]}},
    ]

    prometheus = _find_service(services, lambda name: name.endswith("-prometheus"))
    grafana = _find_service(services, lambda name: name.endswith("-grafana"))

    assert prometheus is not None
    assert prometheus["metadata"]["name"] == "llmd-kube-prometheus-stack-prometheus"
    assert _service_port(prometheus, 9090) == 9090
    assert grafana is not None
    assert grafana["metadata"]["name"] == "llmd-grafana"
    assert _service_port(grafana, 80) == 80


def test_slugify_matches_grafana_title():
    assert _slugify("Intel XPU Manager v2 Exporter") == "intel-xpu-manager-v2-exporter"
    assert _slugify("GPU  Utilization / Temp!") == "gpu-utilization-temp"


def test_compute_engine_dashboard_only_changes_gpu_overview_panels():
    dashboard = {
        "panels": [
            {
                "title": "GPU Utilization",
                "targets": [{"expr": 'avg by (pci_bdf) (hw_gpu_utilization_ratio{node="$Node"})'}],
            },
            {
                "title": "Engine Group Utilization",
                "targets": [{"expr": 'avg by (pci_bdf,hw_gpu_task) (hw_gpu_utilization_ratio{node="$Node"})'}],
            },
        ]
    }

    normalized, changed = _compute_engine_dashboard(json.dumps(dashboard))
    result = json.loads(normalized)

    assert changed is True
    assert result["panels"][0]["targets"][0]["expr"] == (
        'avg by (pci_bdf) (hw_gpu_utilization_ratio{hw_gpu_task="compute-all",node="$Node"})'
    )
    assert result["panels"][1] == dashboard["panels"][1]


@pytest.mark.asyncio
async def test_grafana_dashboard_path_resolves_uid_from_configmap(monkeypatch):
    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        assert resource == "configmaps"
        assert namespace == "llm-d-monitoring"
        assert selector == "grafana_dashboard=1"
        assert cluster_id == "test-cluster"
        return [
            {
                "kind": "ConfigMap",
                "metadata": {"name": "xpumd-dashboard"},
                "data": {
                    "dashboard.json": json.dumps(
                        {
                            "uid": "9VxbZDPink",
                            "title": "Intel XPU Manager v2 Exporter",
                        }
                    )
                },
            }
        ]

    monkeypatch.setattr("llm_d_lens.monitoring.accelerator.service.list_resources", fake_list_resources)

    path = await _grafana_dashboard_path("llm-d-monitoring", "test-cluster")
    assert path == "/d/9VxbZDPink/intel-xpu-manager-v2-exporter"


@pytest.mark.asyncio
async def test_grafana_dashboard_path_returns_none_without_dashboard(monkeypatch):
    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        return []

    monkeypatch.setattr("llm_d_lens.monitoring.accelerator.service.list_resources", fake_list_resources)

    assert await _grafana_dashboard_path("llm-d-monitoring", "test-cluster") is None


@pytest.mark.asyncio
async def test_status_without_cluster_does_not_query_ambient(monkeypatch):
    def unexpected(cluster_id=None):
        raise AssertionError(f"scoped_runner should not be called without a cluster (got {cluster_id!r})")

    monkeypatch.setattr(kubernetes_module, "scoped_runner", unexpected)
    status = await get_status("intel_gpu", "intel-xpumd", cluster_id=None)

    assert status.status == "unknown"
    assert status.cluster_reachable is False
    assert status.message == "No target cluster selected"
    assert status.stale is True


@pytest.mark.asyncio
async def test_links_without_cluster_returns_empty(monkeypatch):
    def unexpected(cluster_id=None):
        raise AssertionError(f"scoped_runner should not be called without a cluster (got {cluster_id!r})")

    monkeypatch.setattr(kubernetes_module, "scoped_runner", unexpected)
    links = await get_links("intel_gpu", cluster_id=None)

    assert links.accelerator == "intel_gpu"
    assert links.links == []


def test_install_request_forbids_unknown_fields_and_validates_namespace():
    with pytest.raises(ValueError):
        AcceleratorInstallRequest.model_validate({"namespace": "Invalid_Name"})
    with pytest.raises(ValueError):
        AcceleratorInstallRequest.model_validate({"unexpected": True})
    assert validate_namespace("intel-xpumd") == "intel-xpumd"


def test_shared_api_registers_accelerator_routes():
    client = TestClient(app)
    paths = client.get("/api/simulation/openapi.json").json()["paths"]

    assert "/api/v1/monitoring/accelerators" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/status" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/installations" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/installations/preflight" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/operations/{operation_id}" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/links" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/admin-script" in paths
    assert "/api/v1/monitoring/accelerators/{accelerator}/recheck" in paths
    admin_script_op = paths["/api/v1/monitoring/accelerators/{accelerator}/admin-script"]["get"]
    recheck_op = paths["/api/v1/monitoring/accelerators/{accelerator}/recheck"]["post"]
    assert "description" not in admin_script_op
    assert admin_script_op["operationId"].endswith("_get")
    assert "description" not in recheck_op
    assert recheck_op["operationId"].endswith("_post")


def test_operation_path_cannot_escape_store(tmp_path: Path):
    store = AcceleratorOperationStore(tmp_path)
    assert store.load("../../etc/passwd") is None

    manager = AcceleratorOperationManager(store)
    from llm_d_lens.monitoring.accelerator.models import AcceleratorOperationResponse

    operation = AcceleratorOperationResponse(
        operation_id="a" * 32,
        status="running",
        phase="installing_chart",
        accelerator="intel_gpu",
        access_mode="dra",
        namespace="intel-xpumd",
    )
    manager._append(operation, "\x1b[32madmin password: secret-value\x1b[0m")
    assert operation.logs[0].message == "admin password: [REDACTED]"


@pytest.mark.asyncio
async def test_install_operation_requires_ready_verification(tmp_path: Path):
    script = tmp_path / "install.sh"
    script.write_text(
        "#!/bin/sh\necho 'Installing xpumd chart'\necho 'admin password: secret-value'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o100)
    manager = AcceleratorOperationManager(AcceleratorOperationStore(tmp_path / "operations"))
    phases = []

    async def configure_monitoring() -> None:
        phases.append("configure")

    async def verify(namespace: str) -> AcceleratorStatusResponse:
        phases.append("verify")
        return AcceleratorStatusResponse(
            accelerator="intel_gpu",
            cluster_reachable=True,
            namespace=namespace,
            release=HelmReleaseSummary(name="xpumd", status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        AcceleratorInstallRequest(),
        context="kind-test",
        argv=[str(script)],
        verifier=verify,
        idempotency_key="request-1",
        post_install=configure_monitoring,
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    assert phases == ["configure", "verify"]
    assert completed.phase == "completed"
    assert all("secret-value" not in entry.message for entry in completed.logs)
    persisted = manager.store.load(created.operation_id)
    assert persisted is not None
    assert persisted.status == "succeeded"


def test_install_request_requires_registered_accelerator():
    from pydantic import ValidationError

    assert AcceleratorInstallRequest(accelerator="intel_gpu").accelerator == "intel_gpu"
    with pytest.raises(ValidationError):
        AcceleratorInstallRequest(accelerator="not-a-real-accelerator")


def _assert_bash_syntax(script: str) -> None:
    import subprocess

    proc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


def _setup_stub_bin(tmp_path: Path, *, kubectl_body: str, helm_body: str) -> dict[str, str]:
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("kubectl", kubectl_body), ("helm", helm_body)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env


def test_registry_profile_mapping_and_cleanup_specs():
    import os
    import subprocess
    import sys

    from llm_d_lens.hardware.registry import get_profile
    from llm_d_lens.monitoring.accelerator import nvidia_gpu
    from llm_d_lens.monitoring.accelerator.registry import (
        AcceleratorCleanupSpec,
        get_registered_provider,
        provider_id_for_profile,
    )

    assert provider_id_for_profile(None) is None
    assert provider_id_for_profile(get_profile("nvidia")) == "nvidia_gpu"
    assert provider_id_for_profile(get_profile("intel-xpu")) == "intel_gpu"

    nv = get_registered_provider("nvidia_gpu")
    intel = get_registered_provider("intel_gpu")
    assert isinstance(nv, nvidia_gpu.NvidiaGpuProvider)
    assert isinstance(intel, IntelGpuProvider)
    assert get_registered_provider("does-not-exist") is None
    assert get_registered_provider("") is None

    # Verify cold-start lazy registration when importing registry alone
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4] / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from llm_d_lens.monitoring.accelerator.registry import get_registered_provider; "
                "assert get_registered_provider('nvidia_gpu') is not None; "
                "assert get_registered_provider('intel_gpu') is not None"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    assert nv.cleanup_spec() == AcceleratorCleanupSpec(
        provider_id="nvidia_gpu",
        display_name="NVIDIA GPU",
        release_name="dcgm-exporter",
        namespace="dcgm-exporter",
    )
    assert nv.cleanup_spec(namespace="custom-dcgm").namespace == "custom-dcgm"

    assert intel.cleanup_spec() == AcceleratorCleanupSpec(
        provider_id="intel_gpu",
        display_name="Intel GPU",
        release_name="xpumd",
        namespace="intel-xpumd",
    )
    assert intel.cleanup_spec(namespace="custom-xpumd").namespace == "custom-xpumd"

    with pytest.raises(ValueError):
        nv.cleanup_spec(namespace="Bad_NS")
    with pytest.raises(ValueError):
        intel.cleanup_spec(namespace="Bad_NS")


def _setup_accelerator_script_env(
    tmp_path: Path,
    *,
    log_file: Path,
    values_copy: Path,
    sm_file: Path,
    dashboard_file: Path,
    target_ns: str,
) -> dict[str, str]:
    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "kubectl $*" >> {str(log_file)!r}
seen_double_dash=0
for arg in "$@"; do
  if [[ "$seen_double_dash" == "1" && "$arg" == -* ]]; then
    echo "error: all resources must be specified before label changes: $arg" >&2
    exit 1
  fi
  if [[ "$arg" == "--" ]]; then
    seen_double_dash=1
  fi
done
if [[ "$1" == "get" && "$2" == "daemonset" ]]; then
  printf '%s' "${{DS_ITEMS:-}}"
  exit 0
fi
if [[ "$1" == "get" && "$2" == "crd" ]]; then
  if [[ "${{HAS_SM_CRD:-1}}" == "1" ]]; then exit 0; else exit 1; fi
fi
if [[ "$1" == "get" && "$2" == "namespace" ]]; then
  if [[ "$3" == {target_ns!r} ]]; then
    exit 0
  fi
  if [[ "${{HAS_MON_NS:-1}}" == "1" ]]; then exit 0; else exit 1; fi
fi
if [[ "$1" == "get" && "$2" == "servicemonitor" ]]; then
  if [[ "$*" == *"namespaceSelector"* ]]; then
    printf '%s' "${{SM_ITEMS_MON_NS:-}}"
  else
    printf '%s' "${{SM_ITEMS_EXISTING_NS:-}}"
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "svc" ]]; then
  if [[ "$3" == "-n" ]]; then
    printf '%s' "${{SVC_LIST_ITEMS:-}}"
    exit 0
  fi
  if [[ "$*" == *"spec.selector"* ]]; then
    echo "ERROR: ServiceMonitor must never query Service.spec.selector" >&2
    exit 99
  fi
  if [[ "$*" == *"metadata.labels"* ]]; then
    printf '%s' "${{SVC_LABELS_LINE:-}}"
    exit 0
  fi
  if [[ "$*" == *"spec.ports"* ]]; then
    printf '%s' "${{SVC_PORTS_LINES:-}}"
    exit 0
  fi
fi
if [[ "$1" == "get" && "$2" == "resourceslices" ]]; then
  printf 'gpu.intel.com\\tgpu-node-dra\\n'
  printf 'gpu.intel.com\\t--all\\n'
  printf 'gpu.intel.com\\t*\\n'
  printf 'gpu.intel.com\\tInvalid_Node\\n'
  printf 'other.driver\\tnode-other\\n'
  exit 0
fi
if [[ "$1" == "get" && "$2" == "nodes" ]]; then
  printf 'gpu-node-plugin\\t{{"gpu.intel.com/xe":"2"}}\\n'
  printf 'evil-node\\t{{"gpu.intel.com.evil/xe":"2"}}\\n'
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  payload="$(cat)"
  if [[ "$payload" == *"kind: ServiceMonitor"* ]]; then
    printf '%s\\n' "$payload" > {str(sm_file)!r}
  else
    printf '%s\\n' "$payload" > {str(dashboard_file)!r}
  fi
  exit 0
fi
exit 0
"""
    helm_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "helm $*" >> {str(log_file)!r}
seen_double_dash=0
for arg in "$@"; do
  if [[ "$seen_double_dash" == "1" && "$arg" == -* ]]; then
    echo "error: unexpected flag after --: $arg" >&2
    exit 1
  fi
  if [[ "$arg" == "--" ]]; then
    seen_double_dash=1
  fi
done
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--values" ]]; then
    cp "$2" {str(values_copy)!r}
    shift 2
  else
    shift
  fi
done
"""
    return _setup_stub_bin(tmp_path, kubectl_body=kubectl_script, helm_body=helm_script)


def test_nvidia_render_install_script_managed_vs_existing(tmp_path: Path):
    import subprocess

    import yaml

    from llm_d_lens.monitoring.accelerator.nvidia_gpu import NvidiaGpuProvider

    provider = NvidiaGpuProvider()
    managed_script = provider.render_install_script(
        namespace="dcgm-exporter",
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=False,
    )
    existing_script = provider.render_install_script(
        namespace="dcgm-exporter",
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=True,
    )
    _assert_bash_syntax(managed_script)
    _assert_bash_syntax(existing_script)
    for script in (managed_script, existing_script):
        assert "set -euo pipefail" in script
        assert "umask 077" in script
        assert "trap 'rm -rf -- \"$TMP_DIR\"' EXIT" in script
        assert "~/.cache/lens/llm-d" not in script
        assert "jq" not in script
        assert "python" not in script
        assert " --set" not in script

    with pytest.raises(ValueError):
        provider.render_install_script(namespace="Bad_NS")
    with pytest.raises(ValueError):
        provider.render_install_script(access_mode="invalid")  # type: ignore[arg-type]

    log_file = tmp_path / "nv.log"
    values_copy = tmp_path / "nv-values.yaml"
    sm_file = tmp_path / "nv-sm.yaml"
    dashboard_file = tmp_path / "nv-dashboard.json"

    env = _setup_accelerator_script_env(
        tmp_path,
        log_file=log_file,
        values_copy=values_copy,
        sm_file=sm_file,
        dashboard_file=dashboard_file,
        target_ns="dcgm-exporter",
    )

    # 1. existing_monitoring=True skips ready DaemonSet in TARGET_NS AND any other namespace,
    #    and never creates a fallback ServiceMonitor
    for ds_entry in (
        "dcgm-exporter\tdcgm-exporter\t2\t2\n",
        "gpu-operator\tnvidia-dcgm-exporter\t1\t1\n",
    ):
        log_file.write_text("", encoding="utf-8")
        sm_file.unlink(missing_ok=True)
        env["DS_ITEMS"] = ds_entry
        env["SVC_LIST_ITEMS"] = "nvidia-dcgm-exporter\n"
        env["SVC_LABELS_LINE"] = "dcgm-exporter\t\t\n"
        env["SVC_PORTS_LINES"] = "metrics\t9400\n"
        proc = subprocess.run(
            ["bash", "-s"], input=existing_script, text=True, env=env, capture_output=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        assert "skipping dcgm-exporter install" in proc.stdout
        assert "helm upgrade" not in log_file.read_text(encoding="utf-8")
        assert not sm_file.exists()

    # 2. existing_monitoring=True installs when no dcgm-exporter DS exists; CRD absent disables serviceMonitor
    log_file.write_text("", encoding="utf-8")
    env["DS_ITEMS"] = "kube-system\tcoredns\t2\t2\n"
    env["HAS_SM_CRD"] = "0"
    env["HAS_MON_NS"] = "0"
    dashboard_file.unlink(missing_ok=True)
    proc = subprocess.run(["bash", "-s"], input=existing_script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "helm upgrade --install dcgm-exporter" in log_file.read_text(encoding="utf-8")
    values = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values["serviceMonitor"]["enabled"] is False
    assert values["runtimeClassName"] == "nvidia"
    assert values["nodeSelector"] == {"feature.node.kubernetes.io/pci-10de.present": "true"}
    assert not dashboard_file.exists()

    # 3. existing_monitoring=False skips ready DS in TARGET_NS, creates fallback SM, and reconciles dashboard
    log_file.write_text("", encoding="utf-8")
    sm_file.unlink(missing_ok=True)
    dashboard_file.unlink(missing_ok=True)
    env["DS_ITEMS"] = "dcgm-exporter\tdcgm-exporter\t2\t2\n"
    env["HAS_SM_CRD"] = "1"
    env["HAS_MON_NS"] = "1"
    env["SM_ITEMS_EXISTING_NS"] = ""
    env["SM_ITEMS_MON_NS"] = ""
    env["SVC_LIST_ITEMS"] = "dcgm-exporter\n"
    env["SVC_LABELS_LINE"] = "dcgm-exporter\t\t\n"
    env["SVC_PORTS_LINES"] = "metrics\t9400\n"
    proc_target_skip = subprocess.run(
        ["bash", "-s"], input=managed_script, text=True, env=env, capture_output=True, check=False
    )
    assert proc_target_skip.returncode == 0, proc_target_skip.stderr
    assert "skipping dcgm-exporter install" in proc_target_skip.stdout
    assert "helm upgrade" not in log_file.read_text(encoding="utf-8")
    assert sm_file.exists()
    sm_doc = yaml.safe_load(sm_file.read_text(encoding="utf-8"))
    assert sm_doc["metadata"]["name"] == "lens-managed-dcgm-exporter"
    assert sm_doc["metadata"]["namespace"] == "dcgm-exporter"
    assert sm_doc["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "lens", "release": "llmd"}
    assert sm_doc["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": "dcgm-exporter"}
    assert sm_doc["spec"]["endpoints"] == [{"port": "metrics", "path": "/metrics"}]
    assert dashboard_file.exists()
    assert "dcgm-exporter-dashboard" in dashboard_file.read_text(encoding="utf-8")

    # 4. existing_monitoring=False reconciles via Helm when DS in TARGET_NS is unready
    log_file.write_text("", encoding="utf-8")
    sm_file.unlink(missing_ok=True)
    dashboard_file.unlink(missing_ok=True)
    env["DS_ITEMS"] = "dcgm-exporter\tdcgm-exporter\t2\t1\n"
    proc_reconcile = subprocess.run(
        ["bash", "-s"], input=managed_script, text=True, env=env, capture_output=True, check=False
    )
    assert proc_reconcile.returncode == 0, proc_reconcile.stderr
    assert "helm upgrade --install dcgm-exporter" in log_file.read_text(encoding="utf-8")
    values_reconcile = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values_reconcile["serviceMonitor"]["enabled"] is True
    assert dashboard_file.exists()
    assert "dcgm-exporter-dashboard" in dashboard_file.read_text(encoding="utf-8")

    # 5. existing_monitoring=False skips ready external DS and reconciles dashboard
    log_file.write_text("", encoding="utf-8")
    sm_file.unlink(missing_ok=True)
    dashboard_file.unlink(missing_ok=True)
    env["DS_ITEMS"] = "gpu-operator\tnvidia-dcgm-exporter\t2\t2\n"
    env["SVC_LIST_ITEMS"] = "nvidia-dcgm-exporter\n"
    env["SVC_LABELS_LINE"] = "\tdcgm-exporter\t\n"
    env["SVC_PORTS_LINES"] = "web\t80\ndcgm\t9400\n"
    proc_ext_skip = subprocess.run(
        ["bash", "-s"], input=managed_script, text=True, env=env, capture_output=True, check=False
    )
    assert proc_ext_skip.returncode == 0, proc_ext_skip.stderr
    assert "helm upgrade" not in log_file.read_text(encoding="utf-8")
    assert sm_file.exists()
    ext_sm = yaml.safe_load(sm_file.read_text(encoding="utf-8"))
    assert ext_sm["metadata"]["namespace"] == "gpu-operator"
    assert ext_sm["spec"]["selector"]["matchLabels"] == {"app": "dcgm-exporter"}
    assert ext_sm["spec"]["endpoints"] == [{"port": "dcgm", "path": "/metrics"}]
    assert dashboard_file.exists()


def test_intel_render_install_script_managed_vs_existing_and_modes(tmp_path: Path):
    import subprocess

    import yaml

    provider = IntelGpuProvider()
    managed_dra = provider.render_install_script(
        namespace="intel-xpumd",
        access_mode="dra",
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=False,
    )
    managed_plugin = provider.render_install_script(
        namespace="intel-xpumd",
        access_mode="plugin",
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=False,
    )
    existing_dra = provider.render_install_script(
        namespace="intel-xpumd",
        access_mode="dra",
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=True,
    )
    for script in (managed_dra, managed_plugin, existing_dra):
        _assert_bash_syntax(script)
        assert "set -euo pipefail" in script
        assert "umask 077" in script
        assert "trap 'rm -rf -- \"$TMP_DIR\"' EXIT" in script
        assert "~/.cache/lens/llm-d" not in script
        assert "jq" not in script
        assert "python" not in script
        assert " --set" not in script

    with pytest.raises(ValueError):
        provider.render_install_script(namespace="Bad_NS")
    with pytest.raises(ValueError):
        provider.render_install_script(access_mode="invalid")  # type: ignore[arg-type]

    log_file = tmp_path / "intel.log"
    values_copy = tmp_path / "intel-values.yaml"
    sm_file = tmp_path / "intel-sm.yaml"
    dashboard_file = tmp_path / "intel-dash.yaml"

    env = _setup_accelerator_script_env(
        tmp_path,
        log_file=log_file,
        values_copy=values_copy,
        sm_file=sm_file,
        dashboard_file=dashboard_file,
        target_ns="intel-xpumd",
    )

    # 1. existing_monitoring=True skips ready xpumd or intel-xpumanager in TARGET_NS or any other namespace,
    #    and never creates a fallback ServiceMonitor
    for ds_entry in (
        "intel-xpumd\txpumd\t2\t2\n",
        "monitoring\tintel-xpumanager-ds\t1\t1\n",
    ):
        log_file.write_text("", encoding="utf-8")
        sm_file.unlink(missing_ok=True)
        env["DS_ITEMS"] = ds_entry
        env["SVC_LIST_ITEMS"] = "intel-xpumanager\n"
        env["SVC_LABELS_LINE"] = "intel-xpumanager\t\t\n"
        env["SVC_PORTS_LINES"] = "metrics\t29999\n"
        proc = subprocess.run(["bash", "-s"], input=existing_dra, text=True, env=env, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert "skipping xpumd install" in proc.stdout
        assert "helm upgrade" not in log_file.read_text(encoding="utf-8")
        assert not sm_file.exists()

    # 2. managed DRA mode reconciles TARGET_NS when DS is unready, sets admin-access label,
    #    labels DRA nodes, and populates values YAML
    log_file.write_text("", encoding="utf-8")
    sm_file.unlink(missing_ok=True)
    env["DS_ITEMS"] = "intel-xpumd\txpumd\t2\t1\n"
    env["HAS_SM_CRD"] = "1"
    env["HAS_MON_NS"] = "1"
    proc_dra = subprocess.run(["bash", "-s"], input=managed_dra, text=True, env=env, capture_output=True, check=False)
    assert proc_dra.returncode == 0, proc_dra.stderr
    calls_dra = log_file.read_text(encoding="utf-8")
    assert "kubectl label --overwrite namespace -- intel-xpumd resource.kubernetes.io/admin-access=true" in calls_dra
    label_node_calls = [
        line for line in calls_dra.splitlines() if line.startswith("kubectl label") and " node " in line
    ]
    assert label_node_calls == [
        "kubectl label --overwrite node -- gpu-node-dra intel.feature.node.kubernetes.io/gpu=true"
    ]
    assert "helm upgrade --install xpumd oci://ghcr.io/intel/xpumanager/charts/xpumd" in calls_dra
    values_dra = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values_dra["gpuAccess"] == "dra"
    assert values_dra["nodeSelector"] == {"intel.feature.node.kubernetes.io/gpu": "true"}
    assert values_dra["prometheus"] == {"monitor": True, "release": "llmd"}
    assert values_dra["grafana"] == {"dashboards": True, "namespace": "obs-mon"}
    assert values_dra["config"]["service"]["pipelines"]["metrics"]["exporters"] == ["intel_xpu_info", "prometheus"]

    # 3. managed plugin mode skips admin-access label, discovers plugin node, and disables missing SM/Grafana probes
    log_file.write_text("", encoding="utf-8")
    env["DS_ITEMS"] = ""
    env["HAS_SM_CRD"] = "0"
    env["HAS_MON_NS"] = "0"
    proc_plugin = subprocess.run(
        ["bash", "-s"], input=managed_plugin, text=True, env=env, capture_output=True, check=False
    )
    assert proc_plugin.returncode == 0, proc_plugin.stderr
    calls_plugin = log_file.read_text(encoding="utf-8")
    assert "resource.kubernetes.io/admin-access" not in calls_plugin
    assert "kubectl label --overwrite node -- gpu-node-plugin intel.feature.node.kubernetes.io/gpu=true" in calls_plugin
    assert "evil-node" not in calls_plugin
    values_plugin = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values_plugin["gpuAccess"] == "plugin"
    assert values_plugin["prometheus"]["monitor"] is False
    assert values_plugin["grafana"]["dashboards"] is False

    # 4. managed mode skips ready DS in TARGET_NS (same or different release) and external NS, creating fallback SM
    for ds_entry, exp_ns, svc_name, labels_line, ports_lines, exp_sel, exp_port in (
        (
            "intel-xpumd\txpumd\t2\t2\n",
            "intel-xpumd",
            "xpumd",
            "xpumd\t\t\n",
            "http-metrics\t8080\n",
            {"app.kubernetes.io/name": "xpumd"},
            "http-metrics",
        ),
        (
            "intel-xpumd\tcustom-intel-xpumanager\t1\t1\n",
            "intel-xpumd",
            "custom-intel-xpumanager",
            "\t\txpumanager-inst\n",
            "exporter\t29999\n",
            {"app.kubernetes.io/instance": "xpumanager-inst"},
            "exporter",
        ),
        (
            "other-ns\tintel-xpumanager\t2\t2\n",
            "other-ns",
            "intel-xpumanager-svc",
            "\txpumanager\t\n",
            "web\t9100\n",
            {"app": "xpumanager"},
            "web",
        ),
    ):
        log_file.write_text("", encoding="utf-8")
        sm_file.unlink(missing_ok=True)
        env["DS_ITEMS"] = ds_entry
        env["HAS_SM_CRD"] = "1"
        env["HAS_MON_NS"] = "1"
        env["SM_ITEMS_EXISTING_NS"] = ""
        env["SM_ITEMS_MON_NS"] = ""
        env["SVC_LIST_ITEMS"] = f"{svc_name}\n"
        env["SVC_LABELS_LINE"] = labels_line
        env["SVC_PORTS_LINES"] = ports_lines
        proc_skip = subprocess.run(
            ["bash", "-s"], input=managed_dra, text=True, env=env, capture_output=True, check=False
        )
        assert proc_skip.returncode == 0, proc_skip.stderr
        assert "skipping xpumd install" in proc_skip.stdout
        assert "helm upgrade" not in log_file.read_text(encoding="utf-8")
        assert sm_file.exists()
        sm_doc = yaml.safe_load(sm_file.read_text(encoding="utf-8"))
        assert sm_doc["metadata"]["name"] == "lens-managed-xpumd"
        assert sm_doc["metadata"]["namespace"] == exp_ns
        assert sm_doc["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "lens", "release": "llmd"}
        assert sm_doc["spec"]["selector"]["matchLabels"] == exp_sel
        assert sm_doc["spec"]["endpoints"] == [{"port": exp_port, "path": "/metrics"}]


@pytest.mark.parametrize("access_mode", [None, "dra", "plugin"])
@pytest.mark.parametrize("existing_monitoring", [False, True])
def test_intel_render_install_script_flag_ordering_and_double_dash_safety(
    tmp_path: Path,
    access_mode,
    existing_monitoring: bool,
):
    import re
    import shlex
    import subprocess

    provider = IntelGpuProvider()
    script = provider.render_install_script(
        namespace="intel-xpumd",
        access_mode=access_mode,
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=existing_monitoring,
    )
    _assert_bash_syntax(script)

    # Collapse backslash continuations and strip heredoc bodies so only executable shell lines are inspected.
    joined = re.sub(r"\\\n", " ", script)
    executable_lines: list[str] = []
    heredoc_end: str | None = None
    for raw_line in joined.splitlines():
        if heredoc_end is not None:
            if raw_line.strip() == heredoc_end:
                heredoc_end = None
            continue
        executable_lines.append(raw_line)
        match = re.search(r"<<-?\s*'?([A-Za-z0-9_]+)'?", raw_line)
        if match:
            heredoc_end = match.group(1)

    def _split_command_substitutions(text: str) -> list[str]:
        chunks: list[str] = []

        def scan(s: str) -> None:
            i = 0
            in_sq = False
            in_dq = False
            top: list[str] = []
            n = len(s)
            while i < n:
                ch = s[i]
                if ch == "\\" and not in_sq and i + 1 < n:
                    top.append(s[i : i + 2])
                    i += 2
                    continue
                if ch == "'" and not in_dq:
                    in_sq = not in_sq
                    top.append(ch)
                    i += 1
                    continue
                if ch == '"' and not in_sq:
                    in_dq = not in_dq
                    top.append(ch)
                    i += 1
                    continue
                if not in_sq and s[i : i + 2] == "$(":
                    j = i + 2
                    depth = 1
                    sub_sq = False
                    sub_dq = False
                    while j < n and depth > 0:
                        c = s[j]
                        if c == "\\" and not sub_sq and j + 1 < n:
                            j += 2
                            continue
                        if c == "'" and not sub_dq:
                            sub_sq = not sub_sq
                        elif c == '"' and not sub_sq:
                            sub_dq = not sub_dq
                        elif not sub_sq and not sub_dq:
                            if c == "(":
                                depth += 1
                            elif c == ")":
                                depth -= 1
                        j += 1
                    assert depth == 0, f"Unbalanced command substitution in line: {s!r}"
                    scan(s[i + 2 : j - 1])
                    top.append(" __SUBSHELL__ ")
                    i = j
                    continue
                top.append(ch)
                i += 1
            chunks.append("".join(top))

        scan(text)
        return chunks

    shell_delims = {";", "|", "||", "&", "&&", "(", ")"}
    shell_keywords = {"if", "elif", "then", "do", "else", "fi", "done", "while", "until", "!"}
    kubectl_and_helm_cmds: list[list[str]] = []
    for line in executable_lines:
        for chunk in _split_command_substitutions(line):
            lexer = shlex.shlex(chunk, posix=True, punctuation_chars=";|&()")
            lexer.whitespace_split = True
            current: list[str] = []
            for tok in lexer:
                if tok in shell_delims or (not current and tok in shell_keywords):
                    if current and current[0] in {"kubectl", "helm"}:
                        kubectl_and_helm_cmds.append(current)
                    current = []
                    continue
                if tok.startswith((">", "<", "2>", "1>")):
                    continue
                current.append(tok)
            if current and current[0] in {"kubectl", "helm"}:
                kubectl_and_helm_cmds.append(current)

    expected_cmd_count = (9 if existing_monitoring else 16) + (8 if access_mode in (None, "dra") else 7)
    assert len(kubectl_and_helm_cmds) == expected_cmd_count, (
        f"Expected {expected_cmd_count} kubectl/helm cmds, got {len(kubectl_and_helm_cmds)}: {kubectl_and_helm_cmds!r}"
    )
    valid_subcommands = {"get", "create", "label", "apply", "upgrade", "template"}
    for cmd_tokens in kubectl_and_helm_cmds:
        assert len(cmd_tokens) >= 2, f"Missing subcommand in {cmd_tokens!r}"
        assert cmd_tokens[1] in valid_subcommands, f"Unexpected subcommand in {cmd_tokens!r}"
        assert not any(tok in {"awk", "grep", "cut", "__SUBSHELL__"} or "$(" in tok for tok in cmd_tokens), (
            f"Leaked pipeline/subshell token in {cmd_tokens!r}"
        )
        if "--" in cmd_tokens:
            dd_idx = cmd_tokens.index("--")
            trailing_flags = [tok for tok in cmd_tokens[dd_idx + 1 :] if tok.startswith("-") and tok != "-"]
            assert not trailing_flags, f"CLI flags found after '--' in {cmd_tokens!r}: {trailing_flags!r}"

    label_cmds = [cmd for cmd in kubectl_and_helm_cmds if cmd[:2] == ["kubectl", "label"]]
    node_label_cmds = [cmd for cmd in label_cmds if "node" in cmd]
    ns_label_cmds = [cmd for cmd in label_cmds if "namespace" in cmd]
    assert len(node_label_cmds) == 1
    assert node_label_cmds[0].index("--overwrite") < node_label_cmds[0].index("--")
    assert node_label_cmds[0].index("--") < node_label_cmds[0].index("$node_name")
    if access_mode in (None, "dra"):
        assert len(ns_label_cmds) == 1
        assert ns_label_cmds[0].index("--overwrite") < ns_label_cmds[0].index("--")
        assert ns_label_cmds[0].index("--") < ns_label_cmds[0].index("$TARGET_NS")
    else:
        assert ns_label_cmds == []

    # Execute end-to-end against stub kubectl that rejects any flag token appearing after '--'.
    log_file = tmp_path / "strict.log"
    values_copy = tmp_path / "strict-values.yaml"
    sm_file = tmp_path / "strict-sm.yaml"
    dashboard_file = tmp_path / "strict-dash.yaml"
    env = _setup_accelerator_script_env(
        tmp_path,
        log_file=log_file,
        values_copy=values_copy,
        sm_file=sm_file,
        dashboard_file=dashboard_file,
        target_ns="intel-xpumd",
    )
    env["DS_ITEMS"] = ""
    proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr

    calls = log_file.read_text(encoding="utf-8").splitlines()
    if access_mode in (None, "dra"):
        assert "kubectl label --overwrite namespace -- intel-xpumd resource.kubernetes.io/admin-access=true" in calls
        assert "kubectl label --overwrite node -- gpu-node-dra intel.feature.node.kubernetes.io/gpu=true" in calls
    else:
        assert not any("resource.kubernetes.io/admin-access" in line for line in calls)
        assert "kubectl label --overwrite node -- gpu-node-plugin intel.feature.node.kubernetes.io/gpu=true" in calls


@pytest.mark.parametrize(
    ("provider_cls", "target_ns", "ready_ds", "svc_name", "sm_name", "known_port"),
    [
        (
            IntelGpuProvider,
            "intel-xpumd",
            "ext-ns\tintel-xpumanager\t2\t2\n",
            "intel-xpumanager",
            "lens-managed-xpumd",
            "29999",
        ),
        (
            NvidiaGpuProvider,
            "dcgm-exporter",
            "ext-ns\tnvidia-dcgm-exporter\t2\t2\n",
            "nvidia-dcgm-exporter",
            "lens-managed-dcgm-exporter",
            "9400",
        ),
    ],
)
def test_accelerator_install_script_fallback_sm_guards_and_idempotency(
    tmp_path: Path, provider_cls, target_ns: str, ready_ds: str, svc_name: str, sm_name: str, known_port: str
):
    import subprocess

    script = provider_cls().render_install_script(
        namespace=target_ns,
        monitoring_namespace="obs-mon",
        monitoring_release="llmd",
        existing_monitoring=False,
    )
    log_file = tmp_path / "guard.log"
    values_copy = tmp_path / "guard-values.yaml"
    sm_file = tmp_path / "guard-sm.yaml"
    dashboard_file = tmp_path / "guard-dash.yaml"
    env = _setup_accelerator_script_env(
        tmp_path,
        log_file=log_file,
        values_copy=values_copy,
        sm_file=sm_file,
        dashboard_file=dashboard_file,
        target_ns=target_ns,
    )
    env["DS_ITEMS"] = ready_ds
    env["HAS_SM_CRD"] = "1"
    env["HAS_MON_NS"] = "1"
    env["SVC_LIST_ITEMS"] = f"{svc_name}\n"
    env["SVC_LABELS_LINE"] = f"{svc_name}\t\t\n"
    env["SVC_PORTS_LINES"] = f"exporter\t{known_port}\n"

    # 1. Existing ServiceMonitor in EXISTING_NS (including our own lens-managed-* SM on re-run) -> idempotent skip
    for sm_row in (
        f"vendor-sm\t{svc_name}\t\t\n",
        f'{sm_name}\t\t\t{{"app.kubernetes.io/name":"{svc_name}"}}\n',
    ):
        sm_file.unlink(missing_ok=True)
        env["SM_ITEMS_EXISTING_NS"] = sm_row
        env["SM_ITEMS_MON_NS"] = ""
        proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert "skipping fallback ServiceMonitor" in proc.stdout
        assert not sm_file.exists()

    # 2. Existing ServiceMonitor in MONITORING_NS targeting EXISTING_NS via matchNames or any:true -> skip
    for mon_sm_row in (
        f'central-sm\t\tother-ns ext-ns\t{{"app":"{svc_name}"}}\n',
        f'central-any-sm\ttrue\t\t{{"app":"{svc_name}"}}\n',
    ):
        sm_file.unlink(missing_ok=True)
        env["SM_ITEMS_EXISTING_NS"] = ""
        env["SM_ITEMS_MON_NS"] = mon_sm_row
        proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert "skipping fallback ServiceMonitor" in proc.stdout
        assert not sm_file.exists()

    # 3. SM in MONITORING_NS targeting a DIFFERENT namespace does NOT block fallback SM,
    #    and port name containing "metrics" takes priority over known exporter port
    sm_file.unlink(missing_ok=True)
    env["SM_ITEMS_EXISTING_NS"] = ""
    env["SM_ITEMS_MON_NS"] = f'other-sm\t\tunrelated-ns\t{{"app":"{svc_name}"}}\n'
    env["SVC_PORTS_LINES"] = f"exporter\t{known_port}\nhttp-metrics\t9100\n"
    proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert sm_file.exists()
    import yaml

    assert yaml.safe_load(sm_file.read_text(encoding="utf-8"))["spec"]["endpoints"] == [
        {"port": "http-metrics", "path": "/metrics"}
    ]

    # 4. Missing CRD -> skips fallback SM cleanly
    sm_file.unlink(missing_ok=True)
    env["HAS_SM_CRD"] = "0"
    env["SM_ITEMS_MON_NS"] = ""
    proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert not sm_file.exists()

    # 5. Missing matching Service, unsafe/missing labels, or unnamed/ambiguous ports -> skips fallback SM cleanly
    env["HAS_SM_CRD"] = "1"
    for svc_list, labels_line, ports_lines in (
        ("", f"{svc_name}\t\t\n", f"metrics\t{known_port}\n"),
        (f"{svc_name}\n", "\t\t\n", f"metrics\t{known_port}\n"),
        (f"{svc_name}\n", "bad value!\t\t\n", f"metrics\t{known_port}\n"),
        (f"{svc_name}\n", f"{svc_name}\t\t\n", f"\t{known_port}\n"),
        (f"{svc_name}\n", f"{svc_name}\t\t\n", "a\t80\nb\t81\n"),
    ):
        sm_file.unlink(missing_ok=True)
        env["SVC_LIST_ITEMS"] = svc_list
        env["SVC_LABELS_LINE"] = labels_line
        env["SVC_PORTS_LINES"] = ports_lines
        proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert not sm_file.exists()


@pytest.mark.parametrize(
    ("provider_cls", "target_ns", "helm_release", "unready_base", "ready_ds"),
    [
        (IntelGpuProvider, "intel-xpumd", "xpumd", "intel-xpumd\txpumd", "ext-ns\tintel-xpumanager\t2\t2\n"),
        (
            NvidiaGpuProvider,
            "dcgm-exporter",
            "dcgm-exporter",
            "dcgm-exporter\tdcgm-exporter",
            "ext-ns\tnvidia-dcgm-exporter\t2\t2\n",
        ),
    ],
)
def test_accelerator_install_script_unready_and_mixed_daemonsets(
    tmp_path: Path, provider_cls, target_ns: str, helm_release: str, unready_base: str, ready_ds: str
):
    import subprocess

    log_file = tmp_path / "ds.log"
    values_copy = tmp_path / "ds-values.yaml"
    sm_file = tmp_path / "ds-sm.yaml"
    dashboard_file = tmp_path / "ds-dash.yaml"
    env = _setup_accelerator_script_env(
        tmp_path,
        log_file=log_file,
        values_copy=values_copy,
        sm_file=sm_file,
        dashboard_file=dashboard_file,
        target_ns=target_ns,
    )
    env["HAS_SM_CRD"] = "1"
    env["HAS_MON_NS"] = "1"

    for existing_monitoring in (False, True):
        script = provider_cls().render_install_script(
            namespace=target_ns,
            monitoring_namespace="obs-mon",
            monitoring_release="llmd",
            existing_monitoring=existing_monitoring,
        )
        # 1. Unready DSes (desired=0, ready<desired, omitted fields) in TARGET_NS and external NS fall through to Helm
        for ns_prefix in (unready_base, f"other-ns\t{unready_base.split(chr(9))[1]}"):
            for status_cols in ("\t0\t0\n", "\t2\t1\n", "\t\t\n", "\t2\t\n"):
                log_file.write_text("", encoding="utf-8")
                env["DS_ITEMS"] = f"{ns_prefix}{status_cols}"
                proc = subprocess.run(
                    ["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False
                )
                assert proc.returncode == 0, proc.stderr
                assert f"helm upgrade --install {helm_release}" in log_file.read_text(encoding="utf-8")

        # 2. Mixed DS list: unready first, ready second -> picks ready DS and skips Helm
        log_file.write_text("", encoding="utf-8")
        env["DS_ITEMS"] = f"{unready_base}\t2\t0\n{ready_ds}"
        env["SVC_LIST_ITEMS"] = ""
        proc_mixed = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
        assert proc_mixed.returncode == 0, proc_mixed.stderr
        assert "helm upgrade" not in log_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_get_status_non_admin_probes_prometheus_without_k8s_or_port_forward(monkeypatch):
    import httpx

    from llm_d_lens.monitoring.accelerator import service as accel_service
    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    async def fake_access(cluster_id, *, scope="cluster_stack", namespace="llm-d-monitoring", **_kwargs):
        assert cluster_id == "c-non-admin"
        assert scope == "accelerator"
        assert namespace == "dcgm-exporter"
        return MonitoringAccessResult(
            reachable=True,
            access_mode="non_admin",
            can_manage=False,
            context="non-admin-ctx",
        )

    async def fail_k8s(*_args, **_kwargs):
        raise AssertionError("K8s/Helm discovery must not run for non_admin status")

    monkeypatch.setattr(accel_service, "probe_monitoring_access", fake_access)
    monkeypatch.setattr(kubernetes_module, "scoped_runner", fail_k8s)
    monkeypatch.setattr(accel_service, "list_resources", fail_k8s)

    # 1. No Prometheus configured -> absent, can_manage=False, access_modes populated
    async def no_prom(cluster_id, *, allow_port_forward=True, use_cache=True):
        assert allow_port_forward is False
        assert use_cache is False
        return

    monkeypatch.setattr(accel_service, "resolve_prometheus_endpoint", no_prom)
    absent = await get_status("nvidia_gpu", "dcgm-exporter", cluster_id="c-non-admin")
    assert absent.cluster_reachable is True
    assert absent.cluster_access_mode == "non_admin"
    assert absent.can_manage is False
    assert absent.status == "absent"
    assert [m.mode for m in absent.access_modes] == ["plugin", "dra"]

    # 2. Prometheus healthy & profile metrics detected -> ready (profile-driven)
    endpoint = PrometheusEndpoint(base_url="http://10.0.0.5:30090", source="explicit_url")

    async def has_prom(cluster_id, *, allow_port_forward=True, use_cache=True):
        assert allow_port_forward is False
        return endpoint

    def make_client(_ep, *, timeout=5.0):
        del timeout
        return httpx.AsyncClient(
            base_url="http://10.0.0.5:30090",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"status": "success"})),
        )

    async def fake_check_health(_ep, *, client=None):
        return True

    async def fake_probe(client, profile):
        assert profile.id == "nvidia"
        return {
            "detected": True,
            "series_count": 8,
            "node_count": 2,
            "metrics_found": ["DCGM_FI_DEV_GPU_UTIL"],
            "source_label": "NVIDIA DCGM device telemetry",
        }

    monkeypatch.setattr(accel_service, "resolve_prometheus_endpoint", has_prom)
    monkeypatch.setattr(accel_service, "create_prometheus_client", make_client)
    monkeypatch.setattr(accel_service, "check_prometheus_health", fake_check_health)
    monkeypatch.setattr(accel_service, "probe_accelerator_metrics_via_prometheus", fake_probe)

    ready = await get_status("nvidia_gpu", "dcgm-exporter", access_mode="plugin", cluster_id="c-non-admin")
    assert ready.status == "ready"
    assert ready.access_mode == "plugin"
    assert ready.cluster_access_mode == "non_admin"
    assert ready.can_manage is False
    comp = ready.components[0]
    assert comp.name == "device_telemetry"
    assert comp.status == "ready"
    assert comp.ready == 2
    assert comp.desired == 2

    # 3. Prometheus configured but unhealthy -> degraded
    async def unhealthy(_ep, *, client=None):
        return False

    monkeypatch.setattr(accel_service, "check_prometheus_health", unhealthy)
    degraded = await get_status("nvidia_gpu", "dcgm-exporter", cluster_id="c-non-admin")
    assert degraded.status == "degraded"
    assert degraded.components[0].status == "degraded"


@pytest.mark.asyncio
async def test_get_status_401_and_admin_modes(monkeypatch):
    from llm_d_lens.monitoring.accelerator import service as accel_service
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    async def unauthorized(*_args, **_kwargs):
        return MonitoringAccessResult(
            reachable=False,
            access_mode=None,
            can_manage=False,
            auth_error=True,
            error="Unauthorized (HTTP 401)",
        )

    monkeypatch.setattr(accel_service, "probe_monitoring_access", unauthorized)
    unreach = await get_status("intel_gpu", "intel-xpumd", cluster_id="c-401")
    assert unreach.cluster_reachable is False
    assert unreach.cluster_access_mode is None
    assert unreach.can_manage is False
    assert unreach.status == "unreachable"
    assert "401" in unreach.message

    async def admin(*_args, **_kwargs):
        return MonitoringAccessResult(
            reachable=True,
            access_mode="admin",
            can_manage=True,
            context="kind-test",
        )

    monkeypatch.setattr(accel_service, "probe_monitoring_access", admin)
    _patch_runner(monkeypatch, FakeRunner(installed=True))
    admin_status = await get_status("intel_gpu", "intel-xpumd", cluster_id="c-admin")
    assert admin_status.status == "ready"
    assert admin_status.cluster_access_mode == "admin"
    assert admin_status.can_manage is True


@pytest.mark.asyncio
async def test_get_links_prefers_direct_urls_and_blocks_non_admin_port_forward(monkeypatch):
    from llm_d_lens.monitoring.accelerator import service as accel_service
    from llm_d_lens.monitoring.cluster_stack.models import ObservabilityLink
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    prom_link = ObservabilityLink(kind="prometheus", label="Prometheus", available=True, url="http://10.0.0.5:30090")
    graf_link = ObservabilityLink(kind="grafana", label="Grafana", available=True, url="http://10.0.0.5:30300")

    monkeypatch.setattr(accel_service, "get_cluster_monitoring_settings", lambda _cid: {"monitoring_namespace": "obs"})
    monkeypatch.setattr(
        accel_service,
        "resolve_direct_monitoring_links",
        lambda _cid, *, namespace="llm-d-monitoring", grafana_dashboard_path=None: (prom_link, graf_link),
    )

    async def fail(*_args, **_kwargs):
        raise AssertionError("Should not probe SSAR or port-forward when both direct links exist")

    monkeypatch.setattr(accel_service, "probe_monitoring_access", fail)
    monkeypatch.setattr(accel_service, "ensure_port_forward", fail)
    direct = await get_links("nvidia_gpu", cluster_id="c-direct")
    assert [link.url for link in direct.links] == ["http://10.0.0.5:30090", "http://10.0.0.5:30300"]

    # Non-admin with missing Grafana link must NEVER call ensure_port_forward
    monkeypatch.setattr(
        accel_service,
        "resolve_direct_monitoring_links",
        lambda _cid, *, namespace="llm-d-monitoring", grafana_dashboard_path=None: (prom_link, None),
    )

    async def non_admin(*_args, **_kwargs):
        return MonitoringAccessResult(reachable=True, access_mode="non_admin", can_manage=False)

    monkeypatch.setattr(accel_service, "probe_monitoring_access", non_admin)
    partial = await get_links("nvidia_gpu", cluster_id="c-partial")
    assert partial.links[0].available is True
    assert partial.links[0].url == "http://10.0.0.5:30090"
    assert partial.links[1].available is False
    assert "administrator" in (partial.links[1].message or "").lower()


@pytest.mark.asyncio
async def test_grafana_dashboard_path_supports_profile_driven_nvidia_without_hardcoded_vendor(monkeypatch):
    async def fake_list_resources(resource, *, namespace=None, selector=None, cluster_id=None):
        assert resource == "configmaps"
        return [
            {
                "kind": "ConfigMap",
                "metadata": {"name": "node-exporter-dashboard"},
                "data": {"dashboard.json": json.dumps({"uid": "nodeUid", "title": "Node Exporter / Nodes"})},
            },
            {
                "kind": "ConfigMap",
                "metadata": {"name": "intel-gpu-dashboard"},
                "data": {"dashboard.json": json.dumps({"uid": "intelUid", "title": "Intel GPU"})},
            },
            {
                "kind": "ConfigMap",
                "metadata": {"name": "dcgm-exporter-dashboard"},
                "data": {"dashboard.json": json.dumps({"uid": "dcgmUid1", "title": "NVIDIA DCGM Exporter"})},
            },
        ]

    monkeypatch.setattr("llm_d_lens.monitoring.accelerator.service.list_resources", fake_list_resources)
    path = await _grafana_dashboard_path("llm-d-monitoring", "c1", accelerator="nvidia_gpu")
    assert path == "/d/dcgmUid1/nvidia-dcgm-exporter"


@pytest.mark.asyncio
async def test_admin_script_recheck_and_non_admin_install_guard(monkeypatch):
    from unittest.mock import AsyncMock

    from llm_d_lens.monitoring.accelerator import service as accel_service
    from llm_d_lens.monitoring.accelerator.errors import AcceleratorError
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    monkeypatch.setattr(
        accel_service,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_mode": "existing", "monitoring_namespace": "obs-mon"},
    )
    script_res = accel_service.get_admin_script("nvidia_gpu", cluster_id="c-1")
    _assert_bash_syntax(script_res.script)
    assert script_res.filename == "lens-accelerator-nvidia_gpu-install.sh"
    assert script_res.monitoring_namespace == "obs-mon"
    assert script_res.warnings  # existing monitoring notice
    assert "skipping dcgm-exporter install" in script_res.script

    invalidated = []
    monkeypatch.setattr(
        accel_service, "invalidate_monitoring_access_cache", lambda cid: invalidated.append(("access", cid))
    )
    monkeypatch.setattr(
        accel_service, "invalidate_prometheus_endpoint_cache", lambda cid: invalidated.append(("prom", cid))
    )
    monkeypatch.setattr(
        accel_service, "read_and_persist_monitoring_marker", AsyncMock(return_value={"status": "complete"})
    )
    monkeypatch.setattr(
        accel_service,
        "probe_monitoring_access",
        AsyncMock(return_value=MonitoringAccessResult(reachable=True, access_mode="non_admin", can_manage=False)),
    )
    monkeypatch.setattr(accel_service, "resolve_prometheus_endpoint", AsyncMock(return_value=None))

    recheck_res = await accel_service.recheck_accelerator("nvidia_gpu", "dcgm-exporter", cluster_id="c-1")
    assert recheck_res.marker_found is True
    assert recheck_res.cluster_access_mode == "non_admin"
    assert recheck_res.can_manage is False
    assert ("access", "c-1") in invalidated and ("prom", "c-1") in invalidated

    with pytest.raises(AcceleratorError) as exc_info:
        await accel_service.install(AcceleratorInstallRequest(), idempotency_key="k1", cluster_id="c-1")
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "INSUFFICIENT_PERMISSIONS"


def test_accelerator_router_enforces_cluster_access_on_routes(monkeypatch):
    import importlib
    from unittest.mock import AsyncMock

    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.auth.permissions import BUILTIN_ROLE_PERMISSIONS
    from llm_d_lens.monitoring.accelerator import service as accel_service

    accel_router = importlib.import_module("llm_d_lens.monitoring.accelerator.router")

    principal = Principal(
        user_id="u1",
        username="u1",
        bindings=(
            RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c-allowed"),
            RoleBinding(role="viewer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c-readonly"),
        ),
        role_permissions={
            "maintainer": BUILTIN_ROLE_PERMISSIONS["maintainer"],
            "viewer": frozenset({"monitoring:accelerator:read"}),
        },
    )
    monkeypatch.setattr(accel_router, "current_principal", lambda _req: principal)
    monkeypatch.setattr(
        accel_service,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_mode": "managed", "monitoring_namespace": "llm-d-monitoring"},
    )
    monkeypatch.setattr(accel_router, "recheck_accelerator", AsyncMock(side_effect=AssertionError("should not run")))

    with TestClient(app) as client:
        # Omitted or empty cluster_id rejected for cluster-scoped principals (no ambient kubeconfig fallback)
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/status").status_code == 403
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/status?cluster_id=").status_code == 403
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/links").status_code == 403
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/admin-script").status_code == 403
        assert client.post("/api/v1/monitoring/accelerators/nvidia_gpu/recheck").status_code == 403
        assert (
            client.post(
                "/api/v1/monitoring/accelerators/nvidia_gpu/installations/preflight",
                json={"accelerator": "nvidia_gpu", "access_mode": "plugin", "namespace": "dcgm-exporter"},
            ).status_code
            == 403
        )

        # Inaccessible cluster rejected
        assert (
            client.get("/api/v1/monitoring/accelerators/nvidia_gpu/admin-script?cluster_id=c-forbidden").status_code
            == 403
        )
        assert (
            client.post("/api/v1/monitoring/accelerators/nvidia_gpu/recheck?cluster_id=c-forbidden").status_code == 403
        )
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/status?cluster_id=c-forbidden").status_code == 403
        assert client.get("/api/v1/monitoring/accelerators/nvidia_gpu/links?cluster_id=c-forbidden").status_code == 403

        # Cross-cluster role escalation rejected: viewer on c-readonly has read but not install
        assert (
            client.post(
                "/api/v1/monitoring/accelerators/nvidia_gpu/installations/preflight?cluster_id=c-readonly",
                json={"accelerator": "nvidia_gpu", "access_mode": "plugin", "namespace": "dcgm-exporter"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/monitoring/accelerators/nvidia_gpu/installations?cluster_id=c-readonly",
                json={"accelerator": "nvidia_gpu", "access_mode": "plugin", "namespace": "dcgm-exporter"},
            ).status_code
            == 403
        )

        allowed_script = client.get("/api/v1/monitoring/accelerators/nvidia_gpu/admin-script?cluster_id=c-allowed")
        assert allowed_script.status_code == 200
        assert allowed_script.json()["accelerator"] == "nvidia_gpu"


@pytest.mark.asyncio
async def test_accelerator_missing_counts_and_custom_monitoring_namespace_end_to_end(monkeypatch):
    import importlib
    from unittest.mock import AsyncMock

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.monitoring.accelerator import service as accel_service
    from llm_d_lens.monitoring.accelerator.models import AcceleratorOperationResponse
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    accel_router = importlib.import_module("llm_d_lens.monitoring.accelerator.router")

    # 1. Missing grafana_dashboard and prometheus_service_monitor report 0/1 (not None/-)
    _patch_runner(monkeypatch, FakeRunner(installed=False))
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda cid: (
            {"monitoring_mode": "managed", "monitoring_namespace": "llm-d-monitoring-123454"}
            if cid == "2790b2ae"
            else None
        ),
    )
    monkeypatch.setattr(
        accel_service,
        "get_cluster_monitoring_settings",
        cluster_registry.get_cluster_monitoring_settings,
    )
    absent_intel = await IntelGpuProvider().discover_status("intel-xpumd", "2790b2ae")
    by_name = {c.name: c for c in absent_intel.components}
    assert (by_name["prometheus_service_monitor"].ready, by_name["prometheus_service_monitor"].desired) == (0, 1)
    assert (by_name["grafana_dashboard"].ready, by_name["grafana_dashboard"].desired) == (0, 1)

    # 2. Live 2790b2ae scenario: cluster monitoring_namespace="llm-d-monitoring-123454" is ready,
    #    preflight + install resolve "llm-d-monitoring-123454" and pass grafana.namespace + configure_monitoring
    runner = FakeRunner(installed=True)
    _patch_runner(monkeypatch, runner)
    monkeypatch.setattr(
        accel_service,
        "probe_monitoring_access",
        AsyncMock(
            return_value=MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )

    seen_stack_ns: list[str | None] = []

    async def fake_stack_status(namespace=None, *, cluster_id=None):
        seen_stack_ns.append(namespace)
        is_ready = namespace == "llm-d-monitoring-123454"
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
            namespace=namespace or "llm-d-monitoring",
            release=HelmReleaseSummary(name="llmd", status="deployed" if is_ready else None),
            status="ready" if is_ready else "absent",
            message="ready" if is_ready else "absent",
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(stack_service, "get_status", fake_stack_status)

    pf = await accel_service.preflight(
        AcceleratorInstallRequest(accelerator="intel_gpu", namespace="intel-xpumd", access_mode="dra"),
        cluster_id="2790b2ae",
    )
    assert pf.allowed is True
    assert seen_stack_ns == ["llm-d-monitoring-123454"]
    assert "grafana.namespace=llm-d-monitoring-123454" in pf.command_preview

    # Explicit monitoring_namespace on AcceleratorInstallRequest overrides cluster default
    seen_stack_ns.clear()
    pf_override = await accel_service.preflight(
        AcceleratorInstallRequest(
            accelerator="intel_gpu",
            namespace="intel-xpumd",
            access_mode="dra",
            monitoring_namespace="other-mon-ns",
        ),
        cluster_id="2790b2ae",
    )
    assert seen_stack_ns == ["other-mon-ns"]
    assert pf_override.allowed is False
    assert "grafana.namespace=other-mon-ns" in pf_override.command_preview

    # Install wires monitoring_namespace into argv, post_install (configure_monitoring), and verifier
    captured_install: dict = {}

    async def fake_op_create(
        request, *, context, argv, verifier, post_install=None, idempotency_key=None, cluster_id=None
    ):
        captured_install["argv"] = argv
        captured_install["post_install"] = post_install
        captured_install["verifier"] = verifier
        return AcceleratorOperationResponse(
            operation_id="c" * 32,
            status="queued",
            phase="preflight",
            accelerator=request.accelerator,
            access_mode=request.access_mode,
            namespace=request.namespace,
        )

    configured_ns_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(accel_service.operations, "create", fake_op_create)
    monkeypatch.setattr(accel_service, "_ensure_namespace", AsyncMock())
    monkeypatch.setattr(IntelGpuProvider, "prepare_nodes", AsyncMock())
    monkeypatch.setattr(
        IntelGpuProvider,
        "configure_monitoring",
        lambda _self, mon_ns, cid: configured_ns_calls.append((mon_ns, cid)) or _noop_async(),
    )

    op = await accel_service.install(
        AcceleratorInstallRequest(
            accelerator="intel_gpu",
            namespace="intel-xpumd",
            access_mode="dra",
            monitoring_namespace="llm-d-monitoring-123454",
        ),
        idempotency_key="repair-1",
        cluster_id="2790b2ae",
    )
    assert op.operation_id == "c" * 32
    assert "grafana.namespace=llm-d-monitoring-123454" in captured_install["argv"]
    assert captured_install["post_install"] is not None
    await captured_install["post_install"]()
    assert configured_ns_calls == [("llm-d-monitoring-123454", "2790b2ae")]

    # 3. Router forwards optional ?monitoring_namespace= on status, links, admin-script, recheck
    monkeypatch.setattr(accel_router, "current_principal", lambda _req: None)
    seen_route_mon_ns: list[tuple[str, str | None]] = []
    orig_get_status = accel_service.get_status

    async def spy_get_status(acc, ns, mode=None, *, monitoring_namespace=None, cluster_id=None):
        seen_route_mon_ns.append(("status", monitoring_namespace))
        return await orig_get_status(acc, ns, mode, monitoring_namespace=monitoring_namespace, cluster_id=cluster_id)

    monkeypatch.setattr(accel_router, "get_status", spy_get_status)
    monkeypatch.setattr(accel_service, "read_and_persist_monitoring_marker", AsyncMock(return_value=None))

    with TestClient(app) as client:
        r1 = client.get(
            "/api/v1/monitoring/accelerators/intel_gpu/status"
            "?cluster_id=2790b2ae&monitoring_namespace=llm-d-monitoring-123454"
        )
        assert r1.status_code == 200
        assert seen_route_mon_ns == [("status", "llm-d-monitoring-123454")]

        r_bad = client.get(
            "/api/v1/monitoring/accelerators/intel_gpu/links?cluster_id=2790b2ae&monitoring_namespace=Bad_NS"
        )
        assert r_bad.status_code == 422

        r_recheck = client.post(
            "/api/v1/monitoring/accelerators/intel_gpu/recheck"
            "?cluster_id=2790b2ae&monitoring_namespace=llm-d-monitoring-123454"
        )
        assert r_recheck.status_code == 200


async def _noop_async() -> None:
    return None


def test_intel_render_install_script_skip_helm_reconciles_missing_grafana_dashboard(tmp_path: Path):
    import subprocess

    import yaml

    provider = IntelGpuProvider()
    script = provider.render_install_script(
        namespace="intel-xpumd",
        access_mode="dra",
        monitoring_namespace="llm-d-monitoring-123454",
        monitoring_release="llmd",
        existing_monitoring=False,
    )
    _assert_bash_syntax(script)

    log_file = tmp_path / "dash-reconcile.log"
    sm_file = tmp_path / "dash-sm.yaml"
    dashboard_file = tmp_path / "dash-cm.yaml"

    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "kubectl $*" >> {str(log_file)!r}
if [[ "$1" == "get" && "$2" == "daemonset" ]]; then
  printf 'intel-xpumd\\txpumd\\t2\\t2\\n'
  exit 0
fi
if [[ "$1" == "get" && "$2" == "crd" ]]; then
  exit 0
fi
if [[ "$1" == "get" && "$2" == "servicemonitor" ]]; then
  printf 'xpumd\\txpumd\\t\\t\\n'
  exit 0
fi
if [[ "$1" == "get" && "$2" == "namespace" ]]; then
  exit 0
fi
if [[ "$1" == "get" && "$2" == "configmap" ]]; then
  if [[ "$*" == *"--all-namespaces"* ]]; then
    printf '%s' "${{ALL_NS_CMS:-}}"
    exit 0
  fi
  if [[ "$3" == "-n" ]]; then
    printf '%s' "${{MON_NS_CMS:-}}"
    exit 0
  fi
  if [[ "$*" == *"dashboard\\\\.json"* ]]; then
    printf '%s' "${{SRC_CM_DASH_JSON:-}}"
    exit 0
  fi
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  payload="$(cat)"
  if [[ "$payload" == *"kind: ServiceMonitor"* ]]; then
    printf '%s\\n' "$payload" > {str(sm_file)!r}
  else
    printf '%s\\n' "$payload" > {str(dashboard_file)!r}
  fi
  exit 0
fi
exit 0
"""
    helm_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "helm $*" >> {str(log_file)!r}
if [[ "$1" == "template" ]]; then
  cat <<'EOF'
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: xpumd
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: xpumd-templated-dashboard
  uid: "1111-2222"
  resourceVersion: "999"
  creationTimestamp: "2026-01-01T00:00:00Z"
  ownerReferences:
  - apiVersion: apps/v1
    kind: DaemonSet
    name: xpumd
    uid: "3333"
  annotations:
    meta.helm.sh/release-name: xpumd
    meta.helm.sh/release-namespace: intel-xpumd
  labels:
    grafana_dashboard: "1"
    helm.sh/chart: xpumd-2.1.0
    app.kubernetes.io/managed-by: Helm
data:
  dashboard.json: '{{"uid":"xpumd-tmpl","title":"Intel XPU Templated"}}'
EOF
  exit 0
fi
exit 0
"""
    env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script, helm_body=helm_script)

    # 1. Dashboard already exists in MONITORING_NS -> idempotent skip
    dashboard_file.unlink(missing_ok=True)
    env["MON_NS_CMS"] = "intel-xpumd-dashboard\n"
    proc_present = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_present.returncode == 0, proc_present.stderr
    assert not dashboard_file.exists()

    # 2. Dashboard exists in another namespace ("monitoring") -> copied into llm-d-monitoring-123454 cleanly
    dashboard_file.unlink(missing_ok=True)
    env["MON_NS_CMS"] = ""
    env["ALL_NS_CMS"] = "monitoring\tintel-xpumd-dashboard\n"
    env["SRC_CM_DASH_JSON"] = '{"uid":"xpumd-copied","title":"Intel XPU Manager"}'
    proc_copy = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_copy.returncode == 0, proc_copy.stderr
    assert dashboard_file.exists()
    copied_cm = yaml.safe_load(dashboard_file.read_text(encoding="utf-8"))
    assert copied_cm["kind"] == "ConfigMap"
    assert copied_cm["metadata"]["name"] == "intel-xpumd-dashboard"
    assert copied_cm["metadata"]["namespace"] == "llm-d-monitoring-123454"
    assert copied_cm["metadata"]["labels"] == {"grafana_dashboard": "1", "app.kubernetes.io/managed-by": "lens"}
    for forbidden_key in ("uid", "resourceVersion", "creationTimestamp", "ownerReferences", "annotations"):
        assert forbidden_key not in copied_cm["metadata"]
    assert json.loads(copied_cm["data"]["dashboard.json"])["uid"] == "xpumd-copied"

    # 3. Dashboard absent cluster-wide (with 2-space ownerReferences & omitted metadata.namespace) ->
    #    falls back to helm template, injects namespace, passes -n $MONITORING_NS, and strips ownership/Helm metadata
    dashboard_file.unlink(missing_ok=True)
    log_file.write_text("", encoding="utf-8")
    env["MON_NS_CMS"] = ""
    env["ALL_NS_CMS"] = ""
    env["SRC_CM_DASH_JSON"] = ""
    proc_tmpl = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_tmpl.returncode == 0, proc_tmpl.stderr
    assert "kubectl apply -n llm-d-monitoring-123454 -f -" in log_file.read_text(encoding="utf-8")
    assert dashboard_file.exists()
    tmpl_cm = yaml.safe_load(dashboard_file.read_text(encoding="utf-8"))
    assert tmpl_cm["kind"] == "ConfigMap"
    assert tmpl_cm["metadata"]["name"] == "xpumd-templated-dashboard"
    assert tmpl_cm["metadata"]["namespace"] == "llm-d-monitoring-123454"
    assert tmpl_cm["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert tmpl_cm["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "lens"
    assert "helm.sh/chart" not in tmpl_cm["metadata"]["labels"]
    for forbidden_key in ("uid", "resourceVersion", "creationTimestamp", "ownerReferences"):
        assert forbidden_key not in tmpl_cm["metadata"]
    assert not tmpl_cm["metadata"].get("annotations")
