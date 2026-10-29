"""Rebuildable OpenSearch projections for published public RAG passages."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from llm_d_lens.benchmark_rag.contracts import model_identity_aliases
from llm_d_lens.benchmark_rag.embedding import validate_vector
from llm_d_lens.benchmark_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchHit,
    PublicSearchScope,
)

INDEX_PREFIX = "public-rag-v1-"
ACTIVE_ALIAS = "public-rag-active"
VECTOR_ALIAS = "public-rag-vector-shadow"


def source_filter(scopes: tuple[PublicSearchScope, ...]) -> dict[str, Any]:
    return {
        "bool": {
            "filter": [
                {"term": {"access_classification": "verified_public"}},
                {
                    "bool": {
                        "should": [
                            {
                                "bool": {
                                    "filter": [
                                        {"term": {"origin_id": scope.origin_id}},
                                        {"term": {"manifest_id": scope.manifest_id}},
                                        {"term": {"publication_generation": scope.generation}},
                                    ]
                                }
                            }
                            for scope in scopes
                        ],
                        "minimum_should_match": 1,
                    }
                },
            ]
        }
    }


def technical_identifiers(text: str, *, expand_aliases: bool = True) -> tuple[str, ...]:
    identifiers = set(re.findall(r"--[\w-]+|[a-z0-9_][a-z0-9_./:-]*", text.lower()))
    if expand_aliases:
        identifiers = {alias for identifier in identifiers for alias in model_identity_aliases(identifier)}
    return tuple(sorted(identifiers))


def alias_query_text(text: str) -> str:
    """Expand retrieval input without changing the user question or cited passage."""
    aliases = sorted(set(technical_identifiers(text)) - set(technical_identifiers(text, expand_aliases=False)))
    return text + "\n" + " ".join(aliases) if aliases else text


def _mapping_build(index: str, value: Any) -> PublicSearchBuild:
    mappings = value["mappings"]
    build = PublicSearchBuild.model_validate(mappings["_meta"])
    if index != INDEX_PREFIX + build.build_id:
        raise ValueError("public index and build identity differ")
    if build.embedding_contract is not None:
        embedding = mappings["properties"]["embedding"]
        if not isinstance(embedding, dict) or not isinstance(embedding.get("method"), dict):
            raise ValueError("public vector mapping is missing its method")
        method = embedding["method"]
        if (
            embedding.get("type") != "knn_vector"
            or type(embedding.get("dimension")) is not int
            or embedding["dimension"] != build.embedding_contract.dimension
            or method.get("space_type") != "cosinesimil"
            or method.get("engine") != "lucene"
            or method.get("name") != "hnsw"
        ):
            raise ValueError("public vector mapping does not implement its embedding contract")
    return build


class OpenSearchEvidenceSearchRepository:
    """Search public projections; authority is rechecked by the RAG service."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("OpenSearch endpoint must be an absolute HTTP(S) service URL")
        if parsed.username or parsed.password:
            raise ValueError("OpenSearch credentials must not be embedded in its URL")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def _request(
        self, method: str, path: str, *, body: object = None, content: str | None = None, allow_missing: bool = False
    ) -> dict[str, Any]:
        headers = {"Authorization": f"ApiKey {self._api_key}"} if self._api_key else {}
        if content is not None:
            headers["Content-Type"] = "application/x-ndjson"
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.request(
                        method, self._endpoint + path, headers=headers, json=body, content=content
                    )
            else:
                response = await self._client.request(
                    method,
                    self._endpoint + path,
                    headers=headers,
                    json=body,
                    content=content,
                    timeout=self._timeout_seconds,
                )
            if allow_missing and response.status_code == 404:
                return {}
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EvidenceSearchError("OpenSearch public RAG request failed") from error
        if not isinstance(payload, dict):
            raise EvidenceSearchError("OpenSearch returned an invalid public RAG response")
        return payload

    async def active_build(self, *, vector: bool = False) -> tuple[str, PublicSearchBuild]:
        alias = VECTOR_ALIAS if vector else ACTIVE_ALIAS
        payload = await self._request("GET", f"/{alias}/_mapping")
        try:
            if len(payload) != 1:
                raise ValueError("public alias must resolve to exactly one index")
            index, value = next(iter(payload.items()))
            build = _mapping_build(index, value)
            if (build.retrieval_mode == "opensearch_vector") != vector:
                raise ValueError("public alias has the wrong retrieval mode")
            return index, build
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise EvidenceSearchError("Public RAG active index metadata is invalid") from error

    async def search_passages(
        self,
        index: str,
        text: str,
        scopes: tuple[PublicSearchScope, ...],
        *,
        limit: int,
        offset: int = 0,
    ) -> tuple[tuple[PublicSearchHit, ...], bool]:
        if not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index):
            raise ValueError("public retrieval requires a fixed physical index")
        if not scopes or not 1 <= limit <= 200 or offset < 0 or offset + limit > 200:
            raise ValueError("public retrieval needs a bounded source scope")
        query = {
            "bool": {
                "filter": source_filter(scopes)["bool"]["filter"],
                "should": [
                    {
                        "multi_match": {
                            "query": text,
                            "fields": ["title^3", "heading_path^2", "text", "text.english", "text.cjk"],
                        }
                    },
                    {"terms": {"identifiers": list(technical_identifiers(text))[:32], "boost": 2}},
                ],
                "minimum_should_match": 1,
            }
        }
        payload = await self._request(
            "POST",
            f"/{index}/_search",
            body={
                "size": limit,
                **({"from": offset} if offset else {}),
                "query": query,
                "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
                "track_total_hits": True,
            },
        )
        try:
            if payload["timed_out"] or payload["_shards"]["failed"] != 0:
                raise ValueError("incomplete public RAG search")
            raw = payload["hits"]["hits"]
            if not isinstance(raw, list) or len(raw) > limit:
                raise ValueError("invalid hit count")
            hits = tuple(PublicSearchHit(document=hit["_source"], score=hit["_score"]) for hit in raw)
            return hits, payload["hits"]["total"]["value"] > offset + len(hits)
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise EvidenceSearchError("OpenSearch public RAG results are invalid or incomplete") from error

    async def search_vectors(
        self,
        index: str,
        vector: tuple[float, ...],
        scopes: tuple[PublicSearchScope, ...],
        *,
        dimension: int,
        limit: int,
        offset: int = 0,
    ) -> tuple[tuple[PublicSearchHit, ...], bool]:
        if not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index):
            raise ValueError("public retrieval requires a fixed physical index")
        if not scopes or not 1 <= limit <= 200 or offset < 0 or offset + limit > 200:
            raise ValueError("public retrieval needs a bounded source scope")
        validate_vector(vector, dimension)
        payload = await self._request(
            "POST",
            f"/{index}/_search",
            body={
                "size": limit,
                **({"from": offset} if offset else {}),
                "query": {"knn": {"embedding": {"vector": vector, "k": 200, "filter": source_filter(scopes)}}},
                "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
                "track_total_hits": True,
            },
        )
        try:
            if payload["timed_out"] or payload["_shards"]["failed"] != 0:
                raise ValueError("incomplete public vector search")
            raw = payload["hits"]["hits"]
            if not isinstance(raw, list) or len(raw) > limit:
                raise ValueError("invalid vector hit count")
            hits = tuple(PublicSearchHit(document=hit["_source"], score=hit["_score"]) for hit in raw)
            for hit in hits:
                if hit.document.embedding is None:
                    raise ValueError("vector result omitted its embedding")
                validate_vector(hit.document.embedding, dimension)
            return hits, len(hits) == limit
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise EvidenceSearchError("OpenSearch vector results are invalid or incomplete") from error


