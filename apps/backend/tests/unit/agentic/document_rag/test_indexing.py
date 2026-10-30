"""A failed or stale build must never replace the active public index."""

import asyncio
import hashlib
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from llm_d_lens.agentic.document_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.agentic.document_rag.document_repository import DocumentRepository
from llm_d_lens.agentic.document_rag.embedding import CONTRACT, located_chunk
from llm_d_lens.agentic.document_rag.indexing import PublicRagIndexBuilder
from llm_d_lens.agentic.document_rag.publication_contracts import InventoryFile, PinnedInventory
from llm_d_lens.agentic.document_rag.repository_corpus import parse_markdown
from llm_d_lens.agentic.document_rag.search_contracts import EvidenceSearchError, PublicSearchScope
from llm_d_lens.agentic.document_rag.service import EvidenceUnavailableError, _verify_passage_text
from llm_d_lens.agentic.document_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN
from llm_d_lens.core.exceptions import ConflictError


def builder_case():
    data = b"# TTFT\nTime to first token.\n"
    file = InventoryFile(path="docs/metrics.md", sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    scope = PublicSearchScope(origin_id=BENCHMARK_ORIGIN, manifest_id="manifest", generation=1)
    dao, archive = Mock(spec=DocumentRepository), Mock()
    dao.public_search_scopes.return_value = (scope,)
    dao.list_published.return_value = parse_markdown(file, inventory, data)
    archive.read.return_value = data
    lifecycle = Mock(spec=DocumentLifecycle, activate=AsyncMock(), recover=AsyncMock())
    builder = PublicRagIndexBuilder(Mock(), dao=dao, archive=archive, lifecycle=lifecycle)
    builder.writer = AsyncMock()
    builder.writer.create.side_effect = lambda build: "public-rag-v1-" + build.build_id
    builder.writer.index_build.side_effect = lambda _: builder.lifecycle.create.call_args.args[0]
    return builder, scope


def test_complete_build_preserves_whole_passage_and_activates_after_verification():
    builder, _ = builder_case()
    result = asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    assert result.document_count == 1
    builder.lifecycle.recover.assert_awaited_once_with(builder.writer, vector=False)
    builder.writer.verify.assert_awaited_once_with("public-rag-v1-" + result.build_id, 1)
    builder.lifecycle.activate.assert_awaited_once_with(result.build_id, builder.writer)
    assert builder.writer.write.call_args.args[1][0].text == "# TTFT\nTime to first token.\n"


@pytest.mark.parametrize("vector", [False, True])
def test_model_alias_ingestion_keeps_one_projection_and_original_source(vector):
    builder, _ = builder_case()
    data = b"# Acme/Atlas-7B-Instruct-FP8\nOriginal model evidence.\n"
    file = InventoryFile(path="docs/model.md", sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    item = parse_markdown(file, inventory, data)[0]
    builder.dao.list_published.return_value = (item,)
    builder.archive.read.return_value = data
    encoder = None
    if vector:
        encoder = Mock(contract=CONTRACT)
        encoder.chunks.side_effect = lambda parent: (located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT),)
        encoder.encode.side_effect = lambda texts: tuple((1.0, *([0.0] * 383)) for _ in texts)
    result = asyncio.run(builder.build((BENCHMARK_ORIGIN,), encoder=encoder))
    assert result.document_count == 1
    documents = builder.writer.write.call_args.args[1]
    assert len(documents) == 1
    document = documents[0]
    assert document.evidence_id == item.item_id
    assert document.text == data.decode() and document.title == "Acme/Atlas-7B-Instruct-FP8"
    assert document.heading_path == tuple(item.heading_path)
    assert document.source_path == item.source_path
    assert {"acme/atlas-7b-instruct-fp8", "atlas-7b-instruct-fp8", "acme-atlas-7b-instruct-fp8"} <= set(
        document.identifiers
    )
    if vector:
        encoder.encode.assert_called_once_with(("passage: " + data.decode(),))


def test_stale_build_does_not_activate():
    builder, scope = builder_case()
    builder.dao.public_search_scopes.side_effect = [(scope,), ()]
    with pytest.raises(ConflictError):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    builder.lifecycle.activate.assert_not_called()


def test_failed_bulk_does_not_activate():
    builder, _ = builder_case()
    builder.writer.write.side_effect = EvidenceSearchError("bulk failure")
    with pytest.raises(EvidenceSearchError):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    builder.lifecycle.activate.assert_not_called()
    builder.lifecycle.fail.assert_called_once()


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("failure_at", [None, "filters", "recover", "archive", "write", "activate", "cancel"])
def test_builder_closes_only_owned_metadata_on_every_exit(monkeypatch, owned, failure_at):
    original, _ = builder_case()
    dao = original.dao
    monkeypatch.setattr(DocumentRepository, "from_environment", Mock(return_value=dao))
    builder = PublicRagIndexBuilder(
        Mock(),
        dao=None if owned else dao,
        archive=original.archive,
        lifecycle=original.lifecycle,
    )
    builder.writer = original.writer
    if failure_at in ("recover", "activate"):
        getattr(builder.lifecycle, failure_at).side_effect = EvidenceSearchError("injected failure")
    elif failure_at == "archive":
        builder.archive.read.side_effect = OSError("injected failure")
    elif failure_at == "write":
        builder.writer.write.side_effect = EvidenceSearchError("injected failure")
    elif failure_at == "cancel":
        builder.writer.write.side_effect = asyncio.CancelledError()

    async def run():
        error = asyncio.CancelledError if failure_at == "cancel" else (ValueError, EvidenceSearchError, OSError)
        with pytest.raises(error) if failure_at else nullcontext(), builder:
            await builder.build(("not-approved",) if failure_at == "filters" else (BENCHMARK_ORIGIN,))

    asyncio.run(run())
    builder.close()
    assert dao.close.call_count == int(owned)


def test_empty_corpus_cannot_replace_active_index():
    builder, _ = builder_case()
    builder.dao.list_published.return_value = []
    with pytest.raises(EvidenceSearchError, match="nonempty"):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    builder.writer.create.assert_not_called()


def test_unresolved_lifecycle_mismatch_prevents_new_build_writes():
    builder, _ = builder_case()
    builder.lifecycle.recover.side_effect = EvidenceSearchError("alias mismatch")
    with pytest.raises(EvidenceSearchError, match="alias"):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    builder.dao.public_search_scopes.assert_not_called()
    builder.writer.create.assert_not_called()
    builder.lifecycle.create.assert_not_called()


def test_physical_mapping_metadata_must_be_verified_before_ready_or_activation():
    builder, _ = builder_case()
    builder.writer.index_build.side_effect = EvidenceSearchError("invalid physical mapping")
    with pytest.raises(EvidenceSearchError, match="mapping"):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,)))
    builder.lifecycle.ready.assert_not_called()
    builder.lifecycle.activate.assert_not_called()
    builder.lifecycle.fail.assert_called_once()


