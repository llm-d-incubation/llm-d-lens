"""Read-only, fixed-case evidence evaluation with mandatory provenance checks."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from llm_d_lens.benchmark_rag.publication_contracts import PublicationModel, content_digest
from llm_d_lens.benchmark_rag.query_contracts import EvidenceType, QueryEvidence, QueryRequest, QueryResponse
from llm_d_lens.benchmark_rag.search_contracts import PublicSearchRequest, PublicSearchResponse
from llm_d_lens.core.exceptions import DomainError

EvaluationCategory = Literal[
    "numeric", "conditions", "definition", "figure_gap", "false_join", "comparison", "access", "provenance", "overlap"
]
_NUMERIC_CLAIM = re.compile(
    r"^\[(public|agentic|public_and_agentic)\] (.*?) \((.*?)\): "
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?) ([^;\n]+);",
    re.MULTILINE,
)


class EvaluationCase(PublicationModel):
    case_id: str = Field(min_length=1)
    category: EvaluationCategory
    actor: str = "reader"
    request: QueryRequest
    expected_status: Literal["answered", "partial", "abstained"] | None = "answered"
    expected_error: str | None = None
    expected_matched_items: int | None = Field(default=None, ge=0)
    expected_returned: int | None = Field(default=None, ge=0)
    minimum_evidence: int = Field(default=0, ge=0)
    expected_truncated: bool | None = None
    allowed_origins: tuple[str, ...] = ()
    allowed_evidence_types: tuple[EvidenceType, ...] = ()
    required_values: tuple[Decimal, ...] = ()
    answer_contains: tuple[str, ...] = ()
    forbidden_fragments: tuple[str, ...] = ()
    expected_abstentions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def explicit_outcome(self) -> Self:
        if self.expected_error is None and self.expected_status is None:
            raise ValueError("a case needs an expected response status or domain error")
        if any(not value.is_finite() for value in self.required_values):
            raise ValueError("expected measurement values must be finite")
        if self.expected_returned is not None and self.minimum_evidence > self.expected_returned:
            raise ValueError("minimum evidence cannot exceed the exact expected count")
        return self


class EvaluationSourcePin(PublicationModel):
    revision: str = Field(min_length=1)
    inventory_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EvaluationSuite(PublicationModel):
    contract: Literal["benchmark-rag.evaluation.v1"] = "benchmark-rag.evaluation.v1"
    name: str = Field(min_length=1)
    source_pins: dict[str, EvaluationSourcePin] = Field(default_factory=dict)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("evaluation case IDs must be unique")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json", exclude_unset=True))


class CaseResult(PublicationModel):
    case_id: str
    category: EvaluationCategory
    passed: bool
    issues: tuple[str, ...]
    evidence_count: int
    numeric_count: int
    verified_citations: int
    error_code: str | None = None


class EvaluationReport(PublicationModel):
    contract: Literal["benchmark-rag.evaluation-report.v1"] = "benchmark-rag.evaluation-report.v1"
    suite: str
    suite_sha256: str
    source_pins: dict[str, EvaluationSourcePin]
    started_at: datetime
    finished_at: datetime
    total_cases: int
    passed_cases: int
    failed_cases: int
    passed: bool
    category_counts: dict[EvaluationCategory, int]
    verified_citations: int
    numeric_observations: int
    results: tuple[CaseResult, ...]


def _response_issues(case: EvaluationCase, response: QueryResponse) -> list[str]:
    issues = []
    if case.expected_error is not None:
        issues.append("expected_domain_error_missing")
    if case.expected_status is not None and response.status != case.expected_status:
        issues.append("unexpected_response_status")
    if response.question != case.request.question:
        issues.append("response_question_mismatch")
    if response.internal_branch_enabled != case.request.include_internal:
        issues.append("internal_opt_in_mismatch")
    if response.returned_observations != len(response.evidence):
        issues.append("returned_observations_mismatch")
    if len(response.evidence) > case.request.limit:
        issues.append("response_exceeds_limit")
    if len({entry.item_id for entry in response.evidence}) != len(response.evidence):
        issues.append("duplicate_evidence_identity")
    if case.expected_matched_items is not None and response.matched_items != case.expected_matched_items:
        issues.append("unexpected_match_count")
    if case.expected_returned is not None and len(response.evidence) != case.expected_returned:
        issues.append("unexpected_evidence_count")
    if len(response.evidence) < case.minimum_evidence:
        issues.append("missing_required_evidence")
    if case.expected_truncated is not None and response.truncated != case.expected_truncated:
        issues.append("unexpected_truncation")
    values = Counter(entry.value for entry in response.evidence if entry.value is not None)
    if Counter(case.required_values) - values:
        issues.append("missing_exact_measurement")
    claims = Counter(
        (origin, metric, statistic, Decimal(value), unit)
        for origin, metric, statistic, value, unit in _NUMERIC_CLAIM.findall(response.answer)
    )
    supported = Counter(
        (
            entry.origin,
            entry.metric,
            entry.statistic or "unspecified statistic",
            entry.value,
            entry.unit or "[unit not reported]",
        )
        for entry in response.evidence
        if entry.value is not None
    )
    if claims != supported:
        issues.append("answer_measurements_disagree_with_evidence")
    if any(text.casefold() not in response.answer.casefold() for text in case.answer_contains):
        issues.append("missing_expected_answer_context")
    visible = response.model_dump_json(include={"answer", "evidence", "warnings", "abstentions"})
    if any(fragment in visible for fragment in case.forbidden_fragments):
        issues.append("forbidden_answer_or_evidence_fragment")
    if not set(case.expected_abstentions).issubset(abstention.code for abstention in response.abstentions):
        issues.append("missing_required_abstention")
    return issues


def run_evaluation(
    suite: EvaluationSuite,
    *,
    execute: Callable[[EvaluationCase], QueryResponse],
    verify_evidence: Callable[[EvaluationCase, QueryEvidence], tuple[str, ...]],
) -> EvaluationReport:
    """Run every case; unexpected implementation errors propagate, never become passes.

    The caller must resolve real authorized principals for execution and independently
    verify every source citation/metric against retained bytes or authorized source
    records. This runner neither grants access nor fabricates an admin principal.
    Reports contain only case IDs/counts/issue codes, never raw private responses.
    """
    suite = EvaluationSuite.model_validate(suite.model_dump(exclude_unset=True))
    started = datetime.now(UTC)
    results = []
    for case in suite.cases:
        try:
            response = execute(case)
        except DomainError as error:
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    category=case.category,
                    passed=error.code == case.expected_error,
                    issues=() if error.code == case.expected_error else ("unexpected_domain_error",),
                    evidence_count=0,
                    numeric_count=0,
                    verified_citations=0,
                    error_code=error.code,
                )
            )
            continue
        response = QueryResponse.model_validate(response.model_dump())
        issues = _response_issues(case, response)
        verified = 0
        for evidence in response.evidence:
            if case.allowed_origins and evidence.citation.origin_id not in case.allowed_origins:
                issues.append("unexpected_evidence_origin")
            if case.allowed_evidence_types and evidence.evidence_type not in case.allowed_evidence_types:
                issues.append("unexpected_evidence_type")
            if not case.request.include_internal and (evidence.origin != "public" or evidence.corroborating_citations):
                issues.append("internal_evidence_without_opt_in")
            citation_issues = verify_evidence(case, evidence)
            if not isinstance(citation_issues, tuple) or any(
                not isinstance(issue, str) or not issue for issue in citation_issues
            ):
                raise ValueError("evidence verifier must return a tuple of nonempty issue codes")
            if citation_issues:
                issues.extend(citation_issues)
            else:
                verified += 1 + len(evidence.corroborating_citations)
        results.append(
            CaseResult(
                case_id=case.case_id,
                category=case.category,
                passed=not issues,
                issues=tuple(dict.fromkeys(issues)),
                evidence_count=len(response.evidence),
                numeric_count=sum(evidence.value is not None for evidence in response.evidence),
                verified_citations=verified,
            )
        )
    passed = sum(result.passed for result in results)
    return EvaluationReport(
        suite=suite.name,
        suite_sha256=suite.digest,
        source_pins=suite.source_pins,
        started_at=started,
        finished_at=datetime.now(UTC),
        total_cases=len(results),
        passed_cases=passed,
        failed_cases=len(results) - passed,
        passed=passed == len(results),
        category_counts=dict(Counter(case.category for case in suite.cases)),
        verified_citations=sum(result.verified_citations for result in results),
        numeric_observations=sum(result.numeric_count for result in results),
        results=tuple(results),
    )


SEARCH_CATEGORIES = ("technical", "navigation", "cross_language", "paraphrase", "disambiguation", "absence")


class SearchJudgment(PublicationModel):
    case_id: str
    category: Literal["technical", "navigation", "cross_language", "paraphrase", "disambiguation", "absence"]
    split: Literal["development", "holdout"]
    question: str = Field(min_length=1)
    relevant: dict[str, int]
    target_origins: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    expect_empty: bool = False

    @model_validator(mode="after")
    def valid_judgments(self) -> Self:
        if any(not re.fullmatch("[0-9a-f]{64}", key) or not 1 <= grade <= 3 for key, grade in self.relevant.items()):
            raise ValueError("judgments need pinned evidence IDs and relevance grades 1..3")
        if self.expect_empty == bool(self.relevant) or set(self.relevant).intersection(self.forbidden):
            raise ValueError("empty and positive judgments must be distinct")
        if any(not origin.strip() for origin in self.target_origins) or len(set(self.target_origins)) != len(
            self.target_origins
        ):
            raise ValueError("target origins must be non-empty and unique")
        return self


class SearchEvaluationSuite(PublicationModel):
    contract: Literal["benchmark-rag.search-evaluation.v1", "benchmark-rag.search-evaluation.v2"]
    annotation_method: str
    scope_note: str
    manifests: dict[str, str]
    revisions: dict[str, str]
    anchors: dict[str, str]
    anchor_origins: dict[str, str] = Field(default_factory=dict)
    cases: tuple[SearchJudgment, ...]

    @model_validator(mode="after")
    def complete_suite(self) -> Self:
        counts = Counter(case.category for case in self.cases)
        if len(self.cases) < 60 or any(counts[category] < 10 for category in SEARCH_CATEGORIES):
            raise ValueError("evaluation requires ten cases in each of six categories")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case identities must be unique")
        if len({case.question for case in self.cases}) != len(self.cases):
            raise ValueError("evaluation questions must be distinct")
        for category in SEARCH_CATEGORIES:
            if {case.split for case in self.cases if case.category == category} != {"development", "holdout"}:
                raise ValueError("each category requires development and holdout queries")
        if set(self.manifests) != set(self.revisions) or not self.manifests:
            raise ValueError("every pinned manifest must have exactly one pinned source revision")
        judged_ids = {evidence_id for case in self.cases for evidence_id in (*case.relevant, *case.forbidden)}
        if not judged_ids.issubset(self.anchors):
            raise ValueError("every judged evidence ID must have a pinned source path")
        if self.contract == "benchmark-rag.search-evaluation.v2":
            self._validate_all_source_coverage()
        return self

    def _validate_all_source_coverage(self) -> None:
        if len(self.manifests) != 8:
            raise ValueError("the all-source v2 suite must pin exactly eight source scopes")
        if set(self.anchor_origins) != set(self.anchors):
            raise ValueError("v2 must pin the source scope for every evidence anchor")
        if any(origin not in self.manifests for origin in self.anchor_origins.values()):
            raise ValueError("anchor source scopes must be pinned in the manifest set")
        if len(self.cases) != 100:
            raise ValueError("the all-source v2 suite must contain exactly 100 cases")

        positive = [case for case in self.cases if not case.expect_empty]
        targeted = [case for case in positive if len(case.target_origins) == 1]
        cross_source = [case for case in positive if len(case.target_origins) > 1]
        absent = [case for case in self.cases if case.expect_empty]
        if len(targeted) != 80 or len(cross_source) != 10 or len(absent) != 10:
            raise ValueError("v2 requires 80 source-targeted, 10 cross-source, and 10 absence cases")
        if any(not case.target_origins for case in self.cases):
            raise ValueError("every v2 query must explicitly pin its target source scopes")
        if any(not set(case.target_origins).issubset(self.manifests) for case in self.cases):
            raise ValueError("query target scopes must be pinned in the manifest set")
        if any(set(case.target_origins) != set(self.manifests) for case in absent):
            raise ValueError("absence cases must test every pinned source scope")
        if any(case.category != "disambiguation" for case in cross_source):
            raise ValueError("cross-source cases must use the disambiguation category")
        if any(
            not {self.anchor_origins[evidence_id] for evidence_id in case.relevant}.issubset(case.target_origins)
            for case in positive
        ):
            raise ValueError("each relevant anchor must belong to a query's target scopes")
        if any(len({self.anchor_origins[evidence_id] for evidence_id in case.relevant}) < 2 for case in cross_source):
            raise ValueError("cross-source cases must judge relevant evidence from multiple scopes")

        for origin in self.manifests:
            source_cases = [case for case in targeted if case.target_origins == (origin,)]
            if len(source_cases) != 10 or Counter(case.category for case in source_cases) != Counter(
                {
                    "technical": 2,
                    "navigation": 2,
                    "cross_language": 2,
                    "paraphrase": 2,
                    "disambiguation": 2,
                }
            ):
                raise ValueError("each source scope needs two cases in each positive category")
            for category in ("technical", "navigation", "cross_language", "paraphrase", "disambiguation"):
                if {case.split for case in source_cases if case.category == category} != {"development", "holdout"}:
                    raise ValueError("each source/category pair needs one development and one holdout case")
            if {case.split for case in source_cases} != {"development", "holdout"}:
                raise ValueError("each source scope needs development and holdout cases")
        for selected in (cross_source, absent):
            if Counter(case.split for case in selected) != Counter({"development": 5, "holdout": 5}):
                raise ValueError("cross-source and absence cases each need five cases per split")

    @classmethod
    def load(cls, path: Path) -> SearchEvaluationSuite:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def ranking_metrics(judgment: SearchJudgment, evidence_ids: tuple[str, ...], *, k: int = 10) -> dict[str, float]:
    if k < 1:
        raise ValueError("ranking cutoff must be positive")
    unique = tuple(dict.fromkeys(evidence_ids))[:k]
    forbidden = len(set(unique).intersection(judgment.forbidden))
    if judgment.expect_empty:
        return {"empty_correct": float(not unique), "forbidden_count": float(forbidden)}
    recall = len(set(unique).intersection(judgment.relevant)) / len(judgment.relevant)
    dcg = sum(
        (2 ** judgment.relevant.get(identity, 0) - 1) / math.log2(rank + 2) for rank, identity in enumerate(unique)
    )
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(sorted(judgment.relevant.values(), reverse=True)[:k])
    )
    return {"recall_at_10": recall, "ndcg_at_10": dcg / ideal, "forbidden_count": float(forbidden)}


SearchCall = Callable[[PublicSearchRequest], Awaitable[PublicSearchResponse]]


async def evaluate_search(suite: SearchEvaluationSuite, search: SearchCall) -> dict[str, object]:
    cases = []
    per_source: dict[str, list[dict[str, float]]] = {origin: [] for origin in suite.manifests}
    absence_by_source: dict[str, list[float]] = {origin: [] for origin in suite.manifests}
    for case in suite.cases:
        target_origins = case.target_origins or tuple(suite.manifests)
        response = await search(PublicSearchRequest(question=case.question, origins=tuple(suite.manifests), limit=10))
        if response.index_stale:
            raise ValueError("evaluation cannot score a stale corpus")
        if response.degraded_reason is not None:
            raise ValueError("evaluation cannot compare a degraded retrieval branch")
        identities = tuple(dict.fromkeys(result.evidence_id for result in response.results))[:10]
        cases.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "split": case.split,
                "target_origins": target_origins,
                "returned_ids": identities,
                "relevant_count": len(case.relevant),
                "relevant_hits": len(set(identities).intersection(case.relevant)),
                **ranking_metrics(case, identities),
            }
        )
        if suite.contract == "benchmark-rag.search-evaluation.v2":
            for origin in target_origins:
                source_ids = tuple(
                    result.evidence_id for result in response.results if result.citation.origin_id == origin
                )
                if case.expect_empty:
                    absence_by_source[origin].append(float(not source_ids))
                    continue
                source_relevant = {
                    evidence_id: grade
                    for evidence_id, grade in case.relevant.items()
                    if suite.anchor_origins[evidence_id] == origin
                }
                if source_relevant:
                    per_source[origin].append(
                        {
                            **ranking_metrics(case, identities),
                            "target_source_hit": float(bool(set(source_ids).intersection(source_relevant))),
                            "development": float(case.split == "development"),
                        }
                    )
    groups = {}
    for label, selected in (
        ("all", cases),
        *((category, [case for case in cases if case["category"] == category]) for category in SEARCH_CATEGORIES),
        *((split, [case for case in cases if case["split"] == split]) for split in ("development", "holdout")),
        *(
            (
                f"{split}/{category}",
                [case for case in cases if case["split"] == split and case["category"] == category],
            )
            for split in ("development", "holdout")
            for category in SEARCH_CATEGORIES
        ),
    ):
        groups[label] = {
            metric: sum(float(case[metric]) for case in selected if metric in case)
            / sum(metric in case for case in selected)
            for metric in ("recall_at_10", "ndcg_at_10", "empty_correct", "forbidden_count")
            if any(metric in case for case in selected)
        }
    result: dict[str, object] = {
        "case_count": len(cases),
        "evaluation_track": "all_pinned_sources_global_parent_top_10",
        "judgment_status": "sparse_labels_not_independent_acceptance",
        "micro_recall_at_10": sum(case["relevant_hits"] for case in cases)
        / sum(case["relevant_count"] for case in cases),
        "groups": groups,
        "cases": cases,
    }
    if suite.contract == "benchmark-rag.search-evaluation.v2":
        source_groups = {
            origin: {
                "case_count": len(metrics),
                **{
                    metric: sum(item[metric] for item in metrics) / len(metrics)
                    for metric in ("recall_at_10", "ndcg_at_10", "forbidden_count", "target_source_hit")
                },
                "splits": {
                    split: {
                        "case_count": sum(item["development"] == float(split == "development") for item in metrics),
                        **{
                            metric: sum(
                                item[metric] for item in metrics if item["development"] == float(split == "development")
                            )
                            / sum(item["development"] == float(split == "development") for item in metrics)
                            for metric in ("recall_at_10", "ndcg_at_10", "target_source_hit")
                        },
                    }
                    for split in ("development", "holdout")
                },
            }
            for origin, metrics in per_source.items()
        }
        source_absence = {
            origin: {
                "case_count": len(values),
                "empty_correct": sum(values) / len(values) if values else 0.0,
            }
            for origin, values in absence_by_source.items()
        }
        groups["source_macro"] = {
            metric: sum(source[metric] for source in source_groups.values()) / len(source_groups)
            for metric in ("recall_at_10", "ndcg_at_10", "forbidden_count")
        }
        for split in ("development", "holdout"):
            groups[f"{split}/source_macro"] = {
                metric: sum(source["splits"][split][metric] for source in source_groups.values()) / len(source_groups)
                for metric in ("recall_at_10", "ndcg_at_10", "target_source_hit")
            }
        result["per_source"] = source_groups
        result["per_source_absence"] = source_absence
    return result


async def benchmark_search(
    requests: tuple[PublicSearchRequest, ...],
    search: SearchCall,
    *,
    concurrency: int = 10,
    samples: int = 500,
    warmup: int = 20,
) -> dict[str, float | int]:
    if not requests or concurrency < 1 or samples < 1 or warmup < 0:
        raise ValueError("benchmark requires requests and positive budgets")
    for index in range(warmup):
        await search(requests[index % len(requests)])
    semaphore = asyncio.Semaphore(concurrency)

    async def sample(index: int) -> float:
        async with semaphore:
            start = time.perf_counter()
            response = await search(requests[index % len(requests)])
            if response.index_stale or not response.results:
                raise ValueError("performance fixture must return current, verified evidence")
            return time.perf_counter() - start

    start = time.perf_counter()
    latencies = sorted(await asyncio.gather(*(sample(index) for index in range(samples))))
    elapsed = time.perf_counter() - start
    return {
        "samples": samples,
        "concurrency": concurrency,
        "warmup": warmup,
        "elapsed_seconds": elapsed,
        "p50_seconds": latencies[math.ceil(samples * 0.5) - 1],
        "p95_seconds": latencies[math.ceil(samples * 0.95) - 1],
        "max_seconds": latencies[-1],
        "queries_per_second": samples / elapsed,
    }