class OpenSearchEvidenceProjectionWriter:
    """Bulk-write public passages and activate only a verified complete build."""

    def __init__(self, repository: OpenSearchEvidenceSearchRepository) -> None:
        self.repository = repository

    async def create(self, build: PublicSearchBuild) -> str:
        index = INDEX_PREFIX + build.build_id
        keywords = (
            "chunk_id",
            "evidence_id",
            "origin_id",
            "manifest_id",
            "source_revision",
            "content_sha256",
            "source_path",
            "access_classification",
            "identifiers",
            "chunking_version",
            "embedding_input_sha256",
        )
        settings: dict[str, Any] = {"number_of_shards": 1, "number_of_replicas": 0}
        vector_fields = {}
        if build.embedding_contract is not None:
            settings["index.knn"] = True
            vector_fields["embedding"] = {
                "type": "knn_vector",
                "dimension": build.embedding_contract.dimension,
                "method": {
                    "name": "hnsw",
                    "engine": "lucene",
                    "space_type": "cosinesimil",
                    "parameters": {"ef_construction": 128, "m": 16},
                },
            }
        payload = await self.repository._request(
            "PUT",
            f"/{index}",
            body={
                "settings": settings,
                "mappings": {
                    "dynamic": "strict",
                    "_meta": build.model_dump(mode="json"),
                    "properties": {
                        **{field: {"type": "keyword"} for field in keywords},
                        "publication_generation": {"type": "long"},
                        "title": {"type": "text"},
                        "heading_path": {"type": "text"},
                        "chunk_start": {"type": "long"},
                        "chunk_end": {"type": "long"},
                        "chunk_index": {"type": "integer"},
                        "embedding_context": {"type": "text", "index": False},
                        **vector_fields,
                        "text": {
                            "type": "text",
                            "fields": {
                                "english": {"type": "text", "analyzer": "english"},
                                "cjk": {"type": "text", "analyzer": "cjk"},
                            },
                        },
                    },
                },
            },
        )
        if payload.get("acknowledged") is not True:
            raise EvidenceSearchError("OpenSearch did not acknowledge public index creation")
        return index

    async def write(self, index: str, documents: Iterable[PublicPassageDocument]) -> int:
        if not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index):
            raise ValueError("public writer requires a fixed physical index")
        documents = tuple(documents)
        if not documents or len(documents) > 200:
            raise ValueError("public bulk batches must contain 1–200 passages")
        lines = []
        for document in documents:
            lines.append(json.dumps({"index": {"_index": index, "_id": document.chunk_id}}))
            if document.embedding is not None:
                validate_vector(document.embedding, len(document.embedding))
            lines.append(document.model_dump_json(exclude_none=True))
        payload = await self.repository._request("POST", "/_bulk", content="\n".join(lines) + "\n")
        try:
            items = payload["items"]
            if payload["errors"] is not False or len(items) != len(documents):
                raise ValueError("partial public projection write")
            for document, item in zip(documents, items, strict=True):
                operation = item["index"]
                if (
                    operation["status"] not in (200, 201)
                    or operation["_id"] != document.chunk_id
                    or operation["_index"] != index
                    or "error" in operation
                ):
                    raise ValueError("public projection was rejected")
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceSearchError("OpenSearch rejected or omitted public RAG projections") from error
        return len(documents)

    async def verify(self, index: str, expected: int) -> None:
        refreshed = await self.repository._request("POST", f"/{index}/_refresh")
        count = await self.repository._request("GET", f"/{index}/_count")
        if refreshed.get("_shards", {}).get("failed") != 0 or count.get("count") != expected:
            raise EvidenceSearchError("Public RAG index visibility or document count is incomplete")

    async def activate(self, index: str, *, vector: bool = False) -> None:
        await self.switch_alias(index, vector)

    async def alias_target(self, vector: bool = False) -> str | None:
        alias = VECTOR_ALIAS if vector else ACTIVE_ALIAS
        payload = await self.repository._request("GET", f"/_alias/{alias}", allow_missing=True)
        if not payload:
            return None
        if len(payload) != 1:
            raise EvidenceSearchError("Public RAG alias must resolve to exactly one index")
        index, value = next(iter(payload.items()))
        if (
            not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index)
            or not isinstance(value, dict)
            or alias not in value.get("aliases", {})
        ):
            raise EvidenceSearchError("Public RAG alias points outside its owned index namespace")
        return index

    async def index_build(self, index: str) -> PublicSearchBuild:
        if not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index):
            raise ValueError("public metadata requires a fixed physical index")
        payload = await self.repository._request("GET", f"/{index}/_mapping")
        try:
            if set(payload) != {index}:
                raise ValueError("physical index metadata is ambiguous")
            return _mapping_build(index, payload[index])
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise EvidenceSearchError("Public RAG physical index metadata is invalid") from error

    async def switch_alias(
        self,
        index: str | None,
        vector: bool = False,
        *,
        expected_previous: str | None = None,
        check_previous: bool = False,
    ) -> None:
        if index is not None and not re.fullmatch(r"public-rag-v1-[a-z0-9][a-z0-9-]{0,63}", index):
            raise ValueError("public activation requires a fixed physical index")
        alias = VECTOR_ALIAS if vector else ACTIVE_ALIAS
        old = await self.alias_target(vector)
        if check_previous and old != expected_previous:
            raise EvidenceSearchError("Public alias changed during serialized publication")
        actions: list[dict[str, dict[str, Any]]] = (
            [{"remove": {"index": old, "alias": alias}}] if old is not None else []
        )
        if actions and check_previous:
            actions[0]["remove"]["must_exist"] = True
        if index is not None:
            actions.append({"add": {"index": index, "alias": alias}})
        if not actions:
            return
        payload = await self.repository._request("POST", "/_aliases", body={"actions": actions})
        if payload.get("acknowledged") is not True or payload.get("errors") is True:
            raise EvidenceSearchError("OpenSearch did not acknowledge public index activation")
