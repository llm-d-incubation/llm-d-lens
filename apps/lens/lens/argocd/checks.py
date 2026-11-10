"""What a plan checks before Argo CD delivers it, and what an administrator checks of the install.

A plan for ``--backend argocd`` stays read-only. Besides the usual checks it asks:

* whether the cluster serves Argo CD Applications, and whether the user may read, create and patch them
  where the template puts them;
* whether the Application's Argo CD project admits it: the repository (``sourceRepos``), the destination
  (``destinations``), the namespace the Application lives in (``sourceNamespaces``, when it is not
  Argo CD's own), and every kind the stack renders (the resource allow and deny lists). The rules follow
  Argo CD's own: globs, ``!`` to deny, and no cluster-scoped kind unless one is listed;
* which objects Argo CD would apply that llmd would not: a stack's objects keep the namespace they were
  written for (Argo CD does not rewrite them), and the objects the namespace policy leaves out
  (cluster-scoped objects, SCC bindings) are in the Application's render too;
* whether an object of the stack already belongs to another Application.

Project rules are read from the AppProject when the user may read it; otherwise they are not checked,
and the plan says so.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from ..catalog import uses_helm
from ..errors import CommandError
from ..kube import Kube
from ..model import INSTANCE_LABEL, Finding, api_group, object_ref
from ..plan import Plan
from ..preflight import resources
from . import application as app


def _glob(pattern: str, value: str, *, separator: str = "") -> bool:
    """A glob as Argo CD matches it: with a ``separator``, ``*`` stops at it and ``**`` crosses it."""
    if pattern == "*":
        return True
    expression, index = "", 0
    stop = f"[^{re.escape(separator)}]" if separator else "."
    while index < len(pattern):
        if pattern.startswith("**", index):
            expression, index = expression + ".*", index + 2
        elif pattern[index] == "*":
            expression, index = expression + stop + "*", index + 1
        elif pattern[index] == "?":
            expression, index = expression + stop, index + 1
        else:
            expression, index = expression + re.escape(pattern[index]), index + 1
    return re.fullmatch(expression, value) is not None


def normalize_url(url: str) -> str:
    """A repository URL as Argo CD compares it: lower case, without ``.git``, SSH as a path."""
    url = url.strip().lower()
    if re.match(r"^[\w.-]+@[\w.-]+:", url) and not url.startswith("ssh://"):
        url = "ssh://" + url.replace(":", "/", 1)
    return url.removesuffix(".git").removeprefix("ssh://")


def source_permitted(patterns: list[str], url: str) -> bool:
    """``sourceRepos``: a match admits, and a ``!`` pattern the URL does not match refuses it."""
    url = normalize_url(url)
    matched = False
    for pattern in patterns:
        deny = pattern.startswith("!")
        normalized = normalize_url(pattern.removeprefix("!"))
        hit = _glob(normalized, url, separator="/")
        if deny and hit:
            return False
        if not deny and hit:
            matched = True
    return matched


def _negatable(pattern: str, value: str) -> bool:
    if pattern.startswith("!"):
        return not _glob(pattern[1:], value)
    return _glob(pattern, value)


def destination_permitted(destinations: list[dict[str, Any]], destination: dict[str, str]) -> bool | None:
    """``destinations``: one entry must match the cluster (by server or name) and the namespace; a ``!``
    entry the destination does not match refuses it. Argo CD knows both the server and the name of the
    cluster, a template often only one: an entry for the other one cannot be judged, so the answer is
    None when only such entries could admit it."""
    matched, unknown = False, False
    namespace = destination.get("namespace", "")
    for item in destinations:
        item_name, item_server = str(item.get("name") or ""), str(item.get("server") or "")
        item_namespace = str(item.get("namespace") or "")
        name = bool(destination.get("name")) and _negatable(item_name, destination["name"])
        server = bool(destination.get("server")) and _negatable(item_server, destination["server"])
        in_namespace = _negatable(item_namespace, namespace)
        # A ``!`` entry refuses only a cluster field the template names; the other one is not known here.
        denied_cluster = (bool(destination.get("name")) and not name and item_name.startswith("!")) or (
            bool(destination.get("server")) and not server and item_server.startswith("!") and in_namespace
        )
        denied_namespace = not in_namespace and item_namespace.startswith("!") and server
        if (server or name) and in_namespace:
            matched = True
        elif denied_cluster or denied_namespace:
            return False
        elif in_namespace and (
            (item_server and not destination.get("server")) or (item_name and not destination.get("name"))
        ):
            unknown = True
    return True if matched else (None if unknown else False)


def _in_list(items: list[dict[str, Any]], group: str, kind: str, name: str = "") -> bool:
    for item in items:
        if not fnmatch.fnmatchcase(kind, str(item.get("kind", ""))):
            continue
        if not fnmatch.fnmatchcase(group, str(item.get("group", ""))):
            continue
        if not item.get("name") or fnmatch.fnmatchcase(name, str(item["name"])):
            return True
    return False


def kind_permitted(spec: dict[str, Any], group: str, kind: str, name: str, *, namespaced: bool) -> bool:
    """The project's resource lists: every namespaced kind unless a whitelist names others or a blacklist
    names it; no cluster-scoped kind unless the cluster whitelist names it."""
    if namespaced:
        whitelist = spec.get("namespaceResourceWhitelist")
        blacklist = spec.get("namespaceResourceBlacklist") or []
        allowed = whitelist is None or (bool(whitelist) and _in_list(whitelist, group, kind))
        return allowed and not (blacklist and _in_list(blacklist, group, kind))
    whitelist = spec.get("clusterResourceWhitelist") or []
    blacklist = spec.get("clusterResourceBlacklist") or []
    return _in_list(whitelist, group, kind, name) and not _in_list(blacklist, group, kind, name)


def app_namespace_permitted(spec: dict[str, Any], namespace: str, argocd_namespace: str) -> bool:
    """``sourceNamespaces``: an Application outside Argo CD's own namespace must be listed (a glob, or
    a regular expression between slashes)."""
    if namespace == argocd_namespace:
        return True
    for pattern in spec.get("sourceNamespaces") or []:
        pattern = str(pattern)
        if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
            if re.search(pattern[1:-1], namespace):
                return True
        elif _glob(pattern, namespace):
            return True
    return False


def _rendered_by_argocd(plan: Plan) -> list[tuple[dict[str, Any], bool]]:
    """(object, namespaced) for everything the Application's render holds: what llmd would apply, and
    what the namespace policy leaves out."""
    found = [(document, True) for document in plan.documents]
    for part in plan.policy.left_out:
        document = part["document"]
        if document["kind"] != "Namespace":
            found.append((document, part["reason"] != "cluster-scoped"))
    return found


def check_render(plan: Plan) -> list[Finding]:
    """What Argo CD would apply differently from llmd: objects written for another namespace, and the
    objects llmd leaves out. These need no cluster."""
    findings = []
    moved = plan.policy.moved
    if moved:
        listed = ", ".join(sorted(moved)[:4]) + (f" and {len(moved) - 4} more" if len(moved) > 4 else "")
        written = ", ".join(sorted(set(moved.values())))
        findings.append(
            Finding(
                "error",
                "argocd-namespace-written",
                f"{len(moved)} object(s) name namespace {written} ({listed}); Argo CD applies them there, "
                f"not in {plan.namespace}",
                hint="Point the stack at an overlay written for this namespace, or stand it up with "
                "--backend direct, which moves them.",
            )
        )
    elif plan.policy.rewrites:
        findings.append(
            Finding(
                "error",
                "argocd-namespace-references",
                f"{plan.policy.rewrites} reference(s) in the stack name another namespace; llmd rewrites them, "
                "Argo CD does not",
                hint="Point the stack at an overlay written for this namespace, or use --backend direct.",
            )
        )
    for part in plan.policy.left_out:
        document, reason = part["document"], part["reason"]
        ref = object_ref(document)
        if reason == "namespace":
            message = f"{ref} is in the stack's files; Argo CD applies it unless the project refuses Namespaces"
            findings.append(Finding("warning", "argocd-namespace-object", message, object=ref))
        else:
            findings.append(
                Finding(
                    "warning",
                    "argocd-applies-admin-part",
                    f"{ref} ({reason}) is in the stack's files, so the Application applies it too; the project "
                    "decides whether it may",
                    object=ref,
                )
            )
    return findings


def _project(kube: Kube, name: str, namespace: str) -> tuple[dict[str, Any] | None, str]:
    """The AppProject, and why it could not be read."""
    try:
        found = kube.get(app.PROJECT_RESOURCE, name, namespace)
    except CommandError as error:
        return None, "not allowed to read it" if "orbidden" in error.message else error.message.splitlines()[0]
    return found, "" if found else "not found"


def check_project(
    plan: Plan, application: dict[str, Any], project: dict[str, Any] | None, *, argocd_namespace: str
) -> list[Finding]:
    """The Application's project admits its sources, destination, namespace, and every kind it renders."""
    spec = (project or {}).get("spec") or {}
    name = application["spec"]["project"]
    findings = []
    for source in app.application_sources(application):
        url = source["repoURL"]
        if not source_permitted(list(spec.get("sourceRepos") or []), url):
            findings.append(
                Finding(
                    "error",
                    "argocd-source-refused",
                    f"Argo CD project {name} does not list {url} in its sourceRepos",
                    hint=f"An administrator adds it to AppProject {name}; `llmd admin argocd check` writes the change.",
                )
            )
    destination = application["spec"]["destination"]
    admitted = destination_permitted(list(spec.get("destinations") or []), destination)
    where = destination.get("name") or destination.get("server")
    if admitted is False:
        findings.append(
            Finding(
                "error",
                "argocd-destination-refused",
                f"Argo CD project {name} does not admit namespace {destination['namespace']} on cluster {where}",
                hint=f"An administrator adds it to AppProject {name}'s destinations.",
            )
        )
    elif admitted is None:
        findings.append(
            Finding(
                "warning",
                "argocd-destination-unknown",
                f"Argo CD project {name} names its clusters by the field the template leaves out; whether it "
                f"admits namespace {destination['namespace']} on cluster {where} is not checked",
                hint="Name the cluster in the template the way the project does (spec.destination.server or name).",
            )
        )
    namespace = application["metadata"]["namespace"]
    if not app_namespace_permitted(spec, namespace, argocd_namespace):
        findings.append(
            Finding(
                "error",
                "argocd-app-namespace-refused",
                f"Argo CD project {name} does not list namespace {namespace} in its sourceNamespaces",
                hint=f"Put the Application in {argocd_namespace} (the template's metadata.namespace), or have an "
                f"administrator add {namespace} to the project and to Argo CD's application namespaces.",
            )
        )
    refused = sorted(
        {
            object_ref(document)
            for document, namespaced in _rendered_by_argocd(plan)
            if not kind_permitted(
                spec,
                api_group(document["apiVersion"]),
                document["kind"],
                document["metadata"]["name"],
                namespaced=namespaced,
            )
        }
    )
    if refused:
        listed = ", ".join(refused[:6]) + (f" and {len(refused) - 6} more" if len(refused) > 6 else "")
        findings.append(
            Finding(
                "error",
                "argocd-kind-refused",
                f"Argo CD project {name} does not admit {listed}",
                hint="A project admits a cluster-scoped kind only when its clusterResourceWhitelist names it.",
            )
        )
    return findings


