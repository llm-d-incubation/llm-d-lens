"""Single-host rollout policy and sanitized public-document service diagnostics."""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.embedding import CONTRACT, PINNED_FILES
from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceProjectionWriter
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.benchmark_rag.query_contracts import QueryModel
from llm_d_lens.benchmark_rag.search_contracts import EmbeddingContract, EvidenceSearchError, PublicSearchBuild
from llm_d_lens.core.exceptions import ConflictError
from llm_d_lens.infrastructure.storage.paths import storage_path

logger = logging.getLogger(__name__)
_POLICY_LOCK = threading.RLock()
_MAX_POLICY_BYTES = 64 * 1024


class RuntimeSettings(QueryModel):
    search_enabled: bool = Field(default=True, strict=True)
    advanced_search_enabled: bool = Field(default=True, strict=True)
    agentic_mode: Literal["disabled", "shadow", "enabled"] = "disabled"
    agentic_user_ids: tuple[str, ...] = Field(default=(), max_length=100)
    agentic_search_mode: Literal["lexical", "hybrid"] = "lexical"
    agentic_query_enhancement: bool = Field(default=True, strict=True)

    @field_validator("agentic_user_ids")
    @classmethod
    def valid_user_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or value != value.strip() or len(value) > 128 for value in values):
            raise ValueError("Agentic rollout requires nonblank stable user IDs")
        if len(set(values)) != len(values):
            raise ValueError("Agentic rollout user IDs must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def controlled_enablement(self) -> Self:
        if self.agentic_mode != "disabled" and not self.agentic_user_ids:
            raise ValueError("Shadow and enabled modes require an explicit user allowlist")
        return self


class RuntimePolicy(RuntimeSettings):
    generation: int = Field(default=0, ge=0, strict=True)


class RuntimeUpdate(RuntimeSettings):
    expected_generation: int = Field(ge=0, strict=True)


def _policy_path() -> Path:
    return storage_path("data", "public-rag", "runtime.json")


def load_runtime_policy() -> RuntimePolicy:
    try:
        with _policy_path().open("rb") as stream:
            data = stream.read(_MAX_POLICY_BYTES + 1)
        if len(data) > _MAX_POLICY_BYTES:
            raise ValueError("policy exceeds size limit")
        return RuntimePolicy.model_validate_json(data)
    except FileNotFoundError:
        return RuntimePolicy()
    except (OSError, ValueError, ValidationError) as error:
        logger.warning("event=rag_runtime_policy_unavailable reason=%s", type(error).__name__)
        raise EvidenceSearchError("Public document rollout policy is invalid or unavailable") from error


def update_runtime_policy(update: RuntimeUpdate) -> RuntimePolicy:
    path = _policy_path()
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _POLICY_LOCK, path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = load_runtime_policy()
            if current.generation != update.expected_generation:
                raise ConflictError("Public document rollout changed; reload its generation before updating")
            policy = RuntimePolicy(
                **update.model_dump(exclude={"expected_generation"}), generation=current.generation + 1
            )
            with NamedTemporaryFile(dir=path.parent, prefix=".runtime-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(policy.model_dump_json().encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        logger.info("event=rag_runtime_policy_updated generation=%d mode=%s", policy.generation, policy.agentic_mode)
        return policy
    except OSError as error:
        logger.warning("event=rag_runtime_policy_write_failed reason=%s", type(error).__name__)
        raise EvidenceSearchError("Public document rollout policy could not be saved") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SearchTelemetry:
    """Bounded process-local aggregates, never questions, source text or identities."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._duration_ms = 0.0
        self._max_duration_ms = 0.0

    def record(self, mode: str, outcome: str, duration_ms: float, *, stale: bool = False) -> None:
        if mode not in ("lexical", "vector", "hybrid") or outcome not in (
            "ok",
            "empty",
            "degraded",
            "unavailable",
            "disabled",
            "cancelled",
        ):
            raise ValueError("Unknown public search telemetry label")
        with self._lock:
            self._counts["requests"] += 1
            self._counts[f"{mode}:{outcome}"] += 1
            self._counts["stale"] += int(stale)
            self._duration_ms += duration_ms
            self._max_duration_ms = max(self._max_duration_ms, duration_ms)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {**self._counts, "duration_ms_total": self._duration_ms, "duration_ms_max": self._max_duration_ms}


SEARCH_TELEMETRY = SearchTelemetry()


class RuntimeBuildStatus(QueryModel):
    mode: Literal["lexical", "vector"]
    state: Literal["ready", "missing", "unavailable", "recovery_required"]
    active_build: PublicSearchBuild | None = None
    latest_build: PublicSearchBuild | None = None
    latest_status: Literal["building", "ready", "active", "failed"] | None = None
    latest_expected_count: int | None = Field(default=None, ge=1)
    latest_actual_count: int | None = Field(default=None, ge=0)
    rebuild_requested: bool = False
    index_stale: bool = False
    error_code: str | None = None


class RuntimeStatus(QueryModel):
    contract: Literal["benchmark-rag.runtime.v1"] = "benchmark-rag.runtime.v1"
    policy: RuntimePolicy
    opensearch_configured: bool
    api_key_configured: bool
    model_files_present: bool
    embedding_contract: EmbeddingContract = CONTRACT
    writer_mode: str
    builds: tuple[RuntimeBuildStatus, ...]
    metrics_scope: Literal["process_since_start"] = "process_since_start"
    search_metrics: dict[str, int | float]


async def runtime_status(*, client: httpx.AsyncClient | None = None) -> RuntimeStatus:
    from llm_d_lens.benchmark_rag.search import configured_repository

    policy = await run_in_threadpool(load_runtime_policy)
    configured = bool(os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_URL"))
    model_path = Path(
        os.environ.get("LENS_PUBLIC_RAG_MODEL_PATH")
        or storage_path("cache", "models", "multilingual-e5-small", CONTRACT.model_revision)
    )
    statuses = []
    if configured:
        for vector in (False, True):
            metadata = None
            try:
                repository = configured_repository(client=client)
                metadata = await run_in_threadpool(DocumentRepository.from_environment)
                lifecycle = DocumentLifecycle(metadata)
                writer = OpenSearchEvidenceProjectionWriter(repository)
                pending = await run_in_threadpool(metadata.read_record, lifecycle._pending_key(vector))
                if pending is not None and pending[0].get("status") != "settled":
                    statuses.append(
                        RuntimeBuildStatus(
                            mode="vector" if vector else "lexical",
                            state="recovery_required",
                            error_code="activation_unsettled",
                        )
                    )
                    continue
                target = await writer.alias_target(vector)
                active = None
                stale = False
                if target is not None:
                    active = await writer.index_build(target)
                    record = await run_in_threadpool(lifecycle.get, active.build_id)
                    if record is None or record.build != active or record.status != "active":
                        raise EvidenceSearchError("Unverified active document build")
                    scopes = await run_in_threadpool(
                        metadata.public_search_scopes, tuple(scope.origin_id for scope in active.scopes)
                    )
                    stale = scopes != active.scopes
                fence = await run_in_threadpool(metadata.read_record, "fence:vector" if vector else "fence:lexical")
                latest = None
                if fence is not None:
                    latest_id = fence[0].get("latest_build_id")
                    if not isinstance(latest_id, str):
                        raise EvidenceSearchError("Invalid latest document build")
                    latest = await run_in_threadpool(lifecycle.get, latest_id)
                    if latest is None:
                        raise EvidenceSearchError("Missing latest document build")
                statuses.append(
                    RuntimeBuildStatus(
                        mode="vector" if vector else "lexical",
                        state="ready" if active else "missing",
                        active_build=active,
                        latest_build=latest.build if latest else None,
                        latest_status=latest.status if latest else None,
                        index_stale=stale,
                        latest_expected_count=latest.expected_count if latest else None,
                        latest_actual_count=latest.actual_count if latest else None,
                        rebuild_requested=await run_in_threadpool(lifecycle.rebuild_requested, vector=vector),
                    )
                )
            except (EvidenceSearchError, PublicationError, OSError, ValidationError) as error:
                logger.warning("event=rag_runtime_status_failed reason=%s", type(error).__name__)
                statuses.append(
                    RuntimeBuildStatus(
                        mode="vector" if vector else "lexical", state="unavailable", error_code="search_unavailable"
                    )
                )
            finally:
                if metadata is not None:
                    await run_in_threadpool(metadata.close)
    return RuntimeStatus(
        policy=policy,
        opensearch_configured=configured,
        api_key_configured=bool(os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_API_KEY")),
        model_files_present=all((model_path / name).is_file() for name in PINNED_FILES),
        writer_mode="single-host"
        if os.environ.get("LENS_PUBLIC_RAG_WRITER_MODE", "single-host") == "single-host"
        else "unsupported",
        builds=tuple(statuses),
        search_metrics=SEARCH_TELEMETRY.snapshot(),
    )
