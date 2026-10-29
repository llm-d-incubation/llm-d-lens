"""Document reads pin the alias without opening PostgreSQL authority."""

import asyncio
import hashlib
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.indexing import passage_document
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PinnedInventory, PublicationError
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION, QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.repository_corpus import parse_markdown
from llm_d_lens.benchmark_rag.search import PublicRagSearchService
from llm_d_lens.benchmark_rag.search_contracts import (
    EvidenceSearchError,
    PublicSearchBuild,
    PublicSearchHit,
    PublicSearchRequest,
    PublicSearchScope,
)
from llm_d_lens.benchmark_rag.service import EvidenceUnavailableError, PublicRagQueryService
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN
from llm_d_lens.db.dao.rag_query import QueryMatch, QuerySlice, RagQueryDao


def document_case(monkeypatch):
    from llm_d_lens.db.dao import rag_query

    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.setattr(rag_query, "RagQueryDao", Mock(side_effect=AssertionError("document read opened SQL")))
    data = b"# TTFT\nTime to first token.\n"
    file = InventoryFile(path="docs/metrics.md", sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    item = parse_markdown(file, inventory, data)[0]
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest", generation=1)
    build = PublicSearchBuild(build_id="fixed", scopes=(scope,), document_count=1)
    metadata = Mock(spec=DocumentRepository)
    metadata.publication_lock.side_effect = nullcontext
    monkeypatch.setattr(DocumentRepository, "from_environment", lambda: metadata)
    metadata.public_search_scopes.return_value = (scope,)
    match = QueryMatch(item, None, item, "manifest", 1)
    metadata.resolve_public_passages.return_value = (match,)
    history = {
        "build": build.model_dump(mode="json"),
        "physical_index": "public-rag-v1-fixed",
        "status": "active",
        "actual_count": 1,
        "error": None,
    }
    metadata.read_record.side_effect = lambda key: (history, 1, 1) if key == "build:fixed" else None
    repository = AsyncMock()
    repository.active_build.return_value = ("public-rag-v1-fixed", build)
    repository.search_passages.return_value = (
        (PublicSearchHit(document=passage_document(item, scope), score=1),),
        False,
    )
    archive = Mock()
    archive.read.return_value = data
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    return PublicRagSearchService(repository, archive=archive), principal, metadata, history


def test_default_document_authority_never_opens_sql_and_pins_alias_once(monkeypatch):
    service, principal, metadata, _ = document_case(monkeypatch)
    result = asyncio.run(service.search(PublicSearchRequest(question="TTFT", origins=(BENCHMARK_ORIGIN,)), principal))
    assert result.retrieval_build_id == "fixed"
    assert result.results[0].citation.published_manifest_id == "manifest"
    service.repository.active_build.assert_awaited_once_with(vector=False)
    assert service.repository.search_passages.call_args.args[0] == "public-rag-v1-fixed"
    metadata.assert_current.assert_called_once()


def test_unknown_alias_build_is_an_explicit_failure(monkeypatch):
    service, principal, metadata, _ = document_case(monkeypatch)
    metadata.read_record.return_value = None
    metadata.read_record.side_effect = None
    with pytest.raises(EvidenceSearchError, match="unverified"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    service.repository.search_passages.assert_not_awaited()


def test_uncertain_publication_blocks_reads_instead_of_using_sql_fallback(monkeypatch):
    service, principal, metadata, _ = document_case(monkeypatch)
    metadata.read_record.side_effect = lambda key: ({"status": "pending"}, 1, 1)
    with pytest.raises(EvidenceSearchError, match="quiesced"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    service.repository.active_build.assert_not_awaited()


def test_disabled_source_excludes_old_index_hits(monkeypatch):
    service, principal, metadata, _ = document_case(monkeypatch)
    metadata.public_search_scopes.return_value = ()
    result = asyncio.run(service.search(PublicSearchRequest(question="TTFT", origins=(BENCHMARK_ORIGIN,)), principal))
    assert result.results == () and result.index_stale and result.rebuild_required
    service.repository.search_passages.assert_not_awaited()


@pytest.mark.parametrize("inject_document_dao", [False, True])
def test_existing_query_definition_contract_uses_document_authority(monkeypatch, inject_document_dao):
    search_service, principal, metadata, _ = document_case(monkeypatch)
    match = metadata.resolve_public_passages.return_value[0]
    metadata.search.return_value = QuerySlice(matches=(match,), total=1)
    experiments = Mock(spec=RagQueryDao)
    experiments.search.side_effect = AssertionError("definition query reached old SQL text search")
    service = PublicRagQueryService(
        dao=experiments,
        archive=search_service.archive,
        document_dao=metadata if inject_document_dao else None,
    )
    result = service.query(
        QueryRequest(
            question="What is TTFT?",
            intent="definition",
            filters=QueryFilters(origins=(BENCHMARK_ORIGIN,)),
        ),
        principal,
    )
    assert result.status == "answered" and result.evidence[0].item_id == match.item.item_id
    assert result.evidence[0].citation.published_manifest_id == "manifest"
    metadata.assert_current.assert_called_once_with((match,))
    experiments.assert_current.assert_not_called()


def test_invalid_document_authority_configuration_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", "invalid")
    with pytest.raises(EvidenceSearchError, match="configuration"):
        PublicRagSearchService(AsyncMock())


@pytest.mark.parametrize("failure_at", ["search", "assert_current"])
def test_document_query_failures_keep_the_existing_unavailable_contract(monkeypatch, failure_at):
    search_service, principal, metadata, _ = document_case(monkeypatch)
    match = metadata.resolve_public_passages.return_value[0]
    metadata.search.return_value = QuerySlice(matches=(match,), total=1)
    getattr(metadata, failure_at).side_effect = PublicationError("document metadata unavailable")
    experiments = Mock(spec=RagQueryDao)
    service = PublicRagQueryService(dao=experiments, archive=search_service.archive, document_dao=metadata)
    with pytest.raises(EvidenceUnavailableError):
        service.query(
            QueryRequest(
                question="What is TTFT?", intent="definition", filters=QueryFilters(origins=(BENCHMARK_ORIGIN,))
            ),
            principal,
        )
    experiments.search.assert_not_called()
