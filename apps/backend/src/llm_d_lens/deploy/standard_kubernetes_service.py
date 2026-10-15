"""Bounded inputs and configuration generation for Standard Kubernetes services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from llm_d_lens.deploy.contracts import DeployableConfiguration
from llm_d_lens.deploy.runtime.composition import _model_environment
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum


class StandardKubernetesServiceRequest(BaseModel):
    """Allowlisted input for a Standard Kubernetes model service."""

    model: str = Field(min_length=1, max_length=300)
    deployment_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    cluster_session_id: str = Field(min_length=36, max_length=36)
    replicas: int = Field(ge=1, le=32)
    tensor_parallel_size: int = Field(ge=1, le=16)
    placement_replicas: Literal["prefer-same-node", "same-node", "balanced", "spread"] = "prefer-same-node"
    placement_node_label: dict[str, str] | None = None
    storage_type: Literal["model-cache"] = "model-cache"
    storage_volume_id: str = Field(min_length=1, max_length=120)
    model_cache_path: str = Field(default_factory=lambda: str(Path.home() / ".cache"), min_length=1, max_length=1024)
    pvc_name: str = Field(default="", max_length=253, pattern=r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?)?$")
    max_model_len: int = Field(default=4096, ge=256, le=131072)
    gpu_memory_utilization: float = Field(default=0.9, ge=0.5, le=0.98)
    max_num_seqs: int = Field(default=256, ge=1, le=4096)
    max_num_batched_tokens: int = Field(default=8192, ge=256, le=131072)
    enable_prefix_caching: bool = True
    enforce_eager: bool = False
    vllm_arguments: list[dict[str, str]] = Field(default_factory=list)
    use_cluster_proxy: bool = True
    http_proxy: str = Field(default="", max_length=1000)
    https_proxy: str = Field(default="", max_length=1000)
    no_proxy: str = Field(default="", max_length=2000)


def build_standard_kubernetes_service_configuration(
    request: StandardKubernetesServiceRequest,
    *,
    accelerator: str | None = None,
    model_revision: str | None = None,
) -> DeployableConfiguration:
    """Build the immutable vLLM configuration currently used by Standard mode."""
    from llm_d_lens.deploy.providers.hardware_profile import runtime_image

    runtime: dict[str, object] = {
        "image": runtime_image(accelerator=accelerator),
        "imageMode": "pull-existing",
        "modelSource": "auto-cache",
        "storageVolumeId": request.storage_volume_id,
        "cacheOffline": True,
    }
    if request.storage_type != "model-cache" or not request.storage_volume_id.strip():
        raise ValueError("Standard deployment requires Model Cache storage")
    if request.use_cluster_proxy:
        # The cluster's own saved proxy config is authoritative; explicit
        # request values override it, and resolve_proxy_env already falls back
        # to this backend's environment for auto/unknown clusters.
        cluster_proxy: dict[str, str] = {}
        try:
            from llm_d_lens.cluster import require_active_session
            from llm_d_lens.cluster.service import resolve_proxy_env

            cluster_proxy = resolve_proxy_env(require_active_session(request.cluster_session_id).server_id)
        except Exception:
            cluster_proxy = {}
        proxy_overrides = {
            **cluster_proxy,
            **{
                name: value
                for name, value in (
                    ("HTTP_PROXY", request.http_proxy),
                    ("HTTPS_PROXY", request.https_proxy),
                    ("NO_PROXY", request.no_proxy),
                )
                if value
            },
        }
        runtime["environment"] = _model_environment({**os.environ, **proxy_overrides})
    content = {
        "model": {"name": request.model},
        **({"hardware_profile": accelerator} if accelerator else {}),
        "decode": {
            "replicaCount": request.replicas,
            "tensorParallelSize": request.tensor_parallel_size,
            "maxModelLen": request.max_model_len,
        },
        "runtime": runtime,
        "customParameters": [
            {
                "target": "decode",
                "kind": "argument",
                "name": "gpu-memory-utilization",
                "value": str(request.gpu_memory_utilization),
            },
            {
                "target": "decode",
                "kind": "argument",
                "name": "enable-prefix-caching",
                "value": str(request.enable_prefix_caching).lower(),
            },
            {"target": "decode", "kind": "argument", "name": "max-num-seqs", "value": str(request.max_num_seqs)},
            {
                "target": "decode",
                "kind": "argument",
                "name": "max-num-batched-tokens",
                "value": str(request.max_num_batched_tokens),
            },
        ],
    }
    if request.enforce_eager:
        content["customParameters"].append(
            {
                "target": "decode",
                "kind": "argument",
                "name": "enforce-eager",
                "value": "true",
            }
        )
    content["customParameters"].extend(
        {"target": "decode", "kind": "argument", "name": argument["name"], "value": argument["value"]}
        for argument in request.vllm_arguments
    )
    requested_revisions = [argument["value"] for argument in request.vllm_arguments if argument["name"] == "revision"]
    if model_revision and any(revision != model_revision for revision in requested_revisions):
        raise ValueError("vLLM revision does not match the selected model cache entry")
    if model_revision and model_revision != "main" and not requested_revisions:
        content["customParameters"].append(
            {"target": "decode", "kind": "argument", "name": "revision", "value": model_revision}
        )
    return DeployableConfiguration(
        type="baseline",
        format="helm",
        content=content,
        provider_ref="baseline-vllm",
        checksum=configuration_checksum(content),
        provenance={
            "source": "standard-kubernetes-service.v1",
            "deployment_name": request.deployment_name,
            "description": request.description.strip(),
            "storage_type": request.storage_type,
        },
    )
