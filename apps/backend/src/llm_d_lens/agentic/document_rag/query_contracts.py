"""Independent document filters and the existing mixed-query wire boundary."""

from pydantic import Field, field_validator

from llm_d_lens.agentic.document_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, WEB_SOURCE_PATHS, validate_path
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QUERY_PERMISSION as QUERY_PERMISSION,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    Abstention as Abstention,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    Citation as Citation,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    EvidenceType as EvidenceType,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryEvidence as QueryEvidence,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryFilters as RecordFilters,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryModel as QueryModel,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryRequest as RecordRequest,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryResponse as RecordResponse,
)
from llm_d_lens.agentic.public_benchmark_record.sources import PRISM_ORIGIN

PUBLIC_ORIGINS = (LLM_D_ORIGIN, BENCHMARK_ORIGIN, *WEB_SOURCE_PATHS)


class QueryFilters(QueryModel):
    origins: tuple[str, ...] = Field(default=PUBLIC_ORIGINS, min_length=1, max_length=len(PUBLIC_ORIGINS))
    source_revision: str | None = Field(default=None, min_length=1, max_length=64)
    source_path: str | None = Field(default=None, min_length=1, max_length=1024)
    normalized: bool = False

    @field_validator("origins")
    @classmethod
    def approved_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or set(value) - set(PUBLIC_ORIGINS):
            raise ValueError("origins must be unique approved document sources")
        return value

    @field_validator("source_path")
    @classmethod
    def canonical_path(cls, value: str | None) -> str | None:
        if value is not None:
            validate_path(value)
        return value


class CompatibilityFilters(RecordFilters):
    origins: tuple[str, ...] = (PRISM_ORIGIN, *PUBLIC_ORIGINS)

    @field_validator("origins")
    @classmethod
    def approved_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value) or set(value) - {PRISM_ORIGIN, *PUBLIC_ORIGINS}:
            raise ValueError("origins must be unique approved public sources")
        return value


class QueryRequest(RecordRequest):
    filters: CompatibilityFilters = Field(default_factory=CompatibilityFilters)


class QueryResponse(RecordResponse):
    filters: CompatibilityFilters
