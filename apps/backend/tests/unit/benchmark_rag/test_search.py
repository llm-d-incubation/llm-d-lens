"""Publication, identity and source-byte checks for public lexical search."""

import asyncio
import hashlib
import threading
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag import search as search_module
from llm_d_lens.benchmark_rag.document_lifecycle import DocumentBuildRecord, DocumentLifecycle
from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.embedding import CONTRACT, located_chunk
from llm_d_lens.benchmark_rag.indexing import passage_document
from llm_d_lens.benchmark_rag.opensearch_evidence_search import alias_query_text, technical_identifiers
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PinnedInventory
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION
from llm_d_lens.benchmark_rag.query_enhancement import expand_query
from llm_d_lens.benchmark_rag.repository_corpus import parse_markdown
from llm_d_lens.benchmark_rag.router import get_search_service, public_search_lifespan
from llm_d_lens.benchmark_rag.search import PublicRagSearchService, configured_repository
from llm_d_lens.benchmark_rag.search_contracts import (
    EvidenceSearchError,
    PublicSearchBuild,
    PublicSearchHit,
    PublicSearchRequest,
    PublicSearchScope,
)
from llm_d_lens.benchmark_rag.service import EvidenceUnavailableError
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN
from llm_d_lens.core.exceptions import UnauthenticatedError
from llm_d_lens.db.dao.rag_query import QueryMatch


@pytest.fixture
def search_case():
    data = b"# TTFT\nTime to first token.\n"
    file = InventoryFile(path="docs/metrics.md", sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    item = parse_markdown(file, inventory, data)[0]
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest", generation=1)
    match = QueryMatch(item=item, experiment=None, source=item, manifest_id="manifest", generation=1)
    dao = Mock(spec=DocumentRepository)
    dao.public_search_scopes.return_value = (scope,)
    dao.resolve_public_passages.return_value = (match,)
    archive = Mock()
    archive.read.return_value = data
    repository = AsyncMock()
    repository.active_build.return_value = (
        "public-rag-v1-test",
        PublicSearchBuild(build_id="test", scopes=(scope,), document_count=1),
    )
    repository.search_passages.return_value = (
        (PublicSearchHit(document=passage_document(item, scope), score=1),),
        False,
    )
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding(role="reader", scope_type=ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    lifecycle = Mock(spec=DocumentLifecycle)
    lifecycle.recover = AsyncMock()
    lifecycle.get.side_effect = lambda build_id: DocumentBuildRecord(
        build=repository.active_build.return_value[1],
        physical_index=repository.active_build.return_value[0],
        status="active",
        expected_count=1,
        actual_count=1,
        error=None,
    )
    return PublicRagSearchService(repository, dao=dao, archive=archive, lifecycle=lifecycle), principal, scope


def test_search_returns_verified_citation(search_case):
    service, principal, _ = search_case
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    assert response.results[0].citation.published_manifest_id == "manifest"
    assert response.results[0].citation.artifact_uri.startswith("lens-artifact://benchmark-rag/")
    assert response.results[0].text == "# TTFT\nTime to first token.\n"
    assert response.retrieval_mode == "opensearch_lexical" and not response.index_stale
    service.dao.assert_current.assert_called_once()
    service.lifecycle._require_settled.assert_called_once_with(False)
    service.repository.active_build.assert_awaited_once_with(vector=False)
    service.lifecycle.get.assert_called_once_with("test")
    service.lifecycle.recover.assert_not_awaited()


def test_permissions_are_checked_before_any_search_io(search_case):
    service, _, _ = search_case
    with pytest.raises(UnauthenticatedError):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), None))
    service.repository.active_build.assert_not_called()
    service.dao.public_search_scopes.assert_not_called()


