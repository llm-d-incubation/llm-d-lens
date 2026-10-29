"""Opt-in, development-only retrieval diagnostics and offline reranking ablations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import resource
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, model_validator

from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.embedding import configured_encoder, located_chunk, structural_context
from llm_d_lens.benchmark_rag.evaluation import SearchEvaluationSuite, SearchJudgment, ranking_metrics
from llm_d_lens.benchmark_rag.indexing import passage_document, passage_projection_matches
from llm_d_lens.benchmark_rag.opensearch_evidence_search import (
    OpenSearchEvidenceSearchRepository,
    alias_query_text,
)
from llm_d_lens.benchmark_rag.publication_contracts import PublicationModel, content_digest
from llm_d_lens.benchmark_rag.query_enhancement import SOURCE_HINT_MULTIPLIER, SOURCE_NAMES
from llm_d_lens.benchmark_rag.query_enhancement import expand_query as expand_query
from llm_d_lens.benchmark_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
)
from llm_d_lens.benchmark_rag.service import _verify_passage_text
from llm_d_lens.benchmark_rag.sources import SourceArchive

RERANKER_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
RERANKER_FILES = {
    "config.json": "cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77",
    "model.safetensors": "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4",
    "tokenizer.json": "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
    "tokenizer_config.json": "e7fbfbfa6347b4e414c1cee50d142e2c2f9a895dad68b068ae83a8b564c3837e",
    "special_tokens_map.json": "378eb3bf733eb16e65792d7e3fda5b8a4631387ca04d2015199c4d4f22ae554d",
}
EXPERIMENT_VERSION = "development-rerank-expansion-v1"
COVERAGE_INTENT_VERSION = "development-coverage-intent-v1"
ALIASES = "/_alias/public-rag-active,public-rag-vector-shadow"
BRANCHES = ("lexical", "vector")


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class BranchPool(PublicationModel):
    lexical: tuple[str, ...]
    vector: tuple[str, ...]


class DevelopmentPool(PublicationModel):
    case: SearchJudgment
    original: BranchPool
    expanded_query: str
    added_terms: tuple[str, ...]
    source_hints: tuple[str, ...]
    enhanced: BranchPool | None = None

    @model_validator(mode="after")
    def development_positive_only(self) -> DevelopmentPool:
        if self.case.split != "development" or self.case.expect_empty:
            raise ValueError("experiments accept only development positives")
        return self


class ExperimentInputs(PublicationModel):
    contract: Literal["benchmark-rag.development-pool.v1"] = "benchmark-rag.development-pool.v1"
    version: str = EXPERIMENT_VERSION
    suite_sha256: str
    created_at: str
    aliases: dict[str, object]
    index: str
    build: PublicSearchBuild
    corpus_sha256: str
    parents: dict[str, PublicPassageDocument]
    documents: dict[str, PublicPassageDocument] = Field(default_factory=dict)
    pools: tuple[DevelopmentPool, ...]


@dataclass(frozen=True)
class Candidate:
    document: PublicPassageDocument
    score: float
    lexical_rank: int | None
    vector_rank: int | None


def fused_candidates(
    pool: BranchPool,
    documents: dict[str, PublicPassageDocument],
    *,
    depth: int,
    source_hints: tuple[str, ...] = (),
) -> tuple[Candidate, ...]:
    if not 1 <= depth <= 100:
        raise ValueError("diagnostic depth must be 1..100")
    parents: dict[str, dict[str, tuple[int, str]]] = {}
    for branch in BRANCHES:
        for rank, chunk_id in enumerate(getattr(pool, branch)[:depth], 1):
            document = documents[chunk_id]
            ranks = parents.setdefault(document.evidence_id, {})
            if branch not in ranks:
                ranks[branch] = (rank, chunk_id)
    candidates = []
    for ranks in parents.values():
        representative = min(ranks.values())[1]
        document = documents[representative]
        score = sum(1 / (60 + rank) for rank, _ in ranks.values())
        if document.origin_id in source_hints:
            score *= SOURCE_HINT_MULTIPLIER
        candidates.append(
            Candidate(
                document,
                score,
                ranks["lexical"][0] if "lexical" in ranks else None,
                ranks["vector"][0] if "vector" in ranks else None,
            )
        )
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.document.evidence_id, item.document.chunk_id)))


def select_candidates(
    candidates: Sequence[Candidate], *, limit: int = 10, per_file: int | None = 3
) -> tuple[Candidate, ...]:
    if limit < 1 or (per_file is not None and per_file < 1):
        raise ValueError("selection budgets must be positive")
    selected = []
    seen = set()
    counts: Counter[tuple[str, str]] = Counter()
    for candidate in candidates:
        document = candidate.document
        source = document.origin_id, document.source_path
        if document.evidence_id in seen or (per_file is not None and counts[source] >= per_file):
            continue
        selected.append(candidate)
        seen.add(document.evidence_id)
        counts[source] += 1
        if len(selected) == limit:
            break
    return tuple(selected)


def ids(candidates: Sequence[Candidate]) -> tuple[str, ...]:
    return tuple(item.document.evidence_id for item in candidates)


def query_intent(question: str) -> Literal["navigation", "comparison", "explanation", "uncertain"]:
    if not question.strip():
        raise ValueError("intent query must not be blank")
    navigation = (
        r"\bwhere\s+(?:in|does|can|should)\b|"
        r"\bwhich\s+(?:[\w-]+\s+){0,3}(?:section|chapter|guide|article|page|document)\b|"
        r"\b(?:locate|find)\s+(?:the|a|an)\s+(?:section|chapter|guide|article|page|document)\b|"
        r"\u54ea\u91cc|\u4f55\u5904|\u54ea(?:\u4e2a|\u7bc7|\u4efd).{0,6}"
        r"(?:\u7ae0\u8282|\u6587\u7ae0|\u6587\u6863|\u9875\u9762|\u6307\u5357)"
    )
    comparison = (
        r"\b(?:compare|comparison|versus|distinguish)\b|\bvs\.?(?=\s)|"
        r"\brather\s+than\b|\binstead\s+of\b|"
        r"\u6bd4\u8f83|\u5bf9\u6bd4|\u533a\u522b|\u800c\u4e0d\u662f"
    )
    explanation = r"\b(?:how|why|explain)\b|\u4e3a\u4ec0\u4e48|\u5982\u4f55|\u600e\u6837|\u600e\u4e48"
    if re.search(navigation, question, re.I):
        return "navigation"
    if re.search(comparison, question, re.I):
        return "comparison"
    if re.search(explanation, question, re.I):
        return "explanation"
    return "uncertain"


def coverage_candidates(
    candidates: tuple[Candidate, ...], protected: Sequence[Candidate], *, budget: int = 30
) -> tuple[Candidate, ...]:
    if not 1 <= budget <= 50:
        raise ValueError("coverage parent budget must be 1..50")
    identities = ids(candidates)
    if len(set(identities)) != len(identities):
        raise ValueError("coverage selection requires unique parent candidates")
    chosen = set(ids(protected))
    if len(chosen) > budget or not chosen <= set(identities):
        raise ValueError("protected parents must fit the budget and belong to the pool")
    branches = [
        iter(
            sorted(
                (candidate for candidate in candidates if getattr(candidate, f"{branch}_rank") is not None),
                key=lambda candidate: (
                    getattr(candidate, f"{branch}_rank"),
                    candidate.document.evidence_id,
                ),
            )
        )
        for branch in BRANCHES
    ]
    while len(chosen) < min(budget, len(candidates)):
        added = False
        for branch in branches:
            candidate = next((item for item in branch if item.document.evidence_id not in chosen), None)
            if candidate is not None:
                chosen.add(candidate.document.evidence_id)
                added = True
            if len(chosen) == budget:
                break
        if not added:
            raise EvidenceSearchError("coverage candidates lack branch ranks")
    return tuple(candidate for candidate in candidates if candidate.document.evidence_id in chosen)


def selected_first(candidates: tuple[Candidate, ...], selected: Sequence[Candidate]) -> tuple[Candidate, ...]:
    chosen = set(ids(selected))
    if len(chosen) != len(selected) or not chosen <= set(ids(candidates)):
        raise ValueError("rerank selection must contain unique members of the candidate pool")
    return tuple(selected) + tuple(
        candidate for candidate in candidates if candidate.document.evidence_id not in chosen
    )


def diagnostics(inputs: ExperimentInputs) -> dict[str, object]:
    if not inputs.pools:
        raise ValueError("candidate diagnostics require development positives")
    cases = []
    for item in inputs.pools:
        case = item.case
        depths = {}
        for depth in (50, 100):
            candidates = fused_candidates(item.original, inputs.documents, depth=depth)
            union = set(ids(candidates))
            uncapped = ids(select_candidates(candidates, per_file=None))
            capped = ids(select_candidates(candidates))
            relevant = set(case.relevant)
            depths[str(depth)] = {
                "unique_parent_candidates": len(union),
                "branch_unique_parents": {
                    branch: len({inputs.documents[key].evidence_id for key in getattr(item.original, branch)[:depth]})
                    for branch in BRANCHES
                },
                "pool_recall": len(relevant & union) / len(relevant),
                "missing_from_pool": sorted(relevant - union),
                "present_below_uncapped_top10": sorted((relevant & union) - set(uncapped)),
                "seed_hits_lost_to_file_cap": sorted((relevant & set(uncapped)) - set(capped)),
                "seed_hits_gained_from_file_cap": sorted((relevant & set(capped)) - set(uncapped)),
                "uncapped_top10": {"ids": uncapped, **ranking_metrics(case, uncapped)},
                "capped_top10": {"ids": capped, **ranking_metrics(case, capped)},
                "relevant_ranks": {
                    identity: {
                        "fused_parent_rank": next(
                            (
                                rank
                                for rank, candidate in enumerate(candidates, 1)
                                if candidate.document.evidence_id == identity
                            ),
                            None,
                        ),
                        **{
                            branch: next(
                                (
                                    rank
                                    for rank, key in enumerate(getattr(item.original, branch)[:depth], 1)
                                    if inputs.documents[key].evidence_id == identity
                                ),
                                None,
                            )
                            for branch in BRANCHES
                        },
                    }
                    for identity in case.relevant
                },
            }
        cases.append({"case_id": case.case_id, "category": case.category, "depths": depths})
    summary = {}
    for depth in ("50", "100"):
        rows = [case["depths"][depth] for case in cases]
        summary[depth] = {
            "pool_recall": sum(row["pool_recall"] for row in rows) / len(rows),
            "uncapped_recall_at_10": sum(row["uncapped_top10"]["recall_at_10"] for row in rows) / len(rows),
            "capped_recall_at_10": sum(row["capped_top10"]["recall_at_10"] for row in rows) / len(rows),
            "capped_ndcg_at_10": sum(row["capped_top10"]["ndcg_at_10"] for row in rows) / len(rows),
            "missing_seed_pairs": sum(len(row["missing_from_pool"]) for row in rows),
            "below_uncapped_top10_seed_pairs": sum(len(row["present_below_uncapped_top10"]) for row in rows),
            "seed_pairs_lost_to_file_cap": sum(len(row["seed_hits_lost_to_file_cap"]) for row in rows),
            "seed_pairs_gained_from_file_cap": sum(len(row["seed_hits_gained_from_file_cap"]) for row in rows),
        }
    return {
        "case_count": len(cases),
        "split": "development",
        "summary": summary,
        "cases": cases,
        "suite_sha256": inputs.suite_sha256,
        "corpus_sha256": inputs.corpus_sha256,
        "index": inputs.index,
        "build_id": inputs.build.build_id,
    }


def verify_chunk(
    document: PublicPassageDocument, parents: dict[str, PublicPassageDocument], build: PublicSearchBuild
) -> PublicPassageDocument:
    parent = parents.get(document.evidence_id)
    if (
        parent is None
        or build.embedding_contract is None
        or document.chunk_start is None
        or document.chunk_end is None
        or document.chunk_index is None
        or not 0 <= document.chunk_start < document.chunk_end <= len(parent.text)
    ):
        raise EvidenceSearchError("experiment candidate lacks a pinned, located parent")
    expected = located_chunk(
        parent,
        document.chunk_start,
        document.chunk_end,
        document.chunk_index,
        structural_context(parent.text, document.chunk_start),
        build.embedding_contract,
    )
    if not passage_projection_matches(document, expected, parent, exclude_embedding=True):
        raise EvidenceSearchError("experiment candidate disagrees with pinned parent bytes or source")
    return document.model_copy(update={"embedding": None})


async def capture_pool(
    question: str,
    inputs: ExperimentInputs,
    repository: OpenSearchEvidenceSearchRepository,
) -> BranchPool:
    encoder = configured_encoder()
    if encoder.contract != inputs.build.embedding_contract:
        raise EvidenceSearchError("experiment encoder does not match fixed vector build")
    vector = encoder.query(alias_query_text(question))
    lexical, dense = await asyncio.gather(
        repository.search_passages(inputs.index, question, inputs.build.scopes, limit=100),
        repository.search_vectors(
            inputs.index, vector, inputs.build.scopes, dimension=encoder.contract.dimension, limit=100
        ),
    )
    branches = {}
    for name, (hits, _) in zip(BRANCHES, (lexical, dense), strict=True):
        for hit in hits:
            checked = verify_chunk(hit.document, inputs.parents, inputs.build)
            existing = inputs.documents.get(checked.chunk_id)
            if existing is not None and existing != checked:
                raise EvidenceSearchError("experiment branches disagree on chunk identity")
            inputs.documents[checked.chunk_id] = checked
        branches[name] = tuple(hit.document.chunk_id for hit in hits)
    return BranchPool(**branches)


def load_parents(
    dao: DocumentRepository, archive: SourceArchive, suite: SearchEvaluationSuite, build: PublicSearchBuild
) -> dict[str, PublicPassageDocument]:
    if {scope.origin_id: scope.manifest_id for scope in build.scopes} != suite.manifests:
        raise EvidenceSearchError("experiment requires exactly the pinned eight-source corpus")
    if dao.public_search_scopes(tuple(suite.manifests)) != build.scopes:
        raise EvidenceSearchError("experiment publication scopes differ from the fixed build")
    parents = {}
    archive_cache: dict[str, bytes] = {}
    for scope in build.scopes:
        head = dao.head(scope.origin_id)
        if head.manifest is None or head.manifest.source_revision != suite.revisions[scope.origin_id]:
            raise EvidenceSearchError("experiment source revision changed")
        for item in dao.list_published(scope.origin_id, kind="passage"):
            if item.content_sha256 not in archive_cache:
                archive_cache[item.content_sha256] = archive.read(item.content_sha256)
            _verify_passage_text(item, archive_cache[item.content_sha256])
            parents[item.item_id] = passage_document(item, scope)
    if not parents:
        raise EvidenceSearchError("experiment corpus has no verified public parents")
    return parents


def parent_digest(parents: dict[str, PublicPassageDocument]) -> str:
    return content_digest({key: value.model_dump(mode="json") for key, value in parents.items()})


async def capture(args: argparse.Namespace) -> None:
    suite = SearchEvaluationSuite.load(args.suite)
    cases = tuple(case for case in suite.cases if case.split == "development" and not case.expect_empty)
    if len(cases) != 45:
        raise ValueError("the fixed development experiment requires exactly 45 positive cases")
    dao = DocumentRepository(args.endpoint)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            repository = OpenSearchEvidenceSearchRepository(args.endpoint, client=client, timeout_seconds=30)
            aliases = await repository._request("GET", ALIASES)
            index, build = await repository.active_build(vector=True)
            parents = load_parents(dao, SourceArchive(), suite, build)
            inputs = ExperimentInputs(
                suite_sha256=file_sha256(args.suite),
                created_at=datetime.now(UTC).isoformat(),
                aliases=aliases,
                index=index,
                build=build,
                corpus_sha256=parent_digest(parents),
                parents=parents,
                pools=(),
            )
            pools = []
            started = time.monotonic()
            for count, case in enumerate(cases, 1):
                original = await capture_pool(case.question, inputs, repository)
                expanded, terms, hints = expand_query(case.question)
                pools.append(
                    DevelopmentPool(
                        case=case, original=original, expanded_query=expanded, added_terms=terms, source_hints=hints
                    )
                )
                print(
                    f"CAPTURE {count}/{len(cases)} {case.case_id} elapsed={time.monotonic() - started:.1f}s", flush=True
                )
            inputs = inputs.model_copy(update={"pools": tuple(pools)})
            report = diagnostics(inputs)
            if args.baseline_report:
                baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
                expected = {case["case_id"]: case["returned_ids"] for case in baseline["cases"]}
                mismatches = [
                    case["case_id"]
                    for case in report["cases"]
                    if list(case["depths"]["50"]["capped_top10"]["ids"]) != expected[case["case_id"]]
                ]
                report["production_top10_replay_mismatches"] = mismatches
                if mismatches:
                    write_json(args.output / "candidate-diagnostic-failed.json", report)
                    raise EvidenceSearchError("candidate replay differs from the fixed production baseline")
            if (
                await repository._request("GET", ALIASES) != aliases
                or dao.public_search_scopes(tuple(suite.manifests)) != build.scopes
            ):
                raise EvidenceSearchError("experiment corpus changed during capture")
            write_json(args.output / "candidate-inputs.json", inputs.model_dump(mode="json"))
            write_json(args.output / "candidate-diagnostic.json", report)
            print("DIAGNOSTIC", json.dumps(report["summary"], sort_keys=True), flush=True)
    finally:
        dao.close()


def candidate_text(document: PublicPassageDocument) -> str:
    publisher = SOURCE_NAMES.get(document.origin_id, (document.origin_id, ""))[0]
    return (
        f"Source: {publisher}\nPath: {document.source_path}\n"
        f"Title: {document.title}\nSection: {' / '.join(document.heading_path)}\n{document.text}"
    )


class LocalReranker:
    def __init__(self, path: Path, pin_path: Path, *, threads: int = 2, batch_size: int = 8) -> None:
        if not 1 <= threads <= 2 or not 1 <= batch_size <= 8:
            raise ValueError("experiment reranker is limited to 2 CPU threads and batch size 8")
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
        if pin["model_id"] != RERANKER_ID or pin["revision"] != RERANKER_REVISION:
            raise EvidenceSearchError("reranker pin does not match the approved model")
        entries = {entry["path"]: entry for entry in pin["files"]}
        if not RERANKER_FILES.keys() <= entries.keys():
            raise EvidenceSearchError("reranker model pin is incomplete")
        for name, expected in RERANKER_FILES.items():
            if entries[name]["sha256"] != expected or file_sha256(path / name) != expected:
                raise EvidenceSearchError("reranker files differ from their verified fixed revision")
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        torch.set_num_threads(threads)
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(
                path, local_files_only=True, trust_remote_code=False, use_safetensors=True
            )
            .to("cpu")
            .eval()
        )
        if self.model.config.num_labels != 1:
            raise EvidenceSearchError("experiment requires scalar relevance logits")
        self.batch_size = batch_size
        self.max_tokens = min(512, self.model.config.max_position_embeddings - 2)
        self.window_count = 0
        self.multiwindow_documents = 0
        self.inference_seconds = 0.0
        self.scores: dict[str, float] = {}

    def _windows(self, question: str, text: str) -> list[dict[str, list[int]]]:
        query_ids = self.tokenizer.encode(question, add_special_tokens=False, verbose=False)
        document_ids = self.tokenizer.encode(text, add_special_tokens=False, verbose=False)
        budget = self.max_tokens - len(query_ids) - self.tokenizer.num_special_tokens_to_add(pair=True)
        if budget < 64 or not document_ids:
            raise EvidenceSearchError("reranker query or document exceeds its bounded token contract")
        step = budget - 32
        starts = [0]
        while starts[-1] + budget < len(document_ids):
            starts.append(starts[-1] + step)
        if len(starts) > 4:
            raise EvidenceSearchError("reranker document exceeds four complete overlapping windows")
        windows = [
            dict(
                self.tokenizer.prepare_for_model(
                    query_ids,
                    document_ids[start : start + budget],
                    add_special_tokens=True,
                    padding=False,
                    truncation=False,
                    return_attention_mask=True,
                    verbose=False,
                )
            )
            for start in starts
        ]
        if any(len(window["input_ids"]) > self.max_tokens for window in windows):
            raise EvidenceSearchError("reranker window exceeds the model token budget")
        if len(windows) > 1:
            self.multiwindow_documents += 1
        return windows

    def score(self, question: str, candidates: Sequence[Candidate]) -> tuple[float, ...]:
        import torch

        keys = [
            content_digest(
                {
                    "version": EXPERIMENT_VERSION,
                    "model": RERANKER_REVISION,
                    "question": question,
                    "document": candidate_text(candidate.document),
                }
            )
            for candidate in candidates
        ]
        pending: dict[str, list[dict[str, list[int]]]] = {}
        for key, candidate in zip(keys, candidates, strict=True):
            if key not in self.scores and key not in pending:
                pending[key] = self._windows(question, candidate_text(candidate.document))
        flat = [(key, window) for key, windows in pending.items() for window in windows]
        started = time.monotonic()
        window_scores: dict[str, list[float]] = {key: [] for key in pending}
        for offset in range(0, len(flat), self.batch_size):
            batch = flat[offset : offset + self.batch_size]
            encoded = self.tokenizer.pad([window for _, window in batch], padding=True, return_tensors="pt")
            with torch.inference_mode():
                logits = self.model(**encoded).logits.reshape(-1).tolist()
            if len(logits) != len(batch) or any(not math.isfinite(value) for value in logits):
                raise EvidenceSearchError("reranker returned invalid or incomplete scores")
            for (key, _), value in zip(batch, logits, strict=True):
                window_scores[key].append(value)
        for key, values in window_scores.items():
            self.scores[key] = max(values)
        self.window_count += len(flat)
        self.inference_seconds += time.monotonic() - started
        return tuple(self.scores[key] for key in keys)


def rerank(
    question: str, candidates: tuple[Candidate, ...], model: LocalReranker, *, budget: int = 30
) -> tuple[Candidate, ...]:
    if not 1 <= budget <= 50:
        raise ValueError("reranking parent budget must be 1..50")
    head = candidates[:budget]
    scored = tuple(
        replace(candidate, score=score) for candidate, score in zip(head, model.score(question, head), strict=True)
    )
    ordered = tuple(sorted(scored, key=lambda item: (-item.score, item.document.evidence_id, item.document.chunk_id)))
    return ordered + candidates[budget:]


def summarize(rows: Sequence[dict]) -> dict[str, object]:
    if not rows:
        raise ValueError("cannot summarize an empty experiment")
    return {
        "case_count": len(rows),
        "recall_at_10": sum(row["recall_at_10"] for row in rows) / len(rows),
        "ndcg_at_10": sum(row["ndcg_at_10"] for row in rows) / len(rows),
        "zero_recall_cases": sum(row["recall_at_10"] == 0 for row in rows),
    }


def coverage_intent_rankings(
    question: str,
    baseline: tuple[Candidate, ...],
    expanded: tuple[Candidate, ...],
    model: LocalReranker,
) -> tuple[dict[str, tuple[Candidate, ...]], tuple[Candidate, ...], str]:
    protected = select_candidates(baseline)
    selected = coverage_candidates(expanded, protected)
    intent = query_intent(question)
    prefix_reranked = rerank(question, expanded, model)
    coverage_reranked = rerank(question, selected_first(expanded, selected), model)
    enabled = intent in {"comparison", "explanation"}
    return (
        {
            "enhancement_baseline": baseline,
            "depth100_only": expanded,
            "prefix30_rerank": prefix_reranked,
            "coverage30_rerank": coverage_reranked,
            "prefix30_intent": prefix_reranked if enabled else baseline,
            "coverage30_intent": coverage_reranked if enabled else baseline,
        },
        selected,
        intent,
    )


def load_coverage_inputs(
    input_path: Path, reference_path: Path, suite_path: Path, pin_path: Path
) -> tuple[ExperimentInputs, SearchEvaluationSuite, dict]:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    inputs = ExperimentInputs.model_validate_json(input_path.read_text(encoding="utf-8"))
    suite = SearchEvaluationSuite.load(suite_path)
    expected_cases = tuple(case for case in suite.cases if case.split == "development" and not case.expect_empty)
    if (
        reference["status"] != "complete"
        or reference["version"] != EXPERIMENT_VERSION
        or inputs.version != EXPERIMENT_VERSION
        or file_sha256(input_path) != reference["enhanced_candidate_inputs_sha256"]
        or file_sha256(suite_path) != inputs.suite_sha256
        or inputs.suite_sha256 != reference["suite_sha256"]
        or parent_digest(inputs.parents) != inputs.corpus_sha256
        or inputs.corpus_sha256 != reference["corpus_sha256"]
        or file_sha256(pin_path) != reference["reranker_model_pin_sha256"]
        or tuple(item.case for item in inputs.pools) != expected_cases
        or len(expected_cases) != 45
    ):
        raise EvidenceSearchError("coverage experiment inputs differ from the pinned development baseline")
    for key, document in inputs.documents.items():
        if key != document.chunk_id:
            raise EvidenceSearchError("coverage snapshot chunk identity changed")
        verify_chunk(document, inputs.parents, inputs.build)
    for item in inputs.pools:
        if (
            item.enhanced is None
            or expand_query(item.case.question) != (item.expanded_query, item.added_terms, item.source_hints)
            or any(
                key not in inputs.documents
                for pool in (item.original, item.enhanced)
                for branch in BRANCHES
                for key in getattr(pool, branch)
            )
        ):
            raise EvidenceSearchError("coverage experiment requires complete, unchanged enhanced pools")
    return inputs, suite, reference


async def coverage_intent_experiment(args: argparse.Namespace) -> None:
    inputs, suite, reference = load_coverage_inputs(args.inputs, args.reference_report, args.suite, args.model_pin)
    report_path = args.output / "coverage-intent-experiment.json"
    progress_path = args.output / "coverage-intent-progress.json"
    score_path = args.output / "coverage-intent-scores.json"
    if any(path.exists() for path in (report_path, progress_path, score_path)):
        raise EvidenceSearchError("coverage experiment output already exists; choose a fresh output directory")
    reference_rows = {row["case_id"]: row for row in reference["variants"]["expansion_only"]}
    if set(reference_rows) != {item.case.case_id for item in inputs.pools}:
        raise EvidenceSearchError("reference report lacks the exact development baseline")
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    variants: dict[str, list[dict]] = {}
    pools = []
    dao = DocumentRepository(args.endpoint)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            repository = OpenSearchEvidenceSearchRepository(args.endpoint, client=client, timeout_seconds=30)
            if (
                await repository._request("GET", ALIASES) != inputs.aliases
                or parent_digest(load_parents(dao, SourceArchive(), suite, inputs.build)) != inputs.corpus_sha256
            ):
                raise EvidenceSearchError("coverage experiment live corpus changed since capture")
            model_started = time.monotonic()
            model = LocalReranker(args.model_path, args.model_pin)
            model_load_seconds = time.monotonic() - model_started
            for count, item in enumerate(inputs.pools, 1):
                if item.enhanced is None:
                    raise EvidenceSearchError("coverage experiment requires enhanced pools")
                baseline = fused_candidates(item.enhanced, inputs.documents, depth=50, source_hints=item.source_hints)
                expanded = fused_candidates(item.enhanced, inputs.documents, depth=100, source_hints=item.source_hints)
                baseline_ids = ids(select_candidates(baseline))
                recorded = reference_rows[item.case.case_id]
                metrics = ranking_metrics(item.case, baseline_ids)
                if list(baseline_ids) != recorded["returned_ids"] or any(
                    metrics[key] != recorded[key] for key in ("recall_at_10", "ndcg_at_10")
                ):
                    raise EvidenceSearchError("enhancement baseline no longer replays exactly")
                windows_before = model.window_count
                inference_before = model.inference_seconds
                ordered, selected, intent = coverage_intent_rankings(item.case.question, baseline, expanded, model)
                relevant = set(item.case.relevant)
                selections = {
                    "depth50_union": baseline,
                    "depth100_union": expanded,
                    "prefix30": expanded[:30],
                    "coverage30": selected,
                }
                pools.append(
                    {
                        "case_id": item.case.case_id,
                        "query_intent": intent,
                        "rerank_enabled_by_route": intent in {"comparison", "explanation"},
                        "selections": {
                            name: {
                                "ids": ids(candidates),
                                "parent_count": len(candidates),
                                "seed_coverage": len(relevant & set(ids(candidates))) / len(relevant),
                                "missing_seed_ids": sorted(relevant - set(ids(candidates))),
                            }
                            for name, candidates in selections.items()
                        },
                        "inference_seconds_for_all_arms": model.inference_seconds - inference_before,
                        "new_windows_for_all_arms": model.window_count - windows_before,
                    }
                )
                for name, candidates in ordered.items():
                    selected_ids = ids(select_candidates(candidates))
                    variants.setdefault(name, []).append(
                        {
                            "case_id": item.case.case_id,
                            "category": item.case.category,
                            "query_intent": intent,
                            "rerank_applied": name.endswith("_rerank")
                            or (name.endswith("_intent") and intent in {"comparison", "explanation"}),
                            "returned_ids": selected_ids,
                            **ranking_metrics(item.case, selected_ids),
                        }
                    )
                write_json(
                    progress_path,
                    {
                        "status": "incomplete",
                        "version": COVERAGE_INTENT_VERSION,
                        "completed_cases": count,
                        "total_cases": len(inputs.pools),
                        "variants": variants,
                        "candidate_selections": pools,
                    },
                )
                write_json(
                    score_path,
                    {
                        "version": COVERAGE_INTENT_VERSION,
                        "score_key_version": EXPERIMENT_VERSION,
                        "model_revision": RERANKER_REVISION,
                        "model_pin_sha256": file_sha256(args.model_pin),
                        "scores": model.scores,
                    },
                )
                print(
                    f"COVERAGE_INTENT {count}/{len(inputs.pools)} {item.case.case_id} "
                    f"intent={intent} elapsed={time.monotonic() - started:.1f}s windows={model.window_count}",
                    flush=True,
                )
            if (
                await repository._request("GET", ALIASES) != inputs.aliases
                or parent_digest(load_parents(dao, SourceArchive(), suite, inputs.build)) != inputs.corpus_sha256
            ):
                raise EvidenceSearchError("coverage experiment corpus changed during ablations")
            summaries = {}
            for name, rows in variants.items():
                comparisons = [(row, old) for row, old in zip(rows, variants["enhancement_baseline"], strict=True)]
                summaries[name] = {
                    **summarize(rows),
                    "categories": {
                        category: summarize([row for row in rows if row["category"] == category])
                        for category in sorted({row["category"] for row in rows})
                    },
                    "routes": {
                        route: summarize([row for row in rows if row["query_intent"] == route])
                        for route in sorted({row["query_intent"] for row in rows})
                    },
                    "recall_improved_cases": [
                        row["case_id"] for row, old in comparisons if row["recall_at_10"] > old["recall_at_10"]
                    ],
                    "recall_regressed_cases": [
                        row["case_id"] for row, old in comparisons if row["recall_at_10"] < old["recall_at_10"]
                    ],
                    "ndcg_regressed_cases": [
                        row["case_id"] for row, old in comparisons if row["ndcg_at_10"] < old["ndcg_at_10"]
                    ],
                    "reranked_cases": sum(row["rerank_applied"] for row in rows),
                }
            report = {
                "contract": "benchmark-rag.coverage-intent-experiment.v1",
                "version": COVERAGE_INTENT_VERSION,
                "status": "complete",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "split": "development",
                "positive_cases": len(inputs.pools),
                "holdout_evaluated": False,
                "absence_evaluated": False,
                "launch_approved": False,
                "metric_status": "sparse seed-label development ablation, not acceptance",
                "suite_sha256": inputs.suite_sha256,
                "corpus_sha256": inputs.corpus_sha256,
                "enhanced_candidate_inputs_sha256": file_sha256(args.inputs),
                "reference_report_sha256": file_sha256(args.reference_report),
                "reranker_model_pin_sha256": file_sha256(args.model_pin),
                "score_cache_sha256": file_sha256(score_path),
                "experiment_code_sha256": file_sha256(Path(__file__)),
                "query_enhancement_code_sha256": file_sha256(Path(__file__).with_name("query_enhancement.py")),
                "fixed_settings": {
                    "baseline_branch_depth": 50,
                    "expanded_branch_depth": 100,
                    "rrf_constant": 60,
                    "source_hint_multiplier": SOURCE_HINT_MULTIPLIER,
                    "rerank_parent_budget": 30,
                    "coverage_selection": (
                        "protect baseline final Top-10, alternate lexical/vector unique parents, restore fused order"
                    ),
                    "intent_policy": (
                        "query text only; navigation first; rerank comparison/explanation; uncertain preserves baseline"
                    ),
                    "per_file_limit": 3,
                    "final_parent_limit": 10,
                    "reranker_query": "original question",
                    "window_aggregation": "max over complete overlapping windows; no silent truncation",
                },
                "baseline_exact_replays": len(inputs.pools),
                "candidate_coverage": {
                    name: sum(row["selections"][name]["seed_coverage"] for row in pools) / len(pools)
                    for name in ("depth50_union", "depth100_union", "prefix30", "coverage30")
                },
                "routing_counts": dict(Counter(row["query_intent"] for row in pools)),
                "summary": summaries,
                "variants": variants,
                "candidate_selections": pools,
                "resources": {
                    "elapsed_seconds": time.monotonic() - started,
                    "reranker_load_seconds": model_load_seconds,
                    "reranker_inference_seconds": model.inference_seconds,
                    "reranker_windows": model.window_count,
                    "scored_unique_pairs": len(model.scores),
                    "process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                    "cpu_threads": 2,
                    "batch_size": 8,
                    "not_end_to_end_latency_acceptance": True,
                    "all_rerank_arms_scored": True,
                },
            }
            write_json(report_path, report)
            write_json(
                progress_path,
                {
                    "status": "complete",
                    "version": COVERAGE_INTENT_VERSION,
                    "completed_cases": len(inputs.pools),
                    "total_cases": len(inputs.pools),
                    "report": report_path.name,
                },
            )
            print("COVERAGE_SUMMARY", json.dumps(report["candidate_coverage"], sort_keys=True), flush=True)
            print("ABLATIONS", json.dumps({name: summarize(rows) for name, rows in variants.items()}), flush=True)
    finally:
        dao.close()


async def experiment(args: argparse.Namespace) -> None:
    input_path = args.output / "candidate-inputs.json"
    inputs = ExperimentInputs.model_validate_json(input_path.read_text(encoding="utf-8"))
    if (
        inputs.version != EXPERIMENT_VERSION
        or inputs.suite_sha256 != file_sha256(args.suite)
        or inputs.corpus_sha256 != parent_digest(inputs.parents)
    ):
        raise EvidenceSearchError("experiment cached inputs changed")
    suite = SearchEvaluationSuite.load(args.suite)
    expected_cases = tuple(case for case in suite.cases if case.split == "development" and not case.expect_empty)
    if tuple(item.case for item in inputs.pools) != expected_cases or len(expected_cases) != 45:
        raise EvidenceSearchError("experiment cache does not contain exactly the fixed development positives")
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    model_started = time.monotonic()
    model = LocalReranker(args.model_path, args.model_pin)
    model_load_seconds = time.monotonic() - model_started
    dao = DocumentRepository(args.endpoint)
    variants: dict[str, list[dict]] = {name: [] for name in ("baseline", "rerank_only", "expansion_only", "combined")}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            repository = OpenSearchEvidenceSearchRepository(args.endpoint, client=client, timeout_seconds=30)
            if (
                await repository._request("GET", ALIASES) != inputs.aliases
                or parent_digest(load_parents(dao, SourceArchive(), suite, inputs.build)) != inputs.corpus_sha256
            ):
                raise EvidenceSearchError("experiment live corpus changed since diagnosis")
            enhanced_pools = []
            for count, item in enumerate(inputs.pools, 1):
                original = fused_candidates(item.original, inputs.documents, depth=50)
                enhanced_pool = (
                    await capture_pool(item.expanded_query, inputs, repository)
                    if item.expanded_query != item.case.question
                    else item.original
                )
                enhanced = fused_candidates(enhanced_pool, inputs.documents, depth=50, source_hints=item.source_hints)
                enhanced_pools.append(item.model_copy(update={"enhanced": enhanced_pool}))
                ordered = {
                    "baseline": original,
                    "rerank_only": rerank(item.case.question, original, model),
                    "expansion_only": enhanced,
                    "combined": rerank(item.case.question, enhanced, model),
                }
                for name, candidates in ordered.items():
                    selected = select_candidates(candidates)
                    variants[name].append(
                        {
                            "case_id": item.case.case_id,
                            "category": item.case.category,
                            "returned_ids": ids(selected),
                            "candidate_parents": len(candidates),
                            "rerank_pool_recall": len(set(ids(candidates[:30])) & set(item.case.relevant))
                            / len(item.case.relevant),
                            **ranking_metrics(item.case, ids(selected)),
                        }
                    )
                write_json(
                    args.output / "experiment-progress.json",
                    {
                        "status": "incomplete",
                        "completed_cases": count,
                        "total_cases": len(inputs.pools),
                        "variants": variants,
                    },
                )
                write_json(
                    args.output / "reranker-score-cache.json",
                    {
                        "version": EXPERIMENT_VERSION,
                        "model_revision": RERANKER_REVISION,
                        "scores": model.scores,
                    },
                )
                print(
                    f"EXPERIMENT {count}/{len(inputs.pools)} {item.case.case_id} "
                    f"elapsed={time.monotonic() - started:.1f}s windows={model.window_count}",
                    flush=True,
                )
            if (
                await repository._request("GET", ALIASES) != inputs.aliases
                or dao.public_search_scopes(tuple(suite.manifests)) != inputs.build.scopes
            ):
                raise EvidenceSearchError("experiment corpus changed during ablations")
            enhanced_path = args.output / "enhanced-candidate-inputs.json"
            write_json(
                enhanced_path, inputs.model_copy(update={"pools": tuple(enhanced_pools)}).model_dump(mode="json")
            )
            summary = {
                name: {
                    **summarize(rows),
                    "categories": {
                        category: summarize([row for row in rows if row["category"] == category])
                        for category in sorted({row["category"] for row in rows})
                    },
                }
                for name, rows in variants.items()
            }
            report = {
                "contract": "benchmark-rag.development-experiment.v1",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "complete",
                "version": EXPERIMENT_VERSION,
                "split": "development",
                "positive_cases": 45,
                "holdout_evaluated": False,
                "absence_evaluated": False,
                "launch_approved": False,
                "metric_status": "sparse seed-label development ablation, not acceptance",
                "suite_sha256": inputs.suite_sha256,
                "corpus_sha256": inputs.corpus_sha256,
                "candidate_inputs_sha256": file_sha256(input_path),
                "enhanced_candidate_inputs_sha256": file_sha256(enhanced_path),
                "reranker_model_pin_sha256": file_sha256(args.model_pin),
                "experiment_code_sha256": file_sha256(Path(__file__)),
                "query_enhancement_code_sha256": file_sha256(Path(__file__).with_name("query_enhancement.py")),
                "fixed_settings": {
                    "branch_depth": 50,
                    "diagnostic_depths": [50, 100],
                    "rrf_constant": 60,
                    "source_hint_multiplier": SOURCE_HINT_MULTIPLIER,
                    "rerank_parent_budget": 30,
                    "per_file_limit": 3,
                    "final_parent_limit": 10,
                    "reranker_query": "original question",
                    "window_aggregation": "max over complete overlapping windows; no silent truncation",
                },
                "query_enhancement": [
                    {
                        "case_id": item.case.case_id,
                        "question": item.case.question,
                        "expanded_query": item.expanded_query,
                        "terms": item.added_terms,
                        "source_hints": item.source_hints,
                    }
                    for item in inputs.pools
                ],
                "summary": summary,
                "variants": variants,
                "resources": {
                    "elapsed_seconds": time.monotonic() - started,
                    "reranker_load_seconds": model_load_seconds,
                    "reranker_inference_seconds": model.inference_seconds,
                    "reranker_windows": model.window_count,
                    "multiwindow_documents": model.multiwindow_documents,
                    "scored_unique_pairs": len(model.scores),
                    "process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                    "cpu_threads": 2,
                    "batch_size": 8,
                    "reranker_max_tokens": model.max_tokens,
                    "not_end_to_end_latency_acceptance": True,
                },
            }
            write_json(args.output / "development-experiment.json", report)
            write_json(
                args.output / "experiment-progress.json",
                {
                    "status": "complete",
                    "completed_cases": len(inputs.pools),
                    "total_cases": len(inputs.pools),
                    "report": "development-experiment.json",
                },
            )
            print("ABLATIONS", json.dumps(summary, sort_keys=True), flush=True)
    finally:
        dao.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("diagnose", "experiment", "coverage-intent"), required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9200")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--model-pin", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--reference-report", type=Path)
    args = parser.parse_args()
    if args.stage != "diagnose" and (args.model_path is None or args.model_pin is None):
        parser.error("experiment stages require --model-path and --model-pin")
    if args.stage == "coverage-intent" and (args.inputs is None or args.reference_report is None):
        parser.error("coverage-intent requires --inputs and --reference-report")
    stage = {"diagnose": capture, "experiment": experiment, "coverage-intent": coverage_intent_experiment}
    asyncio.run(stage[args.stage](args))


if __name__ == "__main__":
    main()
