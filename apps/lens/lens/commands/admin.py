"""``llmd admin``: read-only checks for the people who administer what a namespace user relies on.

``llmd admin argocd check`` reads an Argo CD install: whether the cluster serves Applications, the
kustomize build options and the tracking method in ``argocd-cm``, and the namespaces Argo CD reconciles
Applications in. Given a stack and an Application template, it also checks the Application's project and
writes the AppProject change that would admit it (``--project-yaml FILE``) for the administrator to review
and apply. It changes nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import catalog, yamlio
from ..argocd import application as application_module
from ..argocd import checks, delivery
from ..errors import PolicyError, UsageError
from ..model import validate_namespace
from ..plan import Options, discovered_scope, make_plan
from . import Context, Handler, add_source_arguments, parse_variables


def register(commands: argparse._SubParsersAction) -> dict[str, Handler]:
    parser = commands.add_parser("admin", help="read-only checks of what an administrator provides (Argo CD)")
    tools = parser.add_subparsers(dest="tool", metavar="TOOL", required=True)
    argocd = tools.add_parser("argocd", help="an Argo CD install")
    actions = argocd.add_subparsers(dest="action", metavar="ACTION", required=True)
    check = actions.add_parser(
        "check",
        help="check the Argo CD install, and (with a stack) whether its project admits the stack's Application",
    )
    check.add_argument(
        "stack",
        nargs="?",
        default="",
        help="a stack to check the Application of (see `llmd stacks`); needs --argocd-app",
    )
    check.add_argument(
        "-n",
        "--namespace",
        default="",
        help="the namespace Argo CD runs in (default: the template's metadata.namespace)",
    )
    check.add_argument(
        "--target", default="", metavar="NAMESPACE", help="the namespace the stack is deployed to (with a stack)"
    )
    check.add_argument("--name", default="", help="instance name (default: from the stack's path)")
    check.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="a guide variable")
    check.add_argument(
        "--set", action="append", default=[], dest="sets", metavar="KEY=VALUE", help="a value for the guide's charts"
    )
    check.add_argument("--argocd-app", default="", metavar="FILE", help="the Application template")
    check.add_argument("--argocd-revision", default="", metavar="REF", help="the Application's targetRevision")
    check.add_argument("--argocd-remote", default="origin", metavar="REMOTE", help="the Git remote Argo CD fetches")
    check.add_argument(
        "--project-yaml",
        default="",
        metavar="FILE",
        help="write the AppProject as it would admit the stack's Application, for an administrator to apply",
    )
    add_source_arguments(check)
    connection = check.add_argument_group("connection")
    connection.add_argument("--kubeconfig", default="", help="kubeconfig file (default: kubectl's own)")
    connection.add_argument("--context", default="", help="kube context")
    connection.add_argument("--kube-cli", default="kubectl", choices=["kubectl", "oc"])
    return {"admin": command_admin}


def command_admin(ctx: Context) -> int:
    args = ctx.args
    kube = ctx.kube()
    plan = application = None
    if args.stack:
        if not args.argocd_app or not args.target:
            raise UsageError("a stack is checked with --argocd-app FILE and --target NAMESPACE")
        stack = catalog.find_stack(ctx.source(), args.stack, runner=ctx.runner)
        options = Options(
            namespace=validate_namespace(args.target),
            instance=args.name or catalog.default_name(stack),
            variables=parse_variables(args.var),
            sets=list(args.sets),
        )
        plan = make_plan(stack, options, runner=ctx.runner, scope=discovered_scope(kube))
        settings = delivery.Settings(
            template=application_module.load_template(Path(args.argocd_app).expanduser()),
            namespace=args.namespace,
            revision=args.argocd_revision,
            remote=args.argocd_remote,
        )
        application = delivery.attach(plan, settings, ctx.runner)
    namespace = args.namespace or (application or {}).get("metadata", {}).get("namespace", "")
    if not namespace:
        raise UsageError("pass -n with the namespace Argo CD runs in")
    report, findings, change = checks.check_install(kube, namespace, plan, application)
    if plan is not None:
        findings = [*[finding for finding in plan.findings if finding.code.startswith("argocd-")], *findings]
    if change is not None and args.project_yaml:
        yamlio.write_text_atomic(Path(args.project_yaml), yamlio.dump_documents([change]))
    blocking = [finding for finding in findings if finding.severity == "error"]
    if args.output != "text":
        ctx.emit(
            {
                **report,
                **({"application": application} if application else {}),
                "findings": [finding.as_dict() for finding in findings],
                **({"projectChange": change} if change else {}),
                "blocking": len(blocking),
            }
        )
        return PolicyError.exit_code if blocking else 0
    print(f"Argo CD in {namespace}")
    for key, label in (
        ("kustomizeBuildOptions", "kustomize build options"),
        ("trackingMethod", "tracking method"),
        ("applicationNamespaces", "Application namespaces"),
        ("project", "project"),
    ):
        if key in report:
            value = report[key]
            print(f"  {label}: {', '.join(value) if isinstance(value, list) else value or '(none)'}")
    if application is not None:
        metadata = application["metadata"]
        print(f"  Application {metadata['namespace']}/{metadata['name']} for {plan.stack.id} in {plan.namespace}")
    for finding in findings:
        print(f"  {finding.severity.upper():<8} {finding.message}")
        if finding.hint:
            print(f"           hint: {finding.hint}")
    if change is not None:
        where = f" in {args.project_yaml}" if args.project_yaml else " (pass --project-yaml FILE to write it)"
        print(f"\nThe AppProject change that admits the Application is written{where}; llmd does not apply it.")
    print("Result " + (f"{len(blocking)} problem(s)" if blocking else "ready"))
    return PolicyError.exit_code if blocking else 0
