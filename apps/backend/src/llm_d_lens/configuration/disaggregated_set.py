"""DisaggregatedSet intent and rendered workload validation."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from kubernetes.utils.quantity import parse_quantity


def positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def normalize_disaggregated_set(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"slices", "placementPolicy", "sliceRouting"}:
        raise ValueError("disaggregatedSet contains unknown fields or is not an object")
    result = deepcopy(value)
    if "slices" in result:
        positive_integer(result["slices"], "disaggregatedSet.slices")
    if "sliceRouting" in result and result["sliceRouting"] not in {"inherit", "prefer", "filter"}:
        raise ValueError("disaggregatedSet.sliceRouting must be inherit, prefer (Scorer), or filter (Filter)")
    if "placementPolicy" in result:
        policy = result["placementPolicy"]
        if not isinstance(policy, dict) or set(policy) - {"type", "topology"}:
            raise ValueError("disaggregatedSet.placementPolicy must contain type and optional topology")
        if policy.get("type") not in {"None", "ExclusiveSlice", "ExclusiveTopology"}:
            raise ValueError("disaggregatedSet.placementPolicy.type is unsupported")
        if policy["type"] != "None" and (not isinstance(policy.get("topology"), str) or not policy["topology"].strip()):
            raise ValueError("disaggregatedSet.placementPolicy.topology is required")
        if policy["type"] == "None" and policy.get("topology"):
            raise ValueError("disaggregatedSet.placementPolicy.topology requires an exclusive placement policy")
    return result


def disaggregated_sets(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("kind") == "List":
            result.extend(disaggregated_sets(document.get("items") or []))
        elif document.get("kind") == "DisaggregatedSet":
            result.append(document)
    return result


def validate_disaggregated_sets(documents: list[dict[str, Any]], intent: Any = None) -> dict[str, Any]:
    settings = normalize_disaggregated_set(intent)
    workloads = disaggregated_sets(documents)
    if settings and len(workloads) != 1:
        raise ValueError("disaggregatedSet configuration requires exactly one DisaggregatedSet")
    slice_counts = []
    for document in workloads:
        spec = document.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("DisaggregatedSet.spec must be an object")
        slices = positive_integer(spec.get("slices", 1), "DisaggregatedSet.spec.slices")
        slice_counts.append(slices)
        for field in ("slices", "placementPolicy"):
            if field in settings and settings[field] != spec.get(field, 1 if field == "slices" else {"type": "None"}):
                raise ValueError(f"disaggregatedSet.{field} disagrees with the rendered manifest")
        if "placementPolicy" in spec:
            normalize_disaggregated_set({"placementPolicy": spec["placementPolicy"]})
        roles = spec.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError("DisaggregatedSet.spec.roles must be a nonempty array")
        names = set()
        for role in roles:
            if not isinstance(role, dict) or not isinstance(role.get("name"), str) or not role["name"]:
                raise ValueError("DisaggregatedSet roles require a name")
            if role["name"] in names:
                raise ValueError("DisaggregatedSet role names must be unique")
            names.add(role["name"])
            scaling = role.get("scaling", {})
            if not isinstance(scaling, dict):
                raise ValueError("DisaggregatedSet role scaling must be an object")
            mode = scaling.get("mode", "Static")
            if mode not in {"Static", "External"}:
                raise ValueError("DisaggregatedSet role scaling mode is unsupported")
            if slices > 1 and mode == "External":
                raise ValueError("DisaggregatedSet slices > 1 cannot use External role scaling")
            role_spec = role.get("spec") or {}
            if not isinstance(role_spec, dict) or not isinstance(role_spec.get("leaderWorkerTemplate", {}), dict):
                raise ValueError("DisaggregatedSet role spec and leaderWorkerTemplate must be objects")
            positive_integer(role_spec.get("replicas", 1), f"roles.{role['name']}.replicas")
            positive_integer(
                (role_spec.get("leaderWorkerTemplate") or {}).get("size", 1), f"roles.{role['name']}.groupSize"
            )
    return {
        "disaggregatedSets": len(workloads),
        "slices": slice_counts[0] if len(slice_counts) == 1 else None,
        "adjusted": 0,
    }


def apply_disaggregated_set(documents: list[dict[str, Any]], intent: Any) -> None:
    settings = normalize_disaggregated_set(intent)
    workloads = disaggregated_sets(documents)
    if settings and len(workloads) != 1:
        raise ValueError("disaggregatedSet configuration requires exactly one DisaggregatedSet")
    if settings:
        spec = workloads[0].setdefault("spec", {})
        for field in ("slices", "placementPolicy"):
            if field in settings:
                spec[field] = deepcopy(settings[field])
    validate_disaggregated_sets(documents, settings)


def resource_requests(pod: dict[str, Any]) -> dict[str, float]:
    def requests(container):
        resources = container.get("resources") or {}
        values = {**(resources.get("limits") or {}), **(resources.get("requests") or {})}
        result = {key: float(parse_quantity(value)) for key, value in values.items()}
        if any(value < 0 or not math.isfinite(value) for value in result.values()):
            raise ValueError("Pod resource requests must be finite and nonnegative")
        return result

    def add(target, values):
        for key, value in values.items():
            target[key] = target.get(key, 0) + value

    regular, persistent, peak = {}, {}, {}
    for container in pod.get("containers") or []:
        add(regular, requests(container))
    for container in pod.get("initContainers") or []:
        current = requests(container)
        if container.get("restartPolicy") == "Always":
            add(persistent, current)
            current = dict(persistent)
        else:
            add(current, persistent)
        for key, value in current.items():
            peak[key] = max(peak.get(key, 0), value)
    add(regular, persistent)
    for key, value in peak.items():
        regular[key] = max(regular.get(key, 0), value)
    pod_level = (pod.get("resources") or {}).get("requests") or {}
    for key, value in pod_level.items():
        regular[key] = max(regular.get(key, 0), float(parse_quantity(value)))
    add(regular, {key: float(parse_quantity(value)) for key, value in (pod.get("overhead") or {}).items()})
    if any(value < 0 or not math.isfinite(value) for value in regular.values()):
        raise ValueError("Pod resource requests and overhead must be finite and nonnegative")
    regular["pods"] = 1
    return regular


def workload_pods(documents: list[dict[str, Any]], *, limit: int = 4096) -> list[dict[str, Any]]:
    validate_disaggregated_sets(documents)
    pods = []
    for document in documents:
        kind = document.get("kind")
        if kind == "List":
            pods.extend(workload_pods(document.get("items") or [], limit=limit - len(pods)))
            continue
        if kind not in {"DisaggregatedSet", "LeaderWorkerSet", "Deployment", "Pod"}:
            continue
        spec = document.get("spec") or {}
        roles = spec.get("roles", []) if kind == "DisaggregatedSet" else [{"name": "serving", "spec": spec}]
        slices = spec.get("slices", 1) if kind == "DisaggregatedSet" else 1
        for role in roles:
            role_spec = role.get("spec") or {}
            group = role_spec.get("leaderWorkerTemplate") or {}
            size = positive_integer(group.get("size", 1), "groupSize")
            replicas = positive_integer(role_spec.get("replicas", 1), "replicas")
            if len(pods) + slices * replicas * size > limit:
                raise ValueError(f"Workload expansion exceeds the {limit}-pod planning limit")
            for slice_index in range(slices):
                for replica in range(replicas):
                    for rank in range(size):
                        if kind == "Pod":
                            template = document
                        elif kind == "Deployment":
                            template = role_spec.get("template") or {}
                        else:
                            template = group.get("leaderTemplate") if rank == 0 else None
                            template = template if template is not None else group.get("workerTemplate", {})
                        pod = deepcopy(template.get("spec") or {})
                        pods.append(
                            {
                                "workload": (document.get("metadata") or {}).get("name", ""),
                                "namespace": (document.get("metadata") or {}).get("namespace", "default"),
                                "kind": kind,
                                "role": role["name"],
                                "slice": slice_index,
                                "group": replica,
                                "rank": rank,
                                "pod": pod,
                                "requests": resource_requests(pod),
                                "placementPolicy": deepcopy(spec.get("placementPolicy") or {"type": "None"}),
                            }
                        )
    return pods


def workload_summary(documents: list[dict[str, Any]]) -> dict[str, Any]:
    pods = workload_pods(documents)
    total = {}
    for pod in pods:
        for resource, quantity in pod["requests"].items():
            total[resource] = total.get(resource, 0) + quantity
    return {
        "podCount": len(pods),
        "disaggregatedSet": bool(disaggregated_sets(documents)),
        "resourceRequests": total,
        "unmodeled": ["DRA"] if any(item["pod"].get("resourceClaims") for item in pods) else [],
    }
