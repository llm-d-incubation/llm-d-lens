"""Apply Lens shared-memory policy to rendered model-server manifests."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from llm_d_lens.core.hashing import stable_hash
from llm_d_lens.deploy.providers.guide_adapter import GuideDeploymentArtifact
from llm_d_lens.infrastructure.artifacts.manifests import text_checksum
from llm_d_lens.infrastructure.artifacts.store import register_artifacts

logger = logging.getLogger(__name__)

DEFAULT_SHM_SIZE = "4Gi"
SHM_PATH = "/dev/shm"  # noqa: S108 - Kubernetes volume mount target, not a temporary file.
_MODEL_CONTAINERS = {"modelserver", "model-server", "vllm", "sglang"}
_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"}


def _pod_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "Pod":
        return [spec]
    if kind in _TEMPLATE_KINDS:
        return [spec.get("template", {}).get("spec", {})]
    if kind == "CronJob":
        return [spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})]
    if kind == "LeaderWorkerSet":
        templates = spec.get("leaderWorkerTemplate") or {}
        return [
            template["spec"]
            for key in ("leaderTemplate", "workerTemplate")
            if isinstance(template := templates.get(key), dict) and isinstance(template.get("spec"), dict)
        ]
    return []


def _model_container(container: dict[str, Any]) -> bool:
    if container.get("name") in _MODEL_CONTAINERS:
        return True
    invocation = " ".join(str(token) for token in [*(container.get("command") or []), *(container.get("args") or [])])
    return any(marker in invocation for marker in ("vllm serve", "vllm.entrypoints.", "sglang.launch_server"))


def prepare_shared_memory_manifest(manifest: str, *, exact: bool = False) -> tuple[str, dict[str, Any]]:
    """Preserve explicit mounts; supply missing ones only for ordinary rendering."""
    documents = list(yaml.safe_load_all(manifest))
    warnings: list[str] = []
    mounts: list[dict[str, Any]] = []
    added = 0

    def visit(document: Any) -> None:
        nonlocal added
        if not isinstance(document, dict):
            return
        if document.get("kind") == "List":
            for item in document.get("items") or []:
                visit(item)
            return
        identity = f"{document.get('kind')}/{document.get('metadata', {}).get('name', '<unnamed>')}"
        for pod in _pod_specs(document):
            volumes = pod.get("volumes") or []
            added_volume: dict[str, Any] | None = None
            for container in pod.get("containers") or []:
                if not _model_container(container):
                    continue
                label = f"{identity}/{container.get('name', '<unnamed>')}"
                existing = [
                    mount
                    for mount in container.get("volumeMounts") or []
                    if str(mount.get("mountPath", "")).rstrip("/") == SHM_PATH
                ]
                if len(existing) > 1:
                    raise ValueError(f"{label} has duplicate /dev/shm mounts")
                entry: dict[str, Any] = {"container": label}
                mounts.append(entry)
                if existing:
                    mount = existing[0]
                    sources = [volume for volume in volumes if volume.get("name") == mount.get("name")]
                    if len(sources) != 1:
                        raise ValueError(f"{label} /dev/shm must reference exactly one declared volume")
                    volume = sources[0]
                    entry.update(mode="existing", volume=volume["name"])
                    if mount.get("readOnly") or mount.get("subPath") or mount.get("subPathExpr"):
                        warnings.append(
                            f"{label}: existing /dev/shm is read-only or uses a subpath; verify suitability."
                        )
                    if (volume.get("emptyDir") or {}).get("medium") == "Memory":
                        limit = volume["emptyDir"].get("sizeLimit")
                        entry["sizeLimit"] = limit
                        if limit is None:
                            warnings.append(
                                f"{label}: memory-backed /dev/shm has no explicit sizeLimit; "
                                "effective capacity depends on Kubernetes and memory limits. Existing mount preserved."
                            )
                    else:
                        warnings.append(
                            f"{label}: /dev/shm uses an existing non-memory-emptyDir volume; "
                            "verify shared-memory capacity and semantics. Existing mount preserved."
                        )
                    continue
                if pod.get("hostIPC"):
                    entry["mode"] = "hostIPC"
                    warnings.append(
                        f"{label}: hostIPC is enabled; host shared memory is preserved, not isolated per Pod."
                    )
                    continue
                if exact:
                    entry["mode"] = "missing"
                    warnings.append(
                        f"{label}: published manifest has no /dev/shm mount; the runtime may provide only 64 MiB. "
                        "Exact manifest preserved; update the configuration before deployment if more space is needed."
                    )
                    continue
                if added_volume is None:
                    name = "lens-shm"
                    suffix = 1
                    while any(volume.get("name") == name for volume in volumes):
                        name = f"lens-shm-{suffix}"
                        suffix += 1
                    added_volume = {"name": name, "emptyDir": {"medium": "Memory", "sizeLimit": DEFAULT_SHM_SIZE}}
                    pod.setdefault("volumes", []).append(added_volume)
                    volumes = pod["volumes"]
                container.setdefault("volumeMounts", []).append({"name": added_volume["name"], "mountPath": SHM_PATH})
                entry.update(mode="added", volume=added_volume["name"], sizeLimit=DEFAULT_SHM_SIZE)
                added += 1

    for document in documents:
        visit(document)
    report = {"defaultSizeLimit": DEFAULT_SHM_SIZE, "addedMounts": added, "mounts": mounts, "warnings": warnings}
    return (yaml.safe_dump_all(documents, sort_keys=False) if added else manifest), report


def prepare_shared_memory(
    artifact: GuideDeploymentArtifact, output_root: Path, *, exact: bool
) -> GuideDeploymentArtifact:
    """Prepare deployment-owned output, never the source Guide checkout."""
    if not artifact.manifest_ref:
        return artifact
    reference = Path(artifact.manifest_ref)
    directory_artifact = reference.is_dir()
    path = reference / "manifest.yaml" if directory_artifact else reference
    manifest = path.read_text(encoding="utf-8")
    rendered, report = prepare_shared_memory_manifest(manifest, exact=exact)
    if not report["mounts"]:
        return artifact
    for warning in report["warnings"]:
        logger.warning("Guide %s: %s", artifact.guide_id, warning)
    checksum = text_checksum(rendered)
    if rendered != manifest:
        if directory_artifact:
            # The legacy directory adapter validates manifest_checksum against its
            # source tree. Keep that contract; bind prepared output in the report.
            path.write_text(rendered, encoding="utf-8")
        else:
            directory = output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed shared-memory manifest drift detected")
            path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
    report["manifestChecksum"] = checksum
    return replace(
        artifact,
        manifest_ref=str(reference if directory_artifact else path),
        manifest_checksum=artifact.manifest_checksum if directory_artifact or exact else checksum,
        artifact_hash=stable_hash({"provider": artifact.artifact_hash, "sharedMemory": report}),
        deployment_contract={**artifact.deployment_contract, "sharedMemory": report},
    )
