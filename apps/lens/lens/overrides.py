"""Replica counts set with ``llmd scale``, kept in the instance's record.

A team often scales a stack to the accelerators it can get, before the stack's files say so. The
count then belongs to the instance, not to the files: ``scale`` records it in the record's
``overrides``, and every later plan of the instance (``standup``, ``rollouts``) renders the stack
with it, so an update does not set the count back. ``scale WORKLOAD=--`` clears it.

``llmd`` does not edit the stack's repository; ``status`` and ``plan`` say which counts differ from
the files, so the team can record them there.

A workload is named ``NAME`` (a Deployment, StatefulSet or LeaderWorkerSet) or ``SET/ROLE`` (a role
of a DisaggregatedSet).
"""

from __future__ import annotations

from typing import Any

from .errors import UsageError
from .model import Finding

#: Kinds whose ``spec.replicas`` ``scale`` sets.
SCALABLE = ("Deployment", "StatefulSet", "LeaderWorkerSet")


def parse(item: str) -> tuple[str, int | None]:
    """``WORKLOAD=N`` -> (workload, N); ``WORKLOAD=--`` -> (workload, None), which clears it."""
    workload, separator, value = item.partition("=")
    if not separator or not workload:
        raise UsageError(f"expected WORKLOAD=REPLICAS, got {item!r}", hint="For example decode=1, or pd/decode=2.")
    if value == "--":
        return workload, None
    if not value.isdigit():
        raise UsageError(f"replicas must be a whole number or -- (to clear), got {value!r}")
    return workload, int(value)


def find(documents: list[dict[str, Any]], workload: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the object, the dict that holds its ``replicas``) for a workload of the stack."""
    name, _, role = workload.partition("/")
    for document in documents:
        if document["metadata"]["name"] != name or document["kind"] not in (*SCALABLE, "DisaggregatedSet"):
            continue
        spec = document.setdefault("spec", {})
        if document["kind"] != "DisaggregatedSet":
            if role:
                break
            return document, spec
        for item in spec.get("roles") or []:
            if role and item.get("name") == role:
                return document, item.setdefault("spec", {})
        roles = ", ".join(f"{name}/{item.get('name')}" for item in spec.get("roles") or [])
        if role:
            raise UsageError(f"DisaggregatedSet/{name} has no role {role!r}", hint=f"Its roles: {roles}.")
        raise UsageError(f"name a role of DisaggregatedSet/{name}", hint=f"Its roles: {roles}.")
    raise UsageError(
        f"the stack has no workload {workload!r} to scale",
        hint="Name a Deployment, StatefulSet or LeaderWorkerSet, or SET/ROLE for a DisaggregatedSet role: "
        + ", ".join(workloads(documents)),
    )


def workloads(documents: list[dict[str, Any]]) -> list[str]:
    """The names ``scale`` takes for a stack's workloads."""
    found = []
    for document in documents:
        name = document["metadata"]["name"]
        if document["kind"] == "DisaggregatedSet":
            roles = (document.get("spec") or {}).get("roles") or []
            found += [f"{name}/{role.get('name')}" for role in roles]
        elif document["kind"] in SCALABLE:
            found.append(name)
    return found


def replicas(holder: dict[str, Any]) -> int:
    value = holder.get("replicas")
    return 1 if value is None else int(value)


def apply(documents: list[dict[str, Any]], overrides: dict[str, int]) -> list[Finding]:
    """Set each override on the rendered objects; one finding per count that differs from the files."""
    findings = []
    for workload, count in sorted(overrides.items()):
        try:
            document, holder = find(documents, workload)
        except UsageError:
            findings.append(
                Finding(
                    "warning",
                    "scale-override-unused",
                    f"the instance scales {workload} to {count}, which the stack no longer renders",
                    hint=f"Clear it with `llmd scale INSTANCE {workload}=--`.",
                )
            )
            continue
        files = replicas(holder)
        holder["replicas"] = count
        if files != count:
            findings.append(
                Finding(
                    "info",
                    "scale-override",
                    f"{workload}: {count} replicas, set by `llmd scale`; the stack's files say {files}",
                    object=f"{document['kind']}/{document['metadata']['name']}",
                    hint="Record the count in the stack's repository, or clear it with "
                    "`llmd scale INSTANCE WORKLOAD=--`.",
                )
            )
    return findings
