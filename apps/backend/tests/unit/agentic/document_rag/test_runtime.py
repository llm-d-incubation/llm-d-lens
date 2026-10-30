"""Durable rollout, emergency gates and sanitized diagnostics without external I/O."""

import asyncio
import json
import multiprocessing
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from llm_d_lens.agentic.document_rag import runtime
from llm_d_lens.agentic.document_rag.document_repository import DocumentRepository
from llm_d_lens.agentic.document_rag.publication_contracts import PublicationError
from llm_d_lens.agentic.document_rag.query_contracts import QUERY_PERMISSION, QueryFilters
from llm_d_lens.agentic.document_rag.runtime import (
    RuntimePolicy,
    RuntimeUpdate,
    SearchTelemetry,
    load_runtime_policy,
    runtime_status,
    update_runtime_policy,
)
from llm_d_lens.agentic.document_rag.search import PublicRagSearchService
from llm_d_lens.agentic.document_rag.search_contracts import (
    EvidenceSearchError,
    PublicSearchBuild,
    PublicSearchRequest,
    PublicSearchResponse,
    PublicSearchScope,
)
from llm_d_lens.auth.contracts import Principal, RoleBinding
from llm_d_lens.core.exceptions import ConflictError, UnauthenticatedError


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))


def test_missing_policy_preserves_existing_search_and_disables_agentic():
    policy = load_runtime_policy()
    assert policy == RuntimePolicy()
    assert policy.search_enabled and policy.advanced_search_enabled
    assert policy.agentic_mode == "disabled" and policy.agentic_search_mode == "lexical"
    assert not runtime._policy_path().exists()


def test_policy_is_durable_private_atomic_and_cas_guarded():
    result = update_runtime_policy(
        RuntimeUpdate(expected_generation=0, agentic_mode="shadow", agentic_user_ids=("user-b", "user-a"))
    )
    assert result.generation == 1 and result.agentic_user_ids == ("user-a", "user-b")
    assert load_runtime_policy() == result
    assert stat.S_IMODE(runtime._policy_path().stat().st_mode) == 0o600
    assert not list(runtime._policy_path().parent.glob(".runtime-*"))
    with pytest.raises(ConflictError):
        update_runtime_policy(RuntimeUpdate(expected_generation=0))
    assert load_runtime_policy() == result
    rolled_back = update_runtime_policy(RuntimeUpdate(expected_generation=1))
    assert rolled_back.agentic_mode == "disabled" and rolled_back.generation == 2


def test_concurrent_updates_only_one_can_win():
    def update():
        try:
            return update_runtime_policy(RuntimeUpdate(expected_generation=0))
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert sum(isinstance(result, RuntimePolicy) for result in results) == 1
    assert results.count("conflict") == 1 and load_runtime_policy().generation == 1


def _process_policy_update(_):
    try:
        return update_runtime_policy(RuntimeUpdate(expected_generation=0)).generation
    except ConflictError:
        return "conflict"


def test_separate_workers_share_the_same_generation_fence():
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("fork")) as pool:
        outcomes = list(pool.map(_process_policy_update, range(2)))
    assert sorted(str(value) for value in outcomes) == ["1", "conflict"]
    assert load_runtime_policy().generation == 1


@pytest.mark.parametrize(
    "data",
    [
        b"not-json",
        b'{"generation":-1}',
        b'{"search_enabled":true,"unrecognized":true}',
        b'{"agentic_mode":"enabled"}',
        b"x" * (64 * 1024 + 1),
    ],
)
def test_corrupt_policy_is_never_an_enabled_default(data):
    path = runtime._policy_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    with pytest.raises(EvidenceSearchError, match="policy"):
        load_runtime_policy()


@pytest.mark.parametrize(
    "fields",
    [
        {"agentic_mode": "enabled"},
        {"agentic_mode": "shadow"},
        {"agentic_user_ids": [""]},
        {"agentic_user_ids": [" user"]},
        {"agentic_user_ids": ["user", "user"]},
        {"expected_generation": True},
    ],
)
def test_invalid_rollout_is_rejected(fields):
    with pytest.raises(ValidationError):
        RuntimeUpdate.model_validate({"expected_generation": 0, **fields})


def test_write_failure_is_explicit_and_preserves_previous_policy(monkeypatch):
    original = update_runtime_policy(RuntimeUpdate(expected_generation=0))
    monkeypatch.setattr(runtime.os, "replace", Mock(side_effect=OSError("private filesystem detail")))
    with pytest.raises(EvidenceSearchError, match="could not be saved") as error:
        update_runtime_policy(RuntimeUpdate(expected_generation=1, search_enabled=False))
    assert "private" not in str(error.value)
    assert load_runtime_policy() == original
    assert not list(runtime._policy_path().parent.glob(".runtime-*"))


