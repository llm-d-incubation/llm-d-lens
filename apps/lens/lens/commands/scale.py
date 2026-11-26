"""``llmd scale``: set a workload's replicas on an instance, and keep the count in its record."""

from __future__ import annotations

import argparse
import sys

from .. import catalog, inventory, overrides, scaling
from ..argocd import application, delivery
from ..errors import ConflictError, PolicyError, UsageError
from ..plan import Options, discovered_scope, make_plan
from ..preflight import preflight
from . import Context, Handler, add_source_arguments, add_target_arguments, progress
from .plan import print_plan


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "scale",
        help="set the replicas of an instance's workloads; the count stays until cleared",
        description="Set WORKLOAD=N (a Deployment, StatefulSet or LeaderWorkerSet name, or SET/ROLE for a "
        "DisaggregatedSet role). WORKLOAD=-- goes back to what the stack's files say. Only the scaled "
        "objects are applied; later standups and rollouts keep the count. A guide is rendered with the "
        "--var and --set values the instance was stood up with.",
    )
    parser.add_argument("instance")
    parser.add_argument("counts", nargs="+", metavar="WORKLOAD=N")
    add_source_arguments(parser)
    add_target_arguments(parser)
    drain = parser.add_mutually_exclusive_group()
    drain.add_argument(
        "--drain",
        default="",
        metavar="COMMAND",
        help="the stack's drain tool, run to lower a DisaggregatedSet role (it removes the groups once they "
        "are out of the pool and idle); fields: {role} {set} {count} {namespace} {source}",
    )
    drain.add_argument(
        "--no-drain",
        action="store_true",
        help="lower a DisaggregatedSet role directly: each removed pod drains as it stops",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the change; the server checks it")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before scaling a workload to 0")
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="as at standup: acknowledge privileged pods an administrator granted",
    )
    parser.add_argument(
        "--skip-admin-parts", action="store_true", help="as at standup: leave out what an administrator applies"
    )
    return {"scale": command_scale}


def instance_options(record: dict, namespace: str, instance: str, **extra) -> Options:
    """Options that render an instance's stack the way it was stood up: its --var and --set values."""
    stack = record.get("stack") or {}
    return Options(
        namespace=namespace,
        instance=instance,
        variables=dict(stack.get("variables") or {}),
        sets=list(stack.get("sets") or []),
        **extra,
    )


def file_counts(ctx: Context, stack, record: dict, namespace: str, instance: str, changes: dict) -> dict[str, int]:
    """The replicas the stack's files say for each changed workload (rendered offline, no overrides)."""
    plain = make_plan(stack, instance_options(record, namespace, instance), runner=ctx.runner)
    counts = {}
    for workload in changes:
        try:
            _, holder = overrides.find(plain.documents, workload)
        except UsageError:
            continue
        counts[workload] = overrides.replicas(holder)
    return counts


def command_scale(ctx: Context) -> int:
    args = ctx.args
    changes = dict(overrides.parse(item) for item in args.counts)
    kube = ctx.kube()
    namespace = ctx.namespace(kube)
    record = inventory.require(kube, namespace, args.instance)
    kept = {**(record.get("overrides") or {})}
    for workload, count in changes.items():
        if count is None:
            kept.pop(workload, None)
        else:
            kept[workload] = count
    if delivery.is_argocd(record):
        return _scale_argocd(ctx, kube, record, changes, kept)
    stack = catalog.find_stack(ctx.source(), record["stack"]["id"])
    progress(f"rendering {stack.id}")
    imported = inventory.management(record) == inventory.IMPORTED
    plan = make_plan(
        stack,
        instance_options(
            record,
            namespace,
            args.instance,
            allow_privileged=args.allow_privileged,
            skip_admin_parts=args.skip_admin_parts,
            overrides=kept,
        ),
        runner=ctx.runner,
        scope=discovered_scope(kube),
        label=not imported,
    )
    preflight(kube, plan, existing="import" if imported else "rollout")
    for workload, count in changes.items():
        overrides.find(plan.documents, workload)  # refuses a workload the stack does not render
        if count == 0 and not args.yes and not args.dry_run:
            question = f"Scale {workload} of {args.instance} to 0? It stops serving. [y/N] "
            if not sys.stdin.isatty():
                raise UsageError(f"refusing to scale {workload} to 0 without a terminal to confirm; pass -y")
            if input(question).strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 1
    result = scaling.scale(
        kube,
        plan,
        changes,
        record=record,
        runner=ctx.runner,
        files=file_counts(ctx, stack, record, namespace, args.instance, changes),
        drain=args.drain,
        no_drain=args.no_drain,
        dry_run=args.dry_run,
        actor=plan.cluster.get("user", ""),
    )
    if args.output != "text":
        ctx.emit(result)
        return 0
    verb = "Would scale" if result.get("dryRun") else "Scaled"
    for step in result["steps"]:
        before = "?" if step["from"] is None else step["from"]
        how = " (by the drain command)" if step["drain"] else ""
        same = " (unchanged)" if step["from"] == step["to"] else ""
        print(f"{verb} {step['workload']} of {args.instance}: {before} -> {step['to']}{how}{same}")
    if not result.get("dryRun"):
        if result["overrides"]:
            listed = ", ".join(f"{name}={count}" for name, count in sorted(result["overrides"].items()))
            print(f"  kept for later standups and rollouts: {listed}")
            print("  record it in the stack's repository too, or clear it with WORKLOAD=--")
        else:
            print("  no counts kept: the instance follows the stack's files")
    return 0


