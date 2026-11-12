"""``llmd scale``: set a workload's replicas on an instance, and keep the count in its record."""

from __future__ import annotations

import argparse
import sys

from .. import catalog, draining, inventory, overrides, scaling
from ..errors import UsageError
from ..plan import Options, discovered_scope, make_plan
from ..preflight import preflight
from . import Context, Handler, add_source_arguments, add_target_arguments, label, progress, rollouts


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
        help="the stack's drain tool, run to lower a workload (it removes the pods or groups once they are out "
        "of the pool and idle, and lowers the count itself); fields: {workload} {role} {set} {count} "
        "{namespace} {source}",
    )
    drain.add_argument(
        "--no-drain",
        action="store_true",
        help="lower a DisaggregatedSet role directly: each removed pod drains as it stops",
    )
    drain.add_argument(
        "--drain-idle",
        action="store_true",
        help="llmd drains the pods it removes: out of the router's pool by --pool-label, then removed once "
        "their vLLM engines are idle (read through the pod proxy); a pod that does not go idle in "
        "--drain-timeout goes back in the pool",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=1,
        metavar="N",
        help="with --drain-idle: pods (or groups of a role) drained together (default 1)",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=1800,
        metavar="SECONDS",
        help="with --drain-idle: how long a batch may take to go idle (default 1800)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        action="append",
        default=[],
        metavar="PORT",
        help="with --drain-idle: a port serving vLLM's /metrics (repeatable; default: every container port)",
    )
    parser.add_argument("--drain-interval", type=float, default=10, help=argparse.SUPPRESS)
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait until each raised workload serves: its pods Ready and, with a pool label, in the pool",
    )
    parser.add_argument("--timeout", type=float, default=7200, help="--wait timeout in seconds (default 7200)")
    parser.add_argument("--interval", type=float, default=15, help=argparse.SUPPRESS)
    parser.add_argument("--pool-label", default=None, metavar="LABEL", help=rollouts.POOL_HELP)
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


def _drain_settings(ctx: Context, record: dict) -> draining.Settings:
    args = ctx.args
    if args.batch < 1:
        raise UsageError("--batch takes 1 or more")
    return draining.Settings(
        pool_label=rollouts.pool_label(ctx, record),
        batch=args.batch,
        timeout=args.drain_timeout,
        ports=tuple(args.metrics_port),
        interval=args.drain_interval,
        grace=3 * args.drain_interval,
        emit=progress,
    )


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
        drain_idle=_drain_settings(ctx, record) if args.drain_idle else None,
        dry_run=args.dry_run,
        actor=plan.cluster.get("user", ""),
    )
    raised = {step["workload"]: step["to"] for step in result["steps"] if (step["from"] or 0) < step["to"]}
    if args.wait and raised and not result.get("dryRun"):
        scaling.wait_serving(
            kube,
            plan,
            raised,
            pool_label=rollouts.pool_label(ctx, record),
            timeout=args.timeout,
            interval=args.interval,
            emit=progress,
            each=lambda: label.relabel(kube, namespace, record, list(raised)),
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