@pytest.mark.parametrize("mode", ["lexical", "vector", "hybrid"])
@pytest.mark.parametrize("switch", ["search_enabled", "advanced_search_enabled"])
def test_kill_switch_gates_service_before_retrieval(mode, switch):
    update_runtime_policy(RuntimeUpdate.model_validate({"expected_generation": 0, switch: False}))
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding("reader"),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    service = PublicRagSearchService(AsyncMock(), dao=Mock(), archive=Mock())
    service._search = AsyncMock(
        return_value=PublicSearchResponse(
            retrieval_build_id="build",
            index_stale=False,
            candidate_count=0,
            excluded_count=0,
            truncated=False,
            results=(),
        )
    )
    request = PublicSearchRequest(question="cache", mode=mode)
    if switch == "advanced_search_enabled" and mode == "lexical":
        assert asyncio.run(service.search(request, principal)).results == ()
        service._search.assert_awaited_once()
    else:
        with pytest.raises(EvidenceSearchError, match="administratively disabled"):
            asyncio.run(service.search(request, principal))
        service._search.assert_not_awaited()
    with pytest.raises(UnauthenticatedError):
        asyncio.run(service.search(request, None))


def test_metrics_have_bounded_labels_and_explicit_outcomes():
    metrics = SearchTelemetry()
    metrics.record("hybrid", "degraded", 12, stale=True)
    metrics.record("lexical", "empty", 3)
    assert metrics.snapshot() == {
        "requests": 2,
        "hybrid:degraded": 1,
        "stale": 1,
        "lexical:empty": 1,
        "duration_ms_total": 15,
        "duration_ms_max": 12,
    }
    with pytest.raises(ValueError):
        metrics.record("user-controlled-label", "ok", 1)


def test_search_kill_switch_also_covers_legacy_document_definition():
    update_runtime_policy(RuntimeUpdate(expected_generation=0, search_enabled=False))
    client = Mock(side_effect=AssertionError("disabled definition reached OpenSearch"))
    repository = DocumentRepository("http://127.0.0.1:9200", client=client)
    with pytest.raises(PublicationError, match="administratively disabled"):
        repository.search(QueryFilters(), intent="definition", text_query="cache", limit=10)
    client.request.assert_not_called()
    client.get.assert_not_called()


def test_unconfigured_status_is_sanitized_without_network(monkeypatch):
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_API_KEY", "never-return-this")
    monkeypatch.setenv("LENS_PUBLIC_RAG_MODEL_PATH", "/private/missing-model")
    factory = Mock(side_effect=AssertionError("unconfigured status attempted network"))
    monkeypatch.setattr(runtime.DocumentRepository, "from_environment", factory)
    result = asyncio.run(runtime_status())
    assert not result.opensearch_configured and result.api_key_configured
    assert not result.model_files_present and result.builds == ()
    payload = result.model_dump_json()
    assert "never-return-this" not in payload and "/private/" not in payload
    factory.assert_not_called()


def test_unavailable_status_is_explicit_not_a_ready_empty_build(monkeypatch):
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://127.0.0.1:9200")
    monkeypatch.setattr(runtime.DocumentRepository, "from_environment", Mock(side_effect=EvidenceSearchError("secret")))
    result = asyncio.run(runtime_status())
    assert [item.state for item in result.builds] == ["unavailable", "unavailable"]
    assert "secret" not in result.model_dump_json()


def test_status_distinguishes_unsettled_activation(monkeypatch):
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://127.0.0.1:9200")
    metadata = Mock()
    metadata.read_record.return_value = ({"status": "pending"}, 0, 1)
    monkeypatch.setattr(runtime.DocumentRepository, "from_environment", lambda: metadata)
    result = asyncio.run(runtime_status())
    assert all(item.state == "recovery_required" for item in result.builds)
    assert metadata.close.call_count == 2
    metadata.public_search_scopes.assert_not_called()
    assert json.loads(result.model_dump_json())["contract"] == "benchmark-rag.runtime.v1"


