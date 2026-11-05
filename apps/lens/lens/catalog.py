"""The stacks a source holds, read from the checkout; no stack or model is compiled in.

A stack is what a team deploys into one namespace:

* an llm-d guide: a directory with a ``guide.yaml`` (``guides/optimized-baseline``,
  ``guides/models/<model>``). Its ``deploy`` steps say what it applies: the router chart
  (``helm install``) and the model server overlays (``kubectl apply -k``), picked by the
  guide's variables (``--var ACCELERATOR_TYPE=...``);
* a kustomize directory (``kustomization.yaml``), such as one model server overlay of a guide
  (``guides/<guide>/modelserver/gpu/vllm/base``), applied as ``kubectl apply -k`` would;
* any other directory or YAML file named explicitly, whose manifests are read as they are.

``llmd stacks`` lists the guides, then the other kustomize directories that no other directory
includes (a guide's own overlays and kustomize Components are left out, and so are Helm charts).
A stack is named by its path, or by the end of its path when only one listed stack ends that way
(``optimized-baseline``, ``models/deepseek-v4``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlio
from .errors import NotFoundError, UsageError
from .process import Runner
from .sources import Source

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
GUIDE_FILE = "guide.yaml"
#: Instance names become object names (``lens-instance-<name>``), so they stay short.
MAX_NAME = 40


@dataclass(frozen=True)
class Stack:
    id: str  # the path in the source, e.g. guides/optimized-baseline
    kind: str  # guide | kustomize | manifests
    source: Source

    @property
    def path(self) -> Path:
        return self.source.root / self.id

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "source": self.source.describe()}


def kustomization_file(directory: Path) -> Path | None:
    for name in KUSTOMIZATION_FILES:
        if (directory / name).is_file():
            return directory / name
    return None


def inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``; refuse absolute paths and traversal outside the source."""
    if not relative or Path(relative).is_absolute():
        raise UsageError(f"expected a path relative to the source: {relative!r}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise UsageError(f"path escapes the source checkout: {relative}")
    return resolved


def local_includes(directory: Path) -> list[Path]:
    """The local directories a kustomize unit includes as ``resources`` or ``components``."""
    kustomization = kustomization_file(directory)
    if kustomization is None:
        return []
    try:
        data = yamlio.load_file(kustomization) or {}
    except (OSError, UsageError):
        return []
    if not isinstance(data, dict):
        return []
    entries = [*(data.get("resources") or []), *(data.get("components") or [])]
    return [
        (directory / entry).resolve()
        for entry in entries
        if isinstance(entry, str) and "://" not in entry and (directory / entry).is_dir()
    ]


def uses_helm(directory: Path, seen: set[Path] | None = None) -> bool:
    """Whether a kustomize unit renders a Helm chart (``helmCharts``) itself or in a unit it includes;
    ``kustomize build`` then needs ``--enable-helm``."""
    seen = set() if seen is None else seen
    directory = directory.resolve()
    kustomization = kustomization_file(directory)
    if directory in seen or kustomization is None:
        return False
    seen.add(directory)
    try:
        data = yamlio.load_file(kustomization) or {}
    except (OSError, UsageError):
        return False
    if isinstance(data, dict) and data.get("helmCharts"):
        return True
    return any(uses_helm(include, seen) for include in local_includes(directory))


def manifest_documents(path: Path) -> list[dict[str, Any]] | None:
    """The Kubernetes objects in a YAML file, or None when it is not a manifest file (a kustomization,
    a Helm values file, a guide, anything without apiVersion and kind)."""
    if path.name in (*KUSTOMIZATION_FILES, GUIDE_FILE) or path.name.endswith((".values.yaml", ".values.yml")):
        return None
    if path.name in ("values.yaml", "values.yml", "Chart.yaml"):
        return None
    try:
        documents = yamlio.load_documents(path.read_text(encoding="utf-8"), str(path))
    except (UsageError, UnicodeDecodeError, OSError):
        return None
    objects = [doc for doc in documents if doc.get("apiVersion") and doc.get("kind")]
    if not objects or {doc["kind"] for doc in objects} & {"Kustomization", "Component"}:
        return None
    return objects


def manifest_files(directory: Path) -> list[Path]:
    """Manifest files at or below ``directory``, leaving out nested kustomize units and Helm charts."""
    if directory.is_file():
        return [directory] if manifest_documents(directory) is not None else []
    found = []
    for path in sorted(directory.rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        parents = [parent for parent in path.parents if parent.is_relative_to(directory)]
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        if any(parent != directory and kustomization_file(parent) for parent in parents):
            continue
        if any((parent / "Chart.yaml").is_file() for parent in parents):
            continue
        if manifest_documents(path) is not None:
            found.append(path)
    return found


def _tracked_files(source: Source, runner: Runner) -> list[str] | None:
    """The checkout's files (tracked and untracked, not ignored) when it is a Git work tree."""
    if not source.revision:
        return None
    listed = runner(
        ["git", "-C", str(source.root), "ls-files", "--cached", "--others", "--exclude-standard"], timeout=60
    )
    return listed.stdout.splitlines() if listed.ok else None


def _source_files(source: Source, runner: Runner) -> list[str]:
    """The checkout's files, from Git when it is a work tree, without hidden directories."""
    files = _tracked_files(source, runner)
    if files is None:
        files = [path.relative_to(source.root).as_posix() for path in source.root.rglob("*") if path.is_file()]
    return [line for line in files if not any(part.startswith(".") for part in Path(line).parts[:-1])]


def _is_component(directory: Path) -> bool:
    """A kustomize Component is included by others, never applied on its own."""
    kustomization = kustomization_file(directory)
    try:
        data = yamlio.load_file(kustomization) if kustomization else None
    except (OSError, UsageError):
        return False
    return isinstance(data, dict) and data.get("kind") == "Component"


def _in_chart(root: Path, unit: str) -> bool:
    path = root / unit
    return any((parent / "Chart.yaml").is_file() for parent in [path, *path.parents] if parent != root)


def _inside_any(path: str, directories: set[str]) -> bool:
    return any(path == directory or path.startswith(directory + "/") for directory in directories)


def matches(path: str, pattern: str) -> bool:
    """Whether a stack path matches a glob: ``*`` and ``?`` stay within one path segment, ``**``
    matches any number of segments."""
    return _match(path.split("/"), pattern.strip("/").split("/"))


def _match(parts: list[str], pattern: list[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_match(parts[index:], pattern[1:]) for index in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:])


def list_stacks(
    source: Source,
    runner: Runner,
    prefix: str = "",
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> list[Stack]:
    """The source's stacks: its guides (directories with a ``guide.yaml``), then every kustomize
    directory no other one includes, outside the guides, Helm charts and kustomize Components.

    ``include`` and ``exclude`` are the source's stack patterns (``llmd sources add --stacks``,
    ``--exclude``): with ``include``, only the kustomize directories it matches are listed; guides
    are always listed. ``prefix`` keeps the stacks below one path."""
    files = _source_files(source, runner)
    guides = {
        Path(line).parent.as_posix()
        for line in files
        if Path(line).name == GUIDE_FILE and Path(line).parent.as_posix() != "."
    }
    units = {
        Path(line).parent.as_posix()
        for line in files
        if Path(line).name in KUSTOMIZATION_FILES and Path(line).parent.as_posix() != "."
    }
    paths = {(source.root / unit).resolve(): unit for unit in units}
    included = {paths[target] for path in paths for target in local_includes(path) if target in paths}
    kustomize = sorted(
        unit
        for unit in units - included
        if not _inside_any(unit, guides) and not _in_chart(source.root, unit) and not _is_component(source.root / unit)
    )
    if include:
        kustomize = [unit for unit in kustomize if any(matches(unit, pattern) for pattern in include)]
    kustomize = [unit for unit in kustomize if not any(matches(unit, pattern) for pattern in exclude)]
    prefix = prefix.strip("/")
    stacks = [Stack(id=guide, kind="guide", source=source) for guide in sorted(guides)]
    stacks += [Stack(id=unit, kind="kustomize", source=source) for unit in kustomize]
    return [stack for stack in stacks if not prefix or stack.id == prefix or stack.id.startswith(prefix + "/")]


def source_stacks(source: Source, runner: Runner, prefix: str = "") -> list[Stack]:
    """:func:`list_stacks` with the source's own layout (``llmd sources add --stacks/--exclude``)."""
    return list_stacks(source, runner, prefix, include=source.layout.stacks, exclude=source.layout.exclude)


def find_stack(source: Source, reference: str, *, runner: Runner | None = None, listed=None) -> Stack:
    """A stack by its path in the source: a guide, a kustomize unit, a directory of manifests, or one
    file. A path the source does not have is looked up among the listed stacks (``listed``, or
    :func:`list_stacks` with ``runner``) by the end of their path: ``optimized-baseline`` finds
    ``guides/optimized-baseline`` when no other listed stack ends the same way."""
    reference = reference.strip().strip("/")
    if not reference:
        raise UsageError("no stack selected", hint="Run `llmd stacks` to list the stacks of the source.")
    path = inside(source.root, reference)
    if not path.exists() and (listed is not None or runner is not None):
        stacks = listed if listed is not None else source_stacks(source, runner)
        found = [stack for stack in stacks if stack.id == reference or stack.id.endswith("/" + reference)]
        if len(found) == 1:
            return find_stack(source, found[0].id)
        if found:
            raise UsageError(
                f"{reference!r} names {len(found)} stacks of source {source.name}: "
                + ", ".join(stack.id for stack in found),
                hint="Name one by more of its path.",
            )
    identifier = path.relative_to(source.root.resolve()).as_posix()
    if path.is_dir() and (path / GUIDE_FILE).is_file():
        return Stack(id=identifier, kind="guide", source=source)
    if path.is_dir() and kustomization_file(path):
        return Stack(id=identifier, kind="kustomize", source=source)
    if path.exists() and manifest_files(path):
        return Stack(id=identifier, kind="manifests", source=source)
    if path.exists():
        raise UsageError(f"{reference} holds no Kubernetes manifests and no {GUIDE_FILE}")
    raise NotFoundError(
        f"stack {reference!r} not found in source {source.name}",
        hint="Run `llmd stacks` to list the stacks of the source.",
    )


def default_name(stack: Stack) -> str:
    """An instance name from the stack's path: a guide's directory name, else the last two segments."""
    segments = stack.id.split("/")
    tail = segments[-1] if stack.kind == "guide" else "-".join(segments[-2:])
    name = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-") or "stack"
    if len(name) <= MAX_NAME:
        return name
    digest = hashlib.sha256(stack.id.encode()).hexdigest()[:8]
    return f"{name[: MAX_NAME - 9].rstrip('-')}-{digest}"
