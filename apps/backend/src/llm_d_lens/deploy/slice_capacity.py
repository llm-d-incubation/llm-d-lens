"""Bounded Slice capacity estimates over a complete, read-only cluster snapshot."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.utils.quantity import parse_quantity

from llm_d_lens.configuration.disaggregated_set import resource_requests, workload_pods
from llm_d_lens.infrastructure.kubernetes import scoped_runner

DS_NAME_LABEL = "disaggregatedset.x-k8s.io/name"
HOSTNAME_LABEL = "kubernetes.io/hostname"
WELL_KNOWN_TOPOLOGY_KEYS = frozenset({"topology.kubernetes.io/region", "topology.kubernetes.io/zone", HOSTNAME_LABEL})
SYSTEM_RESOURCES = frozenset({"cpu", "memory", "pods", "ephemeral-storage"})
MAX_COUNTED_SLICES = 16


async def inventory(cluster_id: str, resource: str) -> list[dict]:
    command = ["kubectl", "get", resource, "-o", "json"]
    if resource != "nodes":
        command.append("--all-namespaces")
    result = await scoped_runner(cluster_id).run(command, timeout=30)
    if result.returncode:
        raise ValueError(f"Cluster inventory unavailable for {resource}; verify access and installed APIs")
    response = json.loads(result.stdout)
    if not isinstance(response.get("items"), list) or response.get("metadata", {}).get("continue"):
        raise ValueError(f"Cluster inventory incomplete for {resource}")
    return response["items"]


def _quantity_map(values: dict) -> dict[str, float]:
    return {name: float(parse_quantity(value)) for name, value in values.items()}


def free_node_capacity(nodes: list[dict], pods: list[dict]) -> tuple[dict, dict]:
    """Free allocatable resources and labels of Ready, schedulable nodes."""
    free, labels = {}, {}
    for node in nodes:
        metadata, spec, status = node.get("metadata") or {}, node.get("spec") or {}, node.get("status") or {}
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        if spec.get("unschedulable") or not ready or not metadata.get("name"):
            continue
        free[metadata["name"]] = _quantity_map(status.get("allocatable") or {})
        labels[metadata["name"]] = metadata.get("labels") or {}
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if node not in free or (pod.get("status") or {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        for resource, quantity in resource_requests(pod.get("spec") or {}).items():
            free[node][resource] = free[node].get(resource, 0) - quantity
    return free, labels


def fit_slice(capacity: dict, demands: list[dict]) -> dict | None:
    """First-fit-decreasing estimate; not a scheduler simulation or reservation."""
    remaining = {name: dict(values) for name, values in capacity.items()}
    for item in sorted(demands, key=lambda entry: -sum(entry["requests"].values())):
        for values in remaining.values():
            if all(values.get(resource, 0) >= quantity for resource, quantity in item["requests"].items()):
                for resource, quantity in item["requests"].items():
                    values[resource] = values.get(resource, 0) - quantity
                break
        else:
            return None
    return remaining


def _topology_keys(free: dict, labels: dict, demands: list[dict], occupied: set[str]) -> list[dict]:
    resources = {resource for item in demands for resource in item["requests"] if resource != "pods"}
    result = []
    for key in {key for name in free for key in labels[name]}:
        domains: dict[str, list[str]] = {}
        for name in free:
            value = labels[name].get(key)
            if value:
                domains.setdefault(value, []).append(name)
        if len(domains) < 2 and key not in WELL_KNOWN_TOPOLOGY_KEYS:
            continue
        details = []
        for value, members in domains.items():
            available = {
                resource: sum(max(0.0, free[name].get(resource, 0)) for name in members) for resource in resources
            }
            details.append(
                {
                    "value": value,
                    "nodeCount": len(members),
                    "nodes": sorted(members)[:8],
                    "freeResources": available,
                    "fitsSlice": fit_slice({name: free[name] for name in members}, demands) is not None,
                    "occupied": any(name in occupied for name in members),
                }
            )
        feasible = sum(1 for item in details if item["fitsSlice"])
        if not feasible and key not in WELL_KNOWN_TOPOLOGY_KEYS:
            continue
        details.sort(key=lambda item: (not item["fitsSlice"], item["value"]))
        result.append(
            {
                "key": key,
                "wellKnown": key in WELL_KNOWN_TOPOLOGY_KEYS,
                "labeledNodes": sum(len(members) for members in domains.values()),
                "domainCount": len(domains),
                "domains": {value: len(members) for value, members in sorted(domains.items())},
                "domainDetails": details[:32],
                "feasibleDomains": feasible,
                "exclusiveTopologyDomains": sum(1 for item in details if item["fitsSlice"] and not item["occupied"]),
            }
        )
    result.sort(
        key=lambda item: (
            not item["key"].startswith("topology.kubernetes.io/"),
            item["key"] == HOSTNAME_LABEL,
            -item["feasibleDomains"],
            item["key"],
        )
    )
    return result[:12]


def slice_capacity(documents: list[dict[str, Any]], nodes: list[dict], pods: list[dict]) -> dict[str, Any]:
    """Per-placement Slice ceilings from current free resources; an estimate, not a reservation."""
    demands = [item for item in workload_pods(documents) if item["kind"] == "DisaggregatedSet" and item["slice"] == 0]
    per_slice: dict[str, float] = {}
    for item in demands:
        for resource, quantity in item["requests"].items():
            per_slice[resource] = per_slice.get(resource, 0) + quantity
    free, labels = free_node_capacity(nodes, pods)
    occupied = {
        (pod.get("spec") or {}).get("nodeName")
        for pod in pods
        if ((pod.get("metadata") or {}).get("labels") or {}).get(DS_NAME_LABEL)
        and (pod.get("status") or {}).get("phase") not in {"Succeeded", "Failed"}
    }
    count, remaining = 0, free
    while count < MAX_COUNTED_SLICES:
        packed = fit_slice(remaining, demands)
        if packed is None:
            break
        remaining, count = packed, count + 1
    return {
        "podsPerSlice": len(demands),
        "sliceRequests": {resource: value for resource, value in per_slice.items() if resource != "pods"},
        "acceleratorResources": sorted(
            resource
            for resource in per_slice
            if resource not in SYSTEM_RESOURCES and not resource.startswith("hugepages-")
        ),
        "maxSlices": {"None": count},
        "maxSlicesCapped": count >= MAX_COUNTED_SLICES,
        "unmodeled": ["DRA"] if any(item["pod"].get("resourceClaims") for item in demands) else [],
        "schedulableNodes": len(free),
        "topologyKeys": _topology_keys(free, labels, demands, occupied),
    }
