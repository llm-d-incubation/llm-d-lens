"""Single-host serialized publication with the OpenSearch alias as read authority."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from anyio import from_thread
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from llm_d_lens.benchmark_rag.search_contracts import EvidenceSearchError, PublicSearchBuild
from llm_d_lens.core.exceptions import ConflictError

if TYPE_CHECKING:
    from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceProjectionWriter


@dataclass(frozen=True)
class DocumentBuildRecord:
    build: PublicSearchBuild
    physical_index: str
    status: str
    expected_count: int
    actual_count: int | None
    error: str | None


class DocumentLifecycle:
    def __init__(self, repository: DocumentRepository) -> None:
        self.repository = repository

    def create(self, build: PublicSearchBuild, physical_index: str) -> DocumentBuildRecord:
        if physical_index != "public-rag-v1-" + build.build_id:
            raise ValueError("build requires its fixed physical index")
        with self.repository.publication_lock():
            fence_key = "fence:vector" if build.embedding_contract is not None else "fence:lexical"
            fence = self.repository.read_record(fence_key)
            value = {"latest_build_id": build.build_id}
            if fence is None:
                self.repository.create_record(fence_key, value)
            else:
                self.repository.replace_record(fence_key, value, fence[1], fence[2])
            self.repository.create_record(
                "build:" + build.build_id,
                {
                    "build": build.model_dump(mode="json"),
                    "physical_index": physical_index,
                    "status": "building",
                    "actual_count": None,
                    "error": None,
                },
            )
        record = self.get(build.build_id)
        assert record is not None
        return record

    def get(self, build_id: str) -> DocumentBuildRecord | None:
        record = self.repository.read_record("build:" + build_id)
        if record is None:
            return None
        try:
            value = record[0]
            build = PublicSearchBuild.model_validate(value["build"])
            if build.build_id != build_id or value["physical_index"] != "public-rag-v1-" + build_id:
                raise ValueError("inconsistent physical build identity")
            return DocumentBuildRecord(
                build,
                value["physical_index"],
                value["status"],
                build.document_count,
                value["actual_count"],
                value["error"],
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise EvidenceSearchError("Public document build identity is inconsistent") from error

    def _update(self, build_id: str, **changes: object) -> None:
        key = "build:" + build_id
        record = self.repository.read_record(key)
        if record is None:
            raise EvidenceSearchError("Public document build history is missing")
        value, sequence, term = record
        self.repository.replace_record(key, {**value, **changes}, sequence, term)

    def ready(self, build_id: str, actual_count: int) -> None:
        with self.repository.publication_lock():
            record = self.get(build_id)
            if record is None or record.status != "building" or actual_count != record.expected_count:
                raise EvidenceSearchError("Public document build count or state is inconsistent")
            self._update(build_id, status="ready", actual_count=actual_count)

    def fail(self, build_id: str) -> None:
        with self.repository.publication_lock():
            record = self.get(build_id)
            if record is not None and record.status in ("building", "ready"):
                self._update(build_id, status="failed", error="build_failed")

    def request_rebuild(self, alias: str) -> None:
        if alias not in ("public-rag-active", "public-rag-vector-shadow"):
            raise ValueError("rebuild requires an owned alias")
        with self.repository.publication_lock():
            key = "rebuild:" + alias
            record = self.repository.read_record(key)
            value = {"requested": True}
            if record is None:
                self.repository.create_record(key, value)
            else:
                self.repository.replace_record(key, value, record[1], record[2])

    def rebuild_requested(self, *, vector: bool = False) -> bool:
        alias = "public-rag-vector-shadow" if vector else "public-rag-active"
        record = self.repository.read_record("rebuild:" + alias)
        if record is not None and type(record[0].get("requested")) is not bool:
            raise EvidenceSearchError("Public rebuild request metadata is invalid")
        return record is not None and record[0]["requested"] is True

    async def activate(self, build_id: str, writer: OpenSearchEvidenceProjectionWriter) -> None:
        await run_in_threadpool(self._activate, build_id, writer)

    def _activate(self, build_id: str, writer: OpenSearchEvidenceProjectionWriter) -> None:
        with self.repository.publication_lock():
            record = self.get(build_id)
            if record is None or record.status != "ready" or record.actual_count != record.expected_count:
                raise EvidenceSearchError("Only a complete ready document build may be activated")
            vector = record.build.embedding_contract is not None
            self._require_settled(vector)
            fence = self.repository.read_record("fence:vector" if vector else "fence:lexical")
            if fence is None or fence[0]["latest_build_id"] != build_id:
                raise ConflictError("A newer document build has fenced this worker")
            origins = tuple(scope.origin_id for scope in record.build.scopes)
            if self.repository.public_search_scopes(origins) != record.build.scopes:
                raise ConflictError("Public sources changed while building")
            physical = from_thread.run(writer.index_build, record.physical_index)
            if physical != record.build:
                raise EvidenceSearchError("Public physical build metadata differs from its history")
            old = from_thread.run(writer.alias_target, vector)
            if old == record.physical_index:
                raise ConflictError("A physical build cannot be reactivated; build a fresh version")
            key = self._pending_key(vector)
            pending = self.repository.read_record(key)
            intent = {"build_id": build_id, "previous": old, "status": "pending"}
            if pending is None:
                self.repository.create_record(key, intent)
            else:
                self.repository.replace_record(key, intent, pending[1], pending[2])
            # Keep the lock over the alias command. An uncertain outcome leaves a
            # durable barrier; a later worker must not guess that a timeout failed.
            from_thread.run(
                partial(writer.switch_alias, expected_previous=old, check_previous=True),
                record.physical_index,
                vector,
            )
            if from_thread.run(writer.alias_target, vector) != record.physical_index:
                raise EvidenceSearchError("Public alias activation outcome is uncertain")
            self._update(build_id, status="active", error=None)
            pending = self.repository.read_record(key)
            assert pending is not None
            self.repository.replace_record(key, {**intent, "status": "settled"}, pending[1], pending[2])
            rebuild_key = "rebuild:public-rag-vector-shadow" if vector else "rebuild:public-rag-active"
            rebuild = self.repository.read_record(rebuild_key)
            if rebuild is not None:
                self.repository.replace_record(rebuild_key, {"requested": False}, rebuild[1], rebuild[2])

    @staticmethod
    def _pending_key(vector: bool) -> str:
        return "activation:vector" if vector else "activation:lexical"

    def _require_settled(self, vector: bool) -> None:
        pending = self.repository.read_record(self._pending_key(vector))
        if pending is not None and pending[0].get("status") != "settled":
            raise EvidenceSearchError("Uncertain alias publication requires quiesced administrative recovery")

    async def recover(self, writer: OpenSearchEvidenceProjectionWriter, *, vector: bool = False) -> None:
        await run_in_threadpool(self._require_settled, vector)
        target = await writer.alias_target(vector)
        if target is None:
            return
        build = await writer.index_build(target)
        record = await run_in_threadpool(self.get, build.build_id)
        if record is None or record.build != build or record.status != "active":
            raise EvidenceSearchError("Public alias points to an unverified document build")

    async def reconcile(
        self, writer: OpenSearchEvidenceProjectionWriter, *, vector: bool = False, requests_quiesced: bool = False
    ) -> None:
        """Explicit recovery only after the operator has drained old alias requests."""
        if not requests_quiesced:
            raise EvidenceSearchError("Alias recovery requires explicit confirmation that old requests are drained")
        await run_in_threadpool(self._reconcile, writer, vector)

    def _reconcile(self, writer: OpenSearchEvidenceProjectionWriter, vector: bool) -> None:
        with self.repository.publication_lock():
            key = self._pending_key(vector)
            pending = self.repository.read_record(key)
            if pending is None or pending[0].get("status") == "settled":
                return
            value, sequence, term = pending
            if (
                value.get("status") != "pending"
                or not isinstance(value.get("build_id"), str)
                or "previous" not in value
            ):
                raise EvidenceSearchError("Uncertain activation metadata is invalid")
            record = self.get(value["build_id"])
            target = from_thread.run(writer.alias_target, vector)
            if record is None:
                raise EvidenceSearchError("Uncertain activation has no document build history")
            scopes = self.repository.public_search_scopes(tuple(scope.origin_id for scope in record.build.scopes))
            if target == record.physical_index and scopes == record.build.scopes:
                if from_thread.run(writer.index_build, target) != record.build:
                    raise EvidenceSearchError("Recovery found inconsistent physical build metadata")
                self._update(record.build.build_id, status="active", error=None)
            elif target in (record.physical_index, value["previous"], None):
                if target == record.physical_index:
                    from_thread.run(
                        partial(writer.switch_alias, expected_previous=target, check_previous=True), None, vector
                    )
                self._update(record.build.build_id, status="failed", error="activation_aborted")
            else:
                raise EvidenceSearchError("Alias drift requires explicit administrative repair")
            self.repository.replace_record(key, {**value, "status": "settled"}, sequence, term)
