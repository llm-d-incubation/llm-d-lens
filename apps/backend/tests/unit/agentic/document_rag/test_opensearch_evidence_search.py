"""Public-only transport, index identity and partial-failure contracts."""

import asyncio
import json

import httpx
import pytest

from llm_d_lens.agentic.document_rag.embedding import CONTRACT, located_chunk
from llm_d_lens.agentic.document_rag.opensearch_evidence_search import (
    OpenSearchEvidenceProjectionWriter,
    OpenSearchEvidenceSearchRepository,
    alias_query_text,
    technical_identifiers,
)
from llm_d_lens.agentic.document_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchScope,
)
from llm_d_lens.agentic.document_rag.sources import BENCHMARK_ORIGIN


def document():
    return PublicPassageDocument(
        chunk_id="evidence-1",
        evidence_id="evidence-1",
        origin_id=BENCHMARK_ORIGIN,
        manifest_id="manifest-1",
        publication_generation=1,
        source_revision="a" * 40,
        content_sha256="b" * 64,
        source_path="docs/metrics.md",
        title="TTFT",
        heading_path=("TTFT",),
        text="Time to first token",
        identifiers=("ttft",),
    )


def test_technical_identifiers_preserve_parameters_and_model_paths():
    assert {"llm-d", "--enable-prefix-caching", "org/model-v1", "h100"} <= set(
        technical_identifiers("llm-d --enable-prefix-caching org/Model-v1 H100")
    )


@pytest.mark.parametrize(
    "full,basename,hyphen",
    [
        ("Acme/Atlas-7B-Instruct-FP8", "Atlas-7B-Instruct-FP8", "Acme-Atlas-7B-Instruct-FP8"),
        ("Research/Division/Nova-4bit", "Nova-4bit", "Research-Division-Nova-4bit"),
    ],
)
def test_generated_identifier_aliases_match_in_both_directions_without_a_registry(full, basename, hyphen):
    forms = (full, basename, hyphen)
    for indexed in forms:
        for query in forms:
            if full in (indexed, query) or indexed == query:
                assert set(technical_identifiers(indexed.upper())) & set(technical_identifiers(query.lower()))
    assert set(technical_identifiers(full)) == {form.lower() for form in forms}
    assert not set(technical_identifiers(full)) & set(technical_identifiers(full.rsplit("-", 1)[0]))
    expanded = alias_query_text("Explain " + full)
    assert expanded.startswith("Explain " + full + "\n")
    assert basename.lower() in expanded and hyphen.lower() in expanded
    assert alias_query_text("Explain " + basename) == "Explain " + basename


