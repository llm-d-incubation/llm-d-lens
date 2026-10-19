"""Offline, pinned input collection. No fetching or crawling of external URLs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from llm_d_lens.benchmark_rag.publication_contracts import (
    InputFormat,
    InventoryFile,
    PinnedInventory,
    PublicationError,
    SourceApproval,
)
from llm_d_lens.infrastructure.artifacts.store import artifact_uri, register_artifacts, resolve_artifact
from llm_d_lens.infrastructure.process import popen, run_sync
from llm_d_lens.infrastructure.storage.paths import storage_path

PRISM_ORIGIN = "prism:public-snapshot"
LLM_D_ORIGIN = "https://github.com/VincyZhang/llm-d"
BENCHMARK_ORIGIN = "https://github.com/llm-d/llm-d-benchmark.git"
_PATHS = {
    PRISM_ORIGIN: (
        "archives/archived_drive_data.json",
        "archives/llm-d-benchmarks.json",
        "results/promoted-index.json",
        "results/public-index.json",
        "results/json/*.json",
        "results/zip/*.zip",
        "normalize-and-merge.jq",
        "historical-merged-deduplicated.json",
    ),
    LLM_D_ORIGIN: (
        "guides/**/*.md",
        "guides/**/benchmark-results/**/*.json",
        "guides/**/benchmark-results/**/*.png",
    ),
    BENCHMARK_ORIGIN: ("docs/**/*.md", "benchmark-report/README.md"),
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
    if not set(approval.allowed_paths).issubset(allowed_paths(origin_id)):
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
    if origin_id == PRISM_ORIGIN:
        try:
            valid = date.fromisoformat(revision).isoformat() == revision
        except ValueError:
            valid = False
    else:
        valid = re.fullmatch("[0-9a-f]{40}", revision) is not None
    if not valid:
        raise PublicationError("source revision must be a pinned full commit or an ISO snapshot date")


def _matches(path: str, pattern: str) -> bool:
    expression = re.escape(pattern).replace(r"\*\*/", "(?:[^/]+/)*").replace(r"\*", "[^/]*")
    return re.fullmatch(expression, path) is not None


def input_format(origin_id: str, path: str, approval: SourceApproval | None = None) -> InputFormat | None:
    validate_path(path)
    patterns = allowed_paths(origin_id) if approval is None else approval.allowed_paths
    if any(_matches(path, pattern) for pattern in patterns):
        return _FORMATS[PurePosixPath(path).suffix]
    return None


class SourceArchive:
    """Content-addressed evidence files using the shared Lens artifact layout."""

    @staticmethod
    def _root(digest: str) -> Path:
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise PublicationError("invalid source digest")
        return storage_path("data", "artifacts", "benchmark-rag", digest)

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        root = self._root(digest)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / "source"
        if target.is_symlink():
            raise PublicationError("source archive symlinks are not supported")
        with NamedTemporaryFile(dir=root, prefix=".source-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if target.read_bytes() != data:
                        raise PublicationError("immutable source archive content conflicts") from None
            finally:
                temporary.unlink(missing_ok=True)
        register_artifacts(
            root,
            owner_type="benchmark-rag",
            owner_id=digest,
            retention_class="evidence",
            source_version={"sha256": digest},
            files={"source": {"kind": "source"}},
        )
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.read(digest)
        return digest

    def read(self, digest: str) -> bytes:
        root = self._root(digest)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        uri = artifact_uri("benchmark-rag", digest, "source")
        target = resolve_artifact(root, manifest, uri)
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise PublicationError("pinned source bytes failed hash verification")
        return data


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
    repo: Path,
    revision: str,
    approval: SourceApproval,
    archive: SourceArchive,
) -> PinnedInventory:
    validate_approval(origin_id, approval)
    validate_revision(origin_id, revision)
    if origin_id == PRISM_ORIGIN or not re.fullmatch("[0-9a-f]{40}", revision):
        raise PublicationError("Git inputs require an allowlisted repository and a full pinned commit")
    if _git(repo, "remote", "get-url", "origin") != origin_id:
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
) -> PinnedInventory:
    validate_approval(PRISM_ORIGIN, approval)
    validate_revision(PRISM_ORIGIN, revision)
    if not expected_files or any(path.is_symlink() for path in (root, *root.parents)):
        raise PublicationError("snapshot needs a reviewed hash inventory and a non-symlink root")
    actual = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PublicationError("snapshot symlinks are not supported")
        if path.is_file() and input_format(PRISM_ORIGIN, relative, approval) is not None:
            actual[relative] = path
    if set(actual) != set(expected_files):
        raise PublicationError("snapshot inventory is incomplete or contains unreviewed inputs")
    files = []
    for relative, digest in sorted(expected_files.items()):
        kind = input_format(PRISM_ORIGIN, relative, approval)
        if kind is None:
            raise PublicationError("snapshot contains an unapproved file")
        if hashlib.sha256(actual[relative].read_bytes()).hexdigest() != digest:
            raise PublicationError("snapshot hash does not match the reviewed inventory")
        if archive.put(actual[relative].read_bytes()) != digest:
            raise PublicationError("snapshot changed during collection")
        files.append(InventoryFile(path=relative, sha256=digest, format=kind))
    return PinnedInventory(origin_id=PRISM_ORIGIN, revision=revision, complete=True, files=tuple(files))
