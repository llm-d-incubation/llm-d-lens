"""Provider-neutral provenance contracts for optional document evidence."""

from typing import Literal

from pydantic import Field

from llm_d_lens.benchmark_rag.query_contracts import QueryModel


class EmbeddingContract(QueryModel):
    model_id: str
    model_revision: str
    dimension: int = Field(ge=1)
    max_tokens: int = Field(ge=8)
    encoding_version: str
    chunking_version: str


class SearchBranchStatus(QueryModel):
    status: Literal["ok", "failed", "skipped"]
    candidate_count: int = Field(default=0, ge=0, le=200)
    error: str | None = None
