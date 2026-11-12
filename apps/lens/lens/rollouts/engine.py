"""Start, follow, pause, resume, abort and finish an in-place rollout.

The rollout's state lives in the instance's record (``rollout``), so ``status``, ``pause``
and ``abort`` work from any machine; only the drainer processes are local to the machine
that started them (``pause``/``resume`` there, or start new drainers with ``resume``).

``start`` applies only the objects that change. An imported stack's objects are applied
the way its team applies them (client-side ``kubectl apply``), so their own
``kubectl apply`` keeps working; objects llmd stood up use server-side apply as at standup.

``abort`` re-applies the DisaggregatedSet as it was before the rollout: the controller has
no pause and does not read ``partition``, so the old template is the only way back. It is
itself a roll, and the drainers run for it the same way.

A Deployment or StatefulSet whose pod template changes (a router's, say) is followed too: the
rollout stays Rolling until its new pods are Ready and its old pods are gone, which for an
endpoint picker that finishes its streams before it exits can take its whole grace period.
Until then ``abort`` re-applies it as it was.

A rollout never deletes. ``prune`` removes what the instance records and its stack no longer
renders, once nothing uses it: a workload at 0 with no pod left, a ConfigMap, Secret or
ServiceAccount no pod refers to. Claims are kept unless asked, and so is an object labelled as
another llm-d-lens instance's (an imported stack can record one that tool made).
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from .. import health, inventory, yamlio
from ..errors import ConflictError, NotReadyError, PolicyError, UsageError
from ..importing import comparable
from ..kube import Kube
from ..model import INSTANCE_LABEL, KEPT_KINDS, MANAGED_BY_LABEL, MANAGED_BY_VALUE, object_ref
from ..plan import Plan
from ..preflight import resources
from .drainers import Drainer, Drainers, command
from .plan import RolloutPlan
from .progress import Progress, read_progress

DS_RESOURCE = "disaggregatedsets.disaggregatedset.x-k8s.io"


def _record(kube: Kube, namespace: str, instance: str) -> dict[str, Any]:
    return inventory.require(kube, namespace, instance)


def _rollout(record: dict[str, Any]) -> dict[str, Any]:
    rollout = record.get("rollout") or {}
    if rollout.get("phase") not in ("Rolling", "Paused", "Aborting"):
        raise UsageError(
            f"instance {record['instance']} has no rollout in progress",
            hint="Start one with `llmd rollouts start`.",
        )
    return rollout


def drainer_plan(rollout: RolloutPlan, template: str, directory: Path) -> list[Drainer]:
    roll = rollout.change.roll
    if roll is None or not template:
        return []
    drainers = []
    for role in roll.roles:
        values = {
            "workload": f"{roll.name}/{role.name}",
            "role": role.name,
            "source": str(rollout.plan.stack.source.root),
            "namespace": rollout.plan.namespace,
            "set": roll.name,
            "old_revision": rollout.before.target if rollout.before else "",
            "max_unavailable": str(role.max_unavailable),
        }
        log = directory / f"drain-{role.name}.log"
        drainers.append(Drainer(role.name, command(template, values), log, str(rollout.plan.stack.source.root)))
    return drainers


def start(
    kube: Kube,
    rollout: RolloutPlan,
    *,
    drain: str,
    state: Path,
    expected_digest: str = "",
    env: dict[str, str] | None = None,
    actor: str = "",
    pool_label: str = "",
) -> dict[str, Any]:
    """Start the rollout. ``pool_label`` is recorded with it, so ``status``, ``finish`` and ``--watch``
    judge serving groups the same way without the flag."""
    plan, record = rollout.plan, rollout.record
    if rollout.blocking:
        raise PolicyError(
            f"the rollout has {len(rollout.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in rollout.blocking],
            hint="Run `llmd rollouts plan` to see them.",
        )
    if expected_digest and not yamlio.digest_matches(expected_digest, rollout.digest):
        raise ConflictError("the rollout changed since it was reviewed", hint="Review `llmd rollouts plan` again.")
    if (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(f"instance {plan.instance} already has a rollout in progress")
    roll = rollout.change.roll
    if roll and not drain:
        raise UsageError(
            f"{roll.name} rolls: every group is replaced; say how its groups drain",
            hint="Pass --no-drain when each pod drains as it stops (the llm-d guides' vLLM overlays do), or "
            "--drain 'COMMAND {role} ...' for the stack's drain tool.",
        )
    changed = rollout.change.changed_documents(plan.documents)
    if not changed and rollout.change.left_alone and record["stack"].get("source") != plan.stack.source.describe():
        # The ref only drops objects: nothing to apply, but the instance now runs this ref, so
        # `prune` can delete what it no longer renders.
        record = {**record, "stack": plan.stack_record(), "planDigest": plan.digest, "updatedBy": actor}
        inventory.write(kube, record)
        return {
            "instance": plan.instance,
            "namespace": plan.namespace,
            "applied": [],
            "rolls": "",
            "drainers": [],
            "phase": (record.get("rollout") or {}).get("phase") or "None",
            "leftAlone": rollout.change.left_alone,
        }
    if not changed:
        raise UsageError("nothing to roll out: the instance already runs what this ref renders")
    secrets = [object_ref(doc) for doc in changed if doc["kind"] == "Secret"]
    if secrets:
        # The record keeps what a rollout changes, as it was, for abort; never a Secret's data.
        raise PolicyError(
            f"the rollout would change {', '.join(secrets)}",
            hint="Update Secrets outside a rollout; llmd never records their data.",
        )
    names = dict(zip((object_ref(doc) for doc in plan.documents), resources(kube, plan), strict=True))
    previous = []
    for document in changed:
        live = kube.get(names[object_ref(document)], document["metadata"]["name"], plan.namespace)
        if live is not None:
            previous.append({"resource": names[object_ref(document)], "document": comparable(live)})
    directory = state / "rollouts" / plan.namespace / plan.instance
    planned_drainers = drainer_plan(rollout, drain, directory) if drain != "-" else []
    restarts = [
        {"kind": ref.partition("/")[0], "name": ref.partition("/")[2], "resource": names[ref]}
        for ref in rollout.change.restarts
    ]
    record = {
        **record,
        "rollout": {
            "phase": "Rolling",
            "startedAt": inventory.now(),
            "startedBy": actor,
            "from": (record.get("stack") or {}).get("source"),
            "to": plan.stack.source.describe(),
            "digest": rollout.digest,
            "set": roll.name if roll else "",
            "fromRevision": rollout.before.target if (roll and rollout.before) else "",
            "objects": [object_ref(doc) for doc in changed],
            "previous": previous,
            "drain": drain if drain != "-" else "",
            **({"poolLabel": pool_label} if pool_label else {}),
            **({"workloads": restarts} if restarts else {}),
            "steps": [],
        },
    }
    inventory.write(kube, record)
    started = Drainers(directory).start(planned_drainers, env=env) if planned_drainers else []
    imported = inventory.management(record) == inventory.IMPORTED
    kube.apply(changed, plan.namespace, server_side=not imported)
    record = _record(kube, plan.namespace, plan.instance)
    if record["stack"]["id"] != plan.stack.id:  # --switch-stack
        record["previousStack"] = record["stack"]
    record["stack"] = plan.stack_record()
    record["planDigest"] = plan.digest
    new = [
        inventory.object_entry(doc, names[object_ref(doc)])
        for doc in changed
        if rollout.change.objects[object_ref(doc)] == "new"
    ]
    for entry in new:
        entry["uid"] = (
            (kube.get(entry["resource"], entry["name"], plan.namespace) or {}).get("metadata", {}).get("uid", "")
        )
    record["objects"] = [*record["objects"], *new]
    if not roll and not restarts:
        record["rollout"] = {**record["rollout"], "phase": "Finished", "finishedAt": inventory.now()}
    inventory.write(kube, record)
    rolling = ([roll.name] if roll else []) + [f"{item['kind']}/{item['name']}" for item in restarts]
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "applied": [object_ref(doc) for doc in changed],
        "leftAlone": rollout.change.left_alone,
        "rolls": ", ".join(rolling),
        "drainers": [item.describe() for item in started],
        "phase": record["rollout"]["phase"],
    }


def progress_of(kube: Kube, record: dict[str, Any], *, pool_label: str) -> Progress | None:
    name = (record.get("rollout") or {}).get("set")
    if not name:
        return None
    live = kube.require(DS_RESOURCE, name, record["namespace"])
    progress = read_progress(kube, record["namespace"], live, pool_label=pool_label)
    rollout = record.get("rollout") or {}
    if rollout.get("phase") == "Aborting" and rollout.get("fromRevision"):
        # An abort rolls back to the revision the rollout started from, though its sets are older.
        progress.target = rollout["fromRevision"]
    return progress


def workloads_of(kube: Kube, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Where each Deployment or StatefulSet the rollout replaces pods of stands: done once it reports
    the new template Ready and no pod of it is terminating."""
    found = []
    for item in (record.get("rollout") or {}).get("workloads") or []:
        live = kube.get(item["resource"], item["name"], record["namespace"])
        selector = health.pod_selector(live) if live else ""
        pods = kube.get_json("pods", record["namespace"], selector=selector) if selector else []
        state, detail = health.evaluate(live, pods)
        terminating = sum(1 for pod in pods if (pod.get("metadata") or {}).get("deletionTimestamp"))
        found.append(
            {
                "object": f"{item['kind']}/{item['name']}",
                "state": state,
                "detail": detail,
                "terminating": terminating,
                "done": state == "ready" and not terminating,
            }
        )
    return found


