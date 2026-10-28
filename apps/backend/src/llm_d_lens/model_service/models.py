"""Public response models for the Model Service HTTP and in-process APIs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Response(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ModelServiceAdminResponse(_Response):
    """Admin/model-service payload; extra fields preserve the route contract."""


class ModelServiceInternalResponse(_Response):
    """Internal gateway payload; extra fields preserve the route contract."""


class ModelTokenResponse(_Response):
    id: str
    name: str = "default"
    token: str | None = None


class ModelTokenListResponse(_Response):
    items: list[ModelTokenResponse] = Field(default_factory=list)


class ModelServiceModelResponse(_Response):
    id: str
    name: str


class ModelServiceModelListResponse(_Response):
    items: list[ModelServiceModelResponse] = Field(default_factory=list)


class ModelServiceConnectionCluster(_Response):
    cluster_id: str = Field(alias="clusterId")
    cluster_name: str = Field(alias="clusterName")
    provider: str | None = None
    gateway_name: str | None = Field(default=None, alias="gatewayName")
    address: str | None = None
    public_url: str | None = Field(default=None, alias="publicUrl")
    port: int | None = None
    base_url: str = Field(alias="baseUrl")
    ready: bool = False


class ModelServiceConnectionResponse(_Response):
    base_url: str = Field(alias="baseUrl")
    clusters: list[ModelServiceConnectionCluster] = Field(default_factory=list)


class UsageRecordResponse(_Response):
    id: str


class UsageRecordsResponse(_Response):
    items: list[UsageRecordResponse] = Field(default_factory=list)


class UsageDailyTotalResponse(_Response):
    date: str
    requests: int = 0
    input_tokens: int = Field(default=0, alias="inputTokens")
    cached_input_tokens: int = Field(default=0, alias="cachedInputTokens")
    cache_write_tokens: int = Field(default=0, alias="cacheWriteTokens")
    output_tokens: int = Field(default=0, alias="outputTokens")


class UsageTimeseriesResponse(_Response):
    items: list[UsageDailyTotalResponse] = Field(default_factory=list)


__all__ = [
    "ModelServiceAdminResponse",
    "ModelServiceInternalResponse",
    "ModelServiceConnectionCluster",
    "ModelServiceConnectionResponse",
    "ModelServiceModelListResponse",
    "ModelServiceModelResponse",
    "ModelTokenListResponse",
    "ModelTokenResponse",
    "UsageDailyTotalResponse",
    "UsageRecordResponse",
    "UsageRecordsResponse",
    "UsageTimeseriesResponse",
]
