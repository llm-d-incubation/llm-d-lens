"""Opt-in frozen-corpus quality, revocation and isolated 10k-document load checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from llm_d_lens.api.problems import install_problem_handlers
from llm_d_lens.auth import context as auth_context
from llm_d_lens.auth import router as auth_routes
from llm_d_lens.auth.context import AuthContextMiddleware, require_route_permission
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.service import AuthService
from llm_d_lens.auth.settings import AuthSettings, set_settings_for_testing
from llm_d_lens.benchmark_rag import document_repository as metadata
from llm_d_lens.benchmark_rag import opensearch_evidence_search as adapter
from llm_d_lens.benchmark_rag import router as search_routes
from llm_d_lens.benchmark_rag.document_transfer import MAX_BACKUP_BYTES, DocumentBackupService
from llm_d_lens.benchmark_rag.evaluation import SearchEvaluationSuite, benchmark_search, evaluate_search
from llm_d_lens.benchmark_rag.indexing import PublicRagIndexBuilder, passage_document
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    RegistryState,
    SourceApproval,
    content_digest,
)
from llm_d_lens.benchmark_rag.query_contracts import QUERY_PERMISSION
from llm_d_lens.benchmark_rag.repository_corpus import PARSER_VERSION, parse_markdown
from llm_d_lens.benchmark_rag.search import PublicRagSearchService
from llm_d_lens.benchmark_rag.search_contracts import EvidenceSearchError, PublicSearchRequest, PublicSearchResponse
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, SourceArchive, allowed_paths
from llm_d_lens.core.exceptions import ForbiddenError, UnauthenticatedError
from llm_d_lens.db.base import Base
from llm_d_lens.db.dao.audit_log import AuditLogDao
from llm_d_lens.db.dao.group import GroupDao, GroupRoleBindingDao, UserGroupDao
from llm_d_lens.db.dao.identity_provider import IdentityGroupMappingDao, IdentityProviderDao
from llm_d_lens.db.dao.role import RoleDao, RolePermissionDao, UserRoleBindingDao
from llm_d_lens.db.dao.session import SessionDao
from llm_d_lens.db.dao.user import UserDao

_PERFORMANCE_PASSWORD = "Isolated-Performance-9!"  # noqa: S105 - disposable test database only


def report(name, payload):
    root = os.environ.get("LENS_RAG_ACCEPTANCE_REPORT_DIR")
    if root:
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def authenticated_http_app(factory, monkeypatch):
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
    settings = AuthSettings(auth_mode="local", auto_seed_admin=False)
    auth = AuthService(
        settings,
        user_dao=UserDao(factory),
        session_dao=SessionDao(factory),
        audit_dao=AuditLogDao(factory),
        user_group_dao=UserGroupDao(factory),
        group_dao=GroupDao(factory),
        user_binding_dao=UserRoleBindingDao(factory),
        group_binding_dao=GroupRoleBindingDao(factory),
        role_dao=RoleDao(factory),
        role_permission_dao=RolePermissionDao(factory),
        identity_provider_dao=IdentityProviderDao(factory),
        identity_mapping_dao=IdentityGroupMappingDao(factory),
    )
    user = auth.create_user(username="performance-reader", password=_PERFORMANCE_PASSWORD)
    role = auth.create_role(name="performance-reader", permissions={QUERY_PERMISSION})
    auth.add_user_binding(user.id, role.id, scope_type="global")
    monkeypatch.setattr(auth_context, "default_service", lambda: auth)
    monkeypatch.setattr(auth_routes, "default_service", lambda: auth)
    set_settings_for_testing(settings)
    app = FastAPI(dependencies=[Depends(require_route_permission)])
    install_problem_handlers(app)
    app.add_middleware(AuthContextMiddleware)
    app.include_router(auth_routes.router)
    app.include_router(search_routes.router)
    return app


def restore_document_corpus(dao, archive, bundle, manifests):
    backup = DocumentBackupService(dao, archive)
    verified, records, _, _ = backup._read(Path(bundle), MAX_BACKUP_BYTES)
    restored = backup.restore(Path(bundle))
    assert restored.total_rows == verified.row_count + verified.external_count
    assert backup.restore(Path(bundle)).inserted == 0
    publication = RagPublicationService(dao=dao, archive=archive)
    registries = {item.origin_id: item for item in records if item.source_role == "registry"}
    for origin, manifest_id in manifests.items():
        registry = registries[origin]
        assert registry.published_manifest_id == manifest_id
        state = RegistryState.model_validate(registry.source_details)
        assert state.enabled
        publication.register_source(origin, state.approval)
        publication.activate(origin, manifest_id, expected_generation=0)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    endpoint = os.environ.get("LENS_RAG_ACCEPTANCE_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("Set LENS_RAG_ACCEPTANCE_OPENSEARCH_URL to an isolated, approved acceptance service")
    import pgserver

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(adapter, "ACTIVE_ALIAS", "public-rag-acceptance-" + uuid4().hex)
    monkeypatch.setattr(adapter, "VECTOR_ALIAS", "public-rag-vector-acceptance-" + uuid4().hex)
    monkeypatch.setattr(metadata, "MANAGEMENT_INDEX", "public-rag-management-acceptance-" + uuid4().hex)
    server = pgserver.get_server(tmp_path / "postgres", cleanup_mode="stop")
    engine = create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1), pool_size=12)
    indexes = []
    repository = adapter.OpenSearchEvidenceSearchRepository(endpoint, timeout_seconds=20)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    dao = metadata.DocumentRepository(endpoint)
    dao.prepare(initialize=True)
    archive = SourceArchive()
    principal = Principal(
        user_id="acceptance-reader",
        username="acceptance-reader",
        bindings=(RoleBinding("reader", ScopeType.GLOBAL),),
        role_permissions={"reader": frozenset({QUERY_PERMISSION})},
    )
    try:
        yield dao, archive, repository, principal, indexes, factory
    finally:

        async def cleanup():
            for index in indexes:
                await repository._request("DELETE", "/" + index)
            await repository._request("DELETE", "/" + metadata.MANAGEMENT_INDEX)

        try:
            asyncio.run(cleanup())
        finally:
            engine.dispose()
            server.cleanup()
            dao.close()


def test_frozen_corpus_quality_and_publication_guards(environment):
    dao, archive, repository, principal, indexes, _ = environment
    bundle = os.environ.get("LENS_RAG_ACCEPTANCE_BUNDLE")
    if not bundle:
        pytest.skip("Set LENS_RAG_ACCEPTANCE_BUNDLE to the verified eight-scope backup")
    suite_path = Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance_v2.json"
    suite = SearchEvaluationSuite.load(suite_path)
    restore_document_corpus(dao, archive, bundle, suite.manifests)
    scopes = dao.public_search_scopes(tuple(suite.manifests))
    assert {scope.origin_id: scope.manifest_id for scope in scopes} == suite.manifests
    for origin, revision in suite.revisions.items():
        assert dao.head(origin).manifest.source_revision == revision
    anchors = dao.resolve_public_passages(tuple(suite.manifests), tuple(suite.anchors))
    assert len(anchors) == len(suite.anchors)
    assert all(suite.anchors[match.item.item_id] == match.item.source_path for match in anchors)

    async def run():
        build = await PublicRagIndexBuilder(repository, dao=dao, archive=archive).build(tuple(suite.manifests))
        indexes.append(adapter.INDEX_PREFIX + build.build_id)
        passage_count = sum(len(dao.list_published(origin, kind="passage")) for origin in suite.manifests)
        assert build.document_count == passage_count
        service = PublicRagSearchService(repository, dao=dao, archive=archive)

        async def search(request):
            return await service.search(request, principal)

        result = await evaluate_search(suite, search)
        result.update({"suite_sha256": content_digest(suite.model_dump()), "build": build.model_dump(mode="json")})
        report("public-rag-lexical-quality.json", result)
        print("QUALITY", json.dumps(result["groups"], sort_keys=True))
        assert result["groups"]["all"]["forbidden_count"] == 0

        with pytest.raises(UnauthenticatedError):
            await service.search(PublicSearchRequest(question="TTFT"), None)
        with pytest.raises(ForbiddenError):
            await service.search(
                PublicSearchRequest(question="TTFT"),
                Principal(
                    user_id="cluster-only",
                    username="cluster-only",
                    bindings=(RoleBinding("reader", ScopeType.CLUSTER, scope_cluster_id="fixture"),),
                    role_permissions={"reader": frozenset({QUERY_PERMISSION})},
                ),
            )
        dao.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
        for case in suite.cases[:10]:
            withdrawn = await search(PublicSearchRequest(question=case.question, limit=10))
            assert withdrawn.index_stale
            assert all(hit.citation.origin_id != LLM_D_ORIGIN for hit in withdrawn.results)
        dao.set_enabled(LLM_D_ORIGIN, True, expected_generation=2)
        stale = await search(PublicSearchRequest(question="KEDA WVA", origins=(LLM_D_ORIGIN,)))
        assert stale.index_stale and not stale.results

        bad_repository = adapter.OpenSearchEvidenceSearchRepository("http://127.0.0.1:1", timeout_seconds=0.2)
        with pytest.raises(EvidenceSearchError):
            await PublicRagSearchService(bad_repository, dao=dao, archive=archive).search(
                PublicSearchRequest(question="TTFT"), principal
            )

    asyncio.run(run())


def test_real_model_vector_corpus_cache_replay_and_quality(environment, tmp_path, monkeypatch):
    model_path = os.environ.get("LENS_RAG_ACCEPTANCE_MODEL_PATH")
    bundle = os.environ.get("LENS_RAG_ACCEPTANCE_BUNDLE")
    if not model_path or not bundle:
        pytest.skip("Set approved model path and corpus backup for real S2 acceptance")
    from llm_d_lens.benchmark_rag.embedding import LocalE5Encoder, passage_input

    dao, archive, repository, principal, indexes, auth_factory = environment
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance_v2.json")
    restore_document_corpus(dao, archive, bundle, suite.manifests)
    assert {
        scope.origin_id: scope.manifest_id for scope in dao.public_search_scopes(tuple(suite.manifests))
    } == suite.manifests
    encoder = LocalE5Encoder(
        Path(model_path), Path(os.environ.get("LENS_RAG_ACCEPTANCE_EMBEDDING_CACHE") or tmp_path / "vector-cache")
    )
    chunks = [
        chunk
        for scope in dao.public_search_scopes(tuple(suite.manifests))
        for item in dao.list_published(scope.origin_id, kind="passage")
        for chunk in encoder.chunks(passage_document(item, scope))
    ]
    passage_count = sum(len(dao.list_published(origin, kind="passage")) for origin in suite.manifests)
    assert len(chunks) >= passage_count
    assert all(len(encoder.tokenizer(passage_input(chunk), truncation=False)["input_ids"]) <= 512 for chunk in chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    with pytest.raises(EvidenceSearchError, match="token budget"):
        encoder.query("字 " * 600)

    async def run():
        builder = PublicRagIndexBuilder(repository, dao=dao, archive=archive)
        lexical = await builder.build(tuple(suite.manifests))
        indexes.append(adapter.INDEX_PREFIX + lexical.build_id)
        vector = await builder.build(tuple(suite.manifests), encoder=encoder)
        indexes.append(adapter.INDEX_PREFIX + vector.build_id)
        encoded = encoder.encoded_inputs
        replay = await builder.build(tuple(suite.manifests), encoder=encoder)
        indexes.append(adapter.INDEX_PREFIX + replay.build_id)
        assert encoder.encoded_inputs == encoded and encoder.cache_hits > 0
        assert (await repository.active_build())[1] == lexical
        assert vector.document_count == replay.document_count == len(chunks)
        service = PublicRagSearchService(repository, dao=dao, archive=archive, encoder=encoder)

        async def search(request):
            return await service.search(request.model_copy(update={"mode": "vector"}), principal)

        result = await evaluate_search(suite, search)
        result.update(
            {
                "suite_sha256": content_digest(suite.model_dump()),
                "build": replay.model_dump(mode="json"),
                "encoded_document_inputs": encoded,
                "cache_hits": encoder.cache_hits,
            }
        )
        report("public-rag-vector-quality.json", result)
        print("VECTOR_QUALITY", json.dumps(result["groups"], sort_keys=True))
        assert result["groups"]["all"]["forbidden_count"] == 0
        for category in ("technical", "cross_language"):
            assert result["groups"][category]["recall_at_10"] > 0

        async def lexical_search(request):
            return await service.search(request.model_copy(update={"mode": "lexical"}), principal)

        async def hybrid_search(request):
            response = await service.search(request.model_copy(update={"mode": "hybrid"}), principal)
            assert response.retrieval_mode == "hybrid" and response.degraded_reason is None
            assert response.retrieval_build_id == replay.build_id
            assert response.embedding_contract == encoder.contract
            assert response.candidate_count <= 400 and response.refill_count <= 2
            return response

        baseline = await evaluate_search(suite, lexical_search)
        hybrid = await evaluate_search(suite, hybrid_search)
        gates = {
            "recall_at_least_0_90": hybrid["groups"]["all"]["recall_at_10"] >= 0.90,
            "ndcg_at_least_0_80": hybrid["groups"]["all"]["ndcg_at_10"] >= 0.80,
            "overall_recall_not_lower": hybrid["groups"]["all"]["recall_at_10"]
            >= baseline["groups"]["all"]["recall_at_10"],
            "technical_recall_not_lower": hybrid["groups"]["technical"]["recall_at_10"]
            >= baseline["groups"]["technical"]["recall_at_10"],
            "forbidden_count_zero": hybrid["groups"]["all"]["forbidden_count"] == 0,
            "all_source_scope_reports_present": len(hybrid["per_source"]) == len(suite.manifests),
            "all_source_absence_reports_present": len(hybrid["per_source_absence"]) == len(suite.manifests)
            and all(item["case_count"] == 10 for item in hybrid["per_source_absence"].values()),
        }
        for split in ("all", "holdout"):
            prefix = "" if split == "all" else "holdout/"
            gates[prefix + "recall_at_least_0_90"] = hybrid["groups"][split]["recall_at_10"] >= 0.90
            gates[prefix + "ndcg_at_least_0_80"] = hybrid["groups"][split]["ndcg_at_10"] >= 0.80
            gates[prefix + "overall_recall_not_lower"] = (
                hybrid["groups"][split]["recall_at_10"] >= baseline["groups"][split]["recall_at_10"]
            )
            for category in ("technical", "cross_language", "paraphrase"):
                label = prefix + category
                before = baseline["groups"][label]["recall_at_10"]
                after = hybrid["groups"][label]["recall_at_10"]
                threshold = before if category == "technical" or before >= 0.95 else before + 0.10
                gates[label + "_recall_requirement"] = after + 1e-12 >= threshold
        report(
            "public-rag-hybrid-quality.json",
            {
                **hybrid,
                "baseline_groups": baseline["groups"],
                "vector_groups": result["groups"],
                "build": replay.model_dump(mode="json"),
                "suite_sha256": content_digest(suite.model_dump()),
                "quality_gates": gates,
                "automated_quality_passed": all(gates.values()),
                "independent_human_review_completed": False,
                "launch_approved": False,
            },
        )
        print("HYBRID_QUALITY", json.dumps({"groups": hybrid["groups"], "gates": gates}, sort_keys=True))
        assert gates["forbidden_count_zero"]
        # Relevance failures block launch, not this engineering regression.
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)) as client:
            service.repository = adapter.OpenSearchEvidenceSearchRepository(
                os.environ["LENS_RAG_ACCEPTANCE_OPENSEARCH_URL"], client=client, timeout_seconds=20
            )
            try:
                performance = await benchmark_search(
                    tuple(
                        PublicSearchRequest(question=case.question, origins=tuple(suite.manifests), mode="hybrid")
                        for case in suite.cases
                        if not case.expect_empty
                    ),
                    hybrid_search,
                )
            finally:
                service.repository = repository
        report(
            "public-rag-hybrid-performance.json",
            {
                **performance,
                "parent_passages": passage_count,
                "indexed_chunks": replay.document_count,
                "measurement_boundary": "service: embedding, both searches, RRF, authority, archive and citations",
                "isolation": "disposable PostgreSQL, unique test indexes, shared OpenSearch and host",
                "ten_thousand_passage_gate_verified": False,
                "python_version": platform.python_version(),
                "opensearch_version": (await repository._request("GET", "/"))["version"]["number"],
                "model_contract": encoder.contract.model_dump(mode="json"),
                "host_cpu_count": os.cpu_count(),
                "http_transport": "application-equivalent pooled AsyncClient: 50 connections, 20 keepalive",
            },
        )
        print("HYBRID_PERFORMANCE", json.dumps(performance, sort_keys=True))
        with monkeypatch.context() as faults:
            faults.setattr(encoder, "query", Mock(side_effect=EvidenceSearchError("injected encoder outage")))
            degraded = await service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal)
            assert degraded.retrieval_mode == "opensearch_lexical"
            assert degraded.degraded_reason == "vector_unavailable" and degraded.results
            assert all(hit.matched_branches == ("lexical",) for hit in degraded.results)
        with monkeypatch.context() as faults:
            faults.setattr(encoder, "contract", encoder.contract.model_copy(update={"model_revision": "b" * 40}))
            mismatch = await service.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal)
            assert mismatch.degraded_reason == "embedding_contract_mismatch" and mismatch.results
        unavailable = PublicRagSearchService(
            adapter.OpenSearchEvidenceSearchRepository("http://127.0.0.1:1", timeout_seconds=0.1),
            dao=dao,
            archive=archive,
            encoder=encoder,
        )
        with pytest.raises(EvidenceSearchError):
            await unavailable.search(PublicSearchRequest(question="TTFT", mode="hybrid"), principal)
        app = authenticated_http_app(auth_factory, monkeypatch)
        monkeypatch.setattr(search_routes, "get_search_service", lambda request: service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://acceptance") as browser:
            payload = {"question": "首个词元延迟", "mode": "vector", "limit": 3}
            assert (await browser.post("/api/v1/benchmark-rag/search", json=payload)).status_code == 401
            login = await browser.post(
                "/api/v1/auth/login", json={"username": "performance-reader", "password": _PERFORMANCE_PASSWORD}
            )
            assert login.status_code == 200
            assert (await browser.post("/api/v1/benchmark-rag/search", json=payload)).status_code == 403
            browser.headers["X-Prism-CSRF"] = browser.cookies["prism_csrf"]
            response = await browser.post("/api/v1/benchmark-rag/search", json=payload)
            assert response.status_code == 200, response.text
            body = PublicSearchResponse.model_validate_json(response.content)
            assert body.retrieval_mode == "opensearch_vector" and len(body.results) == 3
            assert body.embedding_contract == encoder.contract
            payload["mode"] = "hybrid"
            response = await browser.post("/api/v1/benchmark-rag/search", json=payload)
            assert response.status_code == 200, response.text
            body = PublicSearchResponse.model_validate_json(response.content)
            assert body.retrieval_mode == "hybrid" and body.degraded_reason is None
            assert all(hit.fusion_score is not None for hit in body.results)
        dao.set_enabled(LLM_D_ORIGIN, False, expected_generation=1)
        withdrawn = await search(PublicSearchRequest(question="prefix cache"))
        assert withdrawn.index_stale
        assert all(hit.citation.origin_id != LLM_D_ORIGIN for hit in withdrawn.results)
        withdrawn_hybrid = await hybrid_search(PublicSearchRequest(question="prefix cache"))
        assert withdrawn_hybrid.index_stale and withdrawn_hybrid.rebuild_required
        assert all(hit.citation.origin_id != LLM_D_ORIGIN for hit in withdrawn_hybrid.results)
        with pytest.raises(UnauthenticatedError):
            await service.search(PublicSearchRequest(question="TTFT", mode="vector"), None)

    asyncio.run(run())


def test_ten_thousand_passages_ten_concurrent_five_hundred_queries(environment, monkeypatch, tmp_path):
    dao, archive, repository, principal, indexes, auth_factory = environment
    publication = RagPublicationService(dao=dao, archive=archive)
    publication.register_source(
        BENCHMARK_ORIGIN,
        SourceApproval(
            verified_public=True,
            approved_by="isolated synthetic performance fixture",
            approval_reference="never used for relevance scoring",
            license_reference="generated fixture",
            parser_version=PARSER_VERSION,
            allowed_paths=allowed_paths(BENCHMARK_ORIGIN),
        ),
    )
    files = []
    contents = {}
    model_path = os.environ.get("LENS_RAG_ACCEPTANCE_MODEL_PATH")
    for file_number in range(100):
        text = "".join(
            f"# Benchmark fixture section {section:03d}\n"
            f"Performance group topic{section:03d} covers prefix caching, TTFT, vLLM and request throughput. "
            "This generated text exercises publication membership and archive verification, not relevance. "
            "Use --enable-prefix-caching for repeated prompt tokens. "
            "Several paragraphs retain unique identities without pretending to be independent quality evidence.\n\n"
            for section in range(100)
        ).encode()
        digest = archive.put(text)
        file = InventoryFile(path=f"docs/performance/fixture-{file_number:03d}.md", sha256=digest, format="markdown")
        assert digest == hashlib.sha256(text).hexdigest()
        files.append(file)
        contents[file.path] = text
    inventory = PinnedInventory(origin_id=BENCHMARK_ORIGIN, revision="b" * 40, complete=True, files=tuple(files))
    parsed = tuple(
        ParsedFile(path=file.path, items=parse_markdown(file, inventory, contents[file.path])) for file in files
    )
    manifest = publication.stage(inventory, parsed, expected_counts={"source": 100, "passage": 10000})
    publication.activate(BENCHMARK_ORIGIN, manifest.item_id)

    async def run():
        encoder = None
        if model_path:
            from llm_d_lens.benchmark_rag.embedding import LocalE5Encoder

            encoder = LocalE5Encoder(
                Path(model_path),
                Path(os.environ.get("LENS_RAG_ACCEPTANCE_EMBEDDING_CACHE") or tmp_path / "vector-cache"),
            )
        mode = "hybrid" if encoder else "lexical"
        build = await PublicRagIndexBuilder(repository, dao=dao, archive=archive).build(
            (BENCHMARK_ORIGIN,), encoder=encoder
        )
        indexes.append(adapter.INDEX_PREFIX + build.build_id)
        assert build.document_count == 10000
        service = PublicRagSearchService(repository, dao=dao, archive=archive)

        async def search(request):
            response = await service.search(request, principal)
            assert response.retrieval_mode == ("hybrid" if encoder else "opensearch_lexical")
            assert response.degraded_reason is None
            return response

        requests = tuple(
            PublicSearchRequest(
                question=f"topic{number:03d} prefix cache TTFT",
                origins=(BENCHMARK_ORIGIN,),
                limit=10,
                mode=mode,
            )
            for number in range(20)
        )
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)) as client:
            pooled = adapter.OpenSearchEvidenceSearchRepository(
                os.environ["LENS_RAG_ACCEPTANCE_OPENSEARCH_URL"], client=client, timeout_seconds=20
            )
            service = PublicRagSearchService(pooled, dao=dao, archive=archive, encoder=encoder)
            result = await benchmark_search(requests, search)
            result.update(
                {
                    "document_count": build.document_count,
                    "fixture_kind": "synthetic-performance-only: 100 repeated sections across 100 distinct paths",
                    "retrieval_mode": mode,
                    "measurement_boundary": "service: authorization, embedding when hybrid, OpenSearch, RRF when hybrid, PostgreSQL authority, archive, citations",
                    "python_version": platform.python_version(),
                    "host_cpu_count": os.cpu_count(),
                    "opensearch_version": (await repository._request("GET", "/"))["version"]["number"],
                    "isolation": "disposable PostgreSQL and unique indexes; OpenSearch and host isolation must be recorded by the operator",
                    "embedding_contract": encoder.contract.model_dump(mode="json") if encoder else None,
                    "encoded_inputs": encoder.encoded_inputs if encoder else None,
                    "cache_hits": encoder.cache_hits if encoder else None,
                }
            )
            report(f"public-rag-{mode}-synthetic-performance.json", result)
            print("PERFORMANCE", json.dumps(result, sort_keys=True))
            app = authenticated_http_app(auth_factory, monkeypatch)
            monkeypatch.setattr(search_routes, "get_search_service", lambda request: service)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://acceptance"
            ) as browser:
                login = await browser.post(
                    "/api/v1/auth/login",
                    json={"username": "performance-reader", "password": _PERFORMANCE_PASSWORD},
                )
                assert login.status_code == 200, login.text
                browser.headers["X-Prism-CSRF"] = browser.cookies["prism_csrf"]

                async def http_search(request):
                    response = await browser.post("/api/v1/benchmark-rag/search", json=request.model_dump(mode="json"))
                    response.raise_for_status()
                    body = PublicSearchResponse.model_validate_json(response.content)
                    assert body.retrieval_mode == ("hybrid" if encoder else "opensearch_lexical")
                    assert body.degraded_reason is None
                    return body

                http_result = await benchmark_search(requests, http_search)
                http_result.update(
                    {
                        "document_count": build.document_count,
                        "measurement_boundary": "ASGI HTTP: real session authentication, CSRF, routing, service and serialization; excludes network/TLS",
                    }
                )
                report(f"public-rag-{mode}-synthetic-http-performance.json", http_result)
                print("HTTP_PERFORMANCE", json.dumps(http_result, sort_keys=True))
                assert http_result["p95_seconds"] <= (3 if encoder else 2)
        assert result["p95_seconds"] <= (3 if encoder else 2)

    asyncio.run(run())
