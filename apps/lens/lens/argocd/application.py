"""The Argo CD Application for a plan: sources that point at the stack's own files, and the rest from a
template the team keeps.

Argo CD renders the sources itself, with its own Helm and kustomize, so the Application deploys what
the team would deploy by hand:

* a kustomize stack (or one kustomize directory a guide applies) is a Git source whose ``path`` is that
  directory;
* manifest files are a Git source of their directory, with ``directory.include`` naming the files;
* a Helm chart a guide installs is a Helm source of the same chart and version (an ``oci://`` chart),
  or a Git source of the chart's directory; its values files come from the checkout through a ``ref``
  source, and ``--set`` values become Helm parameters.

Every path comes from the stack's plan; nothing here names a directory of a source. The template is an
Application manifest (the one a team would write by hand): ``spec.project``, ``spec.destination`` (the
cluster), ``spec.syncPolicy`` and ``metadata.namespace`` come from it. Its ``metadata.name`` may hold
``{instance}``, ``{namespace}``, ``{name}`` (the stack's last segment) and ``{destination}`` (the
destination cluster's name). llmd sets the sources, the destination namespace, and its own labels.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .. import overrides, yamlio
from ..catalog import manifest_files, uses_helm
from ..errors import CommandError, UsageError
from ..guides import Unit
from ..model import INSTANCE_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE, Finding, api_group, object_ref
from ..plan import Plan
from ..process import Runner

API_VERSION = "argoproj.io/v1alpha1"
RESOURCE = "applications.argoproj.io"
PROJECT_RESOURCE = "appprojects.argoproj.io"
#: Deleting an Application that carries it deletes what the Application deployed.
FINALIZER = "resources-finalizer.argocd.argoproj.io"
#: Where Argo CD (annotation tracking, its default since 2.x) records which Application owns an object.
TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
SYNC_OPTIONS_ANNOTATION = "argocd.argoproj.io/sync-options"
#: On the Application: the namespace of the instance it delivers (the Application may live elsewhere).
NAMESPACE_ANNOTATION = "lens.llm-d.ai/namespace"
#: The ``ref`` of the source that only supplies values files (``$values/<path>``).
VALUES_REF = "values"
NAME_FIELDS = ("instance", "namespace", "name", "destination")

#: What Argo CD and Helm read as one version (a tag), not a constraint: ``v0``, ``0.11``, ``v0.11.2``.
_VERSION = re.compile(r"^v?\d+(\.\d+){0,2}([-+][0-9A-Za-z.+-]+)?$")
#: A release version, which a registry is not expected to move.
_RELEASE = re.compile(r"^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.+-]+)?$")
_GLOB_CHARACTERS = set("*?[]{},\\!")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class GitOrigin:
    """Where Argo CD fetches the stack: the remote URL and the revision the Application names."""

    repo_url: str
    #: The commit the plan rendered.
    revision: str
    #: What the Application's ``targetRevision`` says: the commit, or a ref that follows new commits.
    target: str
    #: The source's directory in the repository ("" at its root): Application paths start at the root.
    prefix: str = ""

    def path(self, relative: str) -> str:
        joined = "/".join(part for part in (self.prefix.strip("/"), relative.strip("/")) if part and part != ".")
        return joined or "."


def strip_credentials(url: str) -> str:
    """A clone URL without a user or token, so none reaches an Application or a record."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def is_commit(revision: str) -> bool:
    return bool(_COMMIT.match(revision or ""))


