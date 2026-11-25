"""Tests for cluster monitoring stack discovery and API contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.infrastructure.process import CommandResult
from llm_d_lens.monitoring.cluster_stack.discovery import discover_cluster_stack
from llm_d_lens.monitoring.cluster_stack.models import ClusterStackInstallRequest
from llm_d_lens.monitoring.cluster_stack.service import validate_namespace


def _result(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(tuple(argv), returncode, stdout, stderr)


class FakeRunner:
    def __init__(
        self,
        *,
        installed: bool,
        include_pods: bool = False,
        use_labels: bool = True,
        drop_kube_state: bool = False,
        external_kube_state: bool = False,
    ) -> None:
        self.installed = installed
        self.include_pods = include_pods
        self.use_labels = use_labels
        self.drop_kube_state = drop_kube_state
        self.external_kube_state = external_kube_state

    def executable(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
        del timeout
        command = " ".join(argv)
        if command == "kubectl config current-context":
            return _result(argv, "kind-test\n")
        if command == "kubectl cluster-info":
            return _result(argv, "Kubernetes control plane is running")
        if command.startswith("kubectl get clusterversion"):
            return _result(argv, stderr="not found", returncode=1)
        if command.startswith("helm status"):
            if not self.installed:
                return _result(argv, stderr="Error: release: not found", returncode=1)
            return _result(
                argv,
                json.dumps(
                    {
                        "version": 2,
                        "info": {"status": "deployed"},
                        "chart": "kube-prometheus-stack-72.0.0",
                    }
                ),
            )
        if command.startswith("helm list"):
            return _result(argv, json.dumps([{"chart": "kube-prometheus-stack-72.0.0"}]))
        if "app.kubernetes.io/instance=llmd" in command:
            items = []
            if self.installed:
                if self.include_pods:
                    items = [self._pod("llmd-grafana-85c664997b-s6gxk", 3, 3)]
                    if not self.drop_kube_state:
                        items.append(self._pod("llmd-kube-state-metrics-6d4f9c8b7-abc12", 1, 1))
                else:
                    items = [
                        self._deployment("llmd-kube-prometheus-stack-prometheus"),
                        self._deployment("llmd-grafana"),
                    ]
                    if not self.drop_kube_state:
                        items.append(self._deployment("llmd-kube-state-metrics"))
            return _result(argv, json.dumps({"items": items}))
        if "app.kubernetes.io/name in (prometheus,alertmanager)" in command:
            items = []
            if self.installed:
                if self.include_pods:
                    items = [
                        self._pod("prometheus-llmd-kube-prometheus-stack-prometheus-0", 2, 2, "prometheus"),
                        self._pod("alertmanager-llmd-kube-prometheus-stack-alertmanager-0", 2, 2, "alertmanager"),
                    ]
                else:
                    items = [
                        self._statefulset("prometheus-llmd-kube-prometheus-stack-prometheus", "prometheus"),
                        self._statefulset("alertmanager-llmd-kube-prometheus-stack-alertmanager", "alertmanager"),
                    ]
            return _result(argv, json.dumps({"items": items}))
        if "app.kubernetes.io/name=prometheus-operator" in command:
            items = [self._deployment("shared-prometheus-operator")] if self.installed and self.use_labels else []
            return _result(argv, json.dumps({"items": items}))
        if "app.kubernetes.io/name=kube-state-metrics" in command:
            items = (
                [self._deployment("shared-kube-state-metrics")]
                if self.installed and self.external_kube_state and self.use_labels
                else []
            )
            return _result(argv, json.dumps({"items": items}))
        if "app=node-exporter" in command:
            items = [self._daemonset("shared-node-exporter")] if self.installed and self.use_labels else []
            return _result(argv, json.dumps({"items": items}))
        if "--all-namespaces" in command and "pods,deployments" in command and "-l" not in command:
            items = [self._deployment("shared-prometheus-operator")] if self.installed else []
            if self.installed and self.external_kube_state:
                items.append(self._deployment("shared-kube-state-metrics"))
            return _result(argv, json.dumps({"items": items}))
        if "--all-namespaces" in command and "pods,daemonsets" in command and "-l" not in command:
            items = [self._daemonset("shared-node-exporter")] if self.installed else []
            return _result(argv, json.dumps({"items": items}))
        if "grafana_dashboard=1" in command:
            items = [{"kind": "ConfigMap", "metadata": {"name": "llm-d-dashboard"}}] if self.installed else []
            return _result(argv, json.dumps({"items": items}))
        if command.startswith("kubectl get crd"):
            return _result(argv, "customresourcedefinition") if self.installed else _result(argv, returncode=1)
        raise AssertionError(f"Unexpected command: {command}")

    @staticmethod
    def _deployment(name: str) -> dict:
        return {
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {"replicas": 1},
            "status": {"replicas": 1, "readyReplicas": 1},
        }

    @staticmethod
    def _daemonset(name: str) -> dict:
        return {
            "kind": "DaemonSet",
            "metadata": {"name": name},
            "status": {"numberReady": 1, "desiredNumberScheduled": 1},
        }

    @staticmethod
    def _statefulset(name: str, label: str) -> dict:
        return {
            "kind": "StatefulSet",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/name": label}},
            "spec": {"replicas": 1},
            "status": {"replicas": 1, "readyReplicas": 1},
        }

    @staticmethod
    def _pod(name: str, ready: int, total: int, label: str | None = None) -> dict:
        metadata: dict = {"name": name}
        if label:
            metadata["labels"] = {"app.kubernetes.io/name": label}
        return {
            "kind": "Pod",
            "metadata": metadata,
            "spec": {"containers": [{"name": f"container-{i}"} for i in range(total)]},
            "status": {"containerStatuses": [{"name": f"container-{i}", "ready": i < ready} for i in range(total)]},
        }


@pytest.mark.asyncio
async def test_discovery_reports_absent_stack():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=False))

    assert status.status == "absent"
    assert status.cluster.context == "kind-test"
    assert status.release.name == "llmd"


@pytest.mark.asyncio
async def test_discovery_reports_ready_with_external_operator():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True))

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["prometheus"].status == "ready"
    assert components["grafana"].status == "ready"
    assert components["prometheus_operator"].status == "external"
    assert components["node_exporter"].status == "external"
    assert components["node_exporter"].source == "cluster"
    assert components["alertmanager"].status == "ready"
    assert components["monitoring_crds"].ready == 3


@pytest.mark.asyncio
async def test_discovery_reports_degraded_when_one_of_eight_components_is_not_ready():
    # Regression test: the aggregate status computation used to only look at
    # prometheus/grafana/operator/crds/dashboards, silently ignoring
    # alertmanager/kube_state_metrics/node_exporter entirely. That meant a
    # cluster with e.g. 7 of its 8 tracked components ready (kube-state-metrics
    # missing here) still reported the whole stack as "ready" instead of
    # "degraded" -- exactly the "shows 7/8 but still says Ready" bug.
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, drop_kube_state=True))

    components = {component.name: component for component in status.components}
    ready_count = sum(1 for component in components.values() if component.status in {"ready", "external"})
    assert ready_count == 7
    assert len(components) == 8
    assert components["kube_state_metrics"].status == "missing"
    assert status.status == "degraded"


@pytest.mark.asyncio
async def test_discovery_reports_external_when_labels_absent():
    # Operators/node-exporters installed without the standard labels are still
    # discovered cluster-wide by name, mirroring the installer's detection.
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, use_labels=False))

    assert status.status == "ready"
    components = {component.name: component for component in status.components}
    assert components["prometheus_operator"].status == "external"
    assert components["prometheus_operator"].source == "cluster"
    assert components["node_exporter"].status == "external"
    assert components["node_exporter"].source == "cluster"


@pytest.mark.asyncio
async def test_discovery_reports_pod_container_counts():
    status = await discover_cluster_stack("llm-d-monitoring", FakeRunner(installed=True, include_pods=True))
    components = {component.name: component for component in status.components}
    assert components["prometheus"].status == "ready"
    assert (components["prometheus"].ready, components["prometheus"].desired) == (2, 2)
    assert (components["alertmanager"].ready, components["alertmanager"].desired) == (2, 2)
    assert (components["grafana"].ready, components["grafana"].desired) == (3, 3)


@pytest.mark.asyncio
async def test_discovery_reports_expected_dashboard_counts():
    runner = FakeRunner(installed=True)

    ready_status = await discover_cluster_stack("llm-d-monitoring", runner, expected_dashboards=["llm-d-dashboard"])
    ready_components = {component.name: component for component in ready_status.components}
    assert ready_components["llm_d_dashboards"].status == "ready"
    assert ready_components["llm_d_dashboards"].ready == 1
    assert ready_components["llm_d_dashboards"].desired == 1
    assert ready_status.status == "ready"

    missing_status = await discover_cluster_stack(
        "llm-d-monitoring",
        runner,
        expected_dashboards=["llm-d-vllm-overview", "llm-d-sglang-overview"],
    )
    missing_components = {component.name: component for component in missing_status.components}
    assert missing_components["llm_d_dashboards"].status == "missing"
    assert missing_components["llm_d_dashboards"].ready == 0
    assert missing_components["llm_d_dashboards"].desired == 2
    assert missing_status.status == "degraded"


def test_install_request_forbids_unknown_fields_and_validates_namespace():
    with pytest.raises(ValueError):
        ClusterStackInstallRequest.model_validate({"namespace": "Invalid_Name"})
    with pytest.raises(ValueError):
        ClusterStackInstallRequest.model_validate({"unexpected": True})
    assert validate_namespace("llm-d-monitoring") == "llm-d-monitoring"


def test_shared_api_registers_scoped_monitoring_routes():
    client = TestClient(app)
    paths = client.get("/api/simulation/openapi.json").json()["paths"]

    assert "/api/v1/monitoring/cluster-stack/status" in paths
    assert "/api/v1/monitoring/cluster-stack/installations" in paths
    assert "/api/v1/monitoring/cluster-stack/operations/{operation_id}" in paths
    assert "/api/v1/monitoring/cluster-stack/links" in paths


def test_operation_path_cannot_escape_store(tmp_path: Path):
    from llm_d_lens.monitoring.cluster_stack.models import ClusterStackOperationResponse
    from llm_d_lens.monitoring.cluster_stack.operations import (
        ClusterStackOperationManager,
        OperationStore,
    )

    store = OperationStore(tmp_path)
    assert store.load("../../etc/passwd") is None

    manager = ClusterStackOperationManager(store)
    operation = ClusterStackOperationResponse(
        operation_id="a" * 32,
        status="running",
        phase="installing_chart",
        namespace="llm-d-monitoring",
    )
    manager._append(operation, "\x1b[32mGrafana admin password: admin\x1b[0m")
    assert operation.logs[0].message == "Grafana admin password: [REDACTED]"

    store.save(operation)
    recovered_manager = ClusterStackOperationManager(store)
    recovered = recovered_manager.get(operation.operation_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error is not None
    assert recovered.error.code == "SERVICE_RESTARTED"


@pytest.mark.asyncio
async def test_install_operation_requires_ready_verification(tmp_path: Path):
    import asyncio
    from datetime import UTC, datetime

    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.cluster_stack.operations import ClusterStackOperationManager, OperationStore

    script = tmp_path / "install.sh"
    script.write_text(
        "#!/bin/sh\necho 'Installing Prometheus stack'\necho 'Grafana admin password: admin'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o100)
    manager = ClusterStackOperationManager(OperationStore(tmp_path / "operations"))

    async def verify(namespace: str) -> ClusterStackStatusResponse:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="kind-test", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        ClusterStackInstallRequest(),
        context="kind-test",
        script=script,
        verifier=verify,
        idempotency_key="request-1",
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.phase == "completed"
    assert all("admin" not in entry.message.rsplit(":", 1)[-1].strip() for entry in completed.logs)
    persisted = manager.store.load(created.operation_id)
    assert persisted is not None
    assert persisted.status == "succeeded"


@pytest.mark.asyncio
async def test_reinstall_runs_uninstall_then_install(tmp_path: Path):
    import asyncio
    from datetime import UTC, datetime

    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.cluster_stack.operations import ClusterStackOperationManager, OperationStore

    script = tmp_path / "install.sh"
    script.write_text('#!/bin/sh\necho "argv: $*"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o100)
    manager = ClusterStackOperationManager(OperationStore(tmp_path / "operations"))

    async def verify(namespace: str) -> ClusterStackStatusResponse:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="kind-test", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed"),
            status="ready",
            message="ready",
            observed_at=datetime.now(UTC),
        )

    created = await manager.create(
        ClusterStackInstallRequest(reinstall=True),
        context="kind-test",
        script=script,
        verifier=verify,
        idempotency_key="reinstall-1",
    )
    for _ in range(50):
        completed = manager.get(created.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status == "succeeded"
    argv_lines = [entry.message for entry in completed.logs if entry.message.startswith("argv:")]
    assert len(argv_lines) == 2
    assert "--uninstall" in argv_lines[0]
    assert "--uninstall" not in argv_lines[1]


@pytest.mark.asyncio
async def test_cluster_links_open_tunnels(monkeypatch):
    from llm_d_lens.monitoring.cluster_stack import service as cluster_service
    from llm_d_lens.monitoring.cluster_stack.service import get_links

    class ServicesRunner:
        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            assert "services" in " ".join(argv)
            return _result(
                argv,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "llmd-grafana"},
                                "spec": {"ports": [{"port": 80}]},
                            },
                            {
                                "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                "spec": {"ports": [{"port": 9090}, {"port": 8080}]},
                            },
                        ]
                    }
                ),
            )

    forwarded = {}

    class FakeForward:
        local_port = 18080

    async def fake_ensure(namespace: str, service: str, remote_port: int, **kwargs) -> FakeForward:
        del kwargs
        forwarded[(namespace, service, remote_port)] = 18080
        return FakeForward()

    monkeypatch.setattr(cluster_service, "_runner", lambda _cluster_id: ServicesRunner())
    monkeypatch.setattr(cluster_service, "ensure_port_forward", fake_ensure)

    response = await get_links("llm-d-monitoring")
    links = {link.kind: link for link in response.links}

    assert links["prometheus"].available is True
    assert links["prometheus"].service == "llmd-kube-prometheus-stack-prometheus"
    assert links["prometheus"].port == 9090
    assert links["prometheus"].local_port == 18080
    assert links["grafana"].available is True
    assert links["grafana"].service == "llmd-grafana"
    assert links["grafana"].port == 80
    assert links["grafana"].local_port == 18080
    assert ("llm-d-monitoring", "llmd-kube-prometheus-stack-prometheus", 9090) in forwarded
    assert ("llm-d-monitoring", "llmd-grafana", 80) in forwarded


@pytest.mark.asyncio
async def test_cluster_links_reports_unavailable_when_services_missing(monkeypatch):
    from llm_d_lens.monitoring.cluster_stack import service as cluster_service
    from llm_d_lens.monitoring.cluster_stack.service import get_links

    class EmptyServicesRunner:
        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            return _result(argv, json.dumps({"items": []}))

    monkeypatch.setattr(cluster_service, "_runner", lambda _cluster_id: EmptyServicesRunner())

    response = await get_links("llm-d-monitoring")
    assert {link.kind for link in response.links} == {"prometheus", "grafana"}
    assert all(link.available is False for link in response.links)
    assert all(link.message for link in response.links)


def _assert_bash_syntax(script: str) -> None:
    import subprocess

    proc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


def _setup_stub_bin(
    tmp_path: Path, *, kubectl_body: str, helm_body: str = "#!/usr/bin/env bash\nexit 0\n"
) -> tuple[Path, dict[str, str]]:
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("kubectl", kubectl_body), ("helm", helm_body)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return bin_dir, env


def test_metrics_reader_manifests_honors_monitoring_namespace():
    from llm_d_lens.monitoring.deployment.service import (
        CENTRAL_NAMESPACE,
        METRICS_READER_BINDING,
        METRICS_READER_ROLE,
        _metrics_reader_manifests,
    )

    default_manifests = _metrics_reader_manifests("prom-sa")
    assert default_manifests[0]["metadata"]["name"] == METRICS_READER_ROLE
    assert default_manifests[1]["metadata"]["name"] == METRICS_READER_BINDING
    assert default_manifests[1]["subjects"][0]["namespace"] == CENTRAL_NAMESPACE

    custom_manifests = _metrics_reader_manifests("custom-sa", monitoring_namespace="custom-mon")
    assert custom_manifests[1]["subjects"][0]["name"] == "custom-sa"
    assert custom_manifests[1]["subjects"][0]["namespace"] == "custom-mon"


def test_render_monitoring_stack_script_validation_and_contract():
    from llm_d_lens.monitoring.cluster_stack.service import (
        KUBE_PROMETHEUS_STACK_CHART,
        KUBE_PROMETHEUS_STACK_RELEASE,
        KUBE_PROMETHEUS_STACK_REPO,
        KUBE_PROMETHEUS_STACK_VERSION,
        LLM_D_OVERVIEW_DASHBOARD,
        render_monitoring_stack_script,
    )

    assert KUBE_PROMETHEUS_STACK_CHART == "kube-prometheus-stack"
    assert KUBE_PROMETHEUS_STACK_VERSION == "72.6.2"
    assert KUBE_PROMETHEUS_STACK_RELEASE == "llmd"
    assert KUBE_PROMETHEUS_STACK_REPO == "https://prometheus-community.github.io/helm-charts"
    assert any(item.get("name") == "datasource" for item in LLM_D_OVERVIEW_DASHBOARD["templating"]["list"])
    assert LLM_D_OVERVIEW_DASHBOARD["panels"][0]["datasource"]["uid"] == "${datasource}"

    script = render_monitoring_stack_script(
        namespace="obs-mon",
        prometheus_node_port=30900,
        grafana_node_port=30300,
        extra_dashboards={"llm-d-custom": '{"title":"Custom","panels":[]}'},
    )
    _assert_bash_syntax(script)
    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert "trap 'rm -rf -- \"$TMP_DIR\"' EXIT" in script
    assert "~/.cache/lens/llm-d" not in script
    assert "jq" not in script
    assert "python" not in script
    assert " --set" not in script
    assert "serviceMonitorSelectorNilUsesHelmValues: false" in script
    assert "searchNamespace: ALL" in script
    assert "llm-d-overview" in script
    assert "llm-d-prism-metrics-reader" in script
    assert "base64 -d" in script

    with pytest.raises(ValueError):
        render_monitoring_stack_script(namespace="Bad_NS")
    with pytest.raises(ValueError):
        render_monitoring_stack_script(prometheus_node_port=8080)
    with pytest.raises(ValueError):
        render_monitoring_stack_script(grafana_node_port=40000)
    with pytest.raises(ValueError):
        render_monitoring_stack_script(prometheus_node_port=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        render_monitoring_stack_script(prometheus_node_port=30900, grafana_node_port=30900)
    with pytest.raises(ValueError):
        render_monitoring_stack_script(extra_dashboards={"Invalid_Dash": "{}"})


def test_render_monitoring_stack_script_execution_and_external_detection(tmp_path: Path):
    import subprocess

    import yaml

    from llm_d_lens.monitoring.cluster_stack.service import render_monitoring_stack_script

    log_file = tmp_path / "calls.log"
    values_copy = tmp_path / "captured-values.yaml"
    applied_file = tmp_path / "applied.yaml"

    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "kubectl $*" >> {str(log_file)!r}
if [[ "$1" == "get" && "$2" == "namespace" ]]; then
  exit 1
fi
if [[ "$1" == "get" && "$2" == "pods,deployments" ]]; then
  if [[ "${{DENY_ALL_NS:-0}}" == "1" ]]; then
    echo "Error from server (Forbidden)" >&2
    exit 1
  fi
  if [[ "${{EXTERNAL_OP:-0}}" == "1" ]]; then
    printf 'other-ns\\text-prometheus-operator\\tprometheus-operator\\n'
  else
    printf 'obs-mon\\tllmd-operator\\tprometheus-operator\\n'
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "pods,daemonsets" ]]; then
  if [[ "${{DENY_ALL_NS:-0}}" == "1" ]]; then
    echo "Error from server (Forbidden)" >&2
    exit 1
  fi
  if [[ "${{EXTERNAL_NE:-0}}" == "1" ]]; then
    printf 'kube-system\\tprom-node-exporter\\tnode-exporter\\n'
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "prometheus" ]]; then
  printf 'prom-discovered-sa'
  exit 0
fi
if [[ "$1" == "get" && "$2" == "svc" ]]; then
  if [[ "$3" == "-n" ]]; then
    printf 'llmd-kube-prometheus-stack-prometheus\\nllmd-grafana\\n'
  elif [[ "$*" == *"{{range .spec.ports[*]}}"* ]]; then
    if [[ "$3" == *prometheus* ]]; then
      printf '8080:31808\\n9090:%s\\n' "${{CUR_PROM_NP:-30001}}"
    else
      printf '80:%s\\n' "${{CUR_GRAF_NP:-30300}}"
    fi
  fi
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat >> {str(applied_file)!r}
  printf '\\n---\\n' >> {str(applied_file)!r}
  exit 0
fi
exit 0
"""
    helm_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "helm $*" >> {str(log_file)!r}
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--values" ]]; then
    cp "$2" {str(values_copy)!r}
    shift 2
  else
    shift
  fi
