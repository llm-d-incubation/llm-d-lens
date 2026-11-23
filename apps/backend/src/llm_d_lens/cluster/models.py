"""Request and response DTOs for the cluster management and overview API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- Cluster registry DTOs ---


class ProxyConfigDTO(StrictModel):
    """See docs/design/cluster-design.md#cluster-creation-wizard section 4.2.

    ``mode="auto"`` today falls back to whatever proxy env vars the Prism
    backend process itself sees (auto-detection of the target cluster's own
    proxy is not implemented yet -- see the design doc's Phase 2). ``custom``
    uses the three values below verbatim for this cluster only.
    """

    mode: Literal["auto", "custom"] = "auto"
    http_proxy: str | None = Field(default=None, alias="httpProxy")
    https_proxy: str | None = Field(default=None, alias="httpsProxy")
    no_proxy: str | None = Field(default=None, alias="noProxy")


class HfTokenSecretRef(StrictModel):
    """Coordinates of a cluster's HF_TOKEN Secret.

    Only the location is exposed; the token value stays on the target cluster
    and is never returned by the API.
    """

    namespace: str
    name: str


class ClusterRecord(StrictModel):
    id: str
    name: str
    description: str
    created_at: str = Field(alias="createdAt")
    ready: bool = False
    proxy: ProxyConfigDTO = Field(default_factory=ProxyConfigDTO)
    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_lensmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")
    llm_d_repo_path: str | None = Field(default=None, alias="llmDRepoPath")
    llm_d_lensmark_repo_path: str | None = Field(default=None, alias="llmDBenchmarkRepoPath")
    benchmark_source: dict | None = Field(default=None, alias="benchmarkSource")
    deployment_source: dict | None = Field(default=None, alias="deploymentSource")
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_namespace: str | None = Field(
        default=None,
        alias="gatewayNamespace",
        description="Effective Gateway namespace including the stable Lens cluster-registration short ID.",
    )
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    gateway_public_url: str | None = Field(default=None, alias="gatewayPublicUrl")
    gateway_port: int | None = Field(default=None, alias="gatewayPort")
    gateway_authz_host: str | None = Field(default=None, alias="gatewayAuthzHost")
    inotify_max_user_instances: int = Field(default=8192, alias="inotifyMaxUserInstances")
    router_version: str | None = Field(default=None, alias="routerVersion")
    gie_version: str | None = Field(default=None, alias="gieVersion")
    ipp_version: str | None = Field(default=None, alias="ippVersion")
    vllm_image: str | None = Field(default=None, alias="vllmImage")
    hf_token_secret: HfTokenSecretRef | None = Field(default=None, alias="hfTokenSecret")
    kubeconfig_namespace: str | None = Field(default=None, alias="kubeconfigNamespace")
    draft: bool = False
    monitoring_mode: Literal["managed", "existing"] = Field(default="managed", alias="monitoringMode")
    monitoring_namespace: str = Field(default="llm-d-monitoring", alias="monitoringNamespace")
    prometheus_url: str | None = Field(default=None, alias="prometheusUrl")
    grafana_url: str | None = Field(default=None, alias="grafanaUrl")
    prometheus_node_port: int | None = Field(default=None, alias="prometheusNodePort")
    grafana_node_port: int | None = Field(default=None, alias="grafanaNodePort")


class PlanResponse(StrictModel):
    """Stored provisioning plan plus the hashes derived from it."""

    plan: dict[str, Any]
    plan_hash: str = Field(alias="planHash")
    plan_version: int = Field(alias="planVersion")


class ClusterDetailResponse(StrictModel):
    """Cluster record, its stored plan (when any) and the derived hashes."""

    cluster: ClusterRecord
    plan: dict[str, Any] | None = None
    plan_hash: str | None = Field(default=None, alias="planHash")
    plan_version: int | None = Field(default=None, alias="planVersion")
    execution_operations: dict[str, str] | None = Field(
        default=None,
        alias="executionOperations",
        description="Current operation ledger (operation id -> user_done/admin_done/lens_done/failed).",
    )
    execution_completed_at: datetime | None = Field(
        default=None,
        alias="executionCompletedAt",
        description="When the most recent execution run completed, or null while unfinished.",
    )
    execution_completed_plan_hash: str | None = Field(
        default=None,
        alias="executionCompletedPlanHash",
        description="Plan hash recorded by the completed run; compare with planHash before treating it as current.",
    )
    draft_hf_token_saved: bool = Field(
        default=False,
        alias="draftHfTokenSaved",
        description="Whether an encrypted temporary HF token is currently stored for this draft cluster.",
    )


class KubeconfigReplaceResponse(StrictModel):
    """Result of ``PUT /clusters/{id}/kubeconfig``.

    Rotation never returns a capability report and never contains the
    kubeconfig or its credentials.
    """

    plan_hash: str | None = Field(default=None, alias="planHash")
    target_matched_by: str = Field(alias="targetMatchedBy")
    endpoint_changed: bool = Field(default=False, alias="endpointChanged")
    warning: str | None = None


class DraftHfTokenRequest(StrictModel):
    """``PUT /api/cluster/clusters/{id}/draft-hf-token`` payload."""

    token: SecretStr = Field(..., min_length=1, max_length=16_384)


class OperationExecuteRequest(StrictModel):
    """Body for ``POST /clusters/{id}/operations/{operation_id}/execute``."""

    secret_values: dict[str, str] | None = Field(default=None, alias="secretValues", repr=False)


class OperationExecuteResponse(StrictModel):
    """Result of one user operation execution."""

    operation_id: str = Field(alias="operationId")
    status: str
    token: str
    result: dict[str, Any] = Field(default_factory=dict)


class AdminHandoffStatusResponse(StrictModel):
    """Read-only marker state or the result of an admin-handoff verification."""

    plan_hash: str = Field(alias="planHash")
    status: str
    admin_operations: list[str] = Field(alias="adminOperations")
    remaining_operations: list[str] = Field(alias="remainingOperations")
    marker: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    token: str | None = None
    monitoring: dict[str, Any] | None = None


class RemediationResponse(StrictModel):
    """The generated Orchestration administrator operation script and handoff metadata."""

    filename: str
    script: str
    instructions: str
    plan_hash: str = Field(alias="planHash")
    admin_operations: list[str] = Field(alias="adminOperations")
    marker: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


class ExecutionCancelRequest(StrictModel):
    """``POST /clusters/{id}/execution/cancel`` payload."""

    token: str | None = None
    force: bool = False


class ClusterSettingsUpdateRequest(StrictModel):
    """``PATCH /api/cluster/clusters/{id}`` payload; every field is optional,
    only supplied keys are changed. Covers the wizard's review/runtime settings
    and the Clusters overview page's "Edit cluster" action
    (name/description/proxy/version refs). Draft state is finalized only by
    ``POST /execution/complete``. ``kubeconfig`` is rotated separately."""

    name: str | None = Field(default=None, min_length=1, max_length=253)
    description: str | None = None
    proxy: ProxyConfigDTO | None = None
    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_lensmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_namespace: str | None = Field(
        default=None,
        alias="gatewayNamespace",
        max_length=63,
        description="Gateway namespace prefix; Lens appends this registration's stable short ID for isolation. "
        "Passing the effective suffixed namespace preserves it.",
    )
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    gateway_public_url: str | None = Field(default=None, alias="gatewayPublicUrl")
    gateway_port: int | None = Field(default=None, alias="gatewayPort")
    gateway_authz_host: str | None = Field(default=None, alias="gatewayAuthzHost")
    inotify_max_user_instances: int | None = Field(default=None, ge=1, le=10_000_000, alias="inotifyMaxUserInstances")
    router_version: str | None = Field(default=None, alias="routerVersion")
    gie_version: str | None = Field(default=None, alias="gieVersion")
    ipp_version: str | None = Field(default=None, alias="ippVersion")
    vllm_image: str | None = Field(default=None, alias="vllmImage")
    draft: bool | None = None
    monitoring_mode: Literal["managed", "existing"] | None = Field(default=None, alias="monitoringMode")
    monitoring_namespace: str | None = Field(default=None, alias="monitoringNamespace")
    prometheus_url: str | None = Field(default=None, alias="prometheusUrl")
    grafana_url: str | None = Field(default=None, alias="grafanaUrl")
    prometheus_node_port: int | None = Field(default=None, alias="prometheusNodePort")
    grafana_node_port: int | None = Field(default=None, alias="grafanaNodePort")
    monitoring_host: str | None = Field(default=None, alias="monitoringHost")
    install_accelerator_exporters: bool | None = Field(default=None, alias="installAcceleratorExporters")


class HfTokenSecretUpdateRequest(StrictModel):
    """``PUT /api/cluster/clusters/{id}/hf-token-secret`` payload (Edit cluster).

    Rotates the token stored in the cluster's existing HF_TOKEN Secret. Omit
    ``namespace``/``name`` to rewrite the Secret the cluster already points at
    (``hfTokenSecret``); supply them to point the cluster at a different Secret,
    which is created when it does not exist yet."""

    namespace: str | None = Field(default=None, max_length=253)
    name: str | None = Field(default=None, max_length=253)
    token: str = Field(min_length=1)


class SoftwareDownloadRequest(StrictModel):
    """``POST /api/cluster/clusters/{id}/software-downloads`` payload.

    Kicks off (or reuses a cached) download for whichever refs are supplied;
    a ``None``/blank ref leaves that repo's download untouched."""

    llm_d_ref: str | None = Field(default=None, alias="llmDRef")
    llm_d_lensmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")


class RepoDownloadStatus(StrictModel):
    ref: str = ""
    state: Literal["idle", "downloading", "ready", "failed"] = "idle"
    path: str | None = None
    error: str | None = None


class SoftwareDownloadStatusResponse(StrictModel):
    llm_d: RepoDownloadStatus = Field(alias="llmD", default_factory=RepoDownloadStatus)
    llm_d_lensmark: RepoDownloadStatus = Field(alias="llmDBenchmark", default_factory=RepoDownloadStatus)


class ClustersResponse(StrictModel):
    items: list[ClusterRecord] = Field(default_factory=list)


class ClusterSessionResponse(StrictModel):
    cluster: ClusterRecord
    session_id: str = Field(alias="sessionId")


# --- Node maintenance (cordon/uncordon) DTOs ---


class NodeMaintenanceRequest(StrictModel):
    names: list[str] = Field(min_length=1, max_length=256)


class NodeMaintenanceResult(StrictModel):
    name: str
    ok: bool
    scheduling_disabled: bool | None = Field(default=None, alias="schedulingDisabled")
    error: str | None = None


class NodeMaintenanceResponse(StrictModel):
    items: list[NodeMaintenanceResult] = Field(default_factory=list)


class CreateClusterResponse(StrictModel):
    cluster: ClusterRecord
    session_id: str | None = Field(default=None, alias="sessionId")


# --- Kubernetes overview DTOs ---


class ComponentSummary(StrictModel):
    installed: bool
    ready: int
    desired: int
    status: str
    dashboards_ready: bool | None = Field(default=None, alias="dashboardsReady")
    dashboard_only: bool = Field(default=False, alias="dashboardOnly")
    message: str | None = None


class MonitoringComponentDiagnosticSummary(StrictModel):
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    resource: str | None = None


class MonitoringSubcomponentSummary(StrictModel):
    name: str
    status: str
    kind: Literal["workload", "configmap", "crd"] = "workload"
    ready: int | None = None
    desired: int | None = None
    source: Literal["llmd", "cluster", "none"] = "none"
    optional: bool = False
    diagnostics: list[MonitoringComponentDiagnosticSummary] = Field(default_factory=list)


class MonitoringComponentSummary(ComponentSummary):
    access_mode: Literal["admin", "non_admin"] | None = Field(default=None, alias="accessMode")
    monitoring_mode: Literal["managed", "existing"] | None = Field(default=None, alias="monitoringMode")
    can_manage: bool | None = Field(default=None, alias="canManage")
    namespace: str | None = None
    prometheus_url: str | None = Field(default=None, alias="prometheusUrl")
    grafana_url: str | None = Field(default=None, alias="grafanaUrl")
    components: list[MonitoringSubcomponentSummary] = Field(default_factory=list)


class ComponentsSummary(StrictModel):
    intel_device_plugin: ComponentSummary = Field(alias="intelDevicePlugin")
    monitoring: MonitoringComponentSummary
    accelerator_device_plugins: dict[str, ComponentSummary] = Field(
        default_factory=dict, alias="acceleratorDevicePlugins"
    )


class GpuDeviceSummary(StrictModel):
    id: str
    index: int = 0
    name: str
    model: str | None = None
    pci_address: str = Field(alias="pciAddress")
    compute_usage_percent: float = Field(default=0.0, alias="computeUsagePercent")
    vram_total_bytes: int = Field(default=0, alias="vramTotalBytes")
    vram_used_bytes: int | None = Field(default=None, alias="vramUsedBytes")
    vram_usage_percent: float | None = Field(default=None, alias="vramUsagePercent")
    vram_read_throughput_bytes: int = Field(default=0, alias="vramReadThroughputBytes")
    vram_write_throughput_bytes: int = Field(default=0, alias="vramWriteThroughputBytes")
    vram_total_throughput_bytes: int = Field(default=0, alias="vramTotalThroughputBytes")
    vram_bandwidth_percent: float = Field(default=0.0, alias="vramBandwidthPercent")


class NodeSummary(StrictModel):
    name: str
    version: str
    os_image: str = Field(alias="osImage")
    ready: bool
    scheduling_disabled: bool = Field(default=False, alias="schedulingDisabled")
    cpu: float = 0.0
    cpu_usage_percent: float | None = Field(default=None, alias="cpuUsagePercent")
    memory_bytes: int = Field(default=0, alias="memoryBytes")
    memory_usage_percent: float | None = Field(default=None, alias="memoryUsagePercent")
    gpu: bool = False
    gpu_count: int = Field(default=0, alias="gpuCount")
    gpu_by_profile: dict[str, int] = Field(default_factory=dict, alias="gpuByProfile")
    gpu_models_by_profile: dict[str, list[str]] = Field(default_factory=dict, alias="gpuModelsByProfile")
    gpu_usage_percent: float | None = Field(default=None, alias="gpuUsagePercent")
    vram_bytes: int = Field(default=0, alias="vramBytes")
    vram_usage_percent: float | None = Field(default=None, alias="vramUsagePercent")
    role: str = "worker"
    gpus: list[GpuDeviceSummary] = Field(default_factory=list)
    disk_bytes: int = Field(default=0, alias="diskBytes")
    disk_used_bytes: int | None = Field(default=None, alias="diskUsedBytes")
    disk_usage_percent: float | None = Field(default=None, alias="diskUsagePercent")
    cached_images: list[str] = Field(default_factory=list, alias="cachedImages")


class AcceleratorBucket(StrictModel):
    """Per-vendor accelerator count in the cluster hardware summary."""

    id: str
    gpu_count: int = Field(default=0, alias="gpuCount")
    models: list[str] = Field(default_factory=list)


class ClusterHardwareSummary(StrictModel):
    nodes: int = 0
    cpu_cores: float = Field(default=0.0, alias="cpuCores")
    cpu_usage_percent: float | None = Field(default=None, alias="cpuUsagePercent")
    memory_bytes: int = Field(default=0, alias="memoryBytes")
    memory_usage_percent: float | None = Field(default=None, alias="memoryUsagePercent")
    # Free capacity, so the UI can lead with what is left rather than what is
    # taken. ``None`` means the usage reading the figure derives from is
    # unavailable, not that nothing is free.
    memory_available_bytes: int | None = Field(default=None, alias="memoryAvailableBytes")
    gpu_nodes: int = Field(default=0, alias="gpuNodes")
    gpu_count: int = Field(default=0, alias="gpuCount")
    total_gpu_count: int = Field(default=0, alias="totalGpuCount")
    accelerators: list[AcceleratorBucket] = Field(default_factory=list)
    # Per-GPU payload keys the cluster's hardware profiles actually report;
    # the UI hides sections for metrics absent from this list.
    device_metrics: list[str] = Field(default_factory=list, alias="deviceMetrics")
    gpu_usage_percent: float | None = Field(default=None, alias="gpuUsagePercent")
    vram_bytes: int = Field(default=0, alias="vramBytes")
    vram_usage_percent: float | None = Field(default=None, alias="vramUsagePercent")
    disk_bytes: int = Field(default=0, alias="diskBytes")
    disk_used_bytes: int | None = Field(default=None, alias="diskUsedBytes")
    disk_available_bytes: int | None = Field(default=None, alias="diskAvailableBytes")
    disk_usage_percent: float | None = Field(default=None, alias="diskUsagePercent")
    allocated_gpu_count: int = Field(default=0, alias="allocatedGpuCount")
    available_gpu_count: int = Field(default=0, alias="availableGpuCount")
    usable_gpu_count: int = Field(default=0, alias="usableGpuCount")
    configured_gpu_limit: int | None = Field(default=None, alias="configuredGpuLimit")


class DeploymentSummary(StrictModel):
    name: str
    desired: int
    ready: int
    available: int


class NamespaceSummary(StrictModel):
    name: str
    deployments: list[DeploymentSummary] = Field(default_factory=list)
    ready: int


class KubernetesSummary(StrictModel):
    components: ComponentsSummary
    nodes: list[NodeSummary] = Field(default_factory=list)
    namespaces: list[NamespaceSummary] = Field(default_factory=list)
    hardware: ClusterHardwareSummary = Field(default_factory=ClusterHardwareSummary)
    telemetry_source: Literal["prometheus", "kubernetes", "hybrid", "unavailable"] = Field(
        default="kubernetes", alias="telemetrySource"
    )
    prometheus_reachable: bool = Field(default=False, alias="prometheusReachable")
    monitoring_required: bool = Field(default=False, alias="monitoringRequired")
    restricted_resources: list[str] = Field(default_factory=list, alias="restrictedResources")
    notice: str | None = None


class OverviewResponse(StrictModel):
    cluster: ClusterRecord
    fetched_at: str = Field(alias="fetchedAt")
    kubernetes: KubernetesSummary


# --- PCIe-root topology and placement DTOs ---


class TopologyRoot(StrictModel):
    root: str
    gpus: list[str]
    nics: list[str] = Field(description="Unallocated RDMA NICs still published on this root.")
    numa: int | None = None
    busy_gpus: int = Field(alias="busyGpus")
    free: bool = Field(description="Every GPU on the root is unallocated and, if required, its NIC is available.")


class TopologyNode(StrictModel):
    name: str
    ready: bool
    schedulable: bool
    roots: list[TopologyRoot]
    free_roots: int = Field(alias="freeRoots")
    busy_gpus: int = Field(alias="busyGpus")


class TopologyResponse(StrictModel):
    profile: str
    gpus_per_root: int = Field(alias="gpusPerRoot")
    requires_nic: bool = Field(alias="requiresNic")
    nodes: list[TopologyNode]


class PlacementRole(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    replicas: int = Field(ge=1, le=64)
    tensor_parallel_size: int = Field(alias="tensorParallelSize", ge=1, le=64)


class PlacementProbeRequest(StrictModel):
    roles: list[PlacementRole] = Field(min_length=1, max_length=8)
    role_nodes_disjoint: bool = Field(default=False, alias="roleNodesDisjoint")
    exclude_nodes: list[str] = Field(default_factory=list, alias="excludeNodes", max_length=256)


class PlacementProbeResponse(StrictModel):
    fits: bool
    assignment: dict[str, dict[str, int]] = Field(description="Role name -> node name -> pods placed there.")
    reasons: list[str]
    free_roots: dict[str, int] = Field(alias="freeRoots")
    unusable_nodes: list[str] = Field(alias="unusableNodes", description="NotReady, unschedulable or excluded nodes.")