@pytest.mark.parametrize("invalid", ["missing", "inactive", "build"])
def test_alias_requires_matching_active_document_history_without_read_recovery(search_case, invalid):
    service, principal, _ = search_case
    record = service.lifecycle.get("test")
    service.lifecycle.get.reset_mock()
    if invalid == "missing":
        record = None
    elif invalid == "inactive":
        record = replace(record, status="ready")
    else:
        record = replace(record, build=record.build.model_copy(update={"document_count": 2}))
    service.lifecycle.get.side_effect = None
    service.lifecycle.get.return_value = record
    with pytest.raises(EvidenceSearchError, match="unverified"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    service.repository.active_build.assert_awaited_once_with(vector=False)
    service.repository.search_passages.assert_not_awaited()
    service.lifecycle.recover.assert_not_awaited()


def test_unsettled_document_publication_blocks_alias_reads(search_case):
    service, principal, _ = search_case
    service.lifecycle._require_settled.side_effect = EvidenceSearchError("publication is quiesced")
    with pytest.raises(EvidenceSearchError, match="quiesced"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    service.repository.active_build.assert_not_awaited()
    service.lifecycle.get.assert_not_called()
    service.lifecycle.recover.assert_not_awaited()


def test_changed_publication_is_explicitly_stale_and_not_returned(search_case):
    service, principal, scope = search_case
    service.dao.public_search_scopes.return_value = (scope.model_copy(update={"generation": 2}),)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    assert response.index_stale and BENCHMARK_ORIGIN in response.affected_origins
    assert response.results == ()
    service.repository.search_passages.assert_not_called()


def test_retired_hit_is_excluded(search_case):
    service, principal, _ = search_case
    service.dao.resolve_public_passages.return_value = ()
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))
    assert response.results == () and response.excluded_count == 1


def test_authority_batches_continue_past_filtered_candidates_without_exceeding_budget(search_case):
    service, principal, scope = search_case
    original = service.dao.resolve_public_passages.return_value[0]
    matches = tuple(
        replace(
            original,
            item=original.item.model_copy(update={"item_id": hashlib.sha256(str(index).encode()).hexdigest()}),
        )
        for index in range(21)
    )
    service.repository.search_passages.return_value = (
        tuple(PublicSearchHit(document=passage_document(match.item, scope), score=1) for match in matches),
        False,
    )
    service.dao.resolve_public_passages.side_effect = [(), (matches[-1],)]
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", limit=1), principal))
    assert response.results[0].evidence_id == matches[-1].item.item_id
    assert response.candidate_count == 21 and response.excluded_count == 20
    calls = service.dao.resolve_public_passages.call_args_list
    assert [len(call.args[1]) for call in calls] == [20, 1]
    service.dao.assert_current.assert_called_once_with((matches[-1],))


def test_indexed_text_tampering_is_not_trusted(search_case):
    service, principal, _ = search_case
    hits, _ = service.repository.search_passages.return_value
    hit = hits[0].model_copy(update={"document": hits[0].document.model_copy(update={"text": "invented"})})
    service.repository.search_passages.return_value = ((hit,), False)
    with pytest.raises(EvidenceSearchError, match="authoritative"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))


def test_original_byte_corruption_fails_explicitly(search_case):
    service, principal, _ = search_case
    service.archive.read.return_value = b"replaced"
    with pytest.raises(EvidenceUnavailableError):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT"), principal))


def test_no_matches_is_not_an_error(search_case):
    service, principal, _ = search_case
    service.repository.search_passages.return_value = ((), False)
    response = asyncio.run(service.search(PublicSearchRequest(question="no match"), principal))
    assert response.results == () and response.candidate_count == 0 and not response.index_stale


@pytest.mark.parametrize(
    "payload",
    [
        {"question": " "},
        {"question": "TTFT", "origins": ["private"]},
        {"question": "TTFT", "index": "diagnostics"},
        {"question": "TTFT", "limit": 201},
    ],
)
def test_unbounded_or_nonpublic_queries_are_rejected(payload):
    with pytest.raises(ValidationError):
        PublicSearchRequest.model_validate(payload)


def test_missing_search_configuration_does_not_guess_an_endpoint(monkeypatch):
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    with pytest.raises(EvidenceSearchError, match="configured"):
        configured_repository()


