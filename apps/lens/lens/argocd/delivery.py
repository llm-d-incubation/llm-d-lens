"""Deliver an instance through Argo CD: check it, stand it up, follow it, tear it down.

The instance's record stays where it is for the direct backend (``lens-instance-<name>`` in the target
namespace) and says ``delivery: argocd`` with the Application it names. It is written before the
Application, so an interrupted standup can still be torn down.

* **standup** applies the Application with server-side apply. When the Application has no automated
  sync, llmd starts one sync at the commit the plan rendered (an ``operation`` on the Application, as
  ``argocd app sync`` does) and Argo CD applies and prunes; with automated sync, Argo CD starts it.
  Argo CD applies the stack's files as they are, with its own Helm and kustomize, so the record's
  objects are what Argo CD reports it manages, read back after the sync.
* **status** adds the Application's sync status, health, revision and last operation to the objects'
  health, and an instance is Ready only once the Application is synced to the reviewed revision.
* **teardown** deletes the Application. Its resources finalizer makes Argo CD delete what it deployed,
  except objects marked ``argocd.argoproj.io/sync-options: Delete=false``; a claim without that mark is
  deleted too, so a teardown refuses unless ``--delete-volumes`` is passed. ``--forget`` removes the
  finalizer first, so every object is left running.
* An instance may also record an Application someone else made (``createdBy: observed``); llmd then
  only reads it, and a teardown of it can only ``--forget``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .. import inventory, yamlio
from ..errors import ConflictError, NotFoundError, NotReadyError, PolicyError
from ..kube import Kube
from ..model import KEPT_KINDS
from ..plan import Plan
from ..preflight import preflight
from ..process import Runner
from . import application as app
from . import checks, origin

BACKEND = "argocd"
#: Who made the Application: llmd (standup), or someone else (import).
CREATED, OBSERVED = "llmd", "observed"


def is_argocd(record: dict[str, Any] | None) -> bool:
    return ((record or {}).get("delivery") or {}).get("backend") == BACKEND


def application_ref(record: dict[str, Any]) -> tuple[str, str]:
    """(namespace, name) of the instance's Application."""
    delivery = record.get("delivery") or {}
    return str(delivery.get("namespace") or ""), str(delivery.get("application") or "")


def read_application(kube: Kube, record: dict[str, Any]) -> dict[str, Any] | None:
    namespace, name = application_ref(record)
    return kube.get(app.RESOURCE, name, namespace)


def require_application(kube: Kube, record: dict[str, Any]) -> dict[str, Any]:
    found = read_application(kube, record)
    if found is None:
        namespace, name = application_ref(record)
        raise NotFoundError(
            f"Argo CD Application {namespace}/{name} of instance {record['instance']} is gone",
            hint=f"Stand it up again, or stop managing it with `llmd teardown {record['instance']} --forget`.",
        )
    return found


def _revisions(application: dict[str, Any]) -> list[str]:
    return [str(source.get("targetRevision") or "") for source in app.application_sources(application)]


def sync_operation(application: dict[str, Any], *, actor: str, prune: bool = True) -> dict[str, Any]:
    """The ``operation`` that starts one sync of every source at the revision it names, with the
    Application's own sync options (a sync started this way does not read ``spec.syncPolicy``)."""
    options = list(((application.get("spec") or {}).get("syncPolicy") or {}).get("syncOptions") or [])
    sources = app.application_sources(application)
    sync: dict[str, Any] = {"prune": prune, "syncStrategy": {"hook": {}}}
    if len(sources) > 1:
        sync["revisions"] = _revisions(application)
    else:
        sync["revision"] = _revisions(application)[0] if sources else ""
    if options:
        sync["syncOptions"] = options
    return {"initiatedBy": {"username": actor or "llmd"}, "info": [{"name": "by", "value": "llmd"}], "sync": sync}


