"""Hardware identity for rendered deployment overlays.

These helpers read device class, claim request name and overlay variant from
the registered hardware profile instead of hardcoding them. Callers pass the
specific run's accelerator, so concurrent deployments for different hardware
never share selection state. PRISM_DEPLOY_ACCELERATOR is consulted only when
an explicit accelerator is omitted; unresolved hardware stays neutral.
"""

from __future__ import annotations

import os
from pathlib import Path

from llm_d_lens.hardware.models import HardwareProfile
from llm_d_lens.hardware.registry import all_profiles
from llm_d_lens.hardware.resolver import deployment_mode, resolve_by_accelerator_key

DEFAULT_DEVICE_CLASS = ""
DEFAULT_CLAIM_REQUEST_NAME = ""
DEFAULT_OVERLAY_VARIANT = ""
DEFAULT_REQUEST_MODEL = ""
DEFAULT_RUNTIME_IMAGE = ""


def active_profile(accelerator: str | None = None) -> HardwareProfile | None:
    """The hardware profile to render for, honoring an explicit per-run accelerator."""
    key = (accelerator or os.environ.get("PRISM_DEPLOY_ACCELERATOR") or "").strip().lower()
    return resolve_by_accelerator_key(key) if key else None


def cluster_accelerator(hardware: dict) -> str:
    """Select the single detected accelerator profile for a deployment."""
    profiles = {
        item.get("id")
        for item in hardware.get("accelerators") or []
        if item.get("gpuCount", 0) > 0 and isinstance(item.get("id"), str)
    }
    if len(profiles) != 1:
        raise ValueError("cluster must report exactly one accelerator profile for deployment")
    accelerator = profiles.pop()
    if not runtime_image(accelerator=accelerator):
        raise ValueError(f"cluster accelerator {accelerator} has no configured runtime image")
    return accelerator


def device_class(fallback: str = "", *, accelerator: str | None = None, access_mode: str | None = None) -> str:
    profile = active_profile(accelerator)
    return str(deployment_mode(profile, access_mode).get("device_class") or "") if profile else fallback


def claim_request_name(fallback: str = "", *, accelerator: str | None = None, access_mode: str | None = None) -> str:
    profile = active_profile(accelerator)
    return str(deployment_mode(profile, access_mode).get("claim_request_name") or "") if profile else fallback


def overlay_variant(fallback: str = "", *, accelerator: str | None = None) -> str:
    profile = active_profile(accelerator)
    return profile.upstream_variant if profile else fallback


DEFAULT_ROUTER_TOPOLOGY = "single-host"


def router_topology(fallback: str = DEFAULT_ROUTER_TOPOLOGY, *, accelerator: str | None = None) -> str:
    """Topology directory some guides publish per-topology router values under.

    Reads ``deployment.router_topology`` from the active hardware profile
    instead of hardcoding a single path, so a future multi-host profile (e.g.
    a multi-chip TPU LeaderWorkerSet) only needs to set this field, never a
    code change in the guide's path-building logic.
    """
    profile = active_profile(accelerator)
    return (profile.deployment.router_topology if profile else "") or fallback


def accelerator_supported(key: str | None) -> bool:
    """True when a registered hardware profile supports this accelerator key."""
    return bool(key and resolve_by_accelerator_key(str(key)))


def request_model(fallback: str = "", *, accelerator: str | None = None, access_mode: str | None = None) -> str:
    """How the selected profile mode requests accelerators: DRA or extended resources."""
    profile = active_profile(accelerator)
    if profile is None:
        return fallback
    return str(deployment_mode(profile, access_mode)["request_model"])


def requires_dra_claim(*, accelerator: str | None = None) -> bool:
    return request_model(accelerator=accelerator) == "dra"


def resource_name(
    fallback: str | None = None, *, accelerator: str | None = None, access_mode: str | None = None
) -> str | None:
    """Return the selected mode's resource key when it has one unambiguous choice."""
    profile = active_profile(accelerator)
    if profile is None:
        return fallback
    names = deployment_mode(profile, access_mode).get("resource_names", [])
    return names[0] if len(names) == 1 else None


