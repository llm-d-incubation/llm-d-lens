"""``llmd standup``: apply a stack to an empty namespace (or update the instance llmd stood up)."""

from __future__ import annotations

import argparse
import sys

from .. import companions, lifecycle
from ..argocd import delivery
from ..catalog import find_stack
from ..errors import PolicyError, UsageError
from ..kube import Kube
from ..plan import Options, Plan, discovered_scope, make_plan
from ..preflight import preflight
from ..status import wait_ready
from . import Context, Handler, argocd_settings, progress
from .plan import add_plan_arguments, check_plan, plan_from_args, print_plan


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("standup", help="apply a stack to the namespace and record it as an instance")
    add_plan_arguments(parser)
    parser.add_argument(
        "--expected-digest",
        default="",
        metavar="DIGEST",
        help="refuse unless the plan still has this digest (from a reviewed `llmd plan`)",
    )
    parser.add_argument(
        "--with-companions",
        action="store_true",
        help="first stand up what the namespace still lacks from the stack's companions (`llmd sources add "
        "--companion`), each as its own instance that a teardown of the stack keeps",
    )
    parser.add_argument("--wait", action="store_true", help="wait until every object and pod is Ready")
    parser.add_argument("--timeout", type=int, default=1800, help="--wait timeout in seconds")
    return {"standup": command_standup}


def command_standup(ctx: Context) -> int:
    if argocd_settings(ctx.args) is not None:
        return _standup_argocd(ctx)
    kube = ctx.kube()
    plan = plan_from_args(ctx, kube)
    preflight(kube, plan)
    if ctx.args.with_companions and not _blocked_beyond_companions(plan):
        lines = stand_up_companions(ctx, kube, plan)
        for line in lines:
            print(line)
        if lines:
            kube.forget()
            plan = plan_from_args(ctx, kube)
            preflight(kube, plan)
    companions.check(kube, plan, ctx.runner)
    if plan.blocking:
        if ctx.args.output == "text":
            print_plan(plan)
        else:
            ctx.emit(plan.summary())
        return PolicyError.exit_code
    result = lifecycle.standup(kube, plan, expected_digest=ctx.args.expected_digest, actor=plan.cluster.get("user", ""))
    if ctx.args.wait:
        result["status"] = wait_ready(
            kube,
            plan.namespace,
            plan.instance,
            timeout=ctx.args.timeout,
            emit=lambda line: print(f"[wait] {line}", file=sys.stderr, flush=True),
        )
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    where = f"{result['instance']} in {result['namespace']}"
    print(f"Stood up {where} ({result['applied']} objects, digest {result['digest'][:12]})")
    if result.get("switchedFrom"):
        print(f"  moved from stack {result['switchedFrom']}")
    for item in result["pruned"]:
        print(f"  pruned {item} (no longer in the stack)")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0


def _standup_argocd(ctx: Context) -> int:
    """Stand the stack up through Argo CD: record it, submit its Application, follow its sync."""
    if ctx.args.with_companions:
        raise UsageError(
            "--with-companions stands companions up directly; with --backend argocd stand each one up first",
            hint="Run `llmd standup COMPANION --backend argocd` for each companion `llmd plan` lists.",
        )
    kube = ctx.kube()
    plan = plan_from_args(ctx, kube)
    check_plan(ctx, kube, plan)
    companions.check(kube, plan, ctx.runner)
    if plan.blocking or not plan.delivery:
        print_plan(plan) if ctx.args.output == "text" else ctx.emit(plan.summary())
        return PolicyError.exit_code
    emit = lambda line: print(f"[argocd] {line}", file=sys.stderr, flush=True)  # noqa: E731
    result = delivery.standup(
        kube,
        plan,
        plan.delivery,
        expected_digest=ctx.args.expected_digest,
        actor=plan.cluster.get("user", ""),
        timeout=ctx.args.timeout,
        emit=emit,
    )
    if ctx.args.wait:
        result["status"] = wait_ready(kube, plan.namespace, plan.instance, timeout=ctx.args.timeout, emit=emit)
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    where = f"{result['instance']} in {result['namespace']}"
    print(f"Stood up {where} through Argo CD Application {result['application']} (digest {result['digest'][:12]})")
    print(f"  Argo CD synced {result['objects']} object(s) at {result['revision'][:12]}")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0


def _blocked_beyond_companions(plan: Plan) -> bool:
    """Whether the plan is blocked by something its companions cannot provide: a missing prerequisite
    can be one of them (a claim file), so only the other blocking findings stop the companions."""
    return any(finding.code != "prerequisite-missing" for finding in plan.blocking)


def stand_up_companions(ctx: Context, kube: Kube, plan: Plan) -> list[str]:
    """Stand up each companion of ``plan`` the namespace lacks, in the order the source lists their
    templates, each as its own instance. One that cannot be stood up stops here, before the stack."""
    lines = []
    for item in companions.check(kube, plan, ctx.runner):
        stack = find_stack(plan.stack.source, item.path)
        progress(f"standing up companion {item.path} as {item.instance()}")
        companion = make_plan(
            stack,
            Options(namespace=plan.namespace, instance=item.instance()),
            runner=ctx.runner,
            scope=discovered_scope(kube),
        )
        preflight(kube, companion)
        if companion.blocking:
            print_plan(companion)
            raise PolicyError(
                f"companion {item.path} cannot be stood up; nothing of the stack was applied",
                details=[finding.as_dict() for finding in companion.blocking],
            )
        result = lifecycle.standup(kube, companion, actor=companion.cluster.get("user", ""))
        lines.append(f"Stood up companion {result['instance']} ({result['applied']} objects from {item.path})")
    return lines
