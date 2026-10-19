"""Cell-level proofs against immutable published Markdown, never the raw JSON bundle."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.guide_tables import (
    _AGENT_GLM,
    _AGENT_NEMO,
    _AGENT_QWEN,
    _CORE,
    _DATA,
    _GLM,
    _GPT,
    _LUSTRE,
    _MM,
    _MX,
    _MX_GUIDE,
    _P2P_GUIDE,
    _P2P_METHOD,
    _PD,
    _PREDICTED,
    _QPD,
    _REPORTS,
    _SLO,
    _TIER,
    _TPU_YAML,
    _WIDE,
    _WVA,
    _cells,
    _tables,
    parse_published_tables,
)
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PublicationError, item_id
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN

REVISION = "080c14d957755da4cc363745638619fc748558f1"
_COUNTS = {
    "fast-model-actuation/benchmark-results/qwen3-32b-h100/README.md": (6, 86, 38),
    _CORE: (5, 6, 30),
    _MX: (4, 8, 4),
    _GLM: (26, 57, 23),
    _GPT: (43, 114, 10),
    _QPD: (2, 22, 9),
    _DATA: (0, 0, 2),
    "optimized-baseline/benchmark-results/sglang-qwen3-32b-h100/README.md": (34, 76, 6),
    "optimized-baseline/benchmark-results/vllm-gpt-oss-120b-h100/README.md": (34, 108, 6),
    "optimized-baseline/benchmark-results/vllm-qwen3-32b-h100/README.md": (34, 94, 17),
    "precise-prefix-cache-routing/benchmark-results/sglang-qwen3-32b-h100.md": (34, 106, 5),
    "precise-prefix-cache-routing/benchmark-results/vllm-qwen3-32b-h100.md": (34, 106, 5),
    _TIER + "sglang-qwen3-32b-h100-lustre.md": (14, 70, 35),
    _TIER + "sglang-qwen3-32b-h100.md": (8, 40, 20),
    _TIER + "vllm-qwen3-32b-h100.md": (8, 40, 20),
    _TIER + "vllm-qwen3-32b-b60-xpu.md": (8, 40, 20),
    _TIER + "vllm-qwen3-8b-b60-xpu.md": (8, 40, 20),
    _TIER + "vllm-qwen3-32b-tpuv7.md": (2, 10, 5),
    _TIER + "vllm-gpt-oss-120b-h100.md": (22, 117, 57),
    _LUSTRE: (8, 60, 30),
    _AGENT_GLM: (2, 14, 4),
    _AGENT_NEMO: (2, 8, 7),
    _AGENT_QWEN: (5, 29, 7),
    _MX_GUIDE: (2, 2, 6),
    _MM: (16, 48, 2),
    _P2P_GUIDE: (0, 0, 3),
    _P2P_METHOD: (27, 83, 2),
    _PD: (2, 11, 12),
    _TPU_YAML: (0, 0, 1),
    _PREDICTED: (16, 48, 5),
    _WIDE: (1, 4, 3),
    _SLO: (2, 4, 1),
    _WVA: (12, 108, 3),
}


def _parse(path: str, data: bytes) -> tuple[RagItem, ...]:
    file = InventoryFile(path="guides/" + path, sha256=hashlib.sha256(data).hexdigest(), format="markdown")
    return parse_published_tables(file, REVISION, data)


@pytest.fixture(scope="module")
def reports() -> dict[str, bytes]:
    repo = Path(os.environ.get("LLM_D_GUIDE_TEST_REPO", Path(__file__).resolve().parents[5].parent / "llm-d"))
    if not (repo / ".git").exists():
        pytest.skip("set LLM_D_GUIDE_TEST_REPO to the llm-d Git checkout for pinned-report proofs")
    paths = (
        subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", REVISION])
        .decode()
        .splitlines()
    )
    return {
        path.removeprefix("guides/"): subprocess.check_output(["git", "-C", str(repo), "show", f"{REVISION}:{path}"])
        for path in paths
        if path.startswith("guides/") and path.endswith(".md")
    }


@pytest.mark.parametrize("path", tuple(_COUNTS))
def test_actual_published_cells_round_trip_and_replay(path: str, reports: dict[str, bytes]) -> None:
    data = reports[path]
    items = _parse(path, data)
    assert items == _parse(path, data)
    assert [i.model_dump(mode="json") for i in items] == [i.model_dump(mode="json") for i in _parse(path, data)]
    counts = Counter(i.kind for i in items)
    assert (counts["experiment"], counts["measurement"], counts["passage"]) == _COUNTS[path]
    assert set(counts) <= {"experiment", "measurement", "passage"}
    assert len({i.item_id for i in items}) == len(items)
    by_id = {i.item_id: i for i in items}
    tables = _tables(data.decode())
    for item in items:
        assert item.logical_key.startswith((f"guides/{path}#published-table:", f"guides/{path}#published-narrative:"))
        assert item.origin_id == LLM_D_ORIGIN
        assert item.content_sha256 == hashlib.sha256(data).hexdigest()
        p = item.source_pointer
        assert p is not None
        if "table_index" not in p:
            assert item.kind == "passage"
            assert "narrative or fenced YAML" in p["reason"]
            continue
        assert type(p["table_index"]) is int
        assert "#section:" not in item.logical_key
        table = tables[p["table_index"]]
        assert p["heading_path"] == list(table.heading)
        if item.kind == "experiment":
            assert item.parent_id == item_id(LLM_D_ORIGIN, f"file:guides/{path}", REVISION)
            assert item.configuration["published_report"] == data.decode()
        elif item.kind == "passage":
            assert p["reason"]
        else:
            assert item.parent_id == item.experiment_id
            assert by_id[item.experiment_id].kind == "experiment"
            assert p["cell"] == _cells(data.decode().splitlines()[p["line"] - 1])[p["column"]]
            assert p["header"] == table.header[p["column"]]
            assert table.rows[p["row"]][0] == p["line"]
            sub = p["subvalue"]
            literal = p["cell"][sub["start"] : sub["end"]]
            assert literal == sub["text"]
            assert item.original_value.as_tuple() == Decimal(literal.replace(",", "")).as_tuple()
            assert item.value == item.original_value
            assert item.unit == item.original_unit
            assert item.evidence_type == "published_table"


def test_actual_report_allowlist_and_totals(reports: dict[str, bytes]) -> None:
    assert set(_REPORTS) <= set(reports)
    assert len(reports) == 97
    assert sum(len(_tables(data.decode())) for data in reports.values()) == 155
    counts: Counter[str] = Counter()
    for path, data in reports.items():
        items = _parse(path, data)
        counts.update(i.kind for i in items)
        if _tables(data.decode()):
            assert path in _REPORTS
            assert items
    assert counts == {"experiment": 421, "measurement": 1559, "passage": 475}


def test_non_result_configuration_and_derived_tables_are_never_observations(reports: dict[str, bytes]) -> None:
    for path in (
        "recipes/router/calibration/configuration-matrix.md",
        "wide-ep-lws/modelserver/gpu/vllm-glm-5.2/README.md",
        _P2P_GUIDE,
    ):
        items = _parse(path, reports[path])
        assert items
        assert all(i.kind == "passage" and i.source_pointer["reason"] for i in items)
    delta = _parse(_P2P_GUIDE, reports[_P2P_GUIDE])
    assert (
        "derived latency delta"
        in next(i for i in delta if i.source_pointer["table_index"] == 1).source_pointer["reason"]
    )


def test_additional_agentic_scenarios_and_duplicate_citations(reports: dict[str, bytes]) -> None:
    qwen = _parse(_AGENT_QWEN, reports[_AGENT_QWEN])
    assert len([i for i in qwen if i.kind == "measurement" and i.source_pointer["table_index"] == 4]) == 21
    assert {i.configuration["concurrent_coding_sessions"] for i in qwen if i.kind == "experiment"} == {40}
    nemo = _parse(_AGENT_NEMO, reports[_AGENT_NEMO])
    assert {i.configuration["concurrent_coding_sessions"] for i in nemo if i.kind == "experiment"} == {60}
    glm = _parse(_AGENT_GLM, reports[_AGENT_GLM])
    assert {(i.configuration["concurrency"], i.hardware) for i in glm if i.kind == "experiment"} == {
        ("64", "64 GPUs H200"),
        ("128", "64 GPUs H200"),
    }
    for path, cited in ((_MX_GUIDE, _MX), (_P2P_METHOD, _GPT)):
        experiments = [i for i in _parse(path, reports[path]) if i.kind == "experiment"]
        assert experiments
        assert all(i.configuration["citation_of"] == "guides/" + cited for i in experiments)
        assert all(i.configuration["independent_run"] is False for i in experiments)
    assert {i.value for i in _parse(_MX_GUIDE, reports[_MX_GUIDE]) if i.metric == "weight_load"} == {
        Decimal("30.3"),
        Decimal("0.93"),
    }


def test_wva_keeps_row_models_scenario_and_aggregation(reports: dict[str, bytes]) -> None:
    items = _parse(_WVA, reports[_WVA])
    experiments = [i for i in items if i.kind == "experiment"]
    assert len(experiments) == 12
    assert {i.model for i in experiments} == {"Qwen3-32B", "Qwen3-0.6B"}
    assert {i.hardware for i in experiments} == {None}
    assert {i.runtime for i in experiments} == {None}
    assert {i.configuration["aggregation"] for i in experiments} == {"3-run averages"}
    assert {i.source_pointer["heading_path"][-1] for i in experiments} == {
        "Prefill Heavy",
        "Decode Heavy",
        "Bursty",
        "Symmetrical",
    }
    assert {i.configuration["load_generator"] for i in experiments} == {"GuideLLM", "inference-perf"}


def test_predicted_scenarios_and_non_table_yaml_are_auditable(reports: dict[str, bytes]) -> None:
    items = _parse(_PREDICTED, reports[_PREDICTED])
    assert len([i for i in items if i.kind == "passage" and "table_index" not in i.source_pointer]) == 3
    experiments = [i for i in items if i.kind == "experiment"]
    assert {i.model for i in experiments} == {"Qwen/Qwen3-VL-32B-Instruct"}
    assert {i.configuration["predictor"] for i in experiments} == {"warm second pass"}
    assert {i.metric for i in items if i.kind == "measurement"} == {"throughput", "ttft"}
    yaml = _parse(_TPU_YAML, reports[_TPU_YAML])
    assert len(yaml) == 1
    assert yaml[0].kind == "passage"
    assert "fenced YAML" in yaml[0].source_pointer["reason"]
    assert "normalized_time_per_output_token" in yaml[0].passage_text
    pd = _parse(_PD, reports[_PD])
    assert any(i.kind == "passage" and "<1ms" in i.passage_text for i in pd)
    assert {i.hardware for i in pd if i.kind == "experiment"} == {None, "16x H200"}


def test_model_loading_exact_values_and_mixed_models(reports: dict[str, bytes]) -> None:
    items = _parse(_MX, reports[_MX])
    weights = [i for i in items if i.metric == "weight_load"]
    assert {(i.value, i.unit, i.normalized_value) for i in weights} == {
        (Decimal("30.3"), "s", Decimal("30300")),
        (Decimal("0.93"), "s", Decimal("930")),
    }
    items = _parse(_CORE, reports[_CORE])
    models = {i.item_id: i.model for i in items if i.kind == "experiment"}
    assert {models[i.experiment_id] for i in items if i.metric == "weight_load" and i.value == 211} == {
        "meta-llama/Llama-4-Scout-17B-16E-Instruct",
    }
    assert {models[i.experiment_id] for i in items if i.metric == "weight_load" and i.value == Decimal("2.7")} == {
        "meta-llama/Llama-3.3-70B-Instruct",
    }
    assert not any(i.value == Decimal("2.0") for i in items if i.kind == "measurement")
    lustre = _parse(_LUSTRE, reports[_LUSTRE])
    assert {(i.source_pointer["table_index"], i.model) for i in lustre if i.kind == "experiment"} == {
        (0, None),
        (1, None),
        (2, "Qwen/Qwen3-32B"),
        (3, "Llama-3.3-70B"),
    }


def test_compound_cells_and_latency_denominators(reports: dict[str, bytes]) -> None:
    items = _parse(_GPT, reports[_GPT])
    uniform = [i for i in items if i.kind == "measurement" and i.source_pointer["table_index"] == 2]
    assert {i.metric for i in uniform} == {"request_rate", "latency"}
    hot = [i for i in items if i.kind == "measurement" and i.source_pointer["table_index"] == 3]
    assert {i.metric for i in hot} == {"request_rate", "latency", "ttft", "error_count"}
    assert {i.unit for i in hot if i.metric == "ttft"} == {"ms", "s"}
    assert {i.value for i in hot if i.metric == "error_count"} == {Decimal(274), Decimal(0)}
    gpt = _parse(_TIER + "vllm-gpt-oss-120b-h100.md", reports[_TIER + "vllm-gpt-oss-120b-h100.md"])
    assert {(i.metric, i.unit) for i in gpt if i.kind == "measurement" and i.source_pointer["table_index"] == 0} == {
        ("successful_request_rate", "requests/s"),
        ("throughput", "output tokens/s"),
        ("input_throughput", "input tokens/s"),
        ("total_throughput", "tokens/s"),
    }
    assert {i.unit for i in gpt if i.metric in {"itl", "tpot"}} == {"ms"}
    assert {i.unit for i in gpt if i.metric == "internal_cache_hits"} == {"M tokens"}


def test_glm_cells_repeats_and_superseded_campaigns(reports: dict[str, bytes]) -> None:
    items = _parse(_GLM, reports[_GLM])
    assert {
        i.configuration["cell"] for i in items if i.kind == "experiment" and i.source_pointer["table_index"] == 0
    } == {"1P1D"}
    assert {
        i.configuration["cell"] for i in items if i.kind == "experiment" and i.source_pointer["table_index"] in {3, 4}
    } == {"2P2D"}
    assert not any(i.kind == "measurement" and i.source_pointer["table_index"] in {1, 5} for i in items)
    historical = [i for i in items if i.kind == "passage" and i.source_pointer["table_index"] in {1, 5}]
    assert len(historical) == 2
    assert all("superseded" in i.source_pointer["reason"] for i in historical)
    compound = [i for i in items if i.kind == "measurement" and i.source_pointer["table_index"] == 4]
    assert Counter(i.statistic for i in compound if i.metric in {"ttft", "latency"}) == {"p50": 8, "p90": 8}
    assert len([i for i in items if i.kind == "experiment" and i.source_pointer["table_index"] == 3]) == 10
    assert not any("change" in (i.metric or "") for i in items if i.kind == "measurement")


def test_empty_histograms_and_explicit_rate_carry_forward(reports: dict[str, bytes]) -> None:
    path = "optimized-baseline/benchmark-results/vllm-qwen3-32b-h100/README.md"
    items = _parse(path, reports[path])
    assert not any(i.metric == "ttft" and i.value == 0 for i in items)
    assert (
        len([i for i in items if i.kind == "passage" and "empty latency histogram" in i.source_pointer["reason"]]) == 12
    )
    path = _TIER + "vllm-qwen3-32b-h100.md"
    experiments = [i for i in _parse(path, reports[path]) if i.kind == "experiment"]
    assert [i.configuration["target_rate"] for i in experiments] == [
        "5.0 QPS",
        "5.0 QPS",
        "10.0 QPS",
        "10.0 QPS",
        "20.0 QPS",
        "20.0 QPS",
        "40.0 QPS",
        "40.0 QPS",
    ]
    for i in experiments[1::2]:
        assert i.configuration["target_rate_carried_from_row"] == i.configuration["row"] - 1
    bad = reports[path].replace(b"**5.0 QPS**", b"")
    with pytest.raises(PublicationError, match="blank target rate"):
        _parse(path, bad)
    bad = reports[path].replace(b"HBM + CPU RAM", b"HBM-only")
    with pytest.raises(PublicationError, match="alternating target-rate"):
        _parse(path, bad)


_SIMPLE = b"""# Published P/D results
| metric | plain NIXL P/D | + P2P pull | delta |
|---|---:|---:|---:|
| TTFT p50 | 6.830 s | **1.09 s** | **6.3x** |
"""


def test_self_contained_exact_decimal_and_html_wrapping() -> None:
    items = _parse(_QPD, b'<div align="center">\n\n' + _SIMPLE + b"\n</div>\n")
    assert [i.value.as_tuple() for i in items if i.kind == "measurement"] == [
        Decimal("6.830").as_tuple(),
        Decimal("1.09").as_tuple(),
    ]
    assert [i.unit for i in items if i.kind == "measurement"] == ["s", "s"]


def test_fenced_examples_not_tables_or_observations() -> None:
    fenced = b"\n```yaml\n| Example | number |\n|---|---|\n| TTFT | 123 |\n```\n"
    items = _parse(_QPD, _SIMPLE + fenced)
    assert len([i for i in items if i.kind == "measurement"]) == 2
    assert len(_tables((_SIMPLE + fenced).decode())) == 1
    assert _parse("documentation/README.md", _SIMPLE) == ()


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        (b"TTFT p50", b"Invented metric", "unreviewed published metric"),
        (b"plain NIXL P/D", b"Unknown arm", "unreviewed published table layout"),
        (b"6.830 s", b"6.830 bananas", "unreviewed published numeric cell"),
        (b"6.830 s", b"6.83 / 7.93 s", "unreviewed published numeric cell"),
    ],
)
def test_unknown_layout_metric_and_cell_fail_closed(old: bytes, new: bytes, error: str) -> None:
    with pytest.raises(PublicationError, match=error):
        _parse(_QPD, _SIMPLE.replace(old, new))


@pytest.mark.parametrize("value", [b"~6.8 s", b"2.0\xe2\x80\x932.5 s", b"n/a", b"\xe2\x80\x94"])
def test_non_exact_and_missing_are_auditable_context(value: bytes) -> None:
    items = _parse(_QPD, _SIMPLE.replace(b"6.830 s", value))
    assert len([i for i in items if i.kind == "measurement"]) == 1
    excluded = [i for i in items if i.kind == "passage" and "non-exact" in i.source_pointer["reason"]]
    assert len(excluded) == 1
    assert value.decode() in excluded[0].passage_text


def test_hash_mismatch_and_table_count_change_fail() -> None:
    file = InventoryFile(path="guides/" + _QPD, sha256="0" * 64, format="markdown")
    with pytest.raises(PublicationError, match="hash"):
        parse_published_tables(file, REVISION, _SIMPLE)
    with pytest.raises(PublicationError, match="table count"):
        _parse(_QPD, _SIMPLE + b"\n" + _SIMPLE)


@pytest.mark.parametrize(
    ("revision", "error"),
    [("f" * 40, "requires review"), ("main", "pinned full commit")],
)
def test_unreviewed_measurement_revision_fails_closed(revision: str, error: str) -> None:
    file = InventoryFile(
        path="guides/" + _QPD,
        sha256=hashlib.sha256(_SIMPLE).hexdigest(),
        format="markdown",
    )
    with pytest.raises(PublicationError, match=error):
        parse_published_tables(file, revision, _SIMPLE)


def test_every_measured_guide_requires_reviewed_revision(reports: dict[str, bytes]) -> None:
    for path, data in reports.items():
        if not any(item.kind == "measurement" for item in _parse(path, data)):
            continue
        file = InventoryFile(path="guides/" + path, sha256=hashlib.sha256(data).hexdigest(), format="markdown")
        with pytest.raises(PublicationError, match="requires review"):
            parse_published_tables(file, "f" * 40, data)


def test_context_only_source_accepts_another_valid_pinned_revision() -> None:
    data = b"# Configuration\n| Parameter | Value |\n|---|---|\n| Peak prefill throughput | 100000 |\n"
    file = InventoryFile(
        path="guides/recipes/router/calibration/configuration-matrix.md",
        sha256=hashlib.sha256(data).hexdigest(),
        format="markdown",
    )
    items = parse_published_tables(file, "e" * 40, data)
    assert len(items) == 1
    assert items[0].kind == "passage"
    assert items[0].source_revision == "e" * 40
    assert "not measured results" in items[0].source_pointer["reason"]
