import pytest

from llm_d_lens.benchmark_rag.query_contracts import QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.service import plan_query
from llm_d_lens.core.exceptions import DomainValidationError


@pytest.mark.parametrize(
    ("phrase", "statistic"),
    [
        ("p99", "p99"),
        ("P95", "p95"),
        ("99th percentile", "p99"),
        ("99.9th percentile", "p99.9"),
        ("mean", "mean"),
        ("average", "mean"),
        ("avg", "mean"),
        ("median", "p50"),
        ("minimum", "min"),
        ("maximum", "max"),
    ],
)
def test_question_statistics_are_required_filters(phrase, statistic):
    plan = plan_query(QueryRequest(question=f"What was the {phrase} latency for Qwen3-0.6B on H100?"))
    assert plan.filters.statistic == statistic
    assert plan.precondition is None


@pytest.mark.parametrize(
    "model",
    [
        "Llama-3.1-70B",
        "meta-llama/Llama-3.1-70B-Instruct",
        "Mistral-7B",
        "DeepSeek-R1",
        "new-vendor/Unknown-8B",
        "Qwen/Qwen3-32B",
        "Falcon",
    ],
)
def test_model_names_are_not_limited_to_known_families(model):
    plan = plan_query(QueryRequest(question=f"What was the throughput for {model} on H100?"))
    assert plan.filters.model == model
    assert plan.precondition is None


def test_explicitly_named_model_without_family_or_size():
    plan = plan_query(QueryRequest(question='Show throughput on H100 for model "Custom Model".'))
    assert plan.filters.model == "Custom Model"


@pytest.mark.parametrize(
    ("question", "filters", "field"),
    [
        ("p99 latency for Qwen3-0.6B on H100", QueryFilters(statistic="mean"), "statistic"),
        ("Throughput for Llama-3.1-70B on H100", QueryFilters(model="Qwen3-0.6B"), "model"),
    ],
)
def test_question_filter_conflicts_are_rejected(question, filters, field):
    with pytest.raises(DomainValidationError, match=field):
        plan_query(QueryRequest(question=question, filters=filters))


def test_matching_explicit_filters_and_statistic_aliases_are_preserved():
    plan = plan_query(
        QueryRequest(
            question="Average latency for Llama-3.1-70B on H100",
            filters=QueryFilters(model="llama-3.1-70b", statistic="avg"),
        )
    )
    assert plan.filters.model == "llama-3.1-70b"
    assert plan.filters.statistic == "mean"
    assert plan.precondition is None


@pytest.mark.parametrize(
    ("question", "code"),
    [
        ("Compare p50 and p99 latency for Qwen3-0.6B on H100", "statistic_scope_required"),
        ("Compare Llama-3.1-70B and Qwen3-0.6B throughput on H100", "model_scope_required"),
        ("Throughput for an unspecified model on H100", "model_scope_required"),
    ],
)
def test_ambiguous_question_conditions_abstain_before_retrieval(question, code):
    plan = plan_query(QueryRequest(question=question))
    assert plan.precondition is not None
    assert plan.precondition.code == code


def test_hardware_and_record_ids_are_not_model_names():
    plan = plan_query(QueryRequest(question="Throughput for Prism ID 02c8f038-1cad-4ee8-a72e-724affa923f4 on H100"))
    assert plan.filters.model is None
    assert plan.filters.record_id == "02c8f038-1cad-4ee8-a72e-724affa923f4"
    assert plan.precondition is None


def test_hardware_only_question_does_not_invent_a_model_filter():
    plan = plan_query(QueryRequest(question="What was the mean throughput for H100?"))
    assert plan.filters.model is None
    assert plan.filters.hardware == "H100"
    assert plan.precondition is None


def test_known_guide_alias_also_respects_an_explicit_source_path():
    plan = plan_query(
        QueryRequest(
            question="Compare H200 gpt-oss-120b weight-load times",
            filters=QueryFilters(
                model="openai/gpt-oss-120b",
                source_path="guides/modelexpress-p2p/benchmark-results/h200-ib-gpt-oss-120b.md",
            ),
        )
    )
    assert plan.filters.model == "openai/gpt-oss-120b"
    assert plan.precondition is None


@pytest.mark.parametrize(
    "question",
    [
        "What is TTFT?",
        "What is throughput?",
        "What is the TPOT metric?",
        "Define request latency.",
        "Explain time to first token.",
        "What does TTFT mean?",
        "What does TPOT stand for?",
        "Compare the definitions of TTFT and TPOT.",
    ],
)
def test_metric_definitions_use_passage_search(question):
    plan = plan_query(QueryRequest(question=question))
    assert plan.intent == "definition"
    assert plan.filters.metric is None
    assert plan.precondition is None
    assert plan.text_query


@pytest.mark.parametrize(
    ("phrase", "metric"),
    [
        ("TTFT", "ttft"),
        ("time to first token", "ttft"),
        ("TPOT", "tpot"),
        ("time per output token", "tpot"),
        ("latency", "latency"),
        ("throughput", "throughput"),
        ("weight loading time", "weight_load"),
    ],
)
def test_metric_measurements_use_the_same_vocabulary_for_intent_and_filter(phrase, metric):
    plan = plan_query(QueryRequest(question=f"What is the {phrase} for Llama-3.1-70B on H100?"))
    assert plan.intent == "numeric"
    assert plan.filters.metric == metric
    assert plan.filters.model == "Llama-3.1-70B"
    assert plan.precondition is None


def test_explicit_intent_is_not_overridden_by_definition_words():
    plan = plan_query(
        QueryRequest(question="What is TTFT?", intent="numeric", filters=QueryFilters(model="Qwen3-0.6B"))
    )
    assert plan.intent == "numeric"
    assert plan.filters.metric == "ttft"


def test_explicit_measurement_scope_disambiguates_a_short_question():
    plan = plan_query(QueryRequest(question="What is TTFT?", filters=QueryFilters(model="Qwen3-0.6B")))
    assert plan.intent == "numeric"
    assert plan.filters.metric == "ttft"