done
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script, helm_body=helm_script)

    script = render_monitoring_stack_script(
        namespace="obs-mon",
        prometheus_node_port=30900,
        grafana_node_port=30300,
    )
    proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr

    values = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values["prometheusOperator"]["enabled"] is True
    assert values["nodeExporter"]["enabled"] is True
    assert values["prometheus"]["prometheusSpec"]["serviceMonitorSelectorNilUsesHelmValues"] is False
    assert values["grafana"]["sidecar"]["dashboards"]["searchNamespace"] == "ALL"

    applied_text = applied_file.read_text(encoding="utf-8")
    assert "name: prom-discovered-sa" in applied_text
    assert "namespace: obs-mon" in applied_text
    assert "llm-d-overview" in applied_text

    calls = log_file.read_text(encoding="utf-8")
    assert "kubectl patch svc llmd-kube-prometheus-stack-prometheus -n obs-mon --type=merge" in calls
    # Prometheus port 9090 is at index 1, current 30001 != 30900 -> JSON patch on /spec/ports/1/nodePort
    assert '"/spec/ports/1/nodePort","value":30900' in calls
    # Grafana port 80 already matches 30300 -> no JSON patch emitted
    assert '"/spec/ports/0/nodePort","value":30300' not in calls

    # Now simulate pre-existing external operator + node-exporter
    log_file.write_text("", encoding="utf-8")
    env["EXTERNAL_OP"] = "1"
    env["EXTERNAL_NE"] = "1"
    proc2 = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc2.returncode == 0, proc2.stderr
    values2 = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values2["prometheusOperator"]["enabled"] is False
    assert values2["nodeExporter"]["enabled"] is False

    # Least-privilege RBAC denial on cluster-wide probes must not fail under set -euo pipefail
    env["DENY_ALL_NS"] = "1"
    proc3 = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc3.returncode == 0, proc3.stderr
    values3 = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert values3["prometheusOperator"]["enabled"] is True
    assert values3["nodeExporter"]["enabled"] is True


def test_render_metrics_reader_rbac_script_idempotency(tmp_path: Path):
    import subprocess

    from llm_d_lens.monitoring.cluster_stack.service import render_metrics_reader_rbac_script

    script = render_metrics_reader_rbac_script(monitoring_namespace="custom-mon")
    _assert_bash_syntax(script)

    log_file = tmp_path / "rbac.log"
    applied_file = tmp_path / "rbac-applied.yaml"
    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "kubectl $*" >> {str(log_file)!r}
if [[ "$1" == "get" && "$2" == "clusterrole" ]]; then
  if [[ "${{RBAC_EXISTS:-0}}" == "1" ]]; then exit 0; else exit 1; fi
fi
if [[ "$1" == "get" && "$2" == "clusterrolebinding" ]]; then
  if [[ "${{RBAC_EXISTS:-0}}" == "1" ]]; then exit 0; else exit 1; fi
fi
if [[ "$1" == "get" && "$2" == "prometheus" ]]; then
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat > {str(applied_file)!r}
  exit 0
fi
exit 0
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script)

    env["RBAC_EXISTS"] = "1"
    proc_skip = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_skip.returncode == 0, proc_skip.stderr
    assert "skipping" in proc_skip.stdout
    assert not applied_file.exists()

    env["RBAC_EXISTS"] = "0"
    proc_apply = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_apply.returncode == 0, proc_apply.stderr
    applied = applied_file.read_text(encoding="utf-8")
    assert "name: llmd-kube-prometheus-stack-prometheus" in applied
    assert "namespace: custom-mon" in applied


