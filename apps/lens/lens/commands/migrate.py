"""``llmd migrate``: move an instance to another namespace or cluster, one replica at a time."""

from __future__ import annotations

import argparse
import sys

from .. import catalog, companions, inventory, migration, overrides
from ..errors import PolicyError, UsageError
from ..kube import Kube, Target
from ..plan import Options, Plan, discovered_scope, make_plan
from ..preflight import preflight
from ..sources import resolve_source
from . import Context, Handler, progress
from .standup import stand_up_companions

ACTIONS = ("plan", "start", "step", "status", "finish")


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "migrate",
        help="move an instance to another namespace or cluster, one replica at a time (no outage)",
        description="Stand the stack up in the target with the moved workload at 1, then, step by step, "
        "raise the target by one, wait until it is Ready, and lower the source by one. Traffic is moved by "
        "hand between steps (a gateway's route weights, or the like). `finish` tears the source down once it "
        "is at 0.",
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("instance", help="the instance in the source namespace")
    parser.add_argument("--from", dest="source_namespace", required=True, metavar="NS", help="source namespace")
    parser.add_argument("--to", dest="target_namespace", required=True, metavar="NS", help="target namespace")
    parser.add_argument(
        "--workload",
        default="",
        metavar="WORKLOAD",
        help="what moves a replica at a time: a Deployment, StatefulSet or LeaderWorkerSet, or SET/ROLE "
        "(default: the stack's only scalable workload that asks for accelerators)",
    )
    parser.add_argument("--stack", default="", help="the stack path for the target (default: the source's)")
    parser.add_argument("--name", default="", help="instance name in the target (default: the same)")
    connection = parser.add_argument_group("connections (one per side; default: the kube context)")
    for side in ("from", "to"):
        connection.add_argument(f"--{side}-context", default="", metavar="CONTEXT")
        connection.add_argument(f"--{side}-kubeconfig", default="", metavar="FILE")
        connection.add_argument(
            f"--{side}-source",
            default="",
            metavar="NAME|PATH|URL#REF",
            help=f"where the {'source' if side == 'from' else 'target'}'s stack comes from (default: --source)",
        )
    parser.add_argument("--source", default="", metavar="NAME|PATH|URL#REF", help="both sides' source")
    parser.add_argument("--refresh", action="store_true", help="fetch a remote source again")
    parser.add_argument(
        "--kube-cli", default="kubectl", choices=["kubectl", "oc"], help="kubectl or oc, for both sides"
    )
    parser.add_argument("--allow-privileged", action="store_true", help="as at standup, for the target")
    parser.add_argument("--skip-admin-parts", action="store_true", help="as at standup, for the target")
    parser.add_argument("--with-companions", action="store_true", help="start: stand up the target's companions first")
    parser.add_argument("--expected-digest", default="", metavar="DIGEST", help="start: from `migrate plan`")
    drain = parser.add_mutually_exclusive_group()
    drain.add_argument(
        "--drain",
        default="",
        metavar="COMMAND",
        help="step: the source stack's drain tool, to lower a DisaggregatedSet role (as for `llmd scale`)",
    )
    drain.add_argument(
        "--no-drain",
        action="store_true",
        help="step: lower a DisaggregatedSet role in the source directly; its pods drain as they stop",
    )
    parser.add_argument("--timeout", type=int, default=3600, help="step: seconds to wait for the target")
    parser.add_argument("--interval", type=int, default=15, help=argparse.SUPPRESS)
    parser.add_argument("-y", "--yes", action="store_true", help="finish: do not ask before tearing down")
    return {"migrate": command_migrate}


def _kube(ctx: Context, side: str) -> Kube:
    args = ctx.args
    kube = Kube(
        Target(
            kubeconfig=getattr(args, f"{side}_kubeconfig"),
            context=getattr(args, f"{side}_context"),
            cli=args.kube_cli,
        ),
        runner=ctx.runner,
    )
    kube.progress = progress
    return kube


def _source(ctx: Context, side: str):
    spec = getattr(ctx.args, f"{side}_source") or ctx.args.source
    if not spec:
        return ctx.source()
    return resolve_source(spec, registry=ctx.registry, runner=ctx.runner, refresh=ctx.args.refresh)


