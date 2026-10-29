from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from llm_d_lens.benchmark_rag.contracts import normalize_unit
from llm_d_lens.benchmark_rag.guide_json import parse_custom_json
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PublicationError, item_id
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN

_REVISION = "080c14d957755da4cc363745638619fc748558f1"
_DIRECTORY = "guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/"
_ROOT = Path(__file__).resolve().parents[6] / "llm-d"
_PINNED = {
    "complete-policy-source-exact-window.json": (
        "501edf614aa489f9eea8eb93153b169b977a83207886e17c0f812ffbf1915625",
        4,
        100,
    ),
    "four-arm-exact-window.json": (
        "54f2a6db8fd1696c762e4a66a7fb737eb50a17bc7071bc5f241e9c733edd076a",
        4,
        100,
    ),
    "repeat-approx-first-exact-window.json": (
        "b70a2772b5e77f646c387eaf989407e5db2c0d87cd37291954cb6be15f0c0f40",
        2,
        50,
    ),
    "repeat-precise-first-exact-window.json": (
        "cea487ecbbab6fd5a7156dd858038287f9213b5f70005df5d60e13bda969bf2c",
        2,
        50,
    ),
    "three-pair-aggregate.json": (
        "f482f4648cb91448befc9b3418db8f81492d11191924b360ac4f8f6b1e435241",
        12,
        294,
    ),
}


def _file(data: bytes, name: str = "complete-policy-source-exact-window.json") -> InventoryFile:
    return InventoryFile(path=_DIRECTORY + name, sha256=hashlib.sha256(data).hexdigest(), format="json")


def _row(run: str) -> dict:
    return {
        "run": run,
        "window_s": 300,
        "admitted_by_cutoff": 10,
        "admitted_not_terminal_by_cutoff": 1,
        "records": 10,
        "successful_requests": 8,
        "terminal_records": 9,
        "window_errors": 1,
        "input_ktok_s": 1,
        "output_tok_s": 2,
        "successful_request_rps": 3,
        "terminal_request_rps": 4,
        **{metric: {"count": 8, "mean": 2, "p50": 1, "p90": 3, "p99": 4} for metric in ("e2e_ms", "itl_ms", "ttft_ms")},
    }


def _rows() -> list[dict]:
    return [_row(run) for run in ("approx-no-p2p", "approx-p2p", "precise-no-p2p", "precise-p2p")]


def _parse(value: object):
    data = json.dumps(value).encode()
    return parse_custom_json(_file(data), _REVISION, data)


def _at(root: object, pointer: str):
    result = root
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        result = result[int(key)] if isinstance(result, list) else result[key]
    return result


def _numeric_leaves(value: object, pointer: str = "") -> dict[str, Decimal]:
    if isinstance(value, Decimal):
        return {pointer: value}
    children = enumerate(value) if isinstance(value, list) else value.items() if isinstance(value, dict) else ()
    result = {}
    for key, child in children:
        escaped = str(key).replace("~", "~0").replace("/", "~1")
        result.update(_numeric_leaves(child, pointer + "/" + escaped))
    return result


def _expected_unit(pointer: str) -> str:
    parts = pointer.split("/")
    if "paired_delta_aggregates_pct" in parts or "precise_vs_approximate_pct" in parts:
        return "%"
    if parts[-1] == "population_cv_pct":
        return "%"
    metric = parts[-2] if parts[-1] in {"max", "mean", "median", "min", "p50", "p90", "p99", "count"} else parts[-1]
    if parts[-1] == "count" or metric in {
        "admitted_by_cutoff",
        "admitted_not_terminal_by_cutoff",
        "records",
        "successful_requests",
        "terminal_records",
        "window_errors",
    }:
        return "count"
    if metric.startswith(("e2e_ms", "ttft_ms", "itl_ms")):
        return "ms"
    return {
        "input_ktok_s": "input ktokens/s",
        "output_tok_s": "output tokens/s",
        "successful_request_rps": "requests/s",
        "terminal_request_rps": "requests/s",
    }[metric]


