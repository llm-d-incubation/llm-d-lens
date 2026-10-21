"""Hardware identity for rendered deployment overlays.

These helpers read device class, claim request name and overlay variant from
the registered hardware profile instead of hardcoding them. Callers pass the
specific run's accelerator, so concurrent deployments for different hardware
never share selection state. PRISM_DEPLOY_ACCELERATOR is consulted only when
an explicit accelerator is omitted; unresolved hardware stays neutral.
"""

from __future__ import annotations

import os
import re
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


def validate_runtime_class_policy(policy: dict) -> None:
    mode = policy.get("mode", "auto")
    name = policy.get("name")
    if mode not in {"auto", "node-default", "named"} or set(policy) - {"mode", "name"}:
        raise ValueError("Invalid RuntimeClass policy")
    if mode == "named":
        if (
            not isinstance(name, str)
            or len(name) > 253
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split("."))
        ):
            raise ValueError("RuntimeClass name must be a valid DNS subdomain")
    elif name is not None:
        raise ValueError("RuntimeClass name is only allowed in named mode")


def select_runtime_class(policy: dict, classes: list[dict], *, suggested: str | None = None) -> str | None:
    validate_runtime_class_policy(policy)
    mode = policy.get("mode", "auto")
    name = policy.get("name")
    available = {item.get("metadata", {}).get("name") for item in classes}
    if mode == "named":
        if name not in available:
            raise ValueError(f"RuntimeClass {name!r} is not available in the target cluster")
        return name
    return suggested if mode == "auto" and suggested in available else None


async def discover_runtime_classes(cluster_id: str) -> dict:
    import json

    from llm_d_lens.infrastructure.kubernetes import kubeconfig_environment, scoped_runner

    if not cluster_id:
        raise ValueError("RuntimeClass discovery requires an explicit target cluster")
    try:
        kubeconfig_environment(cluster_id, strict=True)
    except Exception as error:
        raise ValueError("RuntimeClass discovery requires a resolvable target cluster") from error
    result = {}
    for key, resource in (("items", "runtimeclasses.node.k8s.io"), ("nodes", "nodes")):
        try:
            response = await scoped_runner(cluster_id).run(["kubectl", "get", resource, "-o", "json"])
        except Exception as error:
            raise ValueError(
                f"Unable to discover {resource} in the target cluster; check connectivity and read permissions"
            ) from error
        if response.returncode:
            raise ValueError(
                f"Unable to discover {resource} in the target cluster; check connectivity and read permissions"
            )
        try:
            payload = json.loads(response.stdout)
            items = payload["items"]
            if not isinstance(items, list):
                raise ValueError("items is not a list")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid discovery response for {resource}") from error
        result[key] = items
    return result


def prepare_runtime_class_manifest(
    manifest: str, policy: dict, capabilities: dict, *, accelerator: str | None = None
) -> tuple[str, dict]:
    import yaml

    from llm_d_lens.deploy.providers.shared_memory import _model_container, _pod_specs
    from llm_d_lens.deploy.providers.target_node import configured_target_node

    validate_runtime_class_policy(policy)
    classes = capabilities.get("items", [])
    nodes = capabilities.get("nodes", [])
    report = {
        "policy": policy,
        "workloads": [],
        "warnings": [
            "RuntimeClass availability does not establish whether it is required or whether node handlers, "
            "CDI, drivers and device injection are ready. Node default runtime readiness is unknown. "
            "Placement validation checks node selectors and names, not complete scheduler feasibility."
        ],
    }
    documents = list(yaml.safe_load_all(manifest))
    changed = False

    def visit(document):
        nonlocal changed
        if not isinstance(document, dict):
            return
        if document.get("kind") == "List":
            for item in document.get("items") or []:
                visit(item)
            return
        for pod in _pod_specs(document):
            if not any(_model_container(container) for container in pod.get("containers", [])):
                continue
            existing = pod.get("runtimeClassName")
            suggested = None
            selection = policy
            if policy.get("mode", "auto") == "auto":
                if existing:
                    selection = {"mode": "named", "name": existing}
                else:
                    suggested_pod = {**pod}
                    apply_runtime_class(suggested_pod, accelerator=accelerator, runtime_classes=classes)
                    suggested = suggested_pod.get("runtimeClassName")
            selected = select_runtime_class(selection, classes, suggested=suggested)
            if selected:
                runtime = next(item for item in classes if item.get("metadata", {}).get("name") == selected)
                selector = (runtime.get("scheduling") or {}).get("nodeSelector") or {}
                pod_selector = pod.get("nodeSelector") or {}
                if any(key in pod_selector and pod_selector[key] != value for key, value in selector.items()):
                    raise ValueError(f"RuntimeClass {selected!r} conflicts with the workload node selector")
                combined = {**pod_selector, **selector}
                target_node = pod.get("nodeName") or configured_target_node()
                if not any(
                    all(node.get("metadata", {}).get("labels", {}).get(key) == value for key, value in combined.items())
                    and (not target_node or node.get("metadata", {}).get("name") == target_node)
                    for node in nodes
                ):
                    raise ValueError(f"RuntimeClass {selected!r} has no nodes matching the workload placement")
            if selected != existing:
                changed = True
                if selected:
                    pod["runtimeClassName"] = selected
                else:
                    pod.pop("runtimeClassName", None)
            report["workloads"].append(
                {
                    "kind": document.get("kind"),
                    "name": document.get("metadata", {}).get("name"),
                    "runtimeClassName": selected,
                }
            )

    for document in documents:
        visit(document)
    return (yaml.safe_dump_all(documents, sort_keys=False) if changed else manifest), report


def apply_runtime_class(
    pod_spec: dict,
    *,
    accelerator: str | None = None,
    access_mode: str | None = None,
    runtime_classes: list[dict] | None = None,
) -> None:
    profile = active_profile(accelerator)
    if profile is None or pod_spec.get("runtimeClassName") or runtime_classes is None:
        return
    container = next((item for item in pod_spec.get("containers", []) if item.get("name") == "modelserver"), None)
    if container is None:
        return
    resources = container.get("resources") or {}
    resource_keys = {key for section in ("limits", "requests") for key in resources.get(section, {})}
    declared_resources = {name for mode in profile.deployment.modes.values() for name in mode.get("resource_names", [])}
    if profile.deployment.resource_name:
        declared_resources.add(profile.deployment.resource_name)
    if resource_keys & declared_resources:
        model = "extended-resource"
    elif resources.get("claims"):
        model = "dra"
    else:
        return
    mode = deployment_mode(profile, access_mode, request_model=model)
    if mode.get("runtime_class_name") and any(
        item.get("metadata", {}).get("name") == mode["runtime_class_name"] for item in runtime_classes
    ):
        pod_spec["runtimeClassName"] = mode["runtime_class_name"]


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
