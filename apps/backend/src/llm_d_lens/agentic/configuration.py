"""Map bounded Agentic candidates to Deploy's existing configuration contract."""

from __future__ import annotations

from llm_d_lens.configuration.models import ModelSecretConfiguration
from llm_d_lens.deploy.contracts import DeployableConfiguration
from llm_d_lens.deploy.standard_kubernetes_service import (
    StandardKubernetesServiceRequest,
    build_standard_kubernetes_service_configuration,
)
from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum

from .planner import PlannedCandidate

AGENTIC_RUNTIME_BACKEND = "vllm"


def build_agentic_configuration(
    request: StandardKubernetesServiceRequest,
    candidate: PlannedCandidate,
    model_secret: ModelSecretConfiguration | None = None,
    *,
    accelerator: str | None = None,
) -> DeployableConfiguration:
    """Create a provider-owned configuration without rendering or deploying it."""
    selected_request = request.model_copy(
        update={
            "replicas": candidate.replicas,
            "tensor_parallel_size": candidate.tensor_parallel_size,
        }
    )
    if candidate.provider_ref == "baseline-vllm":
        configuration = build_standard_kubernetes_service_configuration(selected_request, accelerator=accelerator)
        if model_secret is not None and model_secret.mode != "none":
            content = {
                **configuration.content,
                "modelSecret": model_secret.model_dump(mode="json", by_alias=True),
            }
            return configuration.model_copy(
                update={
                    "content": content,
                    "checksum": configuration_checksum(content),
                }
            )
        return configuration

    from llm_d_lens.deploy.providers.hardware_profile import runtime_image

    runtime = {
        "image": runtime_image(accelerator=accelerator),
        "imageMode": "use-upstream-image",
        "modelSource": "auto-cache",
    }
    if request.storage_type == "local-cache":
        runtime["mountPath"] = request.model_cache_path
    elif request.storage_type == "model-cache":
        runtime["storageVolumeId"] = request.storage_volume_id
    else:
        runtime["pvcName"] = request.pvc_name

    decode = {
        "replicaCount": candidate.replicas,
        "tensorParallelSize": candidate.tensor_parallel_size,
        "maxModelLen": request.max_model_len,
        "gpuMemoryUtilization": request.gpu_memory_utilization,
        "maxNumSeqs": request.max_num_seqs,
    }
    content = {
        "model": {"name": request.model},
        **({"hardware_profile": accelerator} if accelerator else {}),
        "decode": decode,
        "runtime": runtime,
        **({"prefixRouting": candidate.prefix_routing} if candidate.prefix_routing is not None else {}),
        "customParameters": [
            {
                "target": "both" if candidate.provider_ref == "pd-disaggregation" else "decode",
                "kind": "argument",
                "name": "gpu-memory-utilization",
                "value": str(request.gpu_memory_utilization),
            },
            {
                "target": "both" if candidate.provider_ref == "pd-disaggregation" else "decode",
                "kind": "argument",
                "name": "max-num-batched-tokens",
                "value": str(request.max_num_batched_tokens),
            },
            *[
                {"target": "decode", "kind": "argument", "name": argument["name"], "value": argument["value"]}
                for argument in request.vllm_arguments
            ],
        ],
    }
    if candidate.native_cpu_cache_gib is not None and candidate.provider_ref != "tiered-prefix-cache":
        content["guideComposition"] = {"nativeCpuCacheGiB": candidate.native_cpu_cache_gib}
    elif candidate.native_cpu_cache_gib is not None:
        content["guideSettings"] = {"cacheCpuGiB": candidate.native_cpu_cache_gib}
    # Agentic plans must carry the same credential policy as the selected
    # Model Cache entry.  Cache readiness only proves that model files exist;
    # llm-d still requires its deployment Secret, and omitting this field
    # silently degrades the downstream policy to ``mode=none``.
    if model_secret is not None and model_secret.mode != "none":
        content["modelSecret"] = model_secret.model_dump(mode="json", by_alias=True)
    configuration_type = "baseline"
    if candidate.provider_ref == "pd-disaggregation":
        configuration_type = "pd"
        content["prefill"] = {
            "replicaCount": candidate.prefill_replicas,
            "tensorParallelSize": candidate.prefill_tensor_parallel_size,
            "maxModelLen": request.max_model_len,
        }
        content["guideVariant"] = candidate.guide_variant or AGENTIC_RUNTIME_BACKEND
    elif candidate.provider_ref == "tiered-prefix-cache":
        configuration_type = "tiered_cache"
        variant = candidate.guide_variant or "native/cpu"
        if variant == "native/cpu/base":
            variant = "native/cpu"
        content["cache"] = {"variant": variant}
        content["guideVariant"] = variant
    elif candidate.provider_ref == "precise-prefix-cache-routing":
        content["router"] = {}

    return DeployableConfiguration(
        type=configuration_type,
        format="helm",
        content=content,
        provider_ref=candidate.provider_ref,
        checksum=configuration_checksum(content),
        provenance={
            "source": "agentic-deployment.v1",
            "deployment_name": request.deployment_name,
            "description": request.description.strip(),
            "agentic_candidate_id": candidate.id,
        },
    )