def status(kube: Kube, namespace: str, instance: str, *, state: Path, pool_label: str) -> dict[str, Any]:
    record = _record(kube, namespace, instance)
    rollout = record.get("rollout") or {}
    progress = progress_of(kube, record, pool_label=pool_label) if rollout.get("set") else None
    workloads = workloads_of(kube, record) if rollout.get("phase") in ("Rolling", "Paused", "Aborting") else []
    drainers = Drainers(state / "rollouts" / namespace / instance)
    return {
        "instance": instance,
        "namespace": namespace,
        "phase": rollout.get("phase") or "None",
        "set": rollout.get("set", ""),
        "from": (rollout.get("from") or {}).get("revision", ""),
        "to": (rollout.get("to") or {}).get("revision", ""),
        "fromRevision": rollout.get("fromRevision", ""),
        "progress": progress.describe() if progress else None,
        "workloads": workloads,
        "done": _done(rollout, progress, workloads),
        "drainers": {
            "running": [item.describe() for item in drainers.running()],
            "exited": [item.describe() for item in drainers.exited()],
        },
        "startedAt": rollout.get("startedAt", ""),
    }


def _done(rollout: dict[str, Any], progress: Progress | None, workloads: list[dict[str, Any]]) -> bool:
    """Rolled forward: every group serves a revision other than the one the rollout started from.
    Aborted: every group serves that revision again. Either way, every Deployment or StatefulSet it
    replaces pods of is done."""
    if not rollout.get("set") and not rollout.get("workloads"):
        return False
    if rollout.get("set"):
        if progress is None or progress.rolling or not progress.done():
            return False
        back = progress.target == rollout.get("fromRevision")
        if not (back if rollout.get("phase") == "Aborting" else not back):
            return False
    return all(item["done"] for item in workloads)