@pytest.mark.parametrize("tamper", [None, "text", "pointer"])
def test_published_table_context_is_reproduced_from_original_bytes(tamper):
    data = b"# Notes\n\n| Metric | Definition |\n| --- | --- |\n| Cache | Shared |\n\n"
    data += b"| data | campaign |\n| --- | --- |\n| First | Reference |\n"
    file = InventoryFile(
        path="guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/README.md",
        sha256=hashlib.sha256(data).hexdigest(),
        format="markdown",
    )
    inventory = PinnedInventory(origin_id=LLM_D_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    item = parse_markdown(file, inventory, data)[0]
    assert item.kind == "passage" and "start_char" in item.source_pointer
    if tamper == "text":
        item = item.model_copy(update={"passage_text": "invented"})
    elif tamper == "pointer":
        item = item.model_copy(update={"source_pointer": {**item.source_pointer, "end_char": 999}})
    if tamper:
        with pytest.raises(EvidenceUnavailableError, match="disagrees"):
            _verify_passage_text(item, data)
    else:
        _verify_passage_text(item, data)


def test_publication_parser_failure_does_not_become_an_unchecked_context():
    builder, _ = builder_case()
    item = builder.dao.list_published.return_value[0].model_copy(
        update={"origin_id": LLM_D_ORIGIN, "source_pointer": {"line": 1}}
    )
    with pytest.raises(EvidenceUnavailableError):
        _verify_passage_text(item, b"changed")


def test_vector_build_uses_independent_alias_and_preserves_parent_evidence():
    builder, _ = builder_case()
    encoder = Mock()
    encoder.contract = CONTRACT
    encoder.chunks.side_effect = lambda parent: (located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT),)
    encoder.encode.side_effect = lambda texts: tuple((1.0, *([0.0] * 383)) for _ in texts)
    build = asyncio.run(builder.build((BENCHMARK_ORIGIN,), encoder=encoder))
    assert build.retrieval_mode == "opensearch_vector" and build.embedding_contract == CONTRACT
    builder.lifecycle.recover.assert_awaited_once_with(builder.writer, vector=True)
    builder.lifecycle.activate.assert_awaited_once_with(build.build_id, builder.writer)
    document = builder.writer.write.call_args.args[1][0]
    assert document.evidence_id != document.chunk_id
    assert len(document.embedding) == 384 and document.chunk_start == 0


def test_failed_encoding_does_not_activate_any_alias():
    builder, _ = builder_case()
    encoder = Mock()
    encoder.contract = CONTRACT
    encoder.chunks.side_effect = lambda parent: (located_chunk(parent, 0, len(parent.text), 0, "", CONTRACT),)
    encoder.encode.side_effect = EvidenceSearchError("model unavailable")
    with pytest.raises(EvidenceSearchError, match="model"):
        asyncio.run(builder.build((BENCHMARK_ORIGIN,), encoder=encoder))
    builder.lifecycle.activate.assert_not_called()
