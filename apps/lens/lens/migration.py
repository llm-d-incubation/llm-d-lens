"""Move an instance to another namespace, or another cluster, one replica at a time, with no outage.

The instance in A keeps serving while a copy in B is stood up at one replica (or one group) of the
workload being moved. Each step then raises B by one, waits until B's new pods are Ready, and only
then lowers A by one. So capacity never drops, and each step needs only one more replica's worth of
accelerators. The counts are ``llmd scale`` overrides on both instances, so a later standup or rollout
of either keeps them. Traffic is moved by hand (a gateway's route weights, or the like): llmd does not
change it, and ``status`` shows both sides so whoever moves it can see when B is ready for more.

``finish`` tears A down (claims kept) once it is at 0, and clears B's overrides that equal B's files.

Both sides are instances llmd stood up or imported; A may be either. Each side has its own kube
context and its own stack path (its cluster's overlay), so the same steps move a stack between
clusters. B is rendered with A's guide variables (``--var``, ``--set``) unless it already has its own.
The migration's state is in both records (``migration``), so ``status`` works from any machine that
can reach both clusters.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import health, inventory, overrides, scaling
from .errors import ConflictError, NotReadyError, UsageError
from .kube import Kube
from .lifecycle import standup, teardown
from .plan import Options, Plan, discovered_scope, make_plan
from .preflight import preflight
from .process import Runner


@dataclass
class Side:
    """One end of a migration: a cluster connection, a namespace and an instance name."""

    kube: Kube
    namespace: str
    instance: str
    context: str = ""

    def describe(self) -> dict[str, str]:
        return {"namespace": self.namespace, "instance": self.instance, "context": self.context}


def _record(side: Side) -> dict[str, Any] | None:
    return inventory.read(side.kube, side.namespace, side.instance)


def _require(side: Side) -> dict[str, Any]:
    return inventory.require(side.kube, side.namespace, side.instance)


def counts(side: Side, plan: Plan, workload: str) -> tuple[int | None, int, int]:
    """(replicas the cluster asks for, Ready replicas (pods / group size), group size) for a workload."""
    document, _ = overrides.find(list(plan.documents), workload)
    resource = side.kube.resource_name(document)
    live = side.kube.get(resource, document["metadata"]["name"], side.namespace)
    if live is None:
        return None, 0, 1
    _, holder = overrides.find([live], workload)
    wanted = overrides.replicas(holder)
    selector = health.pod_selector(live)
    pods = side.kube.get_json("pods", side.namespace, selector=selector) if selector else []
    role = workload.partition("/")[2]
    size = 1
    if role:
        size = int((holder.get("leaderWorkerTemplate") or {}).get("size", 1) or 1)
        pods = [pod for pod in pods if health.labels(pod).get(health.DS_ROLE_LABEL) == role]
    elif live["kind"] == "LeaderWorkerSet":
        size = int(((live.get("spec") or {}).get("leaderWorkerTemplate") or {}).get("size", 1) or 1)
    ready = sum(1 for pod in pods if health.pod_ready(pod))
    return wanted, ready // size, size


def plan_migration(source: Side, source_plan: Plan, target: Side, target_plan: Plan, workload: str) -> dict:
    """Read-only: what ``start`` would do, and why it cannot."""
    findings: list[dict[str, str]] = []
    record = _record(source)
    if record is None:
        findings.append({"severity": "error", "message": f"no instance {source.instance} in {source.namespace}"})
    elif (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        findings.append({"severity": "error", "message": f"{source.instance} has a rollout in progress"})
    elif record.get("migration"):
        findings.append({"severity": "error", "message": f"{source.instance} is already being migrated"})
    if (source.context, source.namespace) == (target.context, target.namespace):
        findings.append({"severity": "error", "message": "the source and the target are the same namespace"})
    for side, plan in ((source, source_plan), (target, target_plan)):
        try:
            overrides.find(list(plan.documents), workload)
        except UsageError as error:
            findings.append({"severity": "error", "message": f"{side.namespace}: {error.message}"})
    have = None
    if not [item for item in findings if item["severity"] == "error"]:
        have, ready, _ = counts(source, source_plan, workload)
        if have is None:
            findings.append({"severity": "error", "message": f"{workload} is not running in {source.namespace}"})
        elif ready < have:
            findings.append(
                {
                    "severity": "error",
                    "message": f"{workload} in {source.namespace}: {ready} of {have} Ready; start from a "
                    "settled instance",
                }
            )
    existing = _record(target)
    if existing is not None and not existing.get("migration"):
        findings.append(
            {
                "severity": "error",
                "message": f"{target.namespace} already has an instance {target.instance}",
            }
        )
    findings += [
        {"severity": finding.severity, "message": f"{target.namespace}: {finding.message}"}
        for finding in target_plan.findings
        if finding.severity == "error"
    ]
    return {
        "from": {**source.describe(), "stack": source_plan.stack.id},
        "to": {**target.describe(), "stack": target_plan.stack.id},
        "workload": workload,
        "replicas": have,
        "steps": have or 0,
        "findings": findings,
        "blocking": sum(1 for item in findings if item["severity"] == "error"),
        "digest": target_plan.digest,
    }


def _marker(source: Side, target: Side, workload: str, total: int, *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "workload": workload,
        "replicas": total,
        "from": source.describe(),
        "to": target.describe(),
        "startedAt": inventory.now(),
        "steps": [],
    }


def start(
    source: Side,
    target: Side,
    target_plan: Plan,
    workload: str,
    *,
    total: int,
    expected_digest: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Stand B up with ``workload`` at 1, and mark the migration in both records."""
    if target_plan.options.overrides.get(workload) != 1:
        raise UsageError("the target plan must scale the moved workload to 1")
    result = standup(target.kube, target_plan, expected_digest=expected_digest, actor=actor)
    for side, role in ((source, "source"), (target, "target")):
        record = _require(side)
        record["migration"] = _marker(source, target, workload, total, role=role)
        inventory.write(side.kube, record)
    return {**result, "workload": workload, "replicas": total}


