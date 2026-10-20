"""Dashboard composition, scope and issue-page tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.permissions import BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS
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

    issue = next(issue for issue in cluster_issues(row, has_services=False) if issue.code == "monitoring.cluster_stack_degraded")

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
