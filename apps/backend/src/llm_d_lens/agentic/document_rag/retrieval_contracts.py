"""Authority-checked document matches, never numeric SQL query results."""

from dataclasses import dataclass

from llm_d_lens.agentic.document_rag.contracts import DocumentRecord


@dataclass(frozen=True)
class DocumentMatch:
    item: DocumentRecord
    experiment: None
    source: DocumentRecord
    manifest_id: str
    generation: int
    related_experiments: tuple[()] = ()


@dataclass(frozen=True)
class DocumentSlice:
    matches: tuple[DocumentMatch, ...]
    total: int
