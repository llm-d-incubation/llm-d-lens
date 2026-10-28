"""DTOs for the per-deployment observability API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DeploymentMonitoringStatusValue = Literal["unreachable", "enabled", "disabled", "absent"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeploymentEppService(StrictModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    service_account: str | None = None


class DeploymentMonitoringStatusResponse(StrictModel):
    execution_id: str | None = None
    namespace: str
    cluster_reachable: bool
    stack_ready: bool
    status: DeploymentMonitoringStatusValue
    enabled: bool
    epp_service: DeploymentEppService | None = None
    servicemonitors: list[str] = Field(default_factory=list)
    podmonitors: list[str] = Field(default_factory=list)
    message: str


class DeploymentMonitoringListResponse(StrictModel):
    cluster_id: str
    cluster_reachable: bool
    stack_ready: bool
    items: list[DeploymentMonitoringStatusResponse] = Field(default_factory=list)
