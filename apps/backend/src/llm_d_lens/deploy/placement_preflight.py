"""Bounded placement estimates over a complete, read-only cluster snapshot."""

from __future__ import annotations

from typing import Any

from llm_d_lens.configuration.disaggregated_set import resource_requests, workload_pods
from llm_d_lens.deploy.slice_capacity import _quantity_map
from llm_d_lens.deploy.slice_capacity import slice_capacity as slice_capacity


def max_feasible_slices(capacity: dict[str, Any], policy: dict | None) -> int | None:
    policy = policy or {"type": "None"}
    if policy.get("type", "None") == "None":
        return capacity["maxSlices"]["None"]
    for item in capacity["topologyKeys"]:
        if item["key"] == policy.get("topology"):
            return item["exclusiveTopologyDomains" if policy["type"] == "ExclusiveTopology" else "feasibleDomains"]
    return None


def _expression_matches(expression: dict, labels: dict) -> bool:
    key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values", [])
    present, value = key in labels, labels.get(key)
    if operator == "In":
        return present and value in values
    if operator == "NotIn":
        return not present or value not in values
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in {"Gt", "Lt"}:
        try:
            return (
                present
                and len(values) == 1
                and (int(value) > int(values[0]) if operator == "Gt" else int(value) < int(values[0]))
            )
        except (ValueError, TypeError):
            return False
    raise ValueError(f"Unsupported node affinity operator: {operator}")