def test_application_owns_and_closes_the_reused_search_transport(monkeypatch):
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://127.0.0.1:9200")
    app = FastAPI()

    async def run():
        async with public_search_lifespan(app):
            client = app.state.public_rag_client
            request = Request({"type": "http", "app": app})
            first = get_search_service(request)
            second = get_search_service(request)
            assert first.repository._client is second.repository._client is client
            assert not client.is_closed
        assert client.is_closed and app.state.public_rag_client is None

    asyncio.run(run())


def vector_case(search_case):
    service, principal, scope = search_case
    match = service.dao.resolve_public_passages.return_value[0]
    parent = passage_document(match.item, scope)
    document = located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(
        update={"embedding": (1.0, *([0.0] * 383))}
    )
    service.repository.active_build.return_value = (
        "public-rag-v1-vector",
        PublicSearchBuild(
            build_id="vector",
            scopes=(scope,),
            document_count=1,
            retrieval_mode="opensearch_vector",
            embedding_contract=CONTRACT,
        ),
    )
    service.repository.search_vectors.return_value = ((PublicSearchHit(document=document, score=1),), False)
    service.encoder = Mock()
    service.encoder.contract = CONTRACT
    service.encoder.query.return_value = document.embedding
    return service, principal


def test_vector_query_uses_fixed_contract_filters_and_verified_parent_offsets(search_case):
    service, principal = vector_case(search_case)
    response = asyncio.run(service.search(PublicSearchRequest(question="首个词元延迟", mode="vector"), principal))
    assert response.retrieval_mode == "opensearch_vector"
    assert response.embedding_contract == CONTRACT
    hit = response.results[0]
    assert hit.matched_branches == ("vector",) and hit.vector_rank == 1 and hit.lexical_rank is None
    assert hit.chunk_start == 0 and hit.chunk_end == len(hit.text)
    assert hit.evidence_id != hit.chunk_id
    service.lifecycle._require_settled.assert_called_once_with(True)
    service.repository.active_build.assert_awaited_once_with(vector=True)
    service.lifecycle.get.assert_called_once_with("vector")
    service.lifecycle.recover.assert_not_awaited()
    service.repository.search_passages.assert_not_called()


@pytest.mark.parametrize("failure", ["contract", "encoder", "offset", "context", "text"])
def test_vector_failures_are_explicit_without_lexical_fallback(search_case, failure):
    service, principal = vector_case(search_case)
    if failure == "contract":
        service.encoder.contract = CONTRACT.model_copy(update={"model_revision": "b" * 40})
    elif failure == "encoder":
        service.encoder.query.side_effect = EvidenceSearchError("encoder unavailable")
    else:
        hits, more = service.repository.search_vectors.return_value
        updates = {
            "offset": {"chunk_end": 999},
            "context": {"embedding_context": "invented"},
            "text": {"text": "invented"},
        }
        hit = hits[0].model_copy(update={"document": hits[0].document.model_copy(update=updates[failure])})
        service.repository.search_vectors.return_value = ((hit,), more)
    with pytest.raises(EvidenceSearchError):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="vector"), principal))
    service.repository.search_passages.assert_not_called()


def test_vector_children_are_deduplicated_by_parent_and_retired_evidence_is_excluded(search_case):
    service, principal = vector_case(search_case)
    hits, _ = service.repository.search_vectors.return_value
    service.repository.search_vectors.return_value = ((hits[0], hits[0]), False)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="vector"), principal))
    assert len(response.results) == 1 and response.excluded_count == 1
    service.dao.resolve_public_passages.return_value = ()
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="vector"), principal))
    assert response.results == () and response.excluded_count == 2


def hybrid_case(search_case):
    service, principal = vector_case(search_case)
    hits, _ = service.repository.search_vectors.return_value
    service.repository.search_passages.return_value = (hits, False)
    return service, principal, hits