@pytest.mark.parametrize("name", _PINNED)
def test_pinned_five_files_cover_every_measured_numeric_leaf(name: str) -> None:
    source = _ROOT / _DIRECTORY / name
    if not source.is_file():
        pytest.skip("optional pinned llm-d checkout is not available")
    data = source.read_bytes()
    digest, experiments, measurements = _PINNED[name]
    assert hashlib.sha256(data).hexdigest() == digest
    file = _file(data, name)
    items = parse_custom_json(file, _REVISION, data)
    assert Counter(item.kind for item in items) == {
        "experiment": experiments,
        "measurement": measurements,
        "passage": 1,
    }
    assert items == parse_custom_json(file, _REVISION, data)
    assert len({item.item_id for item in items}) == len(items)
    root = json.loads(data, parse_int=Decimal, parse_float=Decimal)
    expected = {
        pointer: number
        for pointer, number in _numeric_leaves(root).items()
        if pointer.rsplit("/", 1)[-1] not in {"window_s", "pair"}
    }
    observed = {}
    by_id = {item.item_id: item for item in items}
    file_id = item_id(LLM_D_ORIGIN, f"file:{file.path}", _REVISION)
    for item in items:
        assert item.origin_id == LLM_D_ORIGIN
        assert item.logical_key.startswith(file.path + "#")
        assert item.item_id == item_id(LLM_D_ORIGIN, item.logical_key, _REVISION)
        assert item.content_sha256 == digest
        assert item.source_path == file.path
        assert item.source_revision == _REVISION
        assert item.source_pointer is not None
        pointer = item.source_pointer["json_pointer"]
        assert isinstance(pointer, str)
        if item.kind != "measurement":
            assert item.parent_id == file_id
            if item.kind == "experiment":
                assert item.model == "GLM-5.2"
                assert item.hardware is item.runtime is item.timestamp is None
                assert item.workload["concurrency"] == "64"
                assert item.configuration["prefill_instances"] == "2"
                assert item.source_pointer["model_provenance"]["revision"] == _REVISION
            continue
        assert item.parent_id == item.experiment_id
        assert by_id[item.experiment_id].kind == "experiment"
        assert pointer not in observed
        observed[pointer] = item.value
        assert _at(root, pointer) == item.value == item.original_value
        assert item.unit == item.original_unit == _expected_unit(pointer)
        assert item.source_pointer["metric_label"] == item.metric
        assert item.source_pointer["statistic_label"] == item.statistic
        assert item.source_pointer["unit_provenance"]
        normalized = normalize_unit(item.value, item.unit)
        if normalized is None:
            assert item.normalized_value is item.normalized_unit is item.normalization_rule is None
        else:
            assert (item.normalized_value, item.normalized_unit, item.normalization_rule) == normalized
    assert observed == expected


def test_exact_decimal_nulls_metadata_and_excluded_arms() -> None:
    rows = _rows()
    rows[0]["e2e_ms"]["mean"] = None
    rows[0]["window_s"] = None
    data = (
        json.dumps(rows)
        .encode()
        .replace(
            b'"input_ktok_s": 1',
            b'"input_ktok_s": 1.12345678901234567890123456789',
            1,
        )
    )
    items = parse_custom_json(_file(data), _REVISION, data)
    first = next(item for item in items if item.kind == "experiment")
    assert first.configuration["window_s"] is None
    assert first.configuration["null_measurements"] == ["/0/e2e_ms/mean"]
    precise = next(item for item in items if item.source_pointer["json_pointer"] == "/0/input_ktok_s")
    assert precise.value == Decimal("1.12345678901234567890123456789")
    assert precise.unit == "input ktokens/s"
    assert precise.normalized_value is None
    assert not any(item.source_pointer["json_pointer"] == "/0/e2e_ms/mean" for item in items)
    excluded = next(item for item in items if item.kind == "experiment" and item.configuration["run"] == "approx-p2p")
    assert "does not isolate P2P" in excluded.configuration["reported_table_status"]
    assert any(
        "not additional" in item.passage_text or "not be counted as independent" in item.passage_text
        for item in items
        if item.kind == "passage"
    )


@pytest.mark.parametrize("bad", [True, False, "2", {}, [], -1, 0.5])
def test_counts_reject_boolean_string_nonintegral_and_negative(bad: object) -> None:
    rows = _rows()
    rows[0]["records"] = bad
    with pytest.raises(PublicationError):
        _parse(rows)


@pytest.mark.parametrize("field", ["input_ktok_s", "output_tok_s", "successful_request_rps"])
def test_rates_reject_boolean_measurements(field: str) -> None:
    rows = _rows()
    rows[0][field] = True
    with pytest.raises(PublicationError, match="finite JSON number"):
        _parse(rows)


def test_latency_boolean_is_not_a_measurement() -> None:
    rows = _rows()
    rows[0]["ttft_ms"]["mean"] = True
    with pytest.raises(PublicationError, match="finite JSON number"):
        _parse(rows)


