"""Real-time metadata authority and failure semantics without an OpenSearch server."""

from __future__ import annotations

import asyncio
import copy
import json
import multiprocessing
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from urllib.parse import unquote

import httpx
import pytest

from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.document_migration import promote_documents, shadow_copy_documents
from llm_d_lens.benchmark_rag.document_repository import (
    MANAGEMENT_INDEX,
    METADATA_READ_BATCH_SIZE,
    DocumentRepository,
    OpenSearchDocumentRepository,
    configured_document_repository,
)
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationConflictError,
    PublicationError,
    ReleaseManifest,
    SourceApproval,
    content_digest,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.query_contracts import QueryFilters
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN, SourceArchive, allowed_paths
from llm_d_lens.core.exceptions import ConflictError


class MetadataServer:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.sequence = 0
        self.reject_cas = False
        self.mapping: dict = {}
        self.fail_key: str | None = None
        self.scroll_cleared = False
        self.retrieval_alias: dict = {}
        self.retrieval_hits: list[dict] = []
        self.projection_mappings: dict[str, dict] = {}
        self.projection_documents: dict[str, list[dict]] = {}
        self.aliases: dict[str, str] = {}
        self.management_refreshes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/_alias/"):
            alias = path.removeprefix("/_alias/")
            index = self.aliases.get(alias)
            return httpx.Response(200 if index else 404, json={index: {"aliases": {alias: {}}}} if index else {})
        if path == "/_aliases":
            for action in json.loads(request.content)["actions"]:
                if "remove" in action:
                    self.aliases.pop(action["remove"]["alias"], None)
                if "add" in action:
                    self.aliases[action["add"]["alias"]] = action["add"]["index"]
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            lines = request.content.decode().splitlines()
            items = []
            for offset in range(0, len(lines), 2):
                operation = json.loads(lines[offset])["index"]
                document = json.loads(lines[offset + 1])
                self.projection_documents[operation["_index"]].append(document)
                items.append({"index": {**operation, "status": 201}})
            return httpx.Response(200, json={"errors": False, "items": items})
        parts = path.strip("/").split("/")
        if parts[0].startswith("public-rag-v1-"):
            index = parts[0]
            if request.method == "PUT" and len(parts) == 1:
                self.projection_mappings[index] = json.loads(request.content)["mappings"]
                self.projection_documents[index] = []
                return httpx.Response(200, json={"acknowledged": True})
            if len(parts) == 2 and index in self.projection_mappings:
                if parts[1] == "_mapping":
                    return httpx.Response(200, json={index: {"mappings": self.projection_mappings[index]}})
                if parts[1] == "_refresh":
                    return httpx.Response(200, json={"_shards": {"failed": 0}})
                if parts[1] == "_count":
                    return httpx.Response(200, json={"count": len(self.projection_documents[index])})
        if path == "/public-rag-active/_mapping" and "public-rag-active" in self.aliases:
            index = self.aliases["public-rag-active"]
            return httpx.Response(200, json={index: {"mappings": self.projection_mappings[index]}})
        if request.url.path == "/public-rag-active/_mapping":
            return httpx.Response(200, json=self.retrieval_alias)
        if request.url.path.startswith("/public-rag-v1-") and request.url.path.endswith("/_search"):
            hits = self.retrieval_hits
            if parts[0] in self.projection_documents:
                hits = [{"_source": item, "_score": 1} for item in self.projection_documents[parts[0]]]
            return httpx.Response(
                200,
                json={
                    "timed_out": False,
                    "_shards": {"failed": 0},
                    "hits": {"hits": hits, "total": {"value": len(hits)}},
                },
            )
        if request.url.path == "/_search/scroll":
            if request.method == "DELETE":
                self.scroll_cleared = True
                return httpx.Response(200, json={"succeeded": True})
            return httpx.Response(200, json={"_scroll_id": "cursor", "hits": {"hits": []}})
        path = request.url.path.removeprefix(f"/{MANAGEMENT_INDEX}")
        if path == "/_mget" and request.method == "POST":
            docs = []
            for key in json.loads(request.content)["ids"]:
                if key == self.fail_key:
                    doc = {"_id": key, "error": {"type": "unavailable"}}
                else:
                    doc = copy.deepcopy(self.records.get(key, {"_id": key, "found": False}))
                docs.append({"_index": MANAGEMENT_INDEX, **doc})
            return httpx.Response(200, json={"docs": docs})
        if path == "/_refresh" and request.method == "POST":
            self.management_refreshes += 1
            return httpx.Response(200, json={"_shards": {"failed": 0}})
        if path == "/_search":
            return httpx.Response(
                200,
                json={
                    "_scroll_id": "cursor",
                    "hits": {"hits": [{"_id": key} for key in self.records]},
                },
            )
        if not path and request.method == "PUT":
            self.mapping = json.loads(request.content)["mappings"]
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_mapping":
            return httpx.Response(200, json={MANAGEMENT_INDEX: {"mappings": self.mapping}})
        key = unquote(path.split("/", 2)[-1])
        if key == self.fail_key:
            return httpx.Response(503, json={"error": "unavailable"})
        if request.method == "GET":
            record = self.records.get(key)
            return httpx.Response(200 if record else 404, json=record or {"_id": key, "found": False})
        current = self.records.get(key)
        if "/_create/" in path and current is not None:
            return httpx.Response(409, json={"error": "conflict"})
        if "/_doc/" in path and (
            self.reject_cas
            or current is None
            or request.url.params.get("if_seq_no") != str(current["_seq_no"])
            or request.url.params.get("if_primary_term") != "1"
        ):
            return httpx.Response(409, json={"error": "conflict"})
        self.sequence += 1
        self.records[key] = {
            "_id": key,
            "_source": json.loads(request.content),
            "found": True,
            "_seq_no": self.sequence,
            "_primary_term": 1,
        }
        return httpx.Response(201, json={"result": "created"})


@pytest.fixture
def metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OpenSearchDocumentRepository, MetadataServer, RagPublicationService]:
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    server = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://metadata.test",
        client=httpx.Client(transport=httpx.MockTransport(server)),
        lock_path=tmp_path / "publication.lock",
    )
    repository.prepare(initialize=True)
    service = RagPublicationService(dao=repository, archive=SourceArchive())
    service.register_source(
        LLM_D_ORIGIN,
        SourceApproval(
            verified_public=True,
            approved_by="reviewer",
            approval_reference="review/1",
            license_reference="Apache-2.0",
            parser_version="test-v1",
            allowed_paths=allowed_paths(LLM_D_ORIGIN),
        ),
    )
    return repository, server, service


def stage(service: RagPublicationService, revision: str = "a" * 40, text: bytes = b"Public evidence") -> RagItem:
    path = "guides/test/README.md"
    digest = service.archive.put(text)
    passage = RagItem(
        item_id=item_id(LLM_D_ORIGIN, f"{path}#passage", revision),
        kind="passage",
        origin_id=LLM_D_ORIGIN,
        logical_key=f"{path}#passage",
        source_revision=revision,
        content_sha256=digest,
        access_classification="verified_public",
        source_path=path,
        format="markdown",
        parent_id=item_id(LLM_D_ORIGIN, f"file:{path}", revision),
        passage_text=text.decode(),
        heading_path=["Overview"],
        source_pointer={"start_char": 0, "end_char": len(text)},
    )
    return service.stage(
        PinnedInventory(
            origin_id=LLM_D_ORIGIN,
            revision=revision,
            complete=True,
            files=(InventoryFile(path=path, sha256=digest, format="markdown"),),
        ),
        (ParsedFile(path=path, items=(passage,)),),
        expected_counts={"source": 1, "passage": 1},
    )