def load_template(path: Path) -> dict[str, Any]:
    """An Application manifest the team keeps; only one document, of kind Application."""
    try:
        documents = yamlio.load_documents(Path(path).read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise UsageError(f"cannot read the Argo CD Application template {path}: {exc}") from exc
    if len(documents) != 1 or documents[0].get("kind") != "Application":
        raise UsageError(
            f"{path} must hold exactly one Argo CD Application",
            hint="Use the Application you would write by hand for one stack; llmd sets its sources.",
        )
    return documents[0]


def application_name(template: dict[str, Any], fields: dict[str, str]) -> str:
    """The template's ``metadata.name`` with its fields filled in (default ``{namespace}-{instance}``)."""
    name = str((template.get("metadata") or {}).get("name") or "{namespace}-{instance}")
    for key in NAME_FIELDS:
        name = name.replace("{" + key + "}", fields.get(key, ""))
    if "{" in name or "}" in name:
        raise UsageError(
            f"the Application name {name!r} has an unknown field",
            hint="Fields: " + ", ".join("{" + key + "}" for key in NAME_FIELDS),
        )
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if len(name) > 63:
        name = f"{name[:54].rstrip('-')}-{yamlio.digest(name)[:8]}"
    return name


def _relative(plan: Plan, origin: GitOrigin, path: Path) -> str:
    """A path of the checkout as the repository names it."""
    return origin.path(path.resolve().relative_to(plan.stack.source.root.resolve()).as_posix())


def _git_source(origin: GitOrigin, path: str) -> dict[str, Any]:
    return {"repoURL": origin.repo_url, "targetRevision": origin.target, "path": path}


def _manifests_source(plan: Plan, origin: GitOrigin, path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    directory = path if path.is_dir() else path.parent
    names = [item.resolve().relative_to(directory.resolve()).as_posix() for item in manifest_files(path)]
    odd = [name for name in names if set(name) & _GLOB_CHARACTERS]
    if odd:
        return None, [
            Finding(
                "error",
                "argocd-file-name",
                f"Argo CD selects files by a glob, and {', '.join(odd)} holds a glob character",
                hint="Rename the file, or put the manifests in a kustomize directory.",
            )
        ]
    include = names[0] if len(names) == 1 else "{" + ",".join(names) + "}"
    source = _git_source(origin, _relative(plan, origin, directory))
    source["directory"] = {"recurse": True, "include": include}
    return source, []


def _split_set(value: str) -> list[str]:
    """``a=1,b=2`` -> ``["a=1", "b=2"]``, as Helm splits one ``--set``; ``\\,`` and ``{...}`` stay whole."""
    parts, current, depth, escaped = [], "", 0, False
    for char in value:
        if escaped:
            current, escaped = current + char, False
            continue
        if char == "\\":
            current, escaped = current + char, True
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current]


def helm_parameters(sets: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[Finding]]:
    """``(flag, "KEY=VALUE")`` pairs as Argo CD Helm parameters."""
    parameters, findings = [], []
    for flag, value in sets:
        if flag == "--set-json":
            findings.append(
                Finding(
                    "error",
                    "argocd-set-json",
                    f"--set-json {value}: Argo CD Helm parameters take strings only",
                    hint="Put the value in a values file of the checkout.",
                )
            )
            continue
        for item in _split_set(value):
            name, separator, setting = item.partition("=")
            if not separator or not name:
                findings.append(Finding("error", "argocd-set", f"{flag} {value}: expected KEY=VALUE"))
                continue
            parameters.append(
                {"name": name, "value": setting, **({"forceString": True} if flag == "--set-string" else {})}
            )
    return parameters, findings


def resolve_chart_version(chart: str, version: str, runner: Runner, helm: str = "helm") -> str:
    """The chart version a constraint (``^0.11``, ``~1.2``) resolves to now, so the Application pins what
    the plan rendered. A version (``v0``, ``v0.11.2``) is a tag to Helm and Argo CD alike, and is kept;
    so is a constraint Helm cannot resolve."""
    if not version or _VERSION.match(version):
        return version
    try:
        result = runner([helm, "show", "chart", chart, "--version", version], timeout=300)
    except CommandError:
        return version
    if not result.ok:
        return version
    try:
        data = yamlio.load_documents(result.stdout, f"{chart} Chart.yaml")
    except UsageError:
        return version
    found = str((data[0] if data else {}).get("version") or "")
    return found or version


def _helm_source(
    plan: Plan, origin: GitOrigin, unit: Unit, sets: list[str], runner: Runner
) -> tuple[dict[str, Any] | None, bool, list[Finding]]:
    """The chart's source, whether it needs the values ``ref`` source, and findings."""
    findings: list[Finding] = []
    chart = unit.chart
    if chart.startswith("oci://"):
        reference = chart.removeprefix("oci://")
        if "/" not in reference:
            return None, False, [Finding("error", "argocd-chart", f"{chart} names no chart")]
        repository, name = reference.rsplit("/", 1)
        version = resolve_chart_version(chart, unit.version, runner)
        if version != unit.version:
            findings.append(
                Finding(
                    "info",
                    "argocd-chart-pinned",
                    f"{name} version {unit.version} is {version} now; the Application pins {version}",
                )
            )
        if version and not _RELEASE.match(version):
            findings.append(
                Finding(
                    "warning",
                    "argocd-chart-tag",
                    f"{name} version {version} is a tag the registry can point at a newer build; Argo CD pulls it "
                    "again when it refreshes, so the chart can change with no new commit",
                    hint="Pin the chart to a release version (for a guide, the variable that sets its version, "
                    "with --var).",
                )
            )
        # Argo CD reads a repository URL without a scheme as an OCI Helm registry.
        source: dict[str, Any] = {"repoURL": repository, "chart": name, "targetRevision": version}
    elif "://" not in chart and Path(chart).is_absolute():
        source = _git_source(origin, _relative(plan, origin, Path(chart)))
    else:
        return (
            None,
            False,
            [
                Finding(
                    "error",
                    "argocd-chart",
                    f"{unit.step}: chart {chart} is neither an oci:// chart nor a directory of the checkout",
                    hint="Argo CD needs the chart's repository; deploy this guide with the direct backend.",
                )
            ],
        )
    parameters, found = helm_parameters([*unit.sets, *(("--set", value) for value in sets)])
    findings += found
    helm: dict[str, Any] = {"releaseName": unit.release, "skipTests": True}
    if unit.values:
        helm["valueFiles"] = [f"${VALUES_REF}/{_relative(plan, origin, path)}" for path in unit.values]
    if parameters:
        helm["parameters"] = parameters
    source["helm"] = helm
    return source, bool(unit.values), findings


def _name_pattern(name: str) -> str:
    """A patch target for an object a kustomize overlay may have renamed. Argo CD adds the patch to the
    top kustomization, where it matches the name an object has before that level's own ``namePrefix``:
    the rendered name less some leading ``-``-separated words. The target admits each of those, so it
    finds ``a-b-model`` but not ``a-b-model-2``. A ``nameSuffix`` is not covered."""
    words = name.split("-")
    tails = ["-".join(words[index:]) for index in range(len(words))]
    return "^(" + "|".join(re.escape(tail) for tail in tails) + ")$"


def replica_overrides(plan: Plan, unit_index: int) -> tuple[dict[str, Any], list[Finding]]:
    """``llmd scale`` counts of the objects one source renders, as that kustomize source's own overrides,
    so every sync renders them: ``kustomize.replicas`` for a Deployment or StatefulSet (matched by its
    rendered name), ``kustomize.patches`` for a LeaderWorkerSet. A DisaggregatedSet role, or an object a
    Helm chart or plain files render, cannot be kept that way; those counts are refused."""
    replicas, patches, findings = [], [], []
    unit = plan.units[unit_index]
    for workload, count in sorted(plan.options.overrides.items()):
        try:
            document, _ = overrides.find(plan.documents, workload)
        except UsageError:
            continue  # the plan already says the stack no longer renders it
        if plan.unit_of.get(object_ref(document)) != unit_index:
            continue
        name, _, role = workload.partition("/")
        if role or unit.kind != "kustomize" or document["kind"] not in ("Deployment", "StatefulSet", "LeaderWorkerSet"):
            findings.append(
                Finding(
                    "error",
                    "argocd-scale-unsupported",
                    f"Argo CD cannot keep the `llmd scale` count of {workload} ({unit.kind} {unit.step}): its next "
                    "sync would set the stack's own count again",
                    hint="Commit the count to the stack's files, or clear it with `llmd scale INSTANCE WORKLOAD=--`.",
                )
            )
        elif document["kind"] == "LeaderWorkerSet":
            target = {
                "group": api_group(document["apiVersion"]),
                "kind": "LeaderWorkerSet",
                "name": _name_pattern(name),
            }
            operation = [{"op": "add", "path": "/spec/replicas", "value": count}]
            patches.append({"target": target, "patch": json.dumps(operation, separators=(",", ":"))})
        else:
            replicas.append({"name": name, "count": count})
    found = {**({"replicas": replicas} if replicas else {}), **({"patches": patches} if patches else {})}
    return found, findings


def sources(plan: Plan, origin: GitOrigin, runner: Runner) -> tuple[list[dict[str, Any]], list[Finding]]:
    """The Application's sources for the plan's stack, in the order its steps apply them."""
    findings: list[Finding] = []
    found: list[dict[str, Any]] = []
    needs_values = False
    for index, unit in enumerate(plan.units):
        if unit.kind == "helm":
            source, values, more = _helm_source(plan, origin, unit, list(plan.options.sets), runner)
            needs_values = needs_values or values
            more += replica_overrides(plan, index)[1]
        elif unit.kind == "kustomize":
            source = _git_source(origin, _relative(plan, origin, unit.path))
            kept, more = replica_overrides(plan, index)
            if kept:
                source["kustomize"] = kept
            if uses_helm(unit.path):
                more.append(
                    Finding(
                        "info",
                        "argocd-kustomize-helm",
                        f"{source['path']} renders a Helm chart: Argo CD's kustomize build "
                        "options (argocd-cm kustomize.buildOptions) must include --enable-helm",
                    )
                )
        else:
            source, more = _manifests_source(plan, origin, unit.path)
            more += replica_overrides(plan, index)[1]
        findings += more
        if source is not None:
            found.append(source)
    if needs_values:
        found.append({"repoURL": origin.repo_url, "targetRevision": origin.target, "ref": VALUES_REF})
    return found, findings


def build(
    plan: Plan, template: dict[str, Any], origin: GitOrigin, runner: Runner
) -> tuple[dict[str, Any], list[Finding]]:
    """The Application that delivers ``plan``, and what the plan should say about it."""
    findings: list[Finding] = []
    application = copy.deepcopy(template)
    application["apiVersion"], application["kind"] = API_VERSION, "Application"
    metadata = application.setdefault("metadata", {}) or {}
    application["metadata"] = metadata
    spec = application.setdefault("spec", {}) or {}
    application["spec"] = spec
    for key in ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields"):
        metadata.pop(key, None)
    application.pop("status", None)
    application.pop("operation", None)
    if not spec.get("project"):
        raise UsageError(
            "the Argo CD Application template names no spec.project",
            hint="Name the Argo CD project the stack's Applications belong to.",
        )
    destination = dict(spec.get("destination") or {})
    if not destination.get("name") and not destination.get("server"):
        raise UsageError(
            "the Argo CD Application template names no destination cluster",
            hint="Set spec.destination.name (a cluster Argo CD knows) or spec.destination.server.",
        )
    written = destination.get("namespace")
    if written and written != plan.namespace and "{" not in str(written):
        findings.append(
            Finding(
                "warning",
                "argocd-destination-namespace",
                f"the template's destination namespace {written} is replaced by {plan.namespace}",
            )
        )
    destination["namespace"] = plan.namespace
    spec["destination"] = destination
    fields = {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "name": plan.stack.id.rstrip("/").rsplit("/", 1)[-1],
        "destination": str(destination.get("name") or urlsplit(str(destination.get("server", ""))).hostname or ""),
    }
    metadata["name"] = application_name(template, fields)
    metadata["namespace"] = metadata.get("namespace") or plan.namespace
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_LABEL: plan.instance}
    metadata["labels"] = {**(metadata.get("labels") or {}), **labels}
    metadata["annotations"] = {**(metadata.get("annotations") or {}), NAMESPACE_ANNOTATION: plan.namespace}
    # A teardown deletes the Application; with this finalizer Argo CD deletes what it deployed first.
    finalizers = list(metadata.get("finalizers") or [])
    metadata["finalizers"] = finalizers if FINALIZER in finalizers else [*finalizers, FINALIZER]
    if spec.get("source") or spec.get("sources"):
        findings.append(
            Finding(
                "info",
                "argocd-template-source",
                "the template's spec.source is replaced: llmd points the Application at the stack",
            )
        )
    spec.pop("source", None)
    spec.pop("sources", None)
    found, more = sources(plan, origin, runner)
    findings += more
    if len(found) == 1:
        spec["source"] = found[0]
    else:
        spec["sources"] = found
    return application, findings


