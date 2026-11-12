"""``llmd scale``: change a workload's replica count, keep it in the instance, and apply only it.

The workload is rendered from the instance's stack with the new count, the way a standup would
render it, and only that object is applied, with the instance's own apply mode (server-side for an
instance llmd stood up, client-side ``kubectl apply`` for an imported one). Nothing else in the
stack is applied, so a scale never carries along another change the files may hold.

Lowering a DisaggregatedSet role removes its highest groups. Its pods then stop the way any pod
does: the llm-d guides' vLLM overlays wait in ``preStop`` and finish requests in flight for
``--shutdown-timeout``, which ``no_drain`` relies on. A stack that guards its groups with an
admission policy admits the lowering only from its own drain tool: with ``drain``, llmd runs that
command, which drains the groups and lowers the count itself, and records the count once it
succeeds. Lowering a Deployment, StatefulSet or LeaderWorkerSet is applied directly unless a
``drain`` command is given, which then lowers it the same way. Raising is applied directly.
"""

from __future__ import annotations

import copy
import sys
from typing import Any

from . import inventory, overrides
from .errors import CommandError, ConflictError, PolicyError, UsageError
from .kube import Kube
from .model import object_ref
from .plan import Plan
from .preflight import resources
from .process import Runner
from .rollouts.drainers import command as drain_command

#: A drain waits for every rank of a group to be idle, which can take tens of minutes.
DRAIN_TIMEOUT = 3 * 3600


def current_replicas(kube: Kube, plan: Plan, workload: str) -> int | None:
    """What the cluster runs for ``workload`` now (None when the object is not there)."""
    document, _ = overrides.find(copy.deepcopy(plan.documents), workload)
    resource = dict(zip((object_ref(doc) for doc in plan.documents), resources(kube, plan), strict=True))
    live = kube.get(resource[object_ref(document)], document["metadata"]["name"], plan.namespace)
    if live is None:
        return None
    _, holder = overrides.find([live], workload)
    return overrides.replicas(holder)


def scale(
    kube: Kube,
    plan: Plan,
    changes: dict[str, int | None],
    *,
    record: dict[str, Any],
    runner: Runner,
    files: dict[str, int] | None = None,
    drain: str = "",
    no_drain: bool = False,
    dry_run: bool = False,
    actor: str = "",
) -> dict[str, Any]:
    """Apply ``changes`` (workload -> replicas, None to clear) to the instance ``plan`` renders.
    ``plan`` was made with the overrides the changes lead to; ``files`` are the counts the stack's
    files say, so a change back to them clears the override."""
    files = files or {}
    if (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(
            f"instance {plan.instance} has a rollout in progress; scale it once the rollout is finished",
            hint=f"See `llmd rollouts status {plan.instance}`.",
        )
    if plan.blocking:
        raise PolicyError(
            f"the plan has {len(plan.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in plan.blocking],
            hint="Run `llmd plan` to see them.",
        )
    imported = inventory.management(record) == inventory.IMPORTED
    names = dict(zip((object_ref(doc) for doc in plan.documents), resources(kube, plan), strict=True))
    steps = []
    for workload in sorted(changes):
        document, holder = overrides.find(plan.documents, workload)
        before = current_replicas(kube, plan, workload)
        after = overrides.replicas(holder)
        lowers = before is not None and after < before
        steps.append(
            {
                "workload": workload,
                "object": object_ref(document),
                "resource": names[object_ref(document)],
                "from": before,
                "to": after,
                "drain": lowers and bool(drain),
            }
        )
    for step in steps:
        lowers_role = "/" in step["workload"] and step["from"] is not None and step["to"] < step["from"]
        if lowers_role and not drain and not no_drain:
            raise UsageError(
                f"lowering {step['workload']} from {step['from']} to {step['to']} removes serving groups",
                hint="Pass --no-drain when each pod drains as it stops (the llm-d guides' vLLM overlays do), "
                "or --drain 'COMMAND' with the stack's drain tool, which lowers the count once the groups "
                "are out of the pool; fields: {role} {set} {namespace} {source} {count}.",
            )
    documents = {object_ref(doc): doc for doc in plan.documents}
    changed = [documents[step["object"]] for step in steps if step["from"] != step["to"] and not step["drain"]]
    unique = list({object_ref(doc): doc for doc in changed}.values())
    if dry_run:
        if unique:
            kube.apply(unique, plan.namespace, dry_run=True, server_side=not imported)
        return {"instance": plan.instance, "namespace": plan.namespace, "steps": steps, "dryRun": True}
    for step in steps:
        if step["drain"] and step["from"] != step["to"]:
            kube.progress(f"draining {step['workload']}: {step['from']} -> {step['to']}")
            _run_drain(plan, step, drain, runner)
    if unique:
        kube.progress(f"applying {', '.join(object_ref(doc) for doc in unique)}")
        kube.apply(unique, plan.namespace, server_side=not imported)
    kept = dict(record.get("overrides") or {})
    for workload, count in changes.items():
        # A count the stack's files already say needs no override.
        if count is None or count == files.get(workload):
            kept.pop(workload, None)
        else:
            kept[workload] = count
    updated = {**record, "updatedBy": actor}
    if kept:
        updated["overrides"] = kept
    else:
        updated.pop("overrides", None)
    inventory.write(kube, updated)
    return {"instance": plan.instance, "namespace": plan.namespace, "steps": steps, "overrides": kept}


def _run_drain(plan: Plan, step: dict[str, Any], template: str, runner: Runner) -> None:
    """The stack's drain tool lowers a guarded role itself; llmd waits for it to finish. Its output
    goes to the terminal: a drain takes minutes and says what it waits for."""
    name, _, role = step["workload"].partition("/")
    values = {
        "workload": step["workload"],
        "role": role,
        "set": name if role else "",
        "namespace": plan.namespace,
        "source": str(plan.stack.source.root),
        "count": str(step["to"]),
        "old_revision": "",
        "max_unavailable": str(step["from"] - step["to"]),
    }
    argv = drain_command(template, values)
    result = runner(argv, cwd=str(plan.stack.source.root), timeout=DRAIN_TIMEOUT)
    for text in (result.stdout, result.stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", file=sys.stderr)
    if not result.ok:
        raise CommandError(
            f"the drain command for {step['workload']} exited with {result.returncode}; the count was not recorded",
            hint="Check its output above; llmd records the new count only once the drain tool succeeds.",
        )
