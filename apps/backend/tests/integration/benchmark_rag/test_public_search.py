"""Real OpenSearch document authority acceptance; never uses business rows."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from starlette.requests import Request

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag import document_repository as metadata
from llm_d_lens.benchmark_rag import indexing
from llm_d_lens.benchmark_rag import opensearch_evidence_search as adapter
from llm_d_lens.benchmark_rag import router as query_routes
from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.benchmark_rag.document_transfer import DocumentBackupService
from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder, passage_document
from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceProjectionWriter
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationError,
    SourceApproval,
    registry_id,
)
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.repository_corpus import PARSER_VERSION, parse_markdown
from llm_d_lens.benchmark_rag.runtime import (
    RuntimePolicy,
    RuntimeUpdate,
    load_runtime_policy,
    update_runtime_policy,
)
from llm_d_lens.benchmark_rag.search import PublicRagSearchService
from llm_d_lens.benchmark_rag.search_contracts import EvidenceSearchError, PublicSearchBuild, PublicSearchRequest
from llm_d_lens.benchmark_rag.service import EvidenceUnavailableError, PublicRagQueryService
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, SourceArchive, allowed_paths
from llm_d_lens.core.exceptions import ConflictError
from llm_d_lens.db.dao.rag_query import QuerySlice, RagQueryDao


def test_real_public_index_search_citation_and_withdrawal(tmp_path, monkeypatch):
    endpoint = os.environ.get("LENS_RAG_TEST_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("Set LENS_RAG_TEST_OPENSEARCH_URL to an explicitly approved test service")
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(adapter, "ACTIVE_ALIAS", "public-rag-test-" + uuid4().hex)
    monkeypatch.setattr(metadata, "MANAGEMENT_INDEX", "public-rag-management-test-" + uuid4().hex)
    dao = metadata.DocumentRepository(endpoint)
    indexes = []
    repository = adapter.OpenSearchEvidenceSearchRepository(endpoint, timeout_seconds=20)
    try:
        dao.prepare(initialize=True)
        archive = SourceArchive()
        publication = RagPublicationService(dao=dao, archive=archive)
        data = (
            b"# Prefix caching\nUse --enable-prefix-caching with vLLM for reused prompts.\n"
            b"## TTFT\nTime to first token measures initial response delay.\n"
        )
        file = InventoryFile(path="docs/search-fixture.md", sha256=archive.put(data), format="markdown")
        inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
        publication.register_source(
            BENCHMARK_ORIGIN,
            SourceApproval(
                verified_public=True,
                approved_by="integration fixture",
                approval_reference="test only",
                license_reference="generated fixture",
                parser_version=PARSER_VERSION,
                allowed_paths=allowed_paths(BENCHMARK_ORIGIN),
            ),
        )
        manifest = publication.stage(
            inventory,
            (ParsedFile(path=file.path, items=parse_markdown(file, inventory, data)),),
            expected_counts={"source": 1, "passage": 2},
        )
        publication.activate(BENCHMARK_ORIGIN, manifest.item_id)
        principal = Principal(
            user_id="reader",
            username="reader",
            bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
            role_permissions={"reader": frozenset({QUERY_PERMISSION})},
        )

        async def run():
            build = await PublicRagIndexBuilder(repository, dao=dao, archive=archive).build((BENCHMARK_ORIGIN,))
            indexes.append(adapter.INDEX_PREFIX + build.build_id)
            writer = OpenSearchEvidenceProjectionWriter(repository)
            lifecycle = DocumentLifecycle(dao)
            scopes = dao.public_search_scopes((BENCHMARK_ORIGIN,))
            assert len(scopes) == 1
            documents = tuple(
                passage_document(item, scopes[0]) for item in dao.list_published(BENCHMARK_ORIGIN, kind="passage")
            )

            async def ready_build(build_id):
                build_record = PublicSearchBuild(build_id=build_id, scopes=scopes, document_count=len(documents))
                index = adapter.INDEX_PREFIX + build_id
                indexes.append(index)
                lifecycle.create(build_record, index)
                assert await writer.create(build_record) == index
                await writer.write(index, documents)
                await writer.verify(index, len(documents))
                lifecycle.ready(build_id, len(documents))
                return build_record

            service = PublicRagSearchService(repository, dao=dao, archive=archive)
            for query in ("prefix cache", "--enable-prefix-caching", "TTFT"):
                result = await service.search(
                    PublicSearchRequest(question=query, origins=(BENCHMARK_ORIGIN,)), principal
                )
                assert result.results and not result.index_stale
                assert all(hit.citation.published_manifest_id == manifest.item_id for hit in result.results)

            old_build = await ready_build("old-" + uuid4().hex)
            new_build = await ready_build("new-" + uuid4().hex)
            old_result, new_result = await asyncio.gather(
                lifecycle.activate(old_build.build_id, writer),
                lifecycle.activate(new_build.build_id, writer),
                return_exceptions=True,
            )
            assert isinstance(old_result, ConflictError)
            assert new_result is None
            new_index = adapter.INDEX_PREFIX + new_build.build_id
            assert await writer.alias_target() == new_index
            assert len(await repository._request("GET", f"/_alias/{adapter.ACTIVE_ALIAS}")) == 1

            interrupted_build = await ready_build("interrupted-" + uuid4().hex)

            class LostAliasResponseWriter(OpenSearchEvidenceProjectionWriter):
                async def switch_alias(self, index, vector=False, **kwargs):
                    await super().switch_alias(index, vector, **kwargs)
                    raise EvidenceSearchError("simulated lost alias response")

            interrupted_index = adapter.INDEX_PREFIX + interrupted_build.build_id
            with pytest.raises(EvidenceSearchError, match="simulated lost alias response"):
                await lifecycle.activate(interrupted_build.build_id, LostAliasResponseWriter(repository))
            assert await writer.alias_target() == interrupted_index
            with pytest.raises(EvidenceSearchError, match="quiesced administrative recovery"):
                await service.search(PublicSearchRequest(question="TTFT", origins=(BENCHMARK_ORIGIN,)), principal)
            await lifecycle.reconcile(writer, requests_quiesced=True)
            await lifecycle.recover(writer)
            recovered = lifecycle.get(interrupted_build.build_id)
            assert recovered is not None and recovered.status == "active"

            verified = await service.search(
                PublicSearchRequest(question="TTFT", origins=(BENCHMARK_ORIGIN,)), principal
            )
            original = archive.read(file.sha256)
            assert original == data
            assert verified.results
            assert all(hit.citation.sha256 == file.sha256 for hit in verified.results)
            assert all(hit.citation.source_path == file.path for hit in verified.results)
            assert all(hit.text.encode() in original for hit in verified.results)

            dao.set_enabled(BENCHMARK_ORIGIN, False, expected_generation=1)
            withdrawn = await service.search(
                PublicSearchRequest(question="TTFT", origins=(BENCHMARK_ORIGIN,)), principal
            )
            assert withdrawn.results == () and withdrawn.index_stale

        asyncio.run(run())
    finally:

        async def cleanup():
            for index in indexes:
                await repository._request("DELETE", "/" + index, allow_missing=True)
            await repository._request("DELETE", "/" + metadata.MANAGEMENT_INDEX, allow_missing=True)

        try:
            asyncio.run(cleanup())
        finally:
            dao.close()


def test_real_isolated_document_restore_rebuild_and_version_rollback(tmp_path, monkeypatch):
    endpoint = os.environ.get("LENS_RAG_TEST_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("Set LENS_RAG_TEST_OPENSEARCH_URL to an explicitly approved test service")
    indexes, management_indexes, daos = [], [], []
    repository = adapter.OpenSearchEvidenceSearchRepository(endpoint, timeout_seconds=20)
    origin = BENCHMARK_ORIGIN
    principal = Principal(
        user_id="recovery-reader",
        username="recovery-reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    approval = SourceApproval(
        verified_public=True,
        approved_by="isolated recovery fixture",
        approval_reference="test only",
        license_reference="generated fixture",
        parser_version=PARSER_VERSION,
        allowed_paths=allowed_paths(origin),
    )
    backup_path = tmp_path / "recovery.zip"
    original = b"# Recovery fixture\nRecoveryoriginal verifies canonical citations after restore.\n"
    replacement = b"# Replacement fixture\nRecoveryreplacement supersedes the first release.\n"

    def isolated_target(name):
        monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / name))
        monkeypatch.setattr(adapter, "ACTIVE_ALIAS", "public-rag-test-" + uuid4().hex)
        management_index = "public-rag-management-test-" + uuid4().hex
        management_indexes.append(management_index)
        monkeypatch.setattr(metadata, "MANAGEMENT_INDEX", management_index)
        dao = metadata.DocumentRepository(endpoint, lock_path=tmp_path / f"{name}.lock")
        daos.append(dao)
        dao.prepare(initialize=True)
        return dao, SourceArchive()

    def stage(publication, archive, data, revision):
        file = InventoryFile(path="docs/recovery-fixture.md", sha256=archive.put(data), format="markdown")
        inventory = PinnedInventory(origin_id=origin, revision=revision * 40, complete=True, files=(file,))
        manifest = publication.stage(
            inventory,
            (ParsedFile(path=file.path, items=parse_markdown(file, inventory, data)),),
            expected_counts={"source": 1, "passage": 1},
        )
        return file, manifest

    async def build(dao, archive):
        identity = uuid4()
        indexes.append(adapter.INDEX_PREFIX + identity.hex)
        with monkeypatch.context() as patch:
            patch.setattr(indexing, "uuid4", lambda: identity)
            return await PublicRagIndexBuilder(repository, dao=dao, archive=archive).build((origin,))

    async def search(service, question):
        return await service.search(PublicSearchRequest(question=question, origins=(origin,)), principal)

    async def run():
        dao, archive = isolated_target("original-data")
        publication = RagPublicationService(dao=dao, archive=archive)
        publication.register_source(origin, approval)
        file, manifest = stage(publication, archive, original, "a")
        publication.activate(origin, manifest.item_id)
        original_build = await build(dao, archive)
        update_runtime_policy(
            RuntimeUpdate(expected_generation=0, agentic_mode="enabled", agentic_user_ids=(principal.user_id,))
        )
        DocumentBackupService(dao, archive).backup(backup_path)

        dao.set_enabled(origin, False, expected_generation=dao.head(origin).state.generation)
        disabled = dao.head(origin)
        assert DocumentBackupService(dao, archive).restore(backup_path).inserted == 0
        assert dao.head(origin) == disabled
        assert not dao.head(origin).state.enabled
        withdrawn = await search(PublicRagSearchService(repository, dao=dao, archive=archive), "Recoveryoriginal")
        assert withdrawn.results == () and withdrawn.index_stale

        dao, archive = isolated_target("clean-recovery-data")
        assert load_runtime_policy() == RuntimePolicy()
        assert not archive._root(file.sha256).exists()
        restore = DocumentBackupService(dao, archive)
        assert restore.restore(backup_path).inserted > 0
        assert load_runtime_policy().agentic_mode == "disabled"
        assert dao.get(registry_id(origin)) is None
        assert dao.public_search_scopes((origin,)) == ()
        assert DocumentLifecycle(dao).get(original_build.build_id) is None
        writer = OpenSearchEvidenceProjectionWriter(repository)
        assert await writer.alias_target() is None
        publication = RagPublicationService(dao=dao, archive=archive)
        with pytest.raises(PublicationError, match="not registered"):
            publication.activate(origin, manifest.item_id, expected_generation=0)
        publication.register_source(origin, approval)
        assert dao.public_search_scopes((origin,)) == ()
        publication.activate(origin, manifest.item_id, expected_generation=0)
        restored_build = await build(dao, archive)
        assert restored_build.build_id != original_build.build_id
        service = PublicRagSearchService(repository, dao=dao, archive=archive)

        def assert_original_citations(result, expected_build):
            assert result.results and not result.index_stale
            assert result.retrieval_build_id == expected_build.build_id
            assert archive.read(file.sha256) == original
            for hit in result.results:
                assert hit.citation.origin_id == origin
                assert hit.citation.published_manifest_id == manifest.item_id
                assert hit.citation.source_revision == "a" * 40
                assert hit.citation.source_path == file.path
                assert hit.citation.sha256 == file.sha256
                assert hit.text.encode() in original

        assert_original_citations(await search(service, "Recoveryoriginal"), restored_build)
        _, second_manifest = stage(publication, archive, replacement, "b")
        second_publication = publication.activate(origin, second_manifest.item_id)
        second_build = await build(dao, archive)
        assert second_build.scopes[0].generation == second_publication.generation
        assert (await search(service, "Recoveryreplacement")).results
        assert (await search(service, "Recoveryoriginal")).results == ()

        rolled_back = publication.rollback(origin, manifest.item_id, expected_generation=second_publication.generation)
        assert rolled_back.generation > second_publication.generation
        assert rolled_back.generation > restored_build.scopes[0].generation
        assert dao.head(origin).state.history[-1].operation == "rollback"
        stale = await search(service, "Recoveryoriginal")
        assert stale.results == () and stale.index_stale
        assert stale.retrieval_build_id == second_build.build_id
        with pytest.raises(EvidenceSearchError, match="complete ready"):
            await DocumentLifecycle(dao).activate(restored_build.build_id, writer)
        rollback_build = await build(dao, archive)
        assert rollback_build.build_id not in {
            original_build.build_id,
            restored_build.build_id,
            second_build.build_id,
        }
        assert rollback_build.scopes[0].generation == rolled_back.generation
        assert_original_citations(await search(service, "Recoveryoriginal"), rollback_build)
        assert (await search(service, "Recoveryreplacement")).results == ()
        assert await writer.alias_target() == adapter.INDEX_PREFIX + rollback_build.build_id

        dao.set_enabled(origin, False, expected_generation=rolled_back.generation)
        disabled = dao.head(origin)
        assert restore.restore(backup_path).inserted == 0
        with pytest.raises(PublicationError, match="disabled"):
            publication.rollback(origin, manifest.item_id, expected_generation=disabled.state.generation)
        assert dao.head(origin) == disabled
        withdrawn = await search(service, "Recoveryoriginal")
        assert withdrawn.results == () and withdrawn.index_stale

    try:
        asyncio.run(run())
    finally:

        async def cleanup():
            for index in (*indexes, *management_indexes):
                await repository._request("DELETE", "/" + index, allow_missing=True)

        try:
            asyncio.run(cleanup())
        finally:
            for dao in daos:
                dao.close()


def test_document_kill_switch_preserves_structured_query_api(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "runtime"))
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    monkeypatch.setattr(query_routes, "current_principal", lambda request: principal)
    retrieval = AsyncMock()
    service = PublicRagSearchService(retrieval, dao=Mock(), archive=Mock())
    monkeypatch.setattr(query_routes, "get_search_service", lambda request: service)
    payload = QueryRequest(question="Published throughput", filters=QueryFilters(model="recovery-fixture-model"))
    sql = Mock(spec=RagQueryDao)
    sql.search.return_value = QuerySlice(matches=(), total=0)
    documents = Mock(spec=metadata.DocumentRepository)
    documents.search.side_effect = lambda *args, **kwargs: metadata.DocumentRepository.search(
        documents, *args, **kwargs
    )
    structured = PublicRagQueryService(dao=sql, document_dao=documents, archive=Mock())
    monkeypatch.setattr(query_routes, "get_service", lambda: structured)
    request = Request({"type": "http", "method": "POST", "path": "/api/v1/benchmark-rag/query"})
    response = query_routes.query_public_benchmark_rag(payload, request)
    assert response.intent == "numeric"
    assert sql.search.call_args.kwargs["intent"] == "numeric"
    update_runtime_policy(RuntimeUpdate(expected_generation=0, search_enabled=False))
    with pytest.raises(EvidenceSearchError, match="administratively disabled"):
        asyncio.run(query_routes.search_public_rag_documents(PublicSearchRequest(question="cache"), request))
    assert retrieval.mock_calls == []
    with pytest.raises(EvidenceUnavailableError, match="document evidence is unavailable"):
        query_routes.query_public_benchmark_rag(QueryRequest(question="What is prefix caching?"), request)
    documents._request.assert_not_called()
    documents.publication_lock.assert_not_called()
    assert query_routes.query_public_benchmark_rag(payload, request) == response
    assert sql.search.call_count == 2
    assert all(call.kwargs["intent"] == "numeric" for call in sql.search.call_args_list)
