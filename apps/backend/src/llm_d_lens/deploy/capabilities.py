"""Stable deployment provider capabilities shared by Configuration and Evaluate."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shlex
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from llm_d_lens.infrastructure.kubernetes import scoped_runner
from llm_d_lens.infrastructure.process import CommandResult

logger = logging.getLogger(__name__)


def shared_prefix_routing_workload(*, full: bool = False) -> dict:
    """Fresh routing-comparison workload; provider goals and baselines stay local."""
    return {
        "num_groups": 150 if full else 4,
        "num_prompts_per_group": 5,
        "system_prompt_len": 6000 if full else 512,
        "question_len": 1200 if full else 128,
        "output_len": 1000 if full else 64,
        "enable_multi_turn_chat": False,
        "interval": 0,
        "stages": [{"rate": rate, "duration": 60 if full else 20} for rate in ((3, 10) if full else (0.4, 0.8))],
    }


# This flag declares whether a provider exposes a second endpoint on the same warmed pods
# for a routing-bypassing Kubernetes Service comparison. Evaluate consumes this registry;
# adding a provider capability must not require provider-name checks in the router or UI.
_PROVIDER_CAPABILITIES = {
    "optimized-baseline": {
        "label": "Optimized baseline",
        "variants": [],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "required_baselines": ["kubernetes-service", "load-only", "affinity-only"],
            "experiment_variable": "routing policy",
            "default_goal": "quick-check",
            "goals": [
                {
                    "id": "quick-check",
                    "label": "Lightweight routing check",
                    "recommended": True,
                    "description": "Small shared-prefix workload for initial validation, not a capacity measurement.",
                    "scenarios": [
                        {
                            "id": "quick-prefix",
                            "name": "Lightweight shared-prefix check",
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        }
                    ],
                },
                {
                    "id": "full-evaluation",
                    "label": "Full routing evaluation",
                    "recommended": False,
                    "description": (
                        "Kubernetes RR, load-only, affinity-only, and full routing arms under a shared-prefix "
                        "saturation sweep."
                    ),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Shared-prefix routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and measure throughput, TTFT, and "
                                "prefix locality against the selected references."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload(full=True)},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": (
                        "Run the shared-prefix workload across RR, load-only, affinity-only, and full optimized "
                        "routing."
                    ),
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "load-only", "affinity-only", "optimized-baseline"],
            "evidence": ["Prefix locality", "Per-endpoint token load", "Queue growth", "TTFT and SLO capacity"],
            "validation": [
                "Expected EPP plugins",
                "Endpoint discovery",
                "Prefill calibration",
                "Router and model metrics",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
            "full_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload(full=True)},
        },
    },
    "pd-disaggregation": {
        "label": "Prefill/decode disaggregation",
        "variants": [],
        "supports_pd": True,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "serving topology",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full P/D evaluation",
                    "recommended": True,
                    "description": (
                        "Resource-parity aggregate control plus long-input, negative-control, and geometry workloads."
                    ),
                    "scenarios": [
                        {
                            "id": "long-input",
                            "name": "P/D long-prompt performance",
                            "description": (
                                "Run long-input requests at increasing concurrency to measure the TTFT and throughput "
                                "benefit of separating prefill and decode."
                            ),
                            "benchmark": {
                                "matrix": [{"isl": 5000, "osl": 250}],
                                "concurrency_stages": [
                                    {"concurrency": 1, "num_requests": 16},
                                    {"concurrency": 8, "num_requests": 32},
                                ],
                            },
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Run the reference long-input workload and aggregate control.",
                    "scenario_ids": ["long-input"],
                },
                {
                    "id": "capacity",
                    "label": "Capacity / SLO",
                    "description": "Focus on the long-input load sweep and stable serving boundary.",
                    "scenario_ids": ["long-input"],
                },
            ],
            "comparison_arms": ["aggregated", "selected-pd-topologies"],
            "evidence": ["P/D routing decisions", "KV handoff", "Prefill and Decode pressure", "TTFT/ITL trade-off"],
            "validation": [
                "Prefill role discovery",
                "Decode role discovery",
                "KV transfer path",
                "Phase-separated metrics",
            ],
            "recommended_workload": {
                "kind": "matrix",
                "matrix": [
                    {"isl": 1024, "osl": 128},
                    {"isl": 8192, "osl": 128},
                    {"isl": 16384, "osl": 128},
                ],
                "concurrency_stages": [
                    {"concurrency": 1, "num_requests": 16},
                    {"concurrency": 8, "num_requests": 32},
                ],
            },
        },
    },
    "tiered-prefix-cache": {
        "label": "Tiered prefix cache",
        "variants": [],
        "variant_description": (
            "Each selected deployment overlay becomes an independent configuration and is "
            "evaluated with the same workload."
        ),
        "supports_pd": False,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "cache-tier",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full tiered-cache evaluation",
                    "recommended": True,
                    "description": "Compare HBM-only and tiered candidates under cache pressure.",
                }
            ],
            "comparison_arms": ["hbm-only", "selected-cache-tier"],
            "evidence": ["Working-set pressure", "Cache hit/offload", "Queue growth", "TTFT and capacity"],
            "validation": ["HBM cache", "Offload activity", "Metrics availability", "Baseline parity"],
            "recommended_workload": {
                "kind": "shared-prefix",
                "shared_prefix": {
                    "num_groups": 15,
                    "num_prompts_per_group": 5,
                    "system_prompt_len": 4000,
                    "question_len": 256,
                    "output_len": 256,
                    "enable_multi_turn_chat": False,
                    "interval": 60,
                    "stages": [
                        {"rate": 1.0, "duration": 60},
                        {"rate": 1.5, "duration": 60},
                    ],
                },
            },
        },
    },
    "precise-prefix-cache-routing": {
        "label": "Precise prefix-cache routing",
        "variants": [],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "required_baselines": ["kubernetes-service", "optimized-baseline"],
            "experiment_variable": "routing-policy",
            "default_goal": "quick-check",
            "goals": [
                {
                    "id": "quick-check",
                    "label": "Lightweight routing check",
                    "recommended": True,
                    "description": "Small shared-prefix workload for initial validation, not a capacity measurement.",
                    "scenarios": [
                        {
                            "id": "quick-prefix",
                            "name": "Lightweight shared-prefix check",
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        }
                    ],
                },
                {
                    "id": "full-evaluation",
                    "label": "Full precise-routing evaluation",
                    "recommended": False,
                    "description": ("RR, approximate, and precise routing under reuse, load, and a low-reuse control."),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Precise prefix-routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and compare precise KV-aware routing "
                                "with Kubernetes round-robin."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload(full=True)},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Compare Kubernetes RR with precise routing on the guide workload.",
                    "scenario_ids": ["shared-prefix"],
                },
                {
                    "id": "precise-value",
                    "label": "Precise value isolation",
                    "description": "Compare approximate and precise prefix knowledge with the same serving shape.",
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "optimized-baseline", "precise-prefix-cache-routing"],
            "evidence": [
                "KV-event/index health",
                "Cached prompt fraction",
                "Recomputed prompt tokens",
                "Warm-route locality",
            ],
            "validation": [
                "Model/tokenizer identity",
                "Block-size alignment",
                "KV publishers/subscribers",
                "Index lookup activity",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
            "full_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload(full=True)},
        },
    },
}


def _supported_accelerators() -> list[str]:
    """Upstream guide variants the deploy providers can render.

    Derived from the registered hardware profiles (``upstream_variant``: xpu,
    gpu, ...) so a new vendor does not need this list edited.
    """
    try:
        from llm_d_lens.hardware.registry import all_profiles

        variants = {profile.upstream_variant for profile in all_profiles() if profile.upstream_variant}
    except Exception:  # pragma: no cover - capabilities must not fail on discovery errors
        variants = set()
    return sorted(variants)


def _hardware_variants(metadata: dict) -> dict:
    """Guide entry points are discovered from the selected source checkout."""
    from copy import deepcopy

    result = deepcopy(metadata)
    result["variants"] = []
    result.pop("variant_labels", None)
    result.get("evaluation", {}).pop("unavailable_variants", None)
    result.get("evaluation", {}).pop("default_variant", None)
    return result


def deployment_capabilities() -> list[dict]:
    return [
        {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(metadata),
        }
        for provider, metadata in _PROVIDER_CAPABILITIES.items()
    ]


def provider_capability(provider: str) -> dict | None:
    capability = _PROVIDER_CAPABILITIES.get(provider)
    return (
        None
        if capability is None
        else {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(capability),
        }
    )


def provider_supports(provider: str, capability: str) -> bool:
    """Return a boolean capability without exposing the mutable registry entry."""
    return bool(_PROVIDER_CAPABILITIES.get(provider, {}).get(capability, False))


# ---------------------------------------------------------------------------
# Per-namespace SSAR preflight, subject detection & Orchestration Administrator Bootstrap Script
# ---------------------------------------------------------------------------

BOOTSTRAP_MARKER_NAME = "lens-deployment-bootstrap"
DEPLOY_ROLE_NAME = "lens-deployment-operator"
PROTECTED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "istio-system",
        "envoy-gateway-system",
        "envoy-ai-gateway-system",
        "agentgateway-system",
        "lens-edit",
        "llm-d-monitoring",
        "llm-d-bench-storage",
    }
)

_SSAR_CHUNK_SIZE = 50
_SSAR_TIMEOUT_SECONDS = 30.0
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_SUBJECT_NAME_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,253}$")
_K8S_LABEL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_K8S_LABEL_PREFIX_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SA_PREFIX = "system:serviceaccount:"
_REDACT_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_REDACT_KEY_VALUE_RE = re.compile(r"(?i)(token|password|secret|authorization|client-key-data)\s*[:=]\s*\S+")
_REDACT_KUBECONFIG_FLAG_RE = re.compile(r"--kubeconfig(?:=|\s+)\S+")
_REDACT_KUBECONFIG_PATH_RE = re.compile(r"(?:/[^\s'\":]+)+\.kubeconfig\b")


def redact_kubectl_error(text: str) -> str:
    """Strip kubeconfig paths, flags, tokens, and bearer credentials from kubectl diagnostics."""
    try:
        from llm_d_lens.deploy import slice_capacity as _slice_capacity
    except Exception:
        _slice_capacity = None
    delegate = getattr(_slice_capacity, "redact_kubectl_error", None) if _slice_capacity is not None else None
    cleaned = str(delegate(text)) if callable(delegate) else (text or "")
    cleaned = _REDACT_BEARER_RE.sub("Bearer <redacted>", cleaned)
    cleaned = _REDACT_KEY_VALUE_RE.sub(r"\1=<redacted>", cleaned)
    cleaned = _REDACT_KUBECONFIG_FLAG_RE.sub("--kubeconfig=<redacted>", cleaned)
    return _REDACT_KUBECONFIG_PATH_RE.sub("<kubeconfig>", cleaned)


_DEFAULT_EPP_SERVICE_ACCOUNTS: dict[str, tuple[str, ...]] = {
    "optimized-baseline": ("optimized-baseline-epp",),
    "pd-disaggregation": ("pd-disaggregation-epp",),
    "precise-prefix-cache-routing": ("precise-prefix-cache-routing-epp",),
    "tiered-prefix-cache": ("tiered-prefix-cache-epp",),
}

_VERIFIED_BINDING_TTL_SECONDS = 3600.0
_VERIFIED_BINDINGS_LOCK = threading.Lock()
_VERIFIED_NAMESPACES: dict[tuple[str, str, str, str], tuple[str, float]] = {}


class DeploymentCapabilityValidationError(ValueError):
    """Raised when deployment permission preflight inputs fail validation."""

    def __init__(self, message: str, *, code: str = "invalid_deployment_request", status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class DeploymentClusterAuthError(ValueError):
    """Raised when the cluster kubeconfig fails authentication (401)."""

    code = "kubeconfig_unauthorized"
    status_code = 401


class DeploymentCapabilityUnavailableError(RuntimeError):
    """Raised when SSAR or cluster discovery cannot answer."""

    code = "deployment_preflight_unavailable"
    status_code = 503


class DeploymentPermissionCheckRequest(BaseModel):
    """Input payload for deployment permission preflight and bootstrap script rendering."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    cluster_id: str = Field(min_length=1, validation_alias=AliasChoices("cluster_id", "clusterId"))
    namespace: str | None = None
    deployment_name: str | None = Field(
        default=None, validation_alias=AliasChoices("deployment_name", "deploymentName")
    )
    provider_ref: str | None = Field(default=None, validation_alias=AliasChoices("provider_ref", "providerRef"))
    agentic_deployment_run_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agentic_deployment_run_id", "agenticDeploymentRunId", "agentic_run_id"),
    )
    prepared_plan_id: str | None = Field(
        default=None, validation_alias=AliasChoices("prepared_plan_id", "preparedPlanId")
    )
    prepared_request_id: str | None = Field(
        default=None, validation_alias=AliasChoices("prepared_request_id", "preparedRequestId")
    )
    uses_storage: bool = Field(default=False, validation_alias=AliasChoices("uses_storage", "usesStorage"))
    storage_volume_id: str | None = Field(
        default=None, validation_alias=AliasChoices("storage_volume_id", "storageVolumeId")
    )
    uses_lws: bool = Field(default=False, validation_alias=AliasChoices("uses_lws", "usesLws"))
    uses_router: bool = Field(default=True, validation_alias=AliasChoices("uses_router", "usesRouter"))
    epp_service_accounts: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("epp_service_accounts", "eppServiceAccounts"),
    )
    subject_override: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("subject_override", "subjectOverride"),
    )
    manifest: str | None = None
    namespace_labels: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("namespace_labels", "namespaceLabels"),
    )


