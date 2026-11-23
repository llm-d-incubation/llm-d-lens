"""Size and place DisaggregatedSet slices from the target cluster's topology domains."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any

from llm_d_lens.hardware.resolver import resolve_by_resource
from llm_d_lens.infrastructure.kubernetes import run_kubectl

from .guide_planning import GuideError

PLACEMENT_TYPES = ("None", "ExclusiveSlice", "ExclusiveTopology")
HOSTNAME = "kubernetes.io/hostname"
EXCLUSIVE_TOPOLOGY_ANNOTATION = "leaderworkerset.sigs.k8s.io/exclusive-topology"
SLICE_LABEL = "disaggregatedset.x-k8s.io/slice"
MAX_SLICES = 64
_LABEL_KEY = re.compile(r"^([a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
# Narrow to wide: a smaller shared domain keeps the P/D KV transfer local.
_INTERMEDIATE = re.compile(r"(nvlink|clique|rack|block|superpod|subblock|switch|domain)", re.IGNORECASE)
_WIDE = ("topology.kubernetes.io/zone", "topology.kubernetes.io/region")


def disaggregated_sets(parsed: list[dict]) -> list[dict]:
    return [doc for doc in parsed if doc.get("kind") == "DisaggregatedSet"]


def validate_policy(policy: Any) -> dict[str, str]:
    if not isinstance(policy, dict) or set(policy) - {"type", "topology"}:
        raise GuideError("Slice placement policy must contain only type and topology.")
    kind = policy.get("type", "None")
    if kind not in PLACEMENT_TYPES:
        raise GuideError("Slice placement type must be None, ExclusiveSlice or ExclusiveTopology.")
    if kind == "None":
        return {"type": "None"}
    topology = policy.get("topology")
    if not isinstance(topology, str) or len(topology) > 316 or not _LABEL_KEY.fullmatch(topology):
        raise GuideError("Slice placement requires a valid node-label topology key.")
    return {"type": kind, "topology": topology}


def validate_slices(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SLICES:
        raise GuideError(f"Slices must be a whole number from 1 to {MAX_SLICES}.")
    return value


def topology_keys(nodes: list[dict]) -> list[str]:
    keys = {key for node in nodes for key in node["labels"]}
    intermediate = sorted(key for key in keys if _INTERMEDIATE.search(key) and key != HOSTNAME and key not in _WIDE)
    return [key for key in [HOSTNAME, *intermediate, *_WIDE] if key in keys]


def _domains(nodes: list[dict], key: str) -> list[dict]:
    grouped: dict[str, dict] = {}
    for node in nodes:
        value = node["labels"].get(key)
        if value is None:
            continue
        domain = grouped.setdefault(value, {"value": value, "free": 0, "occupied": False})
        domain["free"] += node["free"]
        domain["occupied"] = domain["occupied"] or node["occupied"]
    return list(grouped.values())


def pod_placement_conflict(pod_placement: str | None, slices: int, policy: dict[str, str]) -> str | None:
    """Explain why an exclusive slice policy cannot satisfy the selected Pod placement."""
    if policy.get("type", "None") == "None":
        return None
    topology = policy.get("topology")
    if pod_placement == "same-node" and slices > 1:
        return (
            f"Pod placement 'same-node' puts every Pod on one node, but slice placement on {topology} puts each "
            "slice in a different domain. Use one slice, slice placement None, or another Pod placement."
        )
    if topology != HOSTNAME:
        return None
    if pod_placement in {"balanced", "spread"}:
        return (
            f"Pod placement '{pod_placement}' separates model-server Pods across nodes, but slice placement on "
            f"{HOSTNAME} requires each slice's Prefill and Decode on one node. Choose a wider topology key, "
            "None, or Pod placement prefer-same-node/same-node."
        )
    return None


def recommend_layout(
    nodes: list[dict],
    *,
    prefill_replicas: int,
    decode_replicas: int,
    slice_devices: float,
    objective: str = "balanced",
    slices: int | None = None,
    pod_placement: str | None = None,
) -> dict[str, Any]:
    """Choose slices and a hard placement that the current free devices can satisfy.

    ``slice_devices`` is the device demand of the current single slice. With
    automatic slicing the role replicas are split evenly, so each candidate
    count must divide both roles and keeps the total device count unchanged.
    """
    if slices is not None:
        options = [(slices, slice_devices)]
    else:
        common = math.gcd(prefill_replicas, decode_replicas) or 1
        options = [(count, slice_devices / count) for count in range(1, common + 1) if common % count == 0]
    exclusive = objective in {"latency", "reliability"}
    for key in topology_keys(nodes):
        domains = _domains(nodes, key)
        for count, demand in options:
            if pod_placement_conflict(pod_placement, count, {"type": "ExclusiveSlice", "topology": key}):
                continue
            fitting = [domain for domain in domains if domain["free"] >= demand]
            if len(fitting) < count:
                continue
            isolated = [domain for domain in fitting if not domain["occupied"]]
            kind = "ExclusiveTopology" if exclusive and len(isolated) >= count else "ExclusiveSlice"
            scope = "node" if key == HOSTNAME else f"{key} domain"
            return {
                "slices": count,
                "prefillReplicas": prefill_replicas // (1 if slices is not None else count),
                "replicas": decode_replicas // (1 if slices is not None else count),
                "placementPolicy": {"type": kind, "topology": key},
                "eligibleDomains": [
                    domain["value"] for domain in (isolated if kind == "ExclusiveTopology" else fitting)
                ],
                "reason": (
                    f"{len(fitting)} {scope}s have at least {demand:g} free devices, enough for {count} "
                    f"slice{'s' if count > 1 else ''}; each slice keeps prefill and decode in one {scope} "
                    "so KV transfer stays local"
                    + (
                        "; latency/reliability objective reserves each domain for one slice."
                        if kind == "ExclusiveTopology"
                        else "."
                    )
                ),
            }
    return {
        "slices": slices or 1,
        "prefillReplicas": prefill_replicas,
        "replicas": decode_replicas,
        "placementPolicy": {"type": "None"},
        "reason": "No labeled topology domain has enough free devices for a whole slice; hard co-location "
        "would leave Pods Pending, so placement is left to the scheduler.",
    }


async def node_inventory(session_id: str) -> list[dict]:
    """Ready nodes with labels and currently unreserved accelerator devices."""
    from llm_d_lens.cluster.service import _active_gpu_device_keys, _resource_slice_gpu_devices

    from .source_composition import pod_demand

    nodes = await run_kubectl(["get", "nodes", "-o", "json"], timeout=20, cluster_id=session_id)
    pods = await run_kubectl(["get", "pods", "--all-namespaces", "-o", "json"], timeout=20, cluster_id=session_id)
    if not nodes.ok or not pods.ok:
        raise GuideError("Could not read cluster topology for slice placement.", 502)
    slices = await run_kubectl(
        ["get", "resourceslices.resource.k8s.io", "-o", "json"], timeout=20, cluster_id=session_id
    )
    claims = await run_kubectl(
        ["get", "resourceclaims.resource.k8s.io", "-A", "-o", "json"], timeout=20, cluster_id=session_id
    )
    devices = _resource_slice_gpu_devices(json.loads(slices.stdout).get("items", [])) if slices.ok else {}
    allocated = _active_gpu_device_keys(json.loads(claims.stdout).get("items", [])) if claims.ok else set()
    free = defaultdict(float)
    occupied = set()
    for key, device in devices.items():
        if key not in allocated:
            free[device["node"]] += 1
    for pod in json.loads(pods.stdout).get("items", []):
        spec = pod.get("spec", {})
        node = spec.get("nodeName")
        if not node or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        if SLICE_LABEL in (pod.get("metadata", {}).get("labels") or {}):
            occupied.add(node)
        for key, value in pod_demand(spec).items():
            if resolve_by_resource(key):
                free[node] -= value
    result = []
    for node in json.loads(nodes.stdout).get("items", []):
        name = node.get("metadata", {}).get("name")
        if node.get("spec", {}).get("unschedulable") or not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        ):
            continue
        plugin = sum(
            float(value)
            for key, value in node.get("status", {}).get("allocatable", {}).items()
            if resolve_by_resource(key)
        )
        result.append(
            {
                "name": name,
                "labels": node.get("metadata", {}).get("labels") or {},
                "free": max(0.0, plugin + free[name]),
                "occupied": name in occupied,
            }
        )
    return result


def apply_layout(parsed: list[dict], layout: dict[str, Any], *, split: int = 1) -> None:
    """Write slices and placement into every DisaggregatedSet; ``split`` divides role replicas."""
    for document in disaggregated_sets(parsed):
        spec = document.setdefault("spec", {})
        for role in spec.get("roles") or []:
            role_spec = role.get("spec") or {}
            for template in (role_spec.get("leaderWorkerTemplate") or {}).values():
                annotations = (
                    (template.get("metadata") or {}).get("annotations") or {} if isinstance(template, dict) else {}
                )
                if layout["placementPolicy"]["type"] != "None" and EXCLUSIVE_TOPOLOGY_ANNOTATION in annotations:
                    raise GuideError("Slice placement cannot be combined with the LWS exclusive-topology annotation.")
                if (
                    isinstance(template, dict)
                    and layout.get("eligibleDomains")
                    and layout["placementPolicy"].get("topology")
                ):
                    required = (
                        template.setdefault("spec", {})
                        .setdefault("affinity", {})
                        .setdefault("nodeAffinity", {})
                        .setdefault("requiredDuringSchedulingIgnoredDuringExecution", {"nodeSelectorTerms": [{}]})
                    )
                    for term in required["nodeSelectorTerms"]:
                        term.setdefault("matchExpressions", []).append(
                            {
                                "key": layout["placementPolicy"]["topology"],
                                "operator": "In",
                                "values": list(layout["eligibleDomains"]),
                            }
                        )
            annotations = (role.get("metadata") or {}).get("annotations") or {}
            if layout["placementPolicy"]["type"] != "None" and EXCLUSIVE_TOPOLOGY_ANNOTATION in annotations:
                raise GuideError("Slice placement cannot be combined with the LWS exclusive-topology annotation.")
            if split > 1:
                replicas = int(role_spec.get("replicas", 1))
                if replicas % split:
                    raise GuideError("Role replicas must divide evenly across slices.")
                role_spec["replicas"] = replicas // split
        spec["slices"] = layout["slices"]
        if layout["placementPolicy"]["type"] == "None":
            spec.pop("placementPolicy", None)
        else:
            spec["placementPolicy"] = dict(layout["placementPolicy"])