def test_render_nodeport_discovery_snippet_ipv4_and_ipv6(tmp_path: Path):
    import subprocess

    from llm_d_lens.monitoring.cluster_stack.service import render_nodeport_discovery_snippet

    snippet_vars = render_nodeport_discovery_snippet(monitoring_namespace="llm-d-monitoring", emit_marker_vars=True)
    snippet_no_vars = render_nodeport_discovery_snippet(monitoring_namespace="llm-d-monitoring", emit_marker_vars=False)
    assert any("MONITORING_PROMETHEUS_NODE_PORT" in line for line in snippet_vars)
    assert not any("MONITORING_PROMETHEUS_NODE_PORT" in line for line in snippet_no_vars)

    wrapped = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            *snippet_vars,
            'printf "VARS=%s|%s|%s\\n" "$MONITORING_PROMETHEUS_NODE_PORT" "$MONITORING_GRAFANA_NODE_PORT" "$MONITORING_HOST"',
        ]
    )
    _assert_bash_syntax(wrapped)

    kubectl_script = """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "get" && "$2" == "svc" && "$3" == "-n" ]]; then
  printf 'llmd-kube-prometheus-stack-prometheus\\nllmd-grafana\\n'
elif [[ "$1" == "get" && "$2" == "svc" && "$3" == *prometheus* ]]; then
  printf '31900'
elif [[ "$1" == "get" && "$2" == "svc" && "$3" == *grafana* ]]; then
  printf '31300'
elif [[ "$1" == "get" && "$2" == "nodes" ]]; then
  if [[ "$*" == *"InternalIP"* ]]; then
    printf '%s' "${NODE_INTERNAL_IP:-}"
  else
    printf '%s' "${NODE_EXTERNAL_IP:-}"
  fi
fi
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script)

    env["NODE_INTERNAL_IP"] = "10.20.30.40"
    proc_v4 = subprocess.run(["bash", "-s"], input=wrapped, text=True, env=env, capture_output=True, check=False)
    assert proc_v4.returncode == 0, proc_v4.stderr
    assert "Prometheus URL: http://10.20.30.40:31900" in proc_v4.stdout
    assert "Grafana URL: http://10.20.30.40:31300" in proc_v4.stdout
    assert "VARS=31900|31300|10.20.30.40" in proc_v4.stdout

    # Fallback to ExternalIP with IPv6 bracketing
    env["NODE_INTERNAL_IP"] = ""
    env["NODE_EXTERNAL_IP"] = "fd00::10"
    proc_v6 = subprocess.run(["bash", "-s"], input=wrapped, text=True, env=env, capture_output=True, check=False)
    assert proc_v6.returncode == 0, proc_v6.stderr
    assert "Prometheus URL: http://[fd00::10]:31900" in proc_v6.stdout
    assert "Grafana URL: http://[fd00::10]:31300" in proc_v6.stdout


def test_render_accelerator_and_bundle_scripts():
    from llm_d_lens.monitoring.cluster_stack.service import (
        render_accelerator_exporter_script,
        render_monitoring_bundle_script,
    )

    nv_script = render_accelerator_exporter_script("nvidia_gpu", existing_monitoring=False)
    _assert_bash_syntax(nv_script)
    assert "dcgm-exporter" in nv_script

    intel_script = render_accelerator_exporter_script("intel_gpu", access_mode="plugin", existing_monitoring=True)
    _assert_bash_syntax(intel_script)
    assert "xpumd" in intel_script

    with pytest.raises(ValueError):
        render_accelerator_exporter_script("unknown_provider")

    managed_bundle = render_monitoring_bundle_script(
        mode="managed",
        namespace="obs-mon",
        prometheus_node_port=30900,
        install_accelerator_exporters=True,
        accelerator_targets=[("nvidia_gpu", "plugin"), ("intel_gpu", "dra"), ("nvidia_gpu", "dra")],
    )
    _assert_bash_syntax(managed_bundle)
    assert "kube-prometheus-stack" in managed_bundle
    assert managed_bundle.count("LENS_DCGM_VALUES") == 2  # deduplicated per provider_id (start + end heredoc)
    assert "dcgm-exporter-dashboard" in managed_bundle
    assert "xpumd" in managed_bundle
    assert "LENS_FALLBACK_SM" in managed_bundle

    existing_bundle = render_monitoring_bundle_script(
        mode="existing",
        namespace="existing-mon",
        install_accelerator_exporters=True,
        accelerator_targets=[("nvidia_gpu", "plugin")],
    )
    _assert_bash_syntax(existing_bundle)
    assert "CHART_NAME=kube-prometheus-stack" not in existing_bundle
    assert "LENS_MONITORING_VALUES" not in existing_bundle
    assert "llm-d-prism-metrics-reader" in existing_bundle
    assert "dcgm-exporter" in existing_bundle
    assert "LENS_FALLBACK_SM" not in existing_bundle

    existing_no_exporters = render_monitoring_bundle_script(
        mode="existing",
        namespace="existing-mon",
        install_accelerator_exporters=False,
        accelerator_targets=[("nvidia_gpu", "plugin")],
    )
    _assert_bash_syntax(existing_no_exporters)
    assert "dcgm-exporter" not in existing_no_exporters
    assert "llm-d-prism-metrics-reader" not in existing_no_exporters


def test_render_monitoring_bundle_script_with_marker_and_label_limits():
    import re

    from llm_d_lens.monitoring.cluster_stack.service import render_monitoring_bundle_script

    script = render_monitoring_bundle_script(
        mode="managed",
        namespace="obs-mon",
        prometheus_node_port=30900,
        grafana_node_port=30300,
        install_accelerator_exporters=False,
        marker_namespace="team-a",
        marker_name="lens-monitoring-handoff-abcd1234",
    )
    _assert_bash_syntax(script)
    assert "MONITORING_PROMETHEUS_NODE_PORT=" in script
    assert "MONITORING_GRAFANA_NODE_PORT=" in script
    assert "MONITORING_HOST=" in script
    assert "lens-monitoring-handoff-abcd1234" in script
    assert "app.kubernetes.io/managed-by: llm-d-prism" in script
    assert 'llm-d-prism.io/monitoring-mode: "$MARKER_MODE"' in script
    # Label values must be <= 63 chars and valid K8s label format
    for val in ("llm-d-prism", "managed", "existing"):
        assert len(val) <= 63
        assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", val)

    with pytest.raises(ValueError):
        render_monitoring_bundle_script(marker_namespace="team-a")


@pytest.mark.asyncio
async def test_probe_monitoring_access_ssar_admin_non_admin_stream_and_errors(monkeypatch):
    from llm_d_lens.monitoring.service_links import (
        invalidate_monitoring_access_cache,
        is_auth_error,
        is_forbidden_error,
        probe_monitoring_access,
    )

    assert is_auth_error("Kubernetes create failed (Unauthorized, HTTP 401)") is True
    assert is_auth_error("error: You must be logged in to the server (Unauthorized)") is True
    assert is_forbidden_error("Kubernetes create failed (Unauthorized, HTTP 401)") is False
    assert is_forbidden_error("selfsubjectaccessreviews.authorization.k8s.io is forbidden") is True
    assert is_forbidden_error("Kubernetes create failed (Forbidden, HTTP 403)") is True

    class StreamRunner:
        async def run(
            self,
            argv: list[str],
            *,
            input: str | None = None,  # noqa: A002 - matches CommandRunner
            timeout: float = 15,
        ) -> CommandResult:
            del timeout
            req = json.loads(input or "{}")
            # Emit consecutive JSON objects in reversed order to verify attribute-key matching
            docs = []
            for item in reversed(req["items"]):
                attrs = item["spec"]["resourceAttributes"]
                allowed = attrs["resource"] != "clusterrolebindings"
                docs.append(
                    json.dumps(
                        {
                            "apiVersion": "authorization.k8s.io/v1",
                            "kind": "SelfSubjectAccessReview",
                            "spec": {"resourceAttributes": attrs},
                            "status": {"allowed": allowed},
                        }
                    )
                )
            return _result(argv, "\n".join(docs))

    non_admin = await probe_monitoring_access("c1", scope="cluster_stack", runner=StreamRunner(), use_cache=False)
    assert non_admin.reachable is True
    assert non_admin.access_mode == "non_admin"
    assert non_admin.can_manage is False

    class AuthFailRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(argv, stderr="error: You must be logged in to the server (Unauthorized)", returncode=1)

    auth_fail = await probe_monitoring_access("c1", scope="cluster_stack", runner=AuthFailRunner(), use_cache=False)
    assert auth_fail.reachable is False
    assert auth_fail.access_mode is None
    assert auth_fail.can_manage is False
    assert auth_fail.auth_error is True

    class ForbiddenSsarRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(argv, stderr="selfsubjectaccessreviews.authorization.k8s.io is forbidden", returncode=1)

    forbidden = await probe_monitoring_access(
        "c1", scope="cluster_stack", runner=ForbiddenSsarRunner(), use_cache=False
    )
    assert forbidden.reachable is True
    assert forbidden.access_mode == "non_admin"
    assert forbidden.can_manage is False

    class MismatchedRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(
                argv,
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "List",
                        "items": [
                            {
                                "spec": {"resourceAttributes": {"verb": "get", "group": "", "resource": "pods"}},
                                "status": {"allowed": True},
                            }
                        ],
                    }
                ),
            )

    mismatch = await probe_monitoring_access("c1", scope="cluster_stack", runner=MismatchedRunner(), use_cache=False)
    assert mismatch.reachable is False
    assert mismatch.access_mode is None

    # Egress check enforced when runner is None and cluster_id is provided
    from llm_d_lens.cluster import egress as cluster_egress

    def blocked(_cluster_id: str):
        raise cluster_egress.EgressBlockedError("blocked", host="169.254.169.254")

    monkeypatch.setattr(cluster_egress, "validate_stored_cluster_egress", blocked)
    invalidate_monitoring_access_cache("c1")
    egress_res = await probe_monitoring_access("c1", scope="cluster_stack", use_cache=False)
    assert egress_res.reachable is False
    assert egress_res.access_mode is None
    assert "not allowed" in (egress_res.error or "").lower()


@pytest.mark.asyncio
async def test_check_http_endpoint_reachable_and_direct_links(monkeypatch):
    import httpx

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.monitoring.service_links import check_http_endpoint_reachable, resolve_direct_monitoring_links

    async def fake_safe(_url: str, *, allow_query: bool = False) -> None:
        del allow_query

    monkeypatch.setattr(prom_mod, "_ensure_safe_prometheus_url", fake_safe)

    def make_client(handler):
        def factory(endpoint, *, timeout=3.0):
            base = endpoint if isinstance(endpoint, str) else endpoint.base_url
            return httpx.AsyncClient(
                base_url=base,
                timeout=timeout,
                transport=httpx.MockTransport(handler),
            )

        return factory

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        make_client(lambda req: httpx.Response(200) if req.url.path == "/api/health" else httpx.Response(404)),
    )
    assert await check_http_endpoint_reachable("http://grafana.local:30300") is True

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        make_client(
            lambda req: (
                httpx.Response(404)
                if req.url.path == "/api/health"
                else httpx.Response(302, headers={"Location": "/login"})
            )
        ),
    )
    assert await check_http_endpoint_reachable("http://grafana.local:30300") is True

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        make_client(
            lambda req: (
                httpx.Response(404)
                if req.url.path == "/api/health"
                else httpx.Response(302, headers={"Location": "https://evil.example/login"})
            )
        ),
    )
    assert await check_http_endpoint_reachable("http://grafana.local:30300") is False

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_namespace": "obs-mon",
            "prometheus_url": "http://10.0.0.5:30900/",
            "grafana_url": "http://10.0.0.5:30300/",
        },
    )
    prom_link, graf_link = resolve_direct_monitoring_links("c1", grafana_dashboard_path="/d/llm-d-overview")
    assert prom_link is not None and prom_link.url == "http://10.0.0.5:30900"
    assert graf_link is not None and graf_link.url == "http://10.0.0.5:30300"
    assert graf_link.dashboard_path == "/d/llm-d-overview"


@pytest.mark.asyncio
async def test_probe_accelerator_metrics_via_prometheus_uses_max_and_profile():
    import httpx

    from llm_d_lens.hardware.registry import get_profile
    from llm_d_lens.monitoring.service_links import probe_accelerator_metrics_via_prometheus

    profile = get_profile("nvidia")

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if q == "count(DCGM_FI_DEV_GPU_UTIL)":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "4"]}]}})
        if q == "count(DCGM_FI_DEV_FB_USED)":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "8"]}]}})
        if q == "count(count by (Hostname) (DCGM_FI_DEV_GPU_UTIL))":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "2"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    async with httpx.AsyncClient(base_url="http://prom.local:9090", transport=httpx.MockTransport(handler)) as client:
        res = await probe_accelerator_metrics_via_prometheus(client, profile)

    assert res["detected"] is True
    assert res["series_count"] == 8  # max(4, 8), not sum
    assert res["node_count"] == 2
    assert "DCGM_FI_DEV_GPU_UTIL" in res["metrics_found"]
    assert res["source_label"] == profile.telemetry.source_label


@pytest.mark.asyncio
async def test_discover_cluster_stack_non_admin_and_existing_grafana_optional(monkeypatch):
    import httpx

    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.monitoring.cluster_stack import discovery as stack_discovery
    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint

    async def fake_resolve(_cid, *, allow_port_forward=True, use_cache=True):
        assert allow_port_forward is False
        assert use_cache is False
        return PrometheusEndpoint(
            base_url="http://10.0.0.10:30900", source="explicit_url", namespace="llm-d-monitoring"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Prometheus Server is Ready.")
        q = request.url.params.get("query", "")
        if q in {"count(kube_node_info)", "count(node_uname_info)"}:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "2"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(stack_discovery, "resolve_prometheus_endpoint", fake_resolve)
    monkeypatch.setattr(
        stack_discovery,
        "create_prometheus_client",
        lambda ep, *, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )

    async def fake_check_health(_ep, *, client=None, timeout=3.0):
        del client, timeout
        return True

    monkeypatch.setattr(stack_discovery, "check_prometheus_health", fake_check_health)

    # In existing mode with grafana_url unset, Grafana is optional (severity="info") and stack stays ready
    existing_status = await stack_discovery.discover_cluster_stack_non_admin(
        "llm-d-monitoring",
        "c1",
        context="non-admin-ctx",
        settings={"monitoring_mode": "existing", "prometheus_url": "http://10.0.0.10:30900", "grafana_url": None},
    )
    assert existing_status.access_mode == "non_admin"
    assert existing_status.can_manage is False
    assert existing_status.status == "ready"
    comps = {c.name: c for c in existing_status.components}
    assert comps["grafana"].diagnostics[0].code == "GRAFANA_URL_OPTIONAL"
    assert comps["grafana"].diagnostics[0].severity == "info"
    assert comps["kube_state_metrics"].status == "ready"
    assert comps["node_exporter"].ready == 2

    # In managed mode with grafana_url unset, stack is degraded
    managed_status = await stack_discovery.discover_cluster_stack_non_admin(
        "llm-d-monitoring",
        "c1",
        settings={"monitoring_mode": "managed", "prometheus_url": "http://10.0.0.10:30900", "grafana_url": None},
    )
    assert managed_status.status == "degraded"
    assert {c.name: c for c in managed_status.components}["grafana"].diagnostics[0].code == "GRAFANA_URL_UNSET"
    del prom_mod


@pytest.mark.asyncio
async def test_cluster_stack_service_non_admin_links_preflight_script_and_recheck(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.cluster import guard
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.cluster.errors import ClusterOverviewError
    from llm_d_lens.cluster.plan import AcceleratorPlan, GatewayPlan, MonitoringPlan, ProvisioningPlan
    from llm_d_lens.db.dao.cluster import ClusterDao
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.service_links import (
        MonitoringAccessResult,
        read_and_persist_monitoring_marker,
    )

    # 1. Direct links & never port-forwarding for non-admin
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "managed",
            "monitoring_namespace": "llm-d-monitoring",
            "prometheus_url": "http://10.0.0.10:30900",
            "grafana_url": None,
        },
    )

    async def fail_port_forward(*_args, **_kwargs):
        raise AssertionError("ensure_port_forward must not be called for non_admin")

    async def fake_probe_non_admin(*_args, **_kwargs):
        return MonitoringAccessResult(reachable=True, access_mode="non_admin", can_manage=False, context="ctx")

    monkeypatch.setattr(stack_service, "ensure_port_forward", fail_port_forward)
    monkeypatch.setattr(stack_service, "probe_monitoring_access", fake_probe_non_admin)

    links_res = await stack_service.get_links("llm-d-monitoring", cluster_id="c1")
    by_kind = {link.kind: link for link in links_res.links}
    assert by_kind["prometheus"].available is True
    assert by_kind["prometheus"].url == "http://10.0.0.10:30900"
    assert by_kind["grafana"].available is False

    # 2. Preflight & install reject non-admin cleanly with 403
    async def fake_discover_non_admin(namespace, cluster_id, **_kwargs):
        from datetime import UTC, datetime

        from llm_d_lens.monitoring.cluster_stack.models import (
            ClusterStackStatusResponse,
            ClusterSummary,
            HelmReleaseSummary,
        )

        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status="absent",
            message="not configured",
            observed_at=datetime.now(UTC),
            access_mode="non_admin",
            can_manage=False,
        )

    monkeypatch.setattr(stack_service, "discover_cluster_stack_non_admin", fake_discover_non_admin)
    monkeypatch.setattr(stack_service.runner, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_service, "installer_path", lambda _cid=None: _async_val(Path(__file__)))
    with pytest.raises(stack_service.ClusterStackError) as exc_info:
        await stack_service.install(ClusterStackInstallRequest(), idempotency_key="k1", cluster_id="c1")
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "INSUFFICIENT_PERMISSIONS"

    # 3. _load_cluster_plan_preserving_monitoring + get_admin_script + marker persistence
    fake_cluster = SimpleNamespace(id="abcd1234", gateway_public_url="https://gw.local", gateway_port=30080)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda cid: fake_cluster if cid == "abcd1234" else None)
    monkeypatch.setattr(cluster_registry, "cluster_kubeconfig_namespace", lambda _cid: "team-a")
    monkeypatch.setattr(guard, "load_plan", lambda _cid: None)
    monkeypatch.setattr(
        ClusterDao,
        "get_provisioning_plan",
        lambda _self, _cid: {
            "monitoring": {
                "mode": "managed",
                "namespace": "obs-mon",
                "prometheusNodePort": 30901,
                "grafanaNodePort": 30301,
            }
        },
    )

    script_resp = await stack_service.get_admin_script(cluster_id="abcd1234")
    _assert_bash_syntax(script_resp.script)
    assert script_resp.namespace == "obs-mon"
    assert script_resp.marker_namespace == "team-a"
    assert script_resp.marker_name == "lens-monitoring-handoff-abcd1234"

    # Also test plan_invalid fallback preserves monitoring settings & persists marker endpoints
    def raise_invalid(_cid):
        raise ClusterOverviewError("bad plan", code="plan_invalid")

    monkeypatch.setattr(guard, "load_plan", raise_invalid)
    persisted_calls = []

    def fake_persist(cluster, plan, *, prometheus_node_port=None, grafana_node_port=None, host=None):
        persisted_calls.append((cluster.id, plan.monitoring.namespace, prometheus_node_port, grafana_node_port, host))
        return {}

    monkeypatch.setattr(cluster_registry, "persist_discovered_monitoring_endpoints", fake_persist)

    class MarkerRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(
                argv,
                json.dumps(
                    {
                        "data": {
                            "status": "complete",
                            "prometheusNodePort": "31900",
                            "grafanaNodePort": "31300",
                            "monitoringHost": "10.20.30.40",
                        }
                    }
                ),
            )

    marker = await read_and_persist_monitoring_marker("abcd1234", runner=MarkerRunner())
    assert marker is not None and marker["persisted"] is True
    assert persisted_calls == [("abcd1234", "obs-mon", 31900, 31300, "10.20.30.40")]

    # Equal NodePorts are rejected cleanly
    persisted_calls.clear()

    class EqualPortRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(
                argv,
                json.dumps(
                    {
                        "data": {
                            "status": "complete",
                            "prometheusNodePort": "31900",
                            "grafanaNodePort": "31900",
                            "monitoringHost": "10.20.30.40",
                        }
                    }
                ),
            )

    marker_eq = await read_and_persist_monitoring_marker("abcd1234", runner=EqualPortRunner())
    assert marker_eq is not None
    assert persisted_calls == [("abcd1234", "obs-mon", None, None, "10.20.30.40")]

    # 4. recheck_cluster_stack invalidates caches, reads marker, returns ClusterStackRecheckResponse
    async def fake_read_marker(_cid):
        return {"persisted": True}

    monkeypatch.setattr(stack_service, "read_and_persist_monitoring_marker", fake_read_marker)
    recheck_res = await stack_service.recheck_cluster_stack("llm-d-monitoring", cluster_id="abcd1234")
    assert recheck_res.marker_found is True
    assert recheck_res.access_mode == "non_admin"
    assert recheck_res.can_manage is False
    del AcceleratorPlan, GatewayPlan, MonitoringPlan, ProvisioningPlan


async def _async_val(val):
    return val


def test_cluster_stack_router_enforces_cluster_access_and_registers_new_routes(monkeypatch):
    import importlib

    from fastapi import HTTPException

    stack_router = importlib.import_module("llm_d_lens.monitoring.cluster_stack.router")

    client = TestClient(app)
    paths = client.get("/api/simulation/openapi.json").json()["paths"]
    assert "/api/v1/monitoring/cluster-stack/admin-script" in paths
    assert "/api/v1/monitoring/cluster-stack/recheck" in paths
    assert "description" not in paths["/api/v1/monitoring/cluster-stack/admin-script"]["get"]
    assert "description" not in paths["/api/v1/monitoring/cluster-stack/recheck"]["post"]

    def deny(_principal, cluster_id):
        raise HTTPException(status_code=403, detail=f"forbidden for {cluster_id}")

    monkeypatch.setattr(stack_router, "require_cluster_access", deny)
    monkeypatch.setattr(stack_router, "current_principal", lambda _req: object())

    for method, url, kwargs in (
        ("GET", "/api/v1/monitoring/cluster-stack/status?cluster_id=c1", {}),
        ("GET", "/api/v1/monitoring/cluster-stack/links?cluster_id=c1", {}),
        ("GET", "/api/v1/monitoring/cluster-stack/admin-script?cluster_id=c1", {}),
        ("POST", "/api/v1/monitoring/cluster-stack/recheck?cluster_id=c1", {}),
        ("POST", "/api/v1/monitoring/cluster-stack/installations/preflight?cluster_id=c1", {"json": {}}),
        ("POST", "/api/v1/monitoring/cluster-stack/installations?cluster_id=c1", {"json": {}}),
    ):
        resp = client.request(method, url, **kwargs)
        assert resp.status_code == 403, f"{method} {url} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_redact_monitoring_error_and_subpath_grafana_probe(monkeypatch):
    import httpx

    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.monitoring.service_links import (
        check_http_endpoint_reachable,
        probe_monitoring_access,
        redact_monitoring_error,
    )

    raw = (
        'error loading config file "/var/lib/lens/kubeconfig_cache/c1.kubeconfig": '
        "Bearer super-secret-jwt token=abc123 password:my-pw "
        "client-key-data:BASE64DATA https://alice:s3cr3t@api.cluster.local:6443"
    )
    redacted = redact_monitoring_error(raw)
    assert "/var/lib/lens/kubeconfig_cache" not in redacted
    assert "[REDACTED_KUBECONFIG_PATH]" in redacted
    assert "super-secret-jwt" not in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "abc123" not in redacted
    assert "my-pw" not in redacted
    assert "BASE64DATA" not in redacted
    assert "s3cr3t" not in redacted
    assert "https://[REDACTED]@api.cluster.local:6443" in redacted
    assert len(redact_monitoring_error("x" * 600, max_len=120)) == 120

    class SecretAuthFailRunner:
        async def run(self, argv: list[str], **_kwargs) -> CommandResult:
            return _result(
                argv,
                stderr='Unauthorized: Bearer secret-tok using config file "/tmp/kubeconfig_cache/c1.kubeconfig"',
                returncode=1,
            )

    res = await probe_monitoring_access("c1", scope="cluster_stack", runner=SecretAuthFailRunner(), use_cache=False)
    assert res.reachable is False
    assert res.auth_error is True
    assert "secret-tok" not in (res.error or "")
    assert "kubeconfig_cache" not in (res.error or "")

    async def fake_safe(_url: str, *, allow_query: bool = False) -> None:
        del allow_query

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/grafana/api/health":
            return httpx.Response(200)
        return httpx.Response(404)

    monkeypatch.setattr(prom_mod, "_ensure_safe_prometheus_url", fake_safe)
    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=3.0: httpx.AsyncClient(
            base_url=endpoint if isinstance(endpoint, str) else endpoint.base_url,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )
    assert await check_http_endpoint_reachable("http://grafana.local:30300/grafana") is True
    assert await check_http_endpoint_reachable("http://grafana.local:30300/grafana/") is True
    assert seen_paths == ["/grafana/api/health", "/grafana/api/health"]


def test_authorize_monitoring_cluster_request_blocks_ambient_bypass_and_cross_cluster_escalation(monkeypatch):
    import importlib

    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.monitoring.service_links import authorize_monitoring_cluster_request

    stack_router = importlib.import_module("llm_d_lens.monitoring.cluster_stack.router")

    # Maintainer on c1, read-only end-user on c2
    scoped_user = Principal(
        user_id="u1",
        username="u1",
        role_permissions={
            "maintainer": frozenset(
                {
                    "monitoring:cluster-stack:read",
                    "monitoring:cluster-stack:install",
                }
            ),
            "end-user": frozenset({"monitoring:cluster-stack:read"}),
        },
        bindings=(
            RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="c1"),
            RoleBinding(role="end-user", scope_type=ScopeType.CLUSTER, scope_cluster_id="c2"),
        ),
    )

    # 1. Missing or whitespace-only cluster_id must be rejected for cluster-scoped principals
    for blank in (None, "", "   "):
        with pytest.raises(ForbiddenError):
            authorize_monitoring_cluster_request(scoped_user, blank, "monitoring:cluster-stack:read")

    # 2. Cross-cluster escalation: maintainer on c1 cannot install on c2 where they are end-user
    assert authorize_monitoring_cluster_request(scoped_user, " c1 ", "monitoring:cluster-stack:install") == "c1"
    assert authorize_monitoring_cluster_request(scoped_user, "c2", "monitoring:cluster-stack:read") == "c2"
    with pytest.raises(ForbiddenError):
        authorize_monitoring_cluster_request(scoped_user, "c2", "monitoring:cluster-stack:install")

    # 3. Global admin / unrestricted caller can omit cluster_id cleanly
    global_admin = Principal(
        user_id="admin",
        username="admin",
        role_permissions={"admin": frozenset({"*:*:*:*"})},
        bindings=(RoleBinding(role="admin", scope_type=ScopeType.GLOBAL),),
    )
    assert authorize_monitoring_cluster_request(global_admin, "   ", "monitoring:cluster-stack:install") is None
    assert authorize_monitoring_cluster_request(None, None, "monitoring:cluster-stack:read") is None

    # 4. Router enforces both guards via HTTP
    client = TestClient(app)
    monkeypatch.setattr(stack_router, "current_principal", lambda _req: scoped_user)
    assert client.get("/api/v1/monitoring/cluster-stack/status").status_code == 403
    assert client.get("/api/v1/monitoring/cluster-stack/status?cluster_id=%20%20").status_code == 403
    assert (
        client.post("/api/v1/monitoring/cluster-stack/installations/preflight?cluster_id=c2", json={}).status_code
        == 403
    )


def test_load_cluster_plan_preserving_monitoring_fallback_and_marker_cleanup(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.cluster import guard
    from llm_d_lens.db.dao.cluster import ClusterDao
    from llm_d_lens.monitoring.cluster_stack.service import _render_monitoring_marker_block
    from llm_d_lens.monitoring.service_links import _load_cluster_plan_preserving_monitoring

    cluster = SimpleNamespace(id="c-bad", gateway_public_url=None, gateway_port=None)
    monkeypatch.setattr(guard, "load_plan", lambda _cid: None)
    monkeypatch.setattr(
        ClusterDao,
        "get_provisioning_plan",
        lambda _self, _cid: {"monitoring": {"mode": "invalid-mode"}},
    )
    plan, plan_hash = _load_cluster_plan_preserving_monitoring(cluster)
    assert plan_hash is None
    assert plan.monitoring.mode == "managed"

    block = "\n".join(
        _render_monitoring_marker_block(
            marker_namespace="team-a",
            marker_name="lens-monitoring-handoff-c1",
            monitoring_namespace="llm-d-monitoring",
            mode="managed",
        )
    )
    assert "trap 'rm -f -- \"$MARKER_FILE\"' EXIT" in block
    _assert_bash_syntax("#!/usr/bin/env bash\nset -euo pipefail\n" + block + "\n")


@pytest.mark.asyncio
async def test_get_status_admin_existing_uses_http_discovery_and_prevents_port_forward_url_leak(monkeypatch):
    import httpx

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.hardware import registry as hardware_registry
    from llm_d_lens.monitoring.cluster_stack import discovery as stack_discovery
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.prometheus import PrometheusEndpoint
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_mode": "existing",
            "monitoring_namespace": "existing-mon",
            "prometheus_url": None,
            "grafana_url": None,
        },
    )

    async def fake_probe_admin(*_args, **_kwargs):
        return MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="admin-ctx")

    async def fail_helm_discovery(*_args, **_kwargs):
        raise AssertionError("discover_cluster_stack (Helm) must not run for admin+existing")

    monkeypatch.setattr(stack_service, "probe_monitoring_access", fake_probe_admin)
    monkeypatch.setattr(stack_service, "discover_cluster_stack", fail_helm_discovery)

    # 1. Endpoint missing -> absent with admin-specific message
    async def fake_resolve_none(_cid, *, allow_port_forward=False, use_cache=True):
        assert allow_port_forward is True
        assert use_cache is False
        return

    monkeypatch.setattr(stack_discovery, "resolve_prometheus_endpoint", fake_resolve_none)
    absent = await stack_service.get_status("existing-mon", cluster_id="c-admin-existing")
    assert absent.status == "absent"
    assert absent.access_mode == "admin"
    assert absent.can_manage is True
    assert absent.monitoring_mode == "existing"
    assert absent.message == "Existing monitoring endpoints are not configured"
    assert absent.prometheus_url is None

    # 2. Port-forward endpoint -> must NOT leak http://127.0.0.1:<port> into snapshot.prometheus_url
    async def fake_resolve_pf(_cid, *, allow_port_forward=False, use_cache=True):
        assert allow_port_forward is True
        assert use_cache is False
        return PrometheusEndpoint(
            base_url="http://127.0.0.1:43210",
            source="port_forward",
            namespace="existing-mon",
            local_port=43210,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready")
        q = request.url.params.get("query", "")
        if q in {"count(kube_node_info)", "count(node_uname_info)"}:
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "2"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(stack_discovery, "resolve_prometheus_endpoint", fake_resolve_pf)
    monkeypatch.setattr(
        stack_discovery,
        "create_prometheus_client",
        lambda ep, *, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setattr(stack_discovery, "check_prometheus_health", lambda *_a, **_kw: _async_val(True))

    # CPU-only: optional Grafana & optional accelerator_exporters still yield ready
    monkeypatch.setattr(hardware_registry, "all_profiles", lambda: [])
    cpu_status = await stack_service.get_status("existing-mon", cluster_id="c-admin-existing")
    assert cpu_status.status == "ready"
    assert cpu_status.access_mode == "admin"
    assert cpu_status.can_manage is True
    assert cpu_status.monitoring_mode == "existing"
    assert cpu_status.prometheus_url is None
    comps = {c.name: c for c in cpu_status.components}
    assert comps["grafana"].diagnostics[0].code == "GRAFANA_URL_OPTIONAL"
    assert comps["accelerator_exporters"].diagnostics[0].code == "ACCELERATOR_METRICS_NOT_DETECTED"

    # GPU profile detected via profile-driven probe -> accelerator_exporters ready
    fake_profile = type("Profile", (), {"id": "mock-gpu", "display_name": "Mock GPU"})()
    monkeypatch.setattr(hardware_registry, "all_profiles", lambda: [fake_profile])
    monkeypatch.setattr(
        stack_discovery,
        "probe_accelerator_metrics_via_prometheus",
        lambda _client, _profile: _async_val({"detected": True}),
    )
    gpu_status = await stack_service.get_status("existing-mon", cluster_id="c-admin-existing")
    assert gpu_status.status == "ready"
    gpu_comps = {c.name: c for c in gpu_status.components}
    assert gpu_comps["accelerator_exporters"].status == "ready"
    assert gpu_comps["accelerator_exporters"].ready == 1
    assert gpu_comps["accelerator_exporters"].desired == 1


@pytest.mark.asyncio
async def test_cluster_stack_service_invalidates_cluster_monitoring_cache(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from llm_d_lens.cluster import service as cluster_service
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackOperationResponse,
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    installer_script = tmp_path / "install.sh"
    installer_script.write_text("#!/bin/sh\nexit 0\n")
    installer_script.chmod(0o755)

    invalidated: list[str | None] = []
    monkeypatch.setattr(cluster_service, "invalidate_monitoring_cache", lambda cid=None: invalidated.append(cid))
    monkeypatch.setattr(stack_service, "invalidate_monitoring_access_cache", lambda _cid=None: None)
    monkeypatch.setattr(stack_service, "invalidate_prometheus_endpoint_cache", lambda _cid=None: None)
    monkeypatch.setattr(stack_service, "read_and_persist_monitoring_marker", lambda _cid: _async_val(None))

    snapshot = ClusterStackStatusResponse(
        cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
        namespace="llm-d-monitoring",
        release=HelmReleaseSummary(),
        status="absent",
        message="not installed",
        observed_at=datetime.now(UTC),
        access_mode="admin",
        can_manage=True,
    )

    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        lambda *_a, **_kw: _async_val(
            MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )
    monkeypatch.setattr(stack_service, "discover_cluster_stack", lambda *_a, **_kw: _async_val(snapshot))
    monkeypatch.setattr(stack_service, "_runner", lambda _cid: object())
    monkeypatch.setattr(stack_service, "_safe_monitoring_settings", lambda _cid: None)
    monkeypatch.setattr(stack_service.operations, "active_id", lambda *_a: None)
    monkeypatch.setattr(stack_service, "expected_dashboard_names", lambda _cid=None: _async_val([]))
    monkeypatch.setattr(stack_service.runner, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_service, "installer_path", lambda _cid=None: _async_val(installer_script))

    # 1. recheck_cluster_stack
    await stack_service.recheck_cluster_stack("llm-d-monitoring", cluster_id="c-inv")
    assert invalidated == ["c-inv"]

    # 2. verify_status
    invalidated.clear()
    await stack_service.verify_status("llm-d-monitoring", cluster_id="c-inv")
    assert invalidated == ["c-inv"]

    # 3. install after queuing
    invalidated.clear()
    op = ClusterStackOperationResponse(
        operation_id="a" * 32,
        status="queued",
        phase="queued",
        namespace="llm-d-monitoring",
    )
    monkeypatch.setattr(stack_service.operations, "create", lambda *_a, **_kw: _async_val(op))
    res_op = await stack_service.install(ClusterStackInstallRequest(), idempotency_key="k1", cluster_id="c-inv")
    assert res_op.operation_id == "a" * 32
    assert invalidated == ["c-inv"]


@pytest.mark.asyncio
async def test_cluster_stack_custom_monitoring_namespace_resolution_and_registry_sync(monkeypatch, tmp_path):
    import importlib
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from llm_d_lens.cluster import guard
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.db.dao.cluster import ClusterDao
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackOperationResponse,
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    stack_router = importlib.import_module("llm_d_lens.monitoring.cluster_stack.router")

    installer_script = tmp_path / "install.sh"
    installer_script.write_text("#!/bin/sh\nexit 0\n")
    installer_script.chmod(0o755)

    current_mon_plan: dict = {
        "enabled": True,
        "mode": "managed",
        "namespace": "llm-d-monitoring-123454",
        "prometheusUrl": "http://10.0.0.5:30900",
        "grafanaUrl": "http://10.0.0.5:30300",
        "prometheusNodePort": 30900,
        "grafanaNodePort": 30300,
        "host": "10.0.0.5",
        "installAcceleratorExporters": True,
        "autoDiscovered": ["prometheusUrl", "grafanaUrl"],
    }
    fake_cluster = SimpleNamespace(id="2790b2ae", gateway_public_url=None, gateway_port=None)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda cid: fake_cluster if cid == "2790b2ae" else None)
    monkeypatch.setattr(guard, "load_plan", lambda _cid: None)
    monkeypatch.setattr(ClusterDao, "get_provisioning_plan", lambda _self, _cid: {"monitoring": dict(current_mon_plan)})
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda cid: (
            {
                "monitoring_mode": current_mon_plan["mode"],
                "monitoring_namespace": current_mon_plan["namespace"],
                "prometheus_url": current_mon_plan["prometheusUrl"],
                "grafana_url": current_mon_plan["grafanaUrl"],
                "prometheus_node_port": current_mon_plan["prometheusNodePort"],
                "grafana_node_port": current_mon_plan["grafanaNodePort"],
            }
            if cid == "2790b2ae"
            else None
        ),
    )

    updated_plans: list[tuple[str, dict, bool]] = []

    def fake_update_monitoring(cluster_id: str, payload: dict, *, invalidate_execution: bool = False):
        updated_plans.append((cluster_id, dict(payload), invalidate_execution))
        current_mon_plan.clear()
        current_mon_plan.update(payload)
        return fake_cluster

    monkeypatch.setattr(cluster_registry, "update_cluster_monitoring", fake_update_monitoring)

    seen_discover_ns: list[str] = []
    status_by_ns: dict[str, str] = {"llm-d-monitoring-123454": "absent", "custom-install-ns": "absent"}

    async def fake_discover(namespace: str, *_args, **_kwargs):
        seen_discover_ns.append(namespace)
        st = status_by_ns.get(namespace, "absent")
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed" if st == "ready" else None),
            status=st,  # type: ignore[arg-type]
            message=st,
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        lambda *_a, **_kw: _async_val(
            MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )
    monkeypatch.setattr(stack_service, "discover_cluster_stack", fake_discover)
    monkeypatch.setattr(stack_service, "_runner", lambda _cid: object())
    monkeypatch.setattr(stack_service.operations, "active_id", lambda *_a: None)
    monkeypatch.setattr(stack_service, "expected_dashboard_names", lambda _cid=None: _async_val([]))
    monkeypatch.setattr(stack_service.runner, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_service, "installer_path", lambda _cid=None: _async_val(installer_script))
    monkeypatch.setattr(stack_service, "read_and_persist_monitoring_marker", lambda _cid: _async_val(None))
    monkeypatch.setattr(stack_router, "current_principal", lambda _req: None)

    with TestClient(app) as client:
        # 1. GET /status, GET /links, POST /recheck without ?namespace= resolve cluster's custom monitoring_namespace
        st_res = client.get("/api/v1/monitoring/cluster-stack/status?cluster_id=2790b2ae")
        assert st_res.status_code == 200
        assert st_res.json()["namespace"] == "llm-d-monitoring-123454"

        ln_res = client.get("/api/v1/monitoring/cluster-stack/links?cluster_id=2790b2ae")
        assert ln_res.status_code == 200
        assert ln_res.json()["namespace"] == "llm-d-monitoring-123454"
        assert all(link["namespace"] == "llm-d-monitoring-123454" for link in ln_res.json()["links"])

        rc_res = client.post("/api/v1/monitoring/cluster-stack/recheck?cluster_id=2790b2ae")
        assert rc_res.status_code == 200
        assert rc_res.json()["namespace"] == "llm-d-monitoring-123454"
        # Failed/absent recheck must NOT clobber cluster.monitoring_namespace
        assert updated_plans == []

        # Explicit namespace parameter is validated and honored
        override_res = client.get("/api/v1/monitoring/cluster-stack/status?cluster_id=2790b2ae&namespace=override-mon")
        assert override_res.status_code == 200
        assert override_res.json()["namespace"] == "override-mon"
        assert (
            client.get("/api/v1/monitoring/cluster-stack/status?cluster_id=2790b2ae&namespace=Invalid_Name").status_code
            == 422
        )

        # 2. Preflight with omitted namespace resolves cluster's custom monitoring_namespace
        pf_res = client.post("/api/v1/monitoring/cluster-stack/installations/preflight?cluster_id=2790b2ae", json={})
        assert pf_res.status_code == 200
        assert "--namespace" in pf_res.json()["command_preview"]
        assert "llm-d-monitoring-123454" in pf_res.json()["command_preview"]

        # 3. Queuing an install must NOT overwrite cluster.monitoring_namespace before verification succeeds
        captured_verifier = {}

        def fake_create(req, *, verifier, **_kw):
            captured_verifier["fn"] = verifier
            return _async_val(
                ClusterStackOperationResponse(
                    operation_id="b" * 32,
                    status="queued",
                    phase="preflight",
                    namespace=req.namespace or "llm-d-monitoring",
                )
            )

        monkeypatch.setattr(stack_service.operations, "create", fake_create)
        inst_res = client.post(
            "/api/v1/monitoring/cluster-stack/installations?cluster_id=2790b2ae",
            json={"namespace": "custom-install-ns"},
        )
        assert inst_res.status_code == 202
        assert inst_res.json()["namespace"] == "custom-install-ns"
        assert updated_plans == []

        # Failed/degraded verification still leaves cluster.monitoring_namespace untouched
        status_by_ns["custom-install-ns"] = "degraded"
        await captured_verifier["fn"]("custom-install-ns")
        assert updated_plans == []

        # Ready verification syncs cluster.monitoring_namespace while preserving URLs
        status_by_ns["custom-install-ns"] = "ready"
        await captured_verifier["fn"]("custom-install-ns")
        assert len(updated_plans) == 1
        assert updated_plans[0][0] == "2790b2ae"
        assert updated_plans[0][1]["namespace"] == "custom-install-ns"
        assert updated_plans[0][1]["prometheusUrl"] == "http://10.0.0.5:30900"
        assert updated_plans[0][2] is False

        # 4. Recheck on an absent custom namespace does NOT clobber; recheck on a ready custom namespace DOES sync
        updated_plans.clear()
        status_by_ns["absent-other-ns"] = "absent"
        rc_absent = client.post(
            "/api/v1/monitoring/cluster-stack/recheck?cluster_id=2790b2ae&namespace=absent-other-ns"
        )
        assert rc_absent.status_code == 200
        assert updated_plans == []

        status_by_ns["ready-other-ns"] = "ready"
        rc_ready = client.post("/api/v1/monitoring/cluster-stack/recheck?cluster_id=2790b2ae&namespace=ready-other-ns")
        assert rc_ready.status_code == 200
        assert len(updated_plans) == 1
        assert updated_plans[0][1]["namespace"] == "ready-other-ns"


@pytest.mark.asyncio
async def test_online_install_reinstall_preserves_nodeports_applies_rbac_and_updates_monitoring(monkeypatch, tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import yaml

    from llm_d_lens.cluster import guard
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.db.dao.cluster import ClusterDao
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.deployment.service import METRICS_READER_BINDING, METRICS_READER_ROLE
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    installer_script = tmp_path / "install.sh"
    installer_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installer_script.chmod(0o755)

    current_mon_plan: dict = {
        "enabled": True,
        "mode": "managed",
        "namespace": "custom-obs-ns",
        "prometheusUrl": "http://10.20.30.1:30900",
        "grafanaUrl": "http://10.20.30.1:30300",
        "prometheusNodePort": 30900,
        "grafanaNodePort": 30300,
        "host": "10.20.30.1",
        "installAcceleratorExporters": True,
        "autoDiscovered": ["prometheusUrl", "grafanaUrl", "host"],
    }
    fake_cluster = SimpleNamespace(id="c-reinstall", gateway_public_url=None, gateway_port=None)
    monkeypatch.setattr(cluster_registry, "get_cluster", lambda cid: fake_cluster if cid == "c-reinstall" else None)
    monkeypatch.setattr(guard, "load_plan", lambda _cid: None)
    monkeypatch.setattr(ClusterDao, "get_provisioning_plan", lambda _self, _cid: {"monitoring": dict(current_mon_plan)})
    monkeypatch.setattr(
        cluster_registry,
        "get_cluster_monitoring_settings",
        lambda cid: (
            {
                "monitoring_mode": current_mon_plan["mode"],
                "monitoring_namespace": current_mon_plan["namespace"],
                "prometheus_url": current_mon_plan["prometheusUrl"],
                "grafana_url": current_mon_plan["grafanaUrl"],
                "prometheus_node_port": current_mon_plan["prometheusNodePort"],
                "grafana_node_port": current_mon_plan["grafanaNodePort"],
                "monitoring_host": current_mon_plan["host"],
            }
            if cid == "c-reinstall"
            else None
        ),
    )

    updated_calls: list[tuple[str, dict, bool]] = []

    def fake_update(cluster_id: str, payload: dict, *, invalidate_execution: bool = False):
        updated_calls.append((cluster_id, dict(payload), invalidate_execution))
        current_mon_plan.clear()
        current_mon_plan.update(payload)
        return fake_cluster

    monkeypatch.setattr(cluster_registry, "update_cluster_monitoring", fake_update)

    prom_cache_invalidations: list[str | None] = []
    monkeypatch.setattr(
        stack_service,
        "invalidate_prometheus_endpoint_cache",
        lambda cid=None: prom_cache_invalidations.append(cid),
    )
    monkeypatch.setattr(stack_service, "invalidate_monitoring_access_cache", lambda _cid=None: None)
    monkeypatch.setattr(stack_service, "_invalidate_cluster_monitoring_cache", lambda _cid=None: None)

    class ReconcileRunner:
        def __init__(self) -> None:
            self.services_state: dict[str, dict] = {
                "llmd-kube-prometheus-stack-prometheus": {
                    "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                    "spec": {"type": "ClusterIP", "ports": [{"port": 9090}, {"port": 8080}]},
                },
                "llmd-grafana": {
                    "metadata": {"name": "llmd-grafana"},
                    "spec": {"type": "ClusterIP", "ports": [{"port": 80}]},
                },
            }
            self.applied_manifests: list[dict] = []
            self.patch_commands: list[list[str]] = []

        async def run(self, argv: list[str], *, input: str | None = None, timeout: float = 15) -> CommandResult:  # noqa: A002
            del timeout
            if argv[:3] == ["kubectl", "get", "prometheuses"]:
                assert argv[4] == "custom-obs-ns"
                return _result(argv, json.dumps({"items": [{"spec": {"serviceAccountName": "custom-prom-sa"}}]}))
            if argv[:3] == ["kubectl", "apply", "-f"] and input is not None:
                for doc in yaml.safe_load_all(input):
                    if isinstance(doc, dict):
                        self.applied_manifests.append(doc)
                return _result(argv, "applied")
            if argv[:3] == ["kubectl", "get", "services"]:
                assert argv[4] == "custom-obs-ns"
                return _result(argv, json.dumps({"items": list(self.services_state.values())}))
            if argv[:3] == ["kubectl", "get", "svc"]:
                svc = self.services_state[argv[3]]
                return _result(argv, json.dumps(svc))
            if argv[:3] == ["kubectl", "patch", "svc"]:
                self.patch_commands.append(argv)
                svc_name = argv[3]
                svc = self.services_state[svc_name]
                if "--type=merge" in argv:
                    svc["spec"]["type"] = "NodePort"
                    default_np = 31901 if "prometheus" in svc_name else 31301
                    svc["spec"]["ports"][0]["nodePort"] = default_np
                elif "--type=json" in argv:
                    ops = json.loads(argv[argv.index("-p") + 1])
                    for op in ops:
                        idx = int(op["path"].split("/")[3])
                        svc["spec"]["ports"][idx]["nodePort"] = int(op["value"])
                return _result(argv, "patched")
            if argv[:3] == ["kubectl", "get", "nodes"]:
                return _result(
                    argv,
                    json.dumps(
                        {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "10.20.30.40"}]}}]}
                    ),
                )
            raise AssertionError(f"Unexpected kubectl command: {argv}")

    reconcile_runner = ReconcileRunner()
    monkeypatch.setattr(stack_service, "_runner", lambda _cid: reconcile_runner)
    monkeypatch.setattr(stack_service, "expected_dashboard_names", lambda _cid=None: _async_val([]))
    monkeypatch.setattr(
        stack_service,
        "discover_cluster_stack",
        lambda ns, *_a, **_kw: _async_val(
            ClusterStackStatusResponse(
                cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
                namespace=ns,
                release=HelmReleaseSummary(status="deployed"),
                status="ready",
                message="ready",
                observed_at=datetime.now(UTC),
            )
        ),
    )

    # 1. Online reinstall preserves configured NodePorts (30900 / 30300), applies metrics-reader RBAC
    #    for custom_obs_ns, synchronously updates cluster monitoring metadata, and invalidates Prom cache.
    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        lambda *_a, **_kw: _async_val(
            MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )
    monkeypatch.setattr(stack_service.runner, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_service, "installer_path", lambda _cid=None: _async_val(installer_script))
    monkeypatch.setattr(stack_service.operations, "active_id", lambda *_a: None)

    captured_verifier = {}

    def fake_create(req, *, verifier, **_kw):
        captured_verifier["fn"] = verifier
        return _async_val(
            SimpleNamespace(
                operation_id="c" * 32,
                status="queued",
                phase="preflight",
                namespace=req.namespace or "custom-obs-ns",
            )
        )

    monkeypatch.setattr(stack_service.operations, "create", fake_create)
    await stack_service.install(
        ClusterStackInstallRequest(reinstall=True), idempotency_key="reinstall-k1", cluster_id="c-reinstall"
    )

    verified = await captured_verifier["fn"]("custom-obs-ns")
    assert verified.status == "ready"
    assert verified.prometheus_url == "http://10.20.30.40:30900"
    assert verified.grafana_url == "http://10.20.30.40:30300"
    assert verified.prometheus_node_port == 30900
    assert verified.grafana_node_port == 30300

    # Services were patched to NodePort with preserved ports
    assert reconcile_runner.services_state["llmd-kube-prometheus-stack-prometheus"]["spec"]["type"] == "NodePort"
    assert (
        reconcile_runner.services_state["llmd-kube-prometheus-stack-prometheus"]["spec"]["ports"][0]["nodePort"]
        == 30900
    )
    assert reconcile_runner.services_state["llmd-grafana"]["spec"]["type"] == "NodePort"
    assert reconcile_runner.services_state["llmd-grafana"]["spec"]["ports"][0]["nodePort"] == 30300

    # metrics-reader RBAC applied for custom-obs-ns with discovered Prometheus SA & per-namespace binding
    expected_binding = stack_service.metrics_reader_binding_name("custom-obs-ns")
    assert expected_binding != METRICS_READER_BINDING
    roles = [d for d in reconcile_runner.applied_manifests if d.get("kind") == "ClusterRole"]
    bindings = [d for d in reconcile_runner.applied_manifests if d.get("kind") == "ClusterRoleBinding"]
    assert len(roles) == 1 and roles[0]["metadata"]["name"] == METRICS_READER_ROLE
    assert len(bindings) == 1 and bindings[0]["metadata"]["name"] == expected_binding
    assert bindings[0]["subjects"] == [
        {"kind": "ServiceAccount", "name": "custom-prom-sa", "namespace": "custom-obs-ns"}
    ]

    # Cluster record updated with invalidate_execution=False and Prom endpoint cache invalidated
    assert len(updated_calls) == 1
    assert updated_calls[0][0] == "c-reinstall"
    assert updated_calls[0][1]["prometheusUrl"] == "http://10.20.30.40:30900"
    assert updated_calls[0][1]["grafanaUrl"] == "http://10.20.30.40:30300"
    assert updated_calls[0][1]["prometheusNodePort"] == 30900
    assert updated_calls[0][1]["grafanaNodePort"] == 30300
    assert updated_calls[0][2] is False
    assert "c-reinstall" in prom_cache_invalidations

    # 2. Fresh install with no pre-configured NodePorts discovers K8s-assigned NodePorts (31901 / 31301)
    updated_calls.clear()
    prom_cache_invalidations.clear()
    current_mon_plan.update(
        {
            "prometheusUrl": None,
            "grafanaUrl": None,
            "prometheusNodePort": None,
            "grafanaNodePort": None,
            "host": None,
            "autoDiscovered": [],
        }
    )
    reconcile_runner.services_state["llmd-kube-prometheus-stack-prometheus"]["spec"]["type"] = "ClusterIP"
    reconcile_runner.services_state["llmd-kube-prometheus-stack-prometheus"]["spec"]["ports"][0].pop("nodePort", None)
    reconcile_runner.services_state["llmd-grafana"]["spec"]["type"] = "ClusterIP"
    reconcile_runner.services_state["llmd-grafana"]["spec"]["ports"][0].pop("nodePort", None)

    verified_fresh = await stack_service.verify_status("custom-obs-ns", cluster_id="c-reinstall")
    assert verified_fresh.prometheus_node_port == 31901
    assert verified_fresh.grafana_node_port == 31301
    assert len(updated_calls) == 1
    assert updated_calls[0][1]["prometheusUrl"] == "http://10.20.30.40:31901"
    assert updated_calls[0][1]["grafanaUrl"] == "http://10.20.30.40:31301"
    assert updated_calls[0][1]["prometheusNodePort"] == 31901
    assert updated_calls[0][1]["grafanaNodePort"] == 31301
    assert updated_calls[0][2] is False

    # 3. Existing-mode cluster does NOT patch services or overwrite external URLs during verify_status
    updated_calls.clear()
    reconcile_runner.patch_commands.clear()
    current_mon_plan.update(
        {
            "mode": "existing",
            "namespace": "custom-obs-ns",
            "prometheusUrl": "https://ext-prom.example.com",
            "grafanaUrl": "https://ext-graf.example.com",
            "prometheusNodePort": None,
            "grafanaNodePort": None,
        }
    )
    await stack_service.verify_status("custom-obs-ns", cluster_id="c-reinstall")
    assert reconcile_runner.patch_commands == []
    assert updated_calls == []

    # 4. Explicit user-configured monitoring.host is preserved (never overwritten by node InternalIP)
    #    and SSRF-blocked node IP is skipped in favor of next valid node IP.
    updated_calls.clear()
    current_mon_plan.update(
        {
            "mode": "managed",
            "namespace": "custom-obs-ns",
            "prometheusUrl": None,
            "grafanaUrl": None,
            "prometheusNodePort": 30900,
            "grafanaNodePort": 30300,
            "host": "vip.example.com",
            "autoDiscovered": [],
        }
    )
    verified_explicit_host = await stack_service.verify_status("custom-obs-ns", cluster_id="c-reinstall")
    assert verified_explicit_host.prometheus_url == "http://vip.example.com:30900"
    assert verified_explicit_host.grafana_url == "http://vip.example.com:30300"
    assert len(updated_calls) == 1
    assert updated_calls[0][1]["host"] == "vip.example.com"
    assert updated_calls[0][1]["prometheusUrl"] == "http://vip.example.com:30900"
    assert "host" not in updated_calls[0][1]["autoDiscovered"]

    assert (
        stack_service._extract_node_host_from_nodes(
            {
                "items": [
                    {"status": {"addresses": [{"type": "InternalIP", "address": "169.254.169.254"}]}},
                    {"status": {"addresses": [{"type": "InternalIP", "address": "10.20.30.44"}]}},
                ]
            },
            cluster_registry.extract_url_host,
        )
        == "10.20.30.44"
    )

    # 5. Invalid monitoring payload aborts cleanly without persisting raw unvalidated dict
    updated_calls.clear()
    from llm_d_lens.cluster import plan as cluster_plan

    monkeypatch.setattr(guard, "load_plan", lambda _cid: None)
    monkeypatch.setattr(
        cluster_plan,
        "normalize_monitoring_plan",
        lambda _p: (_ for _ in ()).throw(cluster_plan.PlanValidationError("invalid monitoring plan")),
    )
    assert await stack_service._reconcile_managed_post_install("c-reinstall", "custom-obs-ns", reconcile_runner) is None
    assert updated_calls == []

    # 6. get_links rejects invalid service names and redacts PortForwardError credentials
    from llm_d_lens.infrastructure.kubernetes import PortForwardError

    class BadLinksRunner:
        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            return _result(
                argv,
                json.dumps(
                    {
                        "items": [
                            {"metadata": {"name": "-bad-prometheus"}, "spec": {"ports": [{"port": 9090}]}},
                            {"metadata": {"name": "llmd-grafana"}, "spec": {"ports": [{"port": 80}]}},
                        ]
                    }
                ),
            )

    monkeypatch.setattr(stack_service, "_runner", lambda _cid: BadLinksRunner())
    monkeypatch.setattr(
        stack_service,
        "resolve_direct_monitoring_links",
        lambda *_a, **_kw: (None, None),
    )
    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        lambda *_a, **_kw: _async_val(
            MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )
    monkeypatch.setattr(
        stack_service,
        "ensure_port_forward",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            PortForwardError('kubectl port-forward Bearer tok-xyz "/var/lib/lens/kubeconfig_cache/c1.kubeconfig"')
        ),
    )
    links_resp = await stack_service.get_links("custom-obs-ns", cluster_id="c-reinstall")
    by_kind = {link.kind: link for link in links_resp.links}
    assert by_kind["prometheus"].available is False
    assert "Invalid Prometheus service name" in (by_kind["prometheus"].message or "")
    assert by_kind["grafana"].available is False
    assert "tok-xyz" not in (by_kind["grafana"].message or "")
    assert "kubeconfig_cache" not in (by_kind["grafana"].message or "")
    assert "[REDACTED]" in (by_kind["grafana"].message or "")


def test_render_monitoring_stack_script_multi_namespace_overrides_legacy_and_ksm(tmp_path: Path):
    import hashlib
    import subprocess

    import yaml

    from llm_d_lens.monitoring.cluster_stack.service import (
        METRICS_READER_BINDING,
        METRICS_READER_ROLE,
        metrics_reader_binding_name,
        monitoring_namespace_hash,
        render_monitoring_stack_script,
    )

    assert monitoring_namespace_hash("llm-d-monitoring") is None
    assert metrics_reader_binding_name("llm-d-monitoring") == METRICS_READER_BINDING

    custom_ns = "llm-d-monitoring-x1"
    expected_hash = hashlib.sha256(custom_ns.encode("utf-8")).hexdigest()[:8]
    assert monitoring_namespace_hash(custom_ns) == expected_hash
    assert metrics_reader_binding_name(custom_ns) == f"{METRICS_READER_BINDING}-{expected_hash}"

    values_copy = tmp_path / "captured-values.yaml"
    applied_file = tmp_path / "applied.yaml"

    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "get" && "$2" == "namespace" ]]; then
  if [[ "${{NS_EXISTS:-0}}" == "1" ]]; then exit 0; else exit 1; fi
fi
if [[ "$1" == "get" && "$2" == "deploy" ]]; then
  if [[ "${{LEGACY_GRAFANA:-0}}" == "1" ]]; then
    printf 'deployment.apps/llmd-grafana\\n'
    exit 0
  fi
  exit 1
fi
if [[ "$1" == "get" && "$2" == "statefulset" ]]; then
  if [[ "${{LEGACY_PROM_STS:-0}}" == "1" ]]; then
    printf 'statefulset.apps/prometheus-llmd-kube-prometheus-stack-prometheus\\n'
    exit 0
  fi
  exit 1
fi
if [[ "$1" == "get" && "$2" == "pods,deployments" ]]; then
  if [[ "${{EXTERNAL_ADMISSION_POD:-0}}" == "1" ]]; then
    printf 'other-mon\\tllmd-1234abcd-admission-create-jw9k2\\tkube-prometheus-stack-prometheus-operator\\n'
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "pods,daemonsets" ]]; then
  if [[ "${{EXTERNAL_SUFFIXED_NE:-0}}" == "1" ]]; then
    printf 'other-mon\\tllmd-1234abcd-ne\\t\\tprometheus-node-exporter\\n'
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "deployments,pods" ]]; then
  if [[ "${{KSM_MODE:-none}}" == "ready_deploy_other" ]]; then
    printf 'llm-d-monitoring\\tDeployment\\tllmd-kube-state-metrics\\tkube-state-metrics\\t\\t1\\t1\\t\\n'
  elif [[ "${{KSM_MODE:-none}}" == "ready_suffixed_other" ]]; then
    printf 'other-mon\\tDeployment\\tllmd-1234abcd-ksm\\tkube-state-metrics\\t\\t1\\t1\\t\\n'
  elif [[ "${{KSM_MODE:-none}}" == "ready_pod_other" ]]; then
    printf 'kube-system\\tPod\\tkube-state-metrics-abc\\tkube-state-metrics\\t\\t\\t\\tTrue\\n'
  elif [[ "${{KSM_MODE:-none}}" == "unready_other" ]]; then
    printf 'other-mon\\tDeployment\\tllmd-1234abcd-ksm\\tkube-state-metrics\\t\\t1\\t0\\t\\n'
    printf 'kube-system\\tPod\\tkube-state-metrics-abc\\tkube-state-metrics\\t\\t\\t\\tFalse\\n'
  elif [[ "${{KSM_MODE:-none}}" == "ready_same_ns" ]]; then
    printf '%s\\tDeployment\\tllmd-%s-ksm\\tkube-state-metrics\\t\\t1\\t1\\t\\n' {custom_ns!r} {expected_hash!r}
  elif [[ "${{KSM_MODE:-none}}" == "unrelated_ksm_suffix" ]]; then
    printf 'other-ns\\tDeployment\\trouter-ksm\\trouter\\trouter\\t1\\t1\\t\\n'
  fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "prometheus" ]]; then
  printf '%s' "${{PROM_CR_SA:-}}"
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat >> {str(applied_file)!r}
  printf '\\n---\\n' >> {str(applied_file)!r}
  exit 0
fi
exit 0
"""
    helm_script = f"""#!/usr/bin/env bash
set -euo pipefail
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--values" ]]; then
    cp "$2" {str(values_copy)!r}
    shift 2
  else
    shift
  fi
done
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script, helm_body=helm_script)

    # 1. Default namespace ("llm-d-monitoring") keeps un-suffixed values & legacy ClusterRoleBinding name
    default_script = render_monitoring_stack_script(namespace="llm-d-monitoring")
    _assert_bash_syntax(default_script)
    applied_file.write_text("", encoding="utf-8")
    proc_def = subprocess.run(
        ["bash", "-s"], input=default_script, text=True, env=env, capture_output=True, check=False
    )
    assert proc_def.returncode == 0, proc_def.stderr
    def_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert "fullnameOverride" not in def_vals
    assert "fullnameOverride" not in def_vals["grafana"]
    assert "fullnameOverride" not in def_vals["kube-state-metrics"]
    assert "prometheus-node-exporter" not in def_vals
    assert def_vals["kubeStateMetrics"]["enabled"] is True
    def_rbac = [d for d in yaml.safe_load_all(applied_file.read_text(encoding="utf-8")) if isinstance(d, dict)]
    def_crb = next(d for d in def_rbac if d.get("kind") == "ClusterRoleBinding")
    assert def_crb["metadata"]["name"] == METRICS_READER_BINDING
    assert def_crb["subjects"] == [
        {"kind": "ServiceAccount", "name": "llmd-kube-prometheus-stack-prometheus", "namespace": "llm-d-monitoring"}
    ]

    # 2. New install in non-default namespace injects deterministic fullnameOverrides + per-namespace RBAC + suffixed fallback SA
    custom_script = render_monitoring_stack_script(namespace=custom_ns)
    _assert_bash_syntax(custom_script)
    applied_file.write_text("", encoding="utf-8")
    proc_new = subprocess.run(["bash", "-s"], input=custom_script, text=True, env=env, capture_output=True, check=False)
    assert proc_new.returncode == 0, proc_new.stderr
    new_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert new_vals["fullnameOverride"] == f"llmd-{expected_hash}"
    assert new_vals["grafana"]["fullnameOverride"] == f"llmd-{expected_hash}-grafana"
    assert new_vals["kube-state-metrics"]["fullnameOverride"] == f"llmd-{expected_hash}-ksm"
    assert new_vals["prometheus-node-exporter"]["fullnameOverride"] == f"llmd-{expected_hash}-ne"
    assert new_vals["kubeStateMetrics"]["enabled"] is True
    assert new_vals["kube-state-metrics"]["enabled"] is True

    new_rbac = [d for d in yaml.safe_load_all(applied_file.read_text(encoding="utf-8")) if isinstance(d, dict)]
    new_cr = next(d for d in new_rbac if d.get("kind") == "ClusterRole")
    new_crb = next(d for d in new_rbac if d.get("kind") == "ClusterRoleBinding")
    assert new_cr["metadata"]["name"] == METRICS_READER_ROLE
    assert new_crb["metadata"]["name"] == f"{METRICS_READER_BINDING}-{expected_hash}"
    assert new_crb["subjects"] == [
        {"kind": "ServiceAccount", "name": f"llmd-{expected_hash}-prometheus", "namespace": custom_ns}
    ]

    # 3. Pre-existing legacy Deployment/llmd-grafana OR StatefulSet preserves un-suffixed values for safe helm upgrade
    for legacy_var in ("LEGACY_GRAFANA", "LEGACY_PROM_STS"):
        env_legacy = dict(env, NS_EXISTS="1")
        env_legacy[legacy_var] = "1"
        applied_file.write_text("", encoding="utf-8")
        proc_leg = subprocess.run(
            ["bash", "-s"], input=custom_script, text=True, env=env_legacy, capture_output=True, check=False
        )
        assert proc_leg.returncode == 0, proc_leg.stderr
        leg_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
        assert "fullnameOverride" not in leg_vals
        assert "fullnameOverride" not in leg_vals["grafana"]
        assert "fullnameOverride" not in leg_vals["kube-state-metrics"]
        assert "prometheus-node-exporter" not in leg_vals
        leg_rbac = [d for d in yaml.safe_load_all(applied_file.read_text(encoding="utf-8")) if isinstance(d, dict)]
        leg_crb = next(d for d in leg_rbac if d.get("kind") == "ClusterRoleBinding")
        assert leg_crb["metadata"]["name"] == f"{METRICS_READER_BINDING}-{expected_hash}"
        assert leg_crb["subjects"] == [
            {"kind": "ServiceAccount", "name": "llmd-kube-prometheus-stack-prometheus", "namespace": custom_ns}
        ]

    # 4. External ready KSM (Deployment, suffixed Deployment, or Pod) disables bundled KSM while keeping fullnameOverride
    for ksm_mode in ("ready_deploy_other", "ready_suffixed_other", "ready_pod_other"):
        env_ksm = dict(env, KSM_MODE=ksm_mode, EXTERNAL_SUFFIXED_NE="1")
        proc_ksm = subprocess.run(
            ["bash", "-s"], input=custom_script, text=True, env=env_ksm, capture_output=True, check=False
        )
        assert proc_ksm.returncode == 0, proc_ksm.stderr
        ksm_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
        assert ksm_vals["kubeStateMetrics"]["enabled"] is False
        assert ksm_vals["kube-state-metrics"]["enabled"] is False
        assert ksm_vals["kube-state-metrics"]["fullnameOverride"] == f"llmd-{expected_hash}-ksm"
        assert ksm_vals["nodeExporter"]["enabled"] is False

    # 5. Unready KSM in another namespace, ready KSM in SAME namespace, unrelated *-ksm name,
    #    or admission webhook pod in another namespace keeps KSM_ENABLED="true" & OP_ENABLED="true"
    for ksm_mode in ("unready_other", "ready_same_ns", "unrelated_ksm_suffix"):
        env_keep = dict(env, KSM_MODE=ksm_mode, EXTERNAL_ADMISSION_POD="1")
        proc_keep = subprocess.run(
            ["bash", "-s"], input=custom_script, text=True, env=env_keep, capture_output=True, check=False
        )
        assert proc_keep.returncode == 0, proc_keep.stderr
        keep_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
        assert keep_vals["prometheusOperator"]["enabled"] is True
        assert keep_vals["kubeStateMetrics"]["enabled"] is True
        assert keep_vals["kube-state-metrics"]["enabled"] is True


@pytest.mark.asyncio
async def test_online_install_executes_disambiguated_wrapper_script(monkeypatch, tmp_path: Path):
    import asyncio
    from datetime import UTC, datetime

    import yaml

    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackStatusResponse,
        ClusterSummary,
        HelmReleaseSummary,
    )
    from llm_d_lens.monitoring.cluster_stack.operations import ClusterStackOperationManager, OperationStore
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    obs_dir = tmp_path / "llm-d" / "guides" / "recipes" / "observability"
    dash_dir = obs_dir / "grafana" / "dashboards"
    dash_dir.mkdir(parents=True)
    upstream_installer = obs_dir / "install-prometheus-grafana.sh"
    upstream_installer.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    upstream_installer.chmod(0o755)
    dash_loader = obs_dir / "load-llm-d-dashboards.sh"
    dash_marker = tmp_path / "dashboards-loaded.txt"
    dash_loader.write_text(
        f'#!/usr/bin/env bash\nprintf "%s" "$1" > {str(dash_marker)!r}\n',
        encoding="utf-8",
    )
    dash_loader.chmod(0o755)
    (dash_dir / "llm-d-vllm-overview.json").write_text('{"title":"vLLM"}', encoding="utf-8")

    values_copy = tmp_path / "online-values.yaml"
    applied_file = tmp_path / "online-applied.yaml"
    helm_calls = tmp_path / "online-helm.log"
    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "get" && "$2" == "namespace" ]]; then exit 1; fi
if [[ "$1" == "get" && "$2" == "deployments,pods" ]]; then
  printf 'llm-d-monitoring\\tDeployment\\tllmd-kube-state-metrics\\tkube-state-metrics\\t\\t1\\t1\\t\\n'
  exit 0
fi
if [[ "$1" == "get" && "$2" == "secret" ]]; then exit 0; fi
if [[ "$1" == "apply" ]]; then
  cat >> {str(applied_file)!r}
  printf '\\n---\\n' >> {str(applied_file)!r}
  exit 0
fi
exit 0
"""
    helm_script = f"""#!/usr/bin/env bash
set -euo pipefail
echo "helm $*" >> {str(helm_calls)!r}
if [[ "$1" == "status" ]]; then exit 0; fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--values" ]]; then
    cp "$2" {str(values_copy)!r}
    shift 2
  else
    shift
  fi
done
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script, helm_body=helm_script)
    monkeypatch.setenv("PATH", env["PATH"])

    target_ns = "lens-monitoring-abcd1234"
    ns_hash = stack_service.monitoring_namespace_hash(target_ns)
    assert ns_hash is not None

    status_state = {"value": "ready"}

    async def fake_discover(namespace: str, *_a, **_kw):
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="online-ctx", reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(status="deployed"),
            status=status_state["value"],  # type: ignore[arg-type]
            message=status_state["value"],
            observed_at=datetime.now(UTC),
        )

    manager = ClusterStackOperationManager(OperationStore(tmp_path / "ops"))
    monkeypatch.setattr(stack_service, "operations", manager)
    monkeypatch.setattr(stack_service, "installer_path", lambda _cid=None: _async_val(upstream_installer))
    monkeypatch.setattr(stack_service.runner, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_service, "discover_cluster_stack", fake_discover)
    monkeypatch.setattr(stack_service, "_runner", lambda _cid: None)
    monkeypatch.setattr(stack_service, "_safe_monitoring_settings", lambda _cid: {"prometheus_node_port": 30911})
    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        lambda *_a, **_kw: _async_val(
            MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="online-ctx")
        ),
    )

    op = await stack_service.install(
        ClusterStackInstallRequest(namespace=target_ns, mode="individual", enable_tls=True, reinstall=True),
        idempotency_key="online-reinstall-1",
        cluster_id="c-online",
    )
    completed = None
    for _ in range(100):
        completed = manager.get(op.operation_id)
        if completed and completed.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.02)

    assert completed is not None
    assert completed.status == "succeeded", completed.error
    helm_log = helm_calls.read_text(encoding="utf-8")
    assert f"helm uninstall llmd --namespace {target_ns}" in helm_log
    assert "helm upgrade --install llmd kube-prometheus-stack" in helm_log

    online_vals = yaml.safe_load(values_copy.read_text(encoding="utf-8"))
    assert online_vals["fullnameOverride"] == f"llmd-{ns_hash}"
    assert online_vals["grafana"]["fullnameOverride"] == f"llmd-{ns_hash}-grafana"
    assert online_vals["kube-state-metrics"]["fullnameOverride"] == f"llmd-{ns_hash}-ksm"
    assert online_vals["prometheus-node-exporter"]["fullnameOverride"] == f"llmd-{ns_hash}-ne"
    assert online_vals["kubeStateMetrics"]["enabled"] is False
    assert online_vals["prometheus"]["prometheusSpec"]["serviceMonitorSelector"] == {
        "matchLabels": {"monitoring-ns": target_ns}
    }
    assert online_vals["grafana"]["sidecar"]["dashboards"]["searchNamespace"] == target_ns
    assert f"https://llmd-{ns_hash}-prometheus.{target_ns}.svc.cluster.local:9090" in yaml.safe_dump(
        online_vals["grafana"]["datasources"]
    )

    online_rbac = [d for d in yaml.safe_load_all(applied_file.read_text(encoding="utf-8")) if isinstance(d, dict)]
    crb = next(d for d in online_rbac if d.get("kind") == "ClusterRoleBinding")
    assert crb["metadata"]["name"] == f"llm-d-prism-metrics-reader-{ns_hash}"
    assert crb["subjects"] == [{"kind": "ServiceAccount", "name": f"llmd-{ns_hash}-prometheus", "namespace": target_ns}]
    assert dash_marker.read_text(encoding="utf-8") == target_ns


@pytest.mark.asyncio
async def test_render_metrics_reader_rbac_script_and_runner_suffixed_sa(tmp_path: Path):
    import subprocess

    import yaml

    from llm_d_lens.monitoring.cluster_stack.service import (
        _discover_prometheus_service_account_via_runner,
        metrics_reader_binding_name,
        monitoring_namespace_hash,
        render_metrics_reader_rbac_script,
    )

    ns = "custom-mon-x2"
    ns_hash = monitoring_namespace_hash(ns)
    assert ns_hash is not None
    expected_binding = metrics_reader_binding_name(ns)
    expected_sa = f"llmd-{ns_hash}-prometheus"

    script = render_metrics_reader_rbac_script(monitoring_namespace=ns)
    _assert_bash_syntax(script)
    assert expected_binding in script

    applied_file = tmp_path / "rbac-suffixed.yaml"
    kubectl_script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "get" && "$2" == "clusterrole" ]]; then exit 0; fi
if [[ "$1" == "get" && "$2" == "clusterrolebinding" ]]; then
  if [[ "$3" == {expected_binding!r} && "${{OWN_BINDING_EXISTS:-0}}" == "1" ]]; then
    exit 0
  fi
  exit 1
fi
if [[ "$1" == "get" && "$2" == "prometheus" ]]; then exit 0; fi
if [[ "$1" == "get" && "$2" == "sa" ]]; then
  printf '%s' {expected_sa!r}
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat > {str(applied_file)!r}
  exit 0
fi
exit 0
"""
    _, env = _setup_stub_bin(tmp_path, kubectl_body=kubectl_script)

    # Legacy binding existing in another namespace does NOT skip creating custom-mon-x2's per-namespace binding
    proc = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(applied_file.read_text(encoding="utf-8")) if isinstance(d, dict)]
    crb = next(d for d in docs if d.get("kind") == "ClusterRoleBinding")
    assert crb["metadata"]["name"] == expected_binding
    assert crb["subjects"] == [{"kind": "ServiceAccount", "name": expected_sa, "namespace": ns}]

    # When own per-namespace binding exists, script skips idempotently
    env["OWN_BINDING_EXISTS"] = "1"
    proc_skip = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=False)
    assert proc_skip.returncode == 0
    assert "skipping" in proc_skip.stdout

    # Python runner SA fallback resolves suffixed SA when Prometheus CR list is empty
    class SaFallbackRunner:
        async def run(self, argv: list[str], timeout: float = 15) -> CommandResult:
            del timeout
            if argv[2] == "prometheuses":
                return _result(argv, json.dumps({"items": []}))
            if argv[2] == "serviceaccounts":
                return _result(argv, json.dumps({"items": [{"metadata": {"name": expected_sa}}]}))
            raise AssertionError(f"Unexpected argv: {argv}")

    assert await _discover_prometheus_service_account_via_runner(SaFallbackRunner(), ns) == expected_sa