@pytest.mark.parametrize("mode", ["lexical", "vector", "hybrid"])
@pytest.mark.parametrize("identifiers", ["current", "legacy", "forged"])
def test_model_alias_projections_keep_old_indexes_readable_and_reject_tampering(search_case, mode, identifiers):
    service, principal, scope = search_case
    if mode != "lexical":
        service, principal = vector_case(search_case)
    data = b"# Acme/Atlas-7B-Instruct-FP8\nOriginal model evidence.\n"
    file = InventoryFile(path="docs/model.md", sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    item = parse_markdown(file, inventory, data)[0]
    old_match = service.dao.resolve_public_passages.return_value[0]
    service.dao.resolve_public_passages.return_value = (replace(old_match, item=item, source=item),)
    service.archive.read.return_value = data
    parent = passage_document(item, scope)
    document = parent
    if mode != "lexical":
        document = located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(
            update={"embedding": (1.0, *([0.0] * 383))}
        )
    if identifiers != "current":
        legacy = technical_identifiers(parent.title + "\n" + parent.text, expand_aliases=False)
        document = document.model_copy(update={"identifiers": legacy if identifiers == "legacy" else (*legacy, "fake")})
    hits = ((PublicSearchHit(document=document, score=1),), False)
    service.repository.search_passages.return_value = hits
    service.repository.search_vectors.return_value = hits
    question = "Explain ACME/ATLAS-7B-INSTRUCT-FP8"
    request = PublicSearchRequest(question=question, mode=mode, query_enhancement=False)
    if identifiers == "forged":
        with pytest.raises(EvidenceSearchError, match="differs"):
            asyncio.run(service.search(request, principal))
        return
    response = asyncio.run(service.search(request, principal))
    assert len(response.results) == 1
    result = response.results[0]
    assert result.text == data.decode() and result.title == parent.title
    assert result.citation.source_path == item.source_path
    assert result.citation.heading_path == tuple(item.heading_path)
    assert result.evidence_id == item.item_id and request.question == question
    if mode != "lexical":
        service.encoder.query.assert_called_once_with(alias_query_text(question))
    if mode == "hybrid":
        assert response.query_enhancement is not None and not response.query_enhancement.enabled
        assert result.matched_branches == ("lexical", "vector")


def test_hybrid_runs_both_branches_concurrently_on_one_fixed_build(search_case):
    service, principal, hits = hybrid_case(search_case)
    lexical_started = asyncio.Event()
    vector_started = asyncio.Event()

    async def lexical(*args, **kwargs):
        lexical_started.set()
        await asyncio.wait_for(vector_started.wait(), 1)
        return hits, False

    async def vector(*args, **kwargs):
        vector_started.set()
        await asyncio.wait_for(lexical_started.wait(), 1)
        return hits, False

    service.repository.search_passages.side_effect = lexical
    service.repository.search_vectors.side_effect = vector
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.retrieval_mode == "hybrid" and response.degraded_reason is None
    assert response.results[0].matched_branches == ("lexical", "vector")
    assert response.results[0].fusion_score == pytest.approx(2 / 61)
    assert response.results[0].lexical_rank == response.results[0].vector_rank == 1
    assert response.candidate_count == 2 and response.excluded_count == 0
    service.lifecycle._require_settled.assert_called_once_with(True)
    service.repository.active_build.assert_awaited_once_with(vector=True)
    service.lifecycle.get.assert_called_once_with("vector")
    service.lifecycle.recover.assert_not_awaited()
    assert service.repository.search_passages.call_args.args[0] == "public-rag-v1-vector"
    assert service.repository.search_vectors.call_args.args[0] == "public-rag-v1-vector"
    assert response.branch_status["lexical"].status == response.branch_status["vector"].status == "ok"


@pytest.mark.parametrize("enabled", [True, False])
def test_hybrid_enhancement_is_default_on_and_has_an_exact_request_opt_out(search_case, enabled):
    service, principal, _ = hybrid_case(search_case)
    question = "llm-d-benchmark \u591a\u526f\u672c\u8def\u7531 KV-cache affinityThreshold"
    payload = {"question": question, "mode": "hybrid", "origins": (BENCHMARK_ORIGIN,)}
    if not enabled:
        payload["query_enhancement"] = False
    request = PublicSearchRequest.model_validate(payload)
    expected, terms, hints = expand_query(question) if enabled else (question, (), ())
    response = asyncio.run(service.search(request, principal))
    assert request.question == question
    service.encoder.query.assert_called_once_with(expected)
    service.repository.search_passages.assert_awaited_once_with(
        "public-rag-v1-vector", expected, (search_case[2],), limit=50, offset=0
    )
    assert response.query_enhancement is not None
    assert response.query_enhancement.enabled is enabled
    assert response.query_enhancement.effective_question == expected
    assert response.query_enhancement.added_terms == terms
    assert response.query_enhancement.source_hints == hints
    assert response.results[0].score == pytest.approx((2 / 61) * (1.25 if enabled else 1))
    assert response.results[0].fusion_score == response.results[0].score
    assert response.results[0].text == "# TTFT\nTime to first token.\n"
    assert response.results[0].citation.origin_id == BENCHMARK_ORIGIN
    service.dao.public_search_scopes.assert_called_once_with((BENCHMARK_ORIGIN,))
    service.dao.assert_current.assert_called_once()


@pytest.mark.parametrize("mode", ["lexical", "vector"])
def test_enhancement_does_not_change_explicit_lexical_or_vector_modes(search_case, mode):
    service, principal = vector_case(search_case) if mode == "vector" else search_case[:2]
    question = "llm-d-benchmark \u591a\u526f\u672c\u8def\u7531 KV-cache"
    response = asyncio.run(service.search(PublicSearchRequest(question=question, mode=mode), principal))
    if mode == "vector":
        service.encoder.query.assert_called_once_with(question)
    else:
        assert service.repository.search_passages.call_args.args[1] == question
    assert response.query_enhancement is None


def test_enhanced_hybrid_degradation_remains_explicit_and_keeps_enhanced_lexical_query(search_case):
    service, principal, _ = hybrid_case(search_case)
    service.encoder.query.side_effect = EvidenceSearchError("embedding input exceeds token budget")
    question = "llm-d-benchmark \u526f\u672c\u8def\u7531"
    response = asyncio.run(service.search(PublicSearchRequest(question=question, mode="hybrid"), principal))
    expected, _, _ = expand_query(question)
    assert service.repository.search_passages.call_args.args[1] == expected
    assert response.degraded_reason == "vector_unavailable"
    assert response.retrieval_mode == "opensearch_lexical"
    assert response.query_enhancement is not None and response.query_enhancement.enabled
    assert response.results[0].fusion_score is None
    assert response.results[0].score == pytest.approx(1.25 / 61)


@pytest.mark.parametrize("failure", ["encoder", "contract", "vector", "timeout"])
def test_hybrid_vector_failure_is_explicit_verified_lexical_degradation(search_case, failure):
    service, principal, _ = hybrid_case(search_case)
    if failure == "encoder":
        service.encoder.query.side_effect = EvidenceSearchError("model unavailable")
    elif failure == "contract":
        service.encoder.contract = CONTRACT.model_copy(update={"model_revision": "b" * 40})
    else:
        service.repository.search_vectors.side_effect = (
            TimeoutError() if failure == "timeout" else EvidenceSearchError("partial shards")
        )
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.retrieval_mode == "opensearch_lexical"
    assert response.degraded_reason == (
        "embedding_contract_mismatch" if failure == "contract" else "vector_unavailable"
    )
    assert response.branch_status["vector"].status == "failed"
    assert response.branch_status["lexical"].status == "ok"
    assert response.results[0].matched_branches == ("lexical",)
    assert response.results[0].fusion_score is None
    service.dao.assert_current.assert_called_once()
    service.archive.read.assert_called_once()


def test_hybrid_cold_model_readiness_has_a_separate_bounded_budget(search_case, monkeypatch):
    service, principal, _ = hybrid_case(search_case)
    encoder = service.encoder
    service.encoder = None
    monkeypatch.setattr(search_module, "MODEL_READINESS_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(search_module, "VECTOR_QUERY_TIMEOUT_SECONDS", 0.05)
    loader = Mock(return_value=encoder)
    monkeypatch.setattr(search_module, "configured_encoder", loader)
    original = search_module.run_in_threadpool

    async def threadpool(function, *args, **kwargs):
        if function is loader:
            await asyncio.sleep(0.1)
            return function()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(search_module, "run_in_threadpool", threadpool)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.retrieval_mode == "hybrid" and response.degraded_reason is None
    assert response.branch_status["vector"].status == "ok"
    loader.assert_called_once_with()
    encoder.query.assert_called_once()
    service.repository.search_vectors.assert_awaited_once()


@pytest.mark.parametrize("stage", ["model_readiness", "query_embedding", "vector_search"])
def test_hybrid_real_deadlines_degrade_with_sanitized_stage_and_clean_up(search_case, monkeypatch, caplog, stage):
    service, principal, _ = hybrid_case(search_case)
    loader = Mock()
    if stage == "model_readiness":
        service.encoder = None
    monkeypatch.setattr(search_module, "configured_encoder", loader)
    monkeypatch.setattr(search_module, "MODEL_READINESS_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(search_module, "VECTOR_QUERY_TIMEOUT_SECONDS", 0.02)
    original = search_module.run_in_threadpool
    cleaned = []

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(stage)

    async def threadpool(function, *args, **kwargs):
        if (stage == "model_readiness" and function is loader) or (
            stage == "query_embedding" and function is service.encoder.query
        ):
            return await blocked()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(search_module, "run_in_threadpool", threadpool)
    if stage == "vector_search":
        service.repository.search_vectors.side_effect = blocked
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.degraded_reason == "vector_unavailable"
    assert response.branch_status["vector"].status == "failed"
    assert response.branch_status["lexical"].status == "ok"
    assert f"stage={stage} category=timeout" in caplog.text
    assert cleaned == [stage]
    if stage != "vector_search":
        service.repository.search_vectors.assert_not_awaited()


@pytest.mark.parametrize("stage", ["model_readiness", "query_embedding", "vector_search"])
def test_hybrid_caller_cancellation_cleans_up_without_degradation(search_case, monkeypatch, caplog, stage):
    service, principal, _ = hybrid_case(search_case)
    loader = Mock()
    if stage == "model_readiness":
        service.encoder = None
    monkeypatch.setattr(search_module, "configured_encoder", loader)
    original = search_module.run_in_threadpool

    async def scenario():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def threadpool(function, *args, **kwargs):
            if (stage == "model_readiness" and function is loader) or (
                stage == "query_embedding" and function is service.encoder.query
            ):
                return await blocked()
            return await original(function, *args, **kwargs)

        monkeypatch.setattr(search_module, "run_in_threadpool", threadpool)
        if stage == "vector_search":
            service.repository.search_vectors.side_effect = blocked
        task = asyncio.create_task(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run(scenario())
    assert "event=rag_hybrid_degraded" not in caplog.text


def test_hybrid_readiness_does_not_hide_unknown_errors_or_log_secrets(search_case, monkeypatch, caplog):
    service, principal, _ = hybrid_case(search_case)
    service.encoder = None
    monkeypatch.setattr(search_module, "configured_encoder", Mock(side_effect=TypeError("private diagnostic detail")))
    with pytest.raises(TypeError, match="private diagnostic detail"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert "private diagnostic detail" not in caplog.text
    assert "event=rag_hybrid_degraded" not in caplog.text


def test_hybrid_readiness_failure_logs_only_sanitized_categories(search_case, monkeypatch, caplog):
    service, principal, _ = hybrid_case(search_case)
    service.encoder = None
    monkeypatch.setattr(
        search_module, "configured_encoder", Mock(side_effect=EvidenceSearchError("private model path or credentials"))
    )
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.degraded_reason == "vector_unavailable"
    assert "stage=model_readiness category=unavailable" in caplog.text
    assert "private model path or credentials" not in caplog.text


@pytest.mark.parametrize("stage", ["model_readiness", "query_embedding", "authority"])
def test_hybrid_cancelled_sync_worker_completion_has_no_unobserved_errors(search_case, monkeypatch, caplog, stage):
    service, principal, _ = hybrid_case(search_case)

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        completed = asyncio.Event()
        release = threading.Event()
        unobserved = []
        loop.set_exception_handler(lambda loop, context: unobserved.append(context))

        def worker(*args):
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait(timeout=1)
                raise RuntimeError("private worker failure")
            finally:
                loop.call_soon_threadsafe(completed.set)

        if stage == "model_readiness":
            service.encoder = None
            monkeypatch.setattr(search_module, "configured_encoder", worker)
        elif stage == "authority":
            service.dao.resolve_public_passages.side_effect = worker
        else:
            service.encoder.query.side_effect = worker
        task = asyncio.create_task(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
        finally:
            release.set()
            await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not unobserved
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run(scenario())
    assert "private worker failure" not in caplog.text
    assert "event=rag_hybrid_degraded" not in caplog.text


@pytest.mark.parametrize("failure", ["lexical", "authority", "archive", "tamper"])
def test_hybrid_mandatory_authority_or_lexical_failure_never_becomes_success(search_case, failure):
    from llm_d_lens.benchmark_rag.publication_contracts import PublicationError

    service, principal, hits = hybrid_case(search_case)
    if failure == "lexical":
        service.repository.search_passages.side_effect = EvidenceSearchError("search unavailable")
    elif failure == "authority":
        service.dao.resolve_public_passages.side_effect = PublicationError("authority unavailable")
    elif failure == "archive":
        service.archive.read.side_effect = OSError("missing")
    else:
        tampered = hits[0].model_copy(update={"document": hits[0].document.model_copy(update={"text": "invented"})})
        service.repository.search_vectors.return_value = ((tampered,), False)
    with pytest.raises(EvidenceSearchError):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))


def test_hybrid_empty_success_and_stale_sources_are_distinct(search_case):
    service, principal, _ = hybrid_case(search_case)
    service.repository.search_passages.return_value = ((), False)
    service.repository.search_vectors.return_value = ((), False)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.results == () and response.degraded_reason is None and response.retrieval_mode == "hybrid"
    assert response.candidate_count == 0 and not response.index_stale
    service.dao.public_search_scopes.return_value = ()
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.index_stale and response.rebuild_required and response.results == ()
    assert response.branch_status["lexical"].status == response.branch_status["vector"].status == "skipped"


def test_hybrid_refills_after_authority_filter_with_strict_candidate_budget(search_case):
    service, principal, hits = hybrid_case(search_case)
    original = service.dao.resolve_public_passages.return_value[0]
    documents = tuple(
        hit.document.model_copy(update={"evidence_id": f"retired-{number}", "chunk_id": f"retired-{number}"})
        for number in range(200)
        for hit in hits
    )

    async def page(*args, limit, offset, **kwargs):
        return tuple(PublicSearchHit(document=doc, score=1) for doc in documents[offset : offset + limit]), True

    service.repository.search_passages.side_effect = page
    service.repository.search_vectors.side_effect = page
    service.dao.resolve_public_passages.return_value = ()
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert response.results == () and response.candidate_count == 400 and response.excluded_count == 200
    assert response.refill_count == 2 and response.truncated
    for method in (service.repository.search_passages, service.repository.search_vectors):
        assert [(call.kwargs["offset"], call.kwargs["limit"]) for call in method.call_args_list] == [
            (0, 50),
            (50, 75),
            (125, 75),
        ]
    service.dao.resolve_public_passages.reset_mock()
    documents = (*documents[:50], hits[0].document)
    service.dao.resolve_public_passages.side_effect = [(), (original,)]
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid", limit=1), principal))
    assert response.results[0].evidence_id == original.item.item_id and response.refill_count == 1


@pytest.mark.parametrize("limit", [20, 50])
def test_hybrid_honors_requested_candidate_pool_above_ten_with_full_verification(search_case, limit):
    service, principal, hits = hybrid_case(search_case)
    original = service.dao.resolve_public_passages.return_value[0]
    matches = []
    documents = []
    for number in range(limit):
        path = f"docs/metrics-{number}.md"
        item = original.item.model_copy(update={"item_id": f"parent-{number}", "source_path": path})
        matches.append(replace(original, item=item, source=original.source.model_copy(update={"source_path": path})))
        parent = passage_document(item, search_case[2])
        documents.append(
            located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(
                update={"embedding": hits[0].document.embedding}
            )
        )
    service.dao.resolve_public_passages.return_value = tuple(matches)
    candidates = tuple(PublicSearchHit(document=document, score=1) for document in documents)
    service.repository.search_passages.return_value = (candidates, False)
    service.repository.search_vectors.return_value = (candidates, False)
    service._verify_hit = AsyncMock(wraps=service._verify_hit)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid", limit=limit), principal))
    assert len(response.results) == limit
    assert service._verify_hit.await_count == limit
    assert len(service.dao.assert_current.call_args.args[0]) == limit
    assert response.candidate_count == 2 * limit and response.refill_count == 0


def test_hybrid_rrf_parent_dedup_document_cap_and_stable_ties(search_case):
    service, principal, hits = hybrid_case(search_case)
    original = service.dao.resolve_public_passages.return_value[0]
    matches = []
    documents = []
    for number in range(6):
        item = original.item.model_copy(update={"item_id": f"parent-{number}"})
        match = replace(original, item=item)
        matches.append(match)
        parent = passage_document(item, search_case[2])
        document = located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(
            update={"embedding": hits[0].document.embedding}
        )
        documents.append(document)
    service.dao.resolve_public_passages.return_value = tuple(matches)
    lexical = tuple(PublicSearchHit(document=doc, score=1000) for doc in documents)
    vector = tuple(PublicSearchHit(document=doc, score=0.1) for doc in reversed(documents))
    service.repository.search_passages.return_value = (lexical, False)
    service.repository.search_vectors.return_value = (vector, False)
    service._verify_hit = AsyncMock(wraps=service._verify_hit)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert len(response.results) == 3 and response.excluded_count == 3
    assert [hit.evidence_id for hit in response.results][:2] == ["parent-0", "parent-5"]
    assert all(hit.matched_branches == ("lexical", "vector") for hit in response.results)
    assert response.results[0].score == pytest.approx(1 / 61 + 1 / 66)
    assert service._verify_hit.await_count == 3


def test_hybrid_multiple_children_contribute_only_once_per_parent_per_branch(search_case):
    service, principal, hits = hybrid_case(search_case)
    match = service.dao.resolve_public_passages.return_value[0]
    parent = passage_document(match.item, search_case[2])
    children = tuple(
        PublicSearchHit(
            document=located_chunk(parent, start, end, ordinal, "", CONTRACT).model_copy(
                update={"embedding": hits[0].document.embedding}
            ),
            score=1,
        )
        for start, end, ordinal in ((0, 15, 0), (10, len(parent.text), 1))
    )
    service.repository.search_passages.return_value = (children, False)
    service.repository.search_vectors.return_value = (tuple(reversed(children)), False)
    response = asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
    assert len(response.results) == 1 and response.excluded_count == 1
    assert response.results[0].fusion_score == pytest.approx(2 / 61)
    assert response.results[0].lexical_rank == response.results[0].vector_rank == 1
    assert response.results[0].chunk_id == min(child.document.chunk_id for child in children)


def test_hybrid_does_not_hide_programming_errors_as_vector_outages(search_case):
    service, principal, _ = hybrid_case(search_case)
    service.encoder.query.side_effect = TypeError("unexpected implementation bug")
    with pytest.raises(TypeError, match="implementation"):
        asyncio.run(service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal))