class DeploymentBootstrapVerifyRequest(DeploymentPermissionCheckRequest):
    """Input payload for verifying an Orchestration administrator namespace bootstrap."""

    expected_digest: str | None = Field(
        default=None,
        validation_alias=AliasChoices("expected_digest", "expectedDigest", "bootstrap_digest", "bootstrapDigest"),
    )


def _single_line_text(value: Any, field: str, *, code: str = "invalid_deployment_request") -> str:
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("\r", "\n", "\x00")):
        raise DeploymentCapabilityValidationError(f"{field} must be a non-empty single-line string", code=code)
    return value.strip()


def _namespace_prefix() -> str:
    from llm_d_lens.deploy.orphans import namespace_prefix

    return namespace_prefix()


def _protected_namespaces(cluster_id: str | None = None) -> frozenset[str]:
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.cluster.remediation import PROTECTED_NAMESPACES as _CLUSTER_PROTECTED_NAMESPACES

    protected = set(PROTECTED_NAMESPACES) | set(_CLUSTER_PROTECTED_NAMESPACES)
    if cluster_id:
        clean_id = cluster_id.strip()
        if clean_id:
            protected.add(f"llm-d-bench-storage-{clean_id}")
        cluster = cluster_registry.get_cluster(cluster_id)
        if cluster is not None:
            for attr in ("hf_token_secret_namespace", "gateway_namespace"):
                value = getattr(cluster, attr, None)
                if isinstance(value, str) and value.strip():
                    protected.add(value.strip())
        try:
            kubeconfig_ns = cluster_registry.cluster_kubeconfig_namespace(cluster_id)
        except Exception:
            kubeconfig_ns = None
        if isinstance(kubeconfig_ns, str) and kubeconfig_ns.strip():
            protected.add(kubeconfig_ns.strip())
        try:
            from llm_d_lens.storage.store import default_store

            for volume in default_store().list():
                if getattr(volume, "cluster_id", None) != cluster_id:
                    continue
                for spec_attr in ("local_disk", "nfs", "dynamic_pvc"):
                    spec = getattr(volume, spec_attr, None)
                    spec_ns = getattr(spec, "namespace", None) if spec is not None else None
                    if isinstance(spec_ns, str) and spec_ns.strip():
                        protected.add(spec_ns.strip())
        except Exception:
            logger.debug("Skipping storage volume namespace lookup during protected namespace check", exc_info=True)
    return frozenset(protected)


def _ensure_namespace_not_owned(namespace: str) -> None:
    from llm_d_lens.deploy.contracts import DeploymentStatus
    from llm_d_lens.deploy.executions import _get_store

    store = _get_store()
    active_owner = next(
        (
            execution
            for execution in store.list_executions()
            if execution.namespace == namespace
            and execution.status not in {DeploymentStatus.CLEANED, DeploymentStatus.CLEANED_UP}
        ),
        None,
    )
    if active_owner is not None:
        raise DeploymentCapabilityValidationError(
            f"namespace {namespace!r} is already used by an active deployment ({active_owner.execution_id})",
            code="namespace_in_use",
            status_code=409,
        )


def validate_dns1123_label(value: Any, field: str, *, code: str = "invalid_namespace") -> str:
    text = _single_line_text(value, field, code=code)
    if len(text) > 63 or not _DNS1123_LABEL_RE.fullmatch(text):
        raise DeploymentCapabilityValidationError(
            f"{field} must be a valid DNS-1123 label (lowercase alphanumeric and '-', max 63 chars)",
            code=code,
        )
    return text


def validate_deployment_namespace(
    namespace: Any,
    *,
    cluster_id: str | None = None,
    prefix: str | None = None,
) -> str:
    """Validate that a target deployment namespace is DNS-1123 safe, prefixed, and non-protected."""
    text = validate_dns1123_label(namespace, "namespace", code="invalid_namespace")
    if text in _protected_namespaces(cluster_id):
        raise DeploymentCapabilityValidationError(
            f"namespace {text!r} is protected and cannot be used for a deployment",
            code="protected_namespace",
        )
    active_prefix = prefix or _namespace_prefix()
    if text == active_prefix or not text.startswith(active_prefix):
        raise DeploymentCapabilityValidationError(
            f"namespace {text!r} must start with the configured deployment prefix {active_prefix!r}",
            code="invalid_namespace",
        )
    return text


