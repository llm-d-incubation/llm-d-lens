"""``llmd import``: record a stack deployed with ``kubectl apply`` or Helm, changing nothing."""

from __future__ import annotations

import argparse
from dataclasses import replace

from ..argocd import application, delivery
from ..errors import NotFoundError, PolicyError, UsageError
from ..importing import compare, import_stack
from ..preflight import preflight
from . import Context, Handler, argocd_settings
from .plan import add_plan_arguments, plan_from_args, print_plan


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser(
        "import",
        help="record a running stack (deployed with `kubectl apply` or Helm) as an instance; nothing else changes",
    )
    add_plan_arguments(parser)
    parser.add_argument(
        "--manager",
        default="kubectl",
        choices=["kubectl", "helm", "other"],
        help="the workflow that deployed the stack (default: kubectl, for `kubectl apply -k`)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only check that the stack's objects run and match the files"
    )
    parser.add_argument(
        "--argocd-application",
        default="",
        metavar="[NAMESPACE/]NAME",
        help="with --backend argocd: the Application someone else made that delivers the stack (default: the "
        "name --argocd-app gives it); llmd records it and only reads it",
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="with --backend argocd: record the Application even if its sources differ from the stack's",
    )
    return {"import": command_import}


def command_import(ctx: Context) -> int:
    if argocd_settings(ctx.args) is not None:
        return _import_argocd(ctx)
    kube = ctx.kube()
    # Imported objects are never labelled, so they are planned without llmd's labels.
    plan = plan_from_args(ctx, kube, label=False)
    preflight(kube, plan, existing="import")
    # An import creates no pod, so a claim no new pod can mount does not stop it; a rollout or scale it
    # leads to still refuses.
    plan.policy.findings[:] = [
        replace(finding, severity="warning") if finding.code == "claim-unusable" else finding
        for finding in plan.findings
    ]
    report = compare(kube, plan)
    missing = [item for item in report if item["state"] == "missing"]
    drifted = [item for item in report if item["state"] == "drifted"]
    if ctx.args.dry_run or missing:
        if ctx.args.output != "text":
            ctx.emit({**plan.summary(), "comparison": report})
        else:
            print_plan(plan)
            _print_comparison(report)
        return PolicyError.exit_code if missing else 0
    result = import_stack(kube, plan, manager=ctx.args.manager, actor=plan.cluster.get("user", ""))
    result["drifted"] = [item["object"] for item in drifted]
    if ctx.args.output != "text":
        ctx.emit(result)
        return 0
    where = f"{result['instance']} in {result['namespace']}"
    print(f"Imported {where}: {result['imported']} running objects recorded; nothing on them was changed")
    for item in drifted:
        where = ", ".join(item["differences"][:4])
        print(f"  warning: {item['object']} differs from the stack's files at {where}")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0


def _print_comparison(report: list[dict]) -> None:
    print("\nRunning objects compared with the stack's files")
    for item in report:
        detail = f"  ({', '.join(item['differences'][:4])})" if item["differences"] else ""
        print(f"  {item['state']:<9} {item['object']}{detail}")


def _import_argocd(ctx: Context) -> int:
    """Record an Argo CD Application someone else made; nothing on the cluster changes but the record."""
    args = ctx.args
    kube = ctx.kube()
    plan = plan_from_args(ctx, kube, label=False)
    settings = argocd_settings(args)
    built = delivery.attach(plan, settings, ctx.runner)
    where, _, name = args.argocd_application.rpartition("/")
    if built is not None:
        where = where or built["metadata"]["namespace"]
        name = name or built["metadata"]["name"]
    if not where or not name:
        raise UsageError("name the Application with --argocd-application NAMESPACE/NAME")
    found = kube.get(application.RESOURCE, name, where)
    if found is None:
        raise NotFoundError(
            f"Argo CD Application {where}/{name} not found",
            hint="Pass --argocd-application NAMESPACE/NAME of the Application that delivers the stack.",
        )
    if args.dry_run:
        differences = delivery.compare_application(built, found) if built else []
        result = {"application": f"{where}/{name}", "differences": differences, **delivery.state(found)}
        ctx.emit(result) if args.output != "text" else _print_application(result)
        return PolicyError.exit_code if differences else 0
    result = delivery.import_application(kube, plan, found, actor=kube.whoami(), allow_drift=args.allow_drift)
    if args.output != "text":
        ctx.emit(result)
        return 0
    print(
        f"Imported {result['instance']} in {result['namespace']}: Application {result['application']} and "
        f"{result['imported']} object(s) it manages are recorded; llmd only reads them"
    )
    for item in result["differences"]:
        print(f"  warning: the Application differs from the stack: {item}")
    print(f"Follow it with: llmd status {result['instance']} -n {result['namespace']}")
    return 0


def _print_application(result: dict) -> None:
    print(
        f"Application {result['application']}: {result['state']}, sync {result.get('sync')}, health "
        f"{result.get('health')}"
    )
    for item in result["differences"]:
        print(f"  differs: {item}")
    if not result["differences"]:
        print("  delivers the stack as llmd would")
