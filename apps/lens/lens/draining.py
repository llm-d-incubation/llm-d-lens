"""Drain the pods ``llmd scale`` removes: out of the router's pool, idle, then removed.

Lowering a workload stops pods that may hold requests in flight. ``llmd scale --drain-idle`` drains
them first with what the cluster already offers, for a stack whose InferencePool selects serving
pods by a label (``--pool-label``, "true" while a pod is in the pool):

1. Pick the pods. A Deployment's ReplicaSet chooses which pods a scale-down removes, so llmd drains
   the Ready pods with the fewest requests and marks them with the lowest
   ``controller.kubernetes.io/pod-deletion-cost``; the ReplicaSet removes those first among Ready
   pods. A DisaggregatedSet role loses its highest groups, so llmd drains those.
2. Set the pool label to ``false``. The InferencePool no longer selects the pods, so the router
   sends them nothing new. Every InferencePool that selects a pod must select on the label, or the
   pod would stay in that pool; llmd checks this first.
3. Wait until every vLLM engine on them is idle: no running or waiting request and no KV cache in
   use on several reads in a row, then no request finished during a grace period. The metrics are
   read through the API server's pod proxy, so nothing runs inside the pods.
4. Lower the count by the pods drained, and check that those pods are the ones going away.

``--batch N`` drains up to N pods (or groups) together. A drain that does not reach idle in time
puts the pods back in the pool and lowers nothing. The ReplicaSet removes a pod that is not Ready
before it looks at the deletion cost, so a Deployment with such a pod is refused; so is a workload
whose rollout is in progress.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import health, overrides
from .errors import CommandError, ConflictError, NotReadyError, UsageError
from .kube import Kube

DELETION_COST = "controller.kubernetes.io/pod-deletion-cost"
#: The lowest int32: the ReplicaSet removes a pod with this cost before any other Ready pod.
LOWEST_COST = str(-(2**31))
#: vLLM's gauges and counter, summed over the engines a pod serves.
RUNNING = "vllm:num_requests_running"
WAITING = "vllm:num_requests_waiting"
KV_USAGE = ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")  # newer and older vLLM
FINISHED = "vllm:request_success_total"
#: InferencePool API versions whose live pools are checked, and where each keeps its selector.
POOL_VERSIONS = ("inference.networking.k8s.io/v1", "inference.networking.x-k8s.io/v1alpha2")
#: How long the controller may take to start removing the drained pods after the count is lowered.
REMOVAL_TIMEOUT = 600


@dataclass
class Settings:
    pool_label: str
    #: Pods (or groups) drained together.
    batch: int = 1
    #: Seconds a drain may take before the pods go back in the pool.
    timeout: float = 1800
    #: Container ports to read metrics from; empty means every port a container declares.
    ports: tuple[int, ...] = ()
    interval: float = 10
    #: Reads in a row with nothing running, waiting or cached, before the grace period.
    idle_reads: int = 3
    grace: float = 30
    emit: Callable[[str], None] = field(default=lambda line: None)
    sleep: Callable[[float], None] = field(default=time.sleep)


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus text -> {metric name: value summed over its series}."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, _, rest = line.partition("{")
            rest = rest.rpartition("}")[2]
        else:
            name, _, rest = line.partition(" ")
        try:
            value = float(rest.split()[0])
        except (IndexError, ValueError):
            continue
        found[name] = found.get(name, 0.0) + value
    return found


def _ports(pod: dict[str, Any], settings: Settings) -> list[int]:
    if settings.ports:
        return list(settings.ports)
    containers = (pod.get("spec") or {}).get("containers") or []
    return sorted(
        {
            int(port["containerPort"])
            for container in containers
            for port in container.get("ports") or []
            if port.get("containerPort") and port.get("protocol", "TCP") == "TCP"
        }
    )


def read_load(kube: Kube, namespace: str, pod: dict[str, Any], settings: Settings) -> dict[str, float] | None:
    """What a pod's vLLM engines hold, summed over the ports that serve vLLM metrics; None when no
    port does (or the pod proxy cannot be read)."""
    name = pod["metadata"]["name"]
    load = {"running": 0.0, "waiting": 0.0, "kv": 0.0, "finished": 0.0, "engines": 0}
    for port in _ports(pod, settings):
        text = kube.pod_metrics(name, port, namespace)
        metrics = parse_metrics(text) if text else {}
        if RUNNING not in metrics:
            continue
        load["engines"] += 1
        load["running"] += metrics.get(RUNNING, 0.0)
        load["waiting"] += metrics.get(WAITING, 0.0)
        load["kv"] = max(load["kv"], *(metrics.get(key, 0.0) for key in KV_USAGE))
        load["finished"] += metrics.get(FINISHED, 0.0)
    return load if load["engines"] else None


def _idle(load: dict[str, float] | None) -> bool:
    return load is not None and load["running"] == 0 and load["waiting"] == 0 and load["kv"] == 0


def _pool_selector(pool: dict[str, Any]) -> dict[str, str]:
    selector = (pool.get("spec") or {}).get("selector") or {}
    match = selector.get("matchLabels") if isinstance(selector.get("matchLabels"), dict) else selector
    return {str(key): str(value) for key, value in (match or {}).items() if isinstance(value, (str, int, bool))}


def check_pools(kube: Kube, namespace: str, pods: list[dict[str, Any]], pool_label: str) -> None:
    """Refuse when an InferencePool selects one of ``pods`` without the pool label: setting the label
    to false would leave the pod in that pool, serving."""
    pools = []
    for api_version in POOL_VERSIONS:
        resource = kube.resource(api_version, "InferencePool")
        if resource is None:
            continue
        try:
            pools += kube.get_json(resource.qualified, namespace)
        except CommandError as error:
            raise CommandError(
                f"cannot read the InferencePools in {namespace}: {error.message}",
                hint="--drain-idle checks that each pool selects pods by the pool label before it drains.",
            ) from error
    for pool in pools:
        selector = _pool_selector(pool)
        others = {key: value for key, value in selector.items() if key != pool_label}
        for pod in pods:
            labels = health.labels(pod)
            selects = others and all(labels.get(key) == value for key, value in others.items())
            if selects and selector.get(pool_label) != "true":
                raise UsageError(
                    f"InferencePool {pool['metadata']['name']} selects pod {pod['metadata']['name']} "
                    f"without {pool_label}=true, so setting the label to false would not take it out",
                    hint="Pass the --pool-label that pool selects on, or drain with the stack's --drain command.",
                )


def drain(kube: Kube, namespace: str, pods: list[dict[str, Any]], settings: Settings) -> None:
    """Take ``pods`` out of the pool and wait until every engine on them is idle. On a timeout, or
    any error, they go back in the pool."""
    for pod in pods:
        if read_load(kube, namespace, pod, settings) is None:
            ports = ", ".join(str(port) for port in _ports(pod, settings)) or "none declared"
            raise CommandError(
                f"cannot read the vLLM metrics of pod {pod['metadata']['name']} (ports: {ports})",
                hint="Reading them needs `get` on pods/proxy in the namespace; pass --metrics-port when the "
                "engine's port is not declared on the container.",
            )
    names = [pod["metadata"]["name"] for pod in pods]
    flipped = [pod for pod in pods if health.labels(pod).get(settings.pool_label) == "true"]
    try:
        for pod in flipped:
            kube.patch_metadata("pods", pod["metadata"]["name"], namespace, labels={settings.pool_label: "false"})
        settings.emit(f"out of the pool: {', '.join(names)}; waiting until their engines are idle")
        _wait_idle(kube, namespace, pods, settings)
    except BaseException:
        put_back(kube, namespace, flipped, settings)
        raise


def _wait_idle(kube: Kube, namespace: str, pods: list[dict[str, Any]], settings: Settings) -> None:
    deadline = time.monotonic() + settings.timeout
    quiet, last = 0, ""
    while True:
        loads = [read_load(kube, namespace, pod, settings) for pod in pods]
        quiet = quiet + 1 if all(_idle(load) for load in loads) else 0
        if quiet >= settings.idle_reads:
            finished = sum(load["finished"] for load in loads if load)
            settings.sleep(settings.grace)
            again = [read_load(kube, namespace, pod, settings) for pod in pods]
            if all(_idle(load) for load in again) and sum(load["finished"] for load in again if load) == finished:
                return
            quiet = 0
        line = _describe(loads)
        if line != last:
            settings.emit(f"draining: {line}")
            last = line
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"{', '.join(pod['metadata']['name'] for pod in pods)} did not go idle in {settings.timeout:.0f}s "
                f"({line}); they are back in the pool and nothing was lowered",
                hint="Raise --drain-timeout for long requests, or try again at a quieter time.",
            )
        settings.sleep(settings.interval)


def _describe(loads: list[dict[str, float] | None]) -> str:
    known = [load for load in loads if load]
    running = sum(load["running"] for load in known)
    waiting = sum(load["waiting"] for load in known)
    unread = len(loads) - len(known)
    return f"{running:.0f} running, {waiting:.0f} waiting" + (f", {unread} unreadable" if unread else "")


def put_back(kube: Kube, namespace: str, pods: list[dict[str, Any]], settings: Settings) -> None:
    for pod in pods:
        try:
            kube.patch_metadata(
                "pods",
                pod["metadata"]["name"],
                namespace,
                labels={settings.pool_label: "true"},
                annotations={DELETION_COST: None},
            )
        except CommandError:
            settings.emit(f"could not put pod {pod['metadata']['name']} back in the pool; set {settings.pool_label}")


def _active(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pod for pod in pods if not (pod.get("metadata") or {}).get("deletionTimestamp")]


def lower(
    kube: Kube,
    namespace: str,
    document: dict[str, Any],
    resource: str,
    workload: str,
    before: int,
    after: int,
    settings: Settings,
    *,
    server_side: bool,
    on_lowered: Callable[[int], None],
) -> None:
    """Lower ``workload`` (of the rendered ``document``) from ``before`` to ``after``, a batch at a
    time, draining each batch first. ``on_lowered`` gets each count reached, so the instance's record
    keeps up with the cluster even when a later batch fails."""
    if not settings.pool_label:
        raise UsageError(
            "--drain-idle takes pods out of the router's pool by a label, and the instance has none",
            hint="Pass --pool-label with the label the InferencePool selects on (true while a pod serves).",
        )
    name, _, role = workload.partition("/")
    current = before
    while current > after:
        live = kube.require(resource, name, namespace)
        count = min(max(settings.batch, 1), current - after)
        if role:
            pods = _role_victims(kube, namespace, live, role, count)
        else:
            pods = _pod_victims(kube, namespace, live, count, settings)
        check_pools(kube, namespace, pods, settings.pool_label)
        ready = [pod for pod in pods if health.pod_ready(pod)]
        if ready:
            drain(kube, namespace, ready, settings)
        lowered = _lowered(document, live, workload, current - count)
        try:
            if not role:
                for pod in ready:
                    kube.patch_metadata(
                        "pods", pod["metadata"]["name"], namespace, annotations={DELETION_COST: LOWEST_COST}
                    )
            present = _names(kube, namespace, live)
            settings.emit(f"lowering {workload}: {current} -> {current - count}")
            kube.apply([lowered], namespace, server_side=server_side)
        except BaseException:
            put_back(kube, namespace, ready, settings)
            raise
        current -= count
        on_lowered(current)
        _wait_removed(kube, namespace, live, present, pods, settings)


def _lowered(document: dict[str, Any], live: dict[str, Any], workload: str, count: int) -> dict[str, Any]:
    """The rendered object with ``workload`` at ``count``. The other roles of a set keep the counts they
    run: this call may lower them later, each after its own drain."""
    lowered = copy.deepcopy(document)
    if lowered["kind"] == "DisaggregatedSet":
        running = {
            str(role.get("name")): (role.get("spec") or {}).get("replicas")
            for role in (live.get("spec") or {}).get("roles") or []
        }
        for role in (lowered.get("spec") or {}).get("roles") or []:
            if running.get(str(role.get("name"))) is not None:
                role.setdefault("spec", {})["replicas"] = running[str(role.get("name"))]
    _, holder = overrides.find([lowered], workload)
    holder["replicas"] = count
    return lowered


def _names(kube: Kube, namespace: str, live: dict[str, Any]) -> set[str]:
    pods = kube.get_json("pods", namespace, selector=health.pod_selector(live))
    return {pod["metadata"]["name"] for pod in _active(pods)}


def _pod_victims(
    kube: Kube, namespace: str, live: dict[str, Any], count: int, settings: Settings
) -> list[dict[str, Any]]:
    """The ``count`` Ready pods of a Deployment (or StatefulSet) with the fewest requests."""
    if live["kind"] == "StatefulSet":
        raise UsageError(
            f"StatefulSet/{live['metadata']['name']} removes its highest ordinals, not the pods llmd drains",
            hint="Lower it with the stack's --drain command, or --no-drain.",
        )
    if health.rolling(live):
        raise ConflictError(
            f"{live['kind']}/{live['metadata']['name']} is rolling out; drain it once the rollout is done",
            hint="A scale-down during a rollout takes pods from both ReplicaSets.",
        )
    pods = _active(kube.get_json("pods", namespace, selector=health.pod_selector(live)))
    waiting = [pod["metadata"]["name"] for pod in pods if not health.pod_ready(pod)]
    if waiting:
        raise ConflictError(
            f"{live['kind']}/{live['metadata']['name']} has pods that are not Ready: {', '.join(waiting)}",
            hint="The ReplicaSet removes a pod that is not Ready before it looks at the deletion cost, so the "
            "drained pod could stay. Drain once every pod is Ready.",
        )
    serving = [pod for pod in pods if health.labels(pod).get(settings.pool_label) != "false"]
    loads = {pod["metadata"]["name"]: read_load(kube, namespace, pod, settings) for pod in serving}

    def busy(pod: dict[str, Any]) -> tuple[float, str]:
        load = loads[pod["metadata"]["name"]]
        held = (load["running"] + load["waiting"]) if load else float("inf")
        return (held, pod["metadata"].get("creationTimestamp") or "")

    # Pods already out of the pool (a drain that stopped part way) go first.
    out = [pod for pod in pods if health.labels(pod).get(settings.pool_label) == "false"]
    return (out + sorted(serving, key=busy))[:count]


def _role_victims(kube: Kube, namespace: str, live: dict[str, Any], role: str, count: int) -> list[dict[str, Any]]:
    """The pods of the role's ``count`` highest groups, which lws removes when the role is lowered."""
    selector = f"{health.DS_NAME_LABEL}={live['metadata']['name']}"
    pods = _active(kube.get_json("pods", namespace, selector=selector))
    mine = [pod for pod in pods if health.labels(pod).get(health.DS_ROLE_LABEL) == role]
    revisions = {health.labels(pod).get(health.DS_REVISION_LABEL) for pod in pods}
    if len(revisions) > 1:
        raise ConflictError(
            f"DisaggregatedSet/{live['metadata']['name']} is rolling ({len(revisions)} revisions run)",
            hint="Drain a role once its roll is done; `llmd rollouts` drains during a roll.",
        )
    groups = sorted({int(health.labels(pod).get(health.GROUP_INDEX_LABEL, -1)) for pod in mine}, reverse=True)
    chosen = set(groups[:count])
    return [pod for pod in mine if int(health.labels(pod).get(health.GROUP_INDEX_LABEL, -1)) in chosen]


def _wait_removed(
    kube: Kube,
    namespace: str,
    live: dict[str, Any],
    present: set[str],
    drained: list[dict[str, Any]],
    settings: Settings,
) -> None:
    """Wait until the controller removes the drained pods; refuse to go on when it removed others of
    the pods ``present`` before the count was lowered."""
    names = {pod["metadata"]["name"] for pod in drained}
    deadline = time.monotonic() + REMOVAL_TIMEOUT
    while True:
        now = _names(kube, namespace, live)
        others = sorted((present - now) - names)
        if others:
            raise CommandError(
                f"the controller removed {', '.join(others)}, not only the drained pods",
                hint=f"Check {', '.join(sorted(names & now)) or 'the drained pods'}: still out of the pool, "
                f"set {settings.pool_label}=true on any that should serve again.",
            )
        if not names & now:
            return
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"{', '.join(sorted(names & now))} still run {REMOVAL_TIMEOUT}s after the count was lowered",
                hint="They are drained and out of the pool; follow them with `llmd status`.",
            )
        settings.sleep(settings.interval)