def _scale_argocd(ctx: Context, kube, record: dict, changes: dict, kept: dict) -> int:
    """Keep the counts on the instance's Argo CD Application and sync it: a count set on the objects
    beside Argo CD would be set back by its next sync."""
    args = ctx.args
    if args.drain or args.no_drain:
        raise UsageError(
            "--drain and --no-drain are for the direct backend; Argo CD applies the new count",
            hint="Lower a DisaggregatedSet role in the stack's files; Argo CD cannot keep a role's count.",
        )
    namespace, instance = record["namespace"], record["instance"]
    if (record.get("rollout") or {}).get("phase") in ("Rolling", "Paused", "Aborting"):
        raise ConflictError(f"instance {instance} has a rollout in progress; scale it once the rollout is finished")
    stack = catalog.find_stack(ctx.source(), record["stack"]["id"])
    running = (record.get("delivery") or {}).get("revision") or ""
    if running and stack.source.revision != running:
        raise ConflictError(
            f"the source is at {stack.source.revision[:12]}, but instance {instance} runs {running[:12]}; a scale "
            "would move it to the new commit as well",
            hint="Move it with `llmd rollouts start --backend argocd` first, or pass a --source at that commit.",
        )
    progress(f"rendering {stack.id}")
    files = file_counts(ctx, stack, record, namespace, instance, changes)
    # A count the stack's files already say needs no override.
    kept = {workload: count for workload, count in kept.items() if files.get(workload) != count}
    plan = make_plan(
        stack,
        instance_options(record, namespace, instance, overrides=kept),
        runner=ctx.runner,
        scope=discovered_scope(kube),
    )
    for workload in changes:
        overrides.find(plan.documents, workload)  # refuses a workload the stack does not render
    settings = delivery.settings_for(kube, record, revision=_target_revision(kube, record))
    delivery.check(kube, plan, settings, ctx.runner, existing="rollout")
    if plan.blocking:
        print_plan(plan) if args.output == "text" else ctx.emit(plan.summary())
        return PolicyError.exit_code
    if args.dry_run:
        result = {"instance": instance, "namespace": namespace, "overrides": kept, "application": plan.delivery}
        ctx.emit(result) if args.output != "text" else print(f"Would keep {kept or 'no counts'} on the Application")
        return 0
    for workload, count in changes.items():
        if count == 0 and not args.yes:
            question = f"Scale {workload} of {instance} to 0? It stops serving. [y/N] "
            if not sys.stdin.isatty():
                raise UsageError(f"refusing to scale {workload} to 0 without a terminal to confirm; pass -y")
            if input(question).strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 1
    updated = delivery.update(kube, plan, record, actor=plan.cluster.get("user", ""))
    result = {
        "instance": instance,
        "namespace": namespace,
        "backend": delivery.BACKEND,
        "application": "/".join(delivery.application_ref(updated)),
        "overrides": kept,
    }
    if args.output != "text":
        ctx.emit(result)
        return 0
    listed = ", ".join(f"{name}={count}" for name, count in sorted(kept.items())) or "none"
    print(f"Kept counts on Argo CD Application {result['application']}: {listed}; Argo CD syncs the change")
    print(f"Follow it with: llmd status {instance} -n {namespace}")
    return 0


def _target_revision(kube, record: dict) -> str:
    """The Application's ``targetRevision`` as it is now, so a scale does not move the instance to
    another commit (one source: its revision; several: the first Git source's)."""
    live = delivery.require_application(kube, record)
    for source in application.application_sources(live):
        if source.get("path") is not None or source.get("ref"):
            return str(source.get("targetRevision") or "")
    return ""
