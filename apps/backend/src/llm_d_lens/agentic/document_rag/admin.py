"""Document-only administration; Prism administration belongs to public_benchmark_record."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from llm_d_lens.agentic.document_rag.document_repository import DocumentRepository
from llm_d_lens.agentic.document_rag.document_transfer import MAX_BACKUP_BYTES, DocumentBackupService
from llm_d_lens.agentic.document_rag.publication import RagPublicationService
from llm_d_lens.agentic.document_rag.publication_contracts import PublicationError
from llm_d_lens.agentic.document_rag.refresh import RagRefreshService, RefreshPlan
from llm_d_lens.agentic.public_benchmark_record.sources import SourceArchive
from llm_d_lens.core.exceptions import DomainError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "backup",
        "restore",
        "verify",
        "refresh",
        "status",
        "rollback",
        "search-status",
        "search-rebuild",
        "search-recover",
        "source-enable",
        "source-disable",
    ):
        command = commands.add_parser(name)
        if name != "verify":
            command.add_argument("--data-dir", type=Path, required=True)
        if name in ("backup", "restore", "verify"):
            command.add_argument("--max-bytes", type=int, default=MAX_BACKUP_BYTES)
        if name in ("restore", "verify"):
            command.add_argument("--bundle", type=Path, required=True)
        if name == "backup":
            command.add_argument("--output", type=Path, required=True)
        if name == "restore":
            command.add_argument("--initialize", action="store_true")
        if name == "refresh":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--source", type=Path)
            command.add_argument("--trigger", choices=("manual", "commit", "periodic"), default="manual")
            command.add_argument("--event-revision")
        if name in ("status", "rollback", "source-enable", "source-disable"):
            command.add_argument("--origin", required=True)
        if name in ("rollback", "source-enable", "source-disable"):
            command.add_argument("--expected-generation", type=int, required=True)
        if name == "rollback":
            command.add_argument("--manifest-id", required=True)
        if name in ("search-rebuild", "search-recover"):
            command.add_argument("--mode", choices=("lexical", "vector"), default="lexical")
        if name == "search-rebuild":
            command.add_argument("--origin", action="append", required=True)
        if name == "search-recover":
            command.add_argument("--requests-quiesced", action="store_true", required=True)
    return parser


async def _search_command(args: argparse.Namespace) -> None:
    from llm_d_lens.agentic.document_rag.document_lifecycle import DocumentLifecycle
    from llm_d_lens.agentic.document_rag.embedding import configured_encoder
    from llm_d_lens.agentic.document_rag.indexing import PublicRagIndexBuilder
    from llm_d_lens.agentic.document_rag.opensearch_evidence_search import OpenSearchEvidenceProjectionWriter
    from llm_d_lens.agentic.document_rag.runtime import runtime_status
    from llm_d_lens.agentic.document_rag.search import configured_repository

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
            metadata = DocumentBackupService.verify(args.bundle, max_bytes=args.max_bytes)
            print(json.dumps({"backup_id": metadata.backup_id, **metadata.model_dump(mode="json")}, sort_keys=True))
            return 0
        root = args.data_dir.expanduser().absolute()
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise PublicationError("evidence data directory must not use symlinks")
        if args.command == "backup" and not root.is_dir():
            raise PublicationError("source evidence data directory does not exist")
        os.environ["LENS_DATA_DIR"] = str(root)
        if args.command.startswith("search-"):
            asyncio.run(_search_command(args))
            return 0
        repository = DocumentRepository.from_environment()
        try:
            repository.prepare(initialize=getattr(args, "initialize", False))
            publication = RagPublicationService(repository)
            if args.command == "backup":
                result = DocumentBackupService(repository, SourceArchive()).backup(
                    args.output, max_bytes=args.max_bytes
                )
            elif args.command == "restore":
                result = DocumentBackupService(repository, SourceArchive()).restore(
                    args.bundle, max_bytes=args.max_bytes
                )
            elif args.command == "refresh":
                plan = RefreshPlan.model_validate_json(args.plan.read_bytes())
                result = RagRefreshService(publication).run(
                    plan, args.source, trigger=args.trigger, event_revision=args.event_revision
                )
            elif args.command == "rollback":
                result = publication.rollback(
                    args.origin, args.manifest_id, expected_generation=args.expected_generation
                )
            else:
                if args.command in ("source-enable", "source-disable"):
                    repository.set_enabled(
                        args.origin, args.command == "source-enable", expected_generation=args.expected_generation
                    )
                result = repository.head(args.origin)
            print(result.model_dump_json())
        finally:
            repository.close()
        return 0
    except (DomainError, PublicationError, ValidationError, OSError) as error:
        message = "record validation failed" if isinstance(error, ValidationError) else str(error)
        print(f"document_rag_admin_failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
