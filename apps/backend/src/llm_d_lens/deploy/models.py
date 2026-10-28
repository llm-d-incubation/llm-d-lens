"""Request and response contracts owned by the Deploy module.

The deployment lifecycle contracts live in :mod:`llm_d_lens.deploy.contracts`.
This module re-exports the canonical request/domain classes and defines the
concrete HTTP response projections used by the local and remote deploy routes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm_d_lens.deploy.contracts import (
    DeploymentCase,
    DeploymentCaseStatus,
    DeploymentCreateRequest,
    DeploymentEndpoint,
    DeploymentExecution,
    DeploymentLogEntry,
    DeploymentLogPage,
    DeploymentLogRequest,
    DeploymentMetadata,
    DeploymentMetadataUpdateRequest,
    DeploymentRun,
    DeploymentRunCreateRequest,
    DeploymentRunStatus,
    DeploymentStatus,
    ProblemDetails,
    VersionedPayload,
)
from llm_d_lens.deploy.standard_kubernetes_service import StandardKubernetesServiceRequest


class CleanCaseRequest(BaseModel):
    preserve_rendered_overlay: bool = False


class RebindClusterSessionRequest(BaseModel):
    cluster_session_id: str


class CleanOrphansRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cluster_id: str = Field(alias="clusterId")
    namespaces: list[str] = Field(default_factory=list)


class RestartCaseRequest(BaseModel):
    model_token: str | None = None


class DeploymentExecutionResponse(BaseModel):
    """Execution projection accepted by deployment API clients."""

    execution_id: str
    status: str
    cluster_id: str | None = None
    cluster_session_id: str | None = None
    namespace: str | None = None
    name: str | None = None
    display_name: str = ""
    description: str = ""
    model: str | None = None
    backend: str | None = None
    guide: str | None = None
    endpoint: str | None = None
    forwarded_endpoint: str | None = None
    replicas: int | None = None
    tensor_parallel_size: int | None = None
    image: str | None = None
    preserve_deployment: bool = False
    created_at: Any = None
    updated_at: Any = None
    max_model_len: int | None = None
    gpu_memory_utilization: Any = None
    enable_prefix_caching: Any = None
    max_num_seqs: Any = None
    max_num_batched_tokens: Any = None
    storage_type: str | None = None
    storage_volume_id: str | None = None
    mount_path: str | None = None
    pvc_name: str | None = None
    failure: dict[str, Any] | None = None
    custom_parameters: list[dict[str, Any]] = Field(default_factory=list)
    baseline_endpoint: str | None = None
    service_ref: str | None = None
    scale: dict[str, Any] | None = None

    @classmethod
    def from_context(cls, context: Any) -> DeploymentExecutionResponse:
        created_at = context.created_at
        return cls(
            execution_id=context.execution_id,
            status=context.status,
            cluster_id=context.cluster_id,
            cluster_session_id=context.cluster_session_id,
            namespace=context.namespace,
            name=context.name,
            display_name=context.display_name,
            description=context.description,
            model=context.model,
            backend=context.backend,
            guide=context.guide,
            endpoint=context.endpoint,
            forwarded_endpoint=context.forwarded_endpoint,
            replicas=context.replicas,
            tensor_parallel_size=context.tensor_parallel_size,
            image=context.image,
            preserve_deployment=context.preserve_deployment,
            created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            updated_at=_isoformat(context.updated_at),
            max_model_len=context.max_model_len,
            gpu_memory_utilization=context.gpu_memory_utilization,
            enable_prefix_caching=context.enable_prefix_caching,
            max_num_seqs=context.max_num_seqs,
            max_num_batched_tokens=context.max_num_batched_tokens,
            storage_type=context.storage_type,
            storage_volume_id=context.storage_volume_id,
            mount_path=context.mount_path,
            pvc_name=context.pvc_name,
            failure=context.failure,
            custom_parameters=context.custom_parameters or [],
            baseline_endpoint=context.baseline_endpoint,
            service_ref=context.service_ref,
            scale=context.scale,
        )


class DeploymentExecutionListResponse(BaseModel):
    items: list[DeploymentExecutionResponse] = Field(default_factory=list)


class DeploymentNodeLabel(BaseModel):
    key: str
    value: str


class DeploymentNodeLabelsResponse(BaseModel):
    items: list[DeploymentNodeLabel] = Field(default_factory=list)


class DeploymentRuntimeClassItem(BaseModel):
    name: str
    handler: str | None = None
    scheduling: dict[str, Any] = Field(default_factory=dict)
    overhead: dict[str, Any] = Field(default_factory=dict)
    matching_nodes: list[str] = Field(default_factory=list)


class DeploymentRuntimeClassesResponse(BaseModel):
    items: list[DeploymentRuntimeClassItem] = Field(default_factory=list)
    profile_suggestion: str | None = None
    available_suggestion: str | None = None
    requirement: str = "unknown"
    node_runtime_readiness: str = "unknown"
    warnings: list[str] = Field(default_factory=list)


class DeploymentPodContainer(BaseModel):
    name: str | None = None
    image: str | None = None
    ready: bool = False
    restart_count: int = 0
    state: str = "unknown"


class DeploymentPod(BaseModel):
    name: str | None = None
    namespace: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    phase: str = "Unknown"
    ready: bool = False
    pod_ip: str | None = None
    node_name: str | None = None
    restarts: int = 0
    status_reason: str = "Unknown"
    containers: list[DeploymentPodContainer] = Field(default_factory=list)
    creation_timestamp: str | None = None


class DeploymentPodsResponse(BaseModel):
    execution_id: str
    namespace: str | None = None
    pods: list[DeploymentPod] = Field(default_factory=list)
    error: str | None = None


class DeploymentPodLogsResponse(BaseModel):
    execution_id: str
    namespace: str | None = None
    pod_name: str
    container: str | None = None
    logs: str = ""
    success: bool = False


class DeploymentEndpointResponse(BaseModel):
    success: bool = True
    execution_id: str
    id: str
    local_port: int
    endpoint: str
    forwarded_endpoint: str
    namespace: str | None = None
    service: str | None = None
    remote_port: int | None = None

    @classmethod
    def from_resolved(cls, execution_id: str, resolved: Any) -> DeploymentEndpointResponse:
        return cls(
            success=True,
            execution_id=execution_id,
            id=resolved.forward_id,
            local_port=resolved.local_port,
            endpoint=resolved.endpoint,
            forwarded_endpoint=resolved.endpoint,
            namespace=resolved.namespace,
            service=resolved.service,
            remote_port=resolved.remote_port,
        )


class DeploymentSummary(BaseModel):
    """Serving summary for a deployment run case."""

    run_id: str | None = None
    case_id: str | None = None
    execution_id: str | None = None
    name: str | None = None
    display_name: str = ""
    description: str = ""
    status: str | None = None
    namespace: str | None = None
    endpoint: str | None = None
    forwarded_endpoint: str | None = None
    guide: str | None = None
    backend: str | None = None
    model_name: str | None = None
    replicas: int | None = None
    tensor_parallel_size: int | None = None
    image: str | None = None

    @classmethod
    def from_case(cls, case: Any, execution: Any) -> DeploymentSummary:
        from llm_d_lens.deploy.executions import serving_metadata

        serving = serving_metadata(case, execution)
        metadata = getattr(execution, "metadata", None)
        return cls(
            run_id=getattr(case, "run_id", None) if case else None,
            case_id=getattr(case, "id", None) if case else None,
            execution_id=getattr(case, "execution_id", None) if case else None,
            name=serving["name"],
            display_name=getattr(metadata, "display_name", "") if metadata else "",
            description=getattr(metadata, "description", "") if metadata else "",
            status=case.status.value if case and hasattr(case.status, "value") else None,
            namespace=getattr(execution, "namespace", None) if execution else None,
            endpoint=execution.endpoint.url if execution and execution.endpoint else None,
            forwarded_endpoint=getattr(execution, "forwarded_endpoint", None) if execution else None,
            guide=serving["guide"],
            backend=serving["backend"],
            model_name=serving["model_name"],
            replicas=serving["replicas"],
            tensor_parallel_size=serving["tensor_parallel_size"],
            image=serving["image"],
        )


DeploymentCaseSummary = DeploymentSummary


class DeploymentCaseResponse(BaseModel):
    case: DeploymentCase
    execution: DeploymentExecution | None = None
    deployment: DeploymentSummary

    @classmethod
    def from_records(cls, case: DeploymentCase, execution: DeploymentExecution | None) -> DeploymentCaseResponse:
        return cls(
            case=case,
            execution=execution,
            deployment=DeploymentSummary.from_case(case, execution),
        )


class DeploymentCaseRestartResponse(BaseModel):
    case: DeploymentCase


class DeploymentRunCancelResponse(BaseModel):
    id: str
    status: str = "cancelling"


class DeploymentOrphanNamespace(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    created_at: str | None = Field(default=None, alias="createdAt")
    phase: str | None = None

    @classmethod
    def from_orphan(cls, orphan: Any) -> DeploymentOrphanNamespace:
        return cls(name=orphan.name, created_at=orphan.created_at, phase=orphan.phase)


class DeploymentOrphanListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cluster_id: str = Field(alias="clusterId")
    prefix: str
    items: list[DeploymentOrphanNamespace] = Field(default_factory=list)


class DeploymentOrphanCleanupFailure(BaseModel):
    name: str
    error: str


class DeploymentOrphanCleanupResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cluster_id: str = Field(alias="clusterId")
    cleaned: list[str] = Field(default_factory=list)
    failed: list[DeploymentOrphanCleanupFailure] = Field(default_factory=list)


class RemoteDeploymentDefaultsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    repository: str = ""
    branch: str = ""
    http_proxy: str = Field(default="", alias="httpProxy")
    https_proxy: str = Field(default="", alias="httpsProxy")
    no_proxy: str = Field(default="", alias="noProxy")
    mount_path: str = Field(default="", alias="mountPath")
    mount_model_name: str = Field(default="", alias="mountModelName")


class RemoteFingerprintResponse(BaseModel):
    fingerprint: str
    host: str
    port: int


class RemoteTestResponse(BaseModel):
    ok: bool
    output: str


class RemotePodStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    status: str
    ready: bool = False
    restarts: int = 0
    node: str | None = None
    gpu_count: float = Field(default=0, alias="gpuCount")
    reason: str | None = None
    message: str | None = None


class RemoteDeploymentStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str
    pods: list[RemotePodStatus] = Field(default_factory=list)
    pods_ready: int = Field(default=0, alias="podsReady")
    pods_total: int = Field(default=0, alias="podsTotal")
    endpoint: str | None = None
    error: str | None = None
    log_tail: list[str] = Field(default_factory=list, alias="logTail")


class RemoteDeploymentStartResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    candidate_id: str | None = Field(default=None, alias="candidateId")
    candidate_name: str | None = Field(default=None, alias="candidateName")
    cluster: str
    remote_host: str = Field(alias="remoteHost")
    nodes: int = 1
    gpus_per_node: int = Field(alias="gpusPerNode")
    selected_cards: list[str] = Field(default_factory=list, alias="selectedCards")
    replicas: float = 1
    namespace: str
    status: str = "deploying"
    created_at: str = Field(alias="createdAt")
    endpoint: str | None = None
    pods: list[RemotePodStatus] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list, alias="logTail")
    machine_type: str | None = Field(default=None, alias="machineType")
    accelerator: str | None = None


class RemoteLogsResponse(BaseModel):
    logs: str = ""
    truncated: bool = False


class RemoteTeardownResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    namespace: str
    log_tail: list[str] = Field(default_factory=list, alias="logTail")


class RemoteErrorResponse(BaseModel):
    error: str


def _isoformat(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


__all__ = [
    "CleanCaseRequest",
    "CleanOrphansRequest",
    "DeploymentCase",
    "DeploymentCaseResponse",
    "DeploymentCaseRestartResponse",
    "DeploymentCaseStatus",
    "DeploymentCaseSummary",
    "DeploymentCreateRequest",
    "DeploymentEndpoint",
    "DeploymentEndpointResponse",
    "DeploymentExecution",
    "DeploymentExecutionListResponse",
    "DeploymentExecutionResponse",
    "DeploymentLogEntry",
    "DeploymentLogPage",
    "DeploymentLogRequest",
    "DeploymentMetadata",
    "DeploymentMetadataUpdateRequest",
    "DeploymentNodeLabel",
    "DeploymentNodeLabelsResponse",
    "DeploymentOrphanCleanupFailure",
    "DeploymentOrphanCleanupResponse",
    "DeploymentOrphanListResponse",
    "DeploymentOrphanNamespace",
    "DeploymentPod",
    "DeploymentPodContainer",
    "DeploymentPodLogsResponse",
    "DeploymentPodsResponse",
    "DeploymentRun",
    "DeploymentRunCancelResponse",
    "DeploymentRunCreateRequest",
    "DeploymentRunStatus",
    "DeploymentRuntimeClassItem",
    "DeploymentRuntimeClassesResponse",
    "DeploymentStatus",
    "DeploymentSummary",
    "ProblemDetails",
    "RebindClusterSessionRequest",
    "RemoteDeploymentDefaultsResponse",
    "RemoteDeploymentStartResponse",
    "RemoteDeploymentStatusResponse",
    "RemoteErrorResponse",
    "RemoteFingerprintResponse",
    "RemoteLogsResponse",
    "RemotePodStatus",
    "RemoteTeardownResponse",
    "RemoteTestResponse",
    "RestartCaseRequest",
    "StandardKubernetesServiceRequest",
    "VersionedPayload",
]
