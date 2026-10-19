"""Versioned portable RAG backups; never an alternate application data store."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from llm_d_lens.benchmark_rag.contracts import ItemKind
from llm_d_lens.benchmark_rag.publication_contracts import PublicationModel, content_digest

RAG_SCHEMA_REVISION = "7a12c8e49b60"


class BackupManifest(PublicationModel):
    contract: Literal["benchmark-rag.backup.v1"] = "benchmark-rag.backup.v1"
    schema_revision: Literal["7a12c8e49b60"] = RAG_SCHEMA_REVISION
    records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(gt=0)
    counts: dict[ItemKind, int]
    blobs: dict[str, int]

    @property
    def backup_id(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class RestoreResult(PublicationModel):
    backup_id: str
    inserted: int
    updated_sources: int
    total_rows: int