def preflight(kube: Kube, plan: Plan, application: dict[str, Any], *, argocd_namespace: str = "") -> None:
    """Read-only checks of the Application against the cluster; findings go to the plan."""
    findings = plan.findings
    metadata = application["metadata"]
    if kube.resource(app.API_VERSION, "Application") is None:
        findings.append(
            Finding(
                "error",
                "argocd-missing",
                f"the cluster does not serve Argo CD Applications ({app.API_VERSION})",
                hint="Install Argo CD, or stand the stack up with --backend direct.",
            )
        )
        return
    namespace = metadata["namespace"]
    checks = [(verb, "argoproj.io", "applications", namespace) for verb in ("get", "create", "patch")]
    denied = [verb for (verb, *_), allowed in zip(checks, kube.access_reviews(checks), strict=True) if allowed is False]
    if denied:
        findings.append(
            Finding(
                "error",
                "argocd-permission-denied",
                f"you cannot {', '.join(denied)} Argo CD Applications in namespace {namespace}",
                hint="Ask an Argo CD administrator for an Application role there, or for the change to be applied.",
            )
        )
    plan.cluster["argocd"] = {"application": f"{namespace}/{metadata['name']}"}
    try:
        live = kube.get(app.RESOURCE, metadata["name"], namespace)
    except CommandError:
        live = None
    if live is not None:
        labels = (live.get("metadata") or {}).get("labels") or {}
        if labels.get(INSTANCE_LABEL) != plan.instance:
            findings.append(
                Finding(
                    "error",
                    "argocd-application-exists",
                    f"Application {namespace}/{metadata['name']} exists and is not instance {plan.instance}'s",
                    hint="Record it with `llmd import --backend argocd`, or name another Application in the template.",
                )
            )
    if not argocd_namespace:
        plan.cluster["argocd"]["project"] = "not checked"
        findings.append(
            Finding(
                "warning",
                "argocd-project-unread",
                f"the namespace Argo CD runs in is not known, so AppProject {application['spec']['project']} is "
                "not checked",
                hint="Pass --argocd-namespace, or set metadata.namespace in the template.",
            )
        )
        _check_owners(kube, plan, application)
        return
    project, problem = _project(kube, application["spec"]["project"], argocd_namespace)
    if project is None:
        plan.cluster["argocd"]["project"] = problem
        findings.append(
            Finding(
                "warning",
                "argocd-project-unread",
                f"AppProject {application['spec']['project']} in {argocd_namespace}: {problem}; its rules are "
                "not checked",
                hint="Pass --argocd-namespace if Argo CD runs elsewhere; Argo CD refuses the sync if a rule fails.",
            )
        )
    else:
        plan.cluster["argocd"]["project"] = "checked"
        findings.extend(check_project(plan, application, project, argocd_namespace=argocd_namespace))
    _check_owners(kube, plan, application)


