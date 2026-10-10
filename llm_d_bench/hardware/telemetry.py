"""Consume a hardware profile's telemetry config for scoped metric queries.

``device_metrics`` (full PromQL) drives the cluster overview. Consumers that
must inject a namespace/pod selector (profiling) or parse scraped text
(evaluate) instead read ``device_metric_sources``: a bare metric name plus a
unit, scale and any required label matchers. A missing/empty entry means the
hardware does not report that metric, so callers skip it (empty == disabled).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DeviceMetricSource, HardwareProfile


def device_metric_source(profile: HardwareProfile | None, name: str) -> DeviceMetricSource | None:
    """The profile's configured source for ``name``, or None when disabled."""
    if profile is None or profile.telemetry is None:
        return None
    source = profile.telemetry.device_metric_sources.get(name)
    if source is None or not source.metric:
        return None
    return source


def _selector(*matcher_maps: Mapping[str, str] | None) -> str:
    items: dict[str, str] = {}
    for matchers in matcher_maps:
        for key, value in (matchers or {}).items():
            if value:
                items[str(key)] = str(value)
    if not items:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in items.items()) + "}"


def scoped_device_query(
    source: DeviceMetricSource,
    *,
    aggregation: str,
    match: Mapping[str, str] | None = None,
    by: str | None = None,
) -> str:
    """``<aggregation>(<metric>{<match>}) [by (<by>)] [* <scale>]``."""
    expression = f"{aggregation}({source.metric}{_selector(source.match, match)})"
    if by:
        expression += f" by ({by})"
    if source.scale != 1.0:
        expression += f" * {source.scale:g}"
    return expression


def combined_device_query(
    profiles: Iterable[HardwareProfile],
    name: str,
    *,
    aggregation: str,
    match: Mapping[str, str] | None = None,
    by: str | None = None,
) -> str:
    """OR-combine one metric across every profile that configures it.

    A single cluster runs one vendor, so only one branch returns samples; ``or``
    keeps it a single PromQL expression and ``""`` means no profile declares it.
    """
    expressions = [
        scoped_device_query(source, aggregation=aggregation, match=match, by=by)
        for profile in profiles
        if (source := device_metric_source(profile, name)) is not None
    ]
    if not expressions:
        return ""
    return " or ".join(f"({expression})" for expression in expressions)


# Public result semantics; exporter-specific names and conversions stay in profiles.
DEVICE_RESULT_KEYS = {
    "utilization": "gpu_utilization_percent",
    "framebuffer_used": "gpu_framebuffer_used_bytes",
    "vram": "gpu_memory_total_bytes",
    "power": "gpu_power_watts",
    "temperature": "gpu_temperature_celsius",
    "memory_utilization": "gpu_memory_utilization_ratio",
    "memory_read_throughput": "gpu_memory_read_throughput_bytes",
    "memory_write_throughput": "gpu_memory_write_throughput_bytes",
    "memory_bandwidth_utilization": "gpu_memory_bandwidth_utilization_percent",
}


def parse_device_metrics(payload: str | None, profile: HardwareProfile | None) -> dict | None:
    """Normalize a direct exporter snapshot using every declared metric source."""
    import json
    import math
    import re
    from datetime import UTC, datetime

    if not payload or profile is None or profile.telemetry is None:
        return None
    telemetry = profile.telemetry
    devices: dict = {}
    labels_schema = telemetry.label_schema
    pattern = re.compile(r"^([\w:]+)(?:\{(.*)\})?\s+([-+\deE.]+)(?:\s+\S+)?$")
    for line in payload.splitlines():
        sample = pattern.match(line.strip())
        if not sample:
            continue
        labels = {
            key: json.loads('"' + value + '"')
            for key, value in re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', sample[2] or "")
        }
        value = float(sample[3])
        if not math.isfinite(value):
            continue
        identity = next(
            (
                labels.get(labels_schema.get(key, ""))
                for key in ("pci", "uuid", "fallback_device", "device")
                if labels.get(labels_schema.get(key, ""))
            ),
            None,
        )
        if identity is None:
            continue
        node = labels.get(labels_schema.get("node", ""), "")
        for semantic, source in telemetry.device_metric_sources.items():
            if source.metric != sample[1] or any(labels.get(key) != expected for key, expected in source.match.items()):
                continue
            device = devices.setdefault(
                (node, identity),
                {"id": identity, "node": node, "name": labels.get(labels_schema.get("name", "")), "metrics": {}},
            )
            tile = labels.get(labels_schema.get("device", ""), "")
            device["metrics"].setdefault(semantic, {})[tile] = value * source.scale
    results = []
    fields = {
        "framebuffer_used": "memory_used_bytes",
        "vram": "memory_total_bytes",
        "power": "power_watts",
        "temperature": "temperature_celsius",
    }
    for device in devices.values():
        for semantic, tiles in device.pop("metrics").items():
            source = telemetry.device_metric_sources[semantic]
            values = [tiles[""]] if "" in tiles else list(tiles.values())
            value = (
                sum(values)
                if source.tile_aggregation == "sum"
                else max(values)
                if source.tile_aggregation == "max"
                else sum(values) / len(values)
            )
            if semantic in {"utilization", "memory_utilization", "memory_bandwidth_utilization"}:
                device[semantic + "_ratio"] = value / 100 if source.output_unit == "percent" else value
            else:
                device[fields.get(semantic, semantic)] = value
        results.append(device)
    if not results:
        return None
    utilization = [item["utilization_ratio"] for item in results if "utilization_ratio" in item]
    return {
        "source": telemetry.provider_id,
        "hardware_profile": profile.id,
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "cluster-device",
        "device_count": len(results),
        "devices": results,
        "average_utilization_ratio": sum(utilization) / len(utilization) if utilization else None,
        "maximum_utilization_ratio": max(utilization) if utilization else None,
    }


def workload_telemetry_profiles(profiles, pods, claims):
    """Select telemetry attribution from actual workload resource access modes."""
    from dataclasses import replace

    from .resolver import deployment_mode

    drivers = {
        result.get("driver")
        for claim in claims
        for result in claim.get("status", {}).get("allocation", {}).get("devices", {}).get("results", [])
    }
    resources = {
        key
        for pod in pods
        for container in pod.get("spec", {}).get("containers", [])
        for group in ("requests", "limits")
        for key in container.get("resources", {}).get(group, {})
    }
    selected = []
    for profile in profiles:
        if not profile.telemetry:
            continue
        requests = set()
        if drivers.intersection(profile.device_classes):
            requests.add("dra")
        if (
            any(
                resource in resources
                for mode in profile.deployment.modes.values()
                for resource in mode.get("resource_names", [])
            )
            or profile.deployment.resource_name in resources
        ):
            requests.add("extended-resource")
        if len(requests) > 1:
            # Do not mix exporter labels with allocated-device attribution.
            selected.append(replace(profile, telemetry=replace(profile.telemetry, allocation_join="none")))
        elif requests:
            mode = deployment_mode(profile, request_model=next(iter(requests)))
            settings = profile.telemetry.modes.get(mode.get("access_mode"), {})
            selected.append(
                replace(
                    profile,
                    telemetry=replace(
                        profile.telemetry,
                        allocation_join=settings.get("allocation_join", profile.telemetry.allocation_join),
                    ),
                )
            )
    return selected
