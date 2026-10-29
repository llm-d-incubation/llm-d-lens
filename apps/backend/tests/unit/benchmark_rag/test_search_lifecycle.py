"""OpenSearch adapter contracts independent of the retired SQL coordinator."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from llm_d_lens.benchmark_rag.embedding import CONTRACT
from llm_d_lens.benchmark_rag.opensearch_evidence_search import (
    ACTIVE_ALIAS,
    VECTOR_ALIAS,
    OpenSearchEvidenceProjectionWriter,
    OpenSearchEvidenceSearchRepository,
)
from llm_d_lens.benchmark_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchScope,
)


def build(*, vector=False):
    return PublicSearchBuild(
        build_id="build-vector" if vector else "build-lexical",
        scopes=(PublicSearchScope(origin_id="benchmark", manifest_id="manifest", generation=1),),
        document_count=3,
        retrieval_mode="opensearch_vector" if vector else "opensearch_lexical",
        embedding_contract=CONTRACT if vector else None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"unowned-index": {"aliases": {ACTIVE_ALIAS: {}}}},
        {"public-rag-v1-a": {"aliases": {}}},
        {"public-rag-v1-a": {"aliases": {ACTIVE_ALIAS: {}}}, "public-rag-v1-b": {"aliases": {ACTIVE_ALIAS: {}}}},
    ],
)
def test_adapter_rejects_malformed_or_ambiguous_alias(payload):
    repository = Mock(_request=AsyncMock(return_value=payload))
    with pytest.raises(EvidenceSearchError, match="alias"):
        asyncio.run(OpenSearchEvidenceProjectionWriter(repository).alias_target())


@pytest.mark.parametrize("vector", [False, True])
def test_adapter_rollback_removes_only_the_selected_owned_alias(vector):
    alias = VECTOR_ALIAS if vector else ACTIVE_ALIAS
    repository = Mock(
        _request=AsyncMock(
            side_effect=[
                {"public-rag-v1-a": {"aliases": {alias: {}}}},
                {"acknowledged": True},
            ]
        )
    )
    asyncio.run(OpenSearchEvidenceProjectionWriter(repository).switch_alias(None, vector))
    assert repository._request.call_args.kwargs["body"] == {
        "actions": [{"remove": {"index": "public-rag-v1-a", "alias": alias}}]
    }


def test_adapter_fixed_index_metadata_cannot_claim_another_build():
    repository = Mock(
        _request=AsyncMock(
            return_value={"public-rag-v1-other": {"mappings": {"_meta": build().model_dump(mode="json")}}}
        )
    )
    with pytest.raises(EvidenceSearchError, match="metadata"):
        asyncio.run(OpenSearchEvidenceProjectionWriter(repository).index_build("public-rag-v1-other"))


def paginated_repository():
    document = PublicPassageDocument(
        chunk_id="chunk",
        evidence_id="evidence",
        origin_id="benchmark",
        manifest_id="manifest",
        publication_generation=1,
        source_revision="a" * 40,
        content_sha256="b" * 64,
        source_path="docs/metrics.md",
        title="Metric",
        heading_path=(),
        text="text",
        chunk_start=0,
        chunk_end=4,
        chunk_index=0,
        chunking_version=CONTRACT.chunking_version,
        embedding_input_sha256="c" * 64,
        embedding=(1.0,),
    )
    payload = {
        "timed_out": False,
        "_shards": {"failed": 0},
        "hits": {"total": {"value": 126}, "hits": [{"_source": document.model_dump(mode="json"), "_score": 1}]},
    }
    repository = OpenSearchEvidenceSearchRepository("http://unused")
    repository._request = AsyncMock(return_value=payload)
    scopes = (PublicSearchScope(origin_id="benchmark", manifest_id="manifest", generation=1),)
    return repository, scopes


@pytest.mark.parametrize("vector", [False, True])
def test_adapter_pages_use_bounded_offsets_and_stable_vector_prefix(vector):
    repository, scopes = paginated_repository()

    async def pages():
        for offset, limit in [(0, 50), (50, 75), (125, 75)]:
            if vector:
                hits, more = await repository.search_vectors(
                    "public-rag-v1-fixed", (1.0,), scopes, dimension=1, limit=limit, offset=offset
                )
            else:
                hits, more = await repository.search_passages(
                    "public-rag-v1-fixed", "metric", scopes, limit=limit, offset=offset
                )
            assert len(hits) == 1
            assert more == (not vector and offset < 125)
            body = repository._request.call_args.kwargs["body"]
            assert body["size"] == limit
            if offset:
                assert body["from"] == offset
            else:
                assert "from" not in body
            if vector:
                assert body["query"]["knn"]["embedding"]["k"] == 200

    asyncio.run(pages())


@pytest.mark.parametrize("vector", [False, True])
@pytest.mark.parametrize("offset,limit", [(-1, 10), (150, 51), (200, 1), (0, 201), (0, 0)])
def test_adapter_rejects_out_of_budget_pagination_before_transport(vector, offset, limit):
    repository, scopes = paginated_repository()

    async def run():
        if vector:
            await repository.search_vectors(
                "public-rag-v1-fixed", (1.0,), scopes, dimension=1, limit=limit, offset=offset
            )
        else:
            await repository.search_passages("public-rag-v1-fixed", "metric", scopes, limit=limit, offset=offset)

    with pytest.raises(ValueError):
        asyncio.run(run())
    repository._request.assert_not_called()


@pytest.mark.parametrize("lookup", ["alias", "physical"])
@pytest.mark.parametrize("tamper", [None, "dimension", "metric", "engine", "type", "missing"])
def test_vector_alias_and_recovery_validate_actual_mapping_not_only_metadata(lookup, tamper):
    metadata = build(vector=True)
    index = "public-rag-v1-" + metadata.build_id
    embedding = {
        "type": "knn_vector",
        "dimension": CONTRACT.dimension,
        "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
    }
    if tamper == "dimension":
        embedding["dimension"] = 768
    elif tamper == "metric":
        embedding["method"]["space_type"] = "l2"
    elif tamper == "engine":
        embedding["method"]["engine"] = "faiss"
    elif tamper == "type":
        embedding["type"] = "float"
    mappings = {"_meta": metadata.model_dump(mode="json"), "properties": {"embedding": embedding}}
    if tamper == "missing":
        mappings["properties"] = {}
    repository = OpenSearchEvidenceSearchRepository("http://unused")
    repository._request = AsyncMock(return_value={index: {"mappings": mappings}})

    async def run():
        if lookup == "alias":
            actual_index, actual_build = await repository.active_build(vector=True)
            assert actual_index == index
            return actual_build
        return await OpenSearchEvidenceProjectionWriter(repository).index_build(index)

    if tamper is None:
        assert asyncio.run(run()) == metadata
    else:
        with pytest.raises(EvidenceSearchError, match="metadata"):
            asyncio.run(run())
