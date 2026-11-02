"""Alias publication fencing does not need PostgreSQL build pointers."""

import asyncio
from copy import deepcopy
from threading import RLock
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.agentic.document_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.agentic.document_rag.search_contracts import EvidenceSearchError, PublicSearchBuild, PublicSearchScope
from llm_d_lens.core.exceptions import ConflictError


class MemoryMetadata:
    def __init__(self):
        self.records = {}
        self.lock = RLock()
        self.scope = PublicSearchScope(origin_id="https://llm-d.ai", manifest_id="manifest", generation=1)

    def publication_lock(self):
        return self.lock

    def read_record(self, key):
        return deepcopy(self.records.get(key))

    def create_record(self, key, value):
        assert key not in self.records
        self.records[key] = (deepcopy(value), 0, 1)

    def replace_record(self, key, value, sequence, term):
        assert self.records[key][1:] == (sequence, term)
        self.records[key] = (deepcopy(value), sequence + 1, term)

    def public_search_scopes(self, origins):
        return (self.scope,) if self.scope is not None else ()


def case():
    metadata = MemoryMetadata()
    lifecycle = DocumentLifecycle(metadata)
    writer = AsyncMock()
    writer.alias_target.return_value = None
    builds = {}

    def create(name):
        build = PublicSearchBuild(build_id=name, scopes=(metadata.scope,), document_count=1)
        index = "public-rag-v1-" + name
        builds[index] = build
        lifecycle.create(build, index)
        lifecycle.ready(name, 1)
        return build

    async def switch(index, vector=False, **kwargs):
        writer.alias_target.return_value = index

    writer.switch_alias.side_effect = switch
    writer.index_build.side_effect = lambda index: builds[index]
    return metadata, lifecycle, writer, create


def test_complete_activation_is_alias_authoritative_without_database_pointer():
    metadata, lifecycle, writer, create = case()
    build = create("first")
    asyncio.run(lifecycle.activate(build.build_id, writer))
    assert writer.alias_target.return_value == "public-rag-v1-first"
    assert lifecycle.get("first").status == "active"
    assert not any("pointer" in key for key in metadata.records)
    asyncio.run(lifecycle.recover(writer))


def test_old_worker_cannot_overwrite_newer_build():
    _, lifecycle, writer, create = case()
    create("old")
    create("new")
    asyncio.run(lifecycle.activate("new", writer))
    with pytest.raises(ConflictError, match="fenced"):
        asyncio.run(lifecycle.activate("old", writer))
    assert writer.alias_target.return_value == "public-rag-v1-new"


def test_concurrent_activation_only_accepts_latest_worker():
    _, lifecycle, writer, create = case()
    create("old")
    create("new")

    async def run():
        return await asyncio.gather(
            lifecycle.activate("old", writer), lifecycle.activate("new", writer), return_exceptions=True
        )

    old, new = asyncio.run(run())
    assert isinstance(old, ConflictError) and new is None
    assert writer.alias_target.return_value == "public-rag-v1-new"


def test_revoked_source_blocks_activation():
    metadata, lifecycle, writer, create = case()
    create("first")
    metadata.scope = None
    with pytest.raises(ConflictError, match="sources changed"):
        asyncio.run(lifecycle.activate("first", writer))
    writer.switch_alias.assert_not_awaited()


def test_uncertain_alias_request_blocks_following_workers_until_explicit_recovery():
    _, lifecycle, writer, create = case()
    create("first")
    writer.switch_alias.side_effect = EvidenceSearchError("timeout")
    with pytest.raises(EvidenceSearchError, match="timeout"):
        asyncio.run(lifecycle.activate("first", writer))
    create("second")
    with pytest.raises(EvidenceSearchError, match="quiesced"):
        asyncio.run(lifecycle.activate("second", writer))
    with pytest.raises(EvidenceSearchError, match="drained"):
        asyncio.run(lifecycle.reconcile(writer))


def test_crash_after_alias_switch_is_recovered_without_republishing():
    metadata, lifecycle, writer, create = case()
    create("first")

    async def switched_then_timeout(index, vector=False, **kwargs):
        writer.alias_target.return_value = index
        raise EvidenceSearchError("lost response")

    writer.switch_alias.side_effect = switched_then_timeout
    with pytest.raises(EvidenceSearchError, match="lost response"):
        asyncio.run(lifecycle.activate("first", writer))
    asyncio.run(lifecycle.reconcile(writer, requests_quiesced=True))
    assert lifecycle.get("first").status == "active"
    assert metadata.read_record("activation:lexical")[0]["status"] == "settled"
    writer.switch_alias.assert_awaited_once()


def test_recovery_never_revives_revoked_content():
    metadata, lifecycle, writer, create = case()
    create("first")

    async def uncertain(index, vector=False, **kwargs):
        writer.alias_target.return_value = index
        raise EvidenceSearchError("lost response")

    writer.switch_alias.side_effect = uncertain
    with pytest.raises(EvidenceSearchError):
        asyncio.run(lifecycle.activate("first", writer))
    metadata.scope = None

    async def clear(index, vector=False, **kwargs):
        writer.alias_target.return_value = index

    writer.switch_alias.side_effect = clear
    asyncio.run(lifecycle.reconcile(writer, requests_quiesced=True))
    assert writer.alias_target.return_value is None
    assert lifecycle.get("first").status == "failed"


def test_alias_drift_does_not_get_silently_repaired():
    metadata, lifecycle, writer, create = case()
    create("first")
    writer.alias_target.return_value = "public-rag-v1-untracked"
    writer.index_build.side_effect = None
    writer.index_build.return_value = PublicSearchBuild(
        build_id="untracked", scopes=(metadata.scope,), document_count=1
    )
    with pytest.raises(EvidenceSearchError, match="unverified"):
        asyncio.run(lifecycle.recover(writer))
    writer.switch_alias.assert_not_awaited()


def test_rebuild_request_is_durable_metadata_not_an_activity_pointer():
    metadata, lifecycle, _, _ = case()
    assert not lifecycle.rebuild_requested()
    lifecycle.request_rebuild("public-rag-active")
    lifecycle.request_rebuild("public-rag-active")
    assert lifecycle.rebuild_requested()
    assert metadata.read_record("rebuild:public-rag-active")[0] == {"requested": True}
