"""Deploy checksum-bound manifests published by Configuration."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import shlex
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from llm_d_lens.configuration.disaggregated_set import validate_disaggregated_sets
from llm_d_lens.configuration.manifest_facts import (
    _invocation_tokens,
    _option,
    vllm_argument_value,
    workload_roles,
)
from llm_d_lens.core.hashing import stable_hash
from llm_d_lens.deploy.providers.deployment_bundle import validate_deployment_bundle
from llm_d_lens.deploy.providers.guide_adapter import (
    GuideAdapter,
    GuideDefinition,
    GuideDeploymentArtifact,
    ValidationResult,
    set_vllm_argument,
    set_vllm_shell_argument,
)
from llm_d_lens.deploy.providers.hardware_profile import (
    accelerator_supported,
    discover_runtime_classes,
    prepare_runtime_class_manifest,
)
from llm_d_lens.deploy.providers.shared_memory import (
    _model_container,
    _pod_specs,
    prepare_shared_memory,
)
from llm_d_lens.deploy.providers.target_node import apply_placement, configured_target_node, write_pinned_manifest
from llm_d_lens.infrastructure.artifacts.manifests import (
    DEPLOYMENT_MANIFEST_DIR,
    text_checksum,
    validate_configuration_manifest_content,
)
from llm_d_lens.infrastructure.artifacts.store import register_artifacts
from llm_d_lens.monitoring.kv_trace.manifest import instrument_manifest
from llm_d_lens.storage.service import ensure_mount_in_namespace, get_ready_volume

logger = logging.getLogger(__name__)

KV_LOAD_FAILURE_POLICY = "recompute"
MODEL_SHUTDOWN_TIMEOUT_SECONDS = 90
MODEL_TERMINATION_GRACE_PERIOD_SECONDS = 120
MODEL_PRESTOP_SLEEP_SECONDS = 5
_MODEL_NAME_PREFIXES = ("modelserver", "model-server")
_VLLM_MARKERS = ("vllm serve", "vllm.entrypoints.")
_SGLANG_MARKERS = ("sglang.launch_server", "sglang serve")


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [value]
    return list(value or [])


def _container_invocation(container: dict[str, Any]) -> str:
    return " ".join(str(item) for item in [*_as_items(container.get("command")), *_as_items(container.get("args"))])


def _is_model_container(container: dict[str, Any]) -> bool:
    if _model_container(container):
        return True
    name = str(container.get("name") or "").lower()
    if name.startswith(_MODEL_NAME_PREFIXES):
        return True
    image = str(container.get("image") or "").lower()
    return "vllm" in image or "sglang" in image


def _runtime_kind(container: dict[str, Any]) -> str:
    invocation = _container_invocation(container).lower()
    if any(marker in invocation for marker in _VLLM_MARKERS):
        return "vllm"
    if any(marker in invocation for marker in _SGLANG_MARKERS):
        return "sglang"
    return "other"


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _existing_shutdown_timeout(container: dict[str, Any]) -> int | None:
    matches = re.findall(r"--shutdown-timeout(?:=|\s+)(\d+)", _container_invocation(container))
    return max(int(item) for item in matches) if matches else None


_SHUTDOWN_FLAG = "--shutdown-timeout"
_SHELLS = {"sh", "bash"}


def _shell_script_slot(container: dict[str, Any]) -> tuple[str, int] | None:
    """Locate the script of a validated `sh -c` invocation split across command and args."""
    command = container.get("command") or []
    if not command or PurePosixPath(command[0]).name not in _SHELLS:
        return None
    return ("command", 2) if len(command) >= 3 else ("args", 2 - len(command))


def _has_shutdown_flag(tokens: list[Any]) -> bool:
    return any(str(item) == _SHUTDOWN_FLAG or str(item).startswith(f"{_SHUTDOWN_FLAG}=") for item in tokens)


def _apply_shutdown_timeout(container: dict[str, Any]) -> int | None:
    """Add --shutdown-timeout only where the shared vLLM invocation parser will see it."""
    existing = _existing_shutdown_timeout(container)
    timeout = max(existing or 0, MODEL_SHUTDOWN_TIMEOUT_SECONDS)
    label = str(container.get("name") or "<unnamed>")
    try:
        current = vllm_argument_value(container, {_SHUTDOWN_FLAG}, label)
    except ValueError as error:
        logger.warning("Cannot safely apply %s to %s: %s", _SHUTDOWN_FLAG, label, error)
        return existing
    if current is not None and not current.isdigit():
        logger.warning("Keeping non-literal %s=%s for %s", _SHUTDOWN_FLAG, current, label)
        return None

    original = {key: copy.deepcopy(container[key]) for key in ("command", "args") if key in container}
    slot = _shell_script_slot(container)
    if slot is not None:
        field, index = slot
        values = list(container[field])
        values[index] = set_vllm_shell_argument(str(values[index]), "shutdown-timeout", timeout)
        container[field] = values
    else:
        command = list(container.get("command") or [])
        args = list(container.get("args") or [])
        command_has_flag = _has_shutdown_flag(command)
        if command_has_flag:
            container["command"] = set_vllm_argument(command, "shutdown-timeout", timeout)
        if not command_has_flag or _has_shutdown_flag(args):
            container["args"] = set_vllm_argument(args, "shutdown-timeout", timeout)

    try:
        applied = vllm_argument_value(container, {_SHUTDOWN_FLAG}, label)
    except ValueError:
        applied = None
    if applied == str(timeout):
        return timeout
    for key in ("command", "args"):
        container.pop(key, None)
    container.update(original)
    logger.warning("Cannot verify %s=%s for %s; leaving the invocation unchanged", _SHUTDOWN_FLAG, timeout, label)
    return existing


def _ensure_prestop(container: dict[str, Any]) -> str:
    lifecycle = container.get("lifecycle")
    if lifecycle is None:
        container["lifecycle"] = {
            "preStop": {"exec": {"command": ["/bin/sh", "-c", f"sleep {MODEL_PRESTOP_SLEEP_SECONDS}"]}}
        }
        return "added"
    if isinstance(lifecycle, dict) and "preStop" not in lifecycle:
        lifecycle["preStop"] = {"exec": {"command": ["/bin/sh", "-c", f"sleep {MODEL_PRESTOP_SLEEP_SECONDS}"]}}
        return "added"
    return "preserved"


def _update_kv_transfer_policy(raw: str) -> tuple[str, str]:
    try:
        config = json.loads(raw)
    except (TypeError, ValueError):
        return raw, "invalid"
    if not isinstance(config, dict):
        return raw, "invalid"
    if config.get("kv_load_failure_policy") == KV_LOAD_FAILURE_POLICY:
        return raw, "unchanged"
    config["kv_load_failure_policy"] = KV_LOAD_FAILURE_POLICY
    return json.dumps(config, separators=(",", ":")), "changed"


def _apply_kv_transfer_config(container: dict[str, Any], label: str) -> tuple[int, int, list[str]]:
    detected = 0
    updated = 0
    warnings: list[str] = []
    pattern = re.compile(r"(--kv-transfer-config(?:=|\s+))(?:'([^']*)'|\"([^\"]*)\"|(\S+))")

    def update(raw: str) -> str:
        nonlocal detected, updated
        detected += 1
        next_value, status = _update_kv_transfer_policy(raw)
        if status == "changed":
            updated += 1
            return next_value
        if status == "invalid":
            warnings.append(f"{label}: --kv-transfer-config is not a JSON object; left unchanged.")
        return raw

    def update_shell(command: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw = match.group(2) or match.group(3) or match.group(4) or ""
            next_value = update(raw)
            if next_value == raw:
                return match.group(0)
            return f"{match.group(1)}{shlex.quote(next_value)}"

        return pattern.sub(replace, command)

    def update_list(items: list[Any]) -> None:
        for index, item in enumerate(items):
            if not isinstance(item, str):
                continue
            if item == "--kv-transfer-config":
                if index + 1 < len(items):
                    items[index + 1] = update(str(items[index + 1]))
            elif item.startswith("--kv-transfer-config="):
                raw = item.split("=", 1)[1]
                next_value = update(raw)
                if next_value != raw:
                    items[index] = f"--kv-transfer-config={next_value}"
            elif "--kv-transfer-config" in item:
                next_value = update_shell(item)
                if next_value != item:
                    items[index] = next_value

    command = container.get("command")
    if isinstance(command, str):
        container["command"] = update_shell(command)
    elif isinstance(command, list):
        update_list(command)
    args = container.get("args")
    if isinstance(args, str):
        container["args"] = update_shell(args)
    elif isinstance(args, list):
        update_list(args)
    return detected, updated, warnings


def _ensure_termination_grace_period(pod: dict[str, Any], required_seconds: int) -> bool:
    current = _integer(pod.get("terminationGracePeriodSeconds"))
    if current is not None and current >= required_seconds:
        return False
    pod["terminationGracePeriodSeconds"] = required_seconds
    return True


def _graceful_shutdown_pod_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    pods = list(_pod_specs(document))
    if document.get("kind") != "DisaggregatedSet":
        return pods
    for role in (document.get("spec") or {}).get("roles") or []:
        template = (role.get("spec") or {}).get("leaderWorkerTemplate") or {}
        for key in ("leaderTemplate", "workerTemplate"):
            spec = (template.get(key) or {}).get("spec")
            if isinstance(spec, dict):
                pods.append(spec)
    return pods


def prepare_graceful_shutdown_manifest(manifest: str) -> tuple[str, dict[str, Any]]:
    documents = list(yaml.safe_load_all(manifest))
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    kv_detected = 0
    kv_updated = 0

    def visit(document: Any) -> None:
        nonlocal kv_detected, kv_updated
        if not isinstance(document, dict):
            return
        if document.get("kind") == "List":
            for item in document.get("items") or []:
                visit(item)
            return
        identity = f"{document.get('kind')}/{document.get('metadata', {}).get('name', '<unnamed>')}"
        for pod in _graceful_shutdown_pod_specs(document):
            for container in pod.get("containers") or []:
                label = f"{identity}/{container.get('name', '<unnamed>')}"
                detected, updated, kv_warnings = _apply_kv_transfer_config(container, label)
                kv_detected += detected
                kv_updated += updated
                warnings.extend(kv_warnings)
            model_containers = [
                container for container in pod.get("containers") or [] if _is_model_container(container)
            ]
            if not model_containers:
                continue
            pod_entries: list[dict[str, Any]] = []
            longest_shutdown_timeout = 0
            for container in model_containers:
                runtime = _runtime_kind(container)
                label = f"{identity}/{container.get('name', '<unnamed>')}"
                entry: dict[str, Any] = {
                    "resource": identity,
                    "container": container.get("name"),
                    "runtime": runtime,
                }
                if runtime == "vllm":
                    shutdown_timeout = _apply_shutdown_timeout(container)
                    if shutdown_timeout is not None:
                        longest_shutdown_timeout = max(longest_shutdown_timeout, shutdown_timeout)
                    entry["shutdownTimeoutSeconds"] = shutdown_timeout
                    if shutdown_timeout is None:
                        warnings.append(f"{label}: could not safely apply vLLM --shutdown-timeout")
                elif runtime == "sglang":
                    warnings.append(
                        f"{label}: SGLang does not expose vLLM --shutdown-timeout; "
                        "only endpoint removal and the termination grace period apply."
                    )
                entry["preStop"] = _ensure_prestop(container)
                pod_entries.append(entry)
            required_grace = max(
                MODEL_TERMINATION_GRACE_PERIOD_SECONDS,
                longest_shutdown_timeout + MODEL_PRESTOP_SLEEP_SECONDS + 10 if longest_shutdown_timeout else 0,
            )
            _ensure_termination_grace_period(pod, required_grace)
            for entry in pod_entries:
                entry["terminationGracePeriodSeconds"] = required_grace
            entries.extend(pod_entries)

    for document in documents:
        visit(document)
    report = {
        "kvLoadFailurePolicy": KV_LOAD_FAILURE_POLICY,
        "kvTransferConfigsDetected": kv_detected,
        "kvTransferConfigsUpdated": kv_updated,
        "shutdownTimeoutSeconds": MODEL_SHUTDOWN_TIMEOUT_SECONDS,
        "terminationGracePeriodSeconds": MODEL_TERMINATION_GRACE_PERIOD_SECONDS,
        "preStopSleepSeconds": MODEL_PRESTOP_SLEEP_SECONDS,
        "containers": entries,
        "warnings": warnings,
    }
    if not entries:
        return manifest, report
    return yaml.safe_dump_all(documents, sort_keys=False), report


def prepare_single_slice_disaggregated_set_manifest(manifest: str) -> tuple[str, dict[str, Any]]:
    """Validate DS manifests without changing their declared topology."""
    return manifest, validate_disaggregated_sets(list(yaml.safe_load_all(manifest)))


def prepare_single_slice_disaggregated_set(
    artifact: GuideDeploymentArtifact, output_root: Path
) -> GuideDeploymentArtifact:
    if not artifact.manifest_ref:
        return artifact
    reference = Path(artifact.manifest_ref)
    directory_artifact = reference.is_dir()
    path = reference / "manifest.yaml" if directory_artifact else reference
    if not path.is_file():
        return artifact
    manifest = path.read_text(encoding="utf-8")
    rendered, report = prepare_single_slice_disaggregated_set_manifest(manifest)
    if not report["disaggregatedSets"]:
        return artifact
    if report["adjusted"]:
        logger.warning(
            "Guide %s: forced DisaggregatedSet slices to 1 (Lens supports single-slice manual scaling)",
            artifact.guide_id,
        )
    checksum = text_checksum(rendered)
    if rendered != manifest:
        if directory_artifact:
            path.write_text(rendered, encoding="utf-8")
        else:
            directory = output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed DisaggregatedSet manifest drift detected")
            path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
    return replace(
        artifact,
        manifest_ref=str(reference if directory_artifact else path),
        manifest_checksum=artifact.manifest_checksum if directory_artifact else checksum,
        artifact_hash=stable_hash({"provider": artifact.artifact_hash, "disaggregatedSet": report}),
        deployment_contract={**artifact.deployment_contract, "disaggregatedSet": report},
    )


def prepare_graceful_shutdown(artifact: GuideDeploymentArtifact, output_root: Path) -> GuideDeploymentArtifact:
    if not artifact.manifest_ref:
        return artifact
    reference = Path(artifact.manifest_ref)
    directory_artifact = reference.is_dir()
    path = reference / "manifest.yaml" if directory_artifact else reference
    if not path.is_file():
        return artifact
    manifest = path.read_text(encoding="utf-8")
    rendered, report = prepare_graceful_shutdown_manifest(manifest)
    if not report["containers"]:
        return artifact
    for warning in report["warnings"]:
        logger.warning("Guide %s: %s", artifact.guide_id, warning)
    checksum = text_checksum(rendered)
    if rendered != manifest:
        if directory_artifact:
            path.write_text(rendered, encoding="utf-8")
        else:
            directory = output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed graceful-shutdown manifest drift detected")
            path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
    return replace(
        artifact,
        manifest_ref=str(reference if directory_artifact else path),
        manifest_checksum=artifact.manifest_checksum if directory_artifact else checksum,
        artifact_hash=stable_hash({"provider": artifact.artifact_hash, "gracefulShutdown": report}),
        deployment_contract={**artifact.deployment_contract, "gracefulShutdown": report},
    )


_EXCLUDED_BASELINE_PORTS = frozenset({9002, 9090, 54321})
_PREFERRED_BASELINE_SUFFIXES = ("-baseline", "-model-servers", "-modelserver")


def _modelserver_container_ports(container: dict[str, Any], workload_name: str) -> tuple[set[int], dict[str, int]]:
    numeric_ports: set[int] = set()
    named_ports: dict[str, int] = {}
    declared_ports = container.get("ports")
    if isinstance(declared_ports, list) and declared_ports:
        for port_entry in declared_ports:
            if not isinstance(port_entry, dict):
                continue
            if str(port_entry.get("protocol") or "TCP").upper() != "TCP":
                continue
            container_port = port_entry.get("containerPort")
            if (
                isinstance(container_port, int)
                and not isinstance(container_port, bool)
                and container_port > 0
                and container_port not in _EXCLUDED_BASELINE_PORTS
            ):
                numeric_ports.add(container_port)
                port_name = port_entry.get("name")
                if isinstance(port_name, str) and port_name:
                    named_ports[port_name] = container_port
        return numeric_ports, named_ports

    inferred_port = 8000
    try:
        tokens = _invocation_tokens(container, workload_name or "modelserver")
        port_option = _option(tokens, {"--port"}, workload_name or "modelserver")
        if port_option is not None:
            parsed_port = int(port_option)
            if parsed_port > 0:
                inferred_port = parsed_port
    except (ValueError, TypeError):
        inferred_port = 8000
    if inferred_port not in _EXCLUDED_BASELINE_PORTS:
        numeric_ports.add(inferred_port)
    return numeric_ports, named_ports


def _manifest_baseline_endpoint(manifest_text: str, namespace: str, primary_service: str) -> str | None:
    """Derive the direct model-server Service URL from a rendered deployment manifest."""
    try:
        raw_docs = list(yaml.safe_load_all(manifest_text))
    except yaml.YAMLError:
        return None
    docs: list[dict[str, Any]] = []
    for doc in raw_docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
            docs.extend(item for item in doc["items"] if isinstance(item, dict))
        else:
            docs.append(doc)

    workload_entries: list[tuple[dict[str, Any], set[int], dict[str, int]]] = []
    for workload in workload_roles(docs):
        pod = workload.get("pod") or {}
        containers = [
            container
            for container in (pod.get("containers") or [])
            if isinstance(container, dict) and str(container.get("name") or "").lower().startswith(_MODEL_NAME_PREFIXES)
        ]
        if not containers:
            continue
        numeric_ports: set[int] = set()
        named_ports: dict[str, int] = {}
        workload_name = str(workload.get("name") or "modelserver")
        for container in containers:
            container_numeric, container_named = _modelserver_container_ports(container, workload_name)
            numeric_ports.update(container_numeric)
            named_ports.update(container_named)
        if numeric_ports:
            workload_entries.append((workload.get("pod_labels") or {}, numeric_ports, named_ports))
    if not workload_entries:
        return None

    candidates: list[tuple[tuple[bool, bool, bool, int], str]] = []
    for doc_index, doc in enumerate(docs):
        if doc.get("kind") != "Service":
            continue
        svc_name = str((doc.get("metadata") or {}).get("name") or "")
        if not svc_name or svc_name == primary_service:
            continue
        spec = doc.get("spec") or {}
        if not isinstance(spec, dict):
            continue
        selector = spec.get("selector")
        if not isinstance(selector, dict) or not selector:
            continue
        matched = [
            (nports, nmports)
            for labels, nports, nmports in workload_entries
            if all(labels.get(key) == value for key, value in selector.items())
        ]
        if not matched:
            continue
        matched_numeric = set().union(*(nports for nports, _ in matched))
        matched_named = {key: value for _, nmports in matched for key, value in nmports.items()}
        is_non_headless = str(spec.get("clusterIP") or "").strip().lower() != "none"
        has_preferred_suffix = svc_name.endswith(_PREFERRED_BASELINE_SUFFIXES)
        for port_index, port_entry in enumerate(spec.get("ports") or []):
            if not isinstance(port_entry, dict):
                continue
            if str(port_entry.get("protocol") or "TCP").upper() != "TCP":
                continue
            svc_port = port_entry.get("port")
            if (
                not isinstance(svc_port, int)
                or isinstance(svc_port, bool)
                or svc_port <= 0
                or svc_port in _EXCLUDED_BASELINE_PORTS
            ):
                continue
            target_port = port_entry.get("targetPort")
            resolved_target: int | None
            if target_port is None:
                resolved_target = svc_port
            elif isinstance(target_port, int) and not isinstance(target_port, bool):
                resolved_target = target_port
            elif isinstance(target_port, str) and target_port:
                resolved_target = matched_named.get(target_port)
            else:
                resolved_target = None
            if (
                resolved_target is None
                or resolved_target in _EXCLUDED_BASELINE_PORTS
                or resolved_target not in matched_numeric
            ):
                continue
            app_protocol = str(port_entry.get("appProtocol") or "").strip().lower()
            port_name = str(port_entry.get("name") or "").strip().lower()
            scheme = "https" if (app_protocol == "https" or port_name == "https" or svc_port == 443) else "http"
            url = f"{scheme}://{svc_name}.{namespace}.svc.cluster.local:{svc_port}"
            rank = (is_non_headless, has_preferred_suffix, svc_port == 8000, -(doc_index * 1000 + port_index))
            candidates.append((rank, url))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


class ConfigurationManifestAdapter:
    """Use Configuration's manifest when present, otherwise retain legacy behavior."""

    def __init__(
        self,
        delegate: GuideAdapter | None,
        command_runner,
        namespace_prefix: str,
        readiness_timeout_seconds: int,
        output_root: Path = DEPLOYMENT_MANIFEST_DIR,
    ) -> None:
        self._delegate = delegate
        self._runner = command_runner
        self._namespace_prefix = namespace_prefix
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._output_root = output_root.resolve()

    def discover(self) -> GuideDefinition:
        return self._delegate.discover()

    def register_restored_namespace(self, namespace: str) -> None:
        register = getattr(self._runner, "register_restored_namespace", None)
        if callable(register):
            register(namespace)
        delegate_register = getattr(self._delegate, "register_restored_namespace", None)
        if callable(delegate_register):
            delegate_register(namespace)

    def validate_inputs(
        self, definition: GuideDefinition, cluster_snapshot: dict[str, Any], overrides: dict[str, Any]
    ) -> ValidationResult:
        if not self._is_exact(overrides):
            return self._delegate.validate_inputs(definition, cluster_snapshot, overrides)
        try:
            validate_configuration_manifest_content(overrides, definition.guide_id)
            source = (overrides.get("officialGuide") or {}).get("source") or {}
            if not accelerator_supported(source.get("accelerator")):
                raise ValueError(
                    f"{definition.guide_id} does not support accelerator "
                    f"{source.get('accelerator')!r} in this Prism release"
                )
            allowed_servers = {"vllm", "vllm-rdma"} if definition.guide_id == "pd-disaggregation" else {"vllm"}
            if source.get("modelServer") not in allowed_servers:
                raise ValueError(
                    f"{definition.guide_id} does not support model server {source.get('modelServer')} "
                    "in this Prism release"
                )
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition: GuideDefinition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        if not self._is_exact(overrides):
            artifact = await self._delegate.render(definition, overrides)
        else:
            manifest, source_checksum, deployment, source = validate_configuration_manifest_content(
                overrides, definition.guide_id
            )
            deployment_contract = dict(deployment)
            if definition.guide_id == "source-composition":
                reviewed = manifest
                if overrides.get("_placement") is not None:
                    reviewed, _ = apply_placement(reviewed, overrides["_placement"])
                target = configured_target_node()
                if target:
                    from .target_node import pin_manifest_to_node

                    reviewed, _ = pin_manifest_to_node(reviewed, target)
                if list(yaml.safe_load_all(reviewed)) != list(yaml.safe_load_all(manifest)):
                    raise ValueError("Placement changed after review; regenerate the source configuration.")
                deployment_contract["placementApplied"] = True
            bundle = (overrides.get("officialGuide") or {}).get("deploymentBundle")
            if bundle is not None:
                validate_deployment_bundle(bundle, definition.guide_id, str(source.get("commit") or ""))
                deployment_contract["deploymentBundle"] = bundle
            router_profile = overrides.get("routerProfile")
            if isinstance(router_profile, str) and router_profile:
                deployment_contract["routerProfile"] = router_profile
            namespace = str(overrides.get("_deployment_namespace") or "")
            self._validate_namespace(namespace)
            rendered = manifest.replace("__PRISM_NAMESPACE__", namespace)
            if "__PRISM_NAMESPACE__" in rendered:
                raise ValueError("deployment manifest contains an unresolved namespace binding")
            manifest_checksum = text_checksum(rendered)
            directory = self._output_root / manifest_checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed deployment manifest drift detected")
            if not path.is_file():
                path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=manifest_checksum.removeprefix("sha256:"),
                source_version=source.get("commit"),
                retention_class="configuration",
            )
            artifact = GuideDeploymentArtifact(
                guide_id=definition.guide_id,
                artifact_hash=stable_hash(
                    {
                        "source": source_checksum,
                        "manifest": manifest_checksum,
                        "namespace": namespace,
                        "deployment_contract": deployment_contract,
                    }
                ),
                manifest_ref=str(path),
                source_ref=str(source.get("commit")),
                guide_content_hash=source_checksum,
                manifest_checksum=manifest_checksum,
                deployment_contract=deployment_contract,
            )

        if artifact.manifest_ref and Path(artifact.manifest_ref).is_dir():
            reference = Path(artifact.manifest_ref)
            isolated = Path(tempfile.mkdtemp(prefix=f"{reference.name}-", dir=reference.parent))
            shutil.copytree(reference, isolated, dirs_exist_ok=True)
            artifact = replace(
                artifact,
                manifest_ref=str(isolated),
                artifact_hash=stable_hash({"provider_artifact": artifact.artifact_hash, "overlay": str(isolated)}),
            )
        composition = overrides.get("guideComposition")
        if composition and not self._is_exact(overrides):
            from llm_d_lens.configuration.guide_composition import compose_native_cpu_cache
            from llm_d_lens.deploy.guide_planning import GuideError, select_manifest_paths

            if not isinstance(composition, dict) or set(composition) != {"nativeCpuCacheGiB"}:
                raise ValueError("Unknown Guide composition")
            policy = getattr(self._delegate, "_policy", None)
            root = getattr(self._delegate, "_guide_root", None) or getattr(policy, "guide_root", None)
            if root is None:
                raise ValueError("This provider cannot resolve a source-bound composition")
            root = Path(root)
            accelerator = (
                overrides.get("hardware_profile")
                or getattr(self._delegate, "_accelerator", None)
                or getattr(policy, "accelerator", None)
            )
            paths = [
                str(path.relative_to(root))
                for path in (root / "guides/tiered-prefix-cache").rglob("*")
                if path.is_file()
            ]
            try:
                selected = select_manifest_paths(paths, "tiered-prefix-cache", accelerator, "vllm", "native/cpu")
            except GuideError:
                selected = select_manifest_paths(paths, "tiered-prefix-cache", accelerator, "vllm", "native/cpu/base")
            status, donor, stderr = await self._runner(["kubectl", "kustomize", str(root / selected["guidePath"])])
            if status or not donor.strip():
                raise ValueError("Unable to render the native CPU cache source for composition")
            reference = Path(artifact.manifest_ref or "")
            path = reference / "manifest.yaml" if reference.is_dir() else reference
            documents = compose_native_cpu_cache(
                list(yaml.safe_load_all(path.read_text())),
                list(yaml.safe_load_all(donor)),
                guide=definition.guide_id,
                capacity_gib=composition["nativeCpuCacheGiB"],
            )
            rendered = yaml.safe_dump_all(documents, sort_keys=False)
            checksum = text_checksum(rendered)
            if reference.is_dir():
                path.write_text(rendered)
            else:
                directory = self._output_root / checksum.removeprefix("sha256:")
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "manifest.yaml"
                path.write_text(rendered)
                register_artifacts(
                    directory,
                    owner_type="deployment-manifest",
                    owner_id=checksum.removeprefix("sha256:"),
                    retention_class="configuration",
                )
            artifact = replace(
                artifact,
                manifest_ref=str(reference if reference.is_dir() else path),
                manifest_checksum=checksum,
                artifact_hash=stable_hash(
                    {"provider": artifact.artifact_hash, "composition": composition, "manifest": checksum}
                ),
            )
        artifact = prepare_graceful_shutdown(artifact, self._output_root)
        artifact = prepare_single_slice_disaggregated_set(artifact, self._output_root)
        artifact = prepare_shared_memory(artifact, self._output_root, exact=self._is_exact(overrides))
        if "_model_cache_dependencies" in overrides:
            from llm_d_lens.deploy.providers.storage_mount import validate_cached_model_arguments

            manifest_path = Path(artifact.manifest_ref or "")
            if manifest_path.is_dir():
                manifest_path = manifest_path / "manifest.yaml"
            if not manifest_path.is_file():
                raise ValueError("Model Cache preflight requires a rendered manifest file")
            validate_cached_model_arguments(overrides, manifest_path.read_text(encoding="utf-8"))
        artifact = instrument_manifest(artifact, self._output_root)
        placement = overrides.get("_placement")
        if placement is not None and not artifact.deployment_contract.get("placementApplied"):
            if not isinstance(placement, dict) or not artifact.manifest_ref or Path(artifact.manifest_ref).is_dir():
                raise ValueError("placement requires a rendered Kubernetes manifest file")
            rendered, changed = apply_placement(Path(artifact.manifest_ref).read_text(encoding="utf-8"), placement)
            checksum = text_checksum(rendered)
            directory = self._output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed placement manifest drift detected")
            path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=checksum.removeprefix("sha256:"),
                retention_class="configuration",
            )
            artifact = replace(
                artifact,
                manifest_ref=str(path),
                manifest_checksum=checksum,
                artifact_hash=stable_hash(
                    {"provider_artifact": artifact.artifact_hash, "placement": placement, "workloads": changed}
                ),
            )
        runtime_policy = overrides.get("_runtime_class_policy")
        cluster_id = overrides.get("_cluster_id")
        if runtime_policy is not None:
            capabilities = (
                {"items": [], "nodes": []}
                if runtime_policy.get("mode") == "node-default"
                else await discover_runtime_classes(str(cluster_id or ""))
            )
            reference = Path(artifact.manifest_ref or "")
            path = reference / "manifest.yaml" if reference.is_dir() else reference
            manifest = path.read_text(encoding="utf-8")
            source = (overrides.get("officialGuide") or {}).get("source") or {}
            rendered, report = prepare_runtime_class_manifest(
                manifest,
                runtime_policy,
                capabilities,
                accelerator=overrides.get("hardware_profile")
                or source.get("accelerator")
                or getattr(self._delegate, "_accelerator", None)
                or getattr(getattr(self._delegate, "_policy", None), "accelerator", None),
            )
            checksum = text_checksum(rendered)
            if rendered != manifest:
                if reference.is_dir():
                    path.write_text(rendered, encoding="utf-8")
                else:
                    directory = self._output_root / checksum.removeprefix("sha256:")
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / "manifest.yaml"
                    if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                        raise ValueError("checksum-addressed RuntimeClass manifest drift detected")
                    path.write_text(rendered, encoding="utf-8")
            artifact = replace(
                artifact,
                manifest_ref=str(reference if reference.is_dir() else path),
                manifest_checksum=artifact.manifest_checksum if reference.is_dir() else checksum,
                artifact_hash=stable_hash({"provider_artifact": artifact.artifact_hash, "runtimeClass": report}),
                deployment_contract={**artifact.deployment_contract, "runtimeClass": report},
            )
        if artifact.manifest_ref and artifact.manifest_checksum:
            saved_path = Path(artifact.manifest_ref)
            if saved_path.is_file() and saved_path.resolve().is_relative_to(self._output_root.resolve()):
                register_artifacts(
                    saved_path.parent,
                    owner_type="deployment-manifest",
                    owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                    source_version=artifact.source_ref,
                    retention_class="configuration",
                )
        target_node = configured_target_node()
        if not target_node:
            return artifact
        if not artifact.manifest_ref:
            raise ValueError("target-node enforcement requires a rendered Kubernetes manifest")
        if Path(artifact.manifest_ref).is_dir():
            # Descriptor/Kustomize providers own directory overlays and must
            # apply node pinning while rendering their generated patch. The
            # generic pinning helper accepts a single rendered YAML file only.
            return artifact
        manifest_ref, manifest_checksum, pinned_workloads = write_pinned_manifest(
            artifact.manifest_ref, self._output_root, target_node
        )
        return replace(
            artifact,
            artifact_hash=stable_hash(
                {
                    "provider_artifact": artifact.artifact_hash,
                    "manifest": manifest_checksum,
                    "target_node": target_node,
                    "pinned_workloads": pinned_workloads,
                }
            ),
            manifest_ref=manifest_ref,
            manifest_checksum=manifest_checksum,
        )

    async def prepare_router(self, artifact: GuideDeploymentArtifact, context: dict) -> GuideDeploymentArtifact:
        from llm_d_lens.deploy.data_plane import router_data_plane_effective_values
        from llm_d_lens.deploy.providers.deployment_bundle import freeze_deployment_bundle

        bundle = artifact.deployment_contract.get("deploymentBundle")
        if bundle is None and artifact.guide_id == "source-composition":
            return artifact
        if bundle is None:
            raise ValueError("Prepared deployment requires a saved Router deployment bundle")
        runner = getattr(self._delegate, "_bundle_command_runner", None)
        if runner is None:
            raise ValueError("This provider does not support frozen Router plans")
        base = bundle["helm"]["values"][-1]["content"]
        profile = artifact.deployment_contract.get("routerProfile", "optimized-baseline")
        if profile != "optimized-baseline":
            transform = getattr(self._delegate, "_profiled_router_values_content", None)
            if transform is None:
                raise ValueError("This Router profile cannot be prepared immutably")
            base = transform(base, profile)
        frozen = await freeze_deployment_bundle(
            bundle,
            guide=artifact.guide_id,
            source_commit=str(artifact.source_ref or ""),
            namespace=context["namespace"],
            command_runner=runner,
            effective_values=router_data_plane_effective_values(base, context),
        )
        return replace(artifact, deployment_contract={**artifact.deployment_contract, "deploymentBundle": frozen})

    async def validate_prepared_router(self, artifact: GuideDeploymentArtifact, namespace: str) -> None:
        from llm_d_lens.deploy.providers.deployment_bundle import validate_frozen_bundle

        if artifact.guide_id == "source-composition" and "deploymentBundle" not in artifact.deployment_contract:
            return
        await validate_frozen_bundle(
            artifact.deployment_contract["deploymentBundle"],
            guide=artifact.guide_id,
            source_commit=str(artifact.source_ref or ""),
            namespace=namespace,
            command_runner=self._delegate._bundle_command_runner,
        )

    async def _create_namespace(self, artifact, namespace):
        prepared_namespace = artifact.deployment_contract.get("preparedNamespace")
        if prepared_namespace:
            from llm_d_lens.deploy.slice_capacity import (
                ClusterInventoryAuthError,
                is_forbidden_output,
                is_unauthorized_output,
                redact_kubectl_error,
            )

            namespace_path = Path(artifact.manifest_ref).parent / "namespace.yaml"
            namespace_path.write_text(yaml.safe_dump(prepared_namespace, sort_keys=False), encoding="utf-8")
            status, stdout, stderr = await self._runner(["kubectl", "create", "--filename", str(namespace_path)])
            if status != 0:
                if is_unauthorized_output(stdout, stderr):
                    raise ClusterInventoryAuthError(
                        redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                    )
                is_bootstrapped = (
                    is_forbidden_output(stdout, stderr) or "alreadyexists" in (stdout + stderr).lower()
                ) and await self._verify_prepared_bootstrap_marker(namespace, prepared_namespace)
                if not is_bootstrapped:
                    raise RuntimeError(redact_kubectl_error((stderr or stdout or "namespace creation failed").strip()))
        else:
            from llm_d_lens.deploy.providers.kubernetes import ensure_namespace_accessible

            await ensure_namespace_accessible(self._runner, namespace)

    async def deploy(
        self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any], *, namespace_created=False
    ) -> dict[str, Any]:
        if not {"readinessDeployments", "endpoint"} <= artifact.deployment_contract.keys():
            return await self._delegate.deploy(artifact, execution_context)
        if Path(artifact.manifest_ref or "").is_dir():
            # Legacy Guide adapters may return a Kustomize overlay directory
            # rather than an immutable Configuration manifest file.
            return await self._delegate.deploy(artifact, execution_context)
        namespace = self._namespace(execution_context)
        if not namespace_created:
            await self._create_namespace(artifact, namespace)
        manifest = Path(artifact.manifest_ref or "").read_text(encoding="utf-8")
        model_token = execution_context.get("model_token")
        if "name: llm-d-hf-token" in manifest and model_token:
            create_secret = getattr(self._runner, "create_model_secret_from_token", None)
            if not callable(create_secret):
                raise RuntimeError("deployment runner cannot provision the supplied model token")
            await create_secret(namespace, model_token)
        elif "name: llm-d-hf-token" in manifest:
            policy = execution_context.get("deployment_policy") or {}
            model_secret = policy.get("model_secret") or {"mode": "none"}
            if model_secret.get("mode") == "existing-secret":
                copy_secret = getattr(self._runner, "copy_model_secret", None)
                source_namespace = str(model_secret.get("sourceNamespace") or "")
                source_name = str(model_secret.get("sourceName") or "")
                if not callable(copy_secret) or not source_namespace or not source_name:
                    raise RuntimeError("existing model Secret source is not configured")
                await copy_secret(namespace, source_namespace, source_name)
            elif model_secret.get("mode") == "host":
                create_secret = getattr(self._runner, "create_model_secret", None)
                token_file = Path.home() / ".cache" / "huggingface" / "token"
                if not callable(create_secret) or not token_file.is_file():
                    raise RuntimeError(
                        "deployment requires a model token; select an existing Kubernetes Secret or "
                        "configure a host token"
                    )
                await create_secret(namespace, token_file)
        install_router = (
            getattr(self._delegate, "install_published_router", None)
            if callable(getattr(type(self._delegate), "install_published_router", None))
            else None
        )
        runtime = execution_context.get("runtime") or {}
        storage_volume_id = str(runtime.get("storageVolumeId") or "")
        cluster_id = str((execution_context.get("provenance") or {}).get("cluster_server_id") or "")
        if storage_volume_id:
            # Must run before any delegate-lifecycle early return below: the
            # rendered manifest (from Configuration) already embeds a
            # `persistentVolumeClaim` volume referencing the storage volume's
            # PVC name, but that PVC only exists in Storage's home namespace
            # (see storage.service.ensure_mount_in_namespace). Guides such as
            # pd-disaggregation delegate their whole `deploy()` to install
            # Guide-specific Router/EPP components before applying the
            # manifest, which would otherwise skip this and leave the Pod
            # referencing a PVC that doesn't exist in its own namespace.
            volume = await get_ready_volume(storage_volume_id, cluster_id=cluster_id or None)
            if "preparedStorage" in artifact.deployment_contract:
                from llm_d_lens.storage.service import _satellite_pv_manifest, _satellite_pvc_manifest

                current = [
                    _satellite_pv_manifest(volume, namespace=namespace),
                    _satellite_pvc_manifest(volume, namespace=namespace),
                ]
                if current != artifact.deployment_contract["preparedStorage"]:
                    raise ValueError("Storage resources changed after plan review")
            from llm_d_lens.deploy.providers.storage_mount import is_pvc_bound_in_namespace

            pvc_name = getattr(volume, "pvc_name", None)
            if not pvc_name and isinstance(getattr(volume, "id", None), str):
                from llm_d_lens.storage.service import _pvc_name

                pvc_name = _pvc_name(volume)
            if not (
                isinstance(pvc_name, str)
                and pvc_name
                and cluster_id
                and await is_pvc_bound_in_namespace(pvc_name, namespace=namespace, cluster_id=cluster_id)
            ):
                await ensure_mount_in_namespace(volume, namespace=namespace, cluster_id=cluster_id)
        if callable(install_router):
            await install_router(artifact, execution_context)
        elif self._uses_delegate_lifecycle(artifact):
            # Published manifests contain model-server resources, while these
            # providers also own Router or other Guide-specific components.
            return await self._delegate.deploy(artifact, execution_context)
        status, stdout, stderr = await self._runner(
            ["kubectl", "apply", "--namespace", namespace, "--filename", artifact.manifest_ref or ""]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout or "manifest apply failed").strip())
        reproducibility = {
            key: value
            for key, value in execution_context.items()
            if key.startswith("router_effective_")
            or key.startswith("router_rendered_")
            or key
            in {
                "router_values_path",
                "router_chart",
                "router_version",
                "router_release_name",
                "calibration_script_path",
            }
        }
        return {
            "namespace": namespace,
            "artifact_hash": artifact.artifact_hash,
            "apply_output": stdout,
            **reproducibility,
        }

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        artifact = execution.get("_artifact")
        if not isinstance(artifact, GuideDeploymentArtifact) or not artifact.deployment_contract:
            return await self._delegate.readiness(execution)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.readiness(execution)
        namespace = self._namespace(execution)
        for name in artifact.deployment_contract["readinessDeployments"]:
            # A successful apply can return before the API server's subsequent
            # read path observes the Deployment.  Retry only this propagation
            # race; real rollout failures must still be reported immediately.
            for attempt in range(10):
                status, stdout, stderr = await self._runner(
                    [
                        "kubectl",
                        "rollout",
                        "status",
                        f"deployment/{name}",
                        "--namespace",
                        namespace,
                        f"--timeout={self._readiness_timeout_seconds}s",
                    ]
                )
                message = (stderr or stdout or f"deployment/{name} not ready").strip()
                if status == 0 or not self._is_transient_not_found(message) or attempt == 9:
                    break
                await asyncio.sleep(1)
            if status != 0:
                return ValidationResult(False, [message])
        endpoint = artifact.deployment_contract["endpoint"]
        execution["endpoint_url"] = (
            f"{endpoint['protocol']}://{endpoint['serviceName']}.{namespace}.svc.cluster.local:{endpoint['port']}"
        )
        if not execution.get("baseline_endpoint_url") and artifact.manifest_ref:
            manifest_path = Path(artifact.manifest_ref)
            if manifest_path.is_file():
                baseline_url = _manifest_baseline_endpoint(
                    manifest_path.read_text(encoding="utf-8"),
                    namespace,
                    endpoint["serviceName"],
                )
                if baseline_url:
                    execution["baseline_endpoint_url"] = baseline_url
        return ValidationResult(True)

    @staticmethod
    def _is_transient_not_found(message: str) -> bool:
        """Recognize only the API propagation error that is safe to retry."""
        normalized = message.lower()
        return "deployments.apps" in normalized and "not found" in normalized

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]:
        artifact = execution.get("_artifact")
        if self._delegate is not None and (
            not isinstance(artifact, GuideDeploymentArtifact)
            or not artifact.deployment_contract
            or self._uses_delegate_lifecycle(artifact)
        ):
            diagnostics = await self._delegate.diagnostics(execution)
        else:
            namespace = self._namespace(execution)

            async def capture(arguments: list[str]) -> str:
                status, stdout, stderr = await self._runner(arguments)
                return (stdout if status == 0 else stderr).strip()

            diagnostics = {
                "namespace": namespace,
                "pods": await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"]),
                "events": await capture(
                    ["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"]
                ),
                "resource_claims": await capture(
                    ["kubectl", "get", "resourceclaims.resource.k8s.io", "--namespace", namespace, "-o", "wide"]
                ),
            }
        if "running" in str(diagnostics.get("pods") or "").lower():
            status, stdout, stderr = await self._runner(
                ["kubectl", "get", "pods", "--namespace", self._namespace(execution), "-o", "json"]
            )
            if status != 0:
                diagnostics["pod_statuses_error"] = (stderr or stdout or "Pod status query failed").strip()
                logger.warning("Pod startup diagnostics failed: %s", diagnostics["pod_statuses_error"])
            else:
                pods = json.loads(stdout)["items"]
                timing_fields = ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")
                diagnostics["pod_statuses"] = [
                    {
                        "name": pod["metadata"]["name"],
                        "deleting": bool(pod["metadata"].get("deletionTimestamp")),
                        "status": pod.get("status") or {},
                        "containers": [
                            {
                                "name": container["name"],
                                **{
                                    probe: {
                                        key: container[probe][key] for key in timing_fields if key in container[probe]
                                    }
                                    for probe in ("startupProbe", "readinessProbe")
                                    if probe in container
                                },
                            }
                            for container in pod.get("spec", {}).get("containers") or []
                        ],
                    }
                    for pod in pods
                ]
        if isinstance(artifact, GuideDeploymentArtifact) and "sharedMemory" in artifact.deployment_contract:
            diagnostics = {**diagnostics, "shared_memory": artifact.deployment_contract["sharedMemory"]}
        return diagnostics

    async def stop(self, execution, artifact):
        await self._check_prepared_namespace(execution, artifact)
        if not artifact.deployment_contract:
            return await self._delegate.stop(execution, artifact)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.stop(execution, artifact)
        namespace = self._namespace(execution)
        results = [
            await self._runner(["kubectl", "scale", f"deployment/{name}", "--namespace", namespace, "--replicas=0"])
            for name in artifact.deployment_contract["readinessDeployments"]
        ]
        return {
            "stopped": all(result[0] == 0 for result in results),
            "output": "\n".join(result[1] for result in results),
        }

    async def rollback(self, execution, artifact):
        return await self.stop(execution, artifact)

    async def cleanup(self, execution, artifact, *, force: bool = False):
        await self._check_prepared_namespace(execution, artifact)
        if self._delegate is not None and (not artifact.deployment_contract or self._uses_delegate_lifecycle(artifact)):
            return await self._delegate.cleanup(execution, artifact, force=force)
        namespace = self._namespace(execution)
        status, stdout, stderr = await self._runner(
            ["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true"]
        )
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    async def _read_bootstrap_marker_via_runner(self, namespace: str) -> dict[str, Any] | None:
        from llm_d_lens.deploy.slice_capacity import (
            ClusterInventoryAuthError,
            is_unauthorized_output,
            redact_kubectl_error,
        )

        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "get",
                "configmap",
                "lens-deployment-bootstrap",
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "-o",
                "json",
            ]
        )
        if status and is_unauthorized_output(stdout, stderr):
            raise ClusterInventoryAuthError(
                redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
            )
        if status != 0 or not stdout.strip():
            return None
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _marker_matches_target(marker: dict[str, Any] | None, *, namespace: str, cluster_id: str | None = None) -> bool:
        if marker is None or marker.get("status") != "completed":
            return False
        marker_ns = marker.get("namespace")
        if marker_ns is not None and (
            not isinstance(marker_ns, str) or (bool(marker_ns.strip()) and marker_ns.strip() != namespace)
        ):
            return False
        clean_cluster = (cluster_id or "").strip()
        marker_cluster = marker.get("clusterId")
        return marker_cluster is None or (
            isinstance(marker_cluster, str)
            and (not marker_cluster.strip() or not clean_cluster or marker_cluster.strip() == clean_cluster)
        )

    async def _verify_prepared_bootstrap_marker(
        self, namespace: str, expected: dict[str, Any], *, cluster_id: str | None = None
    ) -> bool:
        data = await self._read_bootstrap_marker_via_runner(namespace)
        if not self._marker_matches_target(data, namespace=namespace, cluster_id=cluster_id):
            return False
        expected_req = (expected.get("metadata") or {}).get("labels", {}).get("lens.io/prepared-request")
        return expected_req is None or data.get("preparedRequest") == expected_req

    async def _check_prepared_namespace(self, execution, artifact):
        from llm_d_lens.deploy.slice_capacity import (
            ClusterInventoryAuthError,
            is_forbidden_output,
            is_unauthorized_output,
            redact_kubectl_error,
        )

        expected = artifact.deployment_contract.get("preparedNamespace")
        if expected is None:
            return
        namespace = self._namespace(execution)
        status, stdout, stderr = await self._runner(
            ["kubectl", "get", "namespace", namespace, "--ignore-not-found=true", "-o", "json"]
        )
        if status:
            if is_unauthorized_output(stdout, stderr):
                raise ClusterInventoryAuthError(
                    redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                )
            if is_forbidden_output(stdout, stderr):
                if await self._verify_prepared_bootstrap_marker(namespace, expected):
                    return
                raise ValueError("Namespace is not owned by this prepared execution; cleanup was not attempted")
            raise ValueError("Cannot verify prepared namespace ownership; cleanup was not attempted")
        if not stdout.strip():
            return
        actual = json.loads(stdout)
        if (
            actual.get("metadata", {}).get("labels", {}).get("lens.io/prepared-request")
            != expected["metadata"]["labels"]["lens.io/prepared-request"]
        ):
            raise ValueError("Namespace is not owned by this prepared execution; cleanup was not attempted")

    @staticmethod
    def _is_exact(overrides: dict[str, Any]) -> bool:
        return isinstance(overrides.get("officialGuide"), dict)

    def _uses_delegate_lifecycle(self, artifact: GuideDeploymentArtifact) -> bool:
        return (
            not {"readinessDeployments", "endpoint"} <= artifact.deployment_contract.keys()
            or artifact.guide_id == "pd-disaggregation"
            or bool(getattr(type(self._delegate), "published_manifest_lifecycle", False))
        )

    def _namespace(self, context: dict[str, Any]) -> str:
        namespace = str(context.get("namespace") or "")
        self._validate_namespace(namespace)
        return namespace

    def _validate_namespace(self, namespace: str) -> None:
        if (
            not namespace.startswith(self._namespace_prefix)
            or namespace == self._namespace_prefix
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace)
        ):
            raise ValueError("deployment namespace is outside the configured allowlist")