def application_sources(application: dict[str, Any]) -> list[dict[str, Any]]:
    spec = application.get("spec") or {}
    return list(spec.get("sources") or ([spec["source"]] if spec.get("source") else []))


def automated(application: dict[str, Any]) -> bool:
    """Whether Argo CD syncs the Application on its own (``automated`` present and not disabled)."""
    policy = ((application.get("spec") or {}).get("syncPolicy") or {}).get("automated")
    return isinstance(policy, dict) and policy.get("enabled", True) is not False


def owner(document: dict[str, Any]) -> str:
    """The Application an object's tracking annotation names (``<app>:<group>/<kind>:<ns>/<name>``)."""
    value = str(((document.get("metadata") or {}).get("annotations") or {}).get(TRACKING_ANNOTATION) or "")
    return value.split(":", 1)[0] if ":" in value else ""


def tracked_by(document: dict[str, Any], application: dict[str, Any]) -> bool:
    """Whether Argo CD tracks an object as the Application's: the annotation names the Application
    as ``<name>``, or as ``<namespace>_<name>`` when it lives outside Argo CD's own namespace."""
    metadata = application.get("metadata") or {}
    name = owner(document)
    return bool(name) and name in (metadata.get("name"), f"{metadata.get('namespace')}_{metadata.get('name')}")
