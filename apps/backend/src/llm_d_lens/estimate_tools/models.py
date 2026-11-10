"""Tool-neutral estimate contracts; legacy AIC HTTP models remain separate."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ToolId = Literal["aisimulate", "blis"]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
NonemptyString = Annotated[str, Field(strict=True, min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ExecutionStatus = Literal["succeeded", "unsupported", "unavailable", "failed", "timed_out", "cancelled"]
ConstraintAssessment = Literal["satisfied", "not_satisfied", "unknown"]
TaskStatus = Literal["queued", "running", "cancelling", "completed", "failed", "cancelled"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    @model_validator(mode="after")
    def finite_json(self) -> Self:
        def check(value: object) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("All numeric values must be finite")
            if isinstance(value, dict):
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.model_dump(mode="python"))
        return self


class Model(Contract):
    name: NonemptyString
    precision: NonemptyString | None = None
    quantization: NonemptyString | None = None


class Hardware(Contract):
    profile_id: NonemptyString
    model: NonemptyString
    nodes: PositiveInt
    devices_per_node: PositiveInt
    fabric: NonemptyString | None = None

    @property
    def device_count(self) -> int:
        return self.nodes * self.devices_per_node


class Role(Contract):
    role: Literal["colocated", "prefill", "decode"]
    tp: PositiveInt = 1
    pp: PositiveInt = 1
    dp: PositiveInt = 1
    replicas: PositiveInt = 1
    expert_parallel: bool = False

    @property
    def device_count(self) -> int:
        return self.tp * self.pp * self.dp * self.replicas


class Configuration(Contract):
    mode: Literal["agg", "disagg"] = "agg"
    engine: NonemptyString = "vllm"
    engine_version: NonemptyString | None = None
    roles: list[Role] = Field(default_factory=lambda: [Role(role="colocated")], min_length=1)
    engine_settings: dict[str, JsonValue] = Field(default_factory=dict)
    router_settings: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_roles(self) -> Self:
        names = [role.role for role in self.roles]
        expected = {"colocated"} if self.mode == "agg" else {"prefill", "decode"}
        if len(set(names)) != len(names) or set(names) != expected:
            raise ValueError("Serving mode requires unique colocated or prefill/decode roles")
        self.roles.sort(key=lambda role: role.role)
        return self


class Workload(Contract):
    distribution: Literal["native_mean", "constant", "trace"] = "native_mean"
    input_tokens: PositiveInt = 4000
    output_tokens: PositiveInt = 1000
    request_rate: PositiveFloat | None = None
    concurrency: PositiveInt | None = None
    trace_artifact: NonemptyString | None = None
    prefix_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    input_includes_prefix: bool = True

    @model_validator(mode="after")
    def check_load(self) -> Self:
        if self.request_rate is not None and self.concurrency is not None:
            raise ValueError("Choose one load target: request_rate or concurrency")
        if self.distribution == "trace":
            if not self.trace_artifact or self.request_rate is not None or self.concurrency is not None:
                raise ValueError("Trace mode needs an artifact and cannot also specify a load target")
        elif self.trace_artifact is not None:
            raise ValueError("trace_artifact requires trace distribution")
        if self.distribution == "constant" and self.request_rate is None and self.concurrency is None:
            raise ValueError("Constant workloads require an explicit load target")
        if self.input_includes_prefix and self.prefix_tokens >= self.input_tokens and self.prefix_tokens:
            raise ValueError("Prefix-inclusive input must include non-prefix prompt tokens")
        return self


class RunControls(Contract):
    seed: Annotated[int, Field(strict=True, ge=0)] | None = None
    num_requests: PositiveInt | None = None
    horizon_s: PositiveFloat | None = None
    request_deadline_s: PositiveFloat | None = None
    wall_timeout_s: PositiveFloat | None = None


class MetricDefinition(Contract):
    name: NonemptyString
    unit: NonemptyString
    statistic: NonemptyString
    formula: NonemptyString
    population: NonemptyString
    window: NonemptyString


class Metric(MetricDefinition):
    value: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class MetricConstraint(Contract):
    metric: MetricDefinition
    comparison: Literal["le", "ge"]
    threshold: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class Constraints(Contract):
    max_devices: PositiveInt | None = None
    metrics: list[MetricConstraint] = Field(default_factory=list)
    require_complete: bool = True
    max_failure_fraction: Annotated[float, Field(strict=True, ge=0, le=1)] | None = None


class Objective(Contract):
    metric: MetricDefinition
    direction: Literal["minimize", "maximize"]
    tie_breaks: list[Literal["device_count", "configuration_fingerprint"]] = Field(min_length=1)

    @model_validator(mode="after")
    def check_tie_breaks(self) -> Self:
        if len(set(self.tie_breaks)) != len(self.tie_breaks):
            raise ValueError("Tie-break rules must be unique")
        return self


class SearchBudget(Contract):
    max_evaluations: PositiveInt
    parallelism: PositiveInt
    wall_duration_s: PositiveFloat
    cross_evaluations: Annotated[int, Field(strict=True, ge=0)] = 0


class SearchSpace(Contract):
    mode: Literal["find_configuration", "find_capacity"]
    tensor_parallel: list[PositiveInt] = Field(default_factory=list)
    replicas: list[PositiveInt] = Field(default_factory=list)
    request_rates: list[PositiveFloat] = Field(default_factory=list)
    concurrencies: list[PositiveInt] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_grid(self) -> Self:
        for values in (self.tensor_parallel, self.replicas, self.request_rates, self.concurrencies):
            if len(set(values)) != len(values):
                raise ValueError("Search grids must not contain duplicates")
        if self.mode == "find_configuration":
            if not self.tensor_parallel or not self.replicas or self.request_rates or self.concurrencies:
                raise ValueError("Configuration search requires only explicit TP and replica grids")
        elif bool(self.request_rates) == bool(self.concurrencies) or self.tensor_parallel or self.replicas:
            raise ValueError("Capacity search requires exactly one rate or concurrency grid at fixed topology")
        return self


class Request(Contract):
    tools: list[ToolId] = Field(default_factory=lambda: ["aisimulate"], min_length=1)
    runtime_profiles: dict[ToolId, NonemptyString] = Field(default_factory=dict)
    model: Model
    hardware: Hardware
    configuration: Configuration = Field(default_factory=Configuration)
    workload: Workload = Field(default_factory=Workload)
    constraints: Constraints = Field(default_factory=Constraints)
    run: RunControls = Field(default_factory=RunControls)

    @model_validator(mode="after")
    def check_selection(self) -> Self:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("Tool selections must be unique")
        if set(self.runtime_profiles) - set(self.tools):
            raise ValueError("Runtime profiles may only select requested tools")
        required = sum(role.device_count for role in self.configuration.roles)
        if required > self.hardware.device_count:
            raise ValueError("Topology exceeds physical device capacity")
        if self.constraints.max_devices is not None and required > self.constraints.max_devices:
            raise ValueError("Topology exceeds the explicit device budget")
        return self


class EstimateRequest(Request):
    operation: Literal["estimate"] = "estimate"


class OptimizeRequest(Request):
    operation: Literal["optimize"] = "optimize"
    objective: Objective
    budget: SearchBudget
    space: SearchSpace


class EffectiveConfiguration(Contract):
    model: Model
    hardware: Hardware
    configuration: Configuration

    @classmethod
    def from_request(cls, request: Request) -> EffectiveConfiguration:
        return cls(model=request.model, hardware=request.hardware, configuration=request.configuration)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


class Problem(Contract):
    code: NonemptyString
    message: NonemptyString


class Capability(Contract):
    operation: Literal["native_recommendation", "native_exact", "replay", "managed_sweep"]
    enabled: bool
    reason: str | None = None


class ToolSupport(Contract):
    tool: ToolId
    runtime_profile: str | None = None
    available: bool
    matching: Literal["unknown", "matched", "unsupported"] = "unknown"
    status: Literal["supported", "unsupported", "unavailable", "unknown"]
    execution_confirmed: bool = False
    evidence_maturity: Literal["unknown", "experimental", "calibrated", "validated"] = "unknown"
    resolved_input_digest: Digest | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    approved_inputs: list[EffectiveConfiguration] = Field(default_factory=list)
    revisions: dict[str, str] = Field(default_factory=dict)
    reason: Problem | None = None
    warnings: list[str] = Field(default_factory=list)


class Evaluation(Contract):
    tool: ToolId
    strategy: NonemptyString
    runtime_profile: str | None = None
    configuration_fingerprint: Digest
    status: ExecutionStatus
    constraint_assessment: ConstraintAssessment = "unknown"
    metrics: list[Metric] = Field(default_factory=list)
    request_accounting: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=dict)
    revisions: dict[str, str] = Field(default_factory=dict)
    coefficient_provenance: dict[str, JsonValue] = Field(default_factory=dict)
    resolved_input_digest: Digest | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_maturity: Literal["unknown", "experimental", "calibrated", "validated"] = "unknown"
    warnings: list[str] = Field(default_factory=list)
    error: Problem | None = None

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("Successful execution cannot carry a failure error")
        if self.status != "succeeded" and self.error is None:
            raise ValueError("Non-success evaluations require a structured error")
        if self.status != "succeeded" and self.constraint_assessment != "unknown":
            raise ValueError("Non-success evaluations cannot establish constraint satisfaction")
        return self


class Candidate(Contract):
    effective_configuration: EffectiveConfiguration
    generated_by: list[ToolId] = Field(min_length=1)
    evaluations: list[Evaluation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if any(
            evaluation.configuration_fingerprint != self.effective_configuration.fingerprint
            for evaluation in self.evaluations
        ):
            raise ValueError("Evaluation fingerprint must match its effective configuration")
        return self


class BatchResult(Contract):
    outcome: Literal["succeeded", "partial", "failed"]
    evaluations: list[Evaluation] = Field(min_length=1)
    candidates: list[Candidate] = Field(default_factory=list)
    search_complete: bool | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        successes = sum(evaluation.status == "succeeded" for evaluation in self.evaluations)
        expected = "succeeded" if successes == len(self.evaluations) else "partial" if successes else "failed"
        if self.outcome != expected:
            raise ValueError("Batch outcome must reflect every selected tool's execution")
        return self


class Task(Contract):
    id: NonemptyString
    owner_id: NonemptyString
    status: TaskStatus
    request: EstimateRequest | OptimizeRequest
    result: BatchResult | None = None
    termination_reason: Problem | None = None
