"""Target-local RAG refresh, status, rollback, rebuild and portable migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.document_transfer import DocumentBackupService
from llm_d_lens.benchmark_rag.prism import PrismSnapshotImporter
from llm_d_lens.benchmark_rag.prism_initial import INITIAL_EXPERIMENTS, INITIAL_FILES, INITIAL_REVISION
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError, SourceApproval
from llm_d_lens.benchmark_rag.refresh import RagRefreshService, RefreshPlan
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN, SourceArchive
from llm_d_lens.benchmark_rag.transfer import MAX_BACKUP_BYTES, RagBackupService
from llm_d_lens.core.exceptions import DomainError
from llm_d_lens.db.dao.rag_item import RagItemDaoError
from llm_d_lens.db.dao.rag_transfer import transfer_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("search-status", "search-rebuild", "search-recover", "source-enable", "source-disable"):
        command = subcommands.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        if name in ("search-rebuild", "search-recover"):
            command.add_argument("--mode", choices=("lexical", "vector"), default="lexical")
        if name == "search-rebuild":
            command.add_argument("--origin", action="append", required=True)
        if name == "search-recover":
            command.add_argument("--requests-quiesced", action="store_true", required=True)
        if name in ("source-enable", "source-disable"):
            command.add_argument("--origin", required=True)
            command.add_argument("--expected-generation", type=int, required=True)
    for name in ("rebuild", "backup", "restore", "verify"):
        command = subcommands.add_parser(name)
        if name != "rebuild":
            command.add_argument("--max-bytes", type=int, default=MAX_BACKUP_BYTES)
            command.add_argument("--structured", action="store_true", help="Transfer SQL public benchmark records")
        if name == "verify":
            command.add_argument("--bundle", type=Path, required=True)
            continue
        command.add_argument("--database-url-env", default="LLM_D_BENCH_DATABASE_URL")
        command.add_argument("--schema", default="public")
        command.add_argument("--data-dir", type=Path, required=True)
        if name in ("rebuild", "restore"):
            command.add_argument(
                "--initialize",
                action="store_true",
                help="Initialize structured benchmark storage or the document management index; never stamp migrations",
            )
        if name == "rebuild":
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--approval", type=Path, required=True)
        elif name == "backup":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--bundle", type=Path, required=True)
    for name in ("refresh", "status", "rollback"):
        command = subcommands.add_parser(name)
        command.add_argument("--database-url-env", default="LLM_D_BENCH_DATABASE_URL")
        command.add_argument("--schema", default="public")
        command.add_argument("--data-dir", type=Path, required=True)
        if name == "refresh":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument(
                "--source",
                type=Path,
                help="Optional pinned Git checkout; required local directory for Prism or reviewed web snapshots.",
            )
            command.add_argument("--trigger", choices=("manual", "commit", "periodic"), default="manual")
            command.add_argument("--event-revision")
        else:
            command.add_argument("--origin", required=True)
            if name == "rollback":
                command.add_argument("--manifest-id", required=True)
                command.add_argument("--expected-generation", type=int, required=True)
    return parser


async def _search_command(args: argparse.Namespace) -> None:
    from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
    from llm_d_lens.benchmark_rag.embedding import configured_encoder
    from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceProjectionWriter
    from llm_d_lens.benchmark_rag.runtime import runtime_status
    from llm_d_lens.benchmark_rag.search import configured_repository

    if args.command == "search-status":
        print((await runtime_status()).model_dump_json())
        return
    repository = configured_repository()
    metadata = DocumentRepository.from_environment()
    try:
        metadata.prepare()
        if args.command == "search-rebuild":
            encoder = configured_encoder() if args.mode == "vector" else None
            result = await PublicRagIndexBuilder(repository, dao=metadata).build(tuple(args.origin), encoder=encoder)
            print(result.model_dump_json())
        else:
            await DocumentLifecycle(metadata).reconcile(
                OpenSearchEvidenceProjectionWriter(repository),
                vector=args.mode == "vector",
                requests_quiesced=args.requests_quiesced,
            )
            print(json.dumps({"status": "settled", "mode": args.mode}))
    finally:
        metadata.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            verifier = RagBackupService if args.structured else DocumentBackupService
            metadata = verifier.verify(args.bundle, max_bytes=args.max_bytes)
            print(json.dumps({"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True))
            return 0
        document_plan = RefreshPlan.model_validate_json(args.plan.read_bytes()) if args.command == "refresh" else None
        source_origin = (
            document_plan.inventory.origin_id if document_plan is not None else getattr(args, "origin", None)
        )
        root = args.data_dir.expanduser().absolute()
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise PublicationError("evidence data directory must not use symlinks")
        if args.command == "backup" and not root.is_dir():
            raise PublicationError("source evidence data directory does not exist")
        os.environ["LENS_DATA_DIR"] = str(root)
        if args.command.startswith("search-"):
            asyncio.run(_search_command(args))
            return 0
        if args.command in ("source-enable", "source-disable"):
            repository = DocumentRepository.from_environment()
            try:
                repository.prepare()
                repository.set_enabled(
                    args.origin, args.command == "source-enable", expected_generation=args.expected_generation
                )
                head = repository.head(args.origin)
                print(
                    json.dumps(
                        {"origin_id": args.origin, "enabled": head.state.enabled, "generation": head.state.generation}
                    )
                )
            finally:
                repository.close()
            return 0
        if (
            not getattr(args, "structured", False)
            and source_origin != PRISM_ORIGIN
            and args.command in ("backup", "restore", "refresh", "status", "rollback")
        ):
            repository = DocumentRepository.from_environment()
            try:
                repository.prepare(initialize=getattr(args, "initialize", False))
                if args.command == "backup":
                    service = DocumentBackupService(repository, SourceArchive())
                    metadata = service.backup(args.output, max_bytes=args.max_bytes)
                    print(
                        json.dumps(
                            {"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True
                        )
                    )
                elif args.command == "restore":
                    service = DocumentBackupService(repository, SourceArchive())
                    result = service.restore(args.bundle, max_bytes=args.max_bytes)
                    print(result.model_dump_json())
                elif args.command == "refresh":
                    if document_plan is None:
                        raise PublicationError("document refresh requires a validated plan")
                    result = RagRefreshService(RagPublicationService(dao=repository)).run(
                        document_plan,
                        args.source,
                        trigger=args.trigger,
                        event_revision=args.event_revision,
                    )
                    print(result.model_dump_json())
                elif args.command == "status":
                    print(repository.head(args.origin).model_dump_json())
                else:
                    result = RagPublicationService(dao=repository).rollback(
                        args.origin,
                        args.manifest_id,
                        expected_generation=args.expected_generation,
                    )
                    print(result.model_dump_json())
            finally:
                repository.close()
            return 0
        database_url = os.environ.get(args.database_url_env)
        if not database_url:
            raise PublicationError("explicit database URL environment variable is missing")
        with transfer_database(database_url, schema=args.schema) as dao:
            dao.prepare(initialize=getattr(args, "initialize", False))
            if args.command == "rebuild":
                approval = SourceApproval.model_validate_json(args.approval.read_bytes())
                result = PrismSnapshotImporter(RagPublicationService(dao=dao)).import_snapshot(
                    args.snapshot,
                    INITIAL_REVISION,
                    INITIAL_FILES,
                    approval,
                    expected_experiments=INITIAL_EXPERIMENTS,
                )
                print(result.model_dump_json())
            elif args.command == "refresh":
                plan = document_plan or RefreshPlan.model_validate_json(args.plan.read_bytes())
                result = RagRefreshService(RagPublicationService(dao=dao)).run(
                    plan, args.source, trigger=args.trigger, event_revision=args.event_revision
                )
                print(result.model_dump_json())
            elif args.command == "status":
                print(dao.head(args.origin).model_dump_json())
            elif args.command == "rollback":
                result = RagPublicationService(dao=dao).rollback(
                    args.origin, args.manifest_id, expected_generation=args.expected_generation
                )
                print(result.model_dump_json())
            elif args.command == "backup":
                metadata = RagBackupService(dao).backup(args.output, max_bytes=args.max_bytes)
                print(json.dumps({"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True))
            else:
                result = RagBackupService(dao).restore(args.bundle, max_bytes=args.max_bytes)
                print(result.model_dump_json())
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