def _set_phase(kube: Kube, record: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    record = {**record, "rollout": {**record["rollout"], "phase": phase, **extra}}
    inventory.write(kube, record)
    return record


def pause(kube: Kube, namespace: str, instance: str, *, state: Path) -> dict[str, Any]:
    """Stop the drainers: the stack's guards then hold the controller where it stands."""
    record = _record(kube, namespace, instance)
    _rollout(record)
    stopped = Drainers(state / "rollouts" / namespace / instance).stop()
    _set_phase(kube, record, "Paused", pausedAt=inventory.now())
    return {"instance": instance, "phase": "Paused", "stopped": [item.role for item in stopped]}


def resume(kube: Kube, namespace: str, instance: str, *, state: Path, env=None) -> dict[str, Any]:
    """Start the drainers again (on this machine) and mark the rollout Rolling."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    directory = state / "rollouts" / namespace / instance
    drainers = Drainers(directory)
    if drainers.running():
        raise ConflictError("the drainers already run", hint="See `llmd rollouts status`.")
    restart = [Drainer(item.role, item.argv, item.log, item.cwd) for item in drainers.load()]
    if not restart and rollout.get("drain"):
        raise UsageError(
            "this machine did not start the rollout's drainers",
            hint="Resume on the machine that ran `llmd rollouts start`, or run the stack's drain command there.",
        )
    started = drainers.start(restart, env=env) if restart else []
    _set_phase(kube, record, "Aborting" if rollout["phase"] == "Aborting" else "Rolling")
    return {"instance": instance, "phase": "Rolling", "drainers": [item.describe() for item in started]}


def abort(kube: Kube, namespace: str, instance: str, *, server_side: bool) -> dict[str, Any]:
    """Re-apply the objects as they were before the rollout. The controller rolls back the same
    way it rolled forward, so the drainers must keep running."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    previous = rollout.get("previous") or []
    if not previous:
        raise UsageError("the rollout recorded no previous objects to restore")
    documents = []
    for item in previous:
        document = copy.deepcopy(item["document"])
        document["metadata"].pop("namespace", None)
        documents.append(document)
    kube.apply(documents, namespace, server_side=server_side, force_conflicts=server_side)
    _set_phase(kube, record, "Aborting", abortedAt=inventory.now())
    return {"instance": instance, "phase": "Aborting", "restored": [object_ref(doc) for doc in documents]}


def finish(kube: Kube, namespace: str, instance: str, *, state: Path, pool_label: str) -> dict[str, Any]:
    """End the rollout once the old revision is gone and every group serves the new one."""
    record = _record(kube, namespace, instance)
    rollout = _rollout(record)
    progress = progress_of(kube, record, pool_label=pool_label)
    workloads = workloads_of(kube, record)
    if (progress or workloads) and not _done(rollout, progress, workloads):
        unsettled = progress is not None and not _done({**rollout, "workloads": []}, progress, [])
        waiting = [f"{progress.name} has not finished rolling ({', '.join(progress.revisions)})"] if unsettled else []
        waiting += [
            f"{item['object']} has not finished rolling ({item['detail']})" for item in workloads if not item["done"]
        ]
        raise NotReadyError(
            "; ".join(waiting) or "the rollout has not finished",
            hint="Follow it with `llmd rollouts status --watch`.",
        )
    Drainers(state / "rollouts" / namespace / instance).stop()
    phase = "Aborted" if rollout["phase"] == "Aborting" else "Finished"
    _set_phase(kube, record, phase, finishedAt=inventory.now())
    return {"instance": instance, "phase": phase, "revision": progress.target if progress else ""}


def watch(
    kube: Kube,
    namespace: str,
    instance: str,
    *,
    state: Path,
    pool_label: str,
    timeout: float,
    interval: float = 30,
    emit=None,
) -> dict[str, Any]:
    """Follow the roll until it is done, then finish it. A drainer that exits while old groups remain
    stops the watch: the roll would wait forever on a group nobody drains."""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        current = status(kube, namespace, instance, state=state, pool_label=pool_label)
        line = describe_progress(current)
        if emit and line != last:
            emit(line)
            last = line
        if current["done"]:
            return finish(kube, namespace, instance, state=state, pool_label=pool_label)
        stalled = _stalled(current)
        if stalled and current["phase"] != "Paused":
            roles = ", ".join(item["role"] for item in stalled)
            logs = ", ".join(item["log"] for item in stalled)
            raise NotReadyError(
                f"the {roles} drainer exited while old {roles} groups remain; the roll waits for it",
                hint=f"Read its log ({logs}), then `llmd rollouts resume`, or `llmd rollouts abort`.",
            )
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"the roll did not finish in {timeout:.0f}s ({line})",
                hint="It keeps going with the drainers; follow it with `llmd rollouts status --watch`.",
            )
        time.sleep(interval)


