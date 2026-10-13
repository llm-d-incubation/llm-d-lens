"""Resolve hardware profiles by the identity each layer actually has.

Core code asks these functions instead of branching on vendor names: a DRA
device class, an extended-resource key, a node label, an upstream variant, or
an accelerator key. Explicit use of an unregistered identity raises rather
than silently guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from .errors import HardwareProfileNotFoundError
from .models import HardwareProfile
from .registry import all_profiles


def _first(predicate: Callable[[HardwareProfile], bool]) -> HardwareProfile | None:
    for profile in all_profiles():
        if predicate(profile):
            return profile
    return None


def resolve_by_device_class(device_class: str) -> HardwareProfile | None:
    if not device_class:
        return None
    return _first(lambda profile: device_class in profile.device_classes)


def resolve_by_resource(resource_key: str) -> HardwareProfile | None:
    """Resolve an extended-resource key (e.g. ``nvidia.com/gpu``) by prefix."""
    if not resource_key:
        return None
    key = resource_key.strip()

    def matches(profile: HardwareProfile) -> bool:
        for prefix in profile.resource_prefixes:
            base = prefix.rstrip("/")
            if key in {prefix, base} or key.startswith(prefix) or key.startswith(f"{base}/"):
                return True
        return False

    return _first(matches)


def resolve_by_node_label(labels: Mapping[str, str]) -> HardwareProfile | None:
    if not labels:
        return None

    def matches(profile: HardwareProfile) -> bool:
        selector = profile.node_label_selector
        return bool(selector) and all(labels.get(key) == value for key, value in selector.items())

    return _first(matches)


def resolve_by_upstream_variant(variant: str, vendor: str | None = None) -> HardwareProfile | None:
    if not variant:
        return None

    def matches(profile: HardwareProfile) -> bool:
        if profile.upstream_variant != variant:
            return False
        return not (vendor and profile.upstream_vendor and profile.upstream_vendor != vendor)

    return _first(matches)


def resolve_by_accelerator_key(key: str) -> HardwareProfile | None:
    if not key:
        return None
    return _first(lambda profile: key in profile.accelerator_aliases)


def resolve_by_aic_system(system_name: str) -> HardwareProfile | None:
    if not system_name:
        return None

    def matches(profile: HardwareProfile) -> bool:
        return any(
            pattern and re.search(pattern, system_name, re.IGNORECASE)
            for pattern in profile.planning.aic_system_patterns
        )

    return _first(matches)


def require_accelerator(key: str) -> HardwareProfile:
    """Return the profile for an explicitly requested accelerator, or raise."""
    profile = resolve_by_accelerator_key(key)
    if profile is None:
        raise HardwareProfileNotFoundError(f"no registered hardware profile supports accelerator {key!r}")
    return profile


def resolve_configuration_profile(content: Mapping) -> HardwareProfile | None:
    """Resolve saved deployment identity, with manifest evidence for older records."""
    import yaml

    source = (content.get("officialGuide") or {}).get("source") or {}
    for value in (content.get("hardware_profile"), source.get("accelerator"), content.get("accelerator")):
        if value:
            return require_accelerator(str(value))
    found = {}
    manifest = (content.get("officialGuide") or {}).get("renderedManifest") or ""
    for document in yaml.safe_load_all(manifest):

        def visit(value):
            if isinstance(value, dict):
                profile = resolve_by_device_class(str(value.get("deviceClassName") or ""))
                if profile:
                    found[profile.id] = profile
                for key, child in value.items():
                    profile = resolve_by_resource(str(key))
                    if profile and not any(str(key).endswith(s) for s in profile.monitor_resource_suffixes):
                        found[profile.id] = profile
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)
    if len(found) == 1:
        return next(iter(found.values()))
    if len(found) > 1:
        raise ValueError("Deployment contains multiple hardware profiles; select hardware explicitly")
    image = str((content.get("runtime") or {}).get("image") or "")
    repository = image.split("@", 1)[0].rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image.split("@", 1)[0]
    return _first(lambda profile: repository in profile.deployment.managed_image_repositories) if repository else None


def configuration_resource_request(content: Mapping, profile: HardwareProfile) -> dict:
    """Preserve the source deployment's access mode when deriving a baseline."""
    import yaml

    explicit = content.get("hardware_request")
    if isinstance(explicit, dict):
        mode = deployment_mode(profile, explicit.get("access_mode"), request_model=explicit.get("request_model"))
        resource = explicit.get("resource_name")
        if resource and resource not in mode.get("resource_names", []):
            raise ValueError("Hardware resource does not belong to the selected access mode")
        return {**mode, **explicit}
    manifest = (content.get("officialGuide") or {}).get("renderedManifest") or ""
    requests = set()

    def visit(value):
        if isinstance(value, dict):
            if value.get("deviceClassName") in profile.device_classes:
                requests.add(("dra", ""))
            for key, child in value.items():
                if resolve_by_resource(key) == profile and not any(
                    key.endswith(suffix) for suffix in profile.monitor_resource_suffixes
                ):
                    requests.add(("extended-resource", key))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for document in yaml.safe_load_all(manifest):
        visit(document)
    if len(requests) > 1:
        raise ValueError("Baseline hardware request is ambiguous")
    mode, resource = next(iter(requests), (profile.request_model, profile.deployment.resource_name))
    return {**deployment_mode(profile, request_model=mode), "resource_name": resource}


def deployment_mode(
    profile: HardwareProfile, access_mode: str | None = None, *, request_model: str | None = None
) -> dict:
    """Resolve one declared access mode without maintaining separate vendor defaults."""
    modes = profile.deployment.modes
    if access_mode:
        if access_mode not in modes:
            raise ValueError(f"Hardware {profile.id} does not configure deployment mode {access_mode}")
        result = dict(modes[access_mode], access_mode=access_mode)
        if request_model and result["request_model"] != request_model:
            raise ValueError("Hardware access mode does not match the resource request")
        return result
    target = request_model or profile.request_model
    candidates = [dict(value, access_mode=key) for key, value in modes.items() if value.get("request_model") == target]
    if len(candidates) > 1:
        raise ValueError("Hardware request requires an explicit access mode")
    if candidates:
        return candidates[0]
    if modes:
        raise ValueError(f"Hardware {profile.id} has no {target} deployment mode")
    return {
        "request_model": target,
        "device_class": profile.deployment.device_class,
        "claim_request_name": profile.deployment.claim_request_name,
        "resource_names": [profile.deployment.resource_name] if profile.deployment.resource_name else [],
    }