def state(application: dict[str, Any] | None, *, revision: str = "") -> dict[str, Any]:
    """The Application in one state, from Argo CD's status: Missing, Failed, Syncing, OutOfSync, Synced;
    ``Synced`` only once the synced revision is the one the instance was planned at."""
    if application is None:
        return {"state": "Missing", "message": "the Argo CD Application does not exist"}
    status = application.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    operation = status.get("operationState") or {}
    conditions = [
        {"type": str(item.get("type", "")), "message": str(item.get("message") or "")[:300]}
        for item in status.get("conditions") or []
        if isinstance(item, dict)
    ]
    synced = [str(item) for item in (sync.get("revisions") or [sync.get("revision") or ""]) if item is not None]
    resources = status.get("resources") or []
    summary = {
        "sync": sync.get("status") or "Unknown",
        "revision": synced[0] if len(synced) == 1 else synced,
        "health": health.get("status") or "Unknown",
        "operation": str(operation.get("phase") or ""),
        "message": str(operation.get("message") or health.get("message") or "")[:300],
        "conditions": conditions,
        "resources": {
            "total": len(resources),
            "outOfSync": sum(1 for item in resources if item.get("status") == "OutOfSync"),
            "degraded": sum(1 for item in resources if (item.get("health") or {}).get("status") == "Degraded"),
        },
    }
    errors = [item for item in conditions if item["type"].endswith("Error")]
    if errors or summary["operation"] in ("Failed", "Error"):
        summary["state"] = "Failed"
        summary["message"] = errors[0]["message"] if errors else summary["message"]
    elif summary["operation"] in ("Running", "Terminating") or application.get("operation"):
        summary["state"] = "Syncing"
    elif summary["sync"] == "Synced" and (not revision or revision in synced):
        summary["state"] = "Synced"
    else:
        summary["state"] = "Syncing" if app.automated(application) else "OutOfSync"
    return summary


#: An instance's health while its Application is not yet synced to the planned revision.
HEALTH = {"Missing": "Degraded", "Failed": "Failed", "Syncing": "Progressing", "OutOfSync": "Progressing"}


def managed_objects(kube: Kube, application: dict[str, Any], namespace: str) -> list[dict[str, str]]:
    """The objects Argo CD reports the Application manages in ``namespace``, as record entries with UIDs."""
    entries = []
    for item in (application.get("status") or {}).get("resources") or []:
        if item.get("namespace") != namespace or item.get("hook"):
            continue
        api_version = f"{item['group']}/{item['version']}" if item.get("group") else str(item.get("version") or "v1")
        document = {"apiVersion": api_version, "kind": item["kind"], "metadata": {"name": item["name"]}}
        resource = kube.resource_name(document)
        live = kube.get(resource, item["name"], namespace)
        entries.append(inventory.object_entry(document, resource, ((live or {}).get("metadata") or {}).get("uid", "")))
    return entries


def wait_synced(
    kube: Kube, record: dict[str, Any], *, timeout: float, interval: float = 5, emit=None
) -> dict[str, Any]:
    """Until the Application is synced to the instance's revision (or fails)."""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        current = state(read_application(kube, record), revision=(record.get("delivery") or {}).get("revision", ""))
        line = f"{current['state']}: sync {current.get('sync')}, health {current.get('health')}"
        if emit and line != last:
            emit(line)
            last = line
        if current["state"] == "Synced":
            return current
        if current["state"] in ("Failed", "Missing"):
            raise NotReadyError(
                f"Argo CD could not sync instance {record['instance']}: {current.get('message') or current['state']}",
                hint="See the Application in Argo CD; fix the stack or the project, then stand it up again.",
            )
        if time.monotonic() >= deadline:
            raise NotReadyError(
                f"Argo CD did not sync instance {record['instance']} in {timeout:.0f}s ({line})",
                hint=f"Argo CD keeps going; follow it with `llmd status {record['instance']}`.",
            )
        time.sleep(interval)
        kube.forget()