def _stalled(current: dict[str, Any]) -> list[dict[str, Any]]:
    """Exited drainers whose role still has groups of a revision the roll moves away from."""
    progress = current.get("progress") or {}
    roles = progress.get("roles") or {}
    target = progress.get("target")
    return [
        drainer
        for drainer in current["drainers"]["exited"]
        if any(
            revision != target and value["groups"]
            for revision, value in (roles.get(drainer["role"]) or {}).get("byRevision", {}).items()
        )
    ]


def describe_progress(current: dict[str, Any]) -> str:
    progress = current.get("progress")
    rolling = [
        f"{item['object']} {item['detail']}"
        + (f", {item['terminating']} old pod(s) finishing" if item["terminating"] else "")
        for item in current.get("workloads") or []
        if not item["done"]
    ]
    if not progress:
        return (
            f"{current['phase']}: " + ("; ".join(rolling) or "done") if current.get("workloads") else current["phase"]
        )
    parts = []
    for role, item in progress["roles"].items():
        new = item["byRevision"].get(progress["target"], {"groups": 0, "serving": 0})
        old = sum(value["groups"] for revision, value in item["byRevision"].items() if revision != progress["target"])
        parts.append(f"{role} {new['serving']}/{item['replicas']} new serving, {old} old left")
    return f"{current['phase']}: " + "; ".join(parts + rolling)


