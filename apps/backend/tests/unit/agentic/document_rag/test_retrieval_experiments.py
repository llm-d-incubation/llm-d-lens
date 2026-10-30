"""Development experiments must preserve provenance, budgets and split isolation."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from llm_d_lens.agentic.document_rag.embedding import CONTRACT, located_chunk
from llm_d_lens.agentic.document_rag.evaluation import SearchJudgment
from llm_d_lens.agentic.document_rag.query_contracts import Citation
from llm_d_lens.agentic.document_rag.retrieval_contracts import DocumentMatch
from llm_d_lens.agentic.document_rag.retrieval_experiments import (
    RERANKER_ID,
    RERANKER_REVISION,
    BranchPool,
    Candidate,
    DevelopmentPool,
    ExperimentInputs,
    LocalReranker,
    coverage_candidates,
    coverage_intent_rankings,
    diagnostics,
    expand_query,
    fused_candidates,
    ids,
    load_coverage_inputs,
    query_intent,
    rerank,
    select_candidates,
    selected_first,
    verify_chunk,
)
from llm_d_lens.agentic.document_rag.search import PublicRagSearchService
from llm_d_lens.agentic.document_rag.search_contracts import (
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchHit,
    PublicSearchResult,
    PublicSearchScope,
)

ORIGIN = "https://aws.amazon.com"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def parent(name="one", path="article.md"):
    return PublicPassageDocument(
        chunk_id=digest(name),
        evidence_id=digest(name),
        origin_id=ORIGIN,
        manifest_id=digest("manifest"),
        publication_generation=1,
        source_revision=digest("revision"),
        content_sha256=digest("body"),
        source_path=path,
        title="Mechanism",
        heading_path=("Title", "Mechanism"),
        text="Direct evidence for a technical mechanism.",
    )


def chunk(name="one", path="article.md"):
    document = parent(name, path)
    return located_chunk(document, 0, len(document.text), 0, "", CONTRACT)


def judgment(**updates):
    return SearchJudgment(
        case_id="development-one",
        category="technical",
        split="development",
        question="What mechanism does AWS describe?",
        relevant={digest("one"): 3},
    ).model_copy(update=updates)


def test_query_enhancement_preserves_question_identifiers_and_explicit_source():
    question = "AWS \u591a\u526f\u672c\u8def\u7531\u5982\u4f55\u5904\u7406 KV-cache \u548c affinityThreshold?"
    expanded, terms, hints = expand_query(question)
    assert expanded.startswith(question)
    assert {"replica", "routing"}.issubset(terms)
    assert hints == (ORIGIN,)
    assert expand_query(question) == (expanded, terms, hints)


def test_source_hints_do_not_infer_publisher_from_topic_or_case_labels():
    assert expand_query("Explain KServe, KV-cache capacity and H100.")[2] == ()
    assert expand_query("Compare AWS and IBM Research routing.")[2] == (
        ORIGIN,
        "https://research.ibm.com",
    )
    with pytest.raises(ValueError, match="blank"):
        expand_query(" ")


@pytest.mark.parametrize(
    "case",
    [
        judgment(split="holdout"),
        judgment(relevant={}, expect_empty=True),
    ],
)
def test_non_development_or_absence_cases_cannot_enter_pool(case):
    with pytest.raises(ValidationError, match="development positives"):
        DevelopmentPool(
            case=case,
            original=BranchPool(lexical=(), vector=()),
            expanded_query=case.question,
            added_terms=(),
            source_hints=(),
        )


def test_parent_fusion_keeps_raw_branch_rank_and_deduplicates_children():
    one, two = chunk(), chunk("two", "other.md")
    sibling = located_chunk(parent(), 0, 10, 1, "", CONTRACT)
    documents = {doc.chunk_id: doc for doc in (one, sibling, two)}
    pool = BranchPool(lexical=(one.chunk_id, sibling.chunk_id, two.chunk_id), vector=(two.chunk_id,))
    ranked = fused_candidates(pool, documents, depth=50)
    assert ids(ranked) == (two.evidence_id, one.evidence_id)
    assert ranked[0].lexical_rank == 3 and ranked[0].vector_rank == 1
    assert ranked[1].score == pytest.approx(1 / 61)


@pytest.mark.parametrize("source_hints", [(), (ORIGIN,)])
def test_fusion_and_cap_replay_production_fuser_exactly(source_hints):
    documents = {
        doc.chunk_id: doc.model_copy(update={"origin_id": ORIGIN if i % 2 else "https://research.ibm.com"})
        for i, doc in enumerate(chunk(str(i), f"file-{i // 5}.md") for i in range(12))
    }
    keys = tuple(documents)
    pool = BranchPool(lexical=keys, vector=tuple(reversed(keys)))
    branches = {
        branch: [PublicSearchHit(document=documents[key], score=1) for key in getattr(pool, branch)]
        for branch in ("lexical", "vector")
    }
    resolved = {}
    for key, document in documents.items():
        result = PublicSearchResult(
            evidence_id=document.evidence_id,
            chunk_id=key,
            title=document.title,
            heading_path=document.heading_path,
            text=document.text,
            score=1,
            citation=Citation(
                origin_id=document.origin_id,
                source_revision=document.source_revision,
                source_path=document.source_path,
                sha256=document.content_sha256,
                artifact_uri="lens-artifact://test",
                source_pointer={},
                published_manifest_id=document.manifest_id,
            ),
        )
        resolved[key] = (result, Mock(spec=DocumentMatch))
    expected, _, _ = PublicRagSearchService._fuse(branches, resolved, set(), 10, False, source_hints=source_hints)
    actual = select_candidates(fused_candidates(pool, documents, depth=50, source_hints=source_hints))
    assert [(item.document.evidence_id, item.document.chunk_id, item.score) for item in actual] == [
        (item.evidence_id, item.chunk_id, item.score) for item in expected
    ]


def test_file_cap_is_applied_after_ordering_and_can_be_diagnosed_without_it():
    candidates = tuple(Candidate(chunk(str(i)), 10 - i, i + 1, None) for i in range(5))
    assert len(select_candidates(candidates)) == 3
    assert len(select_candidates(candidates, per_file=None)) == 5
    with pytest.raises(ValueError, match="positive"):
        select_candidates(candidates, per_file=0)


def test_source_hint_is_a_soft_boost_and_does_not_remove_competitors():
    aws = chunk()
    other = chunk("two").model_copy(update={"origin_id": "https://research.ibm.com"})
    documents = {doc.chunk_id: doc for doc in (aws, other)}
    pool = BranchPool(lexical=(other.chunk_id, aws.chunk_id), vector=())
    ranked = fused_candidates(pool, documents, depth=50, source_hints=(ORIGIN,))
    assert set(ids(ranked)) == {aws.evidence_id, other.evidence_id}
    assert ranked[0].document.evidence_id == aws.evidence_id


def test_chunk_verification_rejects_text_offsets_and_provenance_mismatches():
    document = chunk()
    build = PublicSearchBuild(
        build_id="test",
        scopes=(
            PublicSearchScope(
                origin_id=ORIGIN,
                manifest_id=digest("manifest"),
                generation=1,
            ),
        ),
        document_count=1,
        retrieval_mode="opensearch_vector",
        embedding_contract=CONTRACT,
    )
    parents = {document.evidence_id: parent()}
    assert verify_chunk(document, parents, build) == document
    for changes in ({"text": "corrupted"}, {"source_revision": digest("wrong")}, {"chunk_end": 9999}):
        with pytest.raises(EvidenceSearchError, match="pinned|disagrees"):
            verify_chunk(document.model_copy(update=changes), parents, build)


def test_rerank_scores_only_bounded_candidate_text_not_labels_and_preserves_tail():
    candidates = tuple(Candidate(chunk(str(i), f"{i}.md"), 1 / (61 + i), i + 1, None) for i in range(5))
    model = Mock(spec=LocalReranker)
    model.score.return_value = (-1, 5, 2)
    result = rerank("original question", candidates, model, budget=3)
    model.score.assert_called_once_with("original question", candidates[:3])
    assert ids(result) == (
        candidates[1].document.evidence_id,
        candidates[2].document.evidence_id,
        candidates[0].document.evidence_id,
        *ids(candidates[3:]),
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Which section explains how the cache index is built?", "navigation"),
        ("Where in the guide can I compare routing strategies?", "navigation"),
        ("Which developer-guide section covers dry-run?", "navigation"),
        ("\u54ea\u4e2a\u7ae0\u8282\u89e3\u91ca\u5982\u4f55\u914d\u7f6e\uff1f", "navigation"),
        ("Compare prefill and decode resource allocation.", "comparison"),
        ("Why use A rather than B?", "comparison"),
        ("\u6bd4\u8f83\u4e24\u79cd\u90e8\u7f72\u7684\u533a\u522b", "comparison"),
        ("How does cache-aware routing work?", "explanation"),
        ("\u4e3a\u4ec0\u4e48\u4f7f\u7528\u591a\u526f\u672c\uff1f", "explanation"),
        ("Which transport library is named?", "uncertain"),
        ("AWS affinityThreshold", "uncertain"),
    ],
)
def test_query_only_intent_routing_is_conservative_and_navigation_has_priority(question, expected):
    assert query_intent(question) == expected


def test_blank_intent_is_rejected():
    with pytest.raises(ValueError, match="blank"):
        query_intent(" ")


def test_coverage_selection_protects_baseline_and_adds_branch_specific_candidates():
    candidates = tuple(
        Candidate(
            chunk(str(i), f"{i}.md"),
            1 / (61 + i),
            i + 1 if i < 10 else None,
            i - 9 if i >= 10 else None,
        )
        for i in range(20)
    )
    selected = coverage_candidates(candidates, candidates[:2], budget=6)
    assert ids(selected) == ids(tuple(candidates[i] for i in (0, 1, 2, 3, 10, 11)))
    assert set(ids(candidates[:2])) <= set(ids(selected))
    assert len(coverage_candidates(candidates[:4], (), budget=30)) == 4
    with pytest.raises(ValueError, match="budget"):
        coverage_candidates(candidates, (), budget=0)
    with pytest.raises(ValueError, match="protected"):
        coverage_candidates(candidates, candidates[:3], budget=2)
    with pytest.raises(ValueError, match="belong"):
        coverage_candidates(candidates, (Candidate(chunk("missing"), 1, 1, None),))
    with pytest.raises(ValueError, match="unique"):
        coverage_candidates(candidates + candidates[:1], ())
    with pytest.raises(EvidenceSearchError, match="branch ranks"):
        coverage_candidates((Candidate(chunk(), 1, None, None),), ())


def test_coverage_anchors_survive_top100_rank_changes_and_same_file_is_not_preemptively_removed():
    candidates = tuple(Candidate(chunk(str(i)), 1 / (61 + i), i + 1, 40 - i) for i in range(40))
    protected = (candidates[39],)
    selected = coverage_candidates(candidates, protected)
    assert len(selected) == 30
    assert candidates[39] in selected
    assert len(set(ids(selected))) == 30
    ordered = selected_first(candidates, selected)
    assert ordered[:30] == selected
    assert len(ordered) == len(candidates)
    assert set(ids(ordered)) == set(ids(candidates))
    with pytest.raises(ValueError, match="unique"):
        selected_first(candidates, selected + selected[:1])


@pytest.mark.parametrize("question", ["Where in the guide is the cache described?", "cache transport library"])
def test_navigation_and_uncertain_routes_preserve_enhancement_baseline_exactly(question):
    candidates = tuple(Candidate(chunk(str(i), f"{i}.md"), 1 / (61 + i), i + 1, 40 - i) for i in range(40))
    baseline = tuple(reversed(candidates))
    model = Mock(spec=LocalReranker)
    model.score.side_effect = lambda question, head: tuple(float(i) for i in range(len(head)))
    variants, selected, _ = coverage_intent_rankings(question, baseline, candidates, model)
    assert variants["enhancement_baseline"] is baseline
    assert variants["prefix30_intent"] is baseline
    assert variants["coverage30_intent"] is baseline
    assert len(selected) == 30
    assert set(ids(select_candidates(baseline))) <= set(ids(selected))
    assert model.score.call_count == 2  # Unconditional controls still need their own scores.


def test_explanation_route_uses_same_pool_as_matching_unconditional_control():
    candidates = tuple(Candidate(chunk(str(i), f"{i}.md"), 1 / (61 + i), i + 1, 40 - i) for i in range(40))
    model = Mock(spec=LocalReranker)
    model.score.side_effect = lambda question, head: tuple(float(i) for i in range(len(head)))
    variants, selected, intent = coverage_intent_rankings("How does cache routing work?", candidates, candidates, model)
    assert intent == "explanation"
    assert variants["prefix30_intent"] == variants["prefix30_rerank"]
    assert variants["coverage30_intent"] == variants["coverage30_rerank"]
    assert len(variants) == 6
    assert len(selected) == 30
    assert all(len(call.args[1]) == 30 for call in model.score.call_args_list)
    assert all(call.args[0] == "How does cache routing work?" for call in model.score.call_args_list)


def test_coverage_snapshot_hash_must_match_previous_report_before_scoring(tmp_path):
    document = chunk()
    build = PublicSearchBuild(
        build_id="test",
        scopes=(PublicSearchScope(origin_id=ORIGIN, manifest_id=digest("manifest"), generation=1),),
        document_count=1,
        retrieval_mode="opensearch_vector",
        embedding_contract=CONTRACT,
    )
    inputs = ExperimentInputs(
        suite_sha256=digest("suite"),
        created_at="test",
        aliases={},
        index="test",
        build=build,
        corpus_sha256=digest("corpus"),
        parents={document.evidence_id: parent()},
        documents={document.chunk_id: document},
        pools=(),
    )
    input_path, reference_path = tmp_path / "input.json", tmp_path / "reference.json"
    input_path.write_text(inputs.model_dump_json())
    reference_path.write_text(
        json.dumps(
            {
                "status": "complete",
                "version": inputs.version,
                "enhanced_candidate_inputs_sha256": digest("tampered"),
            }
        )
    )
    with pytest.raises(EvidenceSearchError, match="pinned development baseline"):
        load_coverage_inputs(
            input_path,
            reference_path,
            Path(__file__).parents[3] / "fixtures/public_rag/search_acceptance_v2.json",
            tmp_path / "pin.json",
        )


def test_reranker_requires_approved_pin_before_loading_weights(tmp_path):
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"model_id": "wrong", "revision": "wrong"}))
    with pytest.raises(EvidenceSearchError, match="approved"):
        LocalReranker(tmp_path, pin)
    with pytest.raises(ValueError, match="limited"):
        LocalReranker(tmp_path, pin, threads=3)
    pin.write_text(
        json.dumps(
            {
                "model_id": RERANKER_ID,
                "revision": RERANKER_REVISION,
                "files": [],
            }
        )
    )
    with pytest.raises(EvidenceSearchError, match="incomplete"):
        LocalReranker(tmp_path, pin)


def test_diagnostics_distinguish_candidate_missing_and_file_cap_losses():
    documents = {doc.chunk_id: doc for doc in (chunk(str(i)) for i in range(5))}
    keys = tuple(documents)
    selected_case = judgment(relevant={documents[keys[4]].evidence_id: 3, digest("missing"): 3})
    build = PublicSearchBuild(
        build_id="test",
        scopes=(
            PublicSearchScope(
                origin_id=ORIGIN,
                manifest_id=digest("manifest"),
                generation=1,
            ),
        ),
        document_count=5,
        retrieval_mode="opensearch_vector",
        embedding_contract=CONTRACT,
    )
    inputs = ExperimentInputs(
        suite_sha256=digest("suite"),
        created_at="test",
        aliases={},
        index="test",
        build=build,
        corpus_sha256=digest("corpus"),
        parents={},
        documents=documents,
        pools=(
            DevelopmentPool(
                case=selected_case,
                original=BranchPool(lexical=keys, vector=()),
                expanded_query=selected_case.question,
                added_terms=(),
                source_hints=(),
            ),
        ),
    )
    result = diagnostics(inputs)
    assert result["summary"]["50"]["pool_recall"] == 0.5
    assert result["summary"]["50"]["uncapped_recall_at_10"] == 0.5
    assert result["summary"]["50"]["capped_recall_at_10"] == 0
    assert result["summary"]["50"]["missing_seed_pairs"] == 1
    assert result["summary"]["50"]["seed_pairs_lost_to_file_cap"] == 1


def test_reranker_windows_cover_full_document_without_silent_truncation():
    model = LocalReranker.__new__(LocalReranker)
    tokenizer = Mock()
    tokenizer.encode.side_effect = lambda text, **kwargs: [1, 2] if text == "query" else list(range(1000, 1200))
    tokenizer.num_special_tokens_to_add.return_value = 4
    tokenizer.prepare_for_model.side_effect = lambda query, document, **kwargs: {
        "input_ids": query + [101] + document + [102],
        "attention_mask": [1] * (len(query) + len(document) + 2),
    }
    model.tokenizer = tokenizer
    model.max_tokens = 96
    model.multiwindow_documents = 0
    windows = model._windows("query", "document")
    assert len(windows) == 3
    assert model.multiwindow_documents == 1
    assert {token for window in windows for token in window["input_ids"] if token >= 1000} == set(range(1000, 1200))
    assert max(len(window["input_ids"]) for window in windows) <= 96
    model.max_tokens = 32
    with pytest.raises(EvidenceSearchError, match="bounded"):
        model._windows("query", "document")
