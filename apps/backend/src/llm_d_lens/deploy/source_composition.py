"""Render selected Guide inputs and validate the exact, editable deployment snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import tarfile
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path, PurePosixPath

import yaml

from llm_d_lens.cluster.registry import validate_container_image_ref
from llm_d_lens.configuration.manifest_facts import _invocation_tokens, workload_roles
from llm_d_lens.deploy.data_plane import PLAINTEXT_EPP_VALUES_PATH
from llm_d_lens.deploy.providers.hardware_profile import resolve_cluster_vllm_image
from llm_d_lens.hardware.registry import all_profiles
from llm_d_lens.hardware.resolver import resolve_by_device_class, resolve_by_resource
from llm_d_lens.infrastructure.kubernetes import run_kubectl
from llm_d_lens.infrastructure.storage.paths import storage_path

from .guide_planning import GuideError
from .guide_runtime import apply_runtime_overrides, configure_model_server, runtime_overrides
from .guide_sources import require_internal_permission, validate_planning_session
from .source_catalog import DEFINITION, authorized_inventory, checksum, command, documents, source_facts, source_file

logger = logging.getLogger(__name__)
# Uvicorn's handler is the only INFO sink configured for this service.
timing_logger = logging.getLogger("uvicorn.error").getChild("guide_composition")
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}

CLUSTER_KINDS = {
    "Namespace",
    "Node",
    "PersistentVolume",
    "StorageClass",
    "CustomResourceDefinition",
    "ClusterRole",
    "ClusterRoleBinding",
    "GatewayClass",
    "RuntimeClass",
    "PriorityClass",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "APIService",
    "ClusterQueue",
    "ResourceFlavor",
}


def dump(parsed: list[dict]) -> str:
    return yaml.safe_dump_all(parsed, sort_keys=False, width=100000)


def text_input(value, limit=5_000_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise GuideError(f"Provide nonempty YAML text below {limit} characters.")
    return value


def scalar_edits(parsed: list[dict], edits: list | None = None) -> list[dict]:
    result = deepcopy(parsed)
    if not isinstance(edits or [], list) or len(edits or []) > 10000:
        raise GuideError("Invalid parameter edits.")
    for edit in edits or []:
        path = edit.get("path") if isinstance(edit, dict) else None
        if (
            not isinstance(path, list)
            or not path
            or any(type(key) not in {str, int} or key in ("__proto__", "constructor", "prototype") for key in path)
        ):
            raise GuideError("Invalid parameter path.")
        owner = result
        for key in path:
            if (
                isinstance(owner, list)
                and type(key) is int
                and 0 <= key < len(owner)
                or isinstance(owner, dict)
                and key in owner
            ):
                child = owner[key]
            else:
                raise GuideError("Parameter path no longer exists. Reload source.")
            owner = child
        previous = owner
        value = edit.get("value")
        if type(previous) not in {str, int, float, bool} or type(value) is not type(previous):
            raise GuideError("Parameter type does not match source.")
        if isinstance(value, float) and not math.isfinite(value):
            raise GuideError("Parameter must be finite.")
        owner = result
        for key in path[:-1]:
            owner = owner[key]
        owner[path[-1]] = value
    return result


def transform(value, replace):
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, list):
        return [transform(item, replace) for item in value]
    if isinstance(value, dict):
        return {key: transform(item, replace) for key, item in value.items()}
    return value


def substitute(parsed: list[dict], catalog: dict, asset: dict, selection: dict) -> list[dict]:
    guide = next((item for item in catalog["guides"] if item["id"] == asset["guide"]), {})
    variables = dict(catalog.get("environment", {}))
    for key, spec in guide.get("variables", {}).items():
        value = spec.get("default") if isinstance(spec, dict) else spec
        if isinstance(value, (str, int, float)):
            variables[key] = str(value)
    variables.update(selection.get("variables") or {})
    if selection.get("namespace"):
        variables["NAMESPACE"] = selection["namespace"]

    def expand(text):
        if text == "REPLACE_MODEL_SERVER_IMAGE" and selection.get("runtimeImage"):
            return selection["runtimeImage"]
        return re.sub(
            r"\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)\b",
            lambda match: str(variables.get(match[1] or match[2], match[0])),
            text,
        )

    for _ in range(12):
        variables = {key: expand(str(value)) for key, value in variables.items()}
    return transform(parsed, expand)


def scratch_directory():
    base = storage_path("scratch", "guide-composition")
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="render-", dir=base)


async def chart_archive(chart: str, version: str) -> Path:
    """Return the pinned chart archive, pulling it once per (chart, version) into the tool cache."""
    if not re.fullmatch(r"oci://ghcr\.io/[\w.-]+/[\w./-]+", chart) or ".." in chart.split("/"):
        raise GuideError("Only revision-declared GHCR OCI charts are supported.")
    if not re.fullmatch(r"[v0-9][A-Za-z0-9._+-]{0,80}", version or ""):
        raise GuideError("Inspect the chart and pin a concrete version before generating.")
    key = hashlib.sha256(f"{chart}@{version}".encode()).hexdigest()[:32]
    target = storage_path("cache", "helm-charts", key)
    async with _CACHE_LOCKS.setdefault(str(target), asyncio.Lock()):
        archives = list(target.glob("*.tgz")) if target.is_dir() else []
        if len(archives) == 1:
            return archives[0]
        started = time.perf_counter()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=target.parent))
        try:
            await command(["helm", "pull", chart, "--version", version, "--destination", str(staging)])
            archives = list(staging.glob("*.tgz"))
            if len(archives) != 1:
                raise GuideError("Chart pull did not return one archive.", 502)
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        timing_logger.info("Pulled Helm chart %s:%s in %.1fs", chart, version, time.perf_counter() - started)
        return target / archives[0].name


async def source_checkout(root: Path, commit: str) -> Path:
    """Return a read-only extracted checkout of `commit`, built once per source and commit."""
    key = hashlib.sha256(f"{root.resolve()}@{commit}".encode()).hexdigest()[:32]
    target = storage_path("cache", "guide-sources", key)
    async with _CACHE_LOCKS.setdefault(str(target), asyncio.Lock()):
        # The directory appears only by atomic rename, so its presence means a complete extraction.
        if (target / "source").is_dir():
            return target / "source"
        started = time.perf_counter()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=target.parent))
        try:
            archive = staging / "source.tar"
            await command(["git", "-C", str(root), "archive", "--format=tar", "--output", str(archive), commit])
            checkout = staging / "source"
            checkout.mkdir()
            with tarfile.open(archive) as source:
                if any(
                    member.issym() or member.islnk() or member.name.startswith("/") or ".." in Path(member.name).parts
                    for member in source.getmembers()
                ):
                    raise GuideError("Source archives containing links or escaping paths are not supported.")
                source.extractall(checkout, filter="data")
            archive.unlink()
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        timing_logger.info("Extracted Guide source %s in %.1fs", commit[:12], time.perf_counter() - started)
        return target / "source"


async def shared_result(shared: dict | None, key: tuple, factory):
    """Resolve `factory` once per key within one multi-candidate request."""
    if shared is None:
        return await factory()
    if key not in shared:
        shared[key] = asyncio.ensure_future(factory())
    return await asyncio.shield(shared[key])


def binding_key(body: dict) -> tuple:
    return ("binding", body.get("clusterId"), body.get("modelCacheEntryId"), body.get("namespace"))


def validate_local_kustomization(root: Path, entry: Path, visited: set[Path] | None = None) -> None:
    visited = set() if visited is None else visited
    resolved = entry.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise GuideError("Kustomize input escapes the pinned checkout.")
    if resolved in visited:
        return
    visited.add(resolved)
    if resolved.is_dir():
        resolved = next(
            (
                resolved / name
                for name in ("kustomization.yaml", "kustomization.yml", "Kustomization")
                if (resolved / name).is_file()
            ),
            resolved,
        )
    if not resolved.is_file():
        raise GuideError("Kustomize input is missing from the pinned checkout.")
    parsed = documents(resolved.read_text(encoding="utf-8"))
    if resolved.name.lower() not in {"kustomization.yaml", "kustomization.yml", "kustomization"}:
        return
    for doc in parsed:
        for reference in [*doc.get("resources", []), *doc.get("components", []), *doc.get("bases", [])]:
            if not isinstance(reference, str) or re.search(r"://|[?$]|^git::", reference):
                raise GuideError("Remote or dynamic Kustomize references require a pinned local source adapter.")
            validate_local_kustomization(root, resolved.parent / reference, visited)


def _is_llm_d_router_chart(chart: str) -> bool:
    """Return True when the Helm chart reference names an llm-d router chart."""
    if not isinstance(chart, str) or not chart.strip():
        return False
    return PurePosixPath(chart.strip().rstrip("/")).name in {
        "llm-d-router-standalone",
        "llm-d-router-gateway",
    }


def _explicitly_sets_secure_serving(value_docs: list[dict], set_overrides: list[str]) -> bool:
    """Return True when caller values or --set entries explicitly set router.epp.flags.secure-serving."""
    for doc in value_docs:
        if not isinstance(doc, Mapping):
            continue
        router = doc.get("router")
        if not isinstance(router, Mapping):
            continue
        epp = router.get("epp")
        if not isinstance(epp, Mapping):
            continue
        flags = epp.get("flags")
        if isinstance(flags, Mapping) and "secure-serving" in flags:
            return True
    for entry in set_overrides:
        if not isinstance(entry, str):
            continue
        for part in re.split(r"(?<!\\),", entry):
            key, sep, _value = part.partition("=")
            if sep and key.strip().replace("\\-", "-") == "router.epp.flags.secure-serving":
                return True
    return False


def _container_has_plaintext_epp_flag(container: Mapping) -> bool:
    tokens = [str(item) for item in [*(container.get("command") or []), *(container.get("args") or [])]]
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered == "--secure-serving=false":
            return True
        if lowered == "--secure-serving" and index + 1 < len(tokens) and tokens[index + 1].lower() == "false":
            return True
    return False


def _reconcile_envoy_ext_proc_transport(parsed: list[dict]) -> None:
    """Strip TLS transport settings from Envoy ext_proc when EPP runs with --secure-serving=false."""
    has_plaintext_epp = False
    scoped_cm_names: set[str] = set()
    fallback_cm_names: set[str] = set()
    for doc in parsed:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        pod = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        if not isinstance(pod, dict):
            continue
        containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
        epp_plaintext = any(c.get("name") == "epp" and _container_has_plaintext_epp_flag(c) for c in containers)
        if epp_plaintext:
            has_plaintext_epp = True
        volume_to_cm = {
            vol["name"]: cm_name
            for vol in (pod.get("volumes") or [])
            if isinstance(vol, dict)
            and isinstance(vol.get("name"), str)
            and isinstance((vol.get("configMap") or {}).get("name"), str)
            and (cm_name := vol["configMap"]["name"])
        }
        envoy_cms = {
            volume_to_cm[mount["name"]]
            for container in containers
            if container.get("name") == "envoy-proxy"
            for mount in (container.get("volumeMounts") or [])
            if isinstance(mount, dict) and mount.get("name") in volume_to_cm
        }
        if epp_plaintext:
            scoped_cm_names.update(envoy_cms)
        else:
            fallback_cm_names.update(envoy_cms)
    if not has_plaintext_epp:
        return
    target_cm_names = scoped_cm_names or fallback_cm_names
    if not target_cm_names:
        return
    for doc in parsed:
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        name = (doc.get("metadata") or {}).get("name")
        if name not in target_cm_names:
            continue
        data = doc.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("envoy.yaml"), str):
            continue
        try:
            envoy_cfg = yaml.safe_load(data["envoy.yaml"])
        except yaml.YAMLError:
            continue
        if not isinstance(envoy_cfg, dict):
            continue
        clusters = (envoy_cfg.get("static_resources") or {}).get("clusters")
        if not isinstance(clusters, list):
            continue
        modified = False
        for cluster in clusters:
            if not isinstance(cluster, dict) or cluster.get("name") != "ext_proc":
                continue
            ts = cluster.get("transport_socket")
            if isinstance(ts, dict) and ts.get("name") == "envoy.transport_sockets.tls":
                cluster.pop("transport_socket", None)
                modified = True
            health_checks = cluster.get("health_checks")
            if isinstance(health_checks, list):
                for hc in health_checks:
                    if isinstance(hc, dict) and "tls_options" in hc:
                        hc.pop("tls_options", None)
                        modified = True
        if modified:
            data["envoy.yaml"] = yaml.safe_dump(envoy_cfg, sort_keys=False)


async def render_unit(root: Path, catalog: dict, selection: dict) -> dict:
    asset = next((item for item in catalog["assets"] if item["path"] == selection.get("path")), None)
    if not asset or asset["kind"] == "values":
        raise GuideError("Select a resource, chart or Kustomize source from the authorized catalog.")
    extras = {}
    if asset["kind"] in {"kustomize", "component"}:
        pristine = await source_checkout(root, catalog["commit"])
        with scratch_directory() as temporary:
            directory = Path(temporary)
            checkout = pristine
            if asset.get("requiredFiles"):
                # Generator inputs are request-specific; never write them into the shared checkout.
                checkout = directory / "source"
                await asyncio.to_thread(shutil.copytree, pristine, checkout)
            for name in asset.get("requiredFiles", []):
                content = (selection.get("files") or {}).get(name)
                target = (checkout / asset["path"] / name).resolve()
                if not target.is_relative_to(checkout):
                    raise GuideError("Generator input escapes source checkout.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text_input(content, 2_000_000), encoding="utf-8")
                target.chmod(0o600)
            entry = checkout / asset["path"]
            validate_local_kustomization(checkout, entry)
            if asset["kind"] == "component":
                wrapper = directory / "component"
                wrapper.mkdir()
                (wrapper / "kustomization.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "kustomize.config.k8s.io/v1beta1",
                            "kind": "Kustomization",
                            "components": [str(entry)],
                        }
                    ),
                    encoding="utf-8",
                )
                entry = wrapper
            content = await command(["kubectl", "kustomize", str(entry)])
    elif asset["kind"] == "chart":
        with scratch_directory() as temporary:
            directory = Path(temporary)
            version = selection.get("chartVersion") or asset.get("version")
            if not version:
                version = documents(await command(["helm", "show", "chart", asset["chart"]]))[0].get("version")
            archive = await chart_archive(asset["chart"], version)
            values = selection.get("values") or await command(["helm", "show", "values", str(archive)])
            base_value_docs = documents(text_input(values))
            caller_value_docs = (
                [doc for doc in base_value_docs if isinstance(doc, dict)] if selection.get("values") else []
            )
            value_file = directory / "values.yaml"
            value_file.write_text(values, encoding="utf-8")
            args = [
                "helm",
                "template",
                asset["release"],
                str(archive),
                "--include-crds",
                "--namespace",
                selection.get("namespace") or "default",
                "-f",
                str(value_file),
            ]
            for index, name in enumerate(selection.get("chartValues") or []):
                layer = next(
                    (item for item in catalog["assets"] if item["path"] == name and item["kind"] == "values"), None
                )
                if not layer:
                    raise GuideError("Unknown chart values source.")
                caller_value_docs.extend(doc for doc in documents(layer["content"]) if isinstance(doc, dict))
                file = directory / f"layer-{index}.yaml"
                file.write_text(layer["content"], encoding="utf-8")
                args.extend(["-f", str(file)])
            if _is_llm_d_router_chart(asset["chart"]) and not _explicitly_sets_secure_serving(
                caller_value_docs, selection.get("chartSet") or []
            ):
                args.extend(["-f", str(PLAINTEXT_EPP_VALUES_PATH)])
            for value in selection.get("chartSet") or []:
                if not isinstance(value, str) or "=" not in value:
                    raise GuideError("Invalid chart override.")
                args.extend(["--set", value])
            content = await command(args)
            extras = {
                "values": values,
                "chartVersion": version,
                "chartChecksum": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
    else:
        source_file(root, asset["file"])
        content = asset["content"]
    parsed = substitute(documents(content), catalog, asset, selection)
    if "replacementContent" in selection:
        parsed = documents(text_input(selection["replacementContent"], 2_000_000))
    parsed = scalar_edits(parsed, selection.get("edits"))
    if asset["kind"] == "chart" and _is_llm_d_router_chart(asset["chart"]):
        _reconcile_envoy_ext_proc_transport(parsed)
    return dict(path=asset["path"], checksum=asset["checksum"], documents=parsed, content=dump(parsed), **extras)


def compose_documents(groups: list[list[dict]], namespace: str) -> list[dict]:
    if (
        not isinstance(namespace, str)
        or len(namespace) > 63
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
    ):
        raise GuideError("Enter a valid Kubernetes namespace.")
    by_identity = {}
    scopes = {
        (doc.get("spec", {}).get("group"), doc.get("spec", {}).get("names", {}).get("kind")): doc.get("spec", {}).get(
            "scope"
        )
        for group in groups
        for doc in group
        if doc.get("kind") == "CustomResourceDefinition"
    }

    def add(document):
        if not isinstance(document, dict):
            raise GuideError("Composition documents must be mappings.")
        if document.get("kind") == "List":
            for child in document.get("items", []):
                add(child)
            return
        doc = deepcopy(document)
        if (
            not isinstance(doc.get("apiVersion"), str)
            or not isinstance(doc.get("kind"), str)
            or not isinstance(doc.get("metadata"), dict)
            or not isinstance(doc["metadata"].get("name"), str)
            or not doc["metadata"]["name"]
        ):
            raise GuideError("Composition contains a patch or values file instead of a named resource.")
        group = doc["apiVersion"].split("/")[0] if "/" in doc["apiVersion"] else ""
        if doc["kind"] not in CLUSTER_KINDS and scopes.get((group, doc["kind"])) != "Cluster":
            doc["metadata"].setdefault("namespace", namespace)
            if doc["metadata"]["namespace"] != namespace:
                raise GuideError("All namespaced resources must target the selected deployment namespace.")
        identity = (group, doc["kind"], doc["metadata"].get("namespace", ""), doc["metadata"]["name"])
        if identity in by_identity and by_identity[identity] != doc:
            raise GuideError(f"Conflicting definitions for {doc['kind']}/{doc['metadata']['name']}.")
        by_identity[identity] = doc

    for group in groups:
        for doc in group:
            add(doc)
    return list(by_identity.values())


async def model_binding(request, body: dict) -> dict:
    from llm_d_lens.model_cache.router import get_entry
    from llm_d_lens.storage.router import get_volume

    entry_id = body.get("modelCacheEntryId")
    if not isinstance(entry_id, str) or not entry_id:
        raise GuideError("Select a ready cached model.")
    await require_internal_permission(request, f"/api/v1/model-cache/entries/{entry_id}")
    entry = await get_entry(entry_id, request)
    volume_id = entry["storageVolumeId"]
    await require_internal_permission(request, f"/api/v1/storage/volumes/{volume_id}")
    volume = await get_volume(volume_id, request)
    namespace = (
        (volume.get("dynamicPvc") or {}).get("namespace")
        or (volume.get("localDisk") or {}).get("namespace")
        or (volume.get("nfs") or {}).get("namespace")
        or "llm-d-bench-storage"
    )
    if entry["clusterId"] != body["clusterId"] or volume["clusterId"] != body["clusterId"]:
        raise GuideError("Cached model and storage must belong to the selected cluster.")
    if (
        entry["status"] != "ready"
        or volume["status"] != "ready"
        or entry.get("pendingSyncNodes")
        or any(node.get("status") != "ready" for node in entry.get("nodeProgress", []))
    ):
        raise GuideError("Model cache and storage must be ready and synchronized.", 409)
    source = entry.get("source", {})
    if source.get("kind") != "huggingface" or not source.get("huggingface", {}).get("repoId"):
        raise GuideError("The cached model has no deployable repository identity.")
    if body.get("namespace") != namespace or not volume.get("pvcName"):
        raise GuideError(f"Use the selected cache PVC namespace: {namespace}.")
    return {
        "entryId": entry_id,
        "storageVolumeId": volume_id,
        "namespace": namespace,
        "pvcName": volume["pvcName"],
        "model": source["huggingface"]["repoId"],
        "revision": source["huggingface"].get("revision") or "main",
    }


def bind_model(parsed: list[dict], binding: dict, image: str | None = None) -> None:
    count = 0
    for workload in workload_roles(parsed):
        leader = workload["spec"].get("leaderWorkerTemplate", {}).get("leaderTemplate", {}).get("spec", {})
        if any(container.get("name") == "modelserver" for container in leader.get("containers", [])):
            raise GuideError("Model-serving leader templates require a dedicated cache and runtime adapter.")
        pod = workload["pod"]
        for container in pod.get("containers", []):
            if container.get("name") != "modelserver":
                continue
            if workload["document"]["metadata"].get("namespace") != binding["namespace"]:
                raise GuideError("Model workloads and cache PVC must share a namespace.")
            configure_model_server(container, binding["model"], 0, binding["model"], False)
            apply_runtime_overrides(container, [{"kind": "argument", "name": "revision", "value": binding["revision"]}])
            if image:
                container["image"] = image
            name = "lens-model-cache"
            volume = {"name": name, "persistentVolumeClaim": {"claimName": binding["pvcName"], "readOnly": True}}
            existing = next((item for item in pod.get("volumes", []) if item.get("name") == name), None)
            if existing is not None and existing != volume:
                raise GuideError("Conflicting lens-model-cache volume.")
            mount = {"name": name, "mountPath": "/model-cache", "readOnly": True}
            mounts = container.setdefault("volumeMounts", [])
            if any(
                item != mount and (item.get("mountPath") == "/model-cache" or item.get("name") == name)
                for item in mounts
            ):
                raise GuideError("Conflicting /model-cache mount.")
            if existing is None:
                pod.setdefault("volumes", []).append(volume)
            if mount not in mounts:
                mounts.append(mount)
            env = {"HF_HOME": "/model-cache", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
            container["env"] = [
                item
                for item in container.get("env", [])
                if item.get("name") not in {*env, "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}
            ]
            container["env"].extend({"name": key, "value": value} for key, value in env.items())
            count += 1
    if not count:
        raise GuideError("No supported modelserver workload was found for the cached model.")

    def identity(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"model", "modelName", "model_name", "baseModel", "llm-d.ai/model-id"} and isinstance(
                    child, str
                ):
                    value[key] = binding["model"]
                elif key == "llm-d.ai/model" and isinstance(child, str):
                    value[key] = re.sub(r"[^A-Za-z0-9_.-]", "-", binding["model"].split("/")[-1])[:63].rstrip("._-")
                else:
                    identity(child)
            if (
                isinstance(value.get("name"), str)
                and value["name"] in {"MODEL", "MODEL_NAME", "MODEL_ID"}
                and isinstance(value.get("value"), str)
            ):
                value["value"] = binding["model"]
        elif isinstance(value, list):
            for child in value:
                identity(child)

    identity(parsed)


def pod_claim_templates(parsed: list[dict], pod: dict, namespace: str) -> list[dict]:
    claims = []
    for reference in pod.get("resourceClaims", []):
        name = reference.get("resourceClaimTemplateName")
        claim = next(
            (
                doc
                for doc in parsed
                if doc.get("kind") == "ResourceClaimTemplate"
                and doc.get("metadata", {}).get("name") == name
                and doc.get("metadata", {}).get("namespace", "default") == namespace
            ),
            None,
        )
        if not name or claim is None:
            raise GuideError("DRA sizing requires a generated ResourceClaimTemplate; external claims are unsupported.")
        requests = claim.get("spec", {}).get("spec", {}).get("devices", {}).get("requests", [])
        if not requests or any(
            not request.get("exactly")
            or request["exactly"].get("allocationMode", "ExactCount") != "ExactCount"
            or request.get("firstAvailable")
            for request in requests
        ):
            raise GuideError("DRA sizing supports only explicit ExactCount template requests.")
        for request in requests:
            positive_int(request["exactly"].get("count", 1), "DRA device count")
        if claim not in claims:
            claims.append(claim)
    return claims


def apply_runtime(parsed: list[dict], runtime: dict, model: str) -> None:
    if not runtime:
        return
    profile = next((item for item in all_profiles() if item.id == runtime.get("profileId")), None)
    if not profile:
        raise GuideError("A registered hardware profile is required.")
    rows = runtime_overrides({**runtime, "modelServer": "vllm"})
    counts = defaultdict(int)
    claim_sizes = {}
    for workload in workload_roles(parsed):
        engines = [
            container for container in workload["pod"].get("containers", []) if container.get("name") == "modelserver"
        ]
        if not engines:
            continue
        if workload["kind"] != "Deployment":
            group = workload["spec"].get("leaderWorkerTemplate", {})
            if group.get("size", 1) != 1 or group.get("leaderTemplate") or workload["slices"] != 1:
                raise GuideError("Automatic topology sizing requires single-node, single-slice roles without a leader.")
        prefill = workload["role"] == "prefill"
        role = "prefill" if prefill else "decode"
        counts[role] += 1
        tp = positive_int(runtime.get("prefillTp" if prefill else "tp"), f"{role} TP")
        workload["spec"]["replicas"] = positive_int(
            runtime.get("prefillReplicas" if prefill else "replicas"), f"{role} replicas"
        )
        claims = pod_claim_templates(
            parsed, workload["pod"], workload["document"].get("metadata", {}).get("namespace", "default")
        )
        matching = [
            (claim, request)
            for claim in claims
            for request in claim["spec"]["spec"]["devices"]["requests"]
            if request["exactly"].get("deviceClassName") in profile.device_classes
        ]
        if claims and len(matching) != 1:
            raise GuideError("DRA sizing requires one unambiguous accelerator request matching the hardware profile.")
        if matching:
            from .providers.hardware_profile import set_accelerator_request

            claim = matching[0][0]
            references = {
                item["name"]
                for item in workload["pod"]["resourceClaims"]
                if item.get("resourceClaimTemplateName") == claim["metadata"]["name"]
            }
            if any(
                not any(
                    item.get("name") in references
                    and item.get("request", matching[0][1]["name"]) == matching[0][1]["name"]
                    for item in engine.get("resources", {}).get("claims", [])
                )
                for engine in engines
            ):
                raise GuideError("The model server must consume the DRA accelerator request being resized.")
            identity = (claim["metadata"].get("namespace", "default"), claim["metadata"]["name"])
            if identity in claim_sizes and claim_sizes[identity] != tp:
                raise GuideError("A shared DRA template cannot use different tensor parallel sizes.")
            claim_sizes[identity] = tp
            set_accelerator_request(engines[0], claim, tp, accelerator=profile.id)
        for container in engines:
            configure_model_server(container, model, tp, model, False)
            apply_runtime_overrides(container, [row for row in rows if row.get("target") in {"both", role}])
            for group in ("requests", "limits"):
                for key in container.get("resources", {}).get(group, {}):
                    owner = resolve_by_resource(key)
                    if owner and owner.id == profile.id:
                        container["resources"][group][key] = tp
    if counts["decode"] != 1 or counts["prefill"] != (1 if runtime.get("prefillReplicas") is not None else 0):
        raise GuideError("Rendered serving roles do not match the requested topology.")


def router_controls(parsed: list[dict], catalog: dict, overrides: dict) -> None:
    defaults = []
    for asset in catalog["assets"]:
        if asset["guide"] != "optimized-baseline" or asset["kind"] != "values":
            continue
        for layer in documents(asset.get("content") or "{}"):
            for text in layer.get("router", {}).get("epp", {}).get("pluginsCustomConfig", {}).values():
                for config in documents(text):
                    defaults.extend(config.get("plugins", []))
    for doc in parsed:
        if doc.get("kind") != "ConfigMap":
            continue
        for name, text in doc.get("data", {}).items():
            if not re.search(r"\.ya?ml$|\.json$", name) or not isinstance(text, str):
                continue
            try:
                configs = documents(text)
            except GuideError:
                continue  # Non-YAML application data is not an EPP policy.
            for config in configs:
                if config.get("kind") != "EndpointPickerConfig":
                    continue
                plugins = config.setdefault("plugins", [])
                for identifier, types in (
                    ("affinity", ["approx-prefix-cache-producer", "prefix-cache-affinity-filter"]),
                    ("load-routing", ["inflight-load-producer", "token-load-scorer"]),
                ):
                    if identifier not in overrides:
                        continue
                    if overrides[identifier]:
                        for plugin_type in types:
                            plugin = next((entry for entry in plugins if entry.get("type") == plugin_type), None)
                            if plugin is None:
                                definition = next(
                                    (entry for entry in defaults if entry.get("type") == plugin_type), None
                                )
                                if definition is None:
                                    raise GuideError(f"{identifier}: plugin is absent from this source revision.")
                                plugin = deepcopy(definition)
                                plugins.append(plugin)
                            if plugin_type == types[-1]:
                                ref = plugin.get("name") or plugin["type"]
                                for profile in config.get("schedulingProfiles", []):
                                    if identifier == "affinity" and profile.get("name") not in {"default", "prefill"}:
                                        continue
                                    refs = profile.setdefault("plugins", [])
                                    if not any(entry.get("pluginRef") == ref for entry in refs):
                                        refs.append({"pluginRef": ref})
                    else:
                        removed = {
                            entry.get("name") or entry.get("type")
                            for entry in plugins
                            if entry.get("type") == types[-1]
                        }
                        plugins[:] = [entry for entry in plugins if entry.get("type") != types[-1]]
                        for profile in config.get("schedulingProfiles", []):
                            profile["plugins"] = [
                                entry for entry in profile.get("plugins", []) if entry.get("pluginRef") not in removed
                            ]
                            if not profile["plugins"]:
                                raise GuideError("A routing profile must retain at least one scheduling policy.")
                doc["data"][name] = dump(configs)


def engine_controls(parsed: list[dict], overrides: dict, intent: dict) -> None:
    for workload in workload_roles(parsed):
        for container in workload["pod"].get("containers", []):
            if container.get("name") != "modelserver":
                continue
            words = _invocation_tokens(container, workload["name"])
            switches = [
                (identifier, flag)
                for identifier, flag in (("prefix", "prefix-caching"), ("chunked", "chunked-prefill"))
                if identifier in overrides
            ]
            if switches:
                if not words or words[0].startswith("--"):
                    raise GuideError("Independent engine switches require a direct vLLM serve invocation.")
                container["command"], container["args"] = ["vllm", "serve"], words
                for identifier, flag in switches:
                    cleaned, skip = [], False
                    for index, value in enumerate(container["args"]):
                        if skip:
                            skip = False
                            continue
                        if re.match(rf"^--(?:no-)?enable-{flag}(?:=|$)", value):
                            skip = (
                                "=" not in value
                                and index + 1 < len(container["args"])
                                and (container["args"][index + 1] in {"true", "false"})
                            )
                        else:
                            cleaned.append(value)
                    container["args"] = cleaned
                    container["args"].append(f"--{'no-' if not overrides[identifier] else ''}enable-{flag}")
            controls = [
                (key, flag)
                for key, flag in (("contextLength", "max-model-len"), ("concurrency", "max-num-seqs"))
                if intent.get(key) not in (None, "")
            ]
            apply_runtime_overrides(
                container,
                [
                    {"kind": "argument", "name": flag, "value": str(positive_int(intent[key], key))}
                    for key, flag in controls
                ],
            )


def positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
        raise GuideError(f"{name} must be a positive integer.")
    return int(value)


def validate_engine_parameters(parsed: list[dict]) -> None:
    positive = {
        "tensor-parallel-size",
        "pipeline-parallel-size",
        "data-parallel-size",
        "prefill-context-parallel-size",
        "max-model-len",
        "max-num-seqs",
        "max-num-batched-tokens",
        "block-size",
    }
    for workload in workload_roles(parsed):
        for container in workload["pod"].get("containers", []):
            if container.get("name") != "modelserver":
                continue
            words = _invocation_tokens(container, workload["name"])
            seen = set()
            for index, word in enumerate(words):
                name, _, value = word.removeprefix("--").partition("=")
                if not word.startswith("--") or name not in positive | {"gpu-memory-utilization"}:
                    continue
                if name in seen:
                    raise GuideError(f"Duplicate --{name} parameters.")
                seen.add(name)
                value = value or (words[index + 1] if index + 1 < len(words) else "")
                if name in positive:
                    positive_int(value, name)
                elif not re.fullmatch(r"\d+(?:\.\d+)?", value) or not 0 < float(value) <= 1:
                    raise GuideError("gpu-memory-utilization must be greater than zero and at most one.")


def rendered_optimizations(parsed: list[dict], commit: str) -> dict:
    evidence = []
    mounted = set()
    for workload in workload_roles(parsed):
        for volume in workload["pod"].get("volumes", []):
            if volume.get("configMap", {}).get("name"):
                mounted.add(volume["configMap"]["name"])
        for container in workload["pod"].get("containers", []):
            if container.get("name") == "modelserver":
                fact = source_facts(
                    f"{workload['kind']}/{workload['name']}/{workload['role']}/modelserver", [container]
                )
                fact["role"] = workload["role"]
                evidence.append(fact)
    for doc in parsed:
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") in mounted:
            for name, text in doc.get("data", {}).items():
                if not re.search(r"\.ya?ml$|\.json$", name) or not isinstance(text, str):
                    continue
                try:
                    configs = documents(text)
                except GuideError:
                    continue
                for config in configs:
                    if config.get("kind") != "EndpointPickerConfig":
                        continue
                    refs = {
                        plugin.get("pluginRef")
                        for profile in config.get("schedulingProfiles", [])
                        for plugin in profile.get("plugins", [])
                    }
                    config["plugins"] = [
                        plugin
                        for plugin in config.get("plugins", [])
                        if (plugin.get("name") or plugin.get("type")) in refs
                    ]
                    evidence.append(source_facts(f"ConfigMap/{doc['metadata']['name']}/{name}", [config]))
    roles = sorted({fact["role"] for fact in evidence if "role" in fact})
    features = []
    for identifier, _ in DEFINITION["detectors"]:
        enabled = [fact["path"] for fact in evidence if identifier in fact["features"]]
        disabled = [fact["path"] for fact in evidence if identifier in fact["disabled"]]
        state = (
            "mixed"
            if enabled and disabled
            else "configured"
            if enabled
            else ("disabled" if disabled else "not-declared")
        )
        features.append(
            {
                "id": identifier,
                "state": state,
                "paths": enabled + disabled,
                "evidence": "rendered-manifest",
                "reason": "Explicit final YAML configuration; engine defaults and runtime are not verified.",
            }
        )
    features.append(
        {
            "id": "transfer",
            "state": "configured" if "prefill" in roles and "decode" in roles else "not-declared",
            "paths": [fact["path"] for fact in evidence if "role" in fact],
            "evidence": "rendered-manifest",
            "reason": "Separate Prefill/Decode roles are evaluated from the final YAML.",
        }
    )
    return {
        "schemaVersion": DEFINITION["schemaVersion"],
        "commit": commit,
        "checksum": checksum(dump(parsed)),
        "features": features,
        "roles": roles,
        "evidence": evidence,
        "unresolved": [],
    }


def pod_demand(pod: dict) -> dict[str, float]:
    def resources(container):
        requests = container.get("resources", {}).get("requests", {})
        limits = container.get("resources", {}).get("limits", {})
        result = {}
        for key in requests.keys() | limits.keys():
            if resolve_by_resource(key):
                value = max(float(requests.get(key, 0)), float(limits.get(key, 0)))
                if not math.isfinite(value) or value < 0 or not value.is_integer():
                    raise GuideError("Accelerator requests must be nonnegative integers.")
                result[key] = value
        return result

    total = defaultdict(float)
    for container in pod.get("containers", []):
        for key, value in resources(container).items():
            total[key] += value
    restartable, init_peak = defaultdict(float), defaultdict(float)
    for container in pod.get("initContainers", []):
        current = resources(container)
        if container.get("restartPolicy") == "Always":
            for key, value in current.items():
                restartable[key] += value
                total[key] += value
        for key in current.keys() | restartable.keys():
            init_peak[key] = max(
                init_peak[key],
                current.get(key, 0) + restartable[key]
                if container.get("restartPolicy") != "Always"
                else restartable[key],
            )
    return {key: max(total[key], init_peak[key]) for key in total.keys() | init_peak.keys()}


def accelerator_demand(parsed: list[dict]) -> tuple[dict[str, float], list[str]]:
    total, warnings = defaultdict(float), []
    covered = set()
    scaling = {}
    for doc in parsed:
        if doc.get("kind") in {"ScaledObject", "HorizontalPodAutoscaler"}:
            spec = doc.get("spec", {})
            target = spec.get("scaleTargetRef", {})
            key = (target.get("kind", "Deployment"), target.get("name"))
            if key in scaling:
                raise GuideError("Multiple autoscalers target the same workload.")
            scaling[key] = positive_int(spec.get("maxReplicaCount", spec.get("maxReplicas", 100)), "autoscaler max")
    for workload in workload_roles(parsed):
        if not re.fullmatch(r"\d+", str(workload["replicas"])):
            raise GuideError("Replicas must be a nonnegative integer.")
        replicas = max(int(workload["replicas"]), scaling.get((workload["kind"], workload["name"]), 0))
        template = workload["spec"].get("leaderWorkerTemplate", {})
        size = positive_int(template.get("size", 1), "worker group size")
        copies = positive_int(workload["slices"], "slices") * replicas
        pods = [(workload["pod"], copies * (size - 1 if template.get("leaderTemplate") else size))]
        if template.get("leaderTemplate"):
            pods.append((template["leaderTemplate"].get("spec", {}), copies))
        for pod, count in pods:
            covered.add(id(pod))
            namespace = workload["document"].get("metadata", {}).get("namespace", "default")
            for claim in pod_claim_templates(parsed, pod, namespace):
                for request in claim["spec"]["spec"]["devices"]["requests"]:
                    exactly = request["exactly"]
                    profile = resolve_by_device_class(exactly.get("deviceClassName", ""))
                    if profile:
                        total[f"dra:{profile.id}"] += positive_int(exactly.get("count", 1), "DRA device count") * count
            for key, value in pod_demand(pod).items():
                total[key] += value * count

    def check_layout(value):
        if isinstance(value, dict):
            if (
                isinstance(value.get("containers"), list)
                and id(value) not in covered
                and (pod_demand(value) or value.get("resourceClaims"))
            ):
                raise GuideError("Device budget validation does not support this additional workload layout.")
            for child in value.values():
                check_layout(child)
        elif isinstance(value, list):
            for child in value:
                check_layout(child)

    check_layout(parsed)
    return dict(total), warnings


def resource_references(parsed: list[dict]) -> list[dict]:
    external = {}
    workloads = workload_roles(parsed)
    for workload in list(workloads):
        leader = workload["spec"].get("leaderWorkerTemplate", {}).get("leaderTemplate")
        if leader:
            workloads.append(
                {**workload, "pod": leader.get("spec", {}), "pod_labels": leader.get("metadata", {}).get("labels", {})}
            )

    def require(kind, name, namespace):
        if not name or any(
            doc.get("kind") == kind
            and doc.get("metadata", {}).get("name") == name
            and doc["metadata"].get("namespace", namespace) == namespace
            for doc in parsed
        ):
            return
        external[(kind, namespace, name)] = {"kind": kind, "name": name, "namespace": namespace}

    for workload in workloads:
        namespace = workload["document"]["metadata"]["namespace"]
        pod = workload["pod"]
        for claim in pod_claim_templates(parsed, pod, namespace):
            for request in claim["spec"]["spec"]["devices"]["requests"]:
                require("DeviceClass", request["exactly"].get("deviceClassName"), None)
        if pod.get("serviceAccountName") != "default":
            require("ServiceAccount", pod.get("serviceAccountName"), namespace)
        for volume in pod.get("volumes", []):
            for key, kind, name in (
                ("secret", "Secret", "secretName"),
                ("configMap", "ConfigMap", "name"),
                ("persistentVolumeClaim", "PersistentVolumeClaim", "claimName"),
            ):
                ref = volume.get(key, {})
                if not ref.get("optional"):
                    require(kind, ref.get(name), namespace)
        for container in [*pod.get("containers", []), *pod.get("initContainers", [])]:
            for env in container.get("env", []):
                for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                    ref = env.get("valueFrom", {}).get(key, {})
                    if not ref.get("optional"):
                        require(kind, ref.get("name"), namespace)
            for env in container.get("envFrom", []):
                for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                    ref = env.get(key, {})
                    if not ref.get("optional"):
                        require(kind, ref.get("name"), namespace)
    for doc in parsed:
        namespace = doc["metadata"].get("namespace", "default")
        spec = doc.get("spec", {})
        if doc["kind"] == "HTTPRoute":
            for ref in spec.get("parentRefs", []):
                require(ref.get("kind", "Gateway"), ref.get("name"), ref.get("namespace", namespace))
            for rule in spec.get("rules", []):
                for ref in rule.get("backendRefs", []):
                    require(ref.get("kind", "Service"), ref.get("name"), ref.get("namespace", namespace))
        elif doc["kind"] == "InferencePool":
            selector = spec.get("selector", {})
            selector = selector.get("matchLabels", selector)
            if not selector or not any(
                workload["document"]["metadata"].get("namespace") == namespace
                and all(workload["pod_labels"].get(key) == value for key, value in selector.items())
                for workload in workloads
            ):
                raise GuideError(f"InferencePool/{doc['metadata']['name']} selects no generated workload.")
            endpoint = spec.get("endpointPickerRef", {})
            require(endpoint.get("kind", "Service"), endpoint.get("name"), namespace)
        elif doc["kind"] in {"ScaledObject", "HorizontalPodAutoscaler"}:
            target = spec.get("scaleTargetRef", {})
            require(target.get("kind", "Deployment"), target.get("name"), namespace)
    return list(external.values())


async def check_target(request, body: dict, parsed: list[dict]) -> dict:
    demand, warnings = accelerator_demand(parsed)
    intent = body.get("deploymentIntent") or {}
    budget = intent.get("maxDevices")
    if budget not in (None, ""):
        limit = positive_int(budget, "Device budget")
        if sum(demand.values()) > limit:
            raise GuideError(f"Deployment requires {sum(demand.values()):g} devices, above the budget of {limit}.")
    session_id = body.get("clusterSessionId")
    if not session_id:
        raise GuideError("An authorized cluster session is required for resource checks.")
    session = await validate_planning_session(request, session_id)
    if session.server_id != body["clusterId"]:
        raise GuideError("Cluster and session do not match.", 409)
    from .slice_capacity import (
        ClusterInventoryForbiddenError,
        is_forbidden_result,
        is_unauthorized_result,
        prometheus_inventory_nodes_and_pods,
    )

    for ref in resource_references(parsed):
        args = ["get", ref["kind"], ref["name"]]
        if ref["namespace"] is not None:
            args.extend(["-n", ref["namespace"]])
        result = await run_kubectl(
            [*args, "-o", "name"],
            timeout=15,
            cluster_id=session_id,
        )
        if not result.ok:
            if is_unauthorized_result(result):
                raise GuideError("Cluster kubeconfig is unauthorized (401).", 401)
            location = f" in {ref['namespace']}" if ref["namespace"] is not None else ""
            if is_forbidden_result(result):
                warnings.append(f"Could not verify external {ref['kind']}/{ref['name']}{location} (403 Forbidden).")
                continue
            raise GuideError(f"Required {ref['kind']}/{ref['name']} is unavailable{location}.")
    available = defaultdict(float)
    nodes = await run_kubectl(["get", "nodes", "-o", "json"], timeout=20, cluster_id=session_id)
    pods = await run_kubectl(["get", "pods", "--all-namespaces", "-o", "json"], timeout=20, cluster_id=session_id)
    for response in (nodes, pods):
        if not response.ok and is_unauthorized_result(response):
            raise GuideError("Cluster kubeconfig is unauthorized (401).", 401)
    inventory_source = "kubernetes"
    if not nodes.ok or not pods.ok:
        if (not nodes.ok and is_forbidden_result(nodes)) or (not pods.ok and is_forbidden_result(pods)):
            try:
                node_items, pod_items = await prometheus_inventory_nodes_and_pods(session.server_id)
            except ClusterInventoryForbiddenError as error:
                raise GuideError("Could not verify current cluster device availability.", 403) from error
            inventory_source = "prometheus"
            warnings.append("Node capacity derived from Prometheus metrics (cluster-wide Kubernetes read restricted).")
        else:
            raise GuideError("Could not verify current cluster device availability.", 502)
    else:
        node_items = json.loads(nodes.stdout).get("items", [])
        pod_items = json.loads(pods.stdout).get("items", [])
    constrained = any(
        workload["kind"] == "DisaggregatedSet"
        or workload["pod"].get("nodeSelector")
        or workload["pod"].get("nodeName")
        or workload["pod"].get("affinity", {}).get("nodeAffinity")
        for workload in workload_roles(parsed)
    )
    if constrained:
        from .placement_preflight import placement_preflight

        placement = placement_preflight(parsed, node_items, pod_items, inventory_source=inventory_source)
        if placement["status"] == "fail":
            raise GuideError(
                "Placement validation failed: " + "; ".join(item["message"] for item in placement["errors"])
            )
        warnings.extend(placement["unmodeled"])
    for node in node_items:
        if node.get("spec", {}).get("unschedulable") or not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        ):
            continue
        for key, value in node.get("status", {}).get("allocatable", {}).items():
            if resolve_by_resource(key):
                available[key] += float(value)
    for pod in pod_items:
        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            for key, value in pod_demand(pod.get("spec", {})).items():
                available[key] -= value
    dra_profiles = {key.removeprefix("dra:") for key in demand if key.startswith("dra:")}
    if dra_profiles:
        from llm_d_lens.cluster import registry
        from llm_d_lens.cluster.service import build_overview

        overview = await build_overview(registry.require_cluster(session.server_id))
        hardware = overview.get("hardware") or {}
        detected = {item["id"] for item in hardware.get("accelerators", []) if item.get("gpuCount", 0) > 0}
        if len(dra_profiles) != 1 or detected != dra_profiles:
            raise GuideError("DRA availability requires an unambiguous matching cluster hardware profile.")
        profile_id = next(iter(dra_profiles))
        free = hardware.get("usableGpuCount")
        if isinstance(free, bool) or not isinstance(free, int) or free < 0:
            raise GuideError("Cluster inventory did not establish usable DRA device capacity.")
        shared = sum(
            value for key, value in demand.items() if (owner := resolve_by_resource(key)) and owner.id == profile_id
        )
        available[f"dra:{profile_id}"] = max(0, free - shared)
    for key, count in demand.items():
        if count > available[key]:
            raise GuideError(f"{key}: requires {count:g}; only {max(0, available[key]):g} currently available.")
    return {"demand": demand, "available": dict(available), "warnings": warnings, "level": "device-count"}


async def render_router(catalog: dict, body: dict) -> list[dict]:
    router = body.get("router") or {}
    if not router.get("values"):
        return []
    if router.get("mode") not in {"standalone", "gateway"} or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{0,52}", router.get("release", "")
    ):
        raise GuideError("Configure a valid router mode and release.")
    with scratch_directory() as temporary:
        directory = Path(temporary)
        chart = f"oci://ghcr.io/llm-d/charts/llm-d-router-{router['mode']}"
        archive = await chart_archive(chart, router.get("version"))
        args = ["helm", "template", router["release"], str(archive), "--include-crds", "--namespace", body["namespace"]]
        parsed_value_docs: list[dict] = []
        for index, layer in enumerate(router["values"]):
            asset = next(
                (item for item in catalog["assets"] if item["path"] == layer.get("path") and item["kind"] == "values"),
                None,
            )
            if not asset:
                raise GuideError("Unknown router values source.")
            content = layer.get("content", asset["content"])
            values = documents(content)
            if len(values) > 1:
                raise GuideError("Router values must be one YAML mapping.")
            parsed_value_docs.extend(doc for doc in values if isinstance(doc, dict))
            overrides = (body.get("guideSelection") or {}).get("capabilities") or {}
            if "tracing" in overrides:
                value = values[0] if values else {}
                value.setdefault("router", {}).setdefault("tracing", {})["enabled"] = overrides["tracing"]
                content = dump([value])
            file = directory / f"values-{index}.yaml"
            file.write_text(content, encoding="utf-8")
            args.extend(["-f", str(file)])
        if _is_llm_d_router_chart(chart) and not _explicitly_sets_secure_serving(
            parsed_value_docs, router.get("set") or []
        ):
            args.extend(["-f", str(PLAINTEXT_EPP_VALUES_PATH)])
        for value in router.get("set") or []:
            if not isinstance(value, str) or "=" not in value:
                raise GuideError("Invalid router override.")
            args.extend(["--set", value])
        rendered_docs = documents(await command(args))
        _reconcile_envoy_ext_proc_transport(rendered_docs)
        return rendered_docs


async def resolve_slice_layout(request, body: dict, parsed: list[dict]) -> dict | None:
    """Apply explicit slice settings and fill omitted ones from the cluster's free topology domains."""
    from .slice_placement import (
        apply_layout,
        disaggregated_sets,
        node_inventory,
        pod_placement_conflict,
        recommend_layout,
        validate_policy,
        validate_slices,
    )

    runtime = body.get("runtimeConfiguration") or {}
    sets = disaggregated_sets(parsed)
    if not sets:
        if (
            runtime.get("slices") not in (None, 1)
            or (runtime.get("placementPolicy") or {}).get("type", "None") != "None"
        ):
            raise GuideError("Slices and slice placement apply only to P/D DisaggregatedSet roles.")
        return None
    if len(sets) != 1:
        raise GuideError("Slice sizing supports one DisaggregatedSet per deployment.")
    slices = validate_slices(runtime["slices"]) if runtime.get("slices") is not None else None
    policy = validate_policy(runtime["placementPolicy"]) if runtime.get("placementPolicy") is not None else None
    pod_placement = runtime.get("podPlacement")
    if pod_placement not in {None, "none", "prefer-same-node", "same-node", "balanced", "spread"}:
        raise GuideError("Unsupported Pod placement.")
    current = validate_slices(sets[0].get("spec", {}).get("slices") or 1)
    if slices is None and not runtime:
        slices = current
    roles = defaultdict(int)
    for workload in workload_roles(parsed):
        if workload["kind"] == "DisaggregatedSet":
            roles[workload["role"]] += positive_int(workload["replicas"], f"{workload['role']} replicas")
    session_id = body.get("clusterSessionId")
    if not session_id:
        raise GuideError("An authorized cluster session is required for slice placement.")
    session = await validate_planning_session(request, session_id)
    if session.server_id != body["clusterId"]:
        raise GuideError("Cluster and session do not match.", 409)
    nodes = await node_inventory(session_id)
    demand = sum(accelerator_demand(parsed)[0].values()) / current
    objective = (body.get("deploymentIntent") or {}).get("objective", "balanced")
    recommended = recommend_layout(
        nodes,
        prefill_replicas=roles["prefill"],
        decode_replicas=roles["decode"],
        slice_devices=demand,
        objective=objective,
        slices=slices,
        pod_placement=pod_placement,
    )
    layout = {
        "slices": slices or recommended["slices"],
        "placementPolicy": policy or recommended["placementPolicy"],
    }
    conflict = pod_placement_conflict(pod_placement, layout["slices"], layout["placementPolicy"])
    if conflict:
        raise GuideError(conflict)
    split = 1 if slices is not None else layout["slices"]
    warnings = []
    topology = layout["placementPolicy"].get("topology")
    if topology:
        if not any(topology in node["labels"] for node in nodes):
            raise GuideError(f"No ready node carries the slice topology label {topology}.")
        if policy and recommended["placementPolicy"] != policy:
            warnings.append(
                f"Slice placement {policy['type']} on {topology} differs from the recommendation; "
                "hard co-location may leave Pods Pending if no domain fits a whole slice."
            )
    applied_layout = {**layout, "eligibleDomains": recommended.get("eligibleDomains", [])} if policy is None else layout
    apply_layout(parsed, applied_layout, split=split)
    return {
        **layout,
        "prefillReplicas": roles["prefill"] // split,
        "replicas": roles["decode"] // split,
        "explicit": {"slices": runtime.get("slices") is not None, "placementPolicy": policy is not None},
        "recommended": recommended,
        "warnings": warnings,
    }


