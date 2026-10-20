"""Strict adapter for the five GLM C64 fixed-window JSON exports.

The schema and file-family metadata were verified at ``_METADATA_REVISION``.
Other revisions require explicit compatibility review of the associated README
and hardcoded metadata against the exact commit.
This is not a generic numeric-tree importer. New metrics require unit and
semantic review; repeated source rows and derived aggregates remain distinct
evidence, not additional independent trials. The publication service retains
the complete input bytes.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, DecimalException

from pydantic import JsonValue

from llm_d_lens.benchmark_rag.contracts import RagItem, normalize_unit
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PublicationError, item_id
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN, validate_revision

_DIRECTORY = "guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/"
_METADATA_REVISION = "080c14d957755da4cc363745638619fc748558f1"
_README_SHA256 = "2a4f122cd16550296b482fe2204e3655a09d1fd1147e15a8a463db0eb0b88ada"
_RUNS = {
    "complete-policy-source-exact-window.json": (
        "approx-no-p2p",
        "approx-p2p",
        "precise-no-p2p",
        "precise-p2p",
    ),
    "four-arm-exact-window.json": (
        "r1-approx-no-p2p",
        "r1-approx-p2p",
        "r1-precise-no-p2p",
        "r1-precise-p2p",
    ),
    "repeat-approx-first-exact-window.json": ("pair1-approx-no-p2p", "pair1-precise-p2p"),
    "repeat-precise-first-exact-window.json": ("pair2-approx-no-p2p", "pair2-precise-p2p"),
}
_AGGREGATE = "three-pair-aggregate.json"
_COUNTS = {
    "admitted_by_cutoff",
    "admitted_not_terminal_by_cutoff",
    "records",
    "successful_requests",
    "terminal_records",
    "window_errors",
}
_RATES = {
    "input_ktok_s": "input ktokens/s",
    "output_tok_s": "output tokens/s",
    "successful_request_rps": "requests/s",
    "terminal_request_rps": "requests/s",
}
_LATENCIES = {"e2e_ms", "itl_ms", "ttft_ms"}
_LATENCY_STATS = {"count", "mean", "p50", "p90", "p99"}
_SUMMARY_METRICS = {
    "e2e_ms.p50": "ms",
    "e2e_ms.p90": "ms",
    "itl_ms.p50": "ms",
    "itl_ms.p90": "ms",
    "ttft_ms.p50": "ms",
    "ttft_ms.p90": "ms",
    "input_ktok_s": "input ktokens/s",
    "successful_request_rps": "requests/s",
}
_SUMMARY_STATS = {"max", "mean", "median", "min", "population_cv_pct"}
_METHOD = "Maroon terminal-by-request_end_ns cutoff at 300 seconds"
_LIMITATIONS = (
    "These are terminal-by-cutoff request summaries, not raw requests. A request is included only if terminal "
    "by min(credit_issued_ns) + 300 seconds; later drain completions are excluded. The README reports "
    "new engine pod UIDs for each measured arm. Counts are observed records or metric sample counts, "
    "not workload settings. window_s and pair are context, not measurements. "
    "The complete-policy approximate+P2P record does not isolate P2P and is excluded from reported comparisons; "
    "its precise/no-P2P record also does not enter a reported table. "
    "The aggregate reuses the complete-policy baseline/candidate and both repeats; its embedded arms "
    "and summaries must not be counted as independent trials. Percentage changes are "
    "100 * (precise_p2p / approximate - 1); negative latency changes indicate improvement. "
    "Population CV is a percentage, not the underlying latency/throughput unit. "
    "input_ktok_s retains the source's kilo-input-token/s scale without an undocumented conversion. "
    "Hardware, runtime, timestamps, per-request records, rank time series and DEBUG logs are not supplied "
    "by this JSON/README family. Null measurements remain unknown in experiment context."
)


def _object(value: object, keys: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PublicationError("expected a JSON object")
    if keys is not None and set(value) != keys:
        raise PublicationError(f"unsupported custom JSON shape/metric: expected {sorted(keys)}, got {sorted(value)}")
    return value


def _array(value: object, length: int) -> list[object]:
    if not isinstance(value, list) or len(value) != length:
        raise PublicationError(f"expected a JSON array with {length} records")
    return value


def _number(value: object, *, count: bool = False, signed: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PublicationError("expected a finite JSON number, not a boolean or string")
    if not signed and value < 0:
        raise PublicationError("negative count, latency or rate")
    if count and value != value.to_integral_value():
        raise PublicationError("expected an integral count")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise PublicationError(f"nonfinite JSON constant: {value}")


def _pointer(base: str, *parts: str | int) -> str:
    return base + "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _json(value: object) -> JsonValue:
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, list):
        return [_json(child) for child in value]
    return {key: _json(child) for key, child in _object(value).items()}


def _readme() -> dict[str, JsonValue]:
    return {
        "path": _DIRECTORY + "README.md",
        "revision": _METADATA_REVISION,
        "sha256": _README_SHA256,
        "heading": "GLM-5.2 C64 fixed-window data",
        "workload_section": "Each source workload uses the same AIPerf configuration",
    }


class _Adapter:
    def __init__(self, file: InventoryFile, revision: str) -> None:
        self.file = file
        self.revision = revision
        self.items: list[RagItem] = []
        self.file_id = item_id(LLM_D_ORIGIN, f"file:{file.path}", revision)

    def _item(self, pointer: str, role: str, parent: str, fields: dict[str, object]) -> RagItem:
        key = f"{self.file.path}#{pointer}:{role}"
        item = RagItem.model_validate(
            {
                "item_id": item_id(LLM_D_ORIGIN, key, self.revision),
                "origin_id": LLM_D_ORIGIN,
                "logical_key": key,
                "source_revision": self.revision,
                "content_sha256": self.file.sha256,
                "access_classification": "verified_public",
                "source_path": self.file.path,
                "source_url": f"{LLM_D_ORIGIN}/blob/{self.revision}/{self.file.path}",
                "format": "json",
                "parent_id": parent,
                **fields,
            }
        )
        self.items.append(item)
        return item

    def experiment(self, pointer: str, context: dict[str, object]) -> RagItem:
        return self._item(
            pointer,
            "experiment",
            self.file_id,
            {
                "kind": "experiment",
                "model": "GLM-5.2",
                "workload": {
                    "dataset": "semianalysis_cc_traces_weka_062126",
                    "entries": "48",
                    "concurrency": "64",
                    "seed": "67",
                    "admission_window_s": "300",
                    "drain_s": "120",
                    "provenance": _readme(),
                },
                "configuration": _json(
                    {
                        "prefill_instances": Decimal(2),
                        "decode_instances": Decimal(2),
                        "family_metadata_provenance": _readme(),
                        **context,
                    }
                ),
                "source_pointer": {"json_pointer": pointer, "model_provenance": _readme()},
            },
        )

    def measurement(
        self,
        experiment: RagItem,
        pointer: str,
        metric: str,
        statistic: str | None,
        value: object,
        unit: str,
        *,
        signed: bool = False,
        series: dict[str, JsonValue] | None = None,
    ) -> None:
        if value is None:
            return  # The experiment explicitly retains nullable metric leaves.
        number = _number(value, count=unit == "count", signed=signed)
        normalized = normalize_unit(number, unit)
        self._item(
            pointer,
            "measurement",
            experiment.item_id,
            {
                "kind": "measurement",
                "experiment_id": experiment.item_id,
                "metric": metric,
                "statistic": statistic,
                "value": number,
                "original_value": number,
                "unit": unit,
                "original_unit": unit,
                "evidence_type": "raw_json",
                "series": series,
                "source_pointer": {
                    "json_pointer": pointer,
                    "metric_label": metric,
                    "statistic_label": statistic,
                    "unit_provenance": (
                        "JSON metric/statistic key: count denotes sample/record count; "
                        "_ms milliseconds; _tok_s output tokens/s; _ktok_s kilo-input-tokens/s; "
                        "_rps requests/s; _pct percent"
                    ),
                    "semantics_provenance": _readme(),
                },
                **(
                    {
                        "normalized_value": normalized[0],
                        "normalized_unit": normalized[1],
                        "normalization_rule": normalized[2],
                    }
                    if normalized is not None
                    else {}
                ),
            },
        )

    def arm(
        self,
        value: object,
        pointer: str,
        run: str,
        source_record: dict[str, JsonValue],
    ) -> None:
        row = _object(value, _COUNTS | set(_RATES) | _LATENCIES | {"run", "window_s"})
        if row["run"] != run:
            raise PublicationError("unsupported run identity/order")
        if row["window_s"] is not None and _number(row["window_s"]) != Decimal(300):
            raise PublicationError("unsupported admission window")
        nulls: list[str] = []
        for metric in sorted(_COUNTS | set(_RATES)):
            if row[metric] is None:
                nulls.append(_pointer(pointer, metric))
        for metric in sorted(_LATENCIES):
            for stat, number in _object(row[metric], _LATENCY_STATS).items():
                if number is None:
                    nulls.append(_pointer(pointer, metric, stat))
        experiment = self.experiment(
            pointer,
            {
                "evidence_role": "exported_arm_summary",
                "run": run,
                "window_s": row["window_s"],
                "source_record": source_record,
                "null_measurements": nulls,
                "reported_table_status": (
                    "excluded: approximate producer mismatch; does not isolate P2P"
                    if source_record["file"] == "complete-policy-source-exact-window.json" and run == "approx-p2p"
                    else "not used in reported table"
                    if source_record["file"] == "complete-policy-source-exact-window.json" and run == "precise-no-p2p"
                    else "reported"
                ),
            },
        )
        for metric in sorted(_COUNTS | set(_RATES)):
            self.measurement(
                experiment,
                _pointer(pointer, metric),
                metric,
                None,
                row[metric],
                "count" if metric in _COUNTS else _RATES[metric],
            )
        for metric in sorted(_LATENCIES):
            for stat, number in _object(row[metric], _LATENCY_STATS).items():
                self.measurement(
                    experiment,
                    _pointer(pointer, metric, stat),
                    metric,
                    stat,
                    number,
                    "count" if stat == "count" else "ms",
                )

    def summary(self, value: object, pointer: str, role: str, context: dict[str, object]) -> None:
        metrics = _object(value, set(_SUMMARY_METRICS))
        nulls: list[str] = []
        for metric, stats in metrics.items():
            for stat, number in _object(stats, _SUMMARY_STATS).items():
                if number is None:
                    nulls.append(_pointer(pointer, metric, stat))
        experiment = self.experiment(
            pointer,
            {
                **context,
                "evidence_role": role,
                "null_measurements": nulls,
                "independence": "derived from the same three pairs; not new trials",
            },
        )
        for metric, stats in metrics.items():
            for stat, number in _object(stats, _SUMMARY_STATS).items():
                self.measurement(
                    experiment,
                    _pointer(pointer, metric, stat),
                    metric,
                    stat,
                    number,
                    "%"
                    if role == "paired_delta_aggregate" or stat == "population_cv_pct"
                    else _SUMMARY_METRICS[metric],
                    signed=role == "paired_delta_aggregate" and stat != "population_cv_pct",
                    series={"summary_role": role, "base_metric": metric},
                )

    def aggregate(self, value: object) -> None:
        root = _object(value, {"config_aggregates", "method", "paired_delta_aggregates_pct", "pairs"})
        if root["method"] != _METHOD:
            raise PublicationError("unsupported aggregate method")
        configs = _object(root["config_aggregates"], {"approximate_no_p2p", "precise_p2p"})
        for config, metrics in configs.items():
            self.summary(
                metrics,
                _pointer("", "config_aggregates", config),
                "config_aggregate",
                {
                    "configuration_label": config,
                    "method": root["method"],
                },
            )
        self.summary(
            root["paired_delta_aggregates_pct"],
            "/paired_delta_aggregates_pct",
            "paired_delta_aggregate",
            {"method": root["method"]},
        )
        originals = (
            ("complete-policy-source-exact-window.json", (0, 3)),
            ("repeat-approx-first-exact-window.json", (0, 1)),
            ("repeat-precise-first-exact-window.json", (0, 1)),
        )
        for index, value in enumerate(_array(root["pairs"], 3)):
            pointer = _pointer("", "pairs", index)
            pair = _object(value, {"pair", "approximate", "precise_p2p", "precise_vs_approximate_pct"})
            if _number(pair["pair"], count=True) != Decimal(index + 1):
                raise PublicationError("unsupported pair identity/order")
            original, indices = originals[index]
            for label, original_index in zip(("approximate", "precise_p2p"), indices, strict=True):
                self.arm(
                    pair[label],
                    _pointer(pointer, label),
                    _RUNS[original][original_index],
                    {
                        "file": original,
                        "path": _DIRECTORY + original,
                        "json_pointer": _pointer("", original_index),
                        "relationship": "copy of source arm, not an independent trial",
                    },
                )
            deltas = _object(pair["precise_vs_approximate_pct"], set(_SUMMARY_METRICS))
            experiment = self.experiment(
                _pointer(pointer, "precise_vs_approximate_pct"),
                {
                    "evidence_role": "paired_change",
                    "pair": pair["pair"],
                    "method": root["method"],
                    "comparison": "precise_p2p versus approximate",
                    "formula": "100 * (precise_p2p / approximate - 1)",
                    "null_measurements": [
                        _pointer(pointer, "precise_vs_approximate_pct", metric)
                        for metric, number in deltas.items()
                        if number is None
                    ],
                },
            )
            for metric, number in deltas.items():
                self.measurement(
                    experiment,
                    _pointer(pointer, "precise_vs_approximate_pct", metric),
                    metric,
                    "paired_change",
                    number,
                    "%",
                    signed=True,
                    series={"pair": str(pair["pair"]), "base_metric": metric},
                )


def parse_custom_json(
    file: InventoryFile,
    revision: str,
    data: bytes,
    *,
    reviewed_metadata_revision: str | None = None,
) -> tuple[RagItem, ...]:
    """Parse a reviewed fixed-window export, failing closed on revision/schema drift.

    Decimal values in context are strings; measurement values retain exact JSON
    precision. Null metric leaves are recorded as JSON pointers in their
    experiment's ``null_measurements`` instead of being invented as zero.
    Noninitial revisions require an operator to review existing hardcoded
    model/hardware/workload metadata against the exact commit and pass it as
    ``reviewed_metadata_revision``. None retains the initial revision guard;
    this attestation never relaxes schema checks or infers new metadata.
    """
    if reviewed_metadata_revision is not None and reviewed_metadata_revision != revision:
        raise PublicationError("reviewed metadata revision must match the exact source revision")
    if revision != _METADATA_REVISION and reviewed_metadata_revision != revision:
        raise PublicationError(
            "unreviewed custom JSON revision; associated README metadata requires compatibility review"
        )
    validate_revision(LLM_D_ORIGIN, revision)
    if file.format != "json" or hashlib.sha256(data).hexdigest() != file.sha256:
        raise PublicationError("custom JSON format or SHA-256 mismatch")
    if not file.path.startswith(_DIRECTORY):
        raise PublicationError("unsupported custom JSON file family")
    name = file.path.removeprefix(_DIRECTORY)
    if name not in _RUNS and name != _AGGREGATE:
        raise PublicationError("unsupported custom JSON filename")
    try:
        value = json.loads(
            data,
            parse_int=Decimal,
            parse_float=Decimal,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, DecimalException) as error:
        raise PublicationError("invalid custom JSON") from error
    adapter = _Adapter(file, revision)
    if name == _AGGREGATE:
        adapter.aggregate(value)
    else:
        for index, (row, run) in enumerate(zip(_array(value, len(_RUNS[name])), _RUNS[name], strict=True)):
            adapter.arm(
                row,
                _pointer("", index),
                run,
                {
                    "file": name,
                    "path": file.path,
                    "json_pointer": _pointer("", index),
                    "relationship": "source arm; may also appear in aggregate",
                },
            )
    adapter._item(
        "",
        "limitations",
        adapter.file_id,
        {
            "kind": "passage",
            "passage_text": _LIMITATIONS,
            "heading_path": ["GLM-5.2 C64 fixed-window data", "Evidence limitations"],
            "source_pointer": {"json_pointer": "", "metadata_provenance": _readme()},
        },
    )
    return tuple(adapter.items)