def test_default_publication_uses_document_authority_without_sql(metadata, monkeypatch) -> None:
    from unittest.mock import Mock

    from llm_d_lens.db.dao import rag_publication

    repository, _, original = metadata
    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.setattr(DocumentRepository, "from_environment", Mock(return_value=repository))
    monkeypatch.setattr(
        rag_publication, "RagPublicationDao", Mock(side_effect=AssertionError("publication opened SQL"))
    )
    service = RagPublicationService(archive=original.archive)
    manifest = stage(service)
    assert service.activate(LLM_D_ORIGIN, manifest.item_id).generation == 1
    assert repository.head(LLM_D_ORIGIN).manifest == manifest
    assert len(repository.list_published(LLM_D_ORIGIN, kind="passage")) == 1


def test_stage_activate_resolve_and_revoke(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    assert repository.public_search_scopes((LLM_D_ORIGIN,)) == ()
    assert repository.list_published(LLM_D_ORIGIN) == []
    result = service.activate(LLM_D_ORIGIN, manifest.item_id)
    assert result.generation == 1
    assert not service.activate(LLM_D_ORIGIN, manifest.item_id).changed
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    matches = repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))
    assert matches[0].item == passage
    assert matches[0].source.item_id == passage.parent_id
    assert matches[0].experiment is None
    repository.assert_current(matches)
    repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
    assert repository.public_search_scopes((LLM_D_ORIGIN,)) == ()
    assert repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,)) == ()
    with pytest.raises(ConflictError):
        repository.assert_current(matches)
    assert all(
        request.url.params.get("realtime") == "true"
        for request in server.requests
        if request.method == "GET" and "/_doc/" in request.url.path
    )
    assert all(
        request.url.params.get("refresh") == "wait_for"
        for request in server.requests
        if request.method == "PUT" and ("/_create/" in request.url.path or "/_doc/" in request.url.path)
    )
    assert not any("_aliases" in request.url.path for request in server.requests)


def test_cas_failure_leaves_head_unchanged(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    server.reject_cas = True
    with pytest.raises(PublicationConflictError):
        service.activate(LLM_D_ORIGIN, manifest.item_id)
    assert repository.head(LLM_D_ORIGIN).manifest is None
    assert repository.head(LLM_D_ORIGIN).state.generation == 0


def test_immutable_conflict_and_corrupt_hash_fail_closed(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    with pytest.raises(PublicationError, match="immutable"):
        repository._create_immutable(manifest.model_copy(update={"source_path": "changed"}))
    server.records[manifest.item_id]["_source"]["item"]["source_path"] = "changed"
    with pytest.raises(PublicationError):
        service.activate(LLM_D_ORIGIN, manifest.item_id)
    assert repository.head(LLM_D_ORIGIN).manifest is None


def test_stale_stage_and_rollback(metadata) -> None:
    repository, _, service = metadata
    first = stage(service)
    service.activate(LLM_D_ORIGIN, first.item_id)
    second = stage(service, "b" * 40, b"Second release")
    service.activate(LLM_D_ORIGIN, second.item_id)
    with pytest.raises(PublicationConflictError):
        service.activate(LLM_D_ORIGIN, first.item_id)
    result = service.rollback(LLM_D_ORIGIN, first.item_id, expected_generation=2)
    assert result.generation == 3
    assert repository.list_published(LLM_D_ORIGIN)[0].passage_text == "Public evidence"
    with pytest.raises(PublicationConflictError):
        repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=2)


def test_missing_member_and_source_unavailability_fail_closed(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    server.fail_key = passage.parent_id
    with pytest.raises(PublicationError):
        repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))
    server.fail_key = None
    del server.records[passage.parent_id]
    with pytest.raises(PublicationError):
        repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))


def test_resolution_batches_members_and_does_not_cache_authority(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    server.requests.clear()
    assert repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))[0].item == passage
    batches = [request for request in server.requests if request.url.path.endswith("/_mget")]
    assert len(batches) == 1
    assert set(json.loads(batches[0].content)["ids"]) == set(manifest.manifest_item_ids)
    assert batches[0].url.params["realtime"] == "true"
    assert all(
        unquote(request.url.path.split("/_doc/")[-1]) not in manifest.manifest_item_ids
        for request in server.requests
        if "/_doc/" in request.url.path
    )
    server.records[passage.item_id]["_source"]["item"]["passage_text"] = "corrupted after first resolution"
    with pytest.raises(PublicationError):
        repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))


def test_metadata_batches_are_bounded_deduplicated_and_preserve_missing_records(metadata) -> None:
    repository, server, _ = metadata
    keys = tuple(f"missing:{number}" for number in range(METADATA_READ_BATCH_SIZE + 1))
    server.requests.clear()
    records = repository._read_records((*keys, keys[0]))
    assert records == dict.fromkeys(keys)
    batches = [request for request in server.requests if request.url.path.endswith("/_mget")]
    assert [len(json.loads(request.content)["ids"]) for request in batches] == [METADATA_READ_BATCH_SIZE, 1]


@pytest.mark.parametrize(
    "corruption",
    ["hash", "contract", "id", "index", "sequence", "term", "error", "missing_found", "missing_doc", "extra_doc"],
)
def test_metadata_batches_reject_corrupt_partial_or_misidentified_results(metadata, monkeypatch, corruption) -> None:
    repository, server, _ = metadata
    key = registry_id(LLM_D_ORIGIN)

    def transport(request):
        body = server(request).json()
        doc = body["docs"][0]
        if corruption == "hash":
            doc["_source"]["sha256"] = "0" * 64
        elif corruption == "contract":
            doc["_source"]["contract"] = "wrong"
        elif corruption == "id":
            doc["_id"] = "wrong"
        elif corruption == "index":
            doc["_index"] = "wrong"
        elif corruption == "sequence":
            doc["_seq_no"] = True
        elif corruption == "term":
            doc["_primary_term"] = 0
        elif corruption == "error":
            doc["error"] = {"type": "unavailable"}
        elif corruption == "missing_found":
            del doc["found"]
        elif corruption == "missing_doc":
            body["docs"] = []
        else:
            body["docs"].append(doc)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(repository, "client", client)
        with pytest.raises(PublicationError):
            repository._read_records((key,))


def test_batched_resolution_rechecks_current_head_after_member_validation(metadata, monkeypatch) -> None:
    repository, _, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    original = repository._members

    def members(*args, **kwargs):
        result = original(*args, **kwargs)
        repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
        return result

    monkeypatch.setattr(repository, "_members", members)
    with pytest.raises(ConflictError):
        repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))


@pytest.mark.parametrize("corruption", ["item_identity", "item_schema", "parent_membership"])
def test_batched_resolution_validates_items_and_complete_member_map_beyond_payload_hash(metadata, corruption) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    envelope = server.records[passage.item_id]["_source"]
    if corruption == "item_identity":
        envelope["item"]["item_id"] = "f" * 64
    elif corruption == "item_schema":
        envelope["item"]["kind"] = "invalid"
    else:
        envelope["item"]["parent_id"] = "f" * 64
    envelope["sha256"] = content_digest(envelope["item"])
    with pytest.raises(PublicationError):
        repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))


def test_shadow_copy_is_verified_idempotent_and_never_installs_live_heads(metadata, tmp_path: Path) -> None:
    _, server, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    snapshot = tuple(RagItem.model_validate(value["_source"]["item"]) for value in server.records.values())
    original = copy.deepcopy(server.records)
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://shadow.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "shadow.lock",
    )
    report = shadow_copy_documents(repository, snapshot)
    assert not report.cutover
    assert report.item_count == len(snapshot)
    assert all(key.startswith(f"shadow:{report.snapshot_sha256}:") for key in target.records)
    assert repository.get(registry_id(LLM_D_ORIGIN)) is None
    assert repository.public_search_scopes((LLM_D_ORIGIN,)) == ()
    assert shadow_copy_documents(repository, snapshot) == report
    assert target.management_refreshes == 2
    assert all(
        request.url.params.get("refresh") != "wait_for"
        for request in target.requests
        if request.method == "PUT" and "/_create/" in request.url.path
    )
    assert server.records == original
    assert not any("_aliases" in request.url.path for request in target.requests)