#: Kinds that run pods: pruned only once they ask for none and none is left.
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "ReplicaSet", "DaemonSet", "LeaderWorkerSet", "DisaggregatedSet", "Job")


def prune(kube: Kube, plan: Plan, *, delete_volumes: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Delete the objects the instance records that ``plan`` (its own stack) no longer renders, once
    nothing uses them, and drop them from the record."""
    record = _record(kube, plan.namespace, plan.instance)
    if (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(
            f"instance {plan.instance} has a rollout in progress; prune it once the rollout is finished",
            hint=f"See `llmd rollouts status {plan.instance}`.",
        )
    imported = inventory.management(record) == inventory.IMPORTED
    rendered = {(document["kind"], document["metadata"]["name"]) for document in plan.documents}
    stale = [item for item in record["objects"] if (item["kind"], item["name"]) not in rendered]
    pods = kube.get_json("pods", plan.namespace) if stale else []
    deleted, kept, gone = [], [], []
    for item in stale:
        ref = f"{item['kind']}/{item['name']}"
        live = kube.get(item["resource"], item["name"], plan.namespace)
        if live is None:
            gone.append(ref)
            continue
        reason = _still_needed(
            item, live, pods, instance=plan.instance, imported=imported, delete_volumes=delete_volumes
        )
        if reason:
            kept.append({"object": ref, "reason": reason})
        else:
            deleted.append(item)
    if not dry_run and (deleted or gone):
        names = sorted({item["resource"] for item in deleted})
        answers = kube.access_reviews([("delete", *_split(name), plan.namespace) for name in names])
        denied = [name for name, allowed in zip(names, answers, strict=True) if allowed is False]
        if denied:
            raise PolicyError(
                f"you cannot delete {', '.join(denied)} in {plan.namespace}; nothing was removed",
                hint="Ask a namespace administrator for the missing permission.",
            )
        for item in deleted:
            kube.delete(item["resource"], item["name"], plan.namespace)
        removed = {f"{item['kind']}/{item['name']}" for item in deleted} | set(gone)
        workloads = {ref.partition("/")[2] for ref in removed if ref.partition("/")[0] in WORKLOAD_KINDS}
        updated = {
            **record,
            "objects": [item for item in record["objects"] if f"{item['kind']}/{item['name']}" not in removed],
        }
        for key in ("overrides", "podLabels", "podAnnotations"):
            remaining = {
                workload: value
                for workload, value in (record.get(key) or {}).items()
                if workload.partition("/")[0] not in workloads
            }
            if remaining:
                updated[key] = remaining
            else:
                updated.pop(key, None)
        inventory.write(kube, updated)
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "deleted": [f"{item['kind']}/{item['name']}" for item in deleted],
        "kept": kept,
        "gone": gone,
        "dryRun": dry_run,
    }


def _still_needed(
    item: dict[str, Any],
    live: dict[str, Any],
    pods: list[dict[str, Any]],
    *,
    instance: str,
    imported: bool,
    delete_volumes: bool,
) -> str:
    """Why a stale object stays ("" when it can go)."""
    if item.get("uid") and live["metadata"].get("uid") != item["uid"]:
        return "deleted and created again outside llmd"
    labels = (live.get("metadata") or {}).get("labels") or {}
    if labels.get(INSTANCE_LABEL) and labels[INSTANCE_LABEL] != instance:
        return f"labelled as llm-d-lens instance {labels[INSTANCE_LABEL]}'s"
    owned = labels.get(INSTANCE_LABEL) == instance and labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
    if not imported and not owned:
        return "no longer carries the instance's labels"
    if item["kind"] in KEPT_KINDS and not delete_volumes:
        return "a claim: pass --delete-volumes to delete it"
    if item["kind"] in WORKLOAD_KINDS:
        wanted = _replicas(live)
        selector = health.pod_selector(live)
        running = [pod for pod in pods if selector and _selected(pod, selector)]
        if wanted:
            return f"asks for {wanted} replica(s): lower it to 0 (`llmd scale`) while the stack still renders it"
        if running:
            return f"{len(running)} pod(s) still run; prune again once they are gone"
        return ""
    users = sorted(pod["metadata"]["name"] for pod in pods if _refers(pod, item["kind"], item["name"]))
    if users:
        return f"used by pod(s) {', '.join(users[:5])}" + (" and more" if len(users) > 5 else "")
    return ""


def _replicas(live: dict[str, Any]) -> int:
    spec = live.get("spec") or {}
    if live["kind"] == "DisaggregatedSet":
        return sum(int((role.get("spec") or {}).get("replicas") or 0) for role in spec.get("roles") or [])
    if live["kind"] in ("DaemonSet", "Job"):
        return int(((live.get("status") or {}).get("active") or 0) if live["kind"] == "Job" else 0)
    return int(spec.get("replicas", 1) if spec.get("replicas") is not None else 1)


def _selected(pod: dict[str, Any], selector: str) -> bool:
    labels = health.labels(pod)
    return all(labels.get(key) == value for key, _, value in (term.partition("=") for term in selector.split(",")))


def _refers(pod: dict[str, Any], kind: str, name: str) -> bool:
    """Whether a pod mounts, reads or runs as the ConfigMap, Secret or ServiceAccount ``name``."""
    spec = pod.get("spec") or {}
    if kind == "ServiceAccount":
        return (spec.get("serviceAccountName") or spec.get("serviceAccount")) == name
    key = {
        "ConfigMap": ("configMap", "name", "configMapRef", "configMapKeyRef"),
        "Secret": ("secret", "secretName", "secretRef", "secretKeyRef"),
    }.get(kind)
    if key is None:
        return False
    volume_key, volume_name, from_key, ref_key = key
    for volume in spec.get("volumes") or []:
        if (volume.get(volume_key) or {}).get(volume_name) == name:
            return True
        for source in (volume.get("projected") or {}).get("sources") or []:
            if (source.get(volume_key) or {}).get("name") == name:
                return True
    for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
        if any((item.get(from_key) or {}).get("name") == name for item in container.get("envFrom") or []):
            return True
        if any(
            ((item.get("valueFrom") or {}).get(ref_key) or {}).get("name") == name
            for item in container.get("env") or []
        ):
            return True
    return kind == "Secret" and any(item.get("name") == name for item in spec.get("imagePullSecrets") or [])


def _split(resource: str) -> tuple[str, str]:
    """``plural.group`` -> (group, plural)."""
    plural, _, group = resource.partition(".")
    return group, plural
