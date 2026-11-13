"""Command-line commands. Each module adds its subcommands with ``register(subparsers)`` and
returns ``{name: handler}``; a handler takes a :class:`Context` and returns the exit code."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths, yamlio
from ..argocd import application, delivery
from ..errors import UsageError
from ..kube import Kube, Target
from ..model import validate_namespace
from ..process import Runner, subprocess_runner
from ..sources import Source, SourceRegistry, resolve_source

#: Tests set this to an in-memory cluster; it is never set in normal use.
RUNNER: Runner | None = None


@dataclass
class Context:
    args: argparse.Namespace

    @property
    def runner(self) -> Runner:
        return RUNNER or subprocess_runner

    @property
    def state_dir(self) -> Path:
        return paths.state_dir(self.args.state_dir or None)

    @property
    def registry(self) -> SourceRegistry:
        return SourceRegistry(self.state_dir)

    def source(self) -> Source:
        """The ``--source`` of the command; with exactly one registered source it may be left out."""
        spec = getattr(self.args, "source", "") or ""
        if not spec:
            entries = self.registry.entries()
            if len(entries) == 1:
                spec = entries[0]["name"]
        return resolve_source(
            spec, registry=self.registry, runner=self.runner, refresh=getattr(self.args, "refresh", False)
        )

    def kube(self) -> Kube:
        args = self.args
        target = Target(
            kubeconfig=getattr(args, "kubeconfig", ""),
            context=getattr(args, "context", ""),
            cli=getattr(args, "kube_cli", "kubectl"),
        )
        kube = Kube(target, runner=self.runner)
        kube.progress = progress
        return kube

    def namespace(self, kube: Kube | None = None) -> str:
        """``-n``, else ``LLMD_NAMESPACE`` (the flag's default), else the kube context's namespace."""
        namespace = getattr(self.args, "namespace", "") or (kube.default_namespace() if kube else "")
        return validate_namespace(namespace)

    def emit(self, value: Any) -> None:
        emit_value(value, self.args.output)


Handler = Callable[[Context], int]


def progress(line: str) -> None:
    """What a command is doing, on stderr, so a slow step is visible and ``-o json`` stays clean."""
    print(f"[llmd] {line}", file=sys.stderr, flush=True)


def emit_value(value: Any, output: str) -> None:
    if output == "json":
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        print(yamlio.dump(value), end="")


def parse_variables(items: list[str]) -> dict[str, str]:
    """``--var NAME=VALUE`` items as a mapping (a later item wins)."""
    variables = {}
    for item in items or []:
        name, separator, value = item.partition("=")
        if not separator or not name.isidentifier():
            raise UsageError(f"--var expects NAME=VALUE, got {item!r}")
        variables[name] = value
    return variables


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        default="",
        metavar="NAME|PATH|URL#REF",
        help="where the stack comes from: a registered name, a local checkout, or a Git URL with a ref "
        "(optional when exactly one source is registered)",
    )
    parser.add_argument("--refresh", action="store_true", help="fetch a remote source again")


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    """How a planned stack is delivered: applied by llmd, or by Argo CD from an Application."""
    parser.add_argument(
        "--backend",
        choices=["direct", "argocd"],
        default="direct",
        help="who applies the stack: llmd (direct, the default), or Argo CD through one Application",
    )
    argocd = parser.add_argument_group("Argo CD (--backend argocd)")
    argocd.add_argument(
        "--argocd-app",
        default=os.environ.get("LLMD_ARGOCD_APP", ""),
        metavar="FILE",
        help="the Application manifest to start from, as you would write it for one stack: its project, "
        "destination cluster and sync policy; llmd sets its sources and destination namespace "
        "(env LLMD_ARGOCD_APP)",
    )
    argocd.add_argument(
        "--argocd-namespace",
        default="",
        metavar="NAMESPACE",
        help="the namespace Argo CD runs in, where its AppProjects are (default: the Application's namespace)",
    )
    argocd.add_argument(
        "--argocd-revision",
        default="",
        metavar="REF",
        help="the Application's targetRevision (default: the commit the plan rendered; a branch follows its "
        "new commits)",
    )
    argocd.add_argument(
        "--argocd-remote",
        default="origin",
        metavar="REMOTE",
        help="the Git remote of a local checkout that Argo CD fetches (default origin)",
    )


def argocd_settings(args: argparse.Namespace):
    """The ``--argocd-*`` settings, or None for ``--backend direct``."""
    if getattr(args, "backend", "direct") != "argocd":
        return None
    if not args.argocd_app:
        raise UsageError(
            "--backend argocd needs --argocd-app FILE",
            hint="Write the Application you would make for one stack (project, destination, sync policy); "
            "llmd fills in its sources.",
        )
    return delivery.Settings(
        template=application.load_template(Path(args.argocd_app).expanduser()),
        namespace=args.argocd_namespace,
        revision=args.argocd_revision,
        remote=args.argocd_remote,
    )


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """Which cluster and namespace a command talks to."""
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("LLMD_NAMESPACE", ""),
        help="target namespace (default: $LLMD_NAMESPACE, then the kube context's namespace)",
    )
    connection = parser.add_argument_group("connection")
    connection.add_argument("--kubeconfig", default="", help="kubeconfig file (default: kubectl's own)")
    connection.add_argument("--context", default="", help="kube context")
    connection.add_argument("--kube-cli", default=os.environ.get("LLMD_KUBE_CLI", "kubectl"), choices=["kubectl", "oc"])
