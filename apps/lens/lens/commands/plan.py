"""``llmd plan``: what ``llmd standup`` would do, without changing anything."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .. import catalog, yamlio
from ..errors import PolicyError, UsageError
from ..model import validate_namespace
from ..plan import Options, Plan, make_plan
from . import Context, Handler, add_source_arguments, parse_variables


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments every command that plans a stack takes."""
    parser.add_argument(
        "stack",
        help="a guide (guides/optimized-baseline, or just optimized-baseline), a kustomize directory, or "
        "manifest files of the source (see `llmd stacks`)",
    )
    add_source_arguments(parser)
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("LLMD_NAMESPACE", ""),
        help="target namespace (default: $LLMD_NAMESPACE)",
    )
    parser.add_argument("--name", default="", help="instance name (default: from the stack's path)")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a guide variable, as the README's `export NAME=VALUE` sets it: ACCELERATOR_TYPE, "
        "MODEL_SERVER, INFRA_PROVIDER and the others `llmd stacks GUIDE` lists (repeatable)",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="sets",
        metavar="KEY=VALUE",
        help="a value for the Helm charts the guide installs (the router), as `helm install --set` "
        "takes it (repeatable)",
    )
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="acknowledge privileged containers, host namespaces, hostPath volumes and SCC bindings "
        "that an administrator granted to the namespace",
    )
    parser.add_argument(
        "--skip-admin-parts",
        action="store_true",
        help="leave out cluster-scoped objects and SCC bindings; an administrator applies them",
    )


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("plan", help="render, check, and show what standup would do (no changes)")
    add_plan_arguments(parser)
    parser.add_argument("--offline", action="store_true", help="do not contact the cluster (required for now)")
    parser.add_argument("--manifest", default="", metavar="FILE", help="also write the planned objects here")
    parser.add_argument("--admin-yaml", default="", metavar="FILE", help="write the administrator parts here")
    return {"plan": command_plan}


def plan_from_args(ctx: Context) -> Plan:
    """The plan the command line asks for, offline."""
    args = ctx.args
    stack = catalog.find_stack(ctx.source(), args.stack)
    options = Options(
        namespace=validate_namespace(args.namespace),
        instance=args.name or catalog.default_name(stack),
        allow_privileged=args.allow_privileged,
        skip_admin_parts=args.skip_admin_parts,
        variables=parse_variables(args.var),
        sets=list(args.sets),
    )
    if stack.kind != "guide" and (options.variables or options.sets):
        raise UsageError(
            f"--var and --set are for guides; {stack.id} is a {stack.kind} stack",
            hint="Render a kustomize directory as it is, or name the guide it belongs to.",
        )
    return make_plan(stack, options, runner=ctx.runner)


def command_plan(ctx: Context) -> int:
    if not ctx.args.offline:
        raise UsageError("llmd plan checks a cluster in a later version; pass --offline")
    plan = plan_from_args(ctx)
    if ctx.args.manifest:
        yamlio.write_text_atomic(Path(ctx.args.manifest), yamlio.dump_documents(plan.documents))
    if ctx.args.admin_yaml:
        parts = [part["document"] for part in plan.policy.admin_parts]
        yamlio.write_text_atomic(Path(ctx.args.admin_yaml), yamlio.dump_documents(parts))
    if ctx.args.output != "text":
        ctx.emit({**plan.summary(), "documents": plan.documents})
    else:
        print_plan(plan)
    return PolicyError.exit_code if plan.blocking else 0


def print_plan(plan: Plan) -> None:
    summary = plan.summary()
    source = summary["source"]
    revision = f" @ {source['revision'][:12]}" if source["revision"] else ""
    print(f"Plan   {summary['stack']}")
    print(f"Target {summary['namespace']}/{summary['instance']}")
    print(f"Source {source['name']}{revision}{' (dirty)' if source['dirty'] else ''}")
    if summary.get("choices", {}).get("variables"):
        chosen = " ".join(f"{name}={value}" for name, value in summary["choices"]["variables"].items())
        print(f"Vars   {chosen}")
    for value in summary.get("choices", {}).get("sets") or []:
        print(f"Set    {value}")
    print(f"Render {summary['render']}")
    if summary.get("steps"):
        print("\nGuide steps")
        for item in summary["steps"]:
            if item["state"] not in ("when", "mode", "variables", "not needed"):
                print(f"  {item['state']:<14} {item['step']}: {item['detail']}")
        skipped = sum(1 for item in summary["steps"] if item["state"] in ("when", "mode"))
        if skipped:
            print(f"  ({skipped} step(s) for other variables or Gateway Mode left out; -o yaml lists them)")
    print(f"\nObjects ({len(summary['objects'])})")
    for item in summary["objects"]:
        print(f"  {item}")
    if summary["workloads"]:
        print("\nWorkloads")
        for item in summary["workloads"]:
            where = item["object"] + ("" if item["template"] in ("template", "pod") else f" {item['template']}")
            pods = "?" if item["pods"] is None else item["pods"]
            extras = "".join(f", {resource} x{count}/pod" for resource, count in sorted(item["accelerators"].items()))
            print(f"  {where}: {pods} pod(s){extras}")
        if summary["accelerators"]:
            total = ", ".join(f"{count} {resource}" for resource, count in summary["accelerators"].items())
            print(f"  total: {total}")
    if summary["prerequisites"]:
        print("\nMust already exist in the namespace")
        for item in summary["prerequisites"]:
            state = item.get("state")
            print(f"  {item['kind']}/{item['name']}" + (f"  [{state}]" if state else ""))
    if summary["adminParts"]:
        print("\nNeeds an administrator (`--admin-yaml FILE` writes them for `kubectl apply -f FILE`)")
        for item in summary["adminParts"]:
            print(f"  {item['object']}  [{item['reason']}]")
    if plan.cluster:
        print("\nCluster")
        for key, value in plan.cluster.items():
            print(f"  {key}: {value}")
    if summary["findings"]:
        print("\nFindings")
        for finding in summary["findings"]:
            print(f"  {finding['severity'].upper():<8} {finding['message']}")
            if finding.get("hint"):
                print(f"           hint: {finding['hint']}")
    print(f"\nDigest {summary['digest'][: yamlio.SHORT_DIGEST]}")
    print("Result " + (f"BLOCKED by {summary['blocking']} finding(s)" if summary["blocking"] else "ready to stand up"))
