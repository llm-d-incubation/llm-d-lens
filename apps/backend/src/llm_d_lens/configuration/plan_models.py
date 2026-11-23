"""Shared intent and decision-plan contracts for Manual and Agentic deployment."""

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Workload keys read by the planners; spelling variants are normalized so load targets are never ignored.
_WORKLOAD_KEYS = {
    key.lower(): key
    for key in (
        "inputTokens",
        "p95InputTokens",
        "outputTokens",
        "concurrency",
        "requestRate",
        "prefixReuse",
        "prefillHeavy",
    )
}


class DeploymentRequirements(BaseModel):
    model: str = Field(min_length=1)
    cluster_id: str | None = None
    objective: Literal["balanced", "latency", "throughput", "cost", "reliability"] = "balanced"
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    workload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Workload facts. Planner keys are inputTokens, p95InputTokens, outputTokens, concurrency, requestRate, "
            "prefixReuse and prefillHeavy; snake_case or other case variants are normalized to these names."
        ),
    )
    performance_targets: dict[str, Any] = Field(default_factory=dict)
    required_features: list[str] = Field(default_factory=list)
    forbidden_features: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_workload_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("workload"), dict):
            return data
        workload: dict[str, Any] = {}
        for key, value in data["workload"].items():
            canonical = _WORKLOAD_KEYS.get(key.replace("_", "").lower(), key) if isinstance(key, str) else key
            if canonical in workload and workload[canonical] != value:
                raise ValueError(f"Workload key {key} conflicts with {canonical}; provide one value.")
            if canonical not in workload or canonical == key:
                workload[canonical] = value
        return {**data, "workload": workload}

    @model_validator(mode="after")
    def validate_requirements(self):
        if set(self.required_features) & set(self.forbidden_features):
            raise ValueError("A capability cannot be both required and forbidden.")
        for values in (self.hard_constraints, self.workload, self.performance_targets):
            for key, value in values.items():
                if value is None or isinstance(value, (str, bool)):
                    continue
                if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{key} must be a finite non-negative number.")
        budget = self.hard_constraints.get("maxAccelerators")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
            raise ValueError("maxAccelerators must be a positive whole number.")
        if set(self.hard_constraints) - {"maxAccelerators", "providerRef"}:
            raise ValueError("Unsupported hard constraint; it cannot be silently ignored.")
        provider = self.hard_constraints.get("providerRef")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise ValueError("providerRef must be a non-empty provider identifier or null.")
        for key in ("prefixReuse", "prefillHeavy"):
            if self.workload.get(key) is not None and not isinstance(self.workload[key], bool):
                raise ValueError(f"{key} must be true, false or null.")
        for key in ("inputTokens", "p95InputTokens", "outputTokens", "concurrency", "requestRate"):
            value = self.workload.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise ValueError(f"{key} must be a positive number or null.")
        for key, value in self.performance_targets.items():
            if key not in {"ttftMs", "tpotMs", "e2eMs"}:
                raise ValueError(f"Unsupported performance target: {key}.")
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{key} must be a positive whole number or null.")
        return self


class OptimizationDecision(BaseModel):
    capability_id: str
    catalog_id: str | None = Field(default=None, description="Identity in the source-backed optimization catalog.")
    label: str
    status: Literal[
        "enabled",
        "dependency",
        "excluded",
        "unknown",
        "conflict",
        "user_disabled",
        "unsupported",
        "alternative",
        "resource_limited",
    ]
    reason: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class OptimizationChoice(BaseModel):
    """Planning intent for one user-selectable optimization, before topology and source checks."""

    capability_id: str
    catalog_id: str
    label: str
    checked: bool = Field(description="False when the user turned the optimization off (forbidden_features).")
    auto: bool = Field(default=False, description="True when rules decide whether a checked optimization applies.")
    intended: bool = Field(description="Whether planning will try to enable it; the plan reports what was achieved.")
    reason: str


class ConfigurationPlan(BaseModel):
    plan_id: str
    mode: Literal["manual", "agentic"]
    requirements: DeploymentRequirements
    selected_capabilities: list[str] = Field(default_factory=list)
    decisions: list[OptimizationDecision] = Field(default_factory=list)
    optimization_choices: list[OptimizationChoice] = Field(
        default_factory=list,
        description="User-selectable optimizations with their checked state and planning intent; "
        "a checked optimization that the final decisions do not enable was degraded.",
    )
    resource_estimate: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    evidence_level: Literal["recommendation", "predicted", "validated"] = "recommendation"
    validation: dict[str, Any] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
    source_bundle: dict[str, Any] | None = Field(
        default=None, description="Reviewed rendered resources and provenance."
    )
    source_draft: dict[str, Any] | None = Field(
        default=None, description="Restorable editor state, not deployment authority."
    )
    source_request: dict[str, Any] | None = Field(
        default=None, description="Pinned composition inputs rechecked on approval."
    )
    source_candidates: dict[str, Any] = Field(default_factory=dict)
    endpoints: list[dict[str, Any]] = Field(
        default_factory=list, description="Generated Services available for explicit endpoint selection."
    )


class PlanRequest(BaseModel):
    mode: Literal["manual", "agentic"]
    requirements: DeploymentRequirements
    candidate_limit: int = Field(default=3, ge=1, le=10)