def _plan(ctx: Context, side: migration.Side, stack: dict, source, kept: dict[str, int], *, label: bool):
    """A side's plan: its stack path, the guide variables it was stood up with, and its counts."""
    args = ctx.args
    found = catalog.find_stack(source, stack["id"], runner=ctx.runner)
    progress(f"rendering {found.id} for {side.namespace}")
    return make_plan(
        found,
        Options(
            namespace=side.namespace,
            instance=side.instance,
            allow_privileged=args.allow_privileged,
            skip_admin_parts=args.skip_admin_parts,
            variables=dict(stack.get("variables") or {}),
            sets=list(stack.get("sets") or []),
            overrides=kept,
        ),
        runner=ctx.runner,
        scope=discovered_scope(side.kube),
        label=label,
    )


def _workload(ctx: Context, plan: Plan) -> str:
    if ctx.args.workload:
        return ctx.args.workload
    with_accelerators = {item["object"] for item in plan.policy.workloads if item["accelerators"]}
    found = [
        name
        for name in overrides.workloads(plan.documents)
        if f"{_kind(plan, name)}/{name.partition('/')[0]}" in with_accelerators
    ]
    if len(found) != 1:
        raise UsageError(
            "name the workload to move with --workload",
            hint="The stack's workloads: " + ", ".join(overrides.workloads(plan.documents)),
        )
    return found[0]


def _kind(plan: Plan, name: str) -> str:
    document, _ = overrides.find(list(plan.documents), name)
    return document["kind"]


def command_migrate(ctx: Context) -> int:
    args = ctx.args
    source = migration.Side(_kube(ctx, "from"), args.source_namespace, args.instance, args.from_context)
    target = migration.Side(_kube(ctx, "to"), args.target_namespace, args.name or args.instance, args.to_context)
    source_record = inventory.require(source.kube, source.namespace, source.instance)
    source_imported = inventory.management(source_record) == inventory.IMPORTED
    source_plan = _plan(
        ctx,
        source,
        source_record["stack"],
        _source(ctx, "from"),
        dict(source_record.get("overrides") or {}),
        label=not source_imported,
    )
    workload = (source_record.get("migration") or {}).get("workload") or _workload(ctx, source_plan)
    target_record = inventory.read(target.kube, target.namespace, target.instance)
    # The target follows the source's stack and guide variables, unless it has its own record or --stack.
    target_stack = (target_record or {}).get("stack") or {
        **source_record["stack"],
        "id": args.stack or source_record["stack"]["id"],
    }
    target_kept = dict((target_record or {}).get("overrides") or {})
    if target_record is None:
        target_kept[workload] = 1
    target_plan = _plan(ctx, target, target_stack, _source(ctx, "to"), target_kept, label=True)
    if args.action == "plan":
        return _plan_action(ctx, source, source_plan, target, target_plan, workload)
    if args.action == "start":
        return _start(ctx, source, source_plan, target, target_plan, workload)
    if args.action == "status":
        current = migration.status(source, source_plan, target, target_plan)
        ctx.emit(current) if args.output != "text" else _print_status(current)
        return 0
    if args.action == "step":
        result = migration.step(
            source,
            source_plan,
            target,
            target_plan,
            runner=ctx.runner,
            drain=args.drain,
            no_drain=args.no_drain,
            timeout=args.timeout,
            interval=args.interval,
            emit=lambda line: print(f"[migrate] {line}", file=sys.stderr, flush=True),
            actor=source.kube.whoami(),
        )
        if args.output != "text":
            ctx.emit(result)
        else:
            print(
                f"Step done: {result['workload']} {result['to']} in {target.namespace}, {result['from']} in "
                f"{source.namespace}"
            )
            print("Move traffic toward the target by hand before the next step, if you shift it per step.")
        return 0
    return _finish(ctx, source, source_plan, target, target_plan)


