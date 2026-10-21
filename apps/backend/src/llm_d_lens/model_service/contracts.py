"""Versioned input/output contracts for the model service domain.

These Pydantic models are the DTOs persisted by the ``model_*`` / ``usage_records``
tables (see ``llm_d_lens/db/models/``), so their field names are the source of
truth for the DAO ``column_map`` dot-paths. HTTP payloads use the camelCase
aliases via ``model_dump(by_alias=True)``.

Design reference: docs/design/model-service-design.md#6-domain-model
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

TOKEN_PREFIX = "lens-mk-"  # noqa: S105 - public token prefix, not a secret
MAX_BACKEND_WEIGHT = 1_000_000
BackendWeight = Annotated[int, Field(strict=True, ge=0, le=MAX_BACKEND_WEIGHT)]

SelectionPolicy = Literal["random", "round_robin", "weighted", "affinity"]
EndpointKind = Literal["vllm", "llm-d-epp"]
TokenStatus = Literal["active", "revoked"]
GroupStatus = Literal["active", "disabled"]
MemberStatus = Literal["active", "disabled", "unhealthy"]
UsageSource = Literal["engine", "estimated"]
UsageStatus = Literal["success", "error", "cancelled"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelAccessToken(StrictModel):
    """A user's machine credential for the model gateway (hash only)."""

    id: str = Field(default_factory=lambda: f"mat-{uuid4().hex[:12]}")
    user_id: str = Field(alias="userId")
    name: str = Field(default="default", max_length=100)
    token_hash: str = Field(alias="tokenHash")
    token_hint: str = Field(alias="tokenHint")
    status: TokenStatus = "active"
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    last_used_at: datetime | None = Field(default=None, alias="lastUsedAt")
    last_used_ip: str | None = Field(default=None, alias="lastUsedIp")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    revoked_at: datetime | None = Field(default=None, alias="revokedAt")

    def api_payload(self) -> dict[str, Any]:
        """Project the token for HTTP clients, never exposing the hash."""
        return self.model_dump(mode="json", by_alias=True, exclude={"token_hash"})


class ModelServiceGroup(StrictModel):
    """A user-facing model name aggregating one or more published services."""

    id: str = Field(default_factory=lambda: f"msg-{uuid4().hex[:12]}")
    name: str = Field(min_length=1, max_length=100)
    model_ref: str = Field(min_length=1, max_length=200, alias="modelRef")
    display_name: str = Field(default="", max_length=200, alias="displayName")
    description: str = ""
    selection_policy: SelectionPolicy = Field(default="random", alias="selectionPolicy")
    status: GroupStatus = "active"
    # llm-d Gateway Mode: a model service is scoped to one cluster; served_name is
    # the engine model name and base_model is the IPP model-mapping pool key.
    cluster_id: str | None = Field(default=None, alias="clusterId")
    served_name: str | None = Field(default=None, alias="servedName")
    base_model: str | None = Field(default=None, alias="baseModel")
    created_by_user_id: str | None = Field(default=None, alias="createdByUserId")
    owner_user_id: str | None = Field(default=None, alias="ownerUserId")
    owner_group_id: str | None = Field(default=None, alias="ownerGroupId")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ModelServiceMember(StrictModel):
    """A published deployment backing a group, located in one cluster."""

    id: str = Field(default_factory=lambda: f"msm-{uuid4().hex[:12]}")
    group_id: str = Field(alias="groupId")
    execution_id: str = Field(alias="executionId")
    cluster_id: str = Field(alias="clusterId")
    target_namespace: str = Field(alias="targetNamespace")
    target_service: str = Field(alias="targetService")
    target_port: int = Field(ge=1, le=65535, alias="targetPort")
    endpoint_kind: EndpointKind = Field(default="vllm", alias="endpointKind")
    # llm-d Gateway Mode: this deployment owns one InferencePool + EPP; pool_name /
    # epp_ref identify them for the model service's HTTPRoute (design section 5.1).
    pool_name: str | None = Field(default=None, alias="poolName")
    epp_ref: str | None = Field(default=None, alias="eppRef")
    status: MemberStatus = "active"
    weight: BackendWeight = Field(default=1, description="Gateway API relative request weight; 0 receives no traffic.")
    health_json: dict[str, Any] | None = Field(default=None, alias="healthJson")
    last_health_at: datetime | None = Field(default=None, alias="lastHealthAt")
    owner_user_id: str | None = Field(default=None, alias="ownerUserId")
    owner_group_id: str | None = Field(default=None, alias="ownerGroupId")
    published_by_user_id: str | None = Field(default=None, alias="publishedByUserId")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class UsageRecord(StrictModel):
    """One recorded inference: per user/model/cluster/four token classes."""

    id: str = Field(default_factory=lambda: f"usg-{uuid4().hex[:12]}")
    request_id: str = Field(max_length=64, alias="requestId")
    user_id: str | None = Field(default=None, alias="userId")
    token_id: str | None = Field(default=None, alias="tokenId")
    group_id: str | None = Field(default=None, alias="groupId")
    group_name: str | None = Field(default=None, alias="groupName")
    cluster_id: str | None = Field(default=None, alias="clusterId")
    execution_id: str | None = Field(default=None, alias="executionId")
    model_ref: str = Field(default="", alias="modelRef")
    provider: str = "vllm"
    input_tokens: int = Field(default=0, ge=0, alias="inputTokens")
    cached_input_tokens: int = Field(default=0, ge=0, alias="cachedInputTokens")
    cache_write_tokens: int = Field(default=0, ge=0, alias="cacheWriteTokens")
    output_tokens: int = Field(default=0, ge=0, alias="outputTokens")
    requests: int = Field(default=1, ge=0)
    usage_source: UsageSource = Field(default="engine", alias="usageSource")
    status: UsageStatus = "success"
    error_code: str | None = Field(default=None, alias="errorCode")
    streaming: bool = False
    ttft_ms: int | None = Field(default=None, alias="ttftMs")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    client_ip: str | None = Field(default=None, alias="clientIp")
    user_agent: str | None = Field(default=None, alias="userAgent")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# --- request contracts -------------------------------------------------------