def apply_reviewed_placement(parsed: list[dict], runtime: dict, placement: dict | None = None) -> list[dict]:
    from .providers.target_node import apply_placement, configured_target_node, pin_manifest_to_node

    content = dump(parsed)
    if placement is None and runtime.get("podPlacement") is not None:
        placement = {"replicas": runtime["podPlacement"]}
        if runtime.get("nodeLabel"):
            placement["nodeLabel"] = runtime["nodeLabel"]
    if placement is not None:
        content, _ = apply_placement(content, placement)
    target = configured_target_node()
    if target:
        content, _ = pin_manifest_to_node(content, target)
    return documents(content)


async def compose(request, body: dict, shared: dict | None = None) -> dict:
    if not isinstance(body.get("clusterId"), str) or not body["clusterId"]:
        raise GuideError("Select a target cluster.")
    compose_documents([], body.get("namespace"))
    for field in ("sourcePlan", "guideSelection", "deploymentIntent", "variables", "router"):
        if body.get(field) is not None and not isinstance(body[field], dict):
            raise GuideError(f"{field} must be a mapping.")
    if not re.fullmatch(r"[a-f0-9]{40}", body.get("commit", "")):
        raise GuideError("Pin the catalog commit before generating.")
    try:
        explicit_image = validate_container_image_ref(body.get("runtimeImage"), field="runtimeImage")
    except ValueError as error:
        raise GuideError(str(error)) from error
    explicit_override = bool(body.get("runtimeImageOverride")) and bool(explicit_image)
    started = last = time.perf_counter()
    timings: dict[str, float] = {}

    def lap(stage: str) -> None:
        nonlocal last
        now = time.perf_counter()
        timings[stage] = round(timings.get(stage, 0) + now - last, 2)
        last = now

    root, catalog = await shared_result(
        shared,
        ("inventory", body["clusterId"], body.get("clusterSessionId"), body["commit"]),
        lambda: authorized_inventory(request, body),
    )
    binding = await shared_result(shared, binding_key(body), lambda: model_binding(request, body))
    lap("inputs")
    units = body.get("units")
    if not isinstance(units, list) or not 1 <= len(units) <= 60 or any(not isinstance(unit, dict) for unit in units):
        raise GuideError("Select between 1 and 60 source components.")
    intent = body.get("deploymentIntent") or {}
    if intent.get("scenario", "chat") not in {"chat", "agent", "batch", "multimodal"} or intent.get(
        "objective", "balanced"
    ) not in {"balanced", "latency", "throughput", "cost", "reliability"}:
        raise GuideError("Select a supported workload scenario and objective.")
    for key in ("contextLength", "concurrency", "maxDevices"):
        if intent.get(key) not in (None, ""):
            positive_int(intent[key], key)
    groups, provenance = [], []
    plan = body.get("sourcePlan") or {}
    if plan.get("workloadPatches") or plan.get("existingModelIntegration") or plan.get("existingPoolIntegration"):
        raise GuideError("Existing-workload mutations require a separate operational workflow.")
    cluster_vllm_image = resolve_cluster_vllm_image(body["clusterId"])
    effective_unit_image = explicit_image or cluster_vllm_image
    for selection in units:
        asset = next((item for item in catalog["assets"] if item["path"] == selection.get("path")), None)
        if not asset:
            raise GuideError("Unknown source component.")
        selected = {
            **selection,
            "namespace": body["namespace"],
            "runtimeImage": effective_unit_image,
            "variables": (body.get("variables") or {}).get(asset["guide"], selection.get("variables")),
        }
        rendered = await render_unit(root, catalog, selected)
        replacements = [item for item in plan.get("replacements", []) if selection["path"] in item.get("paths", [])]

        def replace(text, replacements=replacements):
            for replacement in replacements:
                if not isinstance(replacement.get("from"), str) or not replacement["from"]:
                    raise GuideError("Invalid source substitution.")
                text = text.replace(replacement["from"], str(replacement.get("to", "")))
            return text

        groups.append(transform(rendered["documents"], replace))
        provenance.append({"path": selection["path"], "checksum": checksum(rendered["content"])})
    lap("units")
    groups.append(await render_router(catalog, body))
    groups.append(plan.get("documents") or [])
    lap("router")

    def namespace_value(text):
        for name in plan.get("sourceNamespaces") or []:
            text = body["namespace"] if text == name else text.replace(f".{name}.svc", f".{body['namespace']}.svc")
        return text

    parsed = compose_documents(transform(groups, namespace_value), body["namespace"])
    if not any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == body["namespace"] for doc in parsed):
        parsed.insert(0, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": body["namespace"]}})
    for doc in parsed:
        if doc["kind"] == "Namespace" and doc["metadata"]["name"] == body["namespace"]:
            labels = plan.get("namespaceLabels") or {}
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
                raise GuideError("Namespace labels must be string pairs.")
            doc["metadata"]["labels"] = {**doc["metadata"].get("labels", {}), **labels}
    overrides = (body.get("guideSelection") or {}).get("capabilities") or {}
    if any(type(value) is not bool for value in overrides.values()):
        raise GuideError("Optimization overrides must be booleans.")
    router_controls(parsed, catalog, overrides)
    engine_controls(parsed, overrides, intent)
    if body.get("nativeCpuCacheGiB") is not None:
        from llm_d_lens.configuration.guide_composition import compose_native_cpu_cache

        variant = (body.get("guideSelection") or {}).get("accelerator")
        donor = next(
            (
                asset
                for asset in catalog["assets"]
                if asset["kind"] == "kustomize"
                and asset["guide"] == "tiered-prefix-cache"
                and f"/{variant}/" in asset["path"]
                and "/vllm/" in asset["path"]
                and re.search(r"/native/cpu(?:/base)?$", asset["path"])
            ),
            None,
        )
        if donor is None:
            raise GuideError("The pinned source has no native CPU cache adapter for this accelerator.")
        rendered = await render_unit(
            root,
            catalog,
            {
                "path": donor["path"],
                "namespace": body["namespace"],
                "runtimeImage": effective_unit_image,
                "variables": (body.get("variables") or {}).get("tiered-prefix-cache"),
            },
        )
        guide = (
            "pd-disaggregation"
            if any(item["role"] == "prefill" for item in workload_roles(parsed))
            else "optimized-baseline"
        )
        parsed = compose_native_cpu_cache(
            parsed, rendered["documents"], guide=guide, capacity_gib=body["nativeCpuCacheGiB"]
        )
        provenance.append({"path": donor["path"], "checksum": checksum(rendered["content"])})
        lap("cpu_cache")
    bind_model(parsed, binding, explicit_image if explicit_override else cluster_vllm_image)
    apply_runtime(parsed, body.get("runtimeConfiguration"), binding["model"])
    slice_layout = await resolve_slice_layout(request, body, parsed)
    lap("slice_layout")
    validate_engine_parameters(parsed)
    parsed = apply_reviewed_placement(parsed, body.get("runtimeConfiguration") or {}, body.get("_placement"))
    base_checksum = checksum(dump(parsed))
    if "finalConfiguration" in body and body["finalConfiguration"] is not None:
        edited = body["finalConfiguration"]
        if not isinstance(edited, dict) or edited.get("baseChecksum") != base_checksum:
            raise GuideError("Configuration changed. Reload before editing final YAML.", 409)
        parsed = compose_documents([documents(text_input(edited.get("content")))], body["namespace"])
        if not any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == body["namespace"] for doc in parsed):
            raise GuideError("Final YAML must retain the deployment namespace.")
        bind_model(parsed, binding, explicit_image if explicit_override else None)
        validate_engine_parameters(parsed)
        parsed = apply_reviewed_placement(parsed, body.get("runtimeConfiguration") or {}, body.get("_placement"))

    def resolved(text):
        if re.search(r"\$\{[A-Z][A-Z0-9_]*\}|REPLACE_MODEL_SERVER_IMAGE", text):
            raise GuideError("Final YAML contains unresolved source placeholders.")
        return text

    transform(parsed, resolved)
    for workload in workload_roles(parsed):
        for container in workload["pod"].get("containers", []):
            if container.get("name") == "modelserver":
                try:
                    validate_container_image_ref(container.get("image"), field="modelserver.image", allow_empty=False)
                except ValueError as error:
                    raise GuideError(str(error)) from error
    resource_references(parsed)
    facts = rendered_optimizations(parsed, catalog["commit"])
    for identifier in ("prefix", "chunked", "affinity", "load-routing"):
        if identifier not in overrides:
            continue
        state = next(item["state"] for item in facts["features"] if item["id"] == identifier)
        if (
            overrides[identifier]
            and state != "configured"
            or not overrides[identifier]
            and state in {"configured", "mixed"}
        ):
            raise GuideError(f"{identifier}: final YAML does not match the explicit optimization selection.")
    hardware = await check_target(request, body, parsed)
    lap("resource_check")
    content = dump(parsed)
    validation = {
        "errors": [],
        "warnings": hardware["warnings"],
        "level": "rendered",
        "clusterValidated": False,
        "hardware": hardware,
        "runtimeVerified": False,
    }
    if slice_layout:
        validation["warnings"] = [*validation["warnings"], *slice_layout["warnings"]]
    if body.get("validateCluster"):
        with scratch_directory() as temporary:
            file = Path(temporary) / "deployment.yaml"
            file.write_text(content, encoding="utf-8")
            file.chmod(0o600)
            result = await run_kubectl(
                ["apply", "--dry-run=server", "--validate=strict", "-f", str(file)],
                timeout=90,
                cluster_id=body["clusterSessionId"],
            )
            if not result.ok:
                from .slice_capacity import is_forbidden_result, is_unauthorized_result

                if is_unauthorized_result(result):
                    raise GuideError("Cluster kubeconfig is unauthorized (401).", 401)
                if is_forbidden_result(result):
                    validation["warnings"] = [
                        *validation["warnings"],
                        "Cluster admission dry-run skipped (namespace or resource permissions not yet granted).",
                    ]
                else:
                    raise GuideError(
                        "Kubernetes admission dry-run failed. Check required CRDs, namespaces and references."
                    )
            else:
                validation.update(clusterValidated=True, level="admission")
        lap("admission")
    timing_logger.info(
        "Guide composition units=%s stage_seconds=%s total=%.1fs",
        [unit.get("path") for unit in units],
        timings,
        time.perf_counter() - started,
    )
    return {
        "commit": catalog["commit"],
        "compositionChecksum": base_checksum,
        "resourceCount": len(parsed),
        "optimizations": facts,
        "validation": validation,
        "sliceLayout": slice_layout,
        "files": [
            {"name": "deployment.yaml", "content": content},
            {
                "name": "provenance.json",
                "content": json.dumps(
                    {
                        "commit": catalog["commit"],
                        "components": provenance,
                        "modelBinding": binding,
                        "deploymentIntent": intent,
                        "checksum": facts["checksum"],
                        "optimizations": facts,
                        "validation": validation,
                    },
                    indent=2,
                ),
            },
        ],
    }