def runtime_image(fallback: str | None = None, *, accelerator: str | None = None) -> str:
    """The active hardware profile's model-server image (repository and version).

    The profile supplies the full image. If it has no image or cannot be resolved,
    use the caller's fallback, or stay neutral when no fallback was supplied.
    """
    profile = active_profile(accelerator)
    return (profile.deployment.runtime_image if profile else None) or fallback or ""


def _image_repository(image: str) -> str:
    """Strip any tag/digest so two references to the same repository compare equal."""
    image = image.split("@", 1)[0]
    return image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image


def pin_runtime_image(image: str, *, accelerator: str | None = None) -> str:
    """Normalize a managed model-server image using profile-owned repositories.

    An explicit accelerator selects the target profile; otherwise use the image's
    owning profile. Custom images pass through unchanged.
    """
    repository = _image_repository(image)
    owners = [
        p
        for p in all_profiles()
        if repository in {*p.deployment.managed_image_repositories, _image_repository(p.deployment.runtime_image or "")}
    ]
    profile = active_profile(accelerator) if accelerator else (owners[0] if owners else None)
    return profile.deployment.runtime_image if owners and profile and profile.deployment.runtime_image else image


def guide_overlays(
    root: Path, guide: str, *, accelerator: str | None = None, model_server: str = "vllm"
) -> dict[str, Path]:
    """Discover actual Kustomization entry points under the profile's source root."""
    profile = active_profile(accelerator)
    if profile is None:
        return {}
    base = root / profile.deployment.overlay_root.format(
        guide=guide, variant=profile.upstream_variant, model_server=model_server
    )
    paths = {
        p.parent for name in ("kustomization.yaml", "kustomization.yml", "Kustomization") for p in base.rglob(name)
    }
    return {str(p.relative_to(base)): p for p in sorted(paths) if p.resolve().is_relative_to(root.resolve())}


def set_accelerator_request(
    container: dict, claim: dict | None, count: int, *, accelerator: str | None = None, access_mode: str | None = None
) -> None:
    """Set a tensor-parallel accelerator count on a rendered pod.

    Update matching accelerator requests in a DRA ResourceClaimTemplate, or the
    selected mode's extended resource in container limits and requests. Preserve
    unrelated requests, including NIC claims.
    """
    profile = active_profile(accelerator)
    if profile is None:
        raise ValueError("Deployment hardware profile is unresolved")
    if claim is not None:
        requests = claim["spec"]["spec"]["devices"]["requests"]
        matched = [
            r.setdefault("exactly", {})
            for r in requests
            if r.get("exactly", {}).get("deviceClassName") in profile.device_classes
        ]
        if not matched:
            raise ValueError("No accelerator request matches the deployment hardware")
        for request in matched:
            request["count"] = count
        return
    mode = deployment_mode(profile, access_mode, request_model="extended-resource")
    resources = container.setdefault("resources", {})
    names = set(mode.get("resource_names", []))
    existing = {name for group in ("limits", "requests") for name in resources.get(group, {}) if name in names}
    if len(existing) > 1:
        raise ValueError("Multiple accelerator resource names require an explicit selection")
    name = next(iter(existing)) if existing else next(iter(names)) if len(names) == 1 else None
    if not name:
        raise ValueError("Hardware requires an explicit extended resource or a DRA claim")
    for group in ("limits", "requests"):
        resources.setdefault(group, {})[name] = str(count)


def default_guide_variant(variants, *, guide: str = "Guide", accelerator: str | None = None) -> str:
    """Choose only an entry point present in the selected source."""
    for name in (".", "base"):
        if name in variants:
            return name
    available = ", ".join(sorted(variants)) or "none"
    raise ValueError(
        f"{guide} has no generic Kustomize entry point for accelerator {accelerator or 'unresolved'}: "
        f"expected '.' or 'base'; available variants: {available}. "
        "Select a compatible Guide source or explicitly choose a supported variant; "
        "platform-specific variants are not selected automatically."
    )
