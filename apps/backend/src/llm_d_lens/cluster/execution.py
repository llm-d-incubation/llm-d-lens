"""Database-backed execution lock for guarded cluster mutations.

The lock lives on the cluster row (``execution_token``,
``execution_started_at``, ``execution_heartbeat_at``,
``execution_cancel_requested``) so every worker agrees. See
``docs/design/cluster-permission-preflight-design.md`` sections 4.4, 4.4.1, 7
and 12.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import uuid4

from llm_d_lens.db.dao.cluster import ClusterDao
from llm_d_lens.infrastructure.process import CommandResult

from . import gateway_crds as _gateway_crds
from . import remediation as _remediation
from .capabilities import enabled_steps
from .remediation import (
    AdminScriptSourceUnavailableError,
    AdminScriptValidationError,
    validate_crd_yaml,
    validate_field_manager,
)

resolve_chart_crds = _remediation.resolve_chart_crds
_DEFAULT_RESOLVE_CHART_CRDS = _remediation.resolve_chart_crds
invalidate_cluster_discovery_cache = _gateway_crds.invalidate_cluster_discovery_cache
_DEFAULT_INVALIDATE_DISCOVERY_CACHE = _gateway_crds.invalidate_cluster_discovery_cache

__all__ = [
    "EXECUTION_STALE_SECONDS",
    "HEARTBEAT_SECONDS",
    "ExecutionInProgress",
    "acquire",
    "cancel",
    "complete",
    "completed_at",
    "completed_plan_hash",
    "OperationExecutionError",
    "OperationPermissionError",
    "OperationUnauthorizedError",
    "OperationWaitFailedError",
    "enabled_steps",
    "execute_lens_operation",
    "execute_user_operation",
    "finalize",
    "heartbeat",
    "is_completed",
    "mark_operation",
    "mark_operations",
    "now_utc",
    "operations",
    "refresh",
    "release",
    "state",
]

#: A run whose heartbeat is older than this can be taken over.
EXECUTION_STALE_SECONDS = 300
#: Long-running steps refresh the heartbeat at this interval.
HEARTBEAT_SECONDS = 30


class ExecutionInProgress(Exception):  # noqa: N818 - name fixed by the design contract
    """Another worker holds a live execution lock for the cluster."""


def _now() -> datetime:
    return datetime.now(UTC)


def now_utc() -> datetime:
    """Current UTC time; split out so tests can observe the same clock."""

    return _now()


def _dao() -> ClusterDao:
    return ClusterDao()


def acquire(cluster_id: str, *, now: datetime | None = None) -> str:
    """Acquire the execution lock and return its token.

    Takes over an idle or stale lock; a live lock raises
    :class:`ExecutionInProgress`. Acquiring resets the cancel flag and clears
    both ledgers and the completion columns.

    M3b contract: ``admin-handoff/verify`` must call this as the run's first
    acquire and return the token; the following user operations reuse that
    token. It records ``admin_done`` only after acquiring, because a later
    acquire would clear the ledger.
    """

    moment = now or _now()
    stale_before = moment - timedelta(seconds=EXECUTION_STALE_SECONDS)
    token = uuid4().hex
    if not _dao().try_acquire_execution(cluster_id, token, now=moment, stale_before=stale_before):
        raise ExecutionInProgress(f"cluster {cluster_id!r} already has an active execution")
    return token


def refresh(cluster_id: str, token: str, *, now: datetime | None = None) -> bool:
    """Refresh the heartbeat for ``token``; false when the token is not the live one."""

    moment = now or _now()
    current = _dao().get_execution_state(cluster_id)
    if not current or current.get("token") != token:
        return False
    heartbeat = current.get("heartbeat_at")
    if heartbeat is None:
        return False
    if heartbeat < moment - timedelta(seconds=EXECUTION_STALE_SECONDS):
        # The run already expired; a new worker may take over instead of
        # resurrecting a dead heartbeat.
        return False
    return _dao().refresh_execution_heartbeat(cluster_id, token, moment)


def cancel(cluster_id: str, token: str, *, force: bool = False) -> bool:
    """Request cancellation for the run holding ``token`` (or any live run)."""

    return _dao().request_execution_cancel(cluster_id, token, force=force)


@asynccontextmanager
async def heartbeat(
    cluster_id: str,
    token: str,
    *,
    interval: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[None]:
    """Refresh the lock heartbeat while a long-running step is executing.

    The refresh task starts when the context is entered and is cancelled on
    exit, including on an exception. A failed refresh is ignored: the next step
    still reloads the lock and fails closed if the run expired or was taken
    over.
    """

    async def _beat() -> None:
        while True:
            await asyncio.sleep(interval)
            refresh(cluster_id, token)

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def release(cluster_id: str, token: str) -> bool:
    """Clear the lock, heartbeat and cancel flag when ``token`` still owns it."""

    return _dao().release_execution(cluster_id, token)


def complete(
    cluster_id: str,
    token: str | None,
    *,
    now: datetime | None = None,
    plan_hash: str | None = None,
) -> bool:
    """Complete the run owned by ``token`` and record the plan hash it completed."""

    if not token:
        return False
    return _dao().complete_execution(cluster_id, token, now=now, plan_hash=plan_hash)


def finalize(cluster_id: str, token: str | None, *, plan_hash: str, now: datetime | None = None) -> bool:
    """Atomically set ``draft=false`` and release the run token."""

    if not token:
        return False
    return _dao().finalize_execution(cluster_id, token, now=now, plan_hash=plan_hash)


def completed_plan_hash(cluster_id: str) -> str | None:
    """Plan hash recorded by the most recent finalized run."""

    return _dao().get_execution_completed_plan_hash(cluster_id)


def is_completed(cluster_id: str, plan_hash: str | None = None) -> bool:
    """Whether this cluster's run completed for ``plan_hash`` (or any run when omitted)."""

    return _dao().is_execution_completed(cluster_id, plan_hash)