def _check_owners(kube: Kube, plan: Plan, application: dict[str, Any]) -> None:
    """An object the stack renders that Argo CD already tracks for another Application."""
    for document, resource in zip(plan.documents, resources(kube, plan), strict=True):
        try:
            live = kube.get(resource, document["metadata"]["name"], plan.namespace)
        except CommandError:
            continue
        owner = app.owner(live or {})
        if owner and not app.tracked_by(live, application):
            ref = object_ref(document)
            plan.findings.append(
                Finding(
                    "error",
                    "argocd-shared-object",
                    f"{ref} already belongs to Argo CD Application {owner}",
                    object=ref,
                    hint="Two Applications would fight over it; tear the other one down, or record that one with "
                    "`llmd import --backend argocd`.",
                )
            )


# -- the Argo CD install (`llmd admin argocd check`) --------------------------------------------------------
#: Argo CD's settings ConfigMaps, in the namespace it runs in.
SETTINGS_CONFIGMAP = "argocd-cm"
PARAMS_CONFIGMAP = "argocd-cmd-params-cm"


def check_install(kube: Kube, namespace: str, plan: Plan | None = None, application: dict[str, Any] | None = None):
    """Read-only checks of an Argo CD install, and (with a plan and its Application) the AppProject change an
    administrator would make so it admits the stack. Returns ``(report, findings, project_change)``."""
    findings: list[Finding] = []
    report: dict[str, Any] = {"namespace": namespace}
    if kube.resource(app.API_VERSION, "Application") is None:
        findings.append(
            Finding("error", "argocd-missing", f"the cluster does not serve {app.API_VERSION} Applications")
        )
        return report, findings, None
    settings, problem = _configmap(kube, SETTINGS_CONFIGMAP, namespace)
    params, _ = _configmap(kube, PARAMS_CONFIGMAP, namespace)
    if settings is None:
        report["settings"] = problem
        findings.append(
            Finding(
                "warning",
                "argocd-settings-unread",
                f"ConfigMap {namespace}/{SETTINGS_CONFIGMAP}: {problem}; Argo CD's build options are not checked",
                hint="Pass -n with the namespace Argo CD runs in, or run this as a user who may read it.",
            )
        )
    else:
        options = str(settings.get("kustomize.buildOptions") or "")
        tracking = str(settings.get("application.resourceTrackingMethod") or "annotation")
        report.update({"kustomizeBuildOptions": options, "trackingMethod": tracking})
        renders_charts = plan is not None and any(
            unit.kind == "kustomize" and unit.path is not None and uses_helm(unit.path) for unit in plan.units
        )
        if "--enable-helm" not in options.split():
            severity = "error" if renders_charts else "info"
            findings.append(
                Finding(
                    severity,
                    "argocd-kustomize-helm-off",
                    "Argo CD's kustomize.buildOptions lack --enable-helm; a kustomization with helmCharts fails to "
                    "render" + (" (this stack has one)" if renders_charts else ""),
                    hint=f"An administrator adds --enable-helm to kustomize.buildOptions in {SETTINGS_CONFIGMAP}.",
                )
            )
        if tracking == "label":
            findings.append(
                Finding(
                    "warning",
                    "argocd-tracking-label",
                    "Argo CD tracks objects by label; llmd reads the tracking annotation, so it cannot tell which "
                    "Application owns an object",
                    hint="Annotation tracking (Argo CD's default) or annotation+label lets llmd check owners.",
                )
            )
    namespaces = [item.strip() for item in str((params or {}).get("application.namespaces") or "").split(",") if item]
    report["applicationNamespaces"] = namespaces
    if application is not None and plan is not None:
        where = application["metadata"]["namespace"]
        if where != namespace and not any(_glob(item, where) for item in namespaces):
            findings.append(
                Finding(
                    "error",
                    "argocd-app-namespace-off",
                    f"Argo CD reconciles no Applications in {where}: {PARAMS_CONFIGMAP} application.namespaces "
                    "does not list it",
                    hint=f"Keep the Application in {namespace}, or have an administrator list {where} there and in "
                    "the project's sourceNamespaces.",
                )
            )
        project, problem = _project(kube, application["spec"]["project"], namespace)
        report["project"] = application["spec"]["project"]
        if project is None:
            findings.append(
                Finding("warning", "argocd-project-unread", f"AppProject {namespace}/{report['project']}: {problem}")
            )
            return report, findings, None
        found = check_project(plan, application, project, argocd_namespace=namespace)
        findings.extend(found)
        if any(finding.severity == "error" for finding in found):
            return report, findings, project_change(plan, application, project, argocd_namespace=namespace)
    return report, findings, None


