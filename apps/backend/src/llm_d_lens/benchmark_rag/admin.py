"""Operator-approved SQL public benchmark import, publication and portable backup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from llm_d_lens.benchmark_rag.prism import PrismSnapshotImporter
from llm_d_lens.benchmark_rag.prism_initial import INITIAL_EXPERIMENTS, INITIAL_FILES, INITIAL_REVISION
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError, SourceApproval
from llm_d_lens.benchmark_rag.refresh import RagRefreshService, RefreshPlan
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN
from llm_d_lens.benchmark_rag.transfer import MAX_BACKUP_BYTES, RagBackupService
from llm_d_lens.core.exceptions import DomainError
from llm_d_lens.db.dao.rag_item import RagItemDaoError
from llm_d_lens.db.dao.rag_transfer import transfer_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("rebuild", "backup", "restore", "verify", "refresh", "status", "rollback"):
        command = subcommands.add_parser(name)
        if name in ("backup", "restore", "verify"):
            command.add_argument("--max-bytes", type=int, default=MAX_BACKUP_BYTES)
            command.add_argument("--structured", action="store_true", help="Transfer SQL public benchmark records")
        if name in ("verify", "restore"):
            command.add_argument("--bundle", type=Path, required=True)
        if name == "verify":
            continue
        command.add_argument("--database-url-env", default="LLM_D_BENCH_DATABASE_URL")
        command.add_argument("--schema", default="public")
        command.add_argument("--data-dir", type=Path, required=True)
        if name in ("rebuild", "restore"):
            command.add_argument(
                "--initialize",
                action="store_true",
                help="Initialize only SQL benchmark storage; never stamp migrations",
            )
        if name == "rebuild":
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--approval", type=Path, required=True)
        elif name == "backup":
            command.add_argument("--output", type=Path, required=True)
        elif name == "refresh":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--source", type=Path, help="Pinned local Prism snapshot directory")
            command.add_argument("--trigger", choices=("manual", "commit", "periodic"), default="manual")
            command.add_argument("--event-revision")
        elif name in ("status", "rollback"):
            command.add_argument("--origin", choices=(PRISM_ORIGIN,), required=True)
            if name == "rollback":
                command.add_argument("--manifest-id", required=True)
                command.add_argument("--expected-generation", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            metadata = RagBackupService.verify(args.bundle, max_bytes=args.max_bytes)
            print(json.dumps({"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True))
            return 0
        plan = RefreshPlan.model_validate_json(args.plan.read_bytes()) if args.command == "refresh" else None
        if plan is not None and plan.inventory.origin_id != PRISM_ORIGIN:
            raise PublicationError("only Prism SQL benchmark refresh is available; document provider is not installed")
        root = args.data_dir.expanduser().absolute()
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise PublicationError("evidence data directory must not use symlinks")
        if args.command == "backup" and not root.is_dir():
            raise PublicationError("source evidence data directory does not exist")
        os.environ["LENS_DATA_DIR"] = str(root)
        database_url = os.environ.get(args.database_url_env)
        if not database_url:
            raise PublicationError("explicit database URL environment variable is missing")
        with transfer_database(database_url, schema=args.schema) as dao:
            dao.prepare(initialize=getattr(args, "initialize", False))
            publication = RagPublicationService(dao=dao)
            if args.command == "rebuild":
                approval = SourceApproval.model_validate_json(args.approval.read_bytes())
                result = PrismSnapshotImporter(publication).import_snapshot(
                    args.snapshot,
                    INITIAL_REVISION,
                    INITIAL_FILES,
                    approval,
                    expected_experiments=INITIAL_EXPERIMENTS,
                )
                print(result.model_dump_json())
            elif args.command == "refresh":
                assert plan is not None
                result = RagRefreshService(publication).run(
                    plan, args.source, trigger=args.trigger, event_revision=args.event_revision
                )
                print(result.model_dump_json())
            elif args.command == "status":
                print(dao.head(args.origin).model_dump_json())
            elif args.command == "rollback":
                result = publication.rollback(
                    args.origin, args.manifest_id, expected_generation=args.expected_generation
                )
                print(result.model_dump_json())
            elif args.command == "backup":
                metadata = RagBackupService(dao).backup(args.output, max_bytes=args.max_bytes)
                print(json.dumps({"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True))
            else:
                print(RagBackupService(dao).restore(args.bundle, max_bytes=args.max_bytes).model_dump_json())
        return 0
    except (DomainError, PublicationError, RagItemDaoError, ValidationError, OSError) as error:
        message = "record validation failed" if isinstance(error, ValidationError) else str(error)
        if args.command == "refresh":
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "command": "refresh",
                        "trigger": args.trigger,
                        "error_type": type(error).__name__,
                        "message": message,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"rag_admin_failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