def completed_at(cluster_id: str) -> datetime | None:
    """When this cluster's current plan run completed, or ``None``."""

    return _dao().get_execution_completed_at(cluster_id)


def mark_operation(cluster_id: str, token: str | None, operation_id: str, status: str) -> bool:
    """Record an operation status for the run owned by ``token``.

    M3b's verify path acquires the run first and then records ``admin_done``;
    user operations reuse that token. Never acquire after marking admin done,
    because a new acquire clears the operation ledger.
    """

    if not token:
        return False
    return _dao().mark_execution_operation(cluster_id, token, operation_id, status)


def mark_operations(cluster_id: str, token: str | None, statuses: dict[str, str]) -> bool:
    """Atomically record several operation statuses for the run owned by ``token``."""

    if not token:
        return False
    return _dao().mark_execution_operations(cluster_id, token, statuses)


def operations(cluster_id: str) -> dict[str, str] | None:
    """Return the current plan's operation ledger (``None`` for an unknown cluster)."""

    return _dao().get_execution_operations(cluster_id)


def state(cluster_id: str) -> dict[str, Any] | None:
    """Return the raw lock row, or ``None`` when the cluster does not exist."""

    return _dao().get_execution_state(cluster_id)


class OperationExecutionError(Exception):
    """A user or lens operation could not be applied."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "operation_failed",
        operation_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.operation_id = operation_id
        self.extra = dict(extra or {})


class OperationPermissionError(OperationExecutionError):
    """The edit kubeconfig was rejected by the API server."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(
            f"Operation {operation_id!r} was rejected by Kubernetes permissions",
            status_code=403,
            code="kubernetes_permission_denied",
            operation_id=operation_id,
        )