@pytest.mark.parametrize("mutation", ["metric", "statistic", "unit", "missing", "window", "run", "root", "row"])
def test_unknown_schema_or_numeric_metric_fails_closed(mutation: str) -> None:
    rows = _rows()
    if mutation == "metric":
        rows[0]["mystery_numeric_metric"] = 42
    elif mutation == "statistic":
        rows[0]["ttft_ms"]["p95"] = 42
    elif mutation == "unit":
        rows[0]["e2e_ms"]["unit"] = "s"
    elif mutation == "missing":
        del rows[0]["itl_ms"]
    elif mutation == "window":
        rows[0]["window_s"] = 60
    elif mutation == "run":
        rows[0]["run"] = "new-model-hardware-h200"
    elif mutation == "root":
        rows = {"runs": rows}
    else:
        rows[0] = None
    with pytest.raises(PublicationError):
        _parse(rows)


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"run":"a","run":"b"}]',
        b'[{"x":NaN}]',
        b'[{"x":Infinity}]',
        b'[{"x":-Infinity}]',
        b'[{"x":1e99999999999999999999999999999}]',
        b"\xff",
        b"not JSON",
    ],
)
def test_duplicate_keys_nonfinite_and_malformed_json(payload: bytes) -> None:
    with pytest.raises(PublicationError):
        parse_custom_json(_file(payload), _REVISION, payload)


def test_digest_format_and_family_are_validated_before_parsing() -> None:
    data = json.dumps(_rows()).encode()
    file = _file(data)
    for invalid in (
        file.model_copy(update={"sha256": "0" * 64}),
        file.model_copy(update={"format": "markdown"}),
        file.model_copy(update={"path": "other/glm-5.2-h200.json"}),
        file.model_copy(update={"path": _DIRECTORY + "unknown.json"}),
    ):
        with pytest.raises(PublicationError):
            parse_custom_json(invalid, _REVISION, data)
    with pytest.raises(PublicationError, match="SHA-256"):
        parse_custom_json(file, _REVISION, data + b"\n")


@pytest.mark.parametrize("revision", ["f" * 40, "main", ""])
def test_readme_derived_metadata_requires_reviewed_revision(revision: str) -> None:
    data = json.dumps(_rows()).encode()
    file = _file(data)
    assert any(item.kind == "experiment" for item in parse_custom_json(file, _REVISION, data))
    with pytest.raises(PublicationError, match="unreviewed custom JSON revision.*README.*compatibility review"):
        parse_custom_json(file, revision, data)


def test_aggregate_summary_units_and_schema_are_not_generic_numeric_walks() -> None:
    metrics = {
        metric: {"min": 1, "max": 2, "mean": 1, "median": 1, "population_cv_pct": 2}
        for metric in (
            "e2e_ms.p50",
            "e2e_ms.p90",
            "itl_ms.p50",
            "itl_ms.p90",
            "ttft_ms.p50",
            "ttft_ms.p90",
            "input_ktok_s",
            "successful_request_rps",
        )
    }
    root = {
        "method": "Maroon terminal-by-request_end_ns cutoff at 300 seconds",
        "config_aggregates": {"approximate_no_p2p": metrics, "precise_p2p": deepcopy(metrics)},
        "paired_delta_aggregates_pct": deepcopy(metrics),
        "pairs": [
            {
                "pair": index + 1,
                "approximate": _row(approx),
                "precise_p2p": _row(precise),
                "precise_vs_approximate_pct": dict.fromkeys(metrics, -1),
            }
            for index, (approx, precise) in enumerate(
                (
                    ("approx-no-p2p", "precise-p2p"),
                    ("pair1-approx-no-p2p", "pair1-precise-p2p"),
                    ("pair2-approx-no-p2p", "pair2-precise-p2p"),
                )
            )
        ],
    }

    def parse():
        data = json.dumps(root).encode()
        return parse_custom_json(_file(data, "three-pair-aggregate.json"), _REVISION, data)

    items = parse()
    for item in items:
        if item.kind == "measurement":
            assert item.unit == _expected_unit(item.source_pointer["json_pointer"])
        if item.kind == "experiment" and item.configuration["evidence_role"] == "exported_arm_summary":
            assert "not an independent trial" in item.configuration["source_record"]["relationship"]
    root["paired_delta_aggregates_pct"]["ttft_ms.p50"]["mean"] = None
    items = parse()
    aggregate = next(
        item
        for item in items
        if item.kind == "experiment" and item.configuration["evidence_role"] == "paired_delta_aggregate"
    )
    assert aggregate.configuration["null_measurements"] == ["/paired_delta_aggregates_pct/ttft_ms.p50/mean"]
    assert not any(
        item.source_pointer["json_pointer"] == aggregate.configuration["null_measurements"][0] for item in items
    )
    root["config_aggregates"]["approximate_no_p2p"]["e2e_ms.p50"]["new_ratio"] = 2
    with pytest.raises(PublicationError, match="shape/metric"):
        parse()
    del root["config_aggregates"]["approximate_no_p2p"]["e2e_ms.p50"]["new_ratio"]
    root["paired_delta_aggregates_pct"]["unknown_seconds"] = metrics["e2e_ms.p50"]
    with pytest.raises(PublicationError, match="shape/metric"):
        parse()