def validate_namespace_labels(labels: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate Kubernetes Namespace metadata labels strictly."""
    if labels is None:
        return {}
    if not isinstance(labels, Mapping):
        raise DeploymentCapabilityValidationError("namespace_labels must be a mapping", code="invalid_labels")
    validated: dict[str, str] = {}
    for raw_key, raw_value in labels.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise DeploymentCapabilityValidationError(
                "namespace_labels keys and values must be strings", code="invalid_labels"
            )
        if any(ch in raw_key or ch in raw_value for ch in ("\r", "\n", "\x00")):
            raise DeploymentCapabilityValidationError(
                "namespace_labels must not contain control characters", code="invalid_labels"
            )
        key = raw_key.strip()
        value = raw_value.strip()
        if "/" in key:
            prefix_part, name_part = key.split("/", 1)
            if (
                not prefix_part
                or len(prefix_part) > 253
                or not _K8S_LABEL_PREFIX_RE.fullmatch(prefix_part)
                or not name_part
                or len(name_part) > 63
                or not _K8S_LABEL_SEGMENT_RE.fullmatch(name_part)
            ):
                raise DeploymentCapabilityValidationError(
                    f"invalid Kubernetes label key {key!r}", code="invalid_labels"
                )
        elif not key or len(key) > 63 or not _K8S_LABEL_SEGMENT_RE.fullmatch(key):
            raise DeploymentCapabilityValidationError(f"invalid Kubernetes label key {key!r}", code="invalid_labels")
        if len(value) > 63 or (value and not _K8S_LABEL_SEGMENT_RE.fullmatch(value)):
            raise DeploymentCapabilityValidationError(
                f"invalid Kubernetes label value for {key!r}", code="invalid_labels"
            )
        validated[key] = value
    return validated


def validate_subject_override(override: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Validate an explicit RBAC subject override (ServiceAccount, User, or Group)."""
    if override is None:
        return None
    if not isinstance(override, Mapping):
        raise DeploymentCapabilityValidationError("subjectOverride must be an object", code="invalid_subject")
    kind = _single_line_text(override.get("kind"), "subjectOverride.kind", code="invalid_subject")
    if kind not in {"ServiceAccount", "User", "Group"}:
        raise DeploymentCapabilityValidationError(
            "subjectOverride.kind must be ServiceAccount, User, or Group",
            code="invalid_subject",
        )
    raw_name = override.get("name")
    name = _single_line_text(raw_name, "subjectOverride.name", code="invalid_subject")
    if any(ch.isspace() or ch in {"'", '"', "`", "$", "\\"} for ch in name):
        raise DeploymentCapabilityValidationError(
            "subjectOverride.name contains unsupported characters", code="invalid_subject"
        )
    raw_ns = override.get("namespace")
    if kind == "ServiceAccount":
        sa_name = validate_dns1123_label(name, "subjectOverride.name", code="invalid_subject")
        sa_ns = validate_dns1123_label(raw_ns, "subjectOverride.namespace", code="invalid_subject")
        return {
            "kind": "ServiceAccount",
            "name": sa_name,
            "namespace": sa_ns,
            "subject": f"{_SA_PREFIX}{sa_ns}:{sa_name}",
        }
    if raw_ns is not None and str(raw_ns).strip():
        raise DeploymentCapabilityValidationError(
            f"subjectOverride.namespace is only valid for ServiceAccount, not {kind}",
            code="invalid_subject",
        )
    if not _SUBJECT_NAME_RE.fullmatch(name):
        raise DeploymentCapabilityValidationError(
            "subjectOverride.name is not a valid Kubernetes subject", code="invalid_subject"
        )
    return {"kind": kind, "name": name, "namespace": "", "subject": name}


def resolve_target_namespace(request: DeploymentPermissionCheckRequest, *, prefix: str | None = None) -> str:
    """Resolve and strictly validate the target deployment namespace."""
    active_prefix = prefix or _namespace_prefix()
    if request.namespace is not None and request.namespace.strip():
        return validate_deployment_namespace(request.namespace, cluster_id=request.cluster_id, prefix=active_prefix)
    seed_source = (
        request.agentic_deployment_run_id
        or request.prepared_plan_id
        or request.deployment_name
        or request.provider_ref
        or "deployment"
    )
    slug = re.sub(r"[^a-z0-9-]+", "-", seed_source.lower()).strip("-") or "deploy"
    digest = hashlib.sha256(
        json.dumps(
            {
                "cluster_id": request.cluster_id,
                "agentic_deployment_run_id": request.agentic_deployment_run_id,
                "prepared_plan_id": request.prepared_plan_id,
                "deployment_name": request.deployment_name,
                "provider_ref": request.provider_ref,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:8]
    max_slug = max(1, 63 - len(active_prefix) - len(digest) - 1)
    trimmed_slug = slug[:max_slug].rstrip("-") or "d"
    candidate = f"{active_prefix}{trimmed_slug}-{digest}"
    return validate_deployment_namespace(candidate, cluster_id=request.cluster_id, prefix=active_prefix)


def _pod_specs_from_doc(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    specs: list[Mapping[str, Any]] = []
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, Mapping):
        return specs
    if kind == "Pod":
        specs.append(spec)
    elif kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}:
        template = spec.get("template")
        if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping):
            specs.append(template["spec"])
    elif kind == "LeaderWorkerSet":
        lwt = spec.get("leaderWorkerTemplate")
        if isinstance(lwt, Mapping):
            for key in ("leaderTemplate", "workerTemplate"):
                tpl = lwt.get(key)
                if isinstance(tpl, Mapping) and isinstance(tpl.get("spec"), Mapping):
                    specs.append(tpl["spec"])
    elif kind == "DisaggregatedSet":
        for role in spec.get("roles") or ():
            if not isinstance(role, Mapping):
                continue
            role_spec = role.get("spec")
            if not isinstance(role_spec, Mapping):
                continue
            lwt = role_spec.get("leaderWorkerTemplate")
            if isinstance(lwt, Mapping):
                for key in ("leaderTemplate", "workerTemplate"):
                    tpl = lwt.get(key)
                    if isinstance(tpl, Mapping) and isinstance(tpl.get("spec"), Mapping):
                        specs.append(tpl["spec"])
    return specs


def _parse_manifest_hints(manifest: str | None) -> dict[str, Any]:
    """Extract EPP ServiceAccounts and CRD/storage usage from a rendered manifest."""
    if manifest is None or not manifest.strip():
        return {"epp_service_accounts": [], "uses_lws": False, "uses_router": False, "uses_storage": False}
    try:
        documents = [doc for doc in yaml.safe_load_all(manifest) if doc is not None]
    except yaml.YAMLError as error:
        raise DeploymentCapabilityValidationError(
            f"manifest is not valid YAML: {error}", code="invalid_manifest"
        ) from error
    epp_sas: list[str] = []
    uses_lws = False
    uses_router = False
    uses_storage = False
    for doc in documents:
        if not isinstance(doc, Mapping):
            raise DeploymentCapabilityValidationError(
                "manifest documents must be YAML mappings", code="invalid_manifest"
            )
        kind = str(doc.get("kind") or "")
        if kind in {"LeaderWorkerSet", "DisaggregatedSet"}:
            uses_lws = True
        if kind in {"InferencePool", "InferenceModelRewrite", "HTTPRoute", "ReferenceGrant"}:
            uses_router = True
        if kind == "PersistentVolumeClaim":
            uses_storage = True
        for pod in _pod_specs_from_doc(doc):
            containers = pod.get("containers") or ()
            if isinstance(containers, list) and any(
                isinstance(container, Mapping) and container.get("name") == "epp" for container in containers
            ):
                uses_router = True
                sa_name = pod.get("serviceAccountName") or pod.get("serviceAccount")
                if isinstance(sa_name, str) and sa_name.strip():
                    epp_sas.append(sa_name.strip())
            for volume in pod.get("volumes") or ():
                if isinstance(volume, Mapping) and isinstance(volume.get("persistentVolumeClaim"), Mapping):
                    uses_storage = True
    return {
        "epp_service_accounts": list(dict.fromkeys(epp_sas)),
        "uses_lws": uses_lws,
        "uses_router": uses_router,
        "uses_storage": uses_storage,
    }


def _resolve_epp_service_accounts(
    request: DeploymentPermissionCheckRequest, manifest_hints: Mapping[str, Any], *, uses_router: bool
) -> list[str]:
    if not uses_router:
        return []
    candidates: list[str] = []
    for item in request.epp_service_accounts:
        candidates.append(validate_dns1123_label(item, "epp_service_accounts[]", code="invalid_subject"))
    for item in manifest_hints.get("epp_service_accounts") or ():
        candidates.append(validate_dns1123_label(item, "manifest EPP serviceAccountName", code="invalid_manifest"))
    if not candidates and request.provider_ref:
        candidates.extend(_DEFAULT_EPP_SERVICE_ACCOUNTS.get(request.provider_ref, ()))
    if not candidates:
        candidates.append("gaie-inference-scheduling-epp")
    return list(dict.fromkeys(candidates))


def _identity_from_username(username: str, *, source: str) -> dict[str, Any]:
    cleaned = username.strip()
    if cleaned.startswith(_SA_PREFIX):
        rest = cleaned[len(_SA_PREFIX) :]
        if ":" in rest:
            sa_ns, sa_name = rest.split(":", 1)
            if _DNS1123_LABEL_RE.fullmatch(sa_ns) and _DNS1123_LABEL_RE.fullmatch(sa_name):
                return {
                    "kind": "ServiceAccount",
                    "name": sa_name,
                    "namespace": sa_ns,
                    "subject": cleaned,
                    "source": source,
                    "overrideRequired": False,
                }
    if cleaned and _SUBJECT_NAME_RE.fullmatch(cleaned):
        return {
            "kind": "User",
            "name": cleaned,
            "namespace": None,
            "subject": cleaned,
            "source": source,
            "overrideRequired": False,
        }
    return {
        "kind": "unknown",
        "name": None,
        "namespace": None,
        "subject": None,
        "source": "none",
        "overrideRequired": True,
    }


def _identity_from_jwt_token(token: str) -> dict[str, Any] | None:
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    sub = payload.get("sub")
    if isinstance(sub, str) and sub.strip():
        parsed = _identity_from_username(sub, source="kubeconfig")
        if not parsed["overrideRequired"]:
            return parsed
    k8s_claims = payload.get("kubernetes.io/serviceaccount")
    if isinstance(k8s_claims, Mapping):
        sa_ns = k8s_claims.get("namespace")
        sa_name = k8s_claims.get("name")
        if (
            isinstance(sa_ns, str)
            and isinstance(sa_name, str)
            and _DNS1123_LABEL_RE.fullmatch(sa_ns)
            and _DNS1123_LABEL_RE.fullmatch(sa_name)
        ):
            return {
                "kind": "ServiceAccount",
                "name": sa_name,
                "namespace": sa_ns,
                "subject": f"{_SA_PREFIX}{sa_ns}:{sa_name}",
                "source": "kubeconfig",
                "overrideRequired": False,
            }
    ns_claim = payload.get("kubernetes.io/serviceaccount/namespace")
    name_claim = payload.get("kubernetes.io/serviceaccount/service-account.name")
    if (
        isinstance(ns_claim, str)
        and isinstance(name_claim, str)
        and _DNS1123_LABEL_RE.fullmatch(ns_claim)
        and _DNS1123_LABEL_RE.fullmatch(name_claim)
    ):
        return {
            "kind": "ServiceAccount",
            "name": name_claim,
            "namespace": ns_claim,
            "subject": f"{_SA_PREFIX}{ns_claim}:{name_claim}",
            "source": "kubeconfig",
            "overrideRequired": False,
        }
    return None


def _identity_from_kubeconfig(cluster_id: str) -> dict[str, Any] | None:
    from llm_d_lens.cluster import registry as cluster_registry

    raw = cluster_registry.read_kubeconfig(cluster_id)
    if not raw:
        return None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    current_context = parsed.get("current-context")
    user_ref: str | None = None
    for entry in parsed.get("contexts") or ():
        if isinstance(entry, Mapping) and (not current_context or entry.get("name") == current_context):
            ctx = entry.get("context")
            if isinstance(ctx, Mapping) and isinstance(ctx.get("user"), str):
                user_ref = ctx["user"]
                break
    for entry in parsed.get("users") or ():
        if not isinstance(entry, Mapping):
            continue
        if user_ref and entry.get("name") != user_ref:
            continue
        user_obj = entry.get("user")
        if isinstance(user_obj, Mapping):
            token = user_obj.get("token")
            if isinstance(token, str) and token.strip():
                jwt_identity = _identity_from_jwt_token(token)
                if jwt_identity is not None:
                    return jwt_identity
            username = user_obj.get("username")
            if isinstance(username, str) and username.strip():
                ident = _identity_from_username(username, source="kubeconfig")
                if not ident["overrideRequired"]:
                    return ident
        name = entry.get("name")
        if isinstance(name, str) and name.startswith(_SA_PREFIX):
            ident = _identity_from_username(name, source="kubeconfig")
            if not ident["overrideRequired"]:
                return ident
    return None


async def detect_kubeconfig_identity(
    cluster_id: str,
    subject_override: Mapping[str, Any] | None = None,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Auto-detect the Kubernetes subject via SelfSubjectReview -> whoami -> kubeconfig."""
    override = validate_subject_override(subject_override)
    if override is not None:
        return {
            "kind": override["kind"],
            "name": override["name"],
            "namespace": override["namespace"] or None,
            "subject": override["subject"],
            "source": "override",
            "overrideRequired": False,
        }

    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    from llm_d_lens.cluster.service import _is_unauthorized_output

    ssr_body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
    try:
        ssr = await active_runner.run(["kubectl", "create", "-o", "json", "-f", "-"], input=ssr_body, timeout=15)
    except (FileNotFoundError, TimeoutError, OSError):
        ssr = None
    if isinstance(ssr, CommandResult):
        if _is_unauthorized_output(ssr):
            raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
        if ssr.returncode == 0 and ssr.stdout:
            try:
                payload = json.loads(ssr.stdout)
                username = ((payload.get("status") or {}).get("userInfo") or {}).get("username")
                if isinstance(username, str) and username.strip() and username.strip() != "system:anonymous":
                    ident = _identity_from_username(username, source="self_subject_review")
                    if not ident["overrideRequired"]:
                        return ident
            except (ValueError, AttributeError):
                pass

    try:
        whoami = await active_runner.run(["kubectl", "auth", "whoami", "-o", "json"], timeout=15)
    except (FileNotFoundError, TimeoutError, OSError):
        whoami = None
    if isinstance(whoami, CommandResult):
        if _is_unauthorized_output(whoami):
            raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
        if whoami.returncode == 0 and whoami.stdout:
            try:
                payload = json.loads(whoami.stdout)
                username = ((payload.get("status") or {}).get("userInfo") or {}).get("username")
                if isinstance(username, str) and username.strip() and username.strip() != "system:anonymous":
                    ident = _identity_from_username(username, source="whoami")
                    if not ident["overrideRequired"]:
                        return ident
            except (ValueError, AttributeError):
                pass

    from_kubeconfig = _identity_from_kubeconfig(cluster_id)
    if from_kubeconfig is not None:
        return from_kubeconfig

    return {
        "kind": "unknown",
        "name": None,
        "namespace": None,
        "subject": None,
        "source": "none",
        "overrideRequired": True,
    }


def _ssar_key(
    verb: str, api_group: str, resource: str, namespace: str | None = None, name: str | None = None
) -> tuple[str, ...]:
    return (verb, api_group, resource, namespace or "", name or "")


def _ssar_item(key: tuple[str, ...]) -> dict[str, Any]:
    verb, api_group, resource, namespace, name = key
    attrs: dict[str, Any] = {"verb": verb, "group": api_group, "resource": resource}
    if namespace:
        attrs["namespace"] = namespace
    if name:
        attrs["name"] = name
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
    }


def _ssar_key_from_doc(doc: Any) -> tuple[str, ...]:
    if not isinstance(doc, Mapping):
        raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview response item is not an object")
    attrs = (doc.get("spec") or {}).get("resourceAttributes")
    if not isinstance(attrs, Mapping):
        raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview response missing resourceAttributes")
    verb = attrs.get("verb")
    resource = attrs.get("resource")
    if not isinstance(verb, str) or not isinstance(resource, str):
        raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview resourceAttributes incomplete")
    return (
        verb,
        str(attrs.get("group") or ""),
        resource,
        str(attrs.get("namespace") or ""),
        str(attrs.get("name") or ""),
    )


def _parse_ssar_docs(stdout: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview returned empty output")
    decoder = json.JSONDecoder()
    docs: list[dict[str, Any]] = []
    pos = 0
    while pos < len(text):
        try:
            value, pos = decoder.raw_decode(text, pos)
        except ValueError as error:
            raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview returned invalid JSON") from error
        if isinstance(value, Mapping) and str(value.get("kind", "")).endswith("List"):
            items = value.get("items")
            if not isinstance(items, list):
                raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview List missing items")
            docs.extend(dict(item) for item in items if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            docs.append(dict(value))
        else:
            raise DeploymentCapabilityUnavailableError("SelfSubjectAccessReview returned unexpected JSON")
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return docs


async def _run_ssar_checks(
    keys: Sequence[tuple[str, ...]],
    *,
    runner: Any,
) -> dict[tuple[str, ...], bool]:
    if not keys:
        return {}
    from llm_d_lens.cluster.service import _is_unauthorized_output

    answers: dict[tuple[str, ...], bool] = {}
    unique_keys = list(dict.fromkeys(keys))
    for start in range(0, len(unique_keys), _SSAR_CHUNK_SIZE):
        chunk = unique_keys[start : start + _SSAR_CHUNK_SIZE]
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": [_ssar_item(k) for k in chunk]})
        try:
            result = await runner.run(
                ["kubectl", "create", "-o", "json", "-f", "-"],
                input=body,
                timeout=_SSAR_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise DeploymentCapabilityUnavailableError(
                f"SSAR check failed: {redact_kubectl_error(str(error))}"
            ) from error
        if not isinstance(result, CommandResult):
            raise DeploymentCapabilityUnavailableError("SSAR runner returned invalid result")
        if _is_unauthorized_output(result):
            raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
        if result.returncode != 0:
            detail = redact_kubectl_error((result.stderr or result.stdout or "kubectl failed").strip())
            raise DeploymentCapabilityUnavailableError(f"SSAR check failed: {detail[:300]}")
        docs = _parse_ssar_docs(result.stdout or "")
        if len(docs) != len(chunk):
            raise DeploymentCapabilityUnavailableError(
                f"SSAR check returned {len(docs)} answers for {len(chunk)} reviews"
            )
        expected = set(chunk)
        seen: set[tuple[str, ...]] = set()
        for doc in docs:
            key = _ssar_key_from_doc(doc)
            if key in seen or key not in expected:
                raise DeploymentCapabilityUnavailableError("SSAR check returned duplicate or unexpected answer")
            allowed = (doc.get("status") or {}).get("allowed")
            if not isinstance(allowed, bool):
                raise DeploymentCapabilityUnavailableError("SSAR response missing boolean allowed status")
            seen.add(key)
            answers[key] = allowed
        if seen != expected:
            raise DeploymentCapabilityUnavailableError("SSAR response omitted one or more requested checks")
    return answers


async def read_bootstrap_marker(
    cluster_id: str,
    namespace: str,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Read ConfigMap/lens-deployment-bootstrap in ``namespace`` cleanly."""
    from llm_d_lens.cluster.service import _is_forbidden_output, _is_unauthorized_output

    try:
        active_runner = runner if runner is not None else scoped_runner(cluster_id)
        result = await active_runner.run(
            [
                "kubectl",
                "get",
                "configmap",
                BOOTSTRAP_MARKER_NAME,
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "-o",
                "json",
            ],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError, ValueError):
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    if not isinstance(result, CommandResult):
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    if _is_unauthorized_output(result):
        raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
    if result.returncode != 0:
        status = "unconfirmed" if _is_forbidden_output(result) else "missing"
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": status,
            "verified": False,
        }
    text = (result.stdout or "").strip()
    if not text:
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    try:
        payload = json.loads(text)
    except ValueError:
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    if not isinstance(payload, Mapping):
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    data = payload.get("data")
    if not isinstance(data, Mapping):
        data = {}
    marker_cluster = data.get("clusterId")
    marker_namespace = data.get("namespace")
    marker_status = data.get("status") if isinstance(data.get("status"), str) else ""
    bootstrap_digest = data.get("bootstrapDigest") if isinstance(data.get("bootstrapDigest"), str) else None
    epp_auth_delegator = data.get("eppAuthDelegator") if isinstance(data.get("eppAuthDelegator"), str) else None
    prepared_request = data.get("preparedRequest") if isinstance(data.get("preparedRequest"), str) else None
    subject_kind = data.get("subjectKind") if isinstance(data.get("subjectKind"), str) else None
    subject_name = data.get("subjectName") if isinstance(data.get("subjectName"), str) else None
    completed_at = data.get("completedAt") if isinstance(data.get("completedAt"), str) else None
    epp_sas = (
        [item for item in data["eppServiceAccounts"].split(",") if item]
        if isinstance(data.get("eppServiceAccounts"), str)
        else []
    )
    cluster_mismatch = marker_cluster is not None and (
        not isinstance(marker_cluster, str) or (bool(marker_cluster) and marker_cluster != cluster_id)
    )
    namespace_mismatch = marker_namespace is not None and (
        not isinstance(marker_namespace, str) or (bool(marker_namespace) and marker_namespace != namespace)
    )
    if cluster_mismatch or namespace_mismatch:
        return {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "stale",
            "verified": False,
            "bootstrapDigest": bootstrap_digest,
        }
    present = marker_status == "completed"
    return {
        "namespace": namespace,
        "name": BOOTSTRAP_MARKER_NAME,
        "present": present,
        "status": "completed" if present else (marker_status or "missing"),
        "verified": present,
        "bootstrapDigest": bootstrap_digest,
        "eppAuthDelegator": epp_auth_delegator,
        "eppServiceAccounts": epp_sas,
        "preparedRequest": prepared_request,
        "subjectKind": subject_kind,
        "subjectName": subject_name,
        "completedAt": completed_at,
    }


async def is_bootstrap_marker_present(cluster_id: str, namespace: str, *, runner: Any = None) -> bool:
    """Return True when ConfigMap/lens-deployment-bootstrap is completed in ``namespace``."""
    marker = await read_bootstrap_marker(cluster_id, namespace, runner=runner)
    return bool(marker.get("present"))


async def _namespace_exists(cluster_id: str, namespace: str, *, runner: Any) -> bool:
    from llm_d_lens.cluster.service import _is_forbidden_output, _is_unauthorized_output

    try:
        res = await runner.run(
            ["kubectl", "get", "namespace", namespace, "--ignore-not-found=true", "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return False
    if isinstance(res, CommandResult):
        if _is_unauthorized_output(res):
            raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
        if res.returncode == 0:
            return bool((res.stdout or "").strip())
        if _is_forbidden_output(res):
            # Non-Admin may lack cluster-wide get namespace while having namespace-scoped RBAC.
            try:
                sa_res = await runner.run(
                    ["kubectl", "get", "serviceaccounts", "--namespace", namespace, "-o", "name"],
                    timeout=15,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return False
            if isinstance(sa_res, CommandResult):
                if _is_unauthorized_output(sa_res):
                    raise DeploymentClusterAuthError(
                        "Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized)."
                    )
                if sa_res.returncode == 0:
                    return True
    return False


async def _resolve_satellite_storage(
    cluster_id: str,
    storage_volume_id: str | None,
    namespace: str,
    *,
    runner: Any,
) -> dict[str, Any] | None:
    if not storage_volume_id or not storage_volume_id.strip():
        return None
    from llm_d_lens.cluster.service import _is_unauthorized_output
    from llm_d_lens.storage.contracts import StorageVolumeKind
    from llm_d_lens.storage.service import (
        _pvc_name,
        _satellite_pv_manifest,
        _satellite_pvc_manifest,
        get_ready_volume,
    )

    try:
        volume = await get_ready_volume(storage_volume_id.strip(), cluster_id=cluster_id)
    except Exception:
        return None
    if volume.kind not in {StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS}:
        return None
    pvc_name = _pvc_name(volume)
    pvc_bound = False
    try:
        pvc_res = await runner.run(
            ["kubectl", "get", "pvc", pvc_name, "--namespace", namespace, "--ignore-not-found=true", "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        pvc_res = None
    if isinstance(pvc_res, CommandResult):
        if _is_unauthorized_output(pvc_res):
            raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")
        if pvc_res.returncode == 0 and (pvc_res.stdout or "").strip():
            try:
                pvc_doc = json.loads(pvc_res.stdout)
                pvc_bound = (pvc_doc.get("status") or {}).get("phase") == "Bound"
            except ValueError:
                pvc_bound = False
    return {
        "volumeId": volume.id,
        "pvcName": pvc_name,
        "pvcBound": pvc_bound,
        "pvManifest": _satellite_pv_manifest(volume, namespace=namespace),
        "pvcManifest": _satellite_pvc_manifest(volume, namespace=namespace),
    }


def _ssar_operation_specs(
    *,
    namespace: str,
    uses_storage: bool,
    uses_router: bool,
    uses_lws: bool,
    namespace_exists: bool,
    marker: Mapping[str, Any],
    epp_service_accounts: Sequence[str],
    satellite_storage: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    ns_verbs = ("get", "list", "create", "update", "patch", "delete")
    ns_resources: list[tuple[str, str, tuple[str, ...]]] = [
        ("apps", "deployments", ns_verbs),
        ("", "services", ns_verbs),
        ("", "configmaps", ns_verbs),
        ("", "secrets", ns_verbs),
        ("", "serviceaccounts", ns_verbs),
        ("", "pods", ("get", "list", "delete")),
        ("monitoring.coreos.com", "servicemonitors", ns_verbs),
        ("monitoring.coreos.com", "podmonitors", ns_verbs),
        ("rbac.authorization.k8s.io", "roles", ns_verbs),
        ("rbac.authorization.k8s.io", "rolebindings", ns_verbs),
    ]
    if uses_storage:
        ns_resources.append(("", "persistentvolumeclaims", ns_verbs))
    if uses_router:
        ns_resources.extend(
            [
                ("inference.networking.k8s.io", "inferencepools", ns_verbs),
                ("gateway.networking.k8s.io", "referencegrants", ns_verbs),
                ("gateway.networking.k8s.io", "httproutes", ns_verbs),
                ("llm-d.ai", "inferencemodelrewrites", ns_verbs),
            ]
        )
    if uses_lws:
        ns_resources.extend(
            [
                ("leaderworkerset.x-k8s.io", "leaderworkersets", ns_verbs),
                ("disaggregatedset.x-k8s.io", "disaggregatedsets", ns_verbs),
            ]
        )

    operations: list[dict[str, Any]] = [
        {
            "id": "deploy.namespace",
            "step": 1,
            "title": f"Create namespace {namespace}",
            "required": not namespace_exists and not bool(marker.get("present")),
            "keys": [_ssar_key("create", "", "namespaces")],
        },
        {
            "id": "deploy.rbac",
            "step": 2,
            "title": f"Grant deployment RBAC in namespace {namespace}",
            "required": True,
            "keys": [
                _ssar_key(verb, group, resource, namespace) for group, resource, verbs in ns_resources for verb in verbs
            ],
        },
    ]
    if uses_router:
        marker_epp_applied = bool(marker.get("present")) and marker.get("eppAuthDelegator") == "applied"
        marker_sas = set(marker.get("eppServiceAccounts") or ())
        all_epp_covered = marker_epp_applied and (not marker_sas or set(epp_service_accounts).issubset(marker_sas))
        operations.append(
            {
                "id": "deploy.epp_auth_delegator",
                "step": 3,
                "title": f"Grant EPP system:auth-delegator binding for {namespace}",
                "required": not all_epp_covered,
                "keys": [_ssar_key("create", "rbac.authorization.k8s.io", "clusterrolebindings")],
            }
        )
    if satellite_storage is not None:
        operations.append(
            {
                "id": "deploy.satellite_storage",
                "step": 4,
                "title": f"Bind Model Cache storage PV/PVC in {namespace}",
                "required": not bool(satellite_storage.get("pvcBound")),
                "keys": [_ssar_key("create", "", "persistentvolumes")],
            }
        )
    return operations


def _rule_dict(key: tuple[str, ...]) -> dict[str, Any]:
    verb, api_group, resource, namespace, name = key
    entry: dict[str, Any] = {
        "verb": verb,
        "apiGroup": api_group,
        "resource": resource,
        "scope": "namespace" if namespace else "cluster",
    }
    if namespace:
        entry["namespace"] = namespace
    if name:
        entry["name"] = name
    return entry


async def check_deployment_permissions(
    request: DeploymentPermissionCheckRequest,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Evaluate per-namespace SSAR permissions for a deployment target."""
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.cluster.egress import EgressBlockedError, validate_stored_cluster_egress

    cluster_id = _single_line_text(request.cluster_id, "cluster_id")
    if runner is None:
        try:
            validate_stored_cluster_egress(cluster_id)
        except EgressBlockedError as error:
            raise DeploymentCapabilityValidationError(str(error), code="kubeconfig_egress_blocked") from error

    namespace = resolve_target_namespace(request)
    _ensure_namespace_not_owned(namespace)
    labels = validate_namespace_labels(request.namespace_labels)
    manifest_hints = _parse_manifest_hints(request.manifest)
    uses_storage = bool(request.uses_storage or request.storage_volume_id or manifest_hints["uses_storage"])
    uses_lws = bool(request.uses_lws or manifest_hints["uses_lws"])
    uses_router = bool(request.uses_router or manifest_hints["uses_router"])
    epp_service_accounts = _resolve_epp_service_accounts(request, manifest_hints, uses_router=uses_router)

    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    identity = await detect_kubeconfig_identity(
        cluster_id,
        subject_override=request.subject_override,
        runner=active_runner,
    )
    ns_exists = await _namespace_exists(cluster_id, namespace, runner=active_runner)
    marker = (
        await read_bootstrap_marker(cluster_id, namespace, runner=active_runner)
        if ns_exists
        else {
            "namespace": namespace,
            "name": BOOTSTRAP_MARKER_NAME,
            "present": False,
            "status": "missing",
            "verified": False,
        }
    )
    satellite_storage = await _resolve_satellite_storage(
        cluster_id, request.storage_volume_id, namespace, runner=active_runner
    )
    op_specs = _ssar_operation_specs(
        namespace=namespace,
        uses_storage=uses_storage,
        uses_router=uses_router,
        uses_lws=uses_lws,
        namespace_exists=ns_exists,
        marker=marker,
        epp_service_accounts=epp_service_accounts,
        satellite_storage=satellite_storage,
    )

    cluster = cluster_registry.get_cluster(request.cluster_id)
    hf_secret_key: tuple[str, ...] | None = None
    if cluster is not None and cluster.hf_token_secret_namespace and cluster.hf_token_secret_name:
        hf_secret_key = _ssar_key("get", "", "secrets", cluster.hf_token_secret_namespace, cluster.hf_token_secret_name)

    all_keys = [key for spec in op_specs for key in spec["keys"]]
    if hf_secret_key is not None:
        all_keys.append(hf_secret_key)

    answers = await _run_ssar_checks(all_keys, runner=active_runner)

    admin_operations: list[dict[str, Any]] = []
    user_operations: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for spec in op_specs:
        denied_rules = [_rule_dict(key) for key in spec["keys"] if answers.get(key) is False]
        if spec["required"] and denied_rules:
            admin_operations.append(
                {
                    "id": spec["id"],
                    "step": spec["step"],
                    "title": spec["title"],
                    "executor": "admin",
                    "missing": denied_rules,
                }
            )
            for rule in denied_rules:
                missing.append({"operationId": spec["id"], **rule})
        else:
            user_operations.append(
                {
                    "id": spec["id"],
                    "step": spec["step"],
                    "title": spec["title"],
                    "executor": "user",
                }
            )

    warnings: list[str] = []
    if hf_secret_key is not None and answers.get(hf_secret_key) is False:
        warnings.append(
            f"The kubeconfig cannot read source HF_TOKEN Secret "
            f"{cluster.hf_token_secret_namespace}/{cluster.hf_token_secret_name}; "
            "public models work, while gated models require Secret read access or a per-deployment token."
        )
    if identity.get("overrideRequired") and admin_operations:
        warnings.append(
            "Kubernetes identity could not be auto-detected from this kubeconfig; "
            "specify a ServiceAccount or User/Group subject before generating the Orchestration administrator script."
        )

    sufficient = len(admin_operations) == 0
    return {
        "status": "sufficient" if sufficient else "admin_required",
        "sufficient": sufficient,
        "canCreateNamespaces": bool(answers.get(_ssar_key("create", "", "namespaces"))),
        "clusterId": request.cluster_id,
        "namespace": namespace,
        "namespaceExists": ns_exists,
        "namespaceLabels": labels,
        "identity": identity,
        "adminOperations": admin_operations,
        "userOperations": user_operations,
        "missing": missing,
        "warnings": warnings,
        "marker": marker,
        "eppServiceAccounts": epp_service_accounts,
        "usesStorage": uses_storage,
        "usesLws": uses_lws,
        "usesRouter": uses_router,
        "satelliteStorage": satellite_storage,
        "checkedAt": datetime.now(UTC).isoformat(),
    }


def _sanitize_cluster_label(cluster_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", cluster_id).strip(".-_")[:63].strip(".-_")
    if cleaned and _K8S_LABEL_SEGMENT_RE.fullmatch(cleaned):
        return cleaned
    return hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:16]


def _unique_heredoc_delimiter(base: str, content: str) -> str:
    candidate = base
    suffix = 0
    lines = set(content.splitlines())
    while candidate in lines:
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def _assert_no_secret_leak(script: str) -> None:
    forbidden_patterns = (
        r"(?im)^\s*kind:\s*Secret\b",
        r"\bHF_TOKEN\b",
        r"(?i)\bbearer\s+[A-Za-z0-9._-]{10,}",
        r"(?i)client-key-data\s*:",
    )
    for pattern in forbidden_patterns:
        if re.search(pattern, script):
            raise DeploymentCapabilityValidationError(
                "Rendered Orchestration administrator bootstrap script failed secret-leak safety check",
                code="script_validation_failed",
            )


def _deployment_role_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": DEPLOY_ROLE_NAME,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "lens"},
        },
        "rules": [
            {
                "apiGroups": [""],
                "resources": [
                    "configmaps",
                    "events",
                    "persistentvolumeclaims",
                    "pods",
                    "pods/exec",
                    "pods/log",
                    "pods/portforward",
                    "secrets",
                    "serviceaccounts",
                    "services",
                ],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
            {
                "apiGroups": ["apps", "batch", "policy", "networking.k8s.io", "discovery.k8s.io"],
                "resources": ["*"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
            {
                "apiGroups": ["rbac.authorization.k8s.io"],
                "resources": ["roles", "rolebindings"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
            {
                "apiGroups": [
                    "inference.networking.k8s.io",
                    "inference.networking.x-k8s.io",
                    "gateway.networking.k8s.io",
                    "llm-d.ai",
                    "leaderworkerset.x-k8s.io",
                    "disaggregatedset.x-k8s.io",
                    "resource.k8s.io",
                    "monitoring.coreos.com",
                    "keda.sh",
                    "autoscaling",
                ],
                "resources": ["*"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
        ],
    }


def _deployment_role_binding_manifest(namespace: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(identity["kind"])
    subject: dict[str, Any] = {"kind": kind, "name": str(identity["name"])}
    if kind == "ServiceAccount":
        subject["namespace"] = str(identity["namespace"])
    else:
        subject["apiGroup"] = "rbac.authorization.k8s.io"
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": DEPLOY_ROLE_NAME,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "lens"},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": DEPLOY_ROLE_NAME,
        },
        "subjects": [subject],
    }


def _epp_auth_delegator_crb_manifest(namespace: str, service_accounts: Sequence[str]) -> dict[str, Any]:
    from llm_d_lens.monitoring.deployment.service import AUTH_DELEGATOR_ROLE, _epp_auth_delegator_binding_name

    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": _epp_auth_delegator_binding_name(namespace),
            "labels": {
                "app.kubernetes.io/managed-by": "llm-d-prism",
                "llm-d-prism.io/monitoring": "deployment",
                "llm-d-prism.io/namespace": namespace,
            },
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": AUTH_DELEGATOR_ROLE,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": sa_name, "namespace": namespace} for sa_name in service_accounts
        ],
    }


def render_deployment_bootstrap_script(
    *,
    cluster_id: str,
    namespace: str,
    identity: Mapping[str, Any],
    namespace_labels: Mapping[str, str] | None = None,
    epp_service_accounts: Sequence[str] = (),
    uses_router: bool = True,
    uses_lws: bool = False,
    uses_storage: bool = False,
    satellite_storage: Mapping[str, Any] | None = None,
    prepared_request_id: str | None = None,
    admin_operations: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Render a self-contained, idempotent bash Orchestration administrator bootstrap script for ``namespace``."""
    clean_cluster_id = _single_line_text(cluster_id, "cluster_id")
    validated_ns = validate_deployment_namespace(namespace, cluster_id=clean_cluster_id)
    validated_labels = validate_namespace_labels(namespace_labels)
    if identity.get("overrideRequired") or identity.get("kind") not in {"ServiceAccount", "User", "Group"}:
        raise DeploymentCapabilityValidationError(
            "Kubernetes identity is unknown; provide subjectOverride to render the bootstrap script.",
            code="subject_required",
        )
    validated_identity = validate_subject_override(
        {
            "kind": identity.get("kind"),
            "name": identity.get("name"),
            "namespace": identity.get("namespace"),
        }
    )
    assert validated_identity is not None
    validated_sas = [
        validate_dns1123_label(sa, "epp_service_accounts[]", code="invalid_subject") for sa in epp_service_accounts
    ]
    clean_prepared_req = (
        _single_line_text(prepared_request_id, "prepared_request_id", code="invalid_labels")
        if prepared_request_id
        else ""
    )
    if clean_prepared_req:
        if len(clean_prepared_req) > 63 or not _K8S_LABEL_SEGMENT_RE.fullmatch(clean_prepared_req):
            raise DeploymentCapabilityValidationError(
                "prepared_request_id must be a valid Kubernetes label value (max 63 chars)",
                code="invalid_labels",
            )
        validated_labels.setdefault("lens.io/prepared-request", clean_prepared_req)

    canonical_payload = {
        "clusterId": clean_cluster_id,
        "namespace": validated_ns,
        "namespaceLabels": validated_labels,
        "subject": validated_identity["subject"],
        "eppServiceAccounts": validated_sas,
        "usesRouter": uses_router,
        "usesLws": uses_lws,
        "usesStorage": uses_storage,
        "satelliteVolumeId": (satellite_storage or {}).get("volumeId"),
        "preparedRequest": clean_prepared_req,
    }
    bootstrap_digest = hashlib.sha256(
        json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    ns_manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": validated_ns,
            "labels": {
                "app.kubernetes.io/managed-by": "lens",
                **validated_labels,
            },
        },
    }
    ns_yaml = yaml.safe_dump(ns_manifest, sort_keys=False)
    rbac_yaml = yaml.safe_dump_all(
        [
            _deployment_role_manifest(validated_ns),
            _deployment_role_binding_manifest(validated_ns, validated_identity),
        ],
        sort_keys=False,
    )

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# Lens Orchestration administrator deployment namespace bootstrap script.",
        f"# cluster: {clean_cluster_id}",
        f"# namespace: {validated_ns}",
        f"# bootstrapDigest: {bootstrap_digest}",
        "#",
        "# Run once with an Orchestration administrator kubeconfig. Idempotent and secret-free.",
        "",
        "set -euo pipefail",
        "set -f",
        "umask 077",
        "",
        f"CLUSTER_ID={shlex.quote(clean_cluster_id)}",
        f"CLUSTER_LABEL={shlex.quote(_sanitize_cluster_label(clean_cluster_id))}",
        f"TARGET_NAMESPACE={shlex.quote(validated_ns)}",
        f"BOOTSTRAP_DIGEST={shlex.quote(bootstrap_digest)}",
        f"BOOTSTRAP_DIGEST16={shlex.quote(bootstrap_digest[:16])}",
        f"MARKER_NAME={shlex.quote(BOOTSTRAP_MARKER_NAME)}",
        f"SUBJECT_KIND={shlex.quote(validated_identity['kind'])}",
        f"SUBJECT_NAME={shlex.quote(validated_identity['subject'])}",
        f"EPP_AUTH_DELEGATOR={shlex.quote('applied' if (uses_router and validated_sas) else 'not_required')}",
        f"EPP_SERVICE_ACCOUNTS={shlex.quote(','.join(validated_sas))}",
        f"SATELLITE_STORAGE={shlex.quote('applied' if satellite_storage else 'not_required')}",
        f"PREPARED_REQUEST={shlex.quote(clean_prepared_req)}",
        "",
    ]

    ns_delim = _unique_heredoc_delimiter("LENS_NS_EOF", ns_yaml)
    lines.extend(
        [
            "# Step 1: Ensure dedicated deployment Namespace exists",
            f"kubectl apply -f - <<'{ns_delim}'",
            ns_yaml.rstrip("\n"),
            ns_delim,
            "",
        ]
    )

    rbac_delim = _unique_heredoc_delimiter("LENS_RBAC_EOF", rbac_yaml)
    lines.extend(
        [
            "# Step 2: Grant namespace-scoped deployment RBAC to the Lens kubeconfig identity",
            f"kubectl apply -f - <<'{rbac_delim}'",
            rbac_yaml.rstrip("\n"),
            rbac_delim,
            "",
        ]
    )

    if uses_router and validated_sas:
        epp_yaml = yaml.safe_dump(_epp_auth_delegator_crb_manifest(validated_ns, validated_sas), sort_keys=False)
        epp_delim = _unique_heredoc_delimiter("LENS_EPP_EOF", epp_yaml)
        lines.extend(
            [
                "# Step 3: Authorize EPP ServiceAccount(s) for authenticated /metrics TokenReview/SAR",
                f"kubectl apply -f - <<'{epp_delim}'",
                epp_yaml.rstrip("\n"),
                epp_delim,
                "",
            ]
        )

    if satellite_storage is not None:
        storage_docs = [satellite_storage["pvManifest"], satellite_storage["pvcManifest"]]
        storage_yaml = yaml.safe_dump_all(storage_docs, sort_keys=False)
        storage_delim = _unique_heredoc_delimiter("LENS_STORAGE_EOF", storage_yaml)
        lines.extend(
            [
                "# Step 4: Bind static Model Cache storage PV/PVC in the deployment namespace",
                f"kubectl apply -f - <<'{storage_delim}'",
                storage_yaml.rstrip("\n"),
                storage_delim,
                "",
            ]
        )

    lines.extend(
        [
            "# Step 5: Record non-secret completion marker ConfigMap in the deployment namespace",
            'COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"',
            'kubectl create configmap "$MARKER_NAME" \\',
            '  --namespace "$TARGET_NAMESPACE" \\',
            '  --from-literal="clusterId=$CLUSTER_ID" \\',
            '  --from-literal="namespace=$TARGET_NAMESPACE" \\',
            '  --from-literal="bootstrapDigest=$BOOTSTRAP_DIGEST" \\',
            '  --from-literal="subjectKind=$SUBJECT_KIND" \\',
            '  --from-literal="subjectName=$SUBJECT_NAME" \\',
            '  --from-literal="eppAuthDelegator=$EPP_AUTH_DELEGATOR" \\',
            '  --from-literal="eppServiceAccounts=$EPP_SERVICE_ACCOUNTS" \\',
            '  --from-literal="satelliteStorage=$SATELLITE_STORAGE" \\',
            '  --from-literal="preparedRequest=$PREPARED_REQUEST" \\',
            '  --from-literal="status=completed" \\',
            '  --from-literal="completedAt=$COMPLETED_AT" \\',
            "  --dry-run=client -o yaml | \\",
            "kubectl label --local -f - \\",
            '  "app.kubernetes.io/managed-by=lens" \\',
            '  "lens.llm-d.ai/cluster=$CLUSTER_LABEL" \\',
            '  "lens.llm-d.ai/bootstrap-digest=$BOOTSTRAP_DIGEST16" \\',
            "  --overwrite -o yaml | \\",
            'kubectl apply --namespace "$TARGET_NAMESPACE" -f - >/dev/null',
            "",
            "printf 'Lens deployment namespace %s bootstrapped (digest %s)\\n' "
            '"$TARGET_NAMESPACE" "$BOOTSTRAP_DIGEST"',
            "",
        ]
    )

    script = "\n".join(lines)
    _assert_no_secret_leak(script)
    filename = f"lens-deploy-bootstrap-{validated_ns}.sh"
    return {
        "filename": filename,
        "scriptFilename": filename,
        "script": script,
        "instructions": (
            f"Ask an Orchestration administrator to run this idempotent script once for namespace {validated_ns}, "
            "then click Re-check & Deploy in Lens."
        ),
        "bootstrapDigest": bootstrap_digest,
        "digest": bootstrap_digest,
        "namespace": validated_ns,
        "clusterId": cluster_id,
        "identity": dict(identity),
        "adminOperations": list(admin_operations),
        "marker": {
            "namespace": validated_ns,
            "name": BOOTSTRAP_MARKER_NAME,
            "bootstrapDigest": bootstrap_digest,
        },
        "eppServiceAccounts": validated_sas,
        "warnings": list(warnings),
    }


async def generate_deployment_bootstrap_script(
    request: DeploymentPermissionCheckRequest,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Run SSAR preflight and render the Orchestration administrator bootstrap script for the target namespace."""
    report = await check_deployment_permissions(request, runner=runner)
    prepared_req = request.prepared_request_id or request.prepared_plan_id
    return render_deployment_bootstrap_script(
        cluster_id=report["clusterId"],
        namespace=report["namespace"],
        identity=report["identity"],
        namespace_labels=report.get("namespaceLabels"),
        epp_service_accounts=report.get("eppServiceAccounts") or (),
        uses_router=bool(report.get("usesRouter")),
        uses_lws=bool(report.get("usesLws")),
        uses_storage=bool(report.get("usesStorage")),
        satellite_storage=report.get("satelliteStorage"),
        prepared_request_id=prepared_req,
        admin_operations=[item["id"] for item in report["adminOperations"]],
        warnings=report["warnings"],
    )


def _prune_verified_bindings_locked(now: float) -> None:
    expired = [key for key, (_ns, expires_at) in _VERIFIED_NAMESPACES.items() if expires_at <= now]
    for key in expired:
        _VERIFIED_NAMESPACES.pop(key, None)


def record_verified_namespace(
    cluster_id: str,
    namespace: str,
    *,
    owner_user_id: str | None = None,
    agentic_deployment_run_id: str | None = None,
    deployment_name: str | None = None,
    prepared_plan_id: str | None = None,
    ttl_seconds: float = _VERIFIED_BINDING_TTL_SECONDS,
) -> None:
    """Record a verified target namespace for consumption on run start."""
    validated_ns = validate_deployment_namespace(namespace, cluster_id=cluster_id)
    owner_scope = owner_user_id or ""
    expires_at = time.monotonic() + max(ttl_seconds, 1.0)
    with _VERIFIED_BINDINGS_LOCK:
        _prune_verified_bindings_locked(time.monotonic())
        if agentic_deployment_run_id:
            _VERIFIED_NAMESPACES[(owner_scope, cluster_id, "agentic_run", agentic_deployment_run_id)] = (
                validated_ns,
                expires_at,
            )
        if prepared_plan_id:
            _VERIFIED_NAMESPACES[(owner_scope, cluster_id, "prepared_plan", prepared_plan_id)] = (
                validated_ns,
                expires_at,
            )
        if deployment_name:
            _VERIFIED_NAMESPACES[(owner_scope, cluster_id, "deployment_name", deployment_name)] = (
                validated_ns,
                expires_at,
            )


def consume_verified_namespace(
    cluster_id: str,
    *,
    owner_user_id: str | None = None,
    agentic_deployment_run_id: str | None = None,
    deployment_name: str | None = None,
    prepared_plan_id: str | None = None,
) -> str | None:
    """Pop and return the verified target namespace for a single-case run start."""
    if not cluster_id:
        return None
    owner_scope = owner_user_id or ""
    keys: list[tuple[str, str, str, str]] = []
    if agentic_deployment_run_id:
        keys.append((owner_scope, cluster_id, "agentic_run", agentic_deployment_run_id))
    if prepared_plan_id:
        keys.append((owner_scope, cluster_id, "prepared_plan", prepared_plan_id))
    if deployment_name:
        keys.append((owner_scope, cluster_id, "deployment_name", deployment_name))
    if not keys:
        return None
    now = time.monotonic()
    with _VERIFIED_BINDINGS_LOCK:
        _prune_verified_bindings_locked(now)
        matched_ns: str | None = None
        for key in keys:
            entry = _VERIFIED_NAMESPACES.pop(key, None)
            if entry is not None and matched_ns is None:
                matched_ns = entry[0]
        if matched_ns is not None:
            for key in keys:
                entry = _VERIFIED_NAMESPACES.get(key)
                if entry is not None and entry[0] == matched_ns:
                    _VERIFIED_NAMESPACES.pop(key, None)
        return matched_ns


def clear_verified_namespaces() -> None:
    """Clear in-memory verified namespace bindings (for unit tests)."""
    with _VERIFIED_BINDINGS_LOCK:
        _VERIFIED_NAMESPACES.clear()


async def verify_deployment_bootstrap(
    request: DeploymentBootstrapVerifyRequest,
    *,
    owner_user_id: str | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Re-check live SSAR and completion marker after Orchestration administrator bootstrap."""
    _single_line_text(request.cluster_id, "cluster_id")
    cleaned_expected: str | None = None
    if request.expected_digest is not None and request.expected_digest.strip():
        cleaned_expected = _single_line_text(request.expected_digest, "expectedDigest", code="invalid_digest")
        if not _DIGEST_RE.fullmatch(cleaned_expected):
            raise DeploymentCapabilityValidationError(
                "expectedDigest must be a valid 64-char SHA-256 hex digest", code="invalid_digest"
            )

    report = await check_deployment_permissions(request, runner=runner)
    marker = dict(report["marker"])
    warnings = list(report["warnings"])
    remaining = list(report["adminOperations"])

    stale_marker = marker.get("status") == "stale"
    if cleaned_expected is not None and marker.get("present"):
        observed_digest = marker.get("bootstrapDigest")
        if not isinstance(observed_digest, str) or observed_digest != cleaned_expected:
            stale_marker = True
            marker["status"] = "stale"
            marker["verified"] = False
            warnings.append(
                "The cluster's bootstrap marker digest does not match the current deployment specification; "
                "re-generate and run the latest Orchestration administrator bootstrap script."
            )

    marker_ok = True
    if cleaned_expected is not None:
        marker_ok = bool(marker.get("present")) and not stale_marker
    elif marker.get("status") == "unconfirmed":
        marker_ok = bool(report["sufficient"])

    verified = bool(report["sufficient"]) and marker_ok and not stale_marker
    if verified:
        record_verified_namespace(
            report["clusterId"],
            report["namespace"],
            owner_user_id=owner_user_id,
            agentic_deployment_run_id=request.agentic_deployment_run_id,
            deployment_name=request.deployment_name,
            prepared_plan_id=request.prepared_plan_id,
        )
        status = "verified"
        message = f"Deployment namespace {report['namespace']} is verified and ready."
    elif stale_marker:
        status = "stale"
        message = f"Bootstrap marker for namespace {report['namespace']} is stale."
    else:
        status = "pending"
        message = (
            f"Namespace {report['namespace']} still requires Orchestration administrator bootstrap before deployment."
        )

    return {
        "verified": verified,
        "sufficient": bool(report["sufficient"]),
        "status": status,
        "clusterId": report["clusterId"],
        "namespace": report["namespace"],
        "identity": report["identity"],
        "marker": marker,
        "remainingOperations": remaining,
        "missing": report["missing"],
        "warnings": warnings,
        "message": message,
    }