class OperationUnauthorizedError(OperationExecutionError):
    """The edit kubeconfig is invalid or expired."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(
            f"Operation {operation_id!r} could not authenticate to Kubernetes",
            status_code=401,
            code="kubeconfig_unauthorized",
            operation_id=operation_id,
        )


class OperationWaitFailedError(OperationExecutionError):
    """An operation applied but did not become ready."""

    def __init__(self, operation_id: str, wait_kind: str) -> None:
        super().__init__(
            f"Operation {operation_id!r} did not pass its {wait_kind} wait",
            status_code=504,
            code="operation_wait_failed",
            operation_id=operation_id,
        )


def _operation_runner(cluster_id: str) -> Any:
    from llm_d_lens.infrastructure.kubernetes import scoped_runner  # noqa: PLC0415

    return scoped_runner(cluster_id)


async def _run_operation_command(
    runner: Any,
    argv: list[str],
    *,
    operation_id: str,
    input: str | None = None,  # noqa: A002 - subprocess-compatible public keyword
    timeout: float = 300,
) -> CommandResult:
    try:
        result = await runner.run(argv, input=input, timeout=timeout)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OperationExecutionError(
            f"operation {operation_id!r} could not run {argv[0]}: {error}",
            status_code=502,
            code="operation_failed",
            operation_id=operation_id,
        ) from error
    if not isinstance(result, CommandResult):
        raise OperationExecutionError(
            f"operation {operation_id!r} returned an invalid command result",
            status_code=502,
            code="operation_failed",
            operation_id=operation_id,
        )
    if result.returncode != 0:
        message = f"{result.stderr or ''} {result.stdout or ''}".lower()
        if "forbidden" in message:
            raise OperationPermissionError(operation_id)
        if "unauthorized" in message or "must be logged in" in message:
            raise OperationUnauthorizedError(operation_id)
        raise OperationExecutionError(
            f"operation {operation_id!r} failed",
            status_code=502,
            code="operation_failed",
            operation_id=operation_id,
        )
    return result


def _secret_copy_value(payload: str, key: str) -> str:
    import base64  # noqa: PLC0415

    try:
        data = json.loads(payload or "{}").get("data", {})
    except json.JSONDecodeError as error:
        raise OperationExecutionError(
            "source Secret returned invalid JSON",
            status_code=502,
            code="operation_failed",
        ) from error
    encoded = data.get(key) if isinstance(data, dict) else None
    if not isinstance(encoded, str):
        raise OperationExecutionError(
            f"source Secret is missing key {key!r}",
            status_code=502,
            code="operation_failed",
        )
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise OperationExecutionError(
            f"source Secret key {key!r} is not UTF-8 text",
            status_code=502,
            code="operation_failed",
        ) from error


def _inject_secret_values(
    documents: list[dict[str, Any]],
    *,
    secret_values: dict[str, str],
    copy_data: str | None = None,
    copy_key: str | None = None,
) -> None:
    for document in documents:
        if document.get("kind") != "Secret":
            continue
        string_data = document.setdefault("stringData", {})
        if not isinstance(string_data, dict):
            raise OperationExecutionError(
                "Secret manifest has invalid stringData",
                status_code=400,
                code="operation_invalid",
            )
        for key, value in secret_values.items():
            string_data[key] = value
        if copy_data is not None and copy_key is not None:
            string_data[copy_key] = copy_data


def _inject_proxy_env(documents: list[dict[str, Any]], cluster_id: str) -> None:
    from llm_d_lens.cluster.service import resolve_proxy_env  # noqa: PLC0415

    values = resolve_proxy_env(cluster_id)
    if not values:
        return
    environment = [{"name": name, "value": value} for name, value in sorted(values.items())]
    for document in documents:
        kind = document.get("kind")
        if kind in {"Deployment", "DaemonSet", "StatefulSet", "Job", "ReplicaSet"}:
            pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        elif kind == "Pod":
            pod_spec = document.get("spec", {})
        else:
            continue
        if not isinstance(pod_spec, dict):
            continue
        for container_key in ("initContainers", "containers"):
            for container in pod_spec.get(container_key, []) or []:
                if not isinstance(container, dict):
                    continue
                proxy_names = set(values)
                existing = [item for item in container.get("env", []) or [] if isinstance(item, dict)]
                remaining = [item for item in existing if item.get("name") not in proxy_names]
                remaining.extend(environment)
                container["env"] = remaining


def _apply_args(
    apply: Any,
    *,
    namespace: str | None,
    field_manager: str | None = None,
) -> list[str]:
    args = ["kubectl", "apply"]
    options = apply.apply_options
    if options is not None and options.server_side:
        args.append("--server-side")
    if options is not None and options.force_conflicts:
        args.append("--force-conflicts")
    if field_manager:
        try:
            validated_manager = validate_field_manager(field_manager)
        except AdminScriptValidationError as error:
            raise OperationExecutionError(
                str(error),
                status_code=400,
                code="operation_invalid",
            ) from error
        args.append(f"--field-manager={validated_manager}")
    if options is not None and options.timeout_seconds:
        args.append(f"--timeout={int(options.timeout_seconds)}s")
    if namespace:
        args.extend(["--namespace", namespace])
    args.extend(["-f", "-"])
    return args


def _wait_command(wait: Any, remaining: int) -> list[str]:
    timeout = f"--timeout={max(1, remaining)}s"
    namespace = ["--namespace", wait.namespace] if wait.namespace else []
    if wait.kind == "rollout":
        return ["kubectl", "rollout", "status", wait.target, *namespace, timeout]
    if wait.kind == "pvc-bound":
        return ["kubectl", "wait", "--for=jsonpath={.status.phase}=Bound", wait.target, *namespace, timeout]
    if wait.kind == "job-complete" and wait.target.startswith("jobs-by-label/"):
        return [
            "kubectl",
            "wait",
            "--for=condition=Complete",
            f"--selector={wait.target.removeprefix('jobs-by-label/')}",
            "jobs",
            *namespace,
            timeout,
        ]
    if wait.kind == "job-complete":
        return ["kubectl", "wait", "--for=condition=Complete", wait.target, *namespace, timeout]
    if wait.kind == "crd-established":
        return ["kubectl", "wait", "--for=condition=Established", wait.target, timeout]
    if wait.kind == "gateway-programmed":
        return ["kubectl", "wait", "--for=condition=Programmed", wait.target, *namespace, timeout]
    if wait.kind == "helm":
        return ["helm", "status", wait.target, *namespace]
    raise OperationExecutionError(
        f"unsupported wait kind {wait.kind!r}",
        status_code=400,
        code="operation_invalid",
    )


async def _run_wait_command(
    runner: Any,
    argv: list[str],
    *,
    operation_id: str,
    timeout: float,
) -> CommandResult:
    """Run a readiness command, preserving non-zero status for polling."""
    try:
        result = await runner.run(argv, timeout=timeout)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OperationExecutionError(
            f"operation {operation_id!r} could not run {argv[0]}: {error}",
            status_code=502,
            code="operation_failed",
            operation_id=operation_id,
        ) from error
    if not isinstance(result, CommandResult):
        raise OperationExecutionError(
            f"operation {operation_id!r} returned an invalid command result",
            status_code=502,
            code="operation_failed",
            operation_id=operation_id,
        )
    message = f"{result.stderr or ''} {result.stdout or ''}".lower()
    if result.returncode != 0 and "forbidden" in message:
        raise OperationPermissionError(operation_id)
    if result.returncode != 0 and ("unauthorized" in message or "must be logged in" in message):
        raise OperationUnauthorizedError(operation_id)
    return result


def _daemonset_matches_group(item: dict[str, Any], pairs: list[tuple[str, str]]) -> bool:
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or "").lower()
    namespace = str(metadata.get("namespace") or "")
    labels = {str(k): str(v) for k, v in (metadata.get("labels") or {}).items()}
    for key, value in pairs:
        if key == "name_contains":
            if value.lower() not in name:
                return False
        elif key == "name":
            if value.lower() != name:
                return False
        elif key == "namespace":
            if value != namespace:
                return False
        elif key == "label":
            lk, _, lv = value.partition("=")
            if labels.get(lk) != lv:
                return False
        elif labels.get(key) != value:
            return False
    return True


def _daemonset_ready_for_target(items: list[Any], target: str) -> bool:
    groups: list[list[tuple[str, str]]] = []
    for raw_group in target.split(";"):
        pairs = []
        for raw_pair in raw_group.split(","):
            pair = raw_pair.strip()
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            if key and value:
                pairs.append((key, value))
        if pairs:
            groups.append(pairs)
    if not groups:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status") or {}
        desired = int(status.get("desiredNumberScheduled") or 0)
        ready = int(status.get("numberReady") or 0)
        if desired <= 0 or ready < desired:
            continue
        if any(_daemonset_matches_group(item, group) for group in groups):
            return True
    return False


async def _check_driver_ready(
    runner: Any,
    *,
    operation_id: str,
    target: str,
    timeout: float,
    allow_forbidden: bool = False,
) -> bool:
    try:
        result = await _run_wait_command(
            runner,
            ["kubectl", "get", "daemonsets", "--all-namespaces", "-o", "json"],
            operation_id=operation_id,
            timeout=timeout,
        )
    except (OperationPermissionError, OperationExecutionError):
        if allow_forbidden:
            return False
        raise
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return _daemonset_ready_for_target(items if isinstance(items, list) else [], target)


_WAIT_SUBPROCESS_GRACE_SECONDS = 10
_WAIT_SLICE_SECONDS = 60
_GATEWAY_WAIT_SLICE_SECONDS = 15
_POLL_COMMAND_SLICE_SECONDS = 30
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
_NODE_PORT_RE = re.compile(r"nodePort:\s*(\d+)")


def _gateway_manifest_istio_hint(operation: Any) -> bool | None:
    """Return whether ``operation`` targets Istio from its manifests, or ``None`` if inconclusive."""
    manifests = getattr(getattr(operation, "apply", None), "manifests", None) or ()
    for document in manifests:
        if not isinstance(document, Mapping) or document.get("kind") != "GatewayClass":
            continue
        spec = document.get("spec")
        controller = str((spec.get("controllerName") if isinstance(spec, Mapping) else "") or "").lower()
        return "istio" in controller
    for document in manifests:
        if not isinstance(document, Mapping) or document.get("kind") != "Gateway":
            continue
        spec = document.get("spec")
        class_name = str((spec.get("gatewayClassName") if isinstance(spec, Mapping) else "") or "").strip().lower()
        if class_name:
            return class_name == "istio"
        metadata = document.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
        provider_label = (
            str((labels.get("lens.ai/gateway-provider") if isinstance(labels, Mapping) else "") or "").strip().lower()
        )
        if provider_label:
            return provider_label == "istio"
    return None


def _gateway_payload_is_istio(gateway_obj: Mapping[str, Any] | None) -> bool:
    if not isinstance(gateway_obj, Mapping):
        return False
    spec = gateway_obj.get("spec")
    if isinstance(spec, Mapping):
        class_name = str(spec.get("gatewayClassName") or "").strip().lower()
        if class_name:
            return class_name == "istio"
    metadata = gateway_obj.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
    if isinstance(labels, Mapping):
        provider_label = str(labels.get("lens.ai/gateway-provider") or "").strip().lower()
        if provider_label:
            return provider_label == "istio"
    return False


def _gateway_node_port(operation: Any) -> int | None:
    manifests = getattr(getattr(operation, "apply", None), "manifests", None) or ()
    for document in manifests:
        if not isinstance(document, Mapping) or document.get("kind") != "ConfigMap":
            continue
        data = document.get("data")
        if not isinstance(data, Mapping):
            continue
        service = data.get("service")
        if not isinstance(service, str):
            continue
        match = _NODE_PORT_RE.search(service)
        if match is not None:
            value = int(match.group(1))
            if 30000 <= value <= 32767:
                return value
    return None


def _gateway_wait_target(wait: Any) -> tuple[str, str] | None:
    target = getattr(wait, "target", None)
    namespace = getattr(wait, "namespace", None)
    if not isinstance(target, str) or not isinstance(namespace, str) or not namespace:
        return None
    if len(namespace) > 63 or not _DNS1123_LABEL_RE.fullmatch(namespace):
        return None
    kind, sep, name = target.partition("/")
    if sep != "/" or kind != "gateway" or not name or "/" in name:
        return None
    if len(name) > 246 or not _DNS1123_SUBDOMAIN_RE.fullmatch(name):
        return None
    return name, namespace


def _parse_kubectl_json_object(stdout: str | None) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("items"), list):
        items = payload["items"]
        first = items[0] if items else None
        return first if isinstance(first, dict) else None
    return payload


def _gateway_has_address_not_assigned(gateway_obj: Mapping[str, Any] | None) -> bool:
    if not isinstance(gateway_obj, Mapping):
        return False
    status = gateway_obj.get("status")
    if not isinstance(status, Mapping):
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        if (
            condition.get("type") == "Programmed"
            and str(condition.get("status")) == "False"
            and condition.get("reason") == "AddressNotAssigned"
        ):
            return True
    return False


async def _maybe_fallback_istio_gateway_to_nodeport(
    runner: Any,
    operation: Any,
    wait: Any,
    *,
    deadline: float,
) -> bool:
    if getattr(operation, "id", None) != "gateway.manifests":
        return False
    parsed_target = _gateway_wait_target(wait)
    if parsed_target is None:
        return False
    istio_hint = _gateway_manifest_istio_hint(operation)
    if istio_hint is False:
        return False
    remaining_seconds = deadline - time.monotonic()
    if remaining_seconds <= 0:
        return False
    gateway_name, gateway_namespace = parsed_target
    get_slice = min(max(1, int(remaining_seconds)), _GATEWAY_WAIT_SLICE_SECONDS)
    get_result = await _run_wait_command(
        runner,
        ["kubectl", "get", wait.target, "--namespace", gateway_namespace, "-o", "json"],
        operation_id=operation.id,
        timeout=get_slice + _WAIT_SUBPROCESS_GRACE_SECONDS,
    )
    if get_result.returncode != 0:
        return False
    gateway_obj = _parse_kubectl_json_object(get_result.stdout)
    if istio_hint is None and not _gateway_payload_is_istio(gateway_obj):
        return False
    if not _gateway_has_address_not_assigned(gateway_obj):
        return False
    if time.monotonic() >= deadline:
        return False
    import yaml  # noqa: PLC0415

    config_name = f"{gateway_name}-config"
    node_port = _gateway_node_port(operation)
    service_lines = ["spec:", "  type: NodePort"]
    if node_port is not None:
        service_lines.extend(
            [
                "  ports:",
                "  - name: http",
                "    port: 80",
                f"    nodePort: {node_port}",
            ]
        )
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": gateway_namespace},
        "data": {"service": "\n".join(service_lines) + "\n"},
    }
    await _run_operation_command(
        runner,
        ["kubectl", "apply", "--namespace", gateway_namespace, "-f", "-"],
        operation_id=operation.id,
        input=yaml.safe_dump(config_map, sort_keys=False),
        timeout=60,
    )
    patch_payload = json.dumps(
        {
            "spec": {
                "infrastructure": {
                    "parametersRef": {
                        "group": "",
                        "kind": "ConfigMap",
                        "name": config_name,
                    }
                }
            }
        },
        separators=(",", ":"),
    )
    await _run_operation_command(
        runner,
        [
            "kubectl",
            "patch",
            wait.target,
            "--namespace",
            gateway_namespace,
            "--type=merge",
            "-p",
            patch_payload,
        ],
        operation_id=operation.id,
        timeout=60,
    )
    return True


async def _wait_for_operation(runner: Any, operation: Any, wait: Any) -> None:
    deadline = time.monotonic() + max(1, int(wait.timeout_seconds))
    nodeport_retried = False
    while True:
        remaining = max(1, int(deadline - time.monotonic()))
        if wait.kind == "node-label":
            slice_seconds = min(remaining, _POLL_COMMAND_SLICE_SECONDS)
            result = await _run_wait_command(
                runner,
                ["kubectl", "get", "nodes", "-l", wait.target, "-o", "name"],
                operation_id=operation.id,
                timeout=slice_seconds + _WAIT_SUBPROCESS_GRACE_SECONDS,
            )
            if (result.stdout or "").strip():
                return
        elif wait.kind == "driver-status":
            slice_seconds = min(remaining, _POLL_COMMAND_SLICE_SECONDS)
            if await _check_driver_ready(
                runner,
                operation_id=operation.id,
                target=wait.target,
                timeout=slice_seconds + _WAIT_SUBPROCESS_GRACE_SECONDS,
            ):
                return
        else:
            if wait.kind == "gateway-programmed":
                slice_seconds = min(remaining, _GATEWAY_WAIT_SLICE_SECONDS)
            elif wait.kind == "helm":
                slice_seconds = min(remaining, _POLL_COMMAND_SLICE_SECONDS)
            else:
                slice_seconds = min(remaining, _WAIT_SLICE_SECONDS)
            result = await _run_wait_command(
                runner,
                _wait_command(wait, slice_seconds),
                operation_id=operation.id,
                timeout=slice_seconds + _WAIT_SUBPROCESS_GRACE_SECONDS,
            )
            if result.returncode == 0:
                return
            if (
                wait.kind == "gateway-programmed"
                and not nodeport_retried
                and await _maybe_fallback_istio_gateway_to_nodeport(
                    runner,
                    operation,
                    wait,
                    deadline=deadline,
                )
            ):
                nodeport_retried = True
                continue
        if time.monotonic() >= deadline:
            raise OperationWaitFailedError(operation.id, wait.kind)
        await asyncio.sleep(1)


async def _execute_operation_apply(
    cluster_id: str,
    operation: Any,
    *,
    runner: Any,
    secret_values: dict[str, str],
) -> None:
    import copy  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    apply = operation.apply
    if apply.kind == "command":
        configured_timeout = (
            int(apply.apply_options.timeout_seconds)
            if apply.apply_options is not None and apply.apply_options.timeout_seconds > 0
            else None
        )
        timeout = configured_timeout or (900 if apply.argv and apply.argv[0] == "helm" else 300)
        await _run_operation_command(
            runner,
            list(apply.argv),
            operation_id=operation.id,
            input=apply.stdin,
            timeout=timeout,
        )
        return
    if apply.kind == "helm":
        configured_timeout = (
            int(apply.apply_options.timeout_seconds)
            if apply.apply_options is not None and apply.apply_options.timeout_seconds > 0
            else None
        )
        timeout = configured_timeout or 900
        await _run_operation_command(
            runner,
            list(apply.argv),
            operation_id=operation.id,
            input=apply.stdin,
            timeout=timeout,
        )
        return
    if apply.kind == "manifests_from_chart":
        source = apply.manifests_source
        if source is None:
            raise OperationExecutionError(
                f"operation {operation.id!r} has no chart source",
                status_code=400,
                code="operation_invalid",
                operation_id=operation.id,
            )
        if apply.apply_options is not None and apply.apply_options.delete_before_apply:
            raise OperationExecutionError(
                f"operation {operation.id!r} uses unsupported delete_before_apply for chart CRDs",
                status_code=400,
                code="operation_invalid",
                operation_id=operation.id,
            )
        field_manager: str | None = None
        if source.field_manager:
            try:
                field_manager = validate_field_manager(source.field_manager)
            except AdminScriptValidationError as error:
                raise OperationExecutionError(
                    str(error),
                    status_code=400,
                    code="operation_invalid",
                    operation_id=operation.id,
                ) from error
        elif source.field_manager is not None and not isinstance(source.field_manager, str):
            raise OperationExecutionError(
                f"operation {operation.id!r} has invalid field_manager",
                status_code=400,
                code="operation_invalid",
                operation_id=operation.id,
            )
        chart_resolver = (
            resolve_chart_crds
            if resolve_chart_crds is not _DEFAULT_RESOLVE_CHART_CRDS
            else _remediation.resolve_chart_crds
        )
        try:
            resolved = await asyncio.to_thread(chart_resolver, source)
            validate_crd_yaml(resolved.yaml, operation.id)
        except (AdminScriptSourceUnavailableError, AdminScriptValidationError) as error:
            raise OperationExecutionError(
                str(error),
                status_code=503,
                code="admin_script_source_unavailable",
                operation_id=operation.id,
            ) from error
        configured_timeout = (
            int(apply.apply_options.timeout_seconds)
            if apply.apply_options is not None and apply.apply_options.timeout_seconds > 0
            else None
        )
        await _run_operation_command(
            runner,
            _apply_args(apply, namespace=apply.namespace, field_manager=field_manager),
            operation_id=operation.id,
            input=resolved.yaml,
            timeout=configured_timeout or 300,
        )
        return
    if apply.kind != "manifests":
        raise OperationExecutionError(
            f"operation {operation.id!r} cannot be executed by the user executor",
            status_code=409,
            code="operation_invalid",
            operation_id=operation.id,
        )
    documents = copy.deepcopy(list(apply.manifests))
    if not documents:
        raise OperationExecutionError(
            f"operation {operation.id!r} has no manifests",
            status_code=400,
            code="operation_invalid",
            operation_id=operation.id,
        )
    if apply.inject_proxy_env:
        _inject_proxy_env(documents, cluster_id)
    copy_data = None
    copy_key = None
    if apply.secret_copy is not None:
        copied = await _run_operation_command(
            runner,
            [
                "kubectl",
                "get",
                "secret",
                apply.secret_copy.source_name,
                "--namespace",
                apply.secret_copy.source_namespace,
                "-o",
                "json",
            ],
            operation_id=operation.id,
            timeout=30,
        )
        copy_key = apply.secret_copy.key
        copy_data = _secret_copy_value(copied.stdout or "", copy_key)
    _inject_secret_values(
        documents,
        secret_values=secret_values,
        copy_data=copy_data,
        copy_key=copy_key,
    )
    yaml_text = yaml.safe_dump_all(documents, sort_keys=False)
    check_namespace = apply.namespace
    if apply.create_if_absent:
        if not check_namespace:
            for document in documents:
                if document.get("kind") == "Namespace":
                    check_namespace = (document.get("metadata") or {}).get("name")
                    break
        if check_namespace:
            existing = await _run_wait_command(
                runner,
                ["kubectl", "get", "namespace", check_namespace],
                operation_id=operation.id,
                timeout=30,
            )
            if existing.returncode == 0:
                return
    apply_namespace = None if all(document.get("kind") == "Namespace" for document in documents) else apply.namespace
    if apply.apply_options is not None and apply.apply_options.delete_before_apply:
        await _run_operation_command(
            runner,
            ["kubectl", "delete", "--ignore-not-found", "-f", "-"],
            operation_id=operation.id,
            input=yaml_text,
            timeout=120,
        )
    await _run_operation_command(
        runner,
        _apply_args(apply, namespace=apply_namespace),
        operation_id=operation.id,
        input=yaml_text,
        timeout=300,
    )


async def execute_user_operation(
    cluster_id: str,
    operation: Any,
    *,
    secret_values: dict[str, str] | None = None,
    runner: Any | None = None,
) -> None:
    """Apply one user operation and wait for its readiness checks."""
    active_runner = runner or _operation_runner(cluster_id)
    skip_target = getattr(operation.apply, "skip_if_driver_ready", None)
    if skip_target and await _check_driver_ready(
        active_runner,
        operation_id=operation.id,
        target=skip_target,
        timeout=30,
        allow_forbidden=True,
    ):
        return
    should_invalidate_discovery = operation.apply.kind == "manifests_from_chart" or any(
        getattr(wait, "kind", None) == "crd-established" for wait in (operation.apply.wait or ())
    )
    values = dict(secret_values or {})
    try:
        await _execute_operation_apply(cluster_id, operation, runner=active_runner, secret_values=values)
        for wait in operation.apply.wait:
            await _wait_for_operation(active_runner, operation, wait)
    finally:
        if should_invalidate_discovery:
            invalidator = (
                invalidate_cluster_discovery_cache
                if invalidate_cluster_discovery_cache is not _DEFAULT_INVALIDATE_DISCOVERY_CACHE
                else _gateway_crds.invalidate_cluster_discovery_cache
            )
            with suppress(Exception):
                invalidator(cluster_id)


def _raise_lens_error(operation_id: str, error: Exception, *, fallback_message: str) -> NoReturn:
    message = str(error).lower()
    if "forbidden" in message:
        raise OperationPermissionError(operation_id) from error
    if "unauthorized" in message or "must be logged in" in message:
        raise OperationUnauthorizedError(operation_id) from error
    raise OperationExecutionError(
        fallback_message,
        status_code=502,
        code="operation_failed",
        operation_id=operation_id,
    ) from error


def _service_node_port(service: dict[str, Any], preferred_port: int) -> int | None:
    ports = (service.get("spec") or {}).get("ports")
    if not isinstance(ports, list):
        return None
    fallback: int | None = None
    for entry in ports:
        if not isinstance(entry, dict):
            continue
        raw_np = entry.get("nodePort")
        if isinstance(raw_np, bool) or not isinstance(raw_np, int) or not (30000 <= raw_np <= 32767):
            continue
        if entry.get("port") == preferred_port:
            return raw_np
        if fallback is None:
            fallback = raw_np
    return fallback


def _extract_monitoring_service_ports(payload: Any) -> tuple[int | None, int | None]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None, None
    prom_port: int | None = None
    graf_port: int | None = None
    for svc in items:
        if not isinstance(svc, dict):
            continue
        name = str((svc.get("metadata") or {}).get("name") or "")
        if prom_port is None and name.endswith("-prometheus"):
            prom_port = _service_node_port(svc, 9090)
        elif graf_port is None and name.endswith("-grafana"):
            graf_port = _service_node_port(svc, 80)
    if prom_port is not None and graf_port == prom_port:
        graf_port = None
    return prom_port, graf_port


def _extract_node_ip(payload: Any) -> str | None:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    external_fallback: str | None = None
    for node in items:
        if not isinstance(node, dict):
            continue
        addresses = (node.get("status") or {}).get("addresses")
        if not isinstance(addresses, list):
            continue
        for addr in addresses:
            if not isinstance(addr, dict):
                continue
            addr_type = addr.get("type")
            raw_val = str(addr.get("address") or "").strip()
            if not raw_val:
                continue
            if addr_type == "InternalIP":
                return raw_val
            if addr_type == "ExternalIP" and external_fallback is None:
                external_fallback = raw_val
    return external_fallback


async def execute_lens_operation(
    cluster_id: str,
    plan: Any,
    operation: Any,
    *,
    runner: Any | None = None,
) -> None:
    """Run one Lens-local operation and its readiness waits."""
    active_runner = runner or _operation_runner(cluster_id)
    action = operation.lens_action
    if action == "gateway-exposure":
        from llm_d_lens.cluster.registry import get_cluster  # noqa: PLC0415
        from llm_d_lens.model_service.gateway_ops import GatewayOpsError, GatewayOpsService  # noqa: PLC0415

        cluster = get_cluster(cluster_id)
        port = plan.gateway.port or (getattr(cluster, "gateway_port", None) if cluster else None)
        if port:
            try:
                await GatewayOpsService().ensure_cluster_exposure(
                    cluster_id,
                    plan.gateway.provider,
                    plan.gateway.namespace,
                    plan.gateway.name,
                    int(port),
                )
            except GatewayOpsError as error:
                _raise_lens_error(
                    operation.id,
                    error,
                    fallback_message=f"gateway exposure failed for cluster {cluster_id}",
                )
    elif action == "model-cache-jobs":
        from llm_d_lens.cluster.plan import plan_hash16  # noqa: PLC0415
        from llm_d_lens.model_cache import jobs  # noqa: PLC0415
        from llm_d_lens.model_cache.contracts import ModelCacheEntry  # noqa: PLC0415
        from llm_d_lens.model_cache.service import ModelCacheNotFoundError  # noqa: PLC0415
        from llm_d_lens.model_cache.service import default_service as cache_service  # noqa: PLC0415
        from llm_d_lens.storage.service import default_service as storage_service  # noqa: PLC0415

        entry_id = plan.model_cache.entry_id
        if not entry_id:
            raise OperationExecutionError(
                "model-cache entry identity is missing",
                status_code=400,
                code="operation_invalid",
                operation_id=operation.id,
            )
        service = cache_service()
        try:
            entry = await service.get(entry_id)
        except ModelCacheNotFoundError as error:
            raise OperationExecutionError(
                str(error),
                status_code=409,
                code="operation_invalid",
                operation_id=operation.id,
            ) from error
        entry_cluster_id = getattr(entry, "cluster_id", None)
        if entry_cluster_id is not None and entry_cluster_id != cluster_id:
            raise OperationExecutionError(
                f"model-cache entry {entry_id!r} belongs to another cluster",
                status_code=409,
                code="operation_invalid",
                operation_id=operation.id,
            )
        if (
            plan.storage.volume_id
            and entry_cluster_id is not None
            and entry.storage_volume_id != plan.storage.volume_id
        ):
            raise OperationExecutionError(
                f"model-cache entry {entry_id!r} references another storage volume",
                status_code=409,
                code="operation_invalid",
                operation_id=operation.id,
            )
        volume = storage_service().get(entry.storage_volume_id)
        if volume is None:
            raise OperationExecutionError(
                "model-cache storage volume no longer exists",
                status_code=409,
                code="operation_invalid",
                operation_id=operation.id,
            )
        volume_cluster_id = getattr(volume, "cluster_id", None)
        if volume_cluster_id is not None and volume_cluster_id != cluster_id:
            raise OperationExecutionError(
                f"model-cache storage volume {entry.storage_volume_id!r} belongs to another cluster",
                status_code=409,
                code="operation_invalid",
                operation_id=operation.id,
            )
        try:
            current_hash16 = plan_hash16(plan)
            if "plan_hash" in inspect.signature(jobs.start_download).parameters:
                updated_entry = await jobs.start_download(entry, volume=volume, plan_hash=current_hash16)
            else:
                updated_entry = await jobs.start_download(entry, volume=volume)
        except jobs.ModelCacheJobError as error:
            _raise_lens_error(
                operation.id,
                error,
                fallback_message=f"model-cache job launch failed for cluster {cluster_id}: {error}",
            )
        if isinstance(updated_entry, ModelCacheEntry):
            store = getattr(service, "_store", None)
            if store is not None and callable(getattr(store, "save", None)):
                store.save(updated_entry)
    elif action == "monitoring-verify":
        from llm_d_lens.cluster import registry  # noqa: PLC0415
        from llm_d_lens.monitoring.prometheus import check_prometheus_health  # noqa: PLC0415

        settings = registry.get_cluster_monitoring_settings(cluster_id)
        if plan.monitoring.mode == "managed" and not (settings or {}).get("prometheus_url"):
            monitoring_ns = plan.monitoring.namespace
            prom_port: int | None = None
            graf_port: int | None = None
            node_ip: str | None = None

            svc_result = await _run_wait_command(
                active_runner,
                ["kubectl", "get", "svc", "-n", monitoring_ns, "-o", "json"],
                operation_id=operation.id,
                timeout=30,
            )
            if svc_result.returncode == 0:
                try:
                    svc_payload = json.loads(svc_result.stdout or "{}")
                except json.JSONDecodeError:
                    svc_payload = {}
                prom_port, graf_port = _extract_monitoring_service_ports(svc_payload)

            nodes_result = await _run_wait_command(
                active_runner,
                ["kubectl", "get", "nodes", "-o", "json"],
                operation_id=operation.id,
                timeout=30,
            )
            if nodes_result.returncode == 0:
                try:
                    nodes_payload = json.loads(nodes_result.stdout or "{}")
                except json.JSONDecodeError:
                    nodes_payload = {}
                node_ip = registry.extract_url_host(_extract_node_ip(nodes_payload))

            cluster = registry.get_cluster(cluster_id)
            if cluster is not None and (prom_port is not None or graf_port is not None or node_ip is not None):
                registry.persist_discovered_monitoring_endpoints(
                    cluster,
                    plan,
                    prometheus_node_port=prom_port,
                    grafana_node_port=graf_port,
                    host=node_ip,
                )
                settings = registry.get_cluster_monitoring_settings(cluster_id)

        prometheus_url = (settings or {}).get("prometheus_url")
        if not prometheus_url:
            if plan.monitoring.mode == "existing":
                raise OperationExecutionError(
                    "Prometheus URL is required for existing monitoring mode",
                    status_code=400,
                    code="monitoring_prometheus_url_missing",
                    operation_id=operation.id,
                )
            raise OperationExecutionError(
                "Could not discover a Prometheus endpoint for managed monitoring",
                status_code=502,
                code="monitoring_discovery_failed",
                operation_id=operation.id,
            )

        attempts = 3 if plan.monitoring.mode == "managed" else 1
        healthy = False
        for attempt in range(attempts):
            healthy = await check_prometheus_health(prometheus_url, timeout=5.0)
            if healthy:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(2)
        if not healthy:
            raise OperationExecutionError(
                f"Prometheus at {prometheus_url} is not reachable or ready",
                status_code=502,
                code="monitoring_unhealthy",
                operation_id=operation.id,
            )
    else:
        raise OperationExecutionError(
            f"unsupported Lens operation {action!r}",
            status_code=400,
            code="operation_invalid",
            operation_id=operation.id,
        )
    for wait in operation.apply.wait:
        await _wait_for_operation(active_runner, operation, wait)