def _plan_action(ctx, source, source_plan, target, target_plan, workload) -> int:
    preflight(target.kube, target_plan)
    companions.check(target.kube, target_plan, ctx.runner)
    planned = migration.plan_migration(source, source_plan, target, target_plan, workload)
    if ctx.args.output != "text":
        ctx.emit({**planned, "target": target_plan.summary()})
    else:
        print(f"Migrate {source.instance}: {source.namespace} -> {target.namespace}")
        print(f"  from {planned['from']['stack']} (context {source.context or 'current'})")
        print(f"  to   {planned['to']['stack']} (context {target.context or 'current'}) as {target.instance}")
        print(f"  workload {workload}: {planned['replicas']} replica(s), {planned['steps']} step(s)")
        print("  each step raises the target by one, waits until it is Ready, then lowers the source by one")
        for finding in target_plan.findings:
            if finding.severity != "info" or finding.code.startswith("companion") or finding.code == "pv-file":
                print(f"  {finding.severity.upper():<8} {finding.message}")
                if finding.hint:
                    print(f"           hint: {finding.hint}")
        for finding in planned["findings"]:
            if not finding["message"].startswith(f"{target.namespace}: "):
                print(f"  {finding['severity'].upper():<8} {finding['message']}")
        print(f"\nDigest {planned['digest'][:16]}")
        blocked = planned["blocking"]
        print("Result " + (f"BLOCKED by {blocked} finding(s)" if blocked else "ready to start"))
    return PolicyError.exit_code if planned["blocking"] else 0


def _start(ctx, source, source_plan, target, target_plan, workload) -> int:
    planned = migration.plan_migration(source, source_plan, target, target_plan, workload)
    if planned["blocking"]:
        for finding in planned["findings"]:
            print(f"  {finding['severity'].upper():<8} {finding['message']}", file=sys.stderr)
        raise PolicyError(f"the migration has {planned['blocking']} blocking finding(s); see `llmd migrate plan`")
    if ctx.args.with_companions:
        for line in stand_up_companions(ctx, target.kube, target_plan):
            print(line)
        target.kube.forget()
    preflight(target.kube, target_plan)
    if target_plan.blocking:
        raise PolicyError(
            f"the target plan has {len(target_plan.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in target_plan.blocking],
            hint="Run `llmd migrate plan` to see them.",
        )
    result = migration.start(
        source,
        target,
        target_plan,
        workload,
        total=planned["replicas"],
        expected_digest=ctx.args.expected_digest,
        actor=target_plan.cluster.get("user", ""),
    )
    if ctx.args.output != "text":
        ctx.emit(result)
    else:
        print(f"Stood up {target.instance} in {target.namespace} with {workload} at 1 ({result['applied']} objects)")
        print(
            f"Next: once it is Ready, `llmd migrate step {source.instance} --from {source.namespace} --to "
            f"{target.namespace}` ({planned['replicas']} step(s))"
        )
    return 0


def _finish(ctx, source, source_plan, target, target_plan) -> int:
    if not ctx.args.yes:
        if not sys.stdin.isatty():
            raise UsageError("refusing to tear down the source without a terminal to confirm; pass -y")
        question = f"Tear down {source.instance} in {source.namespace} (claims are kept)? [y/N] "
        if input(question).strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    record = inventory.require(target.kube, target.namespace, target.instance)
    workload = record["migration"]["workload"]
    plain_options = Options(
        namespace=target.namespace,
        instance=target.instance,
        variables=dict(target_plan.options.variables),
        sets=list(target_plan.options.sets),
    )
    plain = make_plan(target_plan.stack, plain_options, runner=ctx.runner)
    _, holder = overrides.find(plain.documents, workload)
    result = migration.finish(source, source_plan, target, target_plan, files=overrides.replicas(holder))
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    print(f"Tore down {source.instance} in {source.namespace} ({len(result['tornDown']['deleted'])} objects)")
    for item in result["tornDown"]["kept"]:
        print(f"  kept {item}")
    if result["overrides"]:
        listed = ", ".join(f"{name}={count}" for name, count in sorted(result["overrides"].items()))
        print(f"  {target.instance} keeps {listed}; record it in the stack's repository")
    print(f"Migration of {source.instance} to {target.namespace} finished")
    return 0


def _print_status(current: dict) -> None:
    source, target = current["from"], current["to"]
    print(
        f"Migrating {current['workload']} ({current['replicas']} replica(s)): "
        f"{source['namespace']} -> {target['namespace']}"
    )
    for label, side in (("from", source), ("to", target)):
        wanted = "?" if side["wanted"] is None else side["wanted"]
        print(
            f"  {label:<4} {side['namespace']:<32} {side['ready']}/{wanted} ready  "
            f"({side['context'] or 'current context'})"
        )
    print(f"  steps done: {len(current['steps'])}")
    if current["done"]:
        print("  done: finish it with `llmd migrate finish`")
