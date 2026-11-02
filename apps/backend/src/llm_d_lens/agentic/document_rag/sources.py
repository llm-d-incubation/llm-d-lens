"""Pinned evidence collection from approved repositories and reviewed offline snapshots."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from llm_d_lens.agentic.document_rag.publication_contracts import (
    InputFormat,
    InventoryFile,
    PinnedInventory,
    PublicationError,
    SourceApproval,
    content_digest,
)
from llm_d_lens.agentic.public_benchmark_record.sources import PRISM_ORIGIN, SourceArchive
from llm_d_lens.cluster.repo_downloads import REPO_URLS, RepoName, download_snapshot
from llm_d_lens.infrastructure.process import popen, run_sync

LLM_D_ORIGIN = "https://github.com/llm-d/llm-d"
BENCHMARK_ORIGIN = "https://github.com/llm-d/llm-d-benchmark.git"
WEB_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "https://llm-d.ai": ("blog/**/*.md",),
    "https://cloud.google.com": ("blog/**/*.md",),
    "https://aws.amazon.com": ("blogs/machine-learning/**/*.md",),
    "https://developers.redhat.com": ("articles/**/*.md",),
    "https://www.redhat.com": ("en/blog/**/*.md",),
    "https://research.ibm.com": ("blog/**/*.md",),
    "https://www.amd.com": ("en/developer/resources/technical-articles/**/*.md",),
    "https://rocm.blogs.amd.com": ("artificial-intelligence/**/*.md",),
    "https://kserve.github.io": ("website/blog/**/*.md",),
    "https://medium.com": ("google-cloud/**/*.md",),
    "https://developer.nvidia.com": ("blog/**/*.md",),
    "https://www.coreweave.com": ("blog/**/*.md",),
    "https://github.com": (
        "llm-d/llm-d-kv-cache/blob/*/benchmarking/**/*.md",
        "kserve/website/blob/*/blog/**/*.md",
    ),
}
_PATHS = {
    LLM_D_ORIGIN: (
        "guides/**/*.md",
        "guides/**/benchmark-results/**/*.json",
        "guides/**/benchmark-results/**/*.png",
    ),
    BENCHMARK_ORIGIN: ("docs/**/*.md", "benchmark-report/README.md"),
    **WEB_SOURCE_PATHS,
}
_FORMATS: dict[str, InputFormat] = {
    ".md": "markdown",
    ".json": "json",
    ".png": "png",
    ".zip": "zip",
    ".jq": "jq",
}


def allowed_paths(origin_id: str) -> tuple[str, ...]:
    if origin_id not in _PATHS:
        raise PublicationError("source is not in the v1 allowlist")
    return _PATHS[origin_id]


def validate_approval(origin_id: str, approval: SourceApproval) -> None:
    permitted = allowed_paths(origin_id)
    for pattern in approval.allowed_paths:
        if pattern in permitted:
            continue
        if (
            origin_id in WEB_SOURCE_PATHS
            and not any(character in pattern for character in "*?[]")
            and input_format(origin_id, pattern) == "markdown"
        ):
            continue
        raise PublicationError("approval cannot expand the v1 source/path allowlist")
    if len(set(approval.allowed_paths)) != len(approval.allowed_paths):
        raise PublicationError("approved paths must be unique")


def validate_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or path != parsed.as_posix()
        or ".." in parsed.parts
        or "\\" in path
        or "\x00" in path
        or "\ufffd" in path
        or not parsed.parts
    ):
        raise PublicationError("source path must be canonical, relative, and without traversal")


def validate_revision(origin_id: str, revision: str) -> None:
    allowed_paths(origin_id)
    if origin_id in WEB_SOURCE_PATHS:
        valid = re.fullmatch("[0-9a-f]{64}", revision) is not None
    else:
        valid = re.fullmatch("[0-9a-f]{40}", revision) is not None
    if not valid:
        raise PublicationError(
            "source revision must be a pinned full commit, ISO snapshot date, or web inventory SHA-256"
        )


def _matches(path: str, pattern: str) -> bool:
    expression = re.escape(pattern).replace(r"\*\*/", "(?:[^/]+/)*").replace(r"\*", "[^/]*")
    return re.fullmatch(expression, path) is not None


def input_format(origin_id: str, path: str, approval: SourceApproval | None = None) -> InputFormat | None:
    validate_path(path)
    patterns = allowed_paths(origin_id) if approval is None else approval.allowed_paths
    if any(_matches(path, pattern) for pattern in patterns):
        if origin_id in WEB_SOURCE_PATHS:
            return "markdown"
        return _FORMATS[PurePosixPath(path).suffix]
    return None


def web_source_url(origin_id: str, path: str) -> str:
    """Snapshot paths append .md to the original, origin-relative article path."""
    if origin_id not in WEB_SOURCE_PATHS or input_format(origin_id, path) != "markdown":
        raise PublicationError("web article URL requires an approved origin and Markdown path")
    article_path = path if origin_id == "https://github.com" else path.removesuffix(".md")
    return f"{origin_id}/{quote(article_path, safe='/-._~')}"


def _git(repo: Path, *arguments: str) -> str:
    result = run_sync(["git", "-C", str(repo), *arguments], timeout=60)
    if not result.ok:
        raise PublicationError("could not read a complete pinned Git inventory")
    return result.stdout.strip()


def _git_blob(repo: Path, object_id: str) -> bytes:
    process = popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        data, _ = process.communicate(timeout=60)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise PublicationError("pinned Git blob read timed out") from error
    if process.returncode != 0:
        raise PublicationError("pinned Git blob is unavailable")
    return data


def collect_git(
    origin_id: str,
    repo: Path | None,
    revision: str,
    approval: SourceApproval,
    archive: SourceArchive,
) -> PinnedInventory:
    validate_approval(origin_id, approval)
    validate_revision(origin_id, revision)
    if origin_id == PRISM_ORIGIN or not re.fullmatch("[0-9a-f]{40}", revision):
        raise PublicationError("Git inputs require an allowlisted repository and a full pinned commit")
    managed = repo is None
    if repo is None:
        name: RepoName = "llm-d" if origin_id == LLM_D_ORIGIN else "llm-d-benchmark"
        if REPO_URLS[name].removesuffix(".git") != origin_id.removesuffix(".git"):
            raise PublicationError("managed repository URL does not match the approved source identity")
        try:
            repo = download_snapshot(name, revision)
        except (RuntimeError, OSError) as error:
            raise PublicationError("could not fetch the approved pinned GitHub repository") from error
    remote = _git(repo, "remote", "get-url", "origin")
    if managed and remote == str(repo.parent / "mirror.git"):
        remote = _git(repo, "--git-dir", str(repo.parent / "mirror.git"), "remote", "get-url", "origin")
    if remote.removesuffix(".git") != origin_id.removesuffix(".git"):
        raise PublicationError("checkout origin does not match the approved source identity")
    if _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}") != revision:
        raise PublicationError("checkout does not contain the pinned commit")
    tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    if tree and not tree.endswith("\x00"):
        raise PublicationError("pinned Git listing is truncated")
    files = []
    for entry in tree.split("\x00"):
        if not entry:
            continue
        if "\t" not in entry:
            raise PublicationError("pinned Git listing has an invalid entry")
        metadata, path = entry.split("\t", 1)
        kind = input_format(origin_id, path, approval)
        if kind is None:
            continue
        mode, object_type, object_id = metadata.split()
        if mode not in ("100644", "100755") or object_type != "blob" or not re.fullmatch("[0-9a-f]{40}", object_id):
            raise PublicationError("allowlisted source cannot be a symlink or submodule")
        digest = archive.put(_git_blob(repo, object_id))
        files.append(InventoryFile(path=path, sha256=digest, format=kind))
    return PinnedInventory(
        origin_id=origin_id, revision=revision, complete=True, files=tuple(sorted(files, key=lambda f: f.path))
    )


def collect_snapshot(
    root: Path,
    revision: str,
    expected_files: dict[str, str],
    approval: SourceApproval,
    archive: SourceArchive,
    *,
    origin_id: str = PRISM_ORIGIN,
) -> PinnedInventory:
    if origin_id != PRISM_ORIGIN and origin_id not in WEB_SOURCE_PATHS:
        raise PublicationError("offline snapshots require an approved snapshot origin")
    validate_approval(origin_id, approval)
    validate_revision(origin_id, revision)
    if origin_id in WEB_SOURCE_PATHS and revision != content_digest(expected_files):
        raise PublicationError("web snapshot revision must match the reviewed file inventory SHA-256")
    if (
        (not expected_files and origin_id == PRISM_ORIGIN)
        or not root.is_dir()
        or any(path.is_symlink() for path in (root, *root.parents))
    ):
        raise PublicationError("snapshot needs a reviewed hash inventory and a non-symlink root")
    actual = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PublicationError("snapshot symlinks are not supported")
        if path.is_file() and input_format(origin_id, relative, approval) is not None:
            actual[relative] = path
    if set(actual) != set(expected_files):
        raise PublicationError("snapshot inventory is incomplete or contains unreviewed inputs")
    files = []
    for relative, digest in sorted(expected_files.items()):
        kind = input_format(origin_id, relative, approval)
        if kind is None:
            raise PublicationError("snapshot contains an unapproved file")
        if hashlib.sha256(actual[relative].read_bytes()).hexdigest() != digest:
            raise PublicationError("snapshot hash does not match the reviewed inventory")
        if archive.put(actual[relative].read_bytes()) != digest:
            raise PublicationError("snapshot changed during collection")
        files.append(InventoryFile(path=relative, sha256=digest, format=kind))
    return PinnedInventory(origin_id=origin_id, revision=revision, complete=True, files=tuple(files))