@dataclass(frozen=True)
class Settings:
    """How a command delivers through Argo CD (``--argocd-*``)."""

    #: The Application manifest the team keeps (``--argocd-app FILE``).
    template: dict[str, Any]
    #: The namespace Argo CD runs in, where its AppProjects are (default: the template's namespace).
    namespace: str = ""
    #: ``targetRevision`` to write (default: the commit the plan rendered).
    revision: str = ""
    #: The Git remote of a local checkout that Argo CD fetches (default ``origin``).
    remote: str = "origin"


def attach(plan: Plan, settings: Settings, runner: Runner) -> dict[str, Any] | None:
    """Build the Application for ``plan`` and add what Argo CD changes about it to the plan's findings
    (no cluster needed). The plan's digest then covers the Application."""
    found, findings = origin.resolve(
        plan.stack, runner, paths=origin.stack_paths(plan), remote=settings.remote, target=settings.revision
    )
    plan.policy.findings.extend(findings)
    plan.policy.findings.extend(checks.check_render(plan))
    if found is None:
        return None
    application, more = app.build(plan, settings.template, found, runner)
    plan.policy.findings.extend(more)
    plan.delivery = application
    return application


def check(kube: Kube, plan: Plan, settings: Settings, runner: Runner, *, existing: str = "refuse") -> None:
    """The cluster checks of a plan Argo CD delivers: the namespace, prerequisites and owners of the
    objects (llmd applies none of them, so it needs only to read them), then the Application."""
    built = attach(plan, settings, runner)
    owns = (lambda live: app.tracked_by(live, built)) if built else None
    preflight(kube, plan, existing=existing, applies=False, owns=owns)
    if built is not None:
        checks.preflight(kube, plan, built, argocd_namespace=settings.namespace or built["metadata"]["namespace"])


def _refuse(plan: Plan, expected_digest: str) -> None:
    if plan.blocking:
        raise PolicyError(
            f"the plan has {len(plan.blocking)} blocking finding(s)",
            details=[finding.as_dict() for finding in plan.blocking],
            hint="Run `llmd plan --backend argocd` to see them.",
        )
    if expected_digest and not yamlio.digest_matches(expected_digest, plan.digest):
        raise ConflictError(
            "the stack, the Application or the cluster changed since the plan was reviewed",
            hint=f"Review `llmd plan` again; it now has digest {plan.digest[: yamlio.SHORT_DIGEST]}.",
        )


def _record(plan: Plan, application: dict[str, Any], previous: dict[str, Any] | None, *, actor: str, mode: str):
    metadata = application["metadata"]
    return {
        "schema": inventory.SCHEMA,
        "instance": plan.instance,
        "namespace": plan.namespace,
        "management": {"mode": inventory.MANAGED},
        "delivery": {
            "backend": BACKEND,
            "application": metadata["name"],
            "namespace": metadata["namespace"],
            "project": application["spec"]["project"],
            "revision": plan.stack.source.revision,
            "createdBy": mode,
        },
        "phase": "Applying",
        "stack": plan.stack_record(),
        **({"previousStack": previous["previousStack"]} if (previous or {}).get("previousStack") else {}),
        "planDigest": plan.digest,
        **({"overrides": dict(plan.options.overrides)} if plan.options.overrides else {}),
        "objects": list((previous or {}).get("objects") or []),
        "createdAt": (previous or {}).get("createdAt") or inventory.now(),
        "updatedBy": actor,
    }


def template_of(application: dict[str, Any]) -> dict[str, Any]:
    """The Application a later command builds from again: the live one, without what llmd sets (its
    sources, status, operation, and what the server fills in). ``scale`` and ``rollouts`` start from it,
    so they need no ``--argocd-app`` and keep any change an administrator made to its project or policy."""
    metadata = {
        key: value
        for key, value in (application.get("metadata") or {}).items()
        if key in ("name", "namespace", "labels", "annotations", "finalizers")
    }
    spec = {key: value for key, value in (application.get("spec") or {}).items() if key not in ("source", "sources")}
    return {"apiVersion": app.API_VERSION, "kind": "Application", "metadata": metadata, "spec": spec}