@pytest.mark.parametrize("stale", [False, True])
def test_status_verifies_active_build_and_latest_task_counts(monkeypatch, stale):
    monkeypatch.setenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", "http://127.0.0.1:9200")
    scope = PublicSearchScope(origin_id="https://llm-d.ai", manifest_id="manifest", generation=1)
    build = PublicSearchBuild(build_id="fixed", scopes=(scope,), document_count=2)
    metadata = Mock()
    history = {
        "build": build.model_dump(mode="json"),
        "physical_index": "public-rag-v1-fixed",
        "status": "active",
        "actual_count": 2,
        "error": None,
    }

    def read(key):
        if key == "build:fixed":
            return history, 0, 1
        if key == "fence:lexical":
            return {"latest_build_id": "fixed"}, 0, 1
        return None

    metadata.read_record.side_effect = read
    metadata.public_search_scopes.return_value = () if stale else (scope,)
    monkeypatch.setattr(runtime.DocumentRepository, "from_environment", lambda: metadata)
    writer = AsyncMock()
    writer.alias_target.side_effect = lambda vector: None if vector else "public-rag-v1-fixed"
    writer.index_build.return_value = build
    monkeypatch.setattr(runtime, "OpenSearchEvidenceProjectionWriter", lambda _: writer)
    result = asyncio.run(runtime_status())
    lexical, vector = result.builds
    assert lexical.state == "ready" and lexical.active_build == build
    assert lexical.latest_status == "active" and lexical.latest_expected_count == lexical.latest_actual_count == 2
    assert lexical.index_stale == stale
    assert vector.state == "missing" and vector.active_build is None


def test_cli_status_and_recovery_require_explicit_safe_inputs(monkeypatch, tmp_path, capsys):
    from llm_d_lens.agentic.document_rag import admin

    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    assert admin.main(["search-status", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["agentic_mode"] == "disabled"
    with pytest.raises(SystemExit) as error:
        admin._parser().parse_args(["search-recover", "--data-dir", str(tmp_path)])
    assert error.value.code == 2
    assert admin.main(["search-rebuild", "--data-dir", str(tmp_path), "--origin", "https://llm-d.ai"]) == 1
    assert "no configured OpenSearch" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["lexical", "vector"])
def test_cli_rebuild_uses_complete_origins_and_offline_encoder_only_when_requested(monkeypatch, tmp_path, capsys, mode):
    from llm_d_lens.agentic.document_rag import admin, embedding, indexing, search

    metadata = Mock()
    monkeypatch.setattr(admin.DocumentRepository, "from_environment", lambda: metadata)
    repository = Mock()
    monkeypatch.setattr(search, "configured_repository", lambda: repository)
    encoder = Mock()
    encoder_factory = Mock(return_value=encoder)
    monkeypatch.setattr(embedding, "configured_encoder", encoder_factory)
    build = PublicSearchBuild(
        build_id="rebuilt",
        scopes=(
            PublicSearchScope(
                origin_id="https://llm-d.ai",
                manifest_id="manifest",
                generation=1,
            ),
        ),
        document_count=1,
        retrieval_mode="opensearch_vector" if mode == "vector" else "opensearch_lexical",
        embedding_contract=embedding.CONTRACT if mode == "vector" else None,
    )
    builder = Mock()
    builder.build = AsyncMock(return_value=build)
    factory = Mock(return_value=builder)
    monkeypatch.setattr(indexing, "PublicRagIndexBuilder", factory)
    assert (
        admin.main(
            [
                "search-rebuild",
                "--data-dir",
                str(tmp_path),
                "--mode",
                mode,
                "--origin",
                "https://llm-d.ai",
                "--origin",
                "https://github.com",
            ]
        )
        == 0
    )
    builder.build.assert_awaited_once_with(
        ("https://llm-d.ai", "https://github.com"),
        encoder=encoder if mode == "vector" else None,
    )
    assert encoder_factory.call_count == int(mode == "vector")
    factory.assert_called_once_with(repository, dao=metadata)
    metadata.prepare.assert_called_once_with()
    metadata.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)["build_id"] == "rebuilt"


def test_cli_source_disable_conflict_is_an_explicit_failure_without_status_success(monkeypatch, tmp_path, capsys):
    from llm_d_lens.agentic.document_rag import admin
    from llm_d_lens.agentic.document_rag.publication_contracts import PublicationConflictError

    metadata = Mock()
    metadata.set_enabled.side_effect = PublicationConflictError("source advanced")
    monkeypatch.setattr(admin.DocumentRepository, "from_environment", lambda: metadata)
    assert (
        admin.main(
            [
                "source-disable",
                "--data-dir",
                str(tmp_path),
                "--origin",
                "https://llm-d.ai",
                "--expected-generation",
                "2",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out and "source advanced" in output.err
    metadata.head.assert_not_called()
    metadata.close.assert_called_once_with()