def test_shadow_copy_rejects_incomplete_snapshot_before_writing(metadata, tmp_path: Path) -> None:
    _, server, service = metadata
    manifest = stage(service)
    snapshot = tuple(
        RagItem.model_validate(value["_source"]["item"])
        for value in server.records.values()
        if value["_source"]["item"]["kind"] != "passage"
    )
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://shadow.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "shadow.lock",
    )
    with pytest.raises(PublicationError):
        shadow_copy_documents(repository, snapshot)
    assert not target.records
    assert manifest.item_id not in target.records


def test_publication_lock_serializes_repository_instances(metadata, tmp_path: Path) -> None:
    repository, _, _ = metadata
    other = OpenSearchDocumentRepository("http://metadata.test", lock_path=tmp_path / "publication.lock")
    attempted, entered = Event(), Event()

    def contender() -> None:
        attempted.set()
        with other.publication_lock():
            entered.set()

    with repository.publication_lock(), other.publication_lock():
        thread = Thread(target=contender)
        thread.start()
        assert attempted.wait(2)
        assert not entered.wait(0.05)
    thread.join(2)
    assert entered.is_set()
    other.close()


def test_publication_lock_is_not_inherited_as_reentrant_after_fork(metadata) -> None:
    repository, _, _ = metadata
    context = multiprocessing.get_context("fork")
    attempted, entered = context.Event(), context.Event()

    def contender() -> None:
        attempted.set()
        with repository.publication_lock():
            entered.set()

    with repository.publication_lock():
        process = context.Process(target=contender)
        process.start()
        assert attempted.wait(2)
        assert not entered.wait(0.05)
    process.join(3)
    if process.is_alive():
        process.terminate()
        process.join()
    assert process.exitcode == 0
    assert entered.is_set()


def test_lifecycle_records_are_hash_checked_and_cas_protected(metadata) -> None:
    repository, _, _ = metadata
    repository.create_record("build:one", {"status": "staged"})
    value, sequence, term = repository.read_record("build:one")
    assert value == {"status": "staged"}
    repository.replace_record("build:one", {"status": "active"}, sequence, term)
    with pytest.raises(PublicationConflictError):
        repository.replace_record("build:one", {"status": "failed"}, sequence, term)
    with pytest.raises(PublicationConflictError):
        repository.create_record("build:one", {})
    with pytest.raises(PublicationError):
        repository.create_record(registry_id(LLM_D_ORIGIN), {})


def test_list_records_is_sorted_bounded_and_excludes_other_namespaces(metadata) -> None:
    repository, server, _ = metadata
    repository.create_record("build:z", {"id": "z"})
    repository.create_record("build:a", {"id": "a"})
    repository.create_record("fence:lexical", {"id": "fence"})
    assert repository.list_records("build:") == ({"id": "a"}, {"id": "z"})
    assert repository.list_records("missing:") == ()
    assert server.scroll_cleared
    with pytest.raises(PublicationError, match="bounds"):
        repository.list_records("build:", max_records=1)
    with pytest.raises(PublicationError, match="bounds"):
        repository.list_records("build:", max_bytes=1)
    assert server.scroll_cleared


def test_default_lock_is_persistent_and_multi_host_configuration_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://metadata.test")
    repository = OpenSearchDocumentRepository.from_environment(client=httpx.Client())
    assert repository.lock_path == tmp_path / "data" / "public-rag" / "publication.lock"
    monkeypatch.setenv("LENS_PUBLIC_RAG_WRITER_MODE", "multi-host")
    with pytest.raises(PublicationError, match="multi-host"):
        OpenSearchDocumentRepository.from_environment(client=httpx.Client())


@pytest.mark.parametrize("authority", [None, "opensearch"])
def test_document_authority_is_mandatory_and_never_falls_back_to_sql(
    monkeypatch: pytest.MonkeyPatch,
    authority: str | None,
) -> None:
    from unittest.mock import Mock

    from llm_d_lens.db.dao import rag_query

    legacy = Mock()
    factory = Mock(return_value=legacy)
    monkeypatch.setattr(rag_query, "RagQueryDao", factory)
    if authority is None:
        monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    else:
        monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", authority)
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    with pytest.raises(PublicationError, match="OPENSEARCH_URL"):
        configured_document_repository()
    factory.assert_not_called()
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://metadata.test")
    assert isinstance(configured_document_repository(client=httpx.Client()), DocumentRepository)
    factory.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("authority", ["postgres", "invalid"])
def test_legacy_or_invalid_document_authority_is_rejected(monkeypatch, authority):
    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", authority)
    with pytest.raises(PublicationError, match="DOCUMENT_AUTHORITY"):
        configured_document_repository()
    with pytest.raises(PublicationError, match="DOCUMENT_AUTHORITY"):
        DocumentRepository.from_environment()