def settings_for(kube: Kube, record: dict[str, Any], **changes: Any) -> Settings:
    """Settings that rebuild an instance's Application from the live one (``template_of``)."""
    live = require_application(kube, record)
    return Settings(template=template_of(live), **changes)


def submit(kube: Kube, record: dict[str, Any], application: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Apply the Application and, without automated sync, start one sync of it."""
    body = dict(application)
    if not app.automated(application):
        body["operation"] = sync_operation(application, actor=actor)
    kube.progress(
        f"applying Argo CD Application {application['metadata']['namespace']}/{application['metadata']['name']}"
    )
    applied = kube.apply([body], application["metadata"]["namespace"], force_conflicts=True)
    return applied[0] if applied else body


def standup(
    kube: Kube,
    plan: Plan,
    application: dict[str, Any],
    *,
    expected_digest: str = "",
    actor: str = "",
    wait: bool = True,
    timeout: float = 600,
    emit=None,
) -> dict[str, Any]:
    """Record the instance, submit its Application, and (``wait``) record what Argo CD then manages."""
    _refuse(plan, expected_digest)
    previous = inventory.read(kube, plan.namespace, plan.instance)
    if previous and not is_argocd(previous):
        raise ConflictError(
            f"instance {plan.instance} is delivered directly, not by Argo CD",
            hint="Tear it down first, or stand this stack up under another --name.",
        )
    if previous and (previous.get("delivery") or {}).get("createdBy") == OBSERVED:
        raise PolicyError(
            f"instance {plan.instance} records an Application someone else made; llmd does not change it",
            hint="Change it where it is kept (its repository, or the app-of-apps that makes it).",
        )
    if previous and (previous.get("stack") or {}).get("id") != plan.stack.id and not plan.options.switch_stack:
        raise ConflictError(
            f"instance {plan.instance} runs stack {previous['stack']['id']}, not {plan.stack.id}",
            hint="Pass --switch-stack to move it to this stack, or stand this stack up under another --name.",
        )
    record = _record(plan, application, previous, actor=actor, mode=CREATED)
    inventory.write(kube, record)
    submit(kube, record, application, actor=actor)
    result = {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "backend": BACKEND,
        "application": f"{application['metadata']['namespace']}/{application['metadata']['name']}",
        "revision": plan.stack.source.revision,
        "digest": plan.digest,
        "sync": "automated" if app.automated(application) else "started",
    }
    if not wait:
        return result
    result["argocd"] = wait_synced(kube, record, timeout=timeout, emit=emit)
    live = require_application(kube, record)
    record["objects"] = managed_objects(kube, live, plan.namespace)
    record["phase"] = "Applied"
    inventory.write(kube, record)
    result["objects"] = len(record["objects"])
    return result


def refresh_objects(kube: Kube, record: dict[str, Any]) -> dict[str, Any]:
    """The record with the objects Argo CD now reports, once its Application is synced (``Applied``)."""
    live = read_application(kube, record)
    current = state(live, revision=(record.get("delivery") or {}).get("revision", ""))
    if live is None or current["state"] != "Synced":
        return record
    objects = managed_objects(kube, live, record["namespace"])
    if objects == record.get("objects") and record.get("phase") == "Applied":
        return record
    updated = {
        **record,
        "objects": objects,
        "phase": "Applied" if record.get("phase") == "Applying" else record["phase"],
    }
    inventory.write(kube, updated)
    return updated


def status(kube: Kube, record: dict[str, Any]) -> dict[str, Any]:
    """The Application's part of ``llmd status``."""
    live = read_application(kube, record)
    namespace, name = application_ref(record)
    found = state(live, revision=(record.get("delivery") or {}).get("revision", ""))
    return {
        "application": f"{namespace}/{name}",
        "project": (record.get("delivery") or {}).get("project", ""),
        "createdBy": (record.get("delivery") or {}).get("createdBy", CREATED),
        "plannedRevision": (record.get("delivery") or {}).get("revision", ""),
        "automated": bool(live) and app.automated(live),
        **found,
    }


def teardown(
    kube: Kube, record: dict[str, Any], *, delete_volumes: bool = False, forget: bool = False
) -> dict[str, Any]:
    """Delete the Application: Argo CD deletes what it deployed (``forget``: removes its finalizer first,
    so nothing is deleted), then the record."""
    namespace, name = application_ref(record)
    instance, target = record["instance"], record["namespace"]
    observed = (record.get("delivery") or {}).get("createdBy") == OBSERVED
    live = read_application(kube, record)
    if observed and not forget:
        raise PolicyError(
            f"instance {instance} records an Application someone else made; llmd does not delete it",
            hint=f"Stop managing it with `llmd teardown {instance} --forget`, or delete the Application where it "
            "is kept.",
        )
    claims = [item for item in record.get("objects") or [] if item["kind"] in KEPT_KINDS]
    if claims and not delete_volumes and not forget and live is not None:
        unkept = [item for item in claims if not _kept_by_argocd(kube, item, target)]
        if unkept:
            listed = ", ".join(f"{item['kind']}/{item['name']}" for item in unkept)
            raise PolicyError(
                f"deleting Application {name} would delete {listed}: Argo CD deletes every object it deployed",
                hint="Pass --delete-volumes, or mark the claim in the stack with "
                f"`{app.SYNC_OPTIONS_ANNOTATION}: Delete=false` and stand it up again.",
            )
    if live is not None and not observed:
        reviews = kube.access_reviews([("delete", "argoproj.io", "applications", namespace)])
        if reviews and reviews[0] is False:
            raise PolicyError(f"you cannot delete Argo CD Applications in {namespace}; nothing was removed")
    if live is not None and forget and not observed:
        finalizers = [item for item in (live["metadata"].get("finalizers") or []) if item != app.FINALIZER]
        kube.patch(app.RESOURCE, name, namespace, {"metadata": {"finalizers": finalizers or None}})
    inventory.write(kube, {**record, "phase": "Deleting"})
    if live is not None and not observed:
        kube.progress(f"deleting Argo CD Application {namespace}/{name}")
        kube.delete(app.RESOURCE, name, namespace)
    kube.delete("configmaps", inventory.inventory_name(instance), target)
    return {
        "instance": instance,
        "namespace": target,
        "backend": BACKEND,
        "application": f"{namespace}/{name}",
        "deleted": [] if forget or observed else [f"Application/{name}"],
        "kept": [f"{item['kind']}/{item['name']}" for item in claims if forget or _kept_by_argocd(kube, item, target)],
        "skipped": [],
        **({"forgot": True} if forget else {}),
    }


def _kept_by_argocd(kube: Kube, item: dict[str, Any], namespace: str) -> bool:
    live = kube.get(item["resource"], item["name"], namespace)
    options = str(((live or {}).get("metadata") or {}).get("annotations", {}).get(app.SYNC_OPTIONS_ANNOTATION) or "")
    return "Delete=false" in options.replace(" ", "")


def compare_application(planned: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Where an Application someone else made differs from the one llmd would build for the stack: its
    destination namespace, and each source's repository, path, chart and (for a commit) revision."""
    found = []
    want, have = planned["spec"]["destination"], (live.get("spec") or {}).get("destination") or {}
    if want.get("namespace") != have.get("namespace"):
        found.append(f"destination namespace {have.get('namespace')} (the stack's is {want.get('namespace')})")

    def key(source: dict[str, Any]) -> tuple[str, str, str]:
        url = checks.normalize_url(str(source.get("repoURL") or ""))
        return url, str(source.get("path") or ""), str(source.get("chart") or "")

    wanted = {key(source): source for source in app.application_sources(planned) if not source.get("ref")}
    held = {key(source): source for source in app.application_sources(live) if not source.get("ref")}
    for missing in sorted(set(wanted) - set(held)):
        found.append(f"no source {missing[0]} {missing[1] or missing[2]}")
    for extra in sorted(set(held) - set(wanted)):
        found.append(f"another source {extra[0]} {extra[1] or extra[2]}")
    for shared in sorted(set(wanted) & set(held)):
        target = str(held[shared].get("targetRevision") or "")
        if app.is_commit(target) and target != wanted[shared].get("targetRevision"):
            found.append(
                f"{shared[1] or shared[2]} at {target[:12]}, not the planned {wanted[shared]['targetRevision'][:12]}"
            )
    return found


def import_application(
    kube: Kube, plan: Plan, application: dict[str, Any], *, actor: str = "", allow_drift: bool = False
) -> dict[str, Any]:
    """Record an Application someone else made (by hand, or an app-of-apps) as the instance; nothing in the
    cluster changes but the record, and llmd then only reads the Application. It must deliver the plan's
    stack: the same sources and destination namespace as the Application llmd would build, unless
    ``allow_drift``."""
    if inventory.read(kube, plan.namespace, plan.instance):
        raise ConflictError(
            f"instance {plan.instance} is already recorded in {plan.namespace}",
            hint=f"See `llmd status {plan.instance}`.",
        )
    differences = compare_application(plan.delivery, application) if plan.delivery else []
    if differences and not allow_drift:
        raise PolicyError(
            f"Application {application['metadata']['name']} does not deliver {plan.stack.id} to {plan.namespace}",
            details=[{"message": item} for item in differences],
            hint="Name the stack and source the Application points at, or pass --allow-drift to record it anyway.",
        )
    observed = {
        "apiVersion": app.API_VERSION,
        "kind": "Application",
        "metadata": dict(application["metadata"]),
        "spec": application.get("spec") or {},
    }
    record = _record(plan, observed, None, actor=actor, mode=OBSERVED)
    record["delivery"]["revision"] = ""
    record["objects"] = managed_objects(kube, application, plan.namespace)
    record["phase"] = "Imported"
    inventory.write(kube, record)
    return {
        "instance": plan.instance,
        "namespace": plan.namespace,
        "backend": BACKEND,
        "application": f"{application['metadata']['namespace']}/{application['metadata']['name']}",
        "imported": len(record["objects"]),
        "differences": differences,
    }


def update(kube: Kube, plan: Plan, record: dict[str, Any], *, actor: str, expected_digest: str = "") -> dict[str, Any]:
    """Point the instance's Application at what ``plan`` renders (another revision or stack path, or other
    kept counts), start its sync, and keep the record in step. The objects themselves are Argo CD's to
    change; the record lists them again once the Application is synced (``refresh_objects``)."""
    _refuse(plan, expected_digest)
    if (record.get("delivery") or {}).get("createdBy") == OBSERVED:
        raise PolicyError(
            f"instance {record['instance']} records an Application someone else made; llmd does not change it",
            hint="Change it where it is kept (its repository, or the app-of-apps that makes it).",
        )
    switched = (record.get("stack") or {}).get("id") != plan.stack.id
    updated = {
        **record,
        "delivery": {**record["delivery"], "revision": plan.stack.source.revision},
        "stack": plan.stack_record(),
        **({"previousStack": record["stack"]} if switched else {}),
        "planDigest": plan.digest,
        "phase": "Applying",
        "updatedBy": actor,
    }
    if plan.options.overrides:
        updated["overrides"] = dict(plan.options.overrides)
    else:
        updated.pop("overrides", None)
    inventory.write(kube, updated)
    submit(kube, updated, plan.delivery, actor=actor)
    return updated