class ModelGrantRequest(StrictModel):
    """Grant inference only; cluster and grant role are derived by the server."""

    subject_type: Literal["user", "group"] = Field(alias="subjectType", description="Recipient kind.")
    subject_id: str = Field(min_length=1, alias="subjectId", description="Existing user or group ID.")


class TokenCreateRequest(StrictModel):
    name: str = Field(default="default", min_length=1, max_length=100)


class GroupCreateRequest(StrictModel):
    """Create a public model service, optionally with its first ready deployment."""

    name: str = Field(min_length=1, max_length=100)
    #: Optional; defaults to ``name``. Lens derives the rest (model ref / base model).
    model_ref: str = Field(default="", max_length=200, alias="modelRef")
    display_name: str = Field(default="", max_length=200, alias="displayName")
    description: str = ""
    selection_policy: SelectionPolicy = Field(default="random", alias="selectionPolicy")
    #: A model service is cluster-scoped; the same public name may exist per cluster,
    #: so the cluster is required (uniqueness is enforced on (cluster_id, name)).
    cluster_id: str = Field(alias="clusterId", min_length=1, max_length=32)
    served_name: str | None = Field(default=None, alias="servedName")
    base_model: str | None = Field(default=None, alias="baseModel")
    execution_id: str | None = Field(
        default=None,
        alias="executionId",
        min_length=1,
        description="Optional ready deployment in clusterId. Creates the service and its first member atomically; "
        "modelRef and servedName are derived from the deployment, and baseModel from the public name.",
    )


class GroupPublishRequest(StrictModel):
    """Publish a ready deployment as a new cluster-scoped public model name."""

    name: str = Field(min_length=1, max_length=100)
    cluster_id: str = Field(alias="clusterId", min_length=1, max_length=32)
    execution_id: str = Field(alias="executionId", min_length=1)
    display_name: str = Field(default="", max_length=200, alias="displayName")
    description: str = ""


class GroupUpdateRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    model_ref: str | None = Field(default=None, min_length=1, max_length=200, alias="modelRef")
    display_name: str | None = Field(default=None, max_length=200, alias="displayName")
    description: str | None = None
    selection_policy: SelectionPolicy | None = Field(default=None, alias="selectionPolicy")
    status: GroupStatus | None = Field(
        default=None,
        description="disabled stops this public model; active resumes enabled positive-weight providers. "
        "Provider states, weights and running Pods are preserved.",
    )
    cluster_id: str | None = Field(default=None, alias="clusterId")
    served_name: str | None = Field(default=None, alias="servedName")
    base_model: str | None = Field(default=None, alias="baseModel")


class GroupStatusRequest(StrictModel):
    """Enable or disable a public model service without changing providers."""

    status: Literal["active", "disabled"]


class MemberCreateRequest(StrictModel):
    """Publish an execution into a group; the target is derived server-side."""

    group_id: str = Field(alias="groupId")
    execution_id: str = Field(alias="executionId")
    pool_name: str | None = Field(default=None, alias="poolName")


