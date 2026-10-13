"""Adapt saved P/D plugin settings to the selected Router runtime."""

from __future__ import annotations

import re
from copy import deepcopy

import yaml


def compatible_router_values(values: dict, chart_version: str) -> dict:
    """Return Router 0.11 values without modifying the original source snapshot."""
    result = deepcopy(values)
    epp = result.get("router", {}).get("epp", {})
    image = epp.get("image") or {}
    if (
        not isinstance(image, dict)
        or image.get("repository", "llm-d-router-endpoint-picker") != "llm-d-router-endpoint-picker"
    ):
        return result
    version = str(image.get("tag") or chart_version)
    if not re.fullmatch(r"v?0\.11\.\d+", version):
        return result
    key = epp.get("pluginsConfigFile")
    custom = epp.get("pluginsCustomConfig") or {}
    if not key or not isinstance(custom.get(key), str):
        return result
    document = yaml.safe_load(custom[key])
    if not isinstance(document, dict) or not isinstance(document.get("plugins"), list):
        raise ValueError("Router plugin configuration requires a plugins list")
    plugins = document["plugins"]
    handlers = [p for p in plugins if p.get("type") == "disagg-profile-handler"]
    headers = [p for p in plugins if p.get("type") == "disagg-headers-handler"]
    if headers and (len(handlers) != 1 or any(p.get("parameters") for p in headers)):
        raise ValueError("Legacy disaggregation headers require one profile handler and no custom header parameters")
    changed = bool(headers)
    for handler in handlers:
        parameters = handler.get("parameters") or {}
        if "deciderPluginName" not in parameters:
            continue
        legacy = parameters.pop("deciderPluginName")
        deciders = parameters.setdefault("deciders", {})
        if "prefill" in deciders and deciders["prefill"] != legacy:
            raise ValueError("Conflicting legacy and current P/D deciders")
        deciders["prefill"] = legacy
        handler["parameters"] = parameters
        changed = True
    if changed:
        # Router 0.11's profile handler also implements the former header hook.
        document["plugins"] = [p for p in plugins if p.get("type") != "disagg-headers-handler"]
        custom[key] = yaml.safe_dump(document, sort_keys=False)
    return result