def status(source: Side, source_plan: Plan, target: Side, target_plan: Plan) -> dict[str, Any]:
    record = _require(source)
    marker = record.get("migration") or {}
    if not marker:
        raise UsageError(f"{source.instance} in {source.namespace} is not being migrated")
    workload = marker["workload"]
    a_want, a_ready, size = counts(source, source_plan, workload)
    b_want, b_ready, _ = counts(target, target_plan, workload)
    return {
        "workload": workload,
        "replicas": marker["replicas"],
        "groupSize": size,
        "from": {**source.describe(), "wanted": a_want, "ready": a_ready},
        "to": {**target.describe(), "wanted": b_want, "ready": b_ready},
        "steps": marker.get("steps") or [],
        "done": (a_want or 0) == 0 and (b_ready or 0) >= marker["replicas"],
    }


def step(
    source: Side,
    source_plan: Plan,
    target: Side,
    target_plan: Plan,
    *,
    runner: Runner,
    drain: str = "",
    no_drain: bool = False,
    timeout: float = 3600,
    interval: float = 15,
    emit: Callable[[str], None] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Raise B by one, wait until it is Ready, then lower A by one. ``source_plan``/``target_plan``
    are each side's plan with its current overrides."""
    emit = emit or (lambda line: None)
    current = status(source, source_plan, target, target_plan)
    workload = current["workload"]
    a_want, a_ready = current["from"]["wanted"] or 0, current["from"]["ready"]
    b_want, b_ready = current["to"]["wanted"] or 0, current["to"]["ready"]
    if b_ready < b_want:
        raise NotReadyError(
            f"{target.namespace} has {b_ready} of {b_want} {workload} Ready; wait for it before the next step",
            hint="Run `llmd migrate status`; a step starts from a settled target.",
        )
    if a_want == 0:
        raise UsageError("the source is at 0 already; run `llmd migrate finish`")
    if b_want < current["replicas"]:
        emit(f"raising {workload} in {target.namespace}: {b_want} -> {b_want + 1}")
        _scale(target, target_plan, workload, b_want + 1, runner=runner, actor=actor)
        deadline = time.monotonic() + timeout
        while True:
            target.kube.forget()
            _, b_ready, _ = counts(target, target_plan, workload)
            if b_ready >= b_want + 1:
                break
            if time.monotonic() >= deadline:
                raise NotReadyError(
                    f"{target.namespace}: {workload} not Ready at {b_want + 1} after {timeout:.0f}s; the "
                    f"source is unchanged at {a_want}",
                    hint="Follow it with `llmd status`; run `llmd migrate step` again once it is Ready.",
                )
            emit(f"waiting: {b_ready}/{b_want + 1} {workload} Ready in {target.namespace}")
            time.sleep(interval)
        b_want += 1
    else:
        emit(f"{target.namespace} already runs {b_want} {workload}")
    if a_ready < a_want:
        emit(f"note: {source.namespace} has {a_ready} of {a_want} {workload} Ready")
    emit(f"lowering {workload} in {source.namespace}: {a_want} -> {a_want - 1}")
    _scale(source, source_plan, workload, a_want - 1, runner=runner, drain=drain, no_drain=no_drain, actor=actor)
    entry = {"at": inventory.now(), "to": b_want, "from": a_want - 1}
    for side in (source, target):
        record = _require(side)
        record["migration"]["steps"] = [*(record["migration"].get("steps") or []), entry]
        inventory.write(side.kube, record)
    return {"workload": workload, "from": a_want - 1, "to": b_want}


def _scale(
    side: Side,
    plan: Plan,
    workload: str,
    count: int,
    *,
    runner: Runner,
    drain: str = "",
    no_drain: bool = False,
    actor: str = "",
) -> None:
    record = _require(side)
    kept = {**(record.get("overrides") or {}), workload: count}
    options = Options(
        namespace=side.namespace,
        instance=side.instance,
        allow_privileged=plan.options.allow_privileged,
        skip_admin_parts=plan.options.skip_admin_parts,
        variables=dict(plan.options.variables),
        sets=list(plan.options.sets),
        overrides=kept,
    )
    scaled = make_plan(
        plan.stack,
        options,
        runner=runner,
        scope=discovered_scope(side.kube),
        label=inventory.management(record) != inventory.IMPORTED,
    )
    imported = inventory.management(record) == inventory.IMPORTED
    preflight(side.kube, scaled, existing="import" if imported else "rollout")
    scaling.scale(
        side.kube,
        scaled,
        {workload: count},
        record=record,
        runner=runner,
        drain=drain,
        no_drain=no_drain,
        actor=actor,
    )
    side.kube.forget()


def finish(
    source: Side,
    source_plan: Plan,
    target: Side,
    target_plan: Plan,
    *,
    files: int,
) -> dict[str, Any]:
    """Tear A down once it serves nothing, and end the migration on both sides. B's override of the
    moved workload is cleared when it equals what B's files say (``files``)."""
    current = status(source, source_plan, target, target_plan)
    if (current["from"]["wanted"] or 0) > 0:
        raise ConflictError(
            f"{source.namespace} still runs {current['from']['wanted']} {current['workload']}",
            hint="Run `llmd migrate step` until it is at 0.",
        )
    if current["to"]["ready"] < current["replicas"]:
        raise NotReadyError(
            f"{target.namespace} has {current['to']['ready']} of {current['replicas']} Ready",
            hint="Wait for it, then finish.",
        )
    removed = teardown(source.kube, source.namespace, source.instance)
    record = _require(target)
    record.pop("migration", None)
    kept = dict(record.get("overrides") or {})
    if kept.get(current["workload"]) == files:
        kept.pop(current["workload"])
    if kept:
        record["overrides"] = kept
    else:
        record.pop("overrides", None)
    inventory.write(target.kube, record)
    return {"tornDown": removed, "target": target.describe(), "overrides": kept}
