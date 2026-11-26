"""Deployment data-plane selection and compatible Router transport.

A deployment's data plane decides how a benchmark or a model service reaches it:

- ``shared_gateway``: the cluster's shared Gateway is the data plane; the
  deployment runs no proxy and the EPP is reached as its ext_proc.
- ``standalone_router``: the deployment's router-chart proxy fronts the EPP.
- ``direct``: no router; the model server Service is called directly.
- ``external``: the provider owns its data plane; Lens renders no router.

Guides declare a static ``data_plane_kind`` (``llm-d-router`` for guides built on
the llm-d router chart, ``direct`` for a plain vLLM Service, ``external`` for
provider-owned resources). New router deployments keep their standalone proxy.
Publishing adds a shared-Gateway route without changing their independent entry
point. Recorded shared_gateway deployments retain their legacy behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

#: The concrete data plane assigned to a deployment.
DataPlane = Literal["shared_gateway", "standalone_router", "direct", "external"]
#: The static data plane a guide declares.
DataPlaneKind = Literal["llm-d-router", "direct", "external"]

#: Helm values that switch the llm-d router chart from its own proxy to the
#: cluster's shared Gateway. Shared by every ``llm-d-router`` guide.
GATEWAY_MODE_VALUES_PATH = (
    Path(__file__).resolve().parent / "providers" / "router_values" / "router-gateway-mode.values.yaml"
)

#: Base transport values for plaintext providers. Final provider-specific
#: overrides select TLS when required and configure both EPP and its proxy.
PLAINTEXT_EPP_VALUES_PATH = (
    Path(__file__).resolve().parent / "providers" / "router_values" / "router-plaintext-epp.values.yaml"
)


def _resolved_data_plane(execution_context: Mapping[str, Any] | None) -> DataPlane:
    """Use the recorded data plane, defaulting to an independent router."""
    data_plane = _data_plane_from_context(execution_context)
    if data_plane is not None:
        return data_plane
    return "standalone_router"


def evaluation_owned(provenance: Mapping[str, Any] | None) -> bool:
    """True for ephemeral deployments an evaluation created for benchmarking.

    Those always keep their own router proxy (``standalone_router``) so several
    same-model deployments can be benchmarked without colliding on the shared
    Gateway's single base-model route key.
    """
    provenance = provenance or {}
    return bool(
        provenance.get("evaluate_workflow") or provenance.get("evaluation_id") or provenance.get("evaluation_case_id")
    )


def declared_data_plane_kind(execution: object) -> DataPlaneKind:
    """The ``data_plane_kind`` a deployment's provider declared.

    Falls back to legacy contracts that only carry ``shares_gateway`` (and, older
    still, the ``optimized-baseline`` provider ref, the only guide that ever
    disabled its own proxy).
    """
    artifact = getattr(execution, "artifact", None)
    payload = getattr(artifact, "rendered_payload", None)
    value = getattr(payload, "value", None) or {}
    contract = value.get("deployment_contract") or {}
    kind = contract.get("data_plane_kind")
    if kind in {"llm-d-router", "direct", "external"}:
        return kind
    if contract.get("shares_gateway"):
        return "llm-d-router"
    if value.get("provider_ref") == "optimized-baseline":
        return "llm-d-router"
    return "external"


def deployment_uses_shared_gateway(execution: object) -> bool:
    """True when a benchmark reaches this deployment through the shared Gateway.

    Evaluation-owned deployments keep their own proxy even for ``llm-d-router``
    guides, so they are always reached directly -- several same-model
    deployments created for a multi-deployment comparison would otherwise
    collide on the shared Gateway's single base-model route key. This is the
    Design Configuration path: it intentionally keeps the deployment's own
    router-chart proxy rather than the shared Gateway, even when the
    deployment also happens to be published as a Model Service elsewhere (that
    separate "existing endpoint" path is the one that goes through the shared
    Gateway, with its own access-token flow).

    Prefers the execution's recorded ``data_plane`` (the concrete data plane
    ``resolve_data_plane`` actually rendered at deploy time -- e.g. a
    ``llm-d-router`` deployment that fell back to ``standalone_router``
    because the cluster had no ready shared Gateway yet). Falls back to the
    guide's static ``data_plane_kind`` only for executions recorded before
    that field existed, where every ``llm-d-router`` guide did render the
    shared Gateway values.
    """
    provenance = getattr(execution, "provenance", {}) or {}
    if evaluation_owned(provenance):
        return False
    recorded = getattr(execution, "data_plane", None)
    if recorded is not None:
        return recorded == "shared_gateway"
    return declared_data_plane_kind(execution) == "llm-d-router"


async def resolve_data_plane(
    kind: DataPlaneKind,
    *,
    provenance: Mapping[str, Any] | None,
    cluster_id: str | None,
) -> tuple[DataPlane, str | None]:
    """Keep every new router deployment independent of Gateway publication."""
    if kind == "direct":
        return "direct", None
    if kind != "llm-d-router":
        return "external", None
    return "standalone_router", None


def _data_plane_from_context(execution_context: Mapping[str, Any] | None) -> DataPlane | None:
    value = (execution_context or {}).get("data_plane")
    return value if value in {"shared_gateway", "standalone_router", "direct", "external"} else None


def router_data_plane_args(execution_context: Mapping[str, Any] | None) -> list[str]:
    """Extra Helm ``--values`` args for the llm-d router chart.

    Configure EPP and its proxy using the cluster provider's transport. Only
    an explicitly recorded ``shared_gateway`` deployment disables its proxy.
    """
    data_plane = _resolved_data_plane(execution_context)
    if data_plane in {"direct", "external"}:
        return []
    args = ["--values", str(PLAINTEXT_EPP_VALUES_PATH)]
    if data_plane == "shared_gateway":
        args += ["--values", str(GATEWAY_MODE_VALUES_PATH)]
    for key, value in router_transport_settings(execution_context).items():
        args += ["--set", f"{key}={str(value).lower()}"]
    return args


def router_transport_settings(execution_context: Mapping[str, Any] | None) -> dict[str, bool]:
    """Render both sides of ext_proc from the same provider transport policy."""
    from llm_d_lens.model_service.gateway_providers import epp_secure_serving

    context = execution_context or {}
    return {
        "router.epp.flags.secure-serving": epp_secure_serving(
            cluster_id=(context.get("provenance") or {}).get("cluster_server_id"),
            provider=context.get("gateway_provider"),
        ),
        "router.proxy.enabled": _resolved_data_plane(context) != "shared_gateway",
    }


def router_data_plane_effective_values(content: str, execution_context: Mapping[str, Any] | None) -> str:
    """Merge the data-plane values into saved effective router values.

    The saved-bundle install path renders one effective values file, so the
    values are merged into it rather than passed as separate ``--values``. The
    provider's EPP transport and proxy lifecycle are identical in both paths
    (see :func:`router_data_plane_args`).
    """
    data_plane = _resolved_data_plane(execution_context)
    if data_plane in {"direct", "external"}:
        return content
    base_values = yaml.safe_load(content) or {}
    plaintext_values = yaml.safe_load(PLAINTEXT_EPP_VALUES_PATH.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(base_values, plaintext_values)
    if data_plane == "shared_gateway":
        gateway_values = yaml.safe_load(GATEWAY_MODE_VALUES_PATH.read_text(encoding="utf-8")) or {}
        merged = _deep_merge(merged, gateway_values)
    settings = router_transport_settings(execution_context)
    merged["router"]["epp"]["flags"]["secure-serving"] = settings["router.epp.flags.secure-serving"]
    merged["router"].setdefault("proxy", {})["enabled"] = settings["router.proxy.enabled"]
    return yaml.safe_dump(merged, sort_keys=False)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
