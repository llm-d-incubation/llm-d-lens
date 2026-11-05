"""What a stack needs in its namespace besides itself, and the PersistentVolume a claim waits for.

Some teams keep parts of a namespace apart from the model stack on purpose, so that tearing the
model down leaves them: NetworkPolicies for the namespace, or a model-cache claim whose volume
an administrator binds once. llmd calls each such part a *companion*. It is stood up as its own
instance before the stack, and a teardown of the stack keeps it.

Where a source keeps its companions is the source's layout, registered by the user and kept with
the source (``llmd sources add --companion TEMPLATE``); nothing here names a directory. A template
is a path relative to the source's root, in which

* ``{stack}`` is the stack's path, ``{name}`` its last segment and ``{namespace}`` the target
  namespace;
* ``..`` steps up a directory;
* ``*`` and ``?`` match within one path segment.

A template that matches nothing is skipped; one that matches several paths is reported with all of
them, and none is chosen. Each match is a kustomize directory or a manifest file.

The PersistentVolume behind a claim is cluster-scoped, so llmd neither applies nor reads it. For a
claim that is missing or Pending, it only finds the file in the source that defines it (the volume
named by the claim's ``volumeName``, or one whose ``claimRef`` names the claim) and says which
``kubectl apply -f`` an administrator runs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import inventory
from .catalog import Stack, kustomization_file, manifest_documents, manifest_files, source_files
from .errors import CommandError, UsageError
from .model import MAX_INSTANCE_LENGTH, Finding
from .process import Runner
from .sources import Source


@dataclass(frozen=True)
class Companion:
    #: The path in the source, as ``llmd standup`` takes it.
    path: str
    #: The template it was found by.
    template: str

    def instance(self) -> str:
        """The instance to stand it up as: the path's last segment, with ``-companion``."""
        stem = Path(self.path).name.rsplit(".", 1)[0] if Path(self.path).suffix else Path(self.path).name
        return _short(f"{stem}-companion", self.path)

    def describe(self) -> dict[str, Any]:
        return {"path": self.path, "template": self.template, "instance": self.instance()}


@dataclass
class Found:
    """The companions a stack's templates match, and for each template that matched several, all of them."""

    chosen: list[Companion]
    ambiguous: list[list[Companion]]


def _short(name: str, key: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "companion"
    if len(name) <= MAX_INSTANCE_LENGTH:
        return name
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"{name[: MAX_INSTANCE_LENGTH - 9].rstrip('-')}-{digest}"


def expand(template: str, stack: str, namespace: str) -> str:
    """The template with ``{stack}``, ``{name}`` and ``{namespace}`` filled in and ``..`` resolved."""
    try:
        filled = template.format(stack=stack, name=Path(stack).name, namespace=namespace)
    except (KeyError, IndexError) as exc:
        raise UsageError(
            f"companion template {template!r} names an unknown field {exc}",
            hint="Fields: {stack}, {name}, {namespace}.",
        ) from exc
    normalized = posixpath.normpath(filled)
    if normalized.startswith("..") or posixpath.isabs(normalized):
        raise UsageError(f"companion template {template!r} leaves the source checkout")
    return normalized


def _candidates(files: list[str]) -> set[str]:
    """Every path a companion can be: a directory with a kustomization, or a YAML file."""
    found = set()
    for line in files:
        path = Path(line)
        if path.name in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            found.add(path.parent.as_posix())
        elif path.suffix in (".yaml", ".yml"):
            found.add(line)
    return found


def _matches(path: str, pattern: str) -> bool:
    parts, wanted = path.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, wanted, strict=True))


def for_stack(stack: Stack, runner: Runner, namespace: str) -> Found:
    """The companions the source's templates name for ``stack`` in ``namespace``, in template order."""
    templates = stack.source.layout.companions
    found = Found([], [])
    if not templates:
        return found
    candidates = _candidates(source_files(stack.source, runner))
    for template in templates:
        pattern = expand(template, stack.id, namespace)
        matched = sorted(path for path in candidates if _matches(path, pattern) and _usable(stack.source, path))
        matched = [path for path in matched if path != stack.id]
        if len(matched) == 1:
            found.chosen.append(Companion(matched[0], template))
        elif matched:
            found.ambiguous.append([Companion(path, template) for path in matched])
    return found


def _usable(source: Source, path: str) -> bool:
    resolved = source.root / path
    if resolved.is_dir():
        return kustomization_file(resolved) is not None
    return bool(manifest_files(resolved))