def test_snapshot_excludes_shadow_and_lifecycle_records_and_enforces_bounds(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    repository.create_record("build:one", {"status": "active"})
    repository._create_immutable(manifest, key="shadow:copy:" + manifest.item_id)
    snapshot = repository.snapshot()
    assert len(snapshot) == 4
    assert {item.kind for item in snapshot} == {"source", "passage"}
    assert server.scroll_cleared
    with pytest.raises(PublicationError, match="bounds"):
        repository.snapshot(max_records=1)
    assert server.scroll_cleared


@pytest.mark.parametrize("legacy_identifiers", [False, True])
def test_definition_adapter_pins_alias_and_revalidates_projection(metadata, legacy_identifiers) -> None:
    from llm_d_lens.benchmark_rag.indexing import passage_document
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import technical_identifiers
    from llm_d_lens.benchmark_rag.search_contracts import PublicSearchBuild

    repository, server, service = metadata
    manifest = stage(service, text=b"Public Acme/Atlas-7B-FP8 evidence")
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    scope = repository.public_search_scopes((LLM_D_ORIGIN,))[0]
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    build = PublicSearchBuild(build_id="test", scopes=(scope,), document_count=1)
    index = "public-rag-v1-test"
    server.retrieval_alias = {index: {"mappings": {"_meta": build.model_dump(mode="json")}}}
    repository.create_record(
        "build:test",
        {
            "status": "active",
            "physical_index": index,
            "build": build.model_dump(mode="json"),
        },
    )
    document = passage_document(passage, scope)
    if legacy_identifiers:
        document = document.model_copy(
            update={"identifiers": technical_identifiers(document.title + "\n" + document.text, expand_aliases=False)}
        )
    server.retrieval_hits = [{"_source": document.model_dump(mode="json"), "_score": 1}]
    result = repository.search(
        QueryFilters(origins=(LLM_D_ORIGIN,)),
        intent="definition",
        text_query="ACME/ATLAS-7B-FP8",
        limit=10,
    )
    assert result.matches[0].item == passage
    assert result.total == 1
    query = next(
        json.loads(request.content) for request in reversed(server.requests) if request.url.path == f"/{index}/_search"
    )
    assert {"acme/atlas-7b-fp8", "atlas-7b-fp8", "acme-atlas-7b-fp8"} == set(
        query["query"]["bool"]["should"][1]["terms"]["identifiers"]
    )
    assert (
        repository.search(
            QueryFilters(origins=(LLM_D_ORIGIN,), normalized=True),
            intent="definition",
            text_query="Public",
            limit=10,
        ).matches
        == result.matches
    )
    server.retrieval_hits[0]["_source"]["text"] = "Forged text"
    with pytest.raises(PublicationError, match="projection"):
        repository.search(
            QueryFilters(origins=(LLM_D_ORIGIN,)),
            intent="definition",
            text_query="Public",
            limit=10,
        )


@pytest.mark.parametrize(
    "constraints",
    [
        {"model": "model-a"},
        {"hardware": "device-a"},
        {"runtime": "runtime-a"},
        {"workload": {"input_tokens": 1024}},
        {"experiment_id": "experiment-a"},
        {"record_id": "record-a"},
        {"series": {"batch": 1}},
        {"metric": "ttft"},
        {"unit": "ms"},
        {"value_min": 1, "unit": "ms"},
    ],
)
def test_definition_structured_constraints_do_not_match_unlinked_passages(metadata, constraints) -> None:
    repository, _, _ = metadata
    result = repository.search(
        QueryFilters(origins=(LLM_D_ORIGIN,), **constraints),
        intent="definition",
        text_query="Public",
        limit=10,
    )
    assert result.matches == ()
    assert result.total == 0


def test_http_fixture_publication_build_alias_search_and_revoke(metadata, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
    from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceSearchRepository
    from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION
    from llm_d_lens.benchmark_rag.search import PublicRagSearchService
    from llm_d_lens.benchmark_rag.search_contracts import PublicSearchRequest
    from llm_d_lens.db.dao import rag_query

    repository, server, publication = metadata
    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", "opensearch")
    monkeypatch.setattr(rag_query, "RagQueryDao", Mock(side_effect=AssertionError("document opened SQL")))
    manifest = stage(publication)
    publication.activate(LLM_D_ORIGIN, manifest.item_id)
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            projection = OpenSearchEvidenceSearchRepository("http://metadata.test", client=client)
            lifecycle = DocumentLifecycle(repository)
            build = await PublicRagIndexBuilder(
                projection,
                dao=repository,
                archive=publication.archive,
                lifecycle=lifecycle,
            ).build((LLM_D_ORIGIN,))
            index = "public-rag-v1-" + build.build_id
            assert server.aliases["public-rag-active"] == index
            assert lifecycle.get(build.build_id).status == "active"
            service = PublicRagSearchService(
                projection,
                dao=repository,
                archive=publication.archive,
                lifecycle=lifecycle,
            )
            request = PublicSearchRequest(question="Public evidence", origins=(LLM_D_ORIGIN,))
            result = await service.search(request, principal)
            assert len(result.results) == 1 and not result.index_stale
            assert result.results[0].citation.published_manifest_id == manifest.item_id
            searches = [call for call in server.requests if call.url.path == f"/{index}/_search"]
            assert len(searches) == 1
            assert not any(call.url.path == "/public-rag-active/_search" for call in server.requests)
            repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
            withdrawn = await service.search(request, principal)
            assert withdrawn.results == () and withdrawn.index_stale
            assert len([call for call in server.requests if call.url.path == f"/{index}/_search"]) == 1
            assert lifecycle.rebuild_requested()
            assert server.aliases["public-rag-active"] == index

    asyncio.run(run())


def mixed_snapshot(
    server: MetadataServer,
    manifest: RagItem,
    *,
    include_measurement: bool = False,
) -> tuple[tuple[RagItem, ...], RagItem, RagItem]:
    metadata = ReleaseManifest.model_validate(manifest.source_details)
    source = RagItem.model_validate(server.records[metadata.files[0].file_item_id]["_source"]["item"])
    experiment = RagItem(
        item_id=item_id(source.origin_id, "experiment:private-facts", source.source_revision),
        kind="experiment",
        logical_key="experiment:private-facts",
        origin_id=source.origin_id,
        source_revision=source.source_revision,
        content_sha256=source.content_sha256,
        access_classification="verified_public",
        source_path=source.source_path,
        format=source.format,
        parent_id=source.item_id,
        model="structured-fact-body-must-stay-sql",
        source_pointer={"record_id": "experiment-facts"},
    )
    structured = (experiment,)
    counts = {**metadata.counts, "experiment": 1}
    if include_measurement:
        measurement = RagItem(
            item_id=item_id(source.origin_id, "measurement:private-facts", source.source_revision),
            kind="measurement",
            logical_key="measurement:private-facts",
            origin_id=source.origin_id,
            source_revision=source.source_revision,
            content_sha256=source.content_sha256,
            access_classification="verified_public",
            source_path=source.source_path,
            format=source.format,
            parent_id=experiment.item_id,
            experiment_id=experiment.item_id,
            metric="private-measurement-metric",
            value=Decimal("424242.125"),
            original_value=Decimal("424242.125"),
            unit="ms",
            original_unit="ms",
            source_pointer={"record_id": "measurement-facts"},
            evidence_type="published_table",
        )
        structured = (*structured, measurement)
        counts["measurement"] = 1
    file = metadata.files[0].model_copy(
        update={
            "member_ids": (*metadata.files[0].member_ids, *(item.item_id for item in structured)),
        }
    )
    metadata = metadata.model_copy(
        update={
            "files": (file,),
            "counts": counts,
        }
    )
    digest = content_digest(metadata.model_dump(mode="json"))
    key = f"@manifest:{digest}"
    mixed = manifest.model_copy(
        update={
            "item_id": item_id(manifest.origin_id, key, manifest.source_revision),
            "logical_key": key,
            "content_sha256": digest,
            "source_path": f"@manifests/{digest}",
            "source_details": metadata.model_dump(mode="json"),
            "manifest_item_ids": list(metadata.member_ids),
        }
    )
    members = tuple(
        RagItem.model_validate(record["_source"]["item"])
        for key, record in server.records.items()
        if key != manifest.item_id
    )
    return (*members, *structured, mixed), experiment, mixed


def test_mixed_shadow_preserves_manifest_hash_without_copying_experiment_facts(metadata, tmp_path: Path) -> None:
    _, server, service = metadata
    manifest = stage(service)
    snapshot, experiment, mixed = mixed_snapshot(server, manifest)
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://shadow.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "shadow.lock",
    )
    report = shadow_copy_documents(repository, snapshot)
    assert report.structured_item_ids == (experiment.item_id,)
    assert report.cutover_blocked_reason is None and not report.cutover
    assert report.item_count == len(snapshot) - 1
    copied = repository._read_record(f"shadow:{report.snapshot_sha256}:{mixed.item_id}")[0]
    assert copied == mixed
    assert experiment.item_id in copied.manifest_item_ids
    external = target.records[f"shadow:{report.snapshot_sha256}:external:{experiment.item_id}"]["_source"]["item"]
    assert external["contract"] == "public-rag.external-member.v1"
    assert "model" not in external and "value" not in external and "source_pointer" not in external
    assert not any(b"structured-fact-body-must-stay-sql" in request.content for request in target.requests)


def test_document_publication_rejects_mixed_release_before_writes(metadata) -> None:
    repository, server, service = metadata
    manifest = stage(service)
    snapshot, experiment, mixed = mixed_snapshot(server, manifest)
    before = copy.deepcopy(server.records)
    with pytest.raises(PublicationError, match="explicit document split"):
        repository.stage(mixed, tuple(item for item in snapshot if item.kind != "source"))
    with pytest.raises(PublicationError, match="keep them in SQL"):
        repository._create_immutable(experiment)
    assert server.records == before


def test_explicit_verified_promotion_preserves_citations_and_is_idempotent(metadata, tmp_path: Path) -> None:
    _, source, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    snapshot = tuple(RagItem.model_validate(record["_source"]["item"]) for record in source.records.values())
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "promote.lock",
    )
    baseline = shadow_copy_documents(repository, snapshot)
    report = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=baseline.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=service.archive,
    )
    assert report.installed_sources == (LLM_D_ORIGIN,) and not report.cutover
    assert report.source_generations == ((LLM_D_ORIGIN, 1),)
    assert repository.head(LLM_D_ORIGIN).manifest == manifest
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    match = repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))[0]
    assert (match.manifest_id, match.generation) == (manifest.item_id, 1)
    before = copy.deepcopy(target.records)
    repeated = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=baseline.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=service.archive,
    )
    assert repeated.inserted_items == repeated.inserted_references == 0
    assert target.management_refreshes == 3
    assert target.records == before
    assert not target.aliases
    assert not any(request.url.path == "/_aliases" for request in target.requests)


