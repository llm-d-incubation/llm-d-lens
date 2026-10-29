"""Public-only document search contracts; scores do not assert performance."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from llm_d_lens.benchmark_rag.query_contracts import PUBLIC_ORIGINS, Citation, QueryFilters, QueryModel
from llm_d_lens.core.exceptions import DomainError


class EvidenceSearchError(DomainError):
    code = "rag_search_unavailable"
    status_code = 503


class EmbeddingContractMismatchError(EvidenceSearchError):
    code = "rag_embedding_contract_mismatch"


class PublicSearchScope(QueryModel):
    origin_id: str
    manifest_id: str
    generation: int = Field(ge=0)


class EmbeddingContract(QueryModel):
    model_id: str = "intfloat/multilingual-e5-small"
    model_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    dimension: int = Field(default=384, ge=1)
    max_tokens: int = Field(default=512, ge=8)
    encoding_version: str = "e5-prefix-mask-mean-l2-v1"
    chunking_version: str = "e5-lines-offsets-v1"


class PublicSearchRequest(QueryModel):
    question: str = Field(min_length=1, max_length=2048)
    origins: tuple[str, ...] = Field(default=PUBLIC_ORIGINS, min_length=1, max_length=len(PUBLIC_ORIGINS))
    limit: int = Field(default=10, ge=1, le=50)
    mode: Literal["lexical", "vector", "hybrid"] = "lexical"
    query_enhancement: bool = Field(
        default=True,
        description="Enable controlled terminology and explicit-source weighting in hybrid mode only.",
    )

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value.strip()

    @field_validator("origins")
    @classmethod
    def approved_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return QueryFilters(origins=value).origins


class PublicPassageDocument(QueryModel):
    chunk_id: str
    evidence_id: str
    origin_id: str
    manifest_id: str
    publication_generation: int = Field(ge=0)
    source_revision: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str
    title: str
    heading_path: tuple[str, ...]
    text: str = Field(min_length=1)
    access_classification: Literal["verified_public"] = "verified_public"
    identifiers: tuple[str, ...] = ()
    chunk_start: int | None = Field(default=None, ge=0)
    chunk_end: int | None = Field(default=None, ge=1)
    chunk_index: int | None = Field(default=None, ge=0)
    chunking_version: str | None = None
    embedding_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    embedding_context: str = ""
    embedding: tuple[float, ...] | None = None

    @model_validator(mode="after")
    def complete_vector_chunk(self) -> Self:
        fields = (
            self.chunk_start,
            self.chunk_end,
            self.chunk_index,
            self.chunking_version,
            self.embedding_input_sha256,
        )
        if any(value is not None for value in fields):
            if (
                any(value is None for value in fields)
                or self.chunk_start is None
                or self.chunk_end is None
                or self.chunk_end <= self.chunk_start
            ):
                raise ValueError("vector chunks need complete, ordered parent offsets")
            if len(self.text) != self.chunk_end - self.chunk_start:
                raise ValueError("chunk offsets must exactly locate its text")
        if self.embedding is not None and (not self.embedding or self.chunk_start is None):
            raise ValueError("vectors require a located chunk")
        return self


class PublicSearchBuild(QueryModel):
    contract: Literal["benchmark-rag.search-index.v1"] = "benchmark-rag.search-index.v1"
    build_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    scopes: tuple[PublicSearchScope, ...] = Field(min_length=1)
    document_count: int = Field(ge=1)
    retrieval_mode: Literal["opensearch_lexical", "opensearch_vector"] = "opensearch_lexical"
    embedding_contract: EmbeddingContract | None = None

    @model_validator(mode="after")
    def consistent_mode(self) -> Self:
        if (self.retrieval_mode == "opensearch_vector") != (self.embedding_contract is not None):
            raise ValueError("vector builds require an embedding contract; lexical builds must not have one")
        return self


class PublicSearchHit(QueryModel):
    document: PublicPassageDocument
    score: float = Field(ge=0)


class PublicSearchResult(QueryModel):
    evidence_id: str
    chunk_id: str
    title: str
    heading_path: tuple[str, ...]
    text: str
    citation: Citation
    matched_branches: tuple[Literal["lexical", "vector"], ...] = ("lexical",)
    lexical_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    chunk_start: int | None = None
    chunk_end: int | None = None
    score: float = Field(ge=0)
    fusion_score: float | None = Field(default=None, ge=0)


class SearchBranchStatus(QueryModel):
    status: Literal["ok", "failed", "skipped"]
    candidate_count: int = Field(default=0, ge=0, le=200)
    error: str | None = None


class SearchQueryEnhancement(QueryModel):
    policy: Literal["controlled-v1"] = "controlled-v1"
    enabled: bool
    effective_question: str = Field(min_length=1)
    added_terms: tuple[str, ...] = Field(default=(), max_length=8)
    source_hints: tuple[str, ...] = ()


class PublicSearchResponse(QueryModel):
    contract: Literal["benchmark-rag.search.v1"] = "benchmark-rag.search.v1"
    retrieval_mode: Literal["opensearch_lexical", "opensearch_vector", "hybrid"] = "opensearch_lexical"
    embedding_contract: EmbeddingContract | None = None
    retrieval_build_id: str
    index_stale: bool
    affected_origins: tuple[str, ...] = ()
    candidate_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    truncated: bool
    results: tuple[PublicSearchResult, ...]
    degraded_reason: str | None = None
    branch_status: dict[Literal["lexical", "vector"], SearchBranchStatus] = Field(default_factory=dict)
    refill_count: int = Field(default=0, ge=0, le=2)
    rebuild_required: bool = False
    query_enhancement: SearchQueryEnhancement | None = None