def pv_files(source: Source, runner: Runner, *, claim: str, namespace: str, volume: str = "") -> list[str]:
    """Files in the source that define the PersistentVolume for ``namespace/claim``: the one named
    ``volume`` (the claim's ``volumeName``), or one whose ``claimRef`` names the claim."""
    found = []
    for line in source_files(source, runner):
        if not line.endswith((".yaml", ".yml")):
            continue
        for document in manifest_documents(source.root / line) or []:
            if document.get("kind") != "PersistentVolume":
                continue
            name = (document.get("metadata") or {}).get("name")
            ref = (document.get("spec") or {}).get("claimRef") or {}
            bound = ref.get("name") == claim and ref.get("namespace") in (namespace, None)
            if (volume and name == volume) or (not volume and bound):
                found.append(line)
                break
    return sorted(found)


def shared_prefix(path: str, stack: str) -> int:
    count = 0
    for left, right in zip(Path(path).parts, Path(stack).parts, strict=False):
        if left != right:
            break
        count += 1
    return count


# -- against the cluster ----------------------------------------------------------------------------
def check(kube, plan, runner: Runner) -> list[Companion]:
    """Add findings to ``plan``: each companion the namespace lacks, each template that matched several
    paths, and the PV file of each claim that is missing or Pending. Reads only. Returns the companions
    the namespace lacks, in template order."""
    found = for_stack(plan.stack, runner, plan.namespace)
    for tied in found.ambiguous:
        plan.findings.append(
            Finding(
                "info",
                "companion-ambiguous",
                f"companion template {tied[0].template} matches {len(tied)} paths: "
                + ", ".join(item.path for item in tied),
                hint="Stand the right one up yourself (`llmd standup PATH -n "
                f"{plan.namespace} --name NAME`), or narrow the template.",
            )
        )
    missing = []
    states: dict[str, str] = {}
    for item in found.chosen:
        try:
            record = inventory.read(kube, plan.namespace, item.instance())
        except CommandError:
            states[item.path] = "unknown"
            continue
        if record is not None:
            states[item.path] = "applied"
            continue
        states[item.path] = "missing"
        missing.append(item)
        plan.findings.append(
            Finding(
                "warning",
                "companion-missing",
                f"{item.path} is a companion of this stack and is not stood up in {plan.namespace}",
                hint=f"Stand it up first: `llmd standup {item.path} -n {plan.namespace} --name {item.instance()}`, "
                "or pass --with-companions. A teardown of the stack keeps it.",
            )
        )
    if found.chosen or found.ambiguous:
        plan.cluster["companions"] = states
    for claim in _claims(plan):
        _check_claim(kube, plan, runner, claim)
    return missing


def _claims(plan) -> list[str]:
    claims = [item["name"] for item in plan.policy.prerequisites if item["kind"] == "PersistentVolumeClaim"]
    return claims + [doc["metadata"]["name"] for doc in plan.documents if doc["kind"] == "PersistentVolumeClaim"]


def _check_claim(kube, plan, runner: Runner, claim: str) -> None:
    """A claim the stack uses that is not there, or is Pending: name the file of its PV, for an admin."""
    try:
        live = kube.get("persistentvolumeclaims", claim, plan.namespace)
    except CommandError:
        return
    if live is not None and (live.get("status") or {}).get("phase") in ("Bound", None):
        return
    volume = _volume_name(plan, claim, live)
    written_for = plan.policy.source_namespaces[0] if plan.policy.source_namespaces else plan.namespace
    files = pv_files(plan.stack.source, runner, claim=claim, namespace=written_for, volume=volume)
    if not files:
        return
    files.sort(key=lambda path: (-shared_prefix(path, plan.stack.id), path))  # nearest the stack first
    moved = written_for != plan.namespace
    state = "Pending" if live is not None else "missing"
    plan.findings.append(
        Finding(
            "info",
            "pv-file",
            f"PersistentVolumeClaim/{claim} is {state}; its PersistentVolume"
            + (f" {volume}" if volume else "")
            + f" is defined in {', '.join(files)}",
            object=f"PersistentVolumeClaim/{claim}",
            hint=f"An administrator applies it: `kubectl apply -f {files[0]}`"
            + (
                f". It is written for {written_for}: its claimRef must name {plan.namespace} here, and a "
                "PersistentVolume binds one claim only"
                if moved
                else ". If it exists and is Released (its claim was deleted), the administrator removes "
                "spec.claimRef.uid so it can bind again"
            ),
        )
    )


def _volume_name(plan, claim: str, live: dict[str, Any] | None) -> str:
    """The PV the claim is bound to in advance: on the live claim, else in the stack."""
    if live is not None:
        return str((live.get("spec") or {}).get("volumeName") or "")
    for document in plan.documents:
        if document["kind"] == "PersistentVolumeClaim" and document["metadata"]["name"] == claim:
            return str((document.get("spec") or {}).get("volumeName") or "")
    return ""
