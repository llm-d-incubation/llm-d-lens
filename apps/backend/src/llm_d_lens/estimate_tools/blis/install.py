"""Opt-in installation of administrator-supplied, digest-locked BLIS artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from llm_d_lens.infrastructure.storage.paths import storage_path

from ..models import Problem
from . import service


def installation_root() -> Path:
    return storage_path("cache", "estimate-tools", "managed-blis-762f1605")


def installed_profile(root: Path | None = None) -> service.RuntimeProfile | None:
    root = root or installation_root()
    if not root.exists():
        return None
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError("Managed BLIS installation must not traverse symlinks")
    record = root / "installation.json"
    if record.is_symlink():
        raise ValueError("Managed BLIS installation record must be a regular file")
    try:
        manifest = json.loads(record.read_text(encoding="utf-8"))
        profile = service.provisioned_profile(binary=root / "blis", inputs=root / "inputs")
        expected = {
            "schema_version": "blis-installation.v1",
            "profile_id": profile.id,
            "binary_digest": profile.binary_digest,
            "inputs_digest": profile.inputs_digest,
            "execution_verified": True,
        }
        if manifest != expected:
            raise ValueError("Managed BLIS installation record differs from its lock")
    except (OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Managed BLIS installation record is unreadable or invalid") from error
    return profile


async def install_runtime(binary: Path, inputs: Path, *, root: Path | None = None) -> service.RuntimeProfile:
    if os.name != "posix":
        raise ValueError("Managed BLIS execution requires POSIX process groups")
    root = (root or installation_root()).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("Installation already exists; use check or select a new cache root")
    if any(parent.is_symlink() for parent in root.parents):
        raise ValueError("Installation must not traverse symlinks")
    source = service.provisioned_profile(binary=binary.expanduser().absolute(), inputs=inputs.expanduser().absolute())
    available = service.availability(source)
    if not available.available:
        raise ValueError(available.reason.code if available.reason else "runtime_unavailable")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".blis-install-", dir=root.parent) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir(mode=0o700)
        shutil.copyfile(source.binary, staging / "blis")
        (staging / "blis").chmod(0o700)
        shutil.copytree(source.inputs, staging / "inputs")
        profile = service.provisioned_profile(binary=staging / "blis", inputs=staging / "inputs")
        directory = Path(temporary) / "verification"
        execution = await service._execute_bundle(profile.verified_request, profile, directory, asyncio.Event())
        if isinstance(execution, Problem):
            raise ValueError(f"BLIS installation verification failed: {execution.code}")
        evaluation = service._parse_result(
            profile.verified_request,
            profile,
            directory,
            (directory / "stdout.log").read_bytes(),
            returncode=execution.returncode,
            execution_status=execution.status,
            output_truncated=execution.output_truncated,
        )
        if evaluation.status != "succeeded" or evaluation.constraint_assessment != "satisfied":
            code = evaluation.error.code if evaluation.error else evaluation.constraint_assessment
            raise ValueError(f"BLIS installation verification failed: {code}")
        manifest = {
            "schema_version": "blis-installation.v1",
            "profile_id": profile.id,
            "binary_digest": profile.binary_digest,
            "inputs_digest": profile.inputs_digest,
            "execution_verified": True,
        }
        (staging / "installation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if root.exists() or root.is_symlink():
            raise ValueError("Installation destination was created during verification")
        staging.rename(root)
    profile = installed_profile(root)
    assert profile is not None
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Copy locked artifacts after a real baseline verification")
    install.add_argument("--binary", type=Path, required=True)
    install.add_argument("--inputs", type=Path, required=True)
    commands.add_parser("check", help="Check the installed runtime record and locked artifacts")
    arguments = parser.parse_args()
    try:
        profile = (
            asyncio.run(install_runtime(arguments.binary, arguments.inputs))
            if arguments.command == "install"
            else installed_profile()
        )
        support = service.availability(profile)
        print(support.model_dump_json(indent=2))
        return 0 if support.available else 1
    except (OSError, ValueError) as error:
        parser.exit(1, f"BLIS provisioning failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