class TrafficMemberRequest(StrictModel):
    execution_id: str = Field(alias="executionId", min_length=1)
    weight: BackendWeight


class TrafficUpdateRequest(StrictModel):
    """Replace weights together, optionally adding ready deployments at the same time."""

    members: list[TrafficMemberRequest] = Field(min_length=1, max_length=16)
    expected_weights: dict[str, BackendWeight] = Field(
        alias="expectedWeights",
        description="Previously read executionId-to-weight map; stale membership or weights return 409.",
    )


class PublishableDeployment(StrictModel):
    """A ready execution offered to the publish form (target already derived)."""

    execution_id: str = Field(alias="executionId")
    name: str | None = None
    display_name: str = Field(default="", alias="displayName")
    model: str | None = None
    cluster_id: str = Field(alias="clusterId")
    cluster_name: str = Field(default="", alias="clusterName")
    namespace: str
    service: str
    port: int
    endpoint_kind: EndpointKind = Field(alias="endpointKind")
    model_ref: str | None = Field(default=None, alias="modelRef")
    status: str

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class MemberUpdateRequest(StrictModel):
    status: MemberStatus | None = Field(
        default=None,
        description="disabled takes the provider offline without scaling Pods or changing weight. "
        "Re-enabling requires a ready deployment. Weight 0 remains standby and parent model state is unchanged.",
    )


class ProvidersStatusRequest(StrictModel):
    status: Literal["active", "disabled"] = Field(
        description="Bring every provider online (active) or offline (disabled). "
        "Preserves weights, model status and Pods. Starting requires all deployments to be ready."
    )


class AuthorizeRequest(StrictModel):
    token: str
    model: str = Field(min_length=1, max_length=100)
    request_id: str | None = Field(default=None, alias="requestId")
    client_ip: str | None = Field(default=None, alias="clientIp")


class AuthorizeResult(StrictModel):
    """The routing decision returned to Edge Envoy by the ext_authz endpoint."""

    user_id: str = Field(alias="userId")
    token_id: str = Field(alias="tokenId")
    group_id: str = Field(alias="groupId")
    group_name: str = Field(alias="groupName")
    model_ref: str = Field(alias="modelRef")
    cluster_id: str = Field(alias="clusterId")
    execution_id: str = Field(alias="executionId")
    target_namespace: str = Field(alias="targetNamespace")
    target_service: str = Field(alias="targetService")
    target_port: int = Field(alias="targetPort")
    endpoint_kind: EndpointKind = Field(alias="endpointKind")


class UsageRecordRequest(StrictModel):
    request_id: str = Field(max_length=64, alias="requestId")
    token_id: str | None = Field(default=None, alias="tokenId")
    group_id: str | None = Field(default=None, alias="groupId")
    group_name: str | None = Field(default=None, alias="groupName")
    cluster_id: str | None = Field(default=None, alias="clusterId")
    execution_id: str | None = Field(default=None, alias="executionId")
    model_ref: str = Field(default="", alias="modelRef")
    provider: str = "vllm"
    input_tokens: int = Field(default=0, ge=0, alias="inputTokens")
    cached_input_tokens: int = Field(default=0, ge=0, alias="cachedInputTokens")
    cache_write_tokens: int = Field(default=0, ge=0, alias="cacheWriteTokens")
    output_tokens: int = Field(default=0, ge=0, alias="outputTokens")
    requests: int = Field(default=1, ge=0)
    usage_source: UsageSource = Field(default="engine", alias="usageSource")
    status: UsageStatus = "success"
    error_code: str | None = Field(default=None, alias="errorCode")
    streaming: bool = False
    ttft_ms: int | None = Field(default=None, alias="ttftMs")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    client_ip: str | None = Field(default=None, alias="clientIp")
    user_agent: str | None = Field(default=None, alias="userAgent")


class EppUsageSnapshot(StrictModel):
    """Last observed cumulative EPP token counters for one cluster.

    ``counters`` maps ``f"{model_name}\\x1f{fairness_id}"`` to the cumulative
    ``{input_tokens, output_tokens, cached_input_tokens, requests}`` the cluster's
    EPP reported at ``captured_at``. Successive scrapes are diffed to derive the
    usage written to the ledger (see ``usage_sync``).
    """

    cluster_id: str = Field(alias="clusterId")
    captured_at: datetime = Field(default_factory=utcnow, alias="capturedAt")
    counters: dict[str, dict[str, int]] = Field(default_factory=dict)

    def api_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
