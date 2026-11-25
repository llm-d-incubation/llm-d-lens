"""Dashboard composition, scope and issue-page tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.permissions import BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS
from llm_d_lens.dashboard import collector as collector_module
from llm_d_lens.dashboard import service as service_module
from llm_d_lens.dashboard.collector import DashboardCollector
from llm_d_lens.dashboard.contracts import Coverage, DashboardIssue, WelcomeIssueSnapshot
from llm_d_lens.dashboard.service import DashboardService
from llm_d_lens.db.dao.welcome_issue_snapshot import WelcomeIssueSnapshotDao
from llm_d_lens.model_service.contracts import ModelServiceGroup, ModelServiceMember


def _overview(nodes: int = 2, ready: int = 2, gpu_total: int = 4, gpu_available: int = 1, node_disks=None):
    node_disks = node_disks or [None] * nodes
    return {
        "hardware": {
            "cpuCores": 32,
            "cpuUsagePercent": 20,
            "memoryBytes": 1024,
            "memoryUsagePercent": 30,
            "gpuCount": gpu_total,
            "totalGpuCount": gpu_total,
            "allocatedGpuCount": gpu_total - gpu_available,
            "availableGpuCount": gpu_available,
            "usableGpuCount": gpu_available,
            "gpuUsagePercent": 50,
            "vramUsagePercent": 40,
            "diskUsagePercent": 50,
            "accelerators": [{"id": "profile-a", "gpuCount": gpu_total, "models": ["A"]}],
            "deviceMetrics": ["gpuUsagePercent"],
        },
        "nodes": [
            {"name": f"worker-{index}", "ready": index < ready, "diskUsagePercent": node_disks[index]}
            for index in range(nodes)
        ],
        "components": {
            "monitoring": {"installed": True, "status": "ready"},
        },
    }


class FakeCollector(DashboardCollector):
    def __init__(self):
        super().__init__()
        self.clusters = [
            SimpleNamespace(id="a", name="Cluster A"),
            SimpleNamespace(id="b", name="Cluster B"),
        ]
        self.overview_calls: list[str] = []
        self.storage_volumes_value: list = []
        self.cache_entries_value: list = []

    def cluster_list(self, *, include_drafts: bool = False):
        return list(self.clusters)

    async def cluster_overview(self, cluster, *, force: bool = False):
        self.overview_calls.append(cluster.id)
        return _overview(), datetime.now(UTC), False, None

    async def gateway_status(self, *, force: bool = False):
        return None, None, False, "collecting"

    def storage_volumes(self):
        return self.storage_volumes_value

    async def model_cache_entries(self, *, force: bool = False):
        return self.cache_entries_value


class FakeModelService:
    def __init__(self):
        self.groups = [
            ModelServiceGroup(id="ga", name="model-a", modelRef="model-a", clusterId="a"),
            ModelServiceGroup(id="gb", name="model-b", modelRef="model-b", clusterId="b"),
        ]
        self.members = [
            ModelServiceMember(
                groupId="ga",
                executionId="exec-a",
                clusterId="a",
                targetNamespace="ns",
                targetService="svc",
                targetPort=8000,
                weight=1,
            ),
            ModelServiceMember(
                groupId="gb",
                executionId="exec-b",
                clusterId="b",
                targetNamespace="ns",
                targetService="svc",
                targetPort=8000,
                weight=1,
            ),
        ]

    def list_groups(self):
        return list(self.groups)

    def list_members(self):
        return list(self.members)


def _principal(role: str, permissions: frozenset[str], cluster: str | None = None) -> Principal:
    binding = RoleBinding(role, ScopeType.CLUSTER, scope_cluster_id=cluster) if cluster else RoleBinding(role)
    return Principal(
        user_id=f"{role}-user",
        username=f"{role}-user",
        bindings=(binding,),
        role_permissions={role: permissions},
    )


def _service(fake_model: FakeModelService | None = None):
    collector = FakeCollector()
    model = fake_model or FakeModelService()
    return DashboardService(collector=collector, model_service=model), collector, model


def _context(cluster_id: str, execution_id: str, status: str = "ready") -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        execution_id=execution_id,
        run_id="",
        cluster_id=cluster_id,
        status=status,
        name=execution_id,
        display_name=execution_id,
        model="model-a",
        created_at=now,
        updated_at=now,
        failure=None,
    )


@pytest.mark.asyncio
async def test_welcome_overview_scopes_issues_and_clusters(monkeypatch):
    service, collector, model = _service()
    monkeypatch.setattr(
        service_module,
        "list_execution_contexts",
        lambda **kwargs: (
            [
                _context("a", "exec-a"),
                _context("b", "exec-b"),
            ]
            if kwargs.get("allowed_cluster_ids") in (None, {"a", "b"})
            else [_context("a", "exec-a")]
        ),
    )
    principal = _principal(
        BUILTIN_ROLE_MAINTAINER,
        BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER],
        cluster="a",
    )

    overview = await service.welcome_overview(principal)
    payload = overview.model_dump(mode="json", by_alias=True)
    assert [cluster["id"] for cluster in payload["scope"]["clusters"]] == ["a"]
    assert collector.overview_calls == ["a"]
    assert "services" not in payload
    assert "deployments" not in payload
    assert "usage" not in payload
    assert "exec-b" not in str(payload)
    assert all(issue.first_seen_at is not None for issue in overview.issues.data)


def test_cluster_row_uses_worst_node_disk_and_flat_overview_shape():
    row = service_module._cluster_row(
        "a",
        "Cluster A",
        _overview(node_disks=[10, 94]),
        datetime.now(UTC),
        False,
        None,
    )
    assert row.disk_usage_percent == 50
    assert row.max_disk_usage_percent == 94
    assert row.disk_pressure_node == "worker-1"


def test_cluster_row_ignores_absent_vendor_device_plugins():
    overview = _overview(node_disks=[10, 10])
    overview["hardware"]["accelerators"] = [{"id": "intel-xpu", "gpuCount": 8, "models": ["B60"]}]
    overview["components"]["acceleratorDevicePlugins"] = {
        "intel-xpu": {"installed": True, "ready": 2, "desired": 2, "status": "ready"},
        "nvidia": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
    }
    # The legacy field must not add a second Intel row when the profile map exists.
    overview["components"]["intelDevicePlugin"] = {"installed": True, "ready": 2, "desired": 2, "status": "ready"}

    row = service_module._cluster_row("a", "Cluster A", overview, datetime.now(UTC), False, None)
    names = [component.name for component in row.components]
    assert names.count("intel-xpu accelerator-driver") == 1
    assert all("nvidia" not in name for name in names)


def test_progressing_accelerator_driver_is_not_a_welcome_issue():
    from llm_d_lens.dashboard.contracts import ClusterRow, ComponentRow
    from llm_d_lens.dashboard.rules import cluster_issues

    row = ClusterRow(
        id="a",
        name="Cluster A",
        available=True,
        nodes_ready=2,
        nodes_total=2,
        components=[ComponentRow(name="intel-xpu accelerator-driver", status="progressing")],
    )
    assert cluster_issues(row, has_services=False) == []


def test_missing_accelerator_driver_is_reported():
    from llm_d_lens.dashboard.contracts import ClusterRow, ComponentRow
    from llm_d_lens.dashboard.rules import cluster_issues

    row = ClusterRow(
        id="a",
        name="Cluster A",
        available=True,
        nodes_ready=2,
        nodes_total=2,
        components=[ComponentRow(name="intel-xpu accelerator-driver", status="missing")],
    )
    issues = cluster_issues(row, has_services=False)
    assert [issue.code for issue in issues] == ["cluster.accelerator_driver_missing"]


def test_unknown_accelerator_driver_and_restricted_workloads_do_not_trigger_missing_issue():
    from llm_d_lens.dashboard.contracts import ClusterRow, ComponentRow
    from llm_d_lens.dashboard.rules import cluster_issues

    # 1. Explicit "unknown" status does not emit cluster.accelerator_driver_missing.
    unknown_row = ClusterRow(
        id="a",
        name="sprocean-min",
        available=True,
        nodes_ready=2,
        nodes_total=2,
        components=[ComponentRow(name="intel-xpu accelerator-driver", status="unknown")],
    )
    assert cluster_issues(unknown_row, has_services=False) == []

    # 2. Defense-in-depth: cached/legacy overview with restrictedResources=["workloads"]
    #    coerces "missing" driver status to "unknown" (both flat and wrapped overview shapes).
    cached_overview = _overview(node_disks=[10, 10])
    cached_overview["restrictedResources"] = ["nodes", "workloads"]
    cached_overview["hardware"]["accelerators"] = [{"id": "intel-xpu", "gpuCount": 8, "models": ["B60"]}]
    cached_overview["components"]["acceleratorDevicePlugins"] = {
        "intel-xpu": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
    }
    row_flat = service_module._cluster_row("a", "sprocean-min", cached_overview, datetime.now(UTC), False, None)
    driver_comp = next(c for c in row_flat.components if c.name == "intel-xpu accelerator-driver")
    assert driver_comp.status == "unknown"
    assert cluster_issues(row_flat, has_services=False) == []

    row_wrapped = service_module._cluster_row(
        "a",
        "sprocean-min",
        {"kubernetes": cached_overview},
        datetime.now(UTC),
        False,
        None,
    )
    assert next(c for c in row_wrapped.components if c.name == "intel-xpu accelerator-driver").status == "unknown"
    assert cluster_issues(row_wrapped, has_services=False) == []

    # 3. Genuine "missing" without restricted workloads (including NFD-detected node profile with 0 schedulable GPUs)
    #    still emits cluster.accelerator_driver_missing.
    admin_nfd_overview = _overview(gpu_total=0, gpu_available=0, node_disks=[10, 10])
    admin_nfd_overview["restrictedResources"] = []
    admin_nfd_overview["hardware"]["accelerators"] = []
    admin_nfd_overview["nodes"][0]["gpuModelsByProfile"] = {"intel-xpu": []}
    admin_nfd_overview["components"]["acceleratorDevicePlugins"] = {
        "intel-xpu": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
        "nvidia": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
    }
    row_missing = service_module._cluster_row("a", "sprocean-min", admin_nfd_overview, datetime.now(UTC), False, None)
    issues = cluster_issues(row_missing, has_services=False)
    assert [issue.code for issue in issues] == ["cluster.accelerator_driver_missing"]
    assert issues[0].title == "sprocean-min: intel-xpu accelerator-driver is not installed"

    # 4. Malformed overview structures (string restrictedResources, non-iterable restrictedResources,
    #    malformed gpuByProfile / gpuModelsByProfile / acceleratorDevicePlugins) never split chars or raise TypeError.
    str_restricted = _overview(node_disks=[10, 10])
    str_restricted["restrictedResources"] = "workloads"
    str_restricted["hardware"]["accelerators"] = [{"id": "intel-xpu", "gpuCount": 4, "models": []}]
    str_restricted["components"]["acceleratorDevicePlugins"] = {
        "intel-xpu": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
    }
    row_str = service_module._cluster_row("a", "sprocean-min", str_restricted, datetime.now(UTC), False, None)
    assert next(c for c in row_str.components if c.name == "intel-xpu accelerator-driver").status == "unknown"
    assert cluster_issues(row_str, has_services=False) == []

    malformed_overview = _overview(node_disks=[10, 10])
    malformed_overview["restrictedResources"] = 123
    malformed_overview["nodes"][0]["gpuByProfile"] = "not-a-mapping"
    malformed_overview["nodes"][0]["gpuModelsByProfile"] = "not-a-mapping"
    malformed_overview["nodes"][1]["gpuByProfile"] = {"intel-xpu": "invalid-int"}
    malformed_overview["components"]["acceleratorDevicePlugins"] = "not-a-mapping"
    row_malformed = service_module._cluster_row("a", "sprocean-min", malformed_overview, datetime.now(UTC), False, None)
    assert row_malformed.available is True


def test_ready_deployment_without_a_model_service_is_flagged():
    from llm_d_lens.dashboard.rules import unpublished_deployment_issues

    context = SimpleNamespace(
        execution_id="exec-ready",
        name="qwen-ready",
        display_name="Qwen ready",
        cluster_id="a",
        status="ready",
        model="Qwen/Qwen3-8B",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        evaluate_owned=False,
    )

    issues = unpublished_deployment_issues([context], set(), {"a": "Cluster A"})
    assert [issue.code for issue in issues] == ["deployment.not_published"]
    assert issues[0].primary_action.params["executionId"] == "exec-ready"

    assert unpublished_deployment_issues([context], {"exec-ready"}, {"a": "Cluster A"}) == []

    context.evaluate_owned = True
    assert unpublished_deployment_issues([context], set(), {"a": "Cluster A"}) == []


def test_deployment_pod_health_maps_to_critical_and_warning_issues():
    from llm_d_lens.dashboard.rules import deployment_pod_issue

    unavailable = deployment_pod_issue(
        execution_id="exec-a",
        execution_name="qwen-a",
        cluster_id="a",
        cluster_name="Cluster A",
        ready=0,
        expected=1,
        observed_at=datetime.now(UTC),
    )
    degraded = deployment_pod_issue(
        execution_id="exec-a",
        execution_name="qwen-a",
        cluster_id="a",
        cluster_name="Cluster A",
        ready=1,
        expected=2,
        observed_at=datetime.now(UTC),
    )

    assert unavailable.severity == "critical"
    assert unavailable.code == "deployment.pods_unavailable"
    assert degraded.severity == "warning"
    assert degraded.code == "deployment.pods_degraded"


def test_stopped_gateway_and_ipp_are_cluster_level_issues():
    from llm_d_lens.dashboard.rules import gateway_cluster_issues

    base = {
        "cluster_id": "c",
        "cluster_name": "Cluster A",
        "members": [],
        "gateway_state": "installed",
        "gateway_ready": True,
        "gateway_running": True,
        "ipp_state": "installed",
        "ipp_ready": True,
    }
    stopped_gateway = SimpleNamespace(**{**base, "gateway_running": False})
    stopped_ipp = SimpleNamespace(**{**base, "ipp_ready": False})

    gateway_issues = gateway_cluster_issues(stopped_gateway, cluster_name="Cluster A", service_names=["model-a"])
    ipp_issues = gateway_cluster_issues(stopped_ipp, cluster_name="Cluster A", service_names=[])
    assert gateway_issues and gateway_issues[0].code == "model.gateway_unavailable"
    assert gateway_issues[0].severity == "critical"
    assert ipp_issues and ipp_issues[0].code == "model.ipp_unavailable"
    assert ipp_issues[0].severity == "warning"
    assert ipp_issues[0].affected["serviceCount"] == 0


def test_missing_hf_token_secret_is_critical():
    from llm_d_lens.dashboard.rules import cluster_issues

    row = service_module._cluster_row(
        "a",
        "Cluster A",
        _overview(),
        datetime.now(UTC),
        False,
        None,
        hf_token_secret_status="missing",  # noqa: S106
        hf_token_secret_ref="ns/hf-token",  # noqa: S106
    )
    issues = cluster_issues(row, has_services=False)
    secret_issues = [issue for issue in issues if issue.code == "cluster.hf_token_secret_missing"]
    assert secret_issues and secret_issues[0].severity == "critical"
    assert secret_issues[0].evidence["hfTokenSecret"] == "ns/hf-token"


def test_cluster_monitoring_degraded_is_critical():
    from llm_d_lens.dashboard.contracts import ClusterRow, ComponentRow
    from llm_d_lens.dashboard.rules import cluster_issues

    row = ClusterRow(
        id="a",
        name="Cluster A",
        available=True,
        nodes_ready=2,
        nodes_total=2,
        components=[ComponentRow(name="monitoring", status="degraded")],
    )
    issues = [
        issue for issue in cluster_issues(row, has_services=False) if issue.code == "monitoring.cluster_stack_degraded"
    ]
    assert issues and issues[0].severity == "critical"


def test_deployment_monitoring_unavailable_is_warning():
    from llm_d_lens.dashboard.rules import deployment_monitoring_issue

    unavailable = deployment_monitoring_issue(
        execution_id="e",
        execution_name="deployment-a",
        cluster_id="a",
        cluster_name="Cluster A",
        status="unreachable",
        stack_ready=False,
        message="cluster is not reachable",
    )
    degraded = deployment_monitoring_issue(
        execution_id="e",
        execution_name="deployment-a",
        cluster_id="a",
        cluster_name="Cluster A",
        status="enabled",
        stack_ready=False,
        message="monitoring enabled",
    )
    assert unavailable and unavailable.severity == "warning"
    assert degraded and degraded.severity == "warning"


def test_node_disk_pressure_appears_as_an_issue():
    from llm_d_lens.dashboard.contracts import ClusterRow
    from llm_d_lens.dashboard.rules import cluster_issues

    row = ClusterRow(
        id="a",
        name="Cluster A",
        available=True,
        nodes_ready=2,
        nodes_total=2,
        disk_usage_percent=50,
        max_disk_usage_percent=94,
        disk_pressure_node="worker-1",
    )
    issues = [issue for issue in cluster_issues(row, has_services=False) if issue.code == "cluster.disk_pressure"]
    assert len(issues) == 1
    assert issues[0].severity == "critical"
    assert issues[0].evidence["node"] == "worker-1"
    assert issues[0].evidence["diskUsagePercent"] == 94


@pytest.mark.asyncio
async def test_supporting_issues_cover_model_cache_readiness():
    service, collector, _ = _service()
    collector.storage_volumes_value = []
    collector.cache_entries_value = []

    issues = await service._supporting_issues(None, collector.clusters, {"a", "b"})
    missing = [issue for issue in issues if issue.code == "cluster.model_cache_storage_missing"]
    assert len(missing) == 2
    assert all(issue.severity == "critical" for issue in missing)
    assert missing[0].primary_action.view == "storage-management"
    assert missing[0].primary_action.params == {
        "clusterId": missing[0].cluster_id,
        "action": "create",
        "purpose": "model-cache",
    }
    assert missing[0].primary_action.permission == "storage:volume:create"

    collector.storage_volumes_value = [
        SimpleNamespace(
            id="v1",
            cluster_id="a",
            name="model-cache",
            purposes=[SimpleNamespace(value="model-cache")],
            status=SimpleNamespace(value="ready"),
            capacity="100Gi",
            used_bytes=None,
            updated_at=None,
        )
    ]
    issues = await service._supporting_issues(None, collector.clusters, {"a", "b"})
    by_cluster = {(issue.cluster_id, issue.code): issue for issue in issues}
    assert ("a", "cluster.model_cache_empty") in by_cluster
    assert by_cluster[("a", "cluster.model_cache_empty")].severity == "critical"
    assert ("a", "cluster.model_cache_storage_empty") in by_cluster
    assert by_cluster[("a", "cluster.model_cache_storage_empty")].severity == "warning"
    assert ("b", "cluster.model_cache_storage_missing") in by_cluster
    assert by_cluster[("b", "cluster.model_cache_storage_missing")].severity == "critical"

    collector.cache_entries_value = [
        SimpleNamespace(
            id="e1",
            cluster_id="a",
            storage_volume_id="v1",
            status=SimpleNamespace(value="ready"),
            source=SimpleNamespace(display_name=lambda: "org/model"),
            node_progress=[],
            updated_at=None,
        )
    ]
    issues = await service._supporting_issues(None, collector.clusters, {"a", "b"})
    assert all(not (issue.cluster_id == "a" and issue.code == "cluster.model_cache_empty") for issue in issues)


def test_no_ai_provider_issue_is_informational():
    from llm_d_lens.dashboard.rules import no_ai_provider_issue

    issue = no_ai_provider_issue()
    assert issue.code == "ai-provider.none_configured"
    assert issue.severity == "info"
    assert issue.category == "ai-provider"
    assert issue.primary_action.view == "ai-providers"


def test_no_cluster_issue_has_a_create_cluster_action():
    from llm_d_lens.dashboard.rules import no_cluster_issue

    issue = no_cluster_issue()
    assert issue.code == "cluster.none_available"
    assert issue.primary_action.view == "clusters"
    assert issue.primary_action.params["action"] == "create"


@pytest.mark.asyncio
async def test_welcome_overview_omits_unrelated_sections_without_permission():
    service, _, _ = _service()
    principal = _principal("viewer", frozenset({"cluster:cluster:read"}), cluster="a")

    overview = await service.welcome_overview(principal)
    payload = overview.model_dump(mode="json", by_alias=True)
    assert [cluster["id"] for cluster in payload["scope"]["clusters"]] == ["a"]
    assert "services" not in payload
    assert "deployments" not in payload
    assert "usage" not in payload


def test_role_less_users_exclude_direct_and_group_inherited_roles():
    users = [SimpleNamespace(id="u1"), SimpleNamespace(id="u2"), SimpleNamespace(id="u3")]
    user_bindings = [SimpleNamespace(user_id="u1")]
    group_bindings = [SimpleNamespace(group_id="g1")]
    memberships = [SimpleNamespace(user_id="u2", group_id="g1"), SimpleNamespace(user_id="u3", group_id="g2")]

    role_less = service_module._role_less_users(users, user_bindings, group_bindings, memberships)

    assert [user.id for user in role_less] == ["u3"]


def test_no_role_issues_name_affected_users_and_groups():
    from llm_d_lens.dashboard.rules import no_role_groups_issue, no_role_user_issue

    users_issue = no_role_user_issue(user_id="u1", username="alice")
    groups_issue = no_role_groups_issue(["viewers"])

    assert users_issue.severity == "info"
    assert users_issue.category == "access"
    assert users_issue.title == "alice: no role assigned"
    assert users_issue.resource["id"] == "u1"
    assert users_issue.primary_action.params == {"userId": "u1"}
    assert users_issue.primary_action.permission == "role:binding:manage"
    assert service_module._issue_permission(users_issue) == "user:user:read"
    assert groups_issue.category == "access"
    assert "viewers" in groups_issue.message
    assert groups_issue.primary_action.permission == "group:group:read"
    assert service_module._issue_permission(groups_issue) == "group:group:read"


def test_dashboard_only_monitoring_degradation_is_informational():
    from llm_d_lens.dashboard.contracts import ClusterRow, ComponentRow
    from llm_d_lens.dashboard.rules import cluster_issues

    row = ClusterRow(
        id="a",
        name="Cluster A",
        available=True,
        nodes_ready=1,
        nodes_total=1,
        components=[ComponentRow(name="monitoring", status="degraded")],
        monitoring_dashboard_only=True,
    )

    issue = next(
        issue for issue in cluster_issues(row, has_services=False) if issue.code == "monitoring.cluster_stack_degraded"
    )

    assert issue.severity == "info"
    assert issue.evidence["dashboardOnly"] is True
    assert "does not affect metric collection" in issue.message


def test_model_without_provider_only_offers_fix_action():
    from llm_d_lens.dashboard.contracts import ProviderCounts, ServiceRow
    from llm_d_lens.dashboard.rules import service_issues

    row = ServiceRow(
        group_id="ga",
        name="qwen3-0.6b",
        cluster_id="a",
        cluster_name="Cluster A",
        providers=ProviderCounts(),
    )

    issue = next(issue for issue in service_issues(row) if issue.code == "model.no_provider")

    assert issue.primary_action.intent == "manage_providers"
    assert issue.primary_action.permission == "model-service:member:manage"
    assert issue.secondary_action is None


def test_unhealthy_provider_issue_names_the_provider():
    from llm_d_lens.dashboard.contracts import ProviderCounts, ProviderHealth, ServiceRow
    from llm_d_lens.dashboard.rules import service_issues

    row = ServiceRow(
        group_id="ga",
        name="Qwen/Qwen3-0.6B",
        cluster_id="a",
        cluster_name="Cluster A",
        providers=ProviderCounts(total=2, active=2, ready=1, unhealthy=1),
        provider_details=[
            ProviderHealth(
                execution_id="exec-a",
                name="qwen-0-6b-a",
                status="unhealthy",
                weight=1,
                reason="deployment status is failed",
            ),
            ProviderHealth(execution_id="exec-b", name="qwen-0-6b-b", status="active", weight=1, ready=True),
        ],
    )

    issue = next(issue for issue in service_issues(row) if issue.code == "model.provider_unhealthy")

    assert "qwen-0-6b-a" in issue.title
    assert issue.evidence["unhealthyProviders"] == "qwen-0-6b-a"
    assert "deployment status is failed" in issue.message


@pytest.mark.asyncio
async def test_snapshot_preserves_each_issues_first_seen_time():
    service, _, _ = _service()

    first = await service.run_snapshot_check()
    assert first is not None
    first_issue = next(issue for issue in first.issues if issue.code == "cluster.model_cache_storage_missing")
    assert first_issue.first_seen_at is not None
    assert first.first_seen[first_issue.id] == first_issue.first_seen_at

    second = await service.run_snapshot_check()
    assert second is not None
    second_issue = next(issue for issue in second.issues if issue.code == "cluster.model_cache_storage_missing")

    assert second_issue.first_seen_at == first_issue.first_seen_at
    assert second.first_seen[second_issue.id] == first_issue.first_seen_at


@pytest.mark.asyncio
async def test_persisted_snapshot_filters_issues_by_permission_and_cluster():
    now = datetime.now(UTC)
    WelcomeIssueSnapshotDao().put(
        WelcomeIssueSnapshot(
            snapshot_id="snap-1",
            generated_at=now,
            observed_at=now,
            state="complete",
            coverage=Coverage(observed=2, expected=2, unit="clusters"),
            issues=[
                DashboardIssue(
                    id="storage-a",
                    code="storage.capacity_high",
                    severity="warning",
                    category="storage",
                    title="A storage is full",
                    message="A",
                    cluster_id="a",
                    resource={"type": "storage_volume", "id": "va"},
                ),
                DashboardIssue(
                    id="storage-b",
                    code="storage.capacity_high",
                    severity="warning",
                    category="storage",
                    title="B storage is full",
                    message="B",
                    cluster_id="b",
                    resource={"type": "storage_volume", "id": "vb"},
                ),
                DashboardIssue(
                    id="deployment-a",
                    code="deployment.recent_failure",
                    severity="warning",
                    category="deployment",
                    title="A deployment failed",
                    message="A",
                    cluster_id="a",
                    resource={"type": "deployment_execution", "id": "da"},
                ),
            ],
        )
    )
    service, _, _ = _service()
    principal = _principal(
        "reviewer",
        frozenset({"cluster:cluster:read", "storage:volume:read"}),
        cluster="a",
    )

    overview = await service.welcome_overview(principal)
    alerts = await service.alerts(principal, limit=10)

    assert [issue.id for issue in overview.issues.data] == ["storage-a"]
    assert [issue.id for issue in alerts.items] == ["storage-a"]
    assert alerts.total == 1


@pytest.mark.asyncio
async def test_alerts_paginate_a_stable_snapshot(monkeypatch):
    service, _, _ = _service()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        service_module,
        "list_execution_contexts",
        lambda **kwargs: [_context("a", f"exec-fail-{index}", status="failed") for index in range(3)],
    )
    monkeypatch.setattr(
        service_module,
        "_deployment_item",
        lambda context, names: service_module.DeploymentActivityItem(
            execution_id=context.execution_id,
            name=context.name,
            display_name=context.display_name,
            cluster_id=context.cluster_id,
            cluster_name="Cluster A",
            status=context.status,
            created_at=now,
            updated_at=now,
        ),
    )
    await service.run_snapshot_check()
    first = await service.alerts(None, limit=2)
    assert first.total >= 3
    assert len(first.items) <= 2
    assert first.next_cursor
    second = await service.alerts(None, limit=2, cursor=first.next_cursor)
    assert second.restarted is False
    assert second.snapshot_id == first.snapshot_id
    assert second.total == first.total


@pytest.mark.asyncio
async def test_selected_cluster_keeps_all_accessible_scope_options(monkeypatch):
    service, collector, _ = _service()
    selected = collector.clusters[0]
    monkeypatch.setattr(
        service_module.registry, "get_cluster", lambda cluster_id: selected if cluster_id == "a" else None
    )
    monkeypatch.setattr(service_module, "list_execution_contexts", lambda **kwargs: [])

    overview = await service.welcome_overview(None, cluster_id="a")
    payload = overview.model_dump(mode="json", by_alias=True)
    assert [cluster["id"] for cluster in payload["scope"]["clusters"]] == ["a", "b"]


def test_welcome_routes_are_registered(monkeypatch):
    service, _, _ = _service()
    from llm_d_lens.dashboard import router as dashboard_router

    monkeypatch.setattr(dashboard_router, "default_service", lambda: service)
    client = TestClient(app)
    overview = client.get("/api/v1/welcome/overview")
    assert overview.status_code == 200
    assert "issues" in overview.json()

    alerts = client.get("/api/v1/welcome/issues?severity=warning&limit=10")
    assert alerts.status_code == 200
    assert "snapshotId" in alerts.json()


@pytest.mark.asyncio
async def test_collector_overview_timeout_escalation_and_reset(monkeypatch):
    from llm_d_lens.dashboard.rules import cluster_issues

    cluster = SimpleNamespace(id="c1", name="sprocean-min")
    collector = DashboardCollector()
    monkeypatch.setattr(collector_module.registry, "get_cluster", lambda cid: cluster if cid == "c1" else None)

    async def timeout_overview(_cluster):
        raise TimeoutError

    monkeypatch.setattr(collector_module.cluster_service, "build_overview", timeout_overview)

    # 1st TimeoutError -> overview_timeout (suppressed by cluster_issues)
    await collector._refresh_overview("c1")
    _, _, _, reason1 = await collector.cluster_overview(cluster)
    assert reason1 == "overview_timeout"
    row1 = service_module._cluster_row("c1", "sprocean-min", None, None, False, reason1)
    assert cluster_issues(row1, has_services=False) == []

    # 2nd consecutive TimeoutError -> overview_unavailable (emits cluster.observation_unavailable)
    await collector._refresh_overview("c1")
    _, _, _, reason2 = await collector.cluster_overview(cluster)
    assert reason2 == "overview_unavailable"
    row2 = service_module._cluster_row("c1", "sprocean-min", None, None, False, reason2)
    assert [issue.code for issue in cluster_issues(row2, has_services=False)] == ["cluster.observation_unavailable"]

    # Success resets timeout counter so a subsequent single timeout returns to overview_timeout
    async def ok_overview(_cluster):
        return _overview()

    monkeypatch.setattr(collector_module.cluster_service, "build_overview", ok_overview)
    await collector._refresh_overview("c1")
    overview, _, stale, reason_ok = await collector.cluster_overview(cluster)
    assert overview is not None
    assert stale is False
    assert reason_ok is None
    assert "c1" not in collector._overview_timeouts

    # Failed forced refresh marks cached entry stale and TTL hit preserves entry.stale without retry storm
    monkeypatch.setattr(collector_module.cluster_service, "build_overview", timeout_overview)
    await collector._refresh_overview("c1")
    cached_val, _, cached_stale, _ = await collector.cluster_overview(cluster)
    assert cached_val is not None
    assert cached_stale is True

    # Non-timeout exception escalates to overview_unavailable immediately
    collector2 = DashboardCollector()

    async def auth_error(_cluster):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(collector_module.cluster_service, "build_overview", auth_error)
    await collector2._refresh_overview("c1")
    _, _, _, err_reason = await collector2.cluster_overview(cluster)
    assert err_reason == "overview_unavailable"

    collector.close()
    collector2.close()


@pytest.mark.asyncio
async def test_cold_run_snapshot_check_waits_for_overview_and_coalesces_refresh(monkeypatch):
    cluster = SimpleNamespace(
        id="c1",
        name="sprocean-min",
        hf_token_secret_namespace=None,
        hf_token_secret_name=None,
    )
    collector = DashboardCollector()
    monkeypatch.setattr(collector, "cluster_list", lambda include_drafts=False: [cluster])
    monkeypatch.setattr(collector, "storage_volumes", list)
    monkeypatch.setattr(collector_module.registry, "get_cluster", lambda cid: cluster if cid == "c1" else None)

    overview_calls = 0

    async def slow_overview(_cluster):
        nonlocal overview_calls
        overview_calls += 1
        await asyncio.sleep(0.02)
        return _overview()

    async def ok_secret(_cluster):
        return "present"

    monkeypatch.setattr(collector_module.cluster_service, "build_overview", slow_overview)
    monkeypatch.setattr(collector_module.cluster_service, "hf_token_secret_status", ok_secret)
    monkeypatch.setattr(collector_module.GatewayOpsService, "status", lambda self: asyncio.sleep(0, result=None))

    async def empty_cache(self):
        return []

    monkeypatch.setattr(collector_module.ModelCacheService, "list", empty_cache)
    monkeypatch.setattr(service_module, "list_execution_contexts", lambda **kwargs: [])

    dao = WelcomeIssueSnapshotDao()
    service = DashboardService(collector=collector, model_service=FakeModelService(), snapshot_dao=dao)

    # Cold run_snapshot_check waits for overview and persists state="complete", 1/1
    snapshot = await service.run_snapshot_check()
    assert snapshot is not None
    assert snapshot.state == "complete"
    assert snapshot.stale is False
    assert snapshot.observed_at is not None
    assert snapshot.coverage == Coverage(observed=1, expected=1, unit="clusters")
    assert overview_calls == 1

    # Cold recovery after prior overview_unavailable also waits for the fresh overview
    collector._overview.clear()
    collector._overview_errors["c1"] = "overview_unavailable"
    recovered = await service.run_snapshot_check()
    assert recovered is not None
    assert recovered.state == "complete"
    assert recovered.coverage == Coverage(observed=1, expected=1, unit="clusters")
    assert overview_calls == 2

    # welcome_overview(refresh=True) awaits coalesced snapshot refresh and returns updated snapshot
    refreshed = await service.welcome_overview(None, refresh=True)
    assert refreshed.issues.state == "complete"
    assert refreshed.issues.stale is False
    assert overview_calls == 3

    # Timeout while waiting in welcome_overview(refresh=True) returns existing snapshot marked stale=True
    async def hanging_refresh():
        await asyncio.sleep(1.0)
        return snapshot

    monkeypatch.setattr(service_module, "REFRESH_WAIT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service, "_run_snapshot_check_once", hanging_refresh)
    timed_out_view = await service.welcome_overview(None, refresh=True)
    assert timed_out_view.issues.stale is True

    if service._snapshot_task is not None and not service._snapshot_task.done():
        service._snapshot_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service._snapshot_task
    collector.close()


@pytest.mark.asyncio
async def test_wait_for_overview_returns_timeout_when_exceeded(monkeypatch):
    cluster = SimpleNamespace(id="c1", name="sprocean-min")
    collector = DashboardCollector()
    monkeypatch.setattr(collector_module.registry, "get_cluster", lambda cid: cluster if cid == "c1" else None)

    async def hanging_overview(_cluster):
        await asyncio.sleep(1.0)
        return _overview()

    monkeypatch.setattr(collector_module.cluster_service, "build_overview", hanging_overview)
    overview, observed_at, stale, reason = await collector.wait_for_overview(cluster, timeout=0.01)
    assert overview is None
    assert observed_at is None
    assert stale is False
    assert reason == "overview_timeout"
    collector.close()


def test_deployment_monitoring_issue_respects_cluster_monitoring_ready():
    from llm_d_lens.dashboard.rules import deployment_monitoring_issue

    # Suppressed when cluster monitoring is positively confirmed ready
    assert (
        deployment_monitoring_issue(
            execution_id="e1",
            execution_name="qwen",
            cluster_id="a",
            cluster_name="sprocean-min",
            status="enabled",
            stack_ready=False,
            message="monitoring enabled",
            cluster_monitoring_ready=True,
        )
        is None
    )

    # Preserved when cluster monitoring is False or None
    for ready_flag in (False, None):
        degraded = deployment_monitoring_issue(
            execution_id="e1",
            execution_name="qwen",
            cluster_id="a",
            cluster_name="sprocean-min",
            status="enabled",
            stack_ready=False,
            message="monitoring enabled",
            cluster_monitoring_ready=ready_flag,
        )
        assert degraded is not None
        assert degraded.code == "monitoring.deployment_unavailable"

    # Always warns on status == "unreachable" even if cluster monitoring is ready
    unreachable = deployment_monitoring_issue(
        execution_id="e1",
        execution_name="qwen",
        cluster_id="a",
        cluster_name="sprocean-min",
        status="unreachable",
        stack_ready=True,
        message="forbidden",
        cluster_monitoring_ready=True,
    )
    assert unreachable is not None
    assert unreachable.code == "monitoring.deployment_unavailable"


def test_gateway_cluster_issues_handles_non_admin_visibility_handoff_and_http_unhealthy():
    from llm_d_lens.dashboard.rules import gateway_cluster_issues

    base = {
        "cluster_id": "c1",
        "cluster_name": "sprocean-min",
        "members": [SimpleNamespace(execution_id="e1")],
        "gateway_state": "unknown",
        "gateway_ready": None,
        "gateway_running": None,
        "ipp_state": "unknown",
        "ipp_ready": None,
        "access_mode": "non_admin",
        "gateway_probe_source": "none",
        "reconcile_status": "synced",
        "auth_error": None,
        "pending_operations_count": 0,
    }

    # 1. Non-Admin visibility gap (probe_source="none", state="unknown") -> zero false critical alerts
    assert gateway_cluster_issues(SimpleNamespace(**base), cluster_name="sprocean-min", service_names=["qwen3"]) == []
    assert (
        gateway_cluster_issues(
            SimpleNamespace(**{**base, "gateway_state": "missing", "ipp_state": "missing"}),
            cluster_name="sprocean-min",
            service_names=["qwen3"],
        )
        == []
    )

    # 2. Expired token (auth_error set) suppresses both gateway unavailable and handoff warning
    assert (
        gateway_cluster_issues(
            SimpleNamespace(
                **{
                    **base,
                    "gateway_state": "missing",
                    "reconcile_status": "admin_handoff_required",
                    "auth_error": "kubeconfig token expired",
                }
            ),
            cluster_name="sprocean-min",
            service_names=["qwen3"],
        )
        == []
    )

    # 3. Non-Admin pending Orchestration administrator handoff -> warning
    handoff_issues = gateway_cluster_issues(
        SimpleNamespace(
            **{
                **base,
                "reconcile_status": "admin_handoff_required",
                "pending_operations_count": 2,
            }
        ),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in handoff_issues] == ["model.gateway_admin_handoff_required"]
    assert handoff_issues[0].severity == "warning"
    assert "Orchestration administrator" in handoff_issues[0].title
    assert "Orchestration administrator" in handoff_issues[0].message

    # 4. Non-Admin HTTP 5xx (probe_source="http", running=True, ready=False) -> "Gateway is unhealthy"
    http_unhealthy = gateway_cluster_issues(
        SimpleNamespace(
            **{
                **base,
                "gateway_state": "installed",
                "gateway_running": True,
                "gateway_ready": False,
                "gateway_probe_source": "http",
                "ipp_state": "missing",
            }
        ),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in http_unhealthy] == ["model.gateway_unavailable"]
    assert http_unhealthy[0].title == "sprocean-min: Gateway is unhealthy"

    # HTTP 5xx + admin_handoff_required -> only handoff warning (no duplicate critical)
    http_handoff = gateway_cluster_issues(
        SimpleNamespace(
            **{
                **base,
                "gateway_state": "installed",
                "gateway_running": True,
                "gateway_ready": False,
                "gateway_probe_source": "http",
                "reconcile_status": "admin_handoff_required",
            }
        ),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in http_handoff] == ["model.gateway_admin_handoff_required"]

    # 5. Non-Admin probe_source="http" still reports stopped IPP if Prometheus observed ipp_state="installed", ready=False
    http_with_stopped_ipp = gateway_cluster_issues(
        SimpleNamespace(
            **{
                **base,
                "gateway_state": "installed",
                "gateway_running": True,
                "gateway_ready": True,
                "gateway_probe_source": "http",
                "ipp_state": "installed",
                "ipp_ready": False,
            }
        ),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in http_with_stopped_ipp] == ["model.ipp_unavailable"]

    # 6. Positive k8s/prometheus failures still emit critical Gateway & warning IPP alerts
    prom_down = gateway_cluster_issues(
        SimpleNamespace(
            **{
                **base,
                "gateway_state": "missing",
                "gateway_probe_source": "prometheus",
            }
        ),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in prom_down] == ["model.gateway_unavailable"]
    assert prom_down[0].severity == "critical"


def test_service_availability_ignores_unobserved_non_admin_gateway():
    from llm_d_lens.dashboard.contracts import ProviderCounts

    providers = ProviderCounts(total=1, active=1, ready=1)
    gateway = SimpleNamespace(
        access_mode="non_admin",
        gateway_probe_source="none",
        gateway_state="missing",
        gateway_ready=False,
        gateway_running=False,
        ipp_state="missing",
        ipp_ready=False,
    )
    availability = service_module._service_availability(
        "active",
        providers,
        gateway,
        {"exec-a": SimpleNamespace(status="ready")},
        ["exec-a"],
        has_deployment_access=True,
        has_gateway_access=True,
    )
    assert availability == "available"


def test_service_issues_reports_unhealthy_provider_instead_of_no_provider():
    from llm_d_lens.dashboard.contracts import ProviderCounts, ProviderHealth, ServiceRow
    from llm_d_lens.dashboard.rules import service_issues

    row = ServiceRow(
        group_id="g1",
        name="qwen3",
        cluster_id="c1",
        cluster_name="sprocean-min",
        providers=ProviderCounts(total=1, active=0, ready=0, unhealthy=1),
        provider_details=[
            ProviderHealth(
                execution_id="exec-1",
                name="qwen-qwen3-0-6b",
                status="unhealthy",
                weight=1,
                ready=True,
                reason="epp=missing, ipp=missing, httpRoute=missing",
            )
        ],
        deployment_observed=True,
    )
    issues = service_issues(row)
    assert [issue.code for issue in issues] == ["model.provider_unhealthy"]
    assert issues[0].severity == "warning"
    assert "No healthy provider is currently serving" in issues[0].message


@pytest.mark.asyncio
async def test_supporting_issues_skips_cache_empty_when_cache_read_failed(monkeypatch):
    service, collector, _ = _service()
    collector.storage_volumes_value = [
        SimpleNamespace(
            id="v1",
            cluster_id="a",
            name="model-cache",
            purposes=[SimpleNamespace(value="model-cache")],
            status=SimpleNamespace(value="ready"),
            capacity="100Gi",
            used_bytes=None,
            updated_at=None,
        )
    ]

    # Use real DashboardCollector.model_cache_entries with failing ModelCacheService().list()
    real_collector = DashboardCollector()
    monkeypatch.setattr(real_collector, "storage_volumes", lambda: collector.storage_volumes_value)

    async def failing_list(self):
        raise TimeoutError

    monkeypatch.setattr(collector_module.ModelCacheService, "list", failing_list)
    service._collector = real_collector

    issues = await service._supporting_issues(None, [collector.clusters[0]], {"a"})
    assert real_collector._cache_failed is True
    assert not any(issue.code in {"cluster.model_cache_empty", "cluster.model_cache_storage_empty"} for issue in issues)

    # Recovery via _refresh_model_cache resets _cache_failed to False
    async def ok_list(self):
        return []

    monkeypatch.setattr(collector_module.ModelCacheService, "list", ok_list)
    await real_collector._refresh_model_cache()
    assert real_collector._cache_failed is False
    real_collector.close()


@pytest.mark.asyncio
async def test_compose_suppresses_degraded_deployment_monitoring_when_cluster_monitoring_ready(monkeypatch):
    service, collector, _ = _service()
    monkeypatch.setattr(service_module, "list_execution_contexts", lambda **kwargs: [_context("a", "exec-a")])

    async def fake_dep_mon(cluster_id, *, force=False):
        return (
            {
                "items": [
                    {
                        "execution_id": "exec-a",
                        "status": "enabled",
                        "stack_ready": False,
                        "message": "monitoring enabled",
                    }
                ]
            }
            if cluster_id == "a"
            else {"items": []}
        )

    monkeypatch.setattr(collector, "deployment_monitoring_status", fake_dep_mon)
    composition = await service._compose(None, [collector.clusters[0]], cluster_id="a", refresh=False)
    assert not any(issue.code == "monitoring.deployment_unavailable" for issue in composition.issues)


def test_http_unhealthy_gateway_not_silently_suppressed_without_handoff_warning():
    from llm_d_lens.dashboard.rules import gateway_cluster_issues

    # Admin cluster with stale reconcile_status="admin_handoff_required" or Non-Admin with no
    # services/members/pending_ops does NOT emit handoff warning, so HTTP 5xx must still report unhealthy.
    for access_mode, members in [("admin", [SimpleNamespace(execution_id="e1")]), ("non_admin", [])]:
        gateway = SimpleNamespace(
            cluster_id="c1",
            cluster_name="sprocean-min",
            members=members,
            gateway_state="installed",
            gateway_ready=False,
            gateway_running=True,
            ipp_state="unknown",
            ipp_ready=None,
            access_mode=access_mode,
            gateway_probe_source="http",
            reconcile_status="admin_handoff_required",
            auth_error=None,
            pending_operations_count=0,
        )
        issues = gateway_cluster_issues(gateway, cluster_name="sprocean-min", service_names=[])
        assert [issue.code for issue in issues] == ["model.gateway_unavailable"]
        assert issues[0].title == "sprocean-min: Gateway is unhealthy"


@pytest.mark.asyncio
async def test_gateway_issues_enforce_gateway_and_group_read_per_cluster_and_snapshot(monkeypatch):
    service, collector, _ = _service()
    now = datetime.now(UTC)
    gateway_issues = [
        DashboardIssue(
            id="gw-a",
            code="model.gateway_unavailable",
            severity="critical",
            category="model-service",
            title="A gateway unavailable",
            message="A",
            cluster_id="a",
            resource={"type": "cluster_gateway", "id": "a"},
        ),
        DashboardIssue(
            id="ipp-a",
            code="model.ipp_unavailable",
            severity="warning",
            category="model-service",
            title="A IPP unavailable",
            message="A",
            cluster_id="a",
            resource={"type": "cluster_ipp", "id": "a"},
        ),
        DashboardIssue(
            id="handoff-a",
            code="model.gateway_admin_handoff_required",
            severity="warning",
            category="model-service",
            title="A handoff",
            message="A",
            cluster_id="a",
            resource={"type": "cluster_gateway", "id": "a"},
        ),
    ]
    WelcomeIssueSnapshotDao().put(
        WelcomeIssueSnapshot(
            snapshot_id="snap-gw",
            generated_at=now,
            observed_at=now,
            state="complete",
            coverage=Coverage(observed=2, expected=2, unit="clusters"),
            issues=gateway_issues,
        )
    )

    # Snapshot filtering: needs BOTH model-service:gateway:read AND model-service:group:read
    only_group = _principal("group-only", frozenset({"cluster:cluster:read", "model-service:group:read"}), cluster="a")
    only_gateway = _principal(
        "gateway-only", frozenset({"cluster:cluster:read", "model-service:gateway:read"}), cluster="a"
    )
    both = _principal(
        "both",
        frozenset({"cluster:cluster:read", "model-service:group:read", "model-service:gateway:read"}),
        cluster="a",
    )
    assert (await service.welcome_overview(only_group)).issues.data == []
    assert (await service.welcome_overview(only_gateway)).issues.data == []
    assert [issue.id for issue in (await service.welcome_overview(both)).issues.data] == [
        "gw-a",
        "ipp-a",
        "handoff-a",
    ]

    # Zero-cluster principal must not trigger global snapshot refresh via refresh=True
    no_cluster_principal = _principal("none", frozenset({"cluster:cluster:read"}), cluster="nonexistent")
    refresh_calls = 0

    def fail_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        raise AssertionError("request_snapshot_check should not be called for zero-cluster principal")

    monkeypatch.setattr(service, "request_snapshot_check", fail_refresh)
    empty_view = await service.welcome_overview(no_cluster_principal, refresh=True)
    assert empty_view.issues.data == []
    assert refresh_calls == 0

    # Per-cluster gateway:read check inside _compose: principal has gateway:read on A only, cluster:read on A & B
    gateway_status = SimpleNamespace(
        clusters=[
            SimpleNamespace(
                cluster_id="a",
                cluster_name="Cluster A",
                members=[SimpleNamespace(execution_id="exec-a")],
                gateway_state="installed",
                gateway_ready=False,
                gateway_running=False,
                ipp_state="installed",
                ipp_ready=True,
                access_mode="admin",
                gateway_probe_source="k8s",
                reconcile_status="synced",
                auth_error=None,
                pending_operations_count=0,
            ),
            SimpleNamespace(
                cluster_id="b",
                cluster_name="Cluster B",
                members=[SimpleNamespace(execution_id="exec-b")],
                gateway_state="installed",
                gateway_ready=False,
                gateway_running=False,
                ipp_state="installed",
                ipp_ready=True,
                access_mode="admin",
                gateway_probe_source="k8s",
                reconcile_status="synced",
                auth_error=None,
                pending_operations_count=0,
            ),
        ]
    )

    async def fake_gateway_status(*, force=False):
        return gateway_status, now, False, None

    monkeypatch.setattr(collector, "gateway_status", fake_gateway_status)
    monkeypatch.setattr(service_module, "list_execution_contexts", lambda **kwargs: [])

    scoped_principal = Principal(
        user_id="scoped",
        username="scoped",
        bindings=(
            RoleBinding("cluster-a-gw", ScopeType.CLUSTER, scope_cluster_id="a"),
            RoleBinding("cluster-b-read", ScopeType.CLUSTER, scope_cluster_id="b"),
        ),
        role_permissions={
            "cluster-a-gw": frozenset(
                {"cluster:cluster:read", "model-service:group:read", "model-service:gateway:read"}
            ),
            "cluster-b-read": frozenset({"cluster:cluster:read", "model-service:group:read"}),
        },
    )
    composition = await service._compose(scoped_principal, collector.clusters, cluster_id=None, refresh=False)
    gw_clusters = [issue.cluster_id for issue in composition.issues if issue.code == "model.gateway_unavailable"]
    assert gw_clusters == ["a"]


def test_non_admin_prometheus_healthy_gateway_with_missing_ipp():
    from llm_d_lens.dashboard.rules import gateway_cluster_issues

    healthy_gw_missing_ipp = {
        "cluster_id": "c-na",
        "cluster_name": "sprocean-min",
        "members": [],
        "gateway_state": "installed",
        "gateway_ready": True,
        "gateway_running": True,
        "ipp_state": "missing",
        "ipp_ready": False,
        "access_mode": "non_admin",
        "gateway_probe_source": "prometheus",
        "reconcile_status": "synced",
        "auth_error": None,
        "pending_operations_count": 0,
    }
    assert (
        gateway_cluster_issues(SimpleNamespace(**healthy_gw_missing_ipp), cluster_name="sprocean-min", service_names=[])
        == []
    )
    with_services = gateway_cluster_issues(
        SimpleNamespace(**{**healthy_gw_missing_ipp, "members": [SimpleNamespace(execution_id="e1")]}),
        cluster_name="sprocean-min",
        service_names=["qwen3"],
    )
    assert [issue.code for issue in with_services] == ["model.ipp_unavailable"]
    assert with_services[0].severity == "warning"


def test_non_admin_synced_http_probed_qwen3_has_available_status_and_no_issues():
    from llm_d_lens.dashboard.contracts import ProviderCounts
    from llm_d_lens.dashboard.rules import gateway_cluster_issues, service_issues

    gateway = SimpleNamespace(
        cluster_id="c-na",
        cluster_name="sprocean-min",
        members=[SimpleNamespace(execution_id="exec-qwen3")],
        gateway_state="installed",
        gateway_ready=True,
        gateway_running=True,
        ipp_state="installed",
        ipp_ready=True,
        access_mode="non_admin",
        gateway_probe_source="http",
        reconcile_status="synced",
        auth_error=None,
        pending_operations_count=0,
    )
    gateway_status = SimpleNamespace(clusters=[gateway])
    group = SimpleNamespace(id="g-qwen3", name="qwen3", cluster_id="c-na", status="active")
    member = SimpleNamespace(
        group_id="g-qwen3",
        execution_id="exec-qwen3",
        cluster_id="c-na",
        status="active",
        weight=100,
        health_json={
            "ready": True,
            "components": {"epp": "ready", "httpRoute": "ready", "ipp": "ready", "serving": "ready"},
        },
    )
    context_by_id = {
        "exec-qwen3": SimpleNamespace(
            execution_id="exec-qwen3", name="qwen3-vllm", display_name="qwen3", status="ready"
        )
    }
    principal = _principal(BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER])
    svc = service_module.DashboardService()
    rows, state, _reason = svc._service_rows(
        principal,
        [SimpleNamespace(id="c-na", name="sprocean-min")],
        [group],
        {"g-qwen3": [member]},
        gateway_status,
        context_by_id,
        {},
        {"c-na"},
        True,
    )
    assert state == "complete"
    assert len(rows) == 1
    assert rows[0].availability == "available"
    assert rows[0].providers == ProviderCounts(total=1, active=1, ready=1, unhealthy=0)
    assert service_issues(rows[0]) == []
    assert gateway_cluster_issues(gateway, cluster_name="sprocean-min", service_names=["qwen3"]) == []