def node_matches(pod: dict, node: dict) -> bool:
    metadata, spec = node.get("metadata", {}), node.get("spec", {})
    labels = metadata.get("labels", {})
    if spec.get("unschedulable") or not any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in node.get("status", {}).get("conditions", [])
    ):
        return False
    if pod.get("nodeName") and pod["nodeName"] != metadata.get("name"):
        return False
    if any(labels.get(key) != value for key, value in pod.get("nodeSelector", {}).items()):
        return False
    for taint in spec.get("taints", []):
        if taint.get("effect") not in {"NoSchedule", "NoExecute"}:
            continue
        if not any(
            (not toleration.get("effect") or toleration["effect"] == taint.get("effect"))
            and (
                toleration.get("operator", "Equal") == "Exists"
                and (not toleration.get("key") or toleration["key"] == taint.get("key"))
                or toleration.get("operator", "Equal") == "Equal"
                and toleration.get("key") == taint.get("key")
                and toleration.get("value", "") == taint.get("value", "")
            )
            for toleration in pod.get("tolerations", [])
        ):
            return False
    required = pod.get("affinity", {}).get("nodeAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
    if required is not None:
        return any(
            bool(term.get("matchExpressions") or term.get("matchFields"))
            and all(_expression_matches(expr, labels) for expr in term.get("matchExpressions", []))
            and all(
                _expression_matches(expr, {"metadata.name": metadata.get("name")})
                for expr in term.get("matchFields", [])
            )
            for term in required.get("nodeSelectorTerms", [])
        )
    return True


def placement_preflight(
    documents: list[dict[str, Any]],
    nodes: list[dict],
    existing_pods: list[dict],
    *,
    complete: bool = True,
    runtime_classes: list[dict] | None = None,
    search_limit: int = 20000,
) -> dict[str, Any]:
    errors, warnings, unmodeled = [], [], set()
    report = {"status": "unknown", "errors": errors, "warnings": warnings, "unmodeled": [], "assignments": []}
    if not complete:
        report["unmodeled"] = ["Cluster inventory is incomplete or inaccessible"]
        return report
    demands = workload_pods(documents)
    classes = {item["metadata"]["name"]: item for item in runtime_classes or []}
    for item in demands:
        pod = item["pod"]
        runtime_name = pod.get("runtimeClassName")
        if not runtime_name or runtime_classes is None:
            continue
        runtime = classes.get(runtime_name)
        if runtime is None:
            errors.append({"code": "RUNTIMECLASS_UNAVAILABLE", "message": runtime_name})
            continue
        scheduling = runtime.get("scheduling") or {}
        selectors = pod.setdefault("nodeSelector", {})
        for key, value in (scheduling.get("nodeSelector") or {}).items():
            if key in selectors and selectors[key] != value:
                errors.append({"code": "RUNTIMECLASS_SELECTOR_CONFLICT", "message": key})
            selectors[key] = value
        tolerations = pod.setdefault("tolerations", [])
        tolerations.extend(value for value in scheduling.get("tolerations", []) if value not in tolerations)
        fixed = (runtime.get("overhead") or {}).get("podFixed", {})
        if pod.get("overhead") and pod["overhead"] != fixed:
            errors.append({"code": "RUNTIMECLASS_OVERHEAD_CONFLICT", "message": runtime_name})
        pod["overhead"] = fixed
        item["requests"] = resource_requests(pod)
    remaining = {
        node["metadata"]["name"]: _quantity_map(node.get("status", {}).get("allocatable", {})) for node in nodes
    }
    totals = {}
    for values in remaining.values():
        for resource, quantity in values.items():
            totals[resource] = totals.get(resource, 0) + quantity
    occupants = []
    for pod in existing_pods:
        node_name = pod.get("spec", {}).get("nodeName")
        if node_name not in remaining or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        for resource, quantity in resource_requests(pod.get("spec", {})).items():
            remaining[node_name][resource] = remaining[node_name].get(resource, 0) - quantity
        occupants.append(pod)
    requested = {}
    for item in demands:
        for resource, quantity in item["requests"].items():
            requested[resource] = requested.get(resource, 0) + quantity
    report["summary"] = {"podCount": len(demands), "resourceRequests": requested}
    node_by_name = {node["metadata"]["name"]: node for node in nodes}
    candidates = []
    for item in demands:
        pod = item["pod"]
        affinity = pod.get("affinity", {})
        for condition, reason in (
            (pod.get("resourceClaims"), "DRA allocation"),
            (pod.get("runtimeClassName") and runtime_classes is None, "RuntimeClass scheduling and overhead"),
            (affinity.get("podAffinity") or affinity.get("podAntiAffinity"), "Pod affinity and anti-affinity"),
            (pod.get("topologySpreadConstraints"), "Topology spread constraints"),
            (pod.get("schedulingGates"), "Scheduling gates"),
            (
                any("persistentVolumeClaim" in volume or "hostPath" in volume for volume in pod.get("volumes", [])),
                "Volume placement and availability",
            ),
            (
                any(
                    port.get("hostPort")
                    for container in pod.get("containers", [])
                    for port in container.get("ports", [])
                ),
                "Host port allocation",
            ),
            (pod.get("schedulerName", "default-scheduler") != "default-scheduler", "Custom scheduler"),
        ):
            if condition:
                unmodeled.add(reason)
        policy = item["placementPolicy"]
        topology = policy.get("topology") if policy.get("type") != "None" else None
        eligible = []
        for node in nodes:
            if not node_matches(pod, node):
                continue
            name, labels = node["metadata"]["name"], node["metadata"].get("labels", {})
            if topology and not labels.get(topology):
                continue
            if policy.get("type") == "ExclusiveTopology" and any(
                existing.get("metadata", {}).get("labels", {}).get("disaggregatedset.x-k8s.io/name")
                and node_by_name[existing["spec"]["nodeName"]]["metadata"].get("labels", {}).get(topology)
                == labels.get(topology)
                for existing in occupants
            ):
                continue
            if all(remaining[name].get(resource, 0) >= quantity for resource, quantity in item["requests"].items()):
                eligible.append(name)
        candidates.append(eligible)
        if not eligible:
            errors.append(
                {
                    "code": "NO_NODE_FITS_POD",
                    "role": item["role"],
                    "slice": item["slice"],
                    "field": "disaggregatedSet",
                    "message": "No eligible node can fit this Pod's requests.",
                }
            )
        if topology:
            unmodeled.add("Physical topology source is unverified")
    if errors:
        report["status"] = "fail"
        report["unmodeled"] = sorted(unmodeled)
        return report
    order = sorted(
        range(len(demands)), key=lambda index: (len(candidates[index]), -sum(demands[index]["requests"].values()))
    )
    assigned, domains = {}, {}
    exclusive_slices = {
        (item["namespace"], item["workload"], item["slice"])
        for item in demands
        if item["placementPolicy"].get("type") == "ExclusiveTopology"
    }
    attempts, exhausted = 0, False

    def search(position):
        nonlocal attempts, exhausted
        if position == len(order):
            return True
        if attempts >= search_limit:
            exhausted = True
            return False
        index = order[position]
        item = demands[index]
        policy = item["placementPolicy"]
        topology = policy.get("topology") if policy.get("type") != "None" else None
        identity = (item["namespace"], item["workload"], item["slice"])
        for name in candidates[index]:
            attempts += 1
            if attempts > search_limit:
                exhausted = True
                return False
            domain = node_by_name[name]["metadata"].get("labels", {}).get(topology) if topology else None
            if topology and (identity in domains and domains[identity] != (topology, domain)):
                continue
            if topology and any(
                key != identity
                and value == (topology, domain)
                and (key[:2] == identity[:2] or key in exclusive_slices or identity in exclusive_slices)
                for key, value in domains.items()
            ):
                continue
            if not all(remaining[name].get(resource, 0) >= quantity for resource, quantity in item["requests"].items()):
                continue
            previous = domains.get(identity)
            if topology:
                domains[identity] = (topology, domain)
            for resource, quantity in item["requests"].items():
                remaining[name][resource] -= quantity
            assigned[index] = name
            if search(position + 1):
                return True
            assigned.pop(index)
            for resource, quantity in item["requests"].items():
                remaining[name][resource] += quantity
            if topology and previous is None:
                domains.pop(identity)
        return False

    if len(demands) > 400:
        unmodeled.add("Placement search exceeds the 400-Pod interactive limit")
        found = False
        exhausted = True
    else:
        found = search(0)
    if found:
        report["assignments"] = []
        for index, name in sorted(assigned.items()):
            policy = demands[index]["placementPolicy"]
            topology = policy.get("topology") if policy.get("type") != "None" else None
            report["assignments"].append(
                {
                    "workload": demands[index]["workload"],
                    "slice": demands[index]["slice"],
                    "role": demands[index]["role"],
                    "group": demands[index]["group"],
                    "rank": demands[index]["rank"],
                    "node": name,
                    **({"domain": node_by_name[name]["metadata"].get("labels", {}).get(topology)} if topology else {}),
                }
            )
    elif exhausted:
        unmodeled.add("Bounded placement search did not reach a conclusion")
    else:
        errors.append(
            {
                "code": "NO_FEASIBLE_PLACEMENT",
                "field": "disaggregatedSet.slices",
                "message": "No placement satisfies the modeled node capacity and Slice domain constraints.",
            }
        )
    unmodeled.add("Admission, quota, competing scheduling and rollout surge are not reserved or simulated")
    report["unmodeled"] = sorted(unmodeled)
    report["modeledConstraints"] = "pass" if found else "unknown" if exhausted else "fail"
    report["status"] = report["modeledConstraints"]
    report["searchSteps"] = attempts
    return report
