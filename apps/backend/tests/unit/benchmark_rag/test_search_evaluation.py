"""Ranking metrics must neither inflate duplicate recall nor conceal failures."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from llm_d_lens.benchmark_rag.evaluation import (
    SearchEvaluationSuite,
    SearchJudgment,
    benchmark_search,
    evaluate_search,
    ranking_metrics,
)
from llm_d_lens.benchmark_rag.query_contracts import Citation
from llm_d_lens.benchmark_rag.search_contracts import (
    PublicSearchRequest,
    PublicSearchResponse,
    PublicSearchResult,
)


def judgment(**updates):
    return SearchJudgment(
        case_id="example",
        category="technical",
        split="development",
        question="example",
        relevant={"a" * 64: 3, "b" * 64: 1},
    ).model_copy(update=updates)


def test_duplicate_children_do_not_inflate_parent_recall():
    result = ranking_metrics(judgment(), ("a" * 64, "a" * 64))
    assert result["recall_at_10"] == 0.5
    assert 0 < result["ndcg_at_10"] < 1


def test_exact_order_has_unit_ndcg_and_recall():
    assert ranking_metrics(judgment(), ("a" * 64, "b" * 64)) == {
        "recall_at_10": 1,
        "ndcg_at_10": 1,
        "forbidden_count": 0,
    }


def test_irrelevant_hits_and_forbidden_ids_are_not_hidden():
    result = ranking_metrics(judgment(forbidden=("c" * 64,)), ("c" * 64, "a" * 64))
    assert result["forbidden_count"] == 1 and result["recall_at_10"] == 0.5
    assert result["ndcg_at_10"] < ranking_metrics(judgment(), ("a" * 64,))["ndcg_at_10"]


def test_absent_evidence_is_not_included_in_recall_denominator():
    result = ranking_metrics(judgment(relevant={}, expect_empty=True), ())
    assert result == {"empty_correct": 1, "forbidden_count": 0}


def test_six_category_dataset_is_frozen_complete_and_split():
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance.json")
    assert len(suite.cases) == 60 and len(suite.anchors) == 10
    assert sum(case.split == "holdout" for case in suite.cases) == 24
    with pytest.raises(ValidationError):
        SearchEvaluationSuite.model_validate({**suite.model_dump(), "cases": suite.cases[:59]})
    with pytest.raises(ValidationError):
        SearchEvaluationSuite.model_validate({**suite.model_dump(), "cases": (suite.cases[0],) * 60})


def test_v2_suite_covers_all_pinned_sources_with_balanced_splits():
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance_v2.json")
    assert len(suite.cases) == 100
    assert len(suite.manifests) == 8
    assert len([case for case in suite.cases if case.expect_empty]) == 10
    assert len([case for case in suite.cases if not case.expect_empty and len(case.target_origins) > 1]) == 10
    for origin in suite.manifests:
        targeted = [case for case in suite.cases if not case.expect_empty and case.target_origins == (origin,)]
        assert len(targeted) == 10
        assert sum(case.split == "development" for case in targeted) == 5
        assert sum(case.split == "holdout" for case in targeted) == 5


def test_v2_quality_runner_reports_per_source_and_macro_metrics():
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance_v2.json")
    cases_by_question = {case.question: case for case in suite.cases}

    async def perfect_search(request):
        case = cases_by_question[request.question]
        results = []
        for evidence_id in case.relevant:
            origin = suite.anchor_origins[evidence_id]
            if origin not in request.origins:
                continue
            citation = Citation(
                origin_id=origin,
                source_revision=suite.revisions[origin],
                source_path=suite.anchors[evidence_id],
                sha256="a" * 64,
                artifact_uri="lens-artifact://test",
                source_pointer={},
                published_manifest_id=suite.manifests[origin],
            )
            results.append(
                PublicSearchResult(
                    evidence_id=evidence_id,
                    chunk_id=evidence_id,
                    title="fixture",
                    heading_path=(),
                    text="fixture evidence",
                    citation=citation,
                    score=1,
                )
            )
        return PublicSearchResponse(
            retrieval_build_id="test",
            index_stale=False,
            candidate_count=len(results),
            excluded_count=0,
            truncated=False,
            results=tuple(results),
        )

    result = asyncio.run(evaluate_search(suite, perfect_search))
    assert result["groups"]["source_macro"] == {
        "recall_at_10": 1,
        "ndcg_at_10": 1,
        "forbidden_count": 0,
    }
    assert set(result["per_source"]) == set(suite.manifests)
    assert all(item["case_count"] >= 10 for item in result["per_source"].values())
    assert all(item == {"case_count": 10, "empty_correct": 1} for item in result["per_source_absence"].values())


def empty_response(**updates):
    return PublicSearchResponse(
        retrieval_build_id="test",
        index_stale=False,
        candidate_count=0,
        excluded_count=0,
        truncated=False,
        results=(),
    ).model_copy(update=updates)


def test_quality_runner_propagates_errors_and_refuses_stale_data():
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance.json")
    with pytest.raises(RuntimeError, match="failure"):
        asyncio.run(evaluate_search(suite, AsyncMock(side_effect=RuntimeError("failure"))))
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(evaluate_search(suite, AsyncMock(return_value=empty_response(index_stale=True))))
    with pytest.raises(ValueError, match="degraded"):
        asyncio.run(
            evaluate_search(suite, AsyncMock(return_value=empty_response(degraded_reason="vector_unavailable")))
        )


def test_target_attribution_does_not_filter_competitors_or_promote_source_ranks():
    suite = SearchEvaluationSuite.load(Path(__file__).parents[2] / "fixtures/public_rag/search_acceptance_v2.json")
    cases_by_question = {case.question: case for case in suite.cases}

    async def competing_search(request):
        assert request.origins == tuple(suite.manifests)
        case = cases_by_question[request.question]
        if case.expect_empty:
            return empty_response()
        evidence_id = next(iter(case.relevant))
        origin = suite.anchor_origins[evidence_id]
        other_origin = next(source for source in suite.manifests if source != origin)

        def hit(identity, source):
            return PublicSearchResult(
                evidence_id=identity,
                chunk_id=identity,
                title="fixture",
                heading_path=(),
                text="fixture",
                citation=Citation(
                    origin_id=source,
                    source_revision=suite.revisions[source],
                    source_path="fixture.md",
                    sha256="a" * 64,
                    artifact_uri="lens-artifact://test",
                    source_pointer={},
                    published_manifest_id=suite.manifests[source],
                ),
                score=1,
            )

        return empty_response(results=(hit("f" * 64, other_origin), hit(evidence_id, origin)))

    result = asyncio.run(evaluate_search(suite, competing_search))
    for origin in suite.manifests:
        selected = [case for case in suite.cases if not case.expect_empty and origin in case.target_origins]
        expected = []
        for case in selected:
            relevant = {
                identity: grade for identity, grade in case.relevant.items() if suite.anchor_origins[identity] == origin
            }
            if relevant:
                expected.append(
                    ranking_metrics(
                        case,
                        ("f" * 64, next(iter(case.relevant))),
                    )["ndcg_at_10"]
                )
        assert result["per_source"][origin]["ndcg_at_10"] == pytest.approx(sum(expected) / len(expected))
    assert result["micro_recall_at_10"] == pytest.approx(90 / sum(len(case.relevant) for case in suite.cases))
    assert "holdout/technical" in result["groups"]
    assert "holdout/source_macro" in result["groups"]


def test_performance_runner_rejects_empty_success_and_failed_samples():
    requests = (PublicSearchRequest(question="TTFT"),)
    with pytest.raises(ValueError, match="verified"):
        asyncio.run(benchmark_search(requests, AsyncMock(return_value=empty_response()), samples=1, warmup=0))
    with pytest.raises(RuntimeError, match="failure"):
        asyncio.run(benchmark_search(requests, AsyncMock(side_effect=RuntimeError("failure")), samples=1, warmup=0))
    with pytest.raises(ValueError, match="budgets"):
        asyncio.run(benchmark_search(requests, AsyncMock(), concurrency=0))
