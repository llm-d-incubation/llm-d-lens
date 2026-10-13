"""Join profile-configured device history to exclusive DRA allocations.

XPUM's namespace/pod labels describe the exporter, not the consumer. Never
attribute every GPU on a model server's node to that model server.
"""

import math
import re
from collections import defaultdict
from datetime import datetime

from llm_d_bench.hardware.telemetry import DEVICE_RESULT_KEYS, _selector


def xpum_queries(profile=None) -> dict[str, str]:
    """Compatibility name for unaggregated, profile-selected device queries."""
    if not profile or not profile.telemetry:
        return {}
    return {
        DEVICE_RESULT_KEYS.get(key, key): source.metric + _selector(source.match)
        for key, source in profile.telemetry.device_metric_sources.items()
        if source.metric
    }


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def device_allocations(pods, claims, slices, namespace, profile=None):
    """Return exclusive (node, PCI) -> consumer and allocation start mappings."""
    if not profile or not profile.telemetry:
        return {}
    settings = profile.telemetry.allocation
    if profile.telemetry.allocation_join == "pod-status":
        return plugin_device_allocations(pods, namespace, profile)
    drivers = settings.get("drivers", profile.device_classes)
    devices = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") not in drivers:
            continue
        node = spec.get("nodeName")
        pool = (spec.get("pool") or {}).get("name")
        for device in spec.get("devices") or []:
            attrs = device.get("attributes") or {}
            pci = next((attrs[key].get("string") for key in settings.get("pci_attributes", []) if attrs.get(key)), None)
            if node and pool and pci:
                devices[(pool, device.get("name"))] = (node, pci.lower())
    by_uid = {
        p.get("metadata", {}).get("uid"): p
        for p in pods
        if p.get("metadata", {}).get("uid") and p.get("metadata", {}).get("namespace") == namespace
    }
    owners = defaultdict(list)
    for claim in claims:
        if claim.get("metadata", {}).get("namespace") != namespace:
            continue
        status = claim.get("status") or {}
        consumers = status.get("reservedFor") or []
        # Shared/admin allocations are not exclusive Pod utilization evidence.
        if len(consumers) != 1 or consumers[0].get("resource") != "pods":
            continue
        pod = by_uid.get(consumers[0].get("uid"))
        if not pod or pod.get("metadata", {}).get("name") != consumers[0].get("name"):
            continue
        allocation = status.get("allocation") or {}
        since = max(
            _timestamp(allocation.get("allocationTimestamp")),
            _timestamp(pod.get("status", {}).get("startTime")),
            _timestamp(pod.get("metadata", {}).get("creationTimestamp")),
        )
        for result in allocation.get("devices", {}).get("results") or []:
            if result.get("driver") not in drivers or result.get("adminAccess") or result.get("shareID"):
                continue
            identity = devices.get((result.get("pool"), result.get("device")))
            # Intel DRA encodes PCI BDF in its device ID. A driver refresh can
            # temporarily remove ResourceSlices while allocated claims survive.
            if (
                identity is None
                and settings.get("device_id_pattern")
                and result.get("pool") == pod.get("spec", {}).get("nodeName")
            ):
                pci_id = re.fullmatch(
                    settings["device_id_pattern"],
                    str(result.get("device") or "").lower(),
                )
                if pci_id:
                    domain, bus, slot, function = pci_id.groups()
                    identity = (result["pool"], f"{domain}:{bus}:{slot}.{function}")
            if identity and identity[0] == pod.get("spec", {}).get("nodeName"):
                owners[identity].append({"pod": consumers[0]["name"], "since": since})
    return {
        identity: entries[0] for identity, entries in owners.items() if len({entry["pod"] for entry in entries}) == 1
    }


def aggregate_xpum(samples, allocations, metric, profile=None):
    """Produce normal Prometheus matrices for the deployment and each Pod.

    Require node+PCI identity. Prefer whole-device samples to tiles; deduplicate
    exporter replicas before summing bytes or averaging device utilization.
    """
    if not profile or not profile.telemetry:
        return [], []
    telemetry = profile.telemetry
    source = next(
        (
            source
            for key, source in telemetry.device_metric_sources.items()
            if DEVICE_RESULT_KEYS.get(key, key) == metric
        ),
        None,
    )
    if source is None:
        return [], []
    labels_schema = telemetry.label_schema
    cells = defaultdict(dict)
    for sample in samples:
        labels = sample.get("metric") or {}
        node = next(
            (
                labels.get(key)
                for key in telemetry.allocation.get("node_labels", [labels_schema.get("node", "")])
                if labels.get(key)
            ),
            None,
        )
        identity = (node, str(labels.get(labels_schema.get("pci", "")) or "").lower())
        allocation = allocations.get(identity)
        if not allocation or any(labels.get(key) != value for key, value in source.match.items()):
            continue
        tile = labels.get(labels_schema.get("device", "")) or ""
        for timestamp, raw in sample.get("values") or []:
            try:
                timestamp, value = float(timestamp), float(raw)
            except (ValueError, TypeError):
                continue
            if not math.isfinite(value) or value < 0 or timestamp < allocation["since"]:
                continue
            key = (identity, timestamp)
            # Duplicate scrapes are one device sample, never extra devices.
            cells[key][tile] = max(cells[key].get(tile, value), value)
    deployment = defaultdict(list)
    pods = defaultdict(lambda: defaultdict(list))
    for (identity, timestamp), tiles in cells.items():
        value = (
            tiles[""]
            if "" in tiles
            else sum(tiles.values())
            if source.tile_aggregation == "sum"
            else max(tiles.values())
            if source.tile_aggregation == "max"
            else sum(tiles.values()) / len(tiles)
        )
        value *= source.scale
        deployment[timestamp].append(value)
        pods[allocations[identity]["pod"]][timestamp].append(value)

    def matrix(points, labels):
        return {
            "metric": labels,
            "values": [
                [
                    timestamp,
                    sum(values)
                    if source.aggregation == "sum"
                    else max(values)
                    if source.aggregation == "max"
                    else sum(values) / len(values),
                ]
                for timestamp, values in sorted(points.items())
            ],
        }

    return (
        [matrix(deployment, {})] if deployment else [],
        [matrix(points, {"pod": pod}) for pod, points in sorted(pods.items())],
    )


def plugin_device_allocations(pods, namespace, profile):
    """Use kubelet-reported resource identities; never infer ownership from a node."""
    pattern = profile.telemetry.allocation.get("resource_id_pattern")
    if not pattern:
        return {}
    resources = {name for mode in profile.deployment.modes.values() for name in mode.get("resource_names", [])}
    owners = defaultdict(list)
    for pod in pods:
        metadata, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        if metadata.get("namespace") != namespace or not spec.get("nodeName"):
            continue
        for container in status.get("containerStatuses", []):
            for resource in container.get("allocatedResourcesStatus", []):
                if resource.get("name") not in resources:
                    continue
                for device in resource.get("resources", []):
                    match = re.fullmatch(pattern, str(device.get("resourceID", "")).lower())
                    if not match or not match.groupdict().get("pci"):
                        continue
                    owners[(spec["nodeName"], match.group("pci"))].append(
                        {
                            "pod": metadata["name"],
                            "since": max(
                                _timestamp(status.get("startTime")), _timestamp(metadata.get("creationTimestamp"))
                            ),
                        }
                    )
    return {identity: values[0] for identity, values in owners.items() if len({value["pod"] for value in values}) == 1}