def test_mixed_promotion_retains_external_membership_and_newer_revocation(metadata, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from llm_d_lens.benchmark_rag.publication_contracts import PublicationEvent, RegistryState

    _, source, service = metadata
    snapshot, experiment, manifest = mixed_snapshot(source, stage(service))
    registry = next(item for item in snapshot if item.source_role == "registry")
    state = RegistryState.model_validate(registry.source_details)
    event = PublicationEvent(
        operation="activate",
        manifest_id=manifest.item_id,
        previous_manifest_id=None,
        generation=7,
        at=datetime.now(UTC),
        counts=ReleaseManifest.model_validate(manifest.source_details).counts,
    )
    registry = registry.model_copy(
        update={
            "published_manifest_id": manifest.item_id,
            "source_details": state.model_copy(update={"generation": 7, "history": (event,)}).model_dump(mode="json"),
        }
    )
    snapshot = tuple(registry if item.source_role == "registry" else item for item in snapshot)
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "mixed-promote.lock",
    )
    baseline = shadow_copy_documents(repository, snapshot)
    result = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=baseline.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=service.archive,
    )
    assert result.inserted_references == 1
    assert repository.get(experiment.item_id) is None
    assert repository.head(LLM_D_ORIGIN).manifest == manifest
    passage = repository.list_published(LLM_D_ORIGIN)[0]
    matches = repository.resolve_public_passages((LLM_D_ORIGIN,), (passage.item_id,))
    assert matches[0].generation == 7 and matches[0].manifest_id == manifest.item_id
    repository.assert_current(matches)
    repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=7)
    revoked = repository.head(LLM_D_ORIGIN)
    before = copy.deepcopy(target.records)
    with pytest.raises(PublicationConflictError):
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=baseline.snapshot_sha256,
            expected_heads={LLM_D_ORIGIN: None},
            legacy_writers_quiesced=True,
            fresh_snapshot=snapshot,
            archive=service.archive,
        )
    replay = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=baseline.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: revoked},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=service.archive,
    )
    assert replay.preserved_sources == (LLM_D_ORIGIN,)
    assert repository.head(LLM_D_ORIGIN) == revoked
    assert target.records == before
    assert repository.list_published(LLM_D_ORIGIN) == []
    assert not any(b"structured-fact-body-must-stay-sql" in request.content for request in target.requests)
    assert not target.aliases


def test_mixed_fact_baseline_promotes_builds_searches_and_preserves_revocation(
    metadata,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
    from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceSearchRepository
    from llm_d_lens.benchmark_rag.publication_contracts import PublicationEvent, RegistryState
    from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION
    from llm_d_lens.benchmark_rag.search import PublicRagSearchService
    from llm_d_lens.benchmark_rag.search_contracts import PublicSearchRequest
    from llm_d_lens.db.dao import rag_query

    _, source, publication = metadata
    snapshot, experiment, manifest = mixed_snapshot(source, stage(publication), include_measurement=True)
    measurement = next(item for item in snapshot if item.kind == "measurement")
    registry = next(item for item in snapshot if item.source_role == "registry")
    state = RegistryState.model_validate(registry.source_details)
    event = PublicationEvent(
        operation="activate",
        manifest_id=manifest.item_id,
        previous_manifest_id=None,
        generation=9,
        at=datetime.now(UTC),
        counts=ReleaseManifest.model_validate(manifest.source_details).counts,
    )
    registry = registry.model_copy(
        update={
            "published_manifest_id": manifest.item_id,
            "source_details": state.model_copy(update={"generation": 9, "history": (event,)}).model_dump(mode="json"),
        }
    )
    snapshot = tuple(registry if item.source_role == "registry" else item for item in snapshot)
    original_sql_baseline = tuple(item.model_dump(mode="json") for item in snapshot)
    target = MetadataServer()
    repository = DocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "mixed-e2e.lock",
    )
    shadow = shadow_copy_documents(repository, snapshot)
    promoted = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=shadow.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=publication.archive,
    )
    assert promoted.inserted_references == 2
    assert repository.head(LLM_D_ORIGIN).registry == registry
    assert repository.head(LLM_D_ORIGIN).manifest == manifest
    assert repository.get(experiment.item_id) is repository.get(measurement.item_id) is None
    idempotent = RagPublicationService(dao=repository, archive=publication.archive).activate(
        LLM_D_ORIGIN,
        manifest.item_id,
    )
    assert idempotent.generation == 9 and not idempotent.changed
    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", "opensearch")
    monkeypatch.setattr(rag_query, "RagQueryDao", Mock(side_effect=AssertionError("mixed document opened SQL")))
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(target)) as client:
            projection = OpenSearchEvidenceSearchRepository("http://target.test", client=client)
            lifecycle = DocumentLifecycle(repository)
            build = await PublicRagIndexBuilder(
                projection,
                dao=repository,
                archive=publication.archive,
                lifecycle=lifecycle,
            ).build((LLM_D_ORIGIN,))
            assert build.document_count == 1
            service = PublicRagSearchService(
                projection,
                dao=repository,
                archive=publication.archive,
                lifecycle=lifecycle,
            )
            request = PublicSearchRequest(question="Public evidence", origins=(LLM_D_ORIGIN,))
            result = await service.search(request, principal)
            assert len(result.results) == 1
            citation = result.results[0].citation
            assert citation.published_manifest_id == manifest.item_id and build.scopes[0].generation == 9
            assert result.results[0].evidence_id == next(item.item_id for item in snapshot if item.kind == "passage")
            payload = result.model_dump_json()
            assert "structured-fact-body-must-stay-sql" not in payload
            assert "private-measurement-metric" not in payload and "424242.125" not in payload
            expected = repository.head(LLM_D_ORIGIN)
            repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=9)
            with pytest.raises(PublicationConflictError):
                promote_documents(
                    repository,
                    snapshot,
                    verified_snapshot_sha256=shadow.snapshot_sha256,
                    expected_heads={LLM_D_ORIGIN: expected},
                    legacy_writers_quiesced=True,
                    fresh_snapshot=snapshot,
                    archive=publication.archive,
                )
            revoked = repository.head(LLM_D_ORIGIN)
            promote_documents(
                repository,
                snapshot,
                verified_snapshot_sha256=shadow.snapshot_sha256,
                expected_heads={LLM_D_ORIGIN: revoked},
                legacy_writers_quiesced=True,
                fresh_snapshot=snapshot,
                archive=publication.archive,
            )
            assert repository.head(LLM_D_ORIGIN) == revoked and not revoked.state.enabled
            withdrawn = await service.search(request, principal)
            assert withdrawn.results == () and withdrawn.index_stale

    asyncio.run(run())
    assert tuple(item.model_dump(mode="json") for item in snapshot) == original_sql_baseline
    for request in target.requests:
        assert b"structured-fact-body-must-stay-sql" not in request.content
        assert b"private-measurement-metric" not in request.content and b"424242.125" not in request.content