def planning_resource_request(run) -> dict:
    """Translate a reviewed candidate into the existing source-bound renderer contract."""
    plan = run.configuration_plan
    if plan is None:
        raise ValueError("This legacy plan must be regenerated before resource review.")
    configuration = plan.configuration
    content = configuration["content"]
    source = run.request
    selected = run.selected_candidate
    accelerator = content.get("hardware_profile")
    if not accelerator:
        raise ValueError("The target cluster has no supported hardware profile; resource generation is blocked.")
    body = {
        "guide": configuration["provider_ref"],
        "accelerator": accelerator,
        "modelServer": "vllm",
        "model": source.model,
        "clusterSessionId": source.cluster_session_id,
        "source": {"mode": "official"},
        "environment": {"mode": "current", "kubernetesMode": "required"},
        "modelSource": "auto-cache",
        "runtimeImage": content["runtime"]["image"],
        "replicas": selected.replicas,
        "tensorParallelSize": selected.tensor_parallel_size,
        "decodeReplicas": selected.replicas,
        "decodeTensorParallelSize": selected.tensor_parallel_size,
        "prefillReplicas": selected.prefill_replicas,
        "prefillTensorParallelSize": selected.prefill_tensor_parallel_size,
        "maxModelLen": source.max_model_len,
        "maxNumSeqs": source.max_num_seqs,
        "customParameters": content.get("customParameters", []),
        "guideVariant": selected.guide_variant or "",
        "resourceBasis": "available",
        "recommendationGpuCount": plan.requirements.hard_constraints.get("maxAccelerators"),
        "optimizationPolicy": {
            "mode": source.planning_mode,
            "prefixReuse": (
                "unknown"
                if plan.requirements.workload.get("prefixReuse") is None
                else "yes"
                if plan.requirements.workload["prefixReuse"]
                else "no"
            ),
            "excluded": [
                flag
                for capability, flag in (("prefix_cache", "prefix"), ("chunked_prefill", "chunked"))
                if capability in plan.requirements.forbidden_features
            ],
            "required": [
                flag
                for capability, flag in (("prefix_cache", "prefix"), ("chunked_prefill", "chunked"))
                if capability in plan.requirements.required_features
                or (
                    capability == "prefix_cache"
                    and (
                        selected.native_cpu_cache_gib
                        or selected.prefix_routing
                        or selected.provider_ref in {"precise-prefix-cache-routing", "tiered-prefix-cache"}
                    )
                )
            ],
        },
    }
    if content.get("guideComposition"):
        body["guideComposition"] = content["guideComposition"]
    if content.get("guideSettings"):
        body["guideSettings"] = content["guideSettings"]
    if selected.prefix_routing is not None:
        body["prefixRouting"] = selected.prefix_routing
    return body
