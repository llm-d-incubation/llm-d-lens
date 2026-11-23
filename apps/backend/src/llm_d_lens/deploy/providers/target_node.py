"""Pin rendered Kubernetes workloads to one explicitly configured node."""

from __future__ import annotations

import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from llm_d_lens.infrastructure.artifacts.manifests import text_checksum

TARGET_NODE_ENV_VAR = "PRISM_K8S_TARGET_NODE"
_NODE_NAME_PATTERN = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*")
_POD_TEMPLATE_KINDS = {"DaemonSet", "Deployment", "Job", "ReplicaSet", "ReplicationController", "StatefulSet"}


def configured_target_node(environ: dict[str, str] | None = None) -> str | None:
    """Return and validate the configured Kubernetes node name."""
    target_node = (environ or os.environ).get(TARGET_NODE_ENV_VAR, "").strip()
    if not target_node:
        return None
    if len(target_node) > 253 or not _NODE_NAME_PATTERN.fullmatch(target_node):
        raise ValueError(f"{TARGET_NODE_ENV_VAR} must be a valid Kubernetes node name")
    return target_node


def validate_placement(placement: dict[str, Any]) -> None:
    if not isinstance(placement, dict):
        raise ValueError("placement must be a mapping")
    replicas = placement.get("replicas", "none")
    node_label = placement.get("nodeLabel")
    if replicas not in {"none", "same-node", "prefer-same-node", "balanced", "spread"} or set(placement) - {
        "replicas",
        "nodeLabel",
    }:
        raise ValueError("unsupported replica placement preset")
    if node_label is not None and (
        replicas != "same-node"
        or not isinstance(node_label, dict)
        or not isinstance(node_label.get("key"), str)
        or not node_label["key"]
        or not isinstance(node_label.get("value"), str)
    ):
        raise ValueError("node label requires same-node placement and a key/value pair")


def apply_placement(manifest: str, placement: dict[str, Any]) -> tuple[str, int]:
    validate_placement(placement)
    replicas = placement.get("replicas", "none")
    node_label = placement.get("nodeLabel")
    documents = list(yaml.safe_load_all(manifest))
    modelserver_names = sorted(
        str(document.get("metadata", {}).get("name", ""))
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and any(
            isinstance(container, dict) and container.get("name") == "modelserver"
            for container in document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
    )
    modelserver_group = sha256("/".join(modelserver_names).encode()).hexdigest()[:32]
    changed = 0
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") not in {
            "Deployment",
            "LeaderWorkerSet",
            "DisaggregatedSet",
        }:
            continue
        if document["kind"] == "DisaggregatedSet":
            spec = document.get("spec") or {}
            policy = spec.get("placementPolicy") or {}
            slices = spec.get("slices") or 1
            if policy.get("type", "None") != "None":
                if replicas == "same-node" and slices > 1:
                    raise ValueError(
                        "Requiring one node conflicts with slice placement that puts each slice in a different "
                        "topology domain."
                    )
                if replicas in {"balanced", "spread"} and policy.get("topology") == "kubernetes.io/hostname":
                    raise ValueError(
                        "Pod placement that separates replicas conflicts with slice placement that keeps "
                        "each slice on one node; choose prefer-same-node or same-node."
                    )
            templates = [
                template
                for role in spec.get("roles") or []
                if isinstance(role, dict)
                for template in (
                    ((role.get("spec") or {}).get("leaderWorkerTemplate") or {}).get("leaderTemplate"),
                    ((role.get("spec") or {}).get("leaderWorkerTemplate") or {}).get("workerTemplate"),
                )
            ]
        elif document["kind"] == "LeaderWorkerSet":
            if replicas in {"balanced", "spread"}:
                raise ValueError("distributed replica placement is not supported for LeaderWorkerSet")
            group_templates = document.get("spec", {}).get("leaderWorkerTemplate", {})
            templates = [group_templates.get("leaderTemplate"), group_templates.get("workerTemplate")]
        else:
            templates = [document.get("spec", {}).get("template")]
        group = (
            modelserver_group
            if document["kind"] == "Deployment"
            else sha256(str(document.get("metadata", {}).get("name", "")).encode()).hexdigest()[:32]
        )
        selector = {"matchLabels": {"lens.llm-d.ai/placement-group": group}}
        term = {"labelSelector": selector, "topologyKey": "kubernetes.io/hostname"}
        for template in templates:
            if not isinstance(template, dict) or not isinstance(template.get("spec"), dict):
                continue
            pod_spec = template["spec"]
            if document["kind"] == "Deployment" and not any(
                isinstance(container, dict) and container.get("name") == "modelserver"
                for container in pod_spec.get("containers", [])
            ):
                continue
            template.setdefault("metadata", {}).setdefault("labels", {})["lens.llm-d.ai/placement-group"] = group
            if replicas != "balanced":
                affinity = pod_spec.setdefault("affinity", {})
            if replicas == "same-node":
                terms = affinity.setdefault("podAffinity", {}).setdefault(
                    "requiredDuringSchedulingIgnoredDuringExecution", []
                )
                if term not in terms:
                    terms.append(term)
            elif replicas == "balanced":
                constraint = {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "labelSelector": selector,
                }
                constraints = pod_spec.setdefault("topologySpreadConstraints", [])
                if constraint not in constraints:
                    constraints.append(constraint)
            elif replicas == "spread":
                terms = affinity.setdefault("podAntiAffinity", {}).setdefault(
                    "requiredDuringSchedulingIgnoredDuringExecution", []
                )
                if term not in terms:
                    terms.append(term)
            if replicas in {"none", "prefer-same-node"}:
                terms = affinity.setdefault("podAffinity", {}).setdefault(
                    "preferredDuringSchedulingIgnoredDuringExecution", []
                )
                preferred = {"weight": 100, "podAffinityTerm": term}
                if preferred not in terms:
                    terms.append(preferred)
            if node_label is not None:
                selector_labels = pod_spec.setdefault("nodeSelector", {})
                if not isinstance(selector_labels, dict) or (
                    node_label["key"] in selector_labels and selector_labels[node_label["key"]] != node_label["value"]
                ):
                    raise ValueError("selected node label conflicts with the modelserver nodeSelector")
                selector_labels[node_label["key"]] = node_label["value"]
            changed += 1
    if not changed:
        if replicas in {"none", "prefer-same-node"}:
            return manifest, 0
        raise ValueError("deployment manifest contains no supported modelserver Pod templates")
    return yaml.safe_dump_all(documents, sort_keys=False), changed