def test_new_mixed_sql_release_can_be_imported_then_explicitly_activated_without_identity_split(
    metadata,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder
    from llm_d_lens.benchmark_rag.opensearch_evidence_search import OpenSearchEvidenceSearchRepository
    from llm_d_lens.benchmark_rag.publication_contracts import PublicationEvent, RegistryState
    from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION
    from llm_d_lens.benchmark_rag.search import PublicRagSearchService
    from llm_d_lens.benchmark_rag.search_contracts import PublicSearchRequest

    _, source, sql_publication = metadata
    first_snapshot, _, first_manifest = mixed_snapshot(
        source,
        stage(sql_publication),
        include_measurement=True,
    )
    registry = next(item for item in first_snapshot if item.source_role == "registry")
    initial = RegistryState.model_validate(registry.source_details)
    first_event = PublicationEvent(
        operation="activate",
        manifest_id=first_manifest.item_id,
        previous_manifest_id=None,
        generation=1,
        at=datetime.now(UTC),
        counts=ReleaseManifest.model_validate(first_manifest.source_details).counts,
    )
    first_registry = registry.model_copy(
        update={
            "published_manifest_id": first_manifest.item_id,
            "source_details": initial.model_copy(update={"generation": 1, "history": (first_event,)}).model_dump(
                mode="json"
            ),
        }
    )
    first_snapshot = tuple(first_registry if item.source_role == "registry" else item for item in first_snapshot)
    target = MetadataServer()
    repository = DocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "mixed-update.lock",
    )
    first_shadow = shadow_copy_documents(repository, first_snapshot)
    promote_documents(
        repository,
        first_snapshot,
        verified_snapshot_sha256=first_shadow.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        archive=sql_publication.archive,
        legacy_writers_quiesced=True,
        fresh_snapshot=first_snapshot,
    )
    repository.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
    disabled = repository.head(LLM_D_ORIGIN)

    draft = stage(sql_publication, "b" * 40, b"New public evidence")
    second_rows, _, second_manifest = mixed_snapshot(source, draft, include_measurement=True)
    draft_manifest_id = second_manifest.item_id
    metadata_sql = ReleaseManifest.model_validate(second_manifest.source_details).model_copy(
        update={
            "base_generation": 1,
            "base_manifest_id": first_manifest.item_id,
            "removed_item_ids": tuple(
                sorted(
                    set(first_manifest.manifest_item_ids or ()) - set(second_manifest.manifest_item_ids or ()),
                )
            ),
        }
    )
    digest = content_digest(metadata_sql.model_dump(mode="json"))
    key = f"@manifest:{digest}"
    second_manifest = second_manifest.model_copy(
        update={
            "item_id": item_id(LLM_D_ORIGIN, key, second_manifest.source_revision),
            "logical_key": key,
            "content_sha256": digest,
            "source_path": f"@manifests/{digest}",
            "source_details": metadata_sql.model_dump(mode="json"),
        }
    )
    second_event = PublicationEvent(
        operation="activate",
        manifest_id=second_manifest.item_id,
        previous_manifest_id=first_manifest.item_id,
        generation=2,
        at=datetime.now(UTC),
        counts=metadata_sql.counts,
    )
    second_registry = registry.model_copy(
        update={
            "published_manifest_id": second_manifest.item_id,
            "source_details": initial.model_copy(
                update={
                    "generation": 2,
                    "history": (first_event, second_event),
                }
            ).model_dump(mode="json"),
        }
    )
    combined = {item.item_id: item for item in (*first_snapshot, *second_rows) if item.source_role != "registry"}
    combined.pop(draft_manifest_id, None)
    combined[second_manifest.item_id] = second_manifest
    combined[second_registry.item_id] = second_registry
    second_snapshot = tuple(combined[key] for key in sorted(combined))
    sql_before = tuple(item.model_dump(mode="json") for item in second_snapshot)
    second_shadow = shadow_copy_documents(repository, second_snapshot)
    promote_documents(
        repository,
        second_snapshot,
        verified_snapshot_sha256=second_shadow.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: disabled},
        archive=sql_publication.archive,
        legacy_writers_quiesced=True,
        fresh_snapshot=second_snapshot,
    )
    assert repository.head(LLM_D_ORIGIN) == disabled
    publication = RagPublicationService(dao=repository, archive=sql_publication.archive)
    with pytest.raises(PublicationError, match="disabled"):
        publication.activate(LLM_D_ORIGIN, second_manifest.item_id, expected_generation=2)
    repository.set_enabled(LLM_D_ORIGIN, True, expected_generation=2)
    with pytest.raises(PublicationConflictError):
        publication.activate(LLM_D_ORIGIN, second_manifest.item_id)
    with pytest.raises(PublicationConflictError):
        publication.activate(LLM_D_ORIGIN, second_manifest.item_id, expected_generation=2)
    approved = publication.activate(LLM_D_ORIGIN, second_manifest.item_id, expected_generation=3)
    assert approved.generation == 4
    assert repository.head(LLM_D_ORIGIN).manifest == second_manifest
    old_passage = next(item for item in first_snapshot if item.kind == "passage")
    assert repository.resolve_public_passages((LLM_D_ORIGIN,), (old_passage.item_id,)) == ()
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )

    async def rebuild_and_search() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(target)) as client:
            projection = OpenSearchEvidenceSearchRepository("http://target.test", client=client)
            build = await PublicRagIndexBuilder(
                projection,
                dao=repository,
                archive=sql_publication.archive,
            ).build((LLM_D_ORIGIN,))
            assert build.scopes[0].manifest_id == second_manifest.item_id
            assert build.scopes[0].generation == 4
            result = await PublicRagSearchService(
                projection,
                dao=repository,
                archive=sql_publication.archive,
            ).search(PublicSearchRequest(question="New public evidence", origins=(LLM_D_ORIGIN,)), principal)
            assert len(result.results) == 1
            assert result.results[0].citation.published_manifest_id == second_manifest.item_id
            assert result.results[0].text == "New public evidence"

    asyncio.run(rebuild_and_search())
    assert tuple(item.model_dump(mode="json") for item in second_snapshot) == sql_before
    assert not any(b"424242.125" in request.content for request in target.requests)


def test_explicit_imported_document_activation_never_falls_back_to_sql() -> None:
    from unittest.mock import Mock

    from llm_d_lens.db.dao.rag_publication import RagPublicationDao

    sql = Mock(spec=RagPublicationDao)
    service = RagPublicationService(dao=sql)
    with pytest.raises(PublicationError, match="requires a document repository"):
        service.activate(LLM_D_ORIGIN, "manifest", expected_generation=1)
    sql.activate.assert_not_called()


def test_promotion_rejects_unverified_baseline_or_missing_source_bytes_before_writes(
    metadata,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, service = metadata
    stage(service)
    snapshot = tuple(RagItem.model_validate(record["_source"]["item"]) for record in source.records.values())
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "invalid-promote.lock",
    )
    with pytest.raises(PublicationError, match="complete verified shadow"):
        digest = content_digest(
            tuple(item.model_dump(mode="json") for item in sorted(snapshot, key=lambda x: x.item_id))
        )
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=digest,
            expected_heads={LLM_D_ORIGIN: None},
            legacy_writers_quiesced=True,
            fresh_snapshot=snapshot,
            archive=service.archive,
        )
    assert not target.records
    baseline = shadow_copy_documents(repository, snapshot)
    before = copy.deepcopy(target.records)
    monkeypatch.setattr(service.archive, "read", lambda _: b"incorrect bytes")
    with pytest.raises(PublicationError, match="source archive hash"):
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=baseline.snapshot_sha256,
            expected_heads={LLM_D_ORIGIN: None},
            legacy_writers_quiesced=True,
            fresh_snapshot=snapshot,
            archive=service.archive,
        )
    assert target.records == before


