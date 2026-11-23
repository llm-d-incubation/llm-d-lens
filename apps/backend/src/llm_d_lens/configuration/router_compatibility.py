"""Adapt saved P/D plugin settings to the selected Router runtime."""

from __future__ import annotations

import re
from copy import deepcopy

import yaml


def compose_prefix_routing(values: dict, donor: dict | None, enabled: bool) -> dict:
    """Compose source-established affinity plugins without replacing P/D profiles."""
    result = deepcopy(values)

    def document(source):
        epp = source.get("router", {}).get("epp", {})
        key = epp.get("pluginsConfigFile")
        custom = epp.get("pluginsCustomConfig") or {}
        config = yaml.safe_load(custom.get(key, ""))
        if not isinstance(config, dict) or not isinstance(config.get("plugins"), list):
            raise ValueError("Prefix routing requires an explicit source router plugin configuration.")
        if not config.get("schedulingProfiles"):
            raise ValueError("Prefix routing requires source scheduling profiles.")
        return epp, key, config

    epp, key, config = document(result)
    cache_types = {"approx-prefix-cache-producer", "prefix-cache-affinity-filter"}
    if enabled:
        if donor is None:
            raise ValueError("Prefix routing requires a pinned affinity plugin source.")
        source_epp, _, source = document(donor)
        if source_epp.get("image") != epp.get("image"):
            raise ValueError("Prefix routing composition requires identical router runtime images.")
        required = cache_types | {"inflight-load-producer"}
        additions = [p for p in source["plugins"] if p.get("type") in required]
        if {p["type"] for p in additions} != required:
            raise ValueError("The pinned routing source does not establish all affinity dependencies.")
        by_name = {p.get("name", p["type"]): p for p in config["plugins"]}
        for plugin in additions:
            name = plugin.get("name", plugin["type"])
            if name in by_name and by_name[name] != plugin:
                raise ValueError(f"Prefix routing plugin {name} conflicts with the selected topology.")
            if name not in by_name:
                config["plugins"].append(deepcopy(plugin))
        affinity = next(p for p in additions if p["type"] == "prefix-cache-affinity-filter")
        reference = affinity.get("name", affinity["type"])
        for profile in config["schedulingProfiles"]:
            # Prefill owns prefix state in P/D; leave decode selection unchanged.
            if profile.get("name") not in {"default", "prefill"}:
                continue
            plugins = profile.get("plugins")
            if not isinstance(plugins, list):
                raise ValueError("Prefix routing requires an ordered source scheduling profile.")
            if not any(p.get("pluginRef") == reference for p in plugins):
                position = next(
                    (
                        i
                        for i, p in enumerate(plugins)
                        if "scorer" in p.get("pluginRef", "") or "picker" in p.get("pluginRef", "")
                    ),
                    len(plugins),
                )
                plugins.insert(position, {"pluginRef": reference})
        if not any(p.get("name") in {"default", "prefill"} for p in config["schedulingProfiles"]):
            raise ValueError("No supported default or prefill profile can use prefix routing.")
    else:
        removed = {p.get("name", p["type"]) for p in config["plugins"] if p.get("type") in cache_types}
        if any("precise-prefix" in p.get("type", "") for p in config["plugins"]):
            raise ValueError("Disable precise routing by selecting a load-routed topology.")
        config["plugins"] = [p for p in config["plugins"] if p.get("name", p["type"]) not in removed]
        for profile in config["schedulingProfiles"]:
            profile["plugins"] = [p for p in profile.get("plugins", []) if p.get("pluginRef") not in removed]
            if not profile["plugins"]:
                raise ValueError("Disabling prefix routing would leave an empty scheduling profile.")
    epp["pluginsCustomConfig"][key] = yaml.safe_dump(config, sort_keys=False)
    return result


def configure_slice_routing(values: dict, mode: str, chart_version: str) -> dict:
    result = deepcopy(values)
    if mode == "inherit":
        return result
    if mode not in {"prefer", "filter"}:
        raise ValueError("EPP Topology Affinity supports inherit, prefer (Scorer), or filter (Filter)")
    epp = result.get("router", {}).get("epp", {})
    image = epp.get("image") or {}
    version = str(image.get("tag") or chart_version)
    if not re.fullmatch(r"v?1\.0\.\d+", version):
        raise ValueError("Slice routing requires a validated Router 1.0 release")
    key = epp.get("pluginsConfigFile")
    custom = epp.get("pluginsCustomConfig") or {}
    document = yaml.safe_load(custom.get(key, ""))
    if not isinstance(document, dict) or not isinstance(document.get("plugins"), list):
        raise ValueError("Slice routing requires a structured EPP plugin configuration")
    profiles = [profile for profile in document.get("schedulingProfiles", []) if profile.get("name") == "prefill"]
    if len(profiles) != 1 or not any(plugin.get("type") == "disagg-profile-handler" for plugin in document["plugins"]):
        raise ValueError("Slice routing requires one prefill profile and a disaggregation profile handler")
    extractor = {
        "type": "topology-extractor",
        "name": "lens-slice-topology",
        "parameters": {"hostname": "disaggregatedset.x-k8s.io/slice"},
    }
    affinity = {
        "type": "topology-affinity-filter" if mode == "filter" else "topology-affinity-scorer",
        "name": "lens-slice-affinity",
        "parameters": {"topologyProducerName": "lens-slice-topology"},
    }
    if mode == "filter":
        affinity["parameters"]["minAffinity"] = "host"
    for plugin in (extractor, affinity):
        matching = [item for item in document["plugins"] if item.get("name", item.get("type")) == plugin["name"]]
        if matching and matching != [plugin]:
            raise ValueError(f"Conflicting Router plugin {plugin['name']}")
        if not matching:
            document["plugins"].append(plugin)
    reference = {"pluginRef": "lens-slice-affinity"}
    if mode == "prefer":
        reference["weight"] = 1
    profile_plugins = profiles[0].setdefault("plugins", [])
    matching = [item for item in profile_plugins if item.get("pluginRef") == reference["pluginRef"]]
    if matching and matching != [reference]:
        raise ValueError("Conflicting Slice affinity plugin reference")
    if not matching:
        profile_plugins.append(reference)
    custom[key] = yaml.safe_dump(document, sort_keys=False)
    epp.setdefault("flags", {})["allow-experimental-plugins"] = True
    return result


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
