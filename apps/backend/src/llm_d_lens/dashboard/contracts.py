"""Versioned read contracts for the Welcome page.

Welcome is an issue composition surface. These models describe the normalized
view it returns; they do not replace the owning domain contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")

SectionState = Literal["complete", "partial", "unavailable"]
IssueSeverity = Literal["critical", "warning", "info"]
IssueCategory = Literal[
    "cluster", "model-service", "deployment", "storage", "model-cache", "monitoring", "ai-provider", "access"
]
ServiceAvailability = Literal["available", "degraded", "unavailable", "disabled", "unknown"]
PressureLevel = Literal["elevated", "normal", "unknown"]
ClusterStatus = Literal["healthy", "warning", "critical", "unknown"]
ScopeMode = Literal["all", "cluster"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)


class Coverage(StrictModel):
    """Observed versus expected items or samples for one section or issue."""

    observed: int | None = None
    expected: int | None = None
    unit: str = "items"


class SectionReason(StrictModel):
    code: str
    message: str
    request_id: str | None = None


class SectionEnvelope(StrictModel, Generic[T]):
    """Uniform observation envelope for every independently refreshed section."""

    state: SectionState = "complete"
    generated_at: datetime
    observed_at: datetime | None = None
    stale: bool = False
    coverage: Coverage = Field(default_factory=Coverage)
    reason: SectionReason | None = None
    data: T | None = None


class DashboardScope(StrictModel):
    mode: ScopeMode = "all"
    cluster_id: str | None = None
    cluster_name: str | None = None
    clusters: list[dict[str, str]] = Field(default_factory=list)


class DashboardAction(StrictModel):
    """Typed navigation target; the browser owns labels and routing."""

    intent: str
    view: str
    params: dict[str, str] = Field(default_factory=dict)
    permission: str | None = None


class DashboardIssue(StrictModel):
    id: str
    code: str
    severity: IssueSeverity
    category: IssueCategory
    title: str
    message: str
    cluster_id: str | None = None
    cluster_name: str | None = None
    resource: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    affected: dict[str, Any] = Field(default_factory=dict)
    evaluation_window: str | None = None
    observed_at: datetime | None = None
    first_seen_at: datetime | None = None
    stale: bool = False
    coverage: Coverage = Field(default_factory=Coverage)
    primary_action: DashboardAction | None = None
    secondary_action: DashboardAction | None = None


class AcceleratorBucket(StrictModel):
    id: str
    gpu_count: int = 0
    models: list[str] = Field(default_factory=list)


class ComponentRow(StrictModel):
    name: str
    status: str


class ClusterRow(StrictModel):
    id: str
    name: str
    status: ClusterStatus = "unknown"
    available: bool = True
    reason: str | None = None
    observed_at: datetime | None = None
    stale: bool = False
    nodes_ready: int = 0
    nodes_total: int = 0
    cpu_cores: float = 0.0
    cpu_usage_percent: float | None = None
    memory_bytes: int = 0
    memory_usage_percent: float | None = None
    gpu_total: int = 0
    gpu_allocated: int = 0
    gpu_available: int = 0
    usable_gpu_count: int = 0
    configured_gpu_limit: int | None = None
    gpu_usage_percent: float | None = None
    vram_usage_percent: float | None = None
    disk_usage_percent: float | None = None
    max_disk_usage_percent: float | None = None
    disk_pressure_node: str | None = None
    hf_token_secret_status: str = "unknown"  # noqa: S105
    hf_token_secret_ref: str | None = None
    accelerators: list[AcceleratorBucket] = Field(default_factory=list)
    device_metrics: list[str] = Field(default_factory=list)
    components: list[ComponentRow] = Field(default_factory=list)
    monitoring_dashboard_only: bool = False
    issue_count: int = 0


class ProviderCounts(StrictModel):
    total: int = 0
    active: int = 0
    ready: int = 0
    unhealthy: int = 0
    disabled: int = 0
    standby: int = 0


class GatewayState(StrictModel):
    state: str = "unknown"
    ready: bool | None = None
    running: bool | None = None
    ipp_state: str = "unknown"
    ipp_ready: bool | None = None


class PressureSummary(StrictModel):
    level: PressureLevel = "unknown"
    execution_id: str | None = None
    affected_instances: int = 0
    observed_instances: int = 0
    expected_instances: int = 0
    waiting_current: float | None = None
    waiting_peak: float | None = None
    waiting_fraction: float | None = None
    kv_cache_usage_percent: float | None = None
    kv_cache_fraction: float | None = None
    error_rate: float | None = None
    outcomes: int | None = None
    window_seconds: int | None = None
    shared_execution: bool = False
    stale: bool = False


class ProviderHealth(StrictModel):
    execution_id: str
    name: str = ""
    status: str = "unknown"
    weight: int = 0
    ready: bool = False
    reason: str | None = None


class ServiceRow(StrictModel):
    group_id: str
    name: str
    cluster_id: str
    cluster_name: str = ""
    status: str = "active"
    availability: ServiceAvailability = "unknown"
    providers: ProviderCounts = Field(default_factory=ProviderCounts)
    provider_details: list[ProviderHealth] = Field(default_factory=list)
    deployment_observed: bool = False
    gateway: GatewayState | None = None
    pressure: PressureSummary | None = None
    execution_ids: list[str] = Field(default_factory=list)


class DeploymentActivityItem(StrictModel):
    execution_id: str
    name: str = ""
    display_name: str = ""
    model: str | None = None
    cluster_id: str | None = None
    cluster_name: str = ""
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    failure: dict[str, Any] | None = None


class DeploymentPanel(StrictModel):
    totals: dict[str, int] = Field(default_factory=dict)
    ready: int = 0
    in_progress: list[DeploymentActivityItem] = Field(default_factory=list)
    recent_failures: list[DeploymentActivityItem] = Field(default_factory=list)


class WelcomeIssueSnapshot(StrictModel):
    """Last system-wide issue composition persisted by the background check."""

    snapshot_id: str
    generated_at: datetime
    observed_at: datetime | None = None
    state: SectionState = "complete"
    stale: bool = False
    coverage: Coverage = Field(default_factory=Coverage)
    issues: list[DashboardIssue] = Field(default_factory=list)
    first_seen: dict[str, datetime] = Field(default_factory=dict)


class WelcomeOverview(StrictModel):
    generated_at: datetime
    scope: DashboardScope
    issues: SectionEnvelope[list[DashboardIssue]]


class WelcomeIssuesResponse(StrictModel):
    generated_at: datetime
    snapshot_id: str
    total: int = 0
    items: list[DashboardIssue] = Field(default_factory=list)
    next_cursor: str | None = None
    restarted: bool = False