def test_promotion_refuses_without_legacy_writer_freeze_and_fresh_export(metadata, tmp_path: Path) -> None:
    _, source, service = metadata
    stage(service)
    snapshot = tuple(RagItem.model_validate(record["_source"]["item"]) for record in source.records.values())
    target = MetadataServer()
    repository = DocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "freeze-required.lock",
    )
    shadow = shadow_copy_documents(repository, snapshot)
    before = copy.deepcopy(target.records)
    with pytest.raises(PublicationError, match="explicit confirmation"):
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=shadow.snapshot_sha256,
            expected_heads={LLM_D_ORIGIN: None},
            archive=service.archive,
            fresh_snapshot=snapshot,
        )
    with pytest.raises(PublicationError, match="fresh authoritative export"):
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=shadow.snapshot_sha256,
            expected_heads={LLM_D_ORIGIN: None},
            archive=service.archive,
            legacy_writers_quiesced=True,
        )
    assert target.records == before
    assert repository.get(registry_id(LLM_D_ORIGIN)) is None


@pytest.mark.parametrize("legacy_change", ["revocation", "new_write"])
def test_fresh_legacy_export_detects_stale_shadow_even_when_target_source_is_absent(
    legacy_change: str,
    metadata,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from llm_d_lens.benchmark_rag.publication_contracts import PublicationEvent, RegistryState

    _, source, service = metadata
    manifest = stage(service)
    service.activate(LLM_D_ORIGIN, manifest.item_id)
    snapshot = tuple(RagItem.model_validate(record["_source"]["item"]) for record in source.records.values())
    fresh = snapshot
    if legacy_change == "revocation":
        registry = next(item for item in snapshot if item.source_role == "registry")
        state = RegistryState.model_validate(registry.source_details)
        event = PublicationEvent(
            operation="disable",
            manifest_id=manifest.item_id,
            previous_manifest_id=manifest.item_id,
            generation=state.generation + 1,
            at=datetime.now(UTC),
            counts={},
        )
        registry = registry.model_copy(
            update={
                "source_details": state.model_copy(
                    update={
                        "enabled": False,
                        "generation": event.generation,
                        "history": (*state.history, event),
                    }
                ).model_dump(mode="json"),
            }
        )
        fresh = tuple(registry if item.source_role == "registry" else item for item in snapshot)
    else:
        passage = next(item for item in snapshot if item.kind == "passage")
        key = passage.logical_key + ":new-write"
        fresh = (
            *snapshot,
            passage.model_copy(
                update={
                    "item_id": item_id(passage.origin_id, key, passage.source_revision),
                    "logical_key": key,
                }
            ),
        )
    target = MetadataServer()
    repository = DocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "stale-legacy.lock",
    )
    shadow = shadow_copy_documents(repository, snapshot)
    before = copy.deepcopy(target.records)
    with pytest.raises(PublicationConflictError, match="legacy source export changed"):
        promote_documents(
            repository,
            snapshot,
            verified_snapshot_sha256=shadow.snapshot_sha256,
            expected_heads={LLM_D_ORIGIN: None},
            archive=service.archive,
            legacy_writers_quiesced=True,
            fresh_snapshot=fresh,
        )
    assert target.records == before
    assert repository.get(registry_id(LLM_D_ORIGIN)) is None


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_document_admin_transfer_never_opens_sql(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin

    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    monkeypatch.setattr(admin, "transfer_database", Mock(side_effect=AssertionError("document command opened SQL")))
    repository = Mock(spec=DocumentRepository)
    monkeypatch.setattr(admin.DocumentRepository, "from_environment", Mock(return_value=repository))
    service = Mock()
    service.backup.return_value.backup_id = "fixture"
    service.backup.return_value.model_dump.return_value = {"contract": "benchmark-rag.documents.backup.v1"}
    service.restore.return_value.model_dump_json.return_value = "{}"
    factory = Mock(return_value=service)
    monkeypatch.setattr(admin, "DocumentBackupService", factory)
    args = [command, "--data-dir", str(tmp_path)]
    if command == "backup":
        args += ["--output", str(tmp_path / "backup.zip")]
    else:
        args += ["--bundle", str(tmp_path / "backup.zip"), "--initialize"]
    assert admin.main(args) == 0
    repository.prepare.assert_called_once_with(initialize=command == "restore")
    repository.close.assert_called_once()
    getattr(service, command).assert_called_once()
    factory.assert_called_once()


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_admin_structured_transfer_requires_explicit_flag_and_never_opens_documents(
    command, tmp_path, monkeypatch
) -> None:
    from contextlib import nullcontext
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin

    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", "postgresql://structured-fixture")
    monkeypatch.setattr(
        admin.DocumentRepository,
        "from_environment",
        Mock(side_effect=AssertionError("structured transfer opened document authority")),
    )
    dao = Mock()
    sql = Mock(return_value=nullcontext(dao))
    monkeypatch.setattr(admin, "transfer_database", sql)
    service = Mock()
    service.backup.return_value.backup_id = "fixture"
    service.backup.return_value.model_dump.return_value = {"contract": "benchmark-rag.backup.v1"}
    service.restore.return_value.model_dump_json.return_value = "{}"
    factory = Mock(return_value=service)
    monkeypatch.setattr(admin, "RagBackupService", factory)
    args = [command, "--structured", "--data-dir", str(tmp_path)]
    args += ["--output" if command == "backup" else "--bundle", str(tmp_path / "structured.zip")]
    if command == "restore":
        args += ["--initialize"]
    assert admin.main(args) == 0
    sql.assert_called_once_with("postgresql://structured-fixture", schema="public")
    dao.prepare.assert_called_once_with(initialize=command == "restore")
    factory.assert_called_once_with(dao)
    getattr(service, command).assert_called_once()


@pytest.mark.parametrize("structured", [False, True])
def test_admin_verify_is_offline_and_uses_only_selected_bundle_contract(
    structured: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin

    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    monkeypatch.setattr(admin, "transfer_database", Mock(side_effect=AssertionError("verify opened SQL")))
    monkeypatch.setattr(
        admin.DocumentRepository,
        "from_environment",
        Mock(side_effect=AssertionError("verify opened OpenSearch")),
    )
    selected = Mock()
    selected.verify.return_value.backup_id = "fixture"
    selected.verify.return_value.model_dump.return_value = {"contract": "fixture"}
    rejected = Mock(side_effect=AssertionError("wrong backup contract"))
    monkeypatch.setattr(admin, "DocumentBackupService", rejected if structured else selected)
    monkeypatch.setattr(admin, "RagBackupService", selected if structured else rejected)
    assert admin.main(["verify", "--bundle", "fixture.zip", *(["--structured"] if structured else [])]) == 0
    selected.verify.assert_called_once()


@pytest.mark.parametrize("command", ["refresh", "status", "rollback"])
def test_document_admin_source_lifecycle_never_opens_sql(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin

    monkeypatch.delenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", raising=False)
    monkeypatch.delenv("LLM_D_BENCH_DATABASE_URL", raising=False)
    monkeypatch.setattr(admin, "transfer_database", Mock(side_effect=AssertionError("document lifecycle opened SQL")))
    repository = Mock(spec=DocumentRepository)
    repository.head.return_value.model_dump_json.return_value = "{}"
    monkeypatch.setattr(admin.DocumentRepository, "from_environment", Mock(return_value=repository))
    publication = Mock()
    publication.rollback.return_value.model_dump_json.return_value = "{}"
    publication_factory = Mock(return_value=publication)
    monkeypatch.setattr(admin, "RagPublicationService", publication_factory)
    refresh = Mock()
    refresh.run.return_value.model_dump_json.return_value = "{}"
    refresh_factory = Mock(return_value=refresh)
    monkeypatch.setattr(admin, "RagRefreshService", refresh_factory)
    plan = Mock()
    monkeypatch.setattr(admin, "RefreshPlan", Mock(model_validate_json=Mock(return_value=plan)))
    args = [command, "--data-dir", str(tmp_path)]
    if command == "refresh":
        plan_path = tmp_path / "plan.json"
        plan_path.write_text("{}")
        args += ["--plan", str(plan_path)]
    else:
        args += ["--origin", LLM_D_ORIGIN]
        if command == "rollback":
            args += ["--manifest-id", "manifest", "--expected-generation", "7"]
    assert admin.main(args) == 0
    repository.prepare.assert_called_once_with(initialize=False)
    repository.close.assert_called_once()
    if command == "status":
        repository.head.assert_called_once_with(LLM_D_ORIGIN)
        publication_factory.assert_not_called()
    else:
        publication_factory.assert_called_once_with(dao=repository)
    if command == "refresh":
        refresh_factory.assert_called_once_with(publication)
        refresh.run.assert_called_once_with(plan, None, trigger="manual", event_revision=None)
    elif command == "rollback":
        publication.rollback.assert_called_once_with(LLM_D_ORIGIN, "manifest", expected_generation=7)


def test_document_mode_does_not_redirect_prism_rebuild_to_opensearch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin

    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", "opensearch")
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", "postgresql://fixture")
    monkeypatch.setattr(
        admin.DocumentRepository,
        "from_environment",
        Mock(side_effect=AssertionError("Prism opened OpenSearch")),
    )
    dao = Mock()
    sql = Mock(return_value=nullcontext(dao))
    monkeypatch.setattr(admin, "transfer_database", sql)
    importer = Mock()
    importer.import_snapshot.return_value.model_dump_json.return_value = "{}"
    monkeypatch.setattr(admin, "PrismSnapshotImporter", Mock(return_value=importer))
    monkeypatch.setattr(admin, "SourceApproval", Mock(model_validate_json=Mock(return_value=Mock())))
    approval_path = tmp_path / "approval.json"
    approval_path.write_text("{}")
    assert (
        admin.main(
            [
                "rebuild",
                "--data-dir",
                str(tmp_path),
                "--approval",
                str(approval_path),
                "--snapshot",
                str(tmp_path / "prism"),
            ]
        )
        == 0
    )
    sql.assert_called_once_with("postgresql://fixture", schema="public")
    importer.import_snapshot.assert_called_once()


@pytest.mark.parametrize("command", ["refresh", "status", "rollback"])
def test_prism_source_admin_remains_explicitly_sql_owned(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from unittest.mock import Mock

    from llm_d_lens.benchmark_rag import admin
    from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN

    monkeypatch.setenv("LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY", "opensearch")
    monkeypatch.setenv("LLM_D_BENCH_DATABASE_URL", "postgresql://prism-fixture")
    monkeypatch.setattr(
        admin.DocumentRepository,
        "from_environment",
        Mock(side_effect=AssertionError("Prism opened document authority")),
    )
    dao = Mock()
    dao.head.return_value.model_dump_json.return_value = "{}"
    sql = Mock(return_value=nullcontext(dao))
    monkeypatch.setattr(admin, "transfer_database", sql)
    publication = Mock()
    publication.rollback.return_value.model_dump_json.return_value = "{}"
    publication_factory = Mock(return_value=publication)
    monkeypatch.setattr(admin, "RagPublicationService", publication_factory)
    refresh = Mock()
    refresh.run.return_value.model_dump_json.return_value = "{}"
    monkeypatch.setattr(admin, "RagRefreshService", Mock(return_value=refresh))
    plan = Mock()
    plan.inventory.origin_id = PRISM_ORIGIN
    monkeypatch.setattr(admin, "RefreshPlan", Mock(model_validate_json=Mock(return_value=plan)))
    args = [command, "--data-dir", str(tmp_path)]
    if command == "refresh":
        path = tmp_path / "plan.json"
        path.write_text("{}")
        args += ["--plan", str(path)]
    else:
        args += ["--origin", PRISM_ORIGIN]
        if command == "rollback":
            args += ["--manifest-id", "prism-manifest", "--expected-generation", "7"]
    assert admin.main(args) == 0
    sql.assert_called_once_with("postgresql://prism-fixture", schema="public")
    if command != "status":
        publication_factory.assert_called_once_with(dao=dao)


def test_migration_validates_full_sql_baseline_but_keeps_prism_entirely_sql_owned(metadata, tmp_path: Path) -> None:
    from llm_d_lens.benchmark_rag.publication_contracts import RegistryState
    from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN

    _, source, service = metadata
    stage(service)
    snapshot = tuple(RagItem.model_validate(record["_source"]["item"]) for record in source.records.values())
    approval = SourceApproval(
        verified_public=True,
        approved_by="reviewer",
        approval_reference="sql-only",
        license_reference="fixture",
        parser_version="test-v1",
        allowed_paths=allowed_paths(PRISM_ORIGIN),
    )
    registry = RagItem(
        item_id=registry_id(PRISM_ORIGIN),
        kind="source",
        source_role="registry",
        origin_id=PRISM_ORIGIN,
        logical_key="@registry",
        source_revision="registry-v1",
        content_sha256=content_digest(approval.model_dump(mode="json")),
        access_classification="verified_public",
        source_path="@registry",
        format="json",
        source_details=RegistryState(approval=approval).model_dump(mode="json"),
    )
    file = RagItem(
        item_id=item_id(PRISM_ORIGIN, "file:historical-merged-deduplicated.json", "2026-01-01"),
        kind="source",
        source_role="file",
        origin_id=PRISM_ORIGIN,
        logical_key="file:historical-merged-deduplicated.json",
        source_revision="2026-01-01",
        content_sha256="f" * 64,
        access_classification="verified_public",
        source_path="historical-merged-deduplicated.json",
        format="json",
        parent_id=registry.item_id,
        source_details={"parser_version": "test-v1"},
    )
    experiment = RagItem(
        item_id=item_id(PRISM_ORIGIN, "experiment:prism", "2026-01-01"),
        kind="experiment",
        origin_id=PRISM_ORIGIN,
        logical_key="experiment:prism",
        source_revision="2026-01-01",
        content_sha256=file.content_sha256,
        access_classification="verified_public",
        source_path=file.source_path,
        format="json",
        parent_id=file.item_id,
        model="prism-fact-body-never-copied",
        source_pointer={"record_id": "1"},
    )
    snapshot = (*snapshot, registry, file, experiment)
    target = MetadataServer()
    repository = OpenSearchDocumentRepository(
        "http://target.test",
        client=httpx.Client(transport=httpx.MockTransport(target)),
        lock_path=tmp_path / "sql-only-promote.lock",
    )
    shadow = shadow_copy_documents(repository, snapshot)
    assert shadow.sql_only_origins == (PRISM_ORIGIN,)
    assert shadow.origins == (LLM_D_ORIGIN,)
    report = promote_documents(
        repository,
        snapshot,
        verified_snapshot_sha256=shadow.snapshot_sha256,
        expected_heads={LLM_D_ORIGIN: None},
        legacy_writers_quiesced=True,
        fresh_snapshot=snapshot,
        archive=service.archive,
    )
    assert report.sql_only_origins == (PRISM_ORIGIN,)
    assert not any(b"prism:public-snapshot" in request.content for request in target.requests)
    assert not any(b"prism-fact-body-never-copied" in request.content for request in target.requests)
    assert repository.get(registry.item_id) is None
    with pytest.raises(PublicationError, match="SQL-owned"):
        repository.register(PRISM_ORIGIN, approval)