def pin_manifest_to_node(manifest: str, target_node: str) -> tuple[str, int]:
    """Add a hostname nodeSelector to every workload in a YAML manifest."""
    documents = list(yaml.safe_load_all(manifest))
    pinned = 0

    def pin_resource(resource: Any) -> None:
        nonlocal pinned
        if not isinstance(resource, dict):
            return
        if resource.get("kind") == "List":
            for item in resource.get("items") or []:
                pin_resource(item)
            return

        kind = resource.get("kind")
        spec = resource.get("spec")
        pod_spec: dict[str, Any] | None = None
        if kind == "Pod" and isinstance(spec, dict):
            pod_spec = spec
        elif kind in _POD_TEMPLATE_KINDS and isinstance(spec, dict):
            template = spec.get("template")
            if isinstance(template, dict) and isinstance(template.get("spec"), dict):
                pod_spec = template["spec"]
        elif kind == "CronJob" and isinstance(spec, dict):
            pod_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
        elif kind in {"DisaggregatedSet", "LeaderWorkerSet"} and isinstance(spec, dict):
            if kind == "DisaggregatedSet":
                if (spec.get("slices") or 1) > 1 and (spec.get("placementPolicy") or {}).get("type", "None") != "None":
                    raise ValueError("Target node conflicts with slice placement in different topology domains")
                groups = [
                    (role.get("spec") or {}).get("leaderWorkerTemplate") or {} for role in spec.get("roles") or []
                ]
            else:
                groups = [spec.get("leaderWorkerTemplate") or {}]
            for group in groups:
                for name in ("leaderTemplate", "workerTemplate"):
                    template = group.get(name)
                    if isinstance(template, dict):
                        pin_resource({"kind": "Pod", "spec": template.get("spec")})
            return

        if isinstance(pod_spec, dict):
            selector = pod_spec.setdefault("nodeSelector", {})
            if not isinstance(selector, dict):
                raise ValueError(f"{kind} nodeSelector must be a mapping")
            if (
                selector.get("kubernetes.io/hostname", target_node) != target_node
                or pod_spec.get("nodeName", target_node) != target_node
            ):
                raise ValueError(f"{kind} target node conflicts with the existing node selection")
            selector["kubernetes.io/hostname"] = target_node
            pinned += 1

    for document in documents:
        pin_resource(document)
    if not pinned:
        raise ValueError("deployment manifest contains no pinnable Kubernetes workloads")
    return yaml.safe_dump_all(documents, sort_keys=False), pinned


def write_pinned_manifest(manifest_ref: str, output_root: Path, target_node: str) -> tuple[str, str, int]:
    """Write a content-addressed pinned copy without mutating provider output."""
    manifest = Path(manifest_ref).read_text(encoding="utf-8")
    rendered, pinned = pin_manifest_to_node(manifest, target_node)
    checksum = text_checksum(rendered)
    directory = output_root.resolve() / checksum.removeprefix("sha256:")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.yaml"
    if path.is_file() and path.read_text(encoding="utf-8") != rendered:
        raise ValueError("checksum-addressed pinned deployment manifest drift detected")
    if not path.is_file():
        path.write_text(rendered, encoding="utf-8")
    from llm_d_lens.infrastructure.artifacts.store import register_artifacts

    register_artifacts(
        directory,
        owner_type="deployment-manifest",
        owner_id=checksum.removeprefix("sha256:"),
        retention_class="configuration",
    )
    return str(path), checksum, pinned