def _configmap(kube: Kube, name: str, namespace: str) -> tuple[dict[str, Any] | None, str]:
    try:
        found = kube.get("configmaps", name, namespace)
    except CommandError as error:
        return None, "not allowed to read it" if "orbidden" in error.message else error.message.splitlines()[0]
    return ((found or {}).get("data") or {}) if found else None, "" if found else "not found"


def project_change(
    plan: Plan, application: dict[str, Any], project: dict[str, Any], *, argocd_namespace: str
) -> dict[str, Any]:
    """The AppProject with what it lacks to admit the Application: its repositories, the destination, the
    Application's namespace, and the kinds the stack renders. For an administrator to review and apply;
    llmd never applies it."""
    spec = dict(project.get("spec") or {})
    repos = list(spec.get("sourceRepos") or [])
    for source in app.application_sources(application):
        if not source_permitted(repos, source["repoURL"]):
            repos.append(source["repoURL"])
    destinations = list(spec.get("destinations") or [])
    destination = application["spec"]["destination"]
    if destination_permitted(destinations, destination) is False:
        entry = {key: destination[key] for key in ("name", "server") if destination.get(key)}
        destinations.append({**entry, "namespace": destination["namespace"]})
    changed = {**spec, "sourceRepos": repos, "destinations": destinations}
    where = application["metadata"]["namespace"]
    if not app_namespace_permitted(spec, where, argocd_namespace):
        changed["sourceNamespaces"] = [*(spec.get("sourceNamespaces") or []), where]
    for document, namespaced in _rendered_by_argocd(plan):
        group, kind, name = api_group(document["apiVersion"]), document["kind"], document["metadata"]["name"]
        if kind_permitted(changed, group, kind, name, namespaced=namespaced):
            continue
        key = "namespaceResourceWhitelist" if namespaced else "clusterResourceWhitelist"
        if key == "namespaceResourceWhitelist" and changed.get(key) is None:
            continue  # every namespaced kind is admitted; a blacklist refuses it, which is the project's choice
        entry = {"group": group, "kind": kind}
        changed[key] = [*(changed.get(key) or []), entry]
    metadata = {key: value for key, value in (project.get("metadata") or {}).items() if key in ("name", "namespace")}
    return {"apiVersion": app.API_VERSION, "kind": "AppProject", "metadata": metadata, "spec": changed}