class SourceCompositionAdapter(ConfigurationManifestAdapter):
    """Deploy the reviewed source snapshot without a second Guide router installation."""

    managed_resource_kinds = frozenset(
        {
            "Deployment",
            "LeaderWorkerSet",
            "DisaggregatedSet",
            "Service",
            "ConfigMap",
            "Secret",
            "ServiceAccount",
            "Role",
            "RoleBinding",
            "ClusterRole",
            "ClusterRoleBinding",
            "ResourceClaimTemplate",
            "ResourceClaim",
            "InferencePool",
            "HTTPRoute",
            "Gateway",
            "PodMonitor",
            "ServiceMonitor",
            "HorizontalPodAutoscaler",
            "ScaledObject",
            "TriggerAuthentication",
            "NetworkPolicy",
            "PodDisruptionBudget",
        }
    )

    def __init__(self, command_runner, namespace_prefix, readiness_timeout_seconds, **kwargs):
        super().__init__(None, command_runner, namespace_prefix, readiness_timeout_seconds, **kwargs)

    def discover(self):
        return GuideDefinition("source-composition", "generated", "source-composition.v1", "experimental")

    @staticmethod
    def router_rbac(documents, namespace=None, *, standalone=False):
        if any(not isinstance(doc, dict) for doc in documents):
            raise ValueError("Router RBAC resources must be mappings.")
        resources = [doc for doc in documents if doc.get("kind") in {"ClusterRole", "ClusterRoleBinding"}]
        if not resources:
            return []
        if any(
            not isinstance(doc.get("metadata"), dict) or not isinstance(doc["metadata"].get("name"), str)
            for doc in resources
        ):
            raise ValueError("Router RBAC resources require explicit names.")
        roles = {doc["metadata"]["name"] for doc in resources if doc["kind"] == "ClusterRole"}
        accounts = {
            doc["metadata"]["name"]: doc["metadata"].get("namespace")
            for doc in documents
            if doc.get("kind") == "ServiceAccount"
        }
        router_accounts = {
            pod.get("serviceAccountName")
            for doc in documents
            for pod in _pod_specs(doc)
            if any(container.get("name") == "epp" for container in pod.get("containers", []))
        }
        allowed = {
            ("authentication.k8s.io", "tokenreviews"),
            ("authorization.k8s.io", "subjectaccessreviews"),
        }
        for doc in resources:
            if doc.get("apiVersion") != "rbac.authorization.k8s.io/v1":
                raise ValueError("Router RBAC requires rbac.authorization.k8s.io/v1.")
            if namespace and not re.fullmatch(
                re.escape(namespace) + r"-router-auth-[a-f0-9]{12}", doc["metadata"]["name"]
            ):
                raise ValueError("Router RBAC identity must belong to this deployment namespace.")
            if not standalone and doc["metadata"].get("ownerReferences"):
                raise ValueError("Router RBAC ownership is assigned from the deployment namespace, not source input.")
            if doc["kind"] == "ClusterRole":
                rules = doc.get("rules") or []
                if (
                    doc.get("aggregationRule")
                    or not isinstance(rules, list)
                    or not rules
                    or any(
                        not isinstance(rule, dict)
                        or set(rule) != {"apiGroups", "resources", "verbs"}
                        or rule["verbs"] != ["create"]
                        or not isinstance(rule["apiGroups"], list)
                        or not isinstance(rule["resources"], list)
                        or len(rule["apiGroups"]) != 1
                        or len(rule["resources"]) != 1
                        or not isinstance(rule["apiGroups"][0], str)
                        or not isinstance(rule["resources"][0], str)
                        or (rule["apiGroups"][0], rule["resources"][0]) not in allowed
                        for rule in rules
                    )
                ):
                    raise ValueError("Router ClusterRole may only create TokenReviews and SubjectAccessReviews.")
            else:
                ref = doc.get("roleRef") or {}
                subjects = doc.get("subjects") or []
                if (
                    not isinstance(ref, dict)
                    or not isinstance(subjects, list)
                    or ref.get("apiGroup") != "rbac.authorization.k8s.io"
                    or ref.get("kind") != "ClusterRole"
                    or not isinstance(ref.get("name"), str)
                    or (not standalone and ref.get("name") not in roles)
                    or (
                        namespace and not re.fullmatch(re.escape(namespace) + r"-router-auth-[a-f0-9]{12}", ref["name"])
                    )
                    or len(subjects) != 1
                    or not isinstance(subjects[0], dict)
                    or subjects[0].get("kind") != "ServiceAccount"
                    or subjects[0].get("apiGroup", "") != ""
                    or not isinstance(subjects[0].get("name"), str)
                    or (not standalone and subjects[0].get("name") not in accounts)
                    or (not standalone and subjects[0].get("name") not in router_accounts)
                    or (
                        not standalone
                        and accounts.get(subjects[0].get("name"))
                        and subjects[0].get("namespace") != accounts[subjects[0]["name"]]
                    )
                    or (namespace and subjects[0].get("namespace") != namespace)
                ):
                    raise ValueError("Router binding must target its reviewed role and deployment ServiceAccount.")
        return resources

    def validate_inputs(self, definition, cluster_snapshot, overrides):
        if not self._is_exact(overrides):
            return ValidationResult(False, ["Source composition requires a rendered manifest."])
        result = super().validate_inputs(definition, cluster_snapshot, overrides)
        if not result.accepted:
            return result
        official = overrides["officialGuide"]
        if official.get("deploymentBundle"):
            return ValidationResult(False, ["Source composition requires rendered router resources, not Helm inputs."])
        documents = list(yaml.safe_load_all(official["renderedManifest"]))
        try:
            self.router_rbac(documents)
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        unsupported = sorted(
            {
                str(doc.get("kind"))
                for doc in documents
                if isinstance(doc, dict) and doc.get("kind") not in self.managed_resource_kinds
            }
        )
        if unsupported:
            return ValidationResult(
                False, ["Managed composition lifecycle does not support: " + ", ".join(unsupported)]
            )
        runtime = overrides.get("runtime") or {}
        if runtime.get("modelSource") != "cached-readonly":
            return ValidationResult(False, ["Source compositions require a read-only model cache."])
        try:
            from llm_d_lens.configuration.manifest_facts import validate_manifest_facts

            validate_manifest_facts(overrides, official["renderedManifest"])
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return result

    async def render(self, definition, overrides):
        validation = self.validate_inputs(definition, {}, overrides)
        if not validation.accepted:
            raise ValueError(" ".join(validation.reasons))
        artifact = await super().render(definition, overrides)
        documents = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text(encoding="utf-8")))
        rbac = self.router_rbac(documents)
        if rbac:
            namespace = str(overrides.get("_deployment_namespace") or "")
            self._validate_namespace(namespace)
            roles = {}
            for doc in rbac:
                original = doc["metadata"]["name"]
                suffix = stable_hash({"kind": doc["kind"], "name": original}).removeprefix("sha256:")[:12]
                doc["metadata"]["name"] = f"{namespace}-router-auth-{suffix}"
                doc["metadata"].pop("namespace", None)
                doc["metadata"].pop("ownerReferences", None)
                if doc["kind"] == "ClusterRole":
                    roles[original] = doc["metadata"]["name"]
            for doc in rbac:
                if doc["kind"] == "ClusterRoleBinding":
                    doc["roleRef"]["name"] = roles[doc["roleRef"]["name"]]
                    doc["subjects"][0]["namespace"] = namespace
            self.router_rbac(documents, namespace)
            content = yaml.safe_dump_all(documents, sort_keys=False)
            checksum = text_checksum(content)
            directory = self._output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source-router-scoped.yaml"
            path.write_text(content, encoding="utf-8")
            artifact = replace(
                artifact,
                manifest_ref=str(path),
                manifest_checksum=checksum,
                artifact_hash=stable_hash({"source": artifact.artifact_hash, "routerNamespace": namespace}),
                deployment_contract={
                    **artifact.deployment_contract,
                    "routerRbac": [{"kind": doc["kind"], "name": doc["metadata"]["name"]} for doc in rbac],
                },
            )
            register_artifacts(
                path.parent,
                owner_type="deployment-manifest",
                owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
        labels = (overrides.get("runtime") or {}).get("namespaceLabels") or {}
        if not isinstance(labels, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()
        ):
            raise ValueError("Namespace labels must be string pairs.")
        return replace(
            artifact,
            artifact_hash=stable_hash({"manifest": artifact.artifact_hash, "namespaceLabels": labels}),
            deployment_contract={**artifact.deployment_contract, "namespaceLabels": labels},
        )

    async def deploy(self, artifact, execution_context):
        from llm_d_lens.deploy.slice_capacity import (
            ClusterInventoryAuthError,
            is_forbidden_output,
            is_unauthorized_output,
            redact_kubectl_error,
        )

        namespace = self._namespace(execution_context)
        exec_cluster_id = str(
            (execution_context.get("provenance") or {}).get("cluster_server_id")
            or execution_context.get("cluster_id")
            or ""
        )
        documents = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text(encoding="utf-8")))
        if artifact.deployment_contract.get("preparedNamespace"):
            content = Path(artifact.manifest_ref).read_text(encoding="utf-8")
            if text_checksum(content) != artifact.manifest_checksum:
                raise ValueError("Prepared workload checksum changed before deployment")
            directory = self._output_root / artifact.manifest_checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source-workload.yaml"
            path.write_text(content, encoding="utf-8")
            artifact = replace(artifact, manifest_ref=str(path))
        rbac = self.router_rbac(documents, namespace)
        bootstrapped_via_admin = False

        async def _confirm_epp_bootstrap() -> bool:
            marker = await self._read_bootstrap_marker_via_runner(namespace)
            return (
                self._marker_matches_target(marker, namespace=namespace, cluster_id=exec_cluster_id)
                and marker.get("eppAuthDelegator") == "applied"
            )

        if rbac:
            try:
                await self._create_namespace(artifact, namespace)
            except RuntimeError as error:
                if await _confirm_epp_bootstrap():
                    bootstrapped_via_admin = True
                elif is_forbidden_output("", str(error)):
                    raise RuntimeError(
                        f"Namespace {namespace} requires Orchestration administrator "
                        "bootstrap with EPP RBAC before deploying."
                    ) from error
                else:
                    raise
            if not bootstrapped_via_admin:
                status, stdout, stderr = await self._runner(
                    ["kubectl", "get", "namespace", namespace, "--namespace", namespace, "-o", "json"]
                )
                if status:
                    if is_unauthorized_output(stdout, stderr):
                        raise ClusterInventoryAuthError(
                            redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                        )
                    if is_forbidden_output(stdout, stderr):
                        if not await _confirm_epp_bootstrap():
                            raise RuntimeError(
                                f"Namespace {namespace} requires Orchestration administrator "
                                "bootstrap with EPP RBAC before deploying."
                            )
                        bootstrapped_via_admin = True
                    else:
                        raise RuntimeError(
                            redact_kubectl_error(stderr or stdout or "Cannot establish the Router RBAC namespace owner")
                        )
            if not bootstrapped_via_admin:
                uid = json.loads(stdout).get("metadata", {}).get("uid")
                if not isinstance(uid, str) or not uid:
                    raise ValueError("Namespace UID is required for Router RBAC ownership.")
                owner = {"apiVersion": "v1", "kind": "Namespace", "name": namespace, "uid": uid}
                for doc in rbac:
                    resource = f"{doc['kind'].lower()}/{doc['metadata']['name']}"
                    status, stdout, stderr = await self._runner(
                        ["kubectl", "get", resource, "--namespace", namespace, "--ignore-not-found=true", "-o", "json"]
                    )
                    if status:
                        if is_unauthorized_output(stdout, stderr):
                            raise ClusterInventoryAuthError(
                                redact_kubectl_error(
                                    (stderr or stdout or "Kubernetes authentication failed (401)").strip()
                                )
                            )
                        if is_forbidden_output(stdout, stderr) and await _confirm_epp_bootstrap():
                            bootstrapped_via_admin = True
                            break
                        raise RuntimeError(redact_kubectl_error(stderr or stdout or f"Could not inspect {resource}"))
                    if stdout.strip():
                        existing = json.loads(stdout)
                        try:
                            self.router_rbac([existing], namespace, standalone=True)
                        except ValueError as error:
                            raise ValueError(f"Refusing to overwrite changed Router RBAC: {resource}") from error
                        fields = ("rules",) if doc["kind"] == "ClusterRole" else ("roleRef",)
                        owners = existing.get("metadata", {}).get("ownerReferences") or []
                        subjects_match = doc["kind"] != "ClusterRoleBinding" or [
                            {**subject, "apiGroup": subject.get("apiGroup", "")}
                            for subject in existing.get("subjects", [])
                        ] == [{**subject, "apiGroup": subject.get("apiGroup", "")} for subject in doc["subjects"]]
                        if (
                            len(owners) != 1
                            or any(owners[0].get(key) != value for key, value in owner.items())
                            or not subjects_match
                            or any(existing.get(key) != doc.get(key) for key in fields)
                        ):
                            raise ValueError(f"Refusing to overwrite unowned or changed Router RBAC: {resource}")
                        continue
                    doc["metadata"]["ownerReferences"] = [owner]
                    rbac_name = f"{doc['kind'].lower()}-{doc['metadata']['name']}.yaml"
                    path = Path(artifact.manifest_ref).with_name(rbac_name)
                    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
                    register_artifacts(
                        path.parent,
                        owner_type="deployment-manifest",
                        owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                        source_version=artifact.source_ref,
                        retention_class="configuration",
                    )
                    status, stdout, stderr = await self._runner(
                        ["kubectl", "create", "--namespace", namespace, "--filename", str(path)]
                    )
                    if status:
                        if is_unauthorized_output(stdout, stderr):
                            raise ClusterInventoryAuthError(
                                redact_kubectl_error(
                                    (stderr or stdout or "Kubernetes authentication failed (401)").strip()
                                )
                            )
                        if is_forbidden_output(stdout, stderr) and await _confirm_epp_bootstrap():
                            bootstrapped_via_admin = True
                            break
                        raise RuntimeError(redact_kubectl_error(stderr or stdout or f"Could not create {resource}"))
            content = yaml.safe_dump_all([doc for doc in documents if doc not in rbac], sort_keys=False)
            checksum = text_checksum(content)
            directory = self._output_root / checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source-namespaced.yaml"
            path.write_text(content, encoding="utf-8")
            artifact = replace(artifact, manifest_ref=str(path), manifest_checksum=checksum)
            register_artifacts(
                path.parent,
                owner_type="deployment-manifest",
                owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
        labels = artifact.deployment_contract.get("namespaceLabels")
        if labels and not bootstrapped_via_admin:
            namespace = self._namespace(execution_context)
            path = Path(artifact.manifest_ref).with_name(f"{namespace}-namespace.yaml")
            path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {"name": namespace, "labels": labels},
                    }
                ),
                encoding="utf-8",
            )
            register_artifacts(
                path.parent,
                owner_type="deployment-manifest",
                owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                source_version=artifact.source_ref,
                retention_class="configuration",
            )
            status, stdout, stderr = await self._runner(
                ["kubectl", "apply", "--namespace", namespace, "--filename", str(path)]
            )
            if status:
                if is_unauthorized_output(stdout, stderr):
                    raise ClusterInventoryAuthError(
                        redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                    )
                if is_forbidden_output(stdout, stderr):
                    marker = await self._read_bootstrap_marker_via_runner(namespace)
                    if not self._marker_matches_target(marker, namespace=namespace, cluster_id=exec_cluster_id):
                        raise RuntimeError(
                            redact_kubectl_error(stderr or stdout or "Namespace labels could not be applied")
                        )
                else:
                    raise RuntimeError(
                        redact_kubectl_error(stderr or stdout or "Namespace labels could not be applied")
                    )
        return await super().deploy(artifact, execution_context, namespace_created=bool(rbac))

    async def cleanup(self, execution, artifact, *, force=False):
        from llm_d_lens.deploy.slice_capacity import (
            ClusterInventoryAuthError,
            is_forbidden_output,
            is_unauthorized_output,
            redact_kubectl_error,
        )

        result = await super().cleanup(execution, artifact, force=force)
        if not result["cleaned_up"]:
            return result
        namespace = self._namespace(execution)
        for resource in artifact.deployment_contract.get("routerRbac", []):
            name = f"{resource['kind'].lower()}/{resource['name']}"
            status, stdout, stderr = await self._runner(
                ["kubectl", "get", name, "--namespace", namespace, "--ignore-not-found=true", "-o", "json"]
            )
            if status and is_unauthorized_output(stdout, stderr):
                raise ClusterInventoryAuthError(
                    redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                )
            if status and is_forbidden_output(stdout, stderr):
                continue
            if not status and stdout.strip():
                status, stdout, stderr = await self._runner(
                    [
                        "kubectl",
                        "wait",
                        name,
                        "--namespace",
                        namespace,
                        "--for=delete",
                        f"--timeout={self._readiness_timeout_seconds}s",
                    ]
                )
                if status and is_unauthorized_output(stdout, stderr):
                    raise ClusterInventoryAuthError(
                        redact_kubectl_error((stderr or stdout or "Kubernetes authentication failed (401)").strip())
                    )
                if status and is_forbidden_output(stdout, stderr):
                    continue
            if status and (
                ("(NotFound)" in stderr and resource["name"] in stderr)
                or stderr.strip() == "error: no matching resources found"
            ):
                status = 0
            if status:
                return {
                    "cleaned_up": False,
                    "error": redact_kubectl_error(stderr or stdout or f"Router RBAC cleanup pending: {name}"),
                }
        return result

    @staticmethod
    def _resources(artifact, kinds):
        return [
            doc
            for doc in yaml.safe_load_all(Path(artifact.manifest_ref).read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("kind") in kinds
        ]

    async def readiness(self, execution):
        result = await super().readiness(execution)
        if not result.accepted:
            return result
        for doc in self._resources(execution["_artifact"], {"LeaderWorkerSet", "DisaggregatedSet"}):
            resource = f"{doc['kind'].lower()}/{doc['metadata']['name']}"
            status, stdout, stderr = await self._runner(
                [
                    "kubectl",
                    "wait",
                    resource,
                    "--namespace",
                    execution["namespace"],
                    "--for=condition=Available",
                    f"--timeout={self._readiness_timeout_seconds}s",
                ]
            )
            if status:
                return ValidationResult(False, [(stderr or stdout or f"{resource} not available").strip()])
        return result

    async def stop(self, execution, artifact):
        namespace = self._namespace(execution)
        # Remove reconcilers first; reapplying the reviewed snapshot restores them.
        for doc in self._resources(artifact, {"ScaledObject", "HorizontalPodAutoscaler"}):
            resource = f"{doc['kind'].lower()}/{doc['metadata']['name']}"
            status, stdout, stderr = await self._runner(
                ["kubectl", "delete", resource, "--namespace", namespace, "--ignore-not-found=true"]
            )
            if status:
                return {"stopped": False, "error": stderr or stdout or f"Could not stop {resource}"}
        result = await super().stop(execution, artifact)
        for doc in self._resources(artifact, {"LeaderWorkerSet", "DisaggregatedSet"}):
            resource = f"{doc['kind'].lower()}/{doc['metadata']['name']}"
            command = ["kubectl", "scale", resource, "--namespace", execution["namespace"], "--replicas=0"]
            if doc["kind"] == "DisaggregatedSet":
                patch = [
                    {"op": "add", "path": f"/spec/roles/{index}/spec/replicas", "value": 0}
                    for index, _ in enumerate(doc["spec"]["roles"])
                ]
                command = [
                    "kubectl",
                    "patch",
                    resource,
                    "--namespace",
                    execution["namespace"],
                    "--type=json",
                    "--patch",
                    json.dumps(patch),
                ]
            status, stdout, stderr = await self._runner(command)
            if status:
                result["stopped"] = False
                result["error"] = stderr or stdout or f"Could not stop {resource}"
        return result

    def _uses_delegate_lifecycle(self, artifact):
        return False