@pytest.mark.asyncio
async def test_discovery_recognizes_suffixed_workloads_and_external_kube_state():
    from llm_d_lens.monitoring.cluster_stack.service import monitoring_namespace_hash

    ns = "llm-d-monitoring-x1"
    ns_hash = monitoring_namespace_hash(ns)
    assert ns_hash is not None

    # 1. Legacy stack with disabled local KSM + ready external KSM -> ready (not degraded), both labeled & unlabeled
    ext_labeled = await discover_cluster_stack(
        ns,
        FakeRunner(installed=True, drop_kube_state=True, external_kube_state=True, use_labels=True),
    )
    assert ext_labeled.status == "ready"
    ext_comps = {c.name: c for c in ext_labeled.components}
    assert ext_comps["kube_state_metrics"].status == "external"
    assert ext_comps["kube_state_metrics"].source == "cluster"

    ext_unlabeled = await discover_cluster_stack(
        ns,
        FakeRunner(installed=True, drop_kube_state=True, external_kube_state=True, use_labels=False),
    )
    assert ext_unlabeled.status == "ready"
    assert {c.name: c for c in ext_unlabeled.components}["kube_state_metrics"].status == "external"

    # 2. Full suffixed release workloads (including local suffixed operator/ksm/ne and completed admission pod ignored)
    class SuffixedRunner:
        def __init__(self, *, pods_mode: bool = False, local_ksm: bool = True) -> None:
            self.pods_mode = pods_mode
            self.local_ksm = local_ksm

        async def run(self, argv: list[str], timeout: float = 10) -> CommandResult:
            del timeout
            cmd = " ".join(argv)
            if cmd == "kubectl config current-context":
                return _result(argv, "kind-shared\n")
            if cmd == "kubectl cluster-info":
                return _result(argv, "running")
            if cmd.startswith("kubectl get clusterversion"):
                return _result(argv, returncode=1)
            if cmd.startswith("helm status"):
                return _result(argv, json.dumps({"version": 1, "info": {"status": "deployed"}}))
            if cmd.startswith("helm list"):
                return _result(argv, json.dumps([{"chart": "kube-prometheus-stack-72.6.2"}]))
            if "app.kubernetes.io/instance=llmd" in cmd:
                if self.pods_mode:
                    items = [
                        {
                            "kind": "Pod",
                            "metadata": {
                                "name": f"llmd-{ns_hash}-grafana-7b8c9d-abc12",
                                "labels": {"app.kubernetes.io/name": "grafana"},
                            },
                            "status": {"containerStatuses": [{"name": "grafana", "ready": True}]},
                        },
                        {
                            "kind": "Pod",
                            "metadata": {
                                "name": f"llmd-{ns_hash}-operator-6d5f4-xyz99",
                                "labels": {
                                    "app.kubernetes.io/name": "kube-prometheus-stack-prometheus-operator",
                                    "app": "kube-prometheus-stack-operator",
                                },
                            },
                            "status": {"containerStatuses": [{"name": "operator", "ready": True}]},
                        },
                        {
                            "kind": "Pod",
                            "metadata": {
                                "name": f"llmd-{ns_hash}-admission-create-jw9k2",
                                "labels": {
                                    "app.kubernetes.io/name": "kube-prometheus-stack-prometheus-operator",
                                    "app": "kube-prometheus-stack-admission",
                                },
                            },
                            "status": {
                                "phase": "Succeeded",
                                "containerStatuses": [
                                    {"name": "create", "ready": False, "state": {"terminated": {"reason": "Completed"}}}
                                ],
                            },
                        },
                        {
                            "kind": "Pod",
                            "metadata": {
                                "name": f"llmd-{ns_hash}-ne-k2m9p",
                                "labels": {"app.kubernetes.io/name": "prometheus-node-exporter"},
                            },
                            "status": {"containerStatuses": [{"name": "node-exporter", "ready": True}]},
                        },
                    ]
                    if self.local_ksm:
                        items.append(
                            {
                                "kind": "Pod",
                                "metadata": {
                                    "name": f"llmd-{ns_hash}-ksm-5c4d3-p0q1r",
                                    "labels": {"app.kubernetes.io/name": "kube-state-metrics"},
                                },
                                "status": {"containerStatuses": [{"name": "ksm", "ready": True}]},
                            }
                        )
                else:
                    items = [
                        FakeRunner._deployment(f"llmd-{ns_hash}-grafana"),
                        FakeRunner._deployment(f"llmd-{ns_hash}-operator"),
                        FakeRunner._daemonset(f"llmd-{ns_hash}-ne"),
                    ]
                    if self.local_ksm:
                        items.append(FakeRunner._deployment(f"llmd-{ns_hash}-ksm"))
                return _result(argv, json.dumps({"items": items}))
            if "app.kubernetes.io/name in (prometheus,alertmanager)" in cmd:
                items = [
                    FakeRunner._statefulset(f"prometheus-llmd-{ns_hash}-prometheus", "prometheus"),
                    FakeRunner._statefulset(f"alertmanager-llmd-{ns_hash}-alertmanager", "alertmanager"),
                ]
                return _result(argv, json.dumps({"items": items}))
            if "app.kubernetes.io/name=prometheus-operator" in cmd:
                return _result(argv, json.dumps({"items": []}))
            if "app=node-exporter" in cmd:
                return _result(argv, json.dumps({"items": []}))
            if "app.kubernetes.io/name=kube-state-metrics" in cmd:
                ext_items = [
                    {
                        "kind": "Deployment",
                        "metadata": {
                            "name": "llmd-99887766-ksm",
                            "namespace": "other-mon",
                            "labels": {"app.kubernetes.io/name": "kube-state-metrics"},
                        },
                        "spec": {"replicas": 1},
                        "status": {"replicas": 1, "readyReplicas": 1},
                    }
                ]
                return _result(argv, json.dumps({"items": ext_items}))
            if "--all-namespaces" in cmd and "pods,deployments" in cmd:
                return _result(argv, json.dumps({"items": []}))
            if "--all-namespaces" in cmd and "pods,daemonsets" in cmd:
                return _result(argv, json.dumps({"items": []}))
            if "grafana_dashboard=1" in cmd:
                return _result(
                    argv, json.dumps({"items": [{"kind": "ConfigMap", "metadata": {"name": "llm-d-overview"}}]})
                )
            if cmd.startswith("kubectl get crd"):
                return _result(argv, "crd")
            raise AssertionError(f"Unexpected cmd: {cmd}")

    workload_snap = await discover_cluster_stack(ns, SuffixedRunner(pods_mode=False, local_ksm=True))
    assert workload_snap.status == "ready"
    w_comps = {c.name: c for c in workload_snap.components}
    for key in ("prometheus", "grafana", "prometheus_operator", "alertmanager", "kube_state_metrics", "node_exporter"):
        assert w_comps[key].status == "ready", f"{key} was {w_comps[key]}"
        assert w_comps[key].source == "llmd"

    pods_ext_ksm_snap = await discover_cluster_stack(ns, SuffixedRunner(pods_mode=True, local_ksm=False))
    assert pods_ext_ksm_snap.status == "ready"
    p_comps = {c.name: c for c in pods_ext_ksm_snap.components}
    assert p_comps["prometheus_operator"].status == "ready"
    assert p_comps["node_exporter"].status == "ready"
    assert p_comps["kube_state_metrics"].status == "external"
    assert p_comps["kube_state_metrics"].source == "cluster"

    # 3. Unrelated *-ne / *-ksm workloads in other namespaces must NOT false-match during fallback scans
    from llm_d_lens.monitoring.cluster_stack.discovery import (
        _is_kube_state_metrics_resource,
        _is_node_exporter_resource,
    )

    assert _is_node_exporter_resource(FakeRunner._daemonset("gateway-ne")) is False
    assert _is_node_exporter_resource(FakeRunner._daemonset("logging-agent-ne")) is False
    assert _is_kube_state_metrics_resource(FakeRunner._deployment("router-ksm")) is False
    assert _is_node_exporter_resource(FakeRunner._daemonset(f"llmd-{ns_hash}-ne")) is True
    assert _is_kube_state_metrics_resource(FakeRunner._deployment(f"llmd-{ns_hash}-ksm")) is True
