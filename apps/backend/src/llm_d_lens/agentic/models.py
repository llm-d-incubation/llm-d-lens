"""Contracts owned by the Agentic Deploy orchestration boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from llm_d_lens.agentic.public_benchmark_record.case_retrieval import CandidateEvidenceResult
from llm_d_lens.ai_providers.client import AIRequestRecord
from llm_d_lens.configuration.models import ModelSecretConfiguration
from llm_d_lens.configuration.plan_models import ConfigurationPlan, DeploymentRequirements
from llm_d_lens.deploy.standard_kubernetes_service import StandardKubernetesServiceRequest

from .configuration import AGENTIC_RUNTIME_BACKEND
from .facts import PlanningEvidence
from .planner import PlannedCandidate, PlannerDecision, PlanningFacts


def utcnow() -> datetime:
    return datetime.now(UTC)


class AgenticDeploymentStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    DEPLOYING = "deploying"
    OPTIMIZING = "optimizing"
    RECOMMENDED = "recommended"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgenticExecutionPolicy(BaseModel):
    mode: Literal["approval_required", "automatic"] = "approval_required"
    max_optimization_iterations: int = Field(default=0, ge=0, le=3)
    max_benchmark_runs: int = Field(default=0, ge=0, le=4)
    max_total_duration_minutes: int = Field(default=60, ge=1, le=180)
    allow_trials: bool = Field(
        default=False, description="Permit separately approved trial deployments; planning never starts trials."
    )


class AgenticDeploymentCreateRequest(StandardKubernetesServiceRequest):
    """Bounded Agentic request; serving parameters remain the Standard contract."""

    execution_policy: AgenticExecutionPolicy = Field(default_factory=AgenticExecutionPolicy)
    planning_facts: PlanningFacts
    planner_prompt: str | None = Field(default=None, max_length=2_000)
    ai_provider_id: str | None = Field(default=None, max_length=200)
    planning_mode: Literal["manual", "agentic"] = Field(
        default="agentic",
        description=(
            "Manual ranks source-verified combinations by SLA status, then the P/D decision, then optimization "
            "coverage, without AI; "
            "Agentic recommends among shared compatible combinations."
        ),
    )
    requirements: DeploymentRequirements | None = Field(
        default=None, description="Shared deployment intent. Constraints are enforced before ranking and approval."
    )


class AgenticCandidateRefinementRequest(BaseModel):
    planner_prompt: str = Field(min_length=1, max_length=2_000)


class OptimizationPreviewRequest(BaseModel):
    requirements: DeploymentRequirements
    available_accelerators: int | None = Field(
        default=None, ge=0, le=100_000, description="Free accelerators reported by the selected cluster, if known."
    )


class AgenticCandidate(BaseModel):
    id: str = "candidate-1"
    backend: str = AGENTIC_RUNTIME_BACKEND
    provider_ref: str = "baseline-vllm"
    replicas: int
    tensor_parallel_size: int
    prefill_replicas: int | None = None
    prefill_tensor_parallel_size: int | None = None
    guide_variant: str | None = None
    native_cpu_cache_gib: float | None = Field(default=None, gt=0, le=1024, allow_inf_nan=False)
    prefix_routing: bool | None = Field(
        default=None, description="Independent prefix-affinity routing choice; null preserves legacy source routing."
    )
    max_model_len: int
    gpu_memory_utilization: float
    evidence: list[str] = Field(default_factory=list)
    allocatable_kv_cache_gib: float | None = None
    per_request_kv_cache_gib: float | None = None
    max_concurrent_requests: int | None = None


class AgenticCandidateSelectionRequest(BaseModel):
    candidate_id: str = Field(min_length=1)
    role: Literal["decode", "prefill"] = "decode"
    replicas: int | None = Field(default=None, ge=1, le=128)
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=128)
    render_resources: bool = Field(
        default=False, description="Render and validate the selected configuration without deploying."
    )
    compare_combinations: bool = Field(
        default=False,
        description=(
            "With render_resources in Manual mode, check retained combinations against pinned source and "
            "rank them by SLA status, then the P/D decision, then verified capability coverage, breaking ties by "
            "planning order. "
            "Report unsupported or rejected combinations; never deploy."
        ),
    )
    max_model_len: int | None = Field(default=None, ge=256, le=1_048_576)
    gpu_memory_utilization: float | None = Field(default=None, ge=0.5, le=0.98, allow_inf_nan=False)
    max_num_seqs: int | None = Field(default=None, ge=1, le=4096)
    max_num_batched_tokens: int | None = Field(default=None, ge=256, le=131072)
    native_cpu_cache_gib: float | None = Field(
        default=None,
        gt=0,
        le=1024,
        allow_inf_nan=False,
        description="Revise CPU KV cache GiB per model replica, checked against current free host memory.",
    )


class AgenticApprovalRequest(BaseModel):
    prepared_plan_id: str | None = Field(default=None, description="ID of the frozen deployment plan reviewed.")
    expected_plan_digest: str | None = Field(default=None, description="Digest returned with the frozen plan.")
    acknowledge_unknown: bool = Field(default=False, description="Accept listed unknown preflight constraints.")
    configuration_checksum: str = Field(
        min_length=1, description="Checksum of the exact generated configuration reviewed by the user."
    )
    endpoint: str | None = Field(
        default=None,
        description="Required for source compositions: a reviewed Service value from configuration_plan.endpoints.",
    )


class AgenticDecisionMetadata(BaseModel):
    planner: Literal["deterministic", "openai-compatible"]
    planner_model: str | None = None
    selection_method: str
    score_method: str


class AgenticDeploymentRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: AgenticDeploymentStatus
    request: AgenticDeploymentCreateRequest
    selected_candidate: AgenticCandidate
    # Approval happens after planning, so retain the credential source that was
    # validated with the cache instead of trying to rediscover or guess it.
    model_secret: ModelSecretConfiguration = Field(default_factory=ModelSecretConfiguration)
    candidates: list[PlannedCandidate] = Field(default_factory=list)
    candidate_evidence: tuple[CandidateEvidenceResult, ...] = ()
    decision: PlannerDecision | None = None
    planning_evidence: list[PlanningEvidence] = Field(default_factory=list)
    decision_evidence_ids: list[str] = Field(default_factory=list)
    decision_metadata: AgenticDecisionMetadata | None = None
    planning_cluster_snapshot: dict[str, object] = Field(default_factory=dict)
    generator: Literal["deterministic", "ai-mcp"] = "deterministic"
    generator_model: str | None = None
    generator_fallback_reason: str | None = None
    generator_fallback_detail: str | None = None
    generator_tool_trace: list[dict[str, object]] = Field(default_factory=list)
    generator_requests: list[AIRequestRecord] = Field(default_factory=list)
    planner_requests: list[AIRequestRecord] = Field(default_factory=list)
    planning_trace: list[dict[str, object]] = Field(default_factory=list)
    generator_candidate_proposals: list[dict[str, object]] = Field(default_factory=list)
    rejected_candidates: list[PlannedCandidate] = Field(default_factory=list)
    planner: str = "deterministic"
    planner_fallback_reason: str | None = None
    planning_snapshot_id: str | None = None
    planning_snapshot_path: str | None = None
    configuration_artifact_id: str | None = None
    deployment_run_id: str | None = None
    deployment_execution_id: str | None = None
    optimization: dict | None = None
    error: str | None = None
    configuration_plan: ConfigurationPlan | None = None
    source_endpoint: str | None = None
    revision: int = 1
    parent_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
