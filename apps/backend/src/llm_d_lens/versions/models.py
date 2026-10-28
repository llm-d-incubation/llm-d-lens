"""Response contracts for the pinned llm-d stack profile."""

from __future__ import annotations

from pydantic import BaseModel


class GatewayProviderVersionsResponse(BaseModel):
    istio: str
    envoy_gateway: str
    envoy_ai_gateway: str
    agentgateway: str


class SupportedVersionsResponse(BaseModel):
    llm_d: str
    min_k8s_version: str
    llm_d_router: str
    llm_d_lensmark: str
    llm_d_inference_payload_processor: str
    k8s_gateway_api: str
    k8s_gateway_api_inference_extension: str
    k8s_lws: str
    gateway_providers: GatewayProviderVersionsResponse
