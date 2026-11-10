"""Where Argo CD fetches a stack from: the source's remote URL, and a revision it can check out.

Argo CD sees only what is committed and pushed, so the plan says when the checkout differs from that:
uncommitted changes, files Git does not track, a commit no remote branch holds, or a local checkout
without a remote. Any of these blocks a standup with ``--backend argocd``.
"""

from __future__ import annotations

from pathlib import Path

from ..catalog import Stack, local_includes
from ..errors import CommandError
from ..guides import Unit
from ..model import Finding
from ..plan import Plan
from ..process import Runner
from .application import GitOrigin, is_commit, strip_credentials


def unit_paths(plan: Plan, unit: Unit) -> list[str]:
    """The checkout paths a unit renders from: a kustomize directory and the local directories it
    includes, a manifest path, or a chart directory and values files. Paths outside the checkout (a
    remote base) are left out."""
    root = plan.stack.source.root.resolve()
    found = list(unit.values)
    if unit.chart and "://" not in unit.chart:
        found.append(Path(unit.chart))
    if unit.path is not None:
        found += _included(Path(unit.path)) if unit.kind == "kustomize" else [Path(unit.path)]
    paths = (Path(path).resolve() for path in found)
    return sorted({path.relative_to(root).as_posix() for path in paths if path.is_relative_to(root)})


def _included(directory: Path, seen: set[Path] | None = None) -> list[Path]:
    """A kustomize directory and every local directory it includes, at any depth."""
    seen = set() if seen is None else seen
    directory = directory.resolve()
    if directory in seen:
        return []
    seen.add(directory)
    found = [directory]
    for include in local_includes(directory):
        found += _included(include, seen)
    return found


def stack_paths(plan: Plan) -> list[str]:
    """Every checkout path the plan's units render from; a change to one of them reaches Argo CD."""
    return sorted({path for unit in plan.units for path in unit_paths(plan, unit)})


def _git(runner: Runner, root: str, *args: str) -> str:
    result = runner(["git", "-C", root, *args], timeout=60)
    if not result.ok:
        raise CommandError(result.message)
    return result.stdout.strip()


def _untracked(stack: Stack, runner: Runner, paths: list[str]) -> list[str]:
    if not paths:
        return []
    try:
        listed = _git(runner, str(stack.source.root), "ls-files", "--others", "--exclude-standard", "--", *paths)
    except CommandError:
        return []
    return [line for line in listed.splitlines() if line.strip()]


def _changed(stack: Stack, runner: Runner, paths: list[str]) -> bool:
    """Whether a file the stack uses has uncommitted changes (a change elsewhere in the checkout does not
    reach Argo CD's render, so it does not count)."""
    if not paths:
        return stack.source.dirty
    try:
        status = _git(runner, str(stack.source.root), "status", "--porcelain", "--untracked-files=no", "--", *paths)
    except CommandError:
        return stack.source.dirty
    return bool(status)


def resolve(
    stack: Stack, runner: Runner, *, paths: list[str], remote: str = "origin", target: str = ""
) -> tuple[GitOrigin | None, list[Finding]]:
    """The origin Argo CD uses for ``stack``, and why it may not match what the plan rendered.

    ``paths`` are the stack's own paths in the source, checked for files Git does not track.
    ``target`` is the ``targetRevision`` to write; by default the commit the plan rendered."""
    source = stack.source
    if not source.revision:
        return None, [
            Finding(
                "error",
                "argocd-not-git",
                f"source {source.name} is not a Git checkout; Argo CD renders from Git",
                hint="Register the repository's URL as a source, or use a checkout whose commit is pushed.",
            )
        ]
    findings: list[Finding] = []
    if _changed(stack, runner, paths):
        findings.append(
            Finding(
                "error",
                "argocd-dirty",
                f"the stack has uncommitted changes in source {source.name}; Argo CD sees only committed files",
                hint="Commit and push them, or stand it up with --backend direct.",
            )
        )
    untracked = _untracked(stack, runner, paths)
    if untracked:
        listed = ", ".join(untracked[:3]) + (f" and {len(untracked) - 3} more" if len(untracked) > 3 else "")
        findings.append(
            Finding(
                "error",
                "argocd-untracked",
                f"{len(untracked)} file(s) of the stack are not committed ({listed}); Argo CD does not see them",
                hint="Commit and push them, or stand it up with --backend direct.",
            )
        )
    revision = source.revision
    if source.kind == "git":
        url, prefix = source.location, ""
    else:
        root = str(source.root)
        try:
            url = _git(runner, root, "remote", "get-url", remote)
        except CommandError:
            return None, [
                *findings,
                Finding(
                    "error",
                    "argocd-no-remote",
                    f"source {source.name} has no {remote!r} remote for Argo CD to fetch",
                    hint="Add the remote, pass --argocd-remote, or register the repository URL as a source.",
                ),
            ]
        try:
            containing = _git(runner, root, "branch", "-r", "--contains", revision, "--list", f"{remote}/*")
        except CommandError:
            containing = ""
        if not containing:
            findings.append(
                Finding(
                    "error",
                    "argocd-unpushed",
                    f"commit {revision[:12]} is on no {remote} branch this checkout knows",
                    hint=f"Push it (then `git fetch {remote}`), so Argo CD can check it out.",
                )
            )
        try:
            prefix = _git(runner, root, "rev-parse", "--show-prefix").rstrip("/")
        except CommandError:
            prefix = ""
    clean = strip_credentials(url)
    if clean != url:
        findings.append(Finding("info", "argocd-url-credentials", "the repository URL's credentials are left out"))
    chosen = target or revision
    if target and is_commit(target) and target != revision:
        findings.append(
            Finding(
                "error",
                "argocd-revision",
                f"--argocd-revision {target[:12]} is not the commit the plan rendered ({revision[:12]})",
            )
        )
    elif target and not is_commit(target):
        findings.append(
            Finding(
                "warning",
                "argocd-revision-moves",
                f"the Application follows {target}; Argo CD renders whatever commit it points to then, which "
                f"may differ from the plan's {revision[:12]}",
            )
        )
    return GitOrigin(repo_url=clean, revision=revision, target=chosen, prefix=prefix), findings