def test_serialized_alias_switch_uses_atomic_expected_removal():
    actions = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"public-rag-v1-old": {"aliases": {"public-rag-active": {}}}})
        actions.extend(json.loads(request.content)["actions"])
        return httpx.Response(200, json={"acknowledged": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            writer = OpenSearchEvidenceProjectionWriter(
                OpenSearchEvidenceSearchRepository("http://search", client=client)
            )
            await writer.switch_alias("public-rag-v1-new", expected_previous="public-rag-v1-old", check_previous=True)

    asyncio.run(run())
    assert actions == [
        {"remove": {"index": "public-rag-v1-old", "alias": "public-rag-active", "must_exist": True}},
        {"add": {"index": "public-rag-v1-new", "alias": "public-rag-active"}},
    ]


def test_serialized_alias_drift_fails_before_mutation():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"public-rag-v1-other": {"aliases": {"public-rag-active": {}}}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            writer = OpenSearchEvidenceProjectionWriter(
                OpenSearchEvidenceSearchRepository("http://search", client=client)
            )
            with pytest.raises(EvidenceSearchError, match="changed"):
                await writer.switch_alias(
                    "public-rag-v1-new", expected_previous="public-rag-v1-old", check_previous=True
                )

    asyncio.run(run())
    assert calls == ["GET"]


def test_alias_action_errors_are_not_success_even_when_acknowledged():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"acknowledged": True, "errors": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            writer = OpenSearchEvidenceProjectionWriter(
                OpenSearchEvidenceSearchRepository("http://search", client=client)
            )
            with pytest.raises(EvidenceSearchError, match="acknowledge"):
                await writer.switch_alias("public-rag-v1-new")

    asyncio.run(run())


def test_search_enforces_public_manifest_and_physical_index():
    captured = {}
    doc = document()
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest-1", generation=1)

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {"total": {"value": 1}, "hits": [{"_source": doc.model_dump(mode="json"), "_score": 1}]},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            repository = OpenSearchEvidenceSearchRepository("http://search", client=client)
            result, more = await repository.search_passages(
                "public-rag-v1-test", "Acme/Atlas-7B-FP8", (scope,), limit=10
            )
            assert result[0].document == doc and not more
            with pytest.raises(ValueError):
                await repository.search_passages("evidence-diagnostics-v1", "TTFT", (scope,), limit=10)

    asyncio.run(run())
    filters = captured["query"]["bool"]["filter"]
    assert filters[0] == {"term": {"access_classification": "verified_public"}}
    assert filters[1]["bool"]["should"][0]["bool"]["filter"] == [
        {"term": {"origin_id": BENCHMARK_ORIGIN}},
        {"term": {"manifest_id": "manifest-1"}},
        {"term": {"publication_generation": 1}},
    ]
    assert captured["query"]["bool"]["should"][0]["multi_match"]["query"] == "Acme/Atlas-7B-FP8"
    assert set(captured["query"]["bool"]["should"][1]["terms"]["identifiers"]) == {
        "acme/atlas-7b-fp8",
        "atlas-7b-fp8",
        "acme-atlas-7b-fp8",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"timed_out": True, "_shards": {"failed": 0}},
        {"timed_out": False, "_shards": {"failed": 1}},
        {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": "invalid"}},
    ],
)
def test_incomplete_search_is_not_a_successful_empty_result(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as c:
            repository = OpenSearchEvidenceSearchRepository("http://search", client=c)
            with pytest.raises(EvidenceSearchError):
                await repository.search_passages(
                    "public-rag-v1-test",
                    "TTFT",
                    (PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="m", generation=1),),
                    limit=10,
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": True, "items": []},
        {"errors": False, "items": []},
        {"errors": False, "items": [{"index": {"status": 400, "_id": "evidence-1", "_index": "public-rag-v1-test"}}]},
        {"errors": False, "items": [{"index": {"status": 201, "_id": "wrong", "_index": "public-rag-v1-test"}}]},
    ],
)
def test_bulk_rejections_and_missing_items_are_explicit(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as c:
            writer = OpenSearchEvidenceProjectionWriter(OpenSearchEvidenceSearchRepository("http://search", client=c))
            with pytest.raises(EvidenceSearchError):
                await writer.write("public-rag-v1-test", [document()])

    asyncio.run(run())


def test_bulk_uses_stable_evidence_id_and_checks_every_item():
    def handler(request):
        action, source = map(json.loads, request.content.decode().splitlines())
        assert action == {"index": {"_index": "public-rag-v1-test", "_id": "evidence-1"}}
        assert source["access_classification"] == "verified_public"
        assert "tenant_id" not in source
        return httpx.Response(
            200,
            json={
                "errors": False,
                "items": [
                    {"index": {"status": 201, "_id": "evidence-1", "_index": "public-rag-v1-test"}},
                ],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            writer = OpenSearchEvidenceProjectionWriter(OpenSearchEvidenceSearchRepository("http://search", client=c))
            assert await writer.write("public-rag-v1-test", [document()]) == 1

    asyncio.run(run())


def test_active_build_rejects_diagnostic_index_even_with_valid_metadata():
    build = PublicSearchBuild(
        build_id="test",
        document_count=1,
        scopes=(PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="m", generation=1),),
    )

    async def run():
        payload = {"evidence-diagnostics-v1": {"mappings": {"_meta": build.model_dump(mode="json")}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as c:
            with pytest.raises(EvidenceSearchError):
                await OpenSearchEvidenceSearchRepository("http://search", client=c).active_build()

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_connection_failure_is_a_domain_503(error_type):
    def handler(request):
        raise error_type("unavailable", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(EvidenceSearchError) as error:
                await OpenSearchEvidenceSearchRepository("http://search", client=c).active_build()
            assert error.value.status_code == 503

    asyncio.run(run())


def test_vector_mapping_and_pre_retrieval_filter_use_the_pinned_contract():
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest-1", generation=1)
    build = PublicSearchBuild(
        build_id="vector",
        document_count=1,
        scopes=(scope,),
        retrieval_mode="opensearch_vector",
        embedding_contract=CONTRACT,
    )
    vector = (1.0, *([0.0] * 383))
    parent = document()
    chunk = located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(update={"embedding": vector})

    def handler(request):
        payload = json.loads(request.content)
        if request.method == "PUT":
            mapping = payload["mappings"]
            assert payload["settings"]["index.knn"] is True
            assert mapping["_meta"]["embedding_contract"] == CONTRACT.model_dump()
            assert mapping["properties"]["embedding"]["dimension"] == 384
            assert mapping["properties"]["embedding"]["method"]["space_type"] == "cosinesimil"
            return httpx.Response(200, json={"acknowledged": True})
        branch = payload["query"]["knn"]["embedding"]
        assert branch["k"] == 200 and len(branch["vector"]) == 384
        filters = branch["filter"]["bool"]["filter"]
        assert filters[0] == {"term": {"access_classification": "verified_public"}}
        assert filters[1]["bool"]["should"][0]["bool"]["filter"][1] == {"term": {"manifest_id": "manifest-1"}}
        return httpx.Response(
            200,
            json={
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {"hits": [{"_source": chunk.model_dump(mode="json"), "_score": 1}]},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            repository = OpenSearchEvidenceSearchRepository("http://search", client=client)
            index = await OpenSearchEvidenceProjectionWriter(repository).create(build)
            hits, more = await repository.search_vectors(index, vector, (scope,), dimension=384, limit=200)
            assert len(hits) == 1 and not more

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["nan", "dimension", "timeout", "shard", "invalid_json"])
def test_vector_transport_rejects_invalid_results_and_partial_failures(failure):
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest-1", generation=1)
    vector = (1.0, *([0.0] * 383))
    parent = document()
    chunk = located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT).model_copy(update={"embedding": vector})

    def handler(request):
        if failure == "invalid_json":
            return httpx.Response(200, text="{")
        source = chunk.model_dump(mode="json")
        if failure == "dimension":
            source["embedding"] = [1]
        if failure == "nan":
            source["embedding"] = ["NaN", *([0] * 383)]
        return httpx.Response(
            200,
            json={
                "timed_out": failure == "timeout",
                "_shards": {"failed": int(failure == "shard")},
                "hits": {"hits": [{"_source": source, "_score": 1}]},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            repository = OpenSearchEvidenceSearchRepository("http://search", client=client)
            with pytest.raises(EvidenceSearchError):
                await repository.search_vectors("public-rag-v1-test", vector, (scope,), dimension=384, limit=200)

    asyncio.run(run())
