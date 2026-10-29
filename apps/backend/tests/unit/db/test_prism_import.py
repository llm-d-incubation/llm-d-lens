from __future__ import annotations

import hashlib
import json
import zipfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import sessionmaker

from llm_d_lens.benchmark_rag.prism import PARSER_VERSION, PrismSnapshotImporter
from llm_d_lens.benchmark_rag.prism_initial import INITIAL_EXPERIMENTS, INITIAL_FILES, INITIAL_REVISION
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError, SourceApproval
from llm_d_lens.benchmark_rag.sources import PRISM_ORIGIN, allowed_paths
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.models.public_benchmark_record import PublicBenchmarkRecordRow as RagItemRow

_SNAPSHOT = Path(__file__).resolve().parents[5] / "private/prism-public-2026-10-13"
_ARCHIVE = "archives/archived_drive_data.json"
_MERGED = "historical-merged-deduplicated.json"
_BUNDLE = "results/json/test-run.json"
_STAGE_NAME = "reports/benchmark_report_v0.2,_stage_0_lifecycle_metrics.json.yaml"
_SUMMARY_NAME = "reports/report_v0.2.json"


def _approval() -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated acceptance test",
        approval_reference="local-only fixture; no public endpoint or business database",
        license_reference="fixture approval only; production source approval remains explicit",
        parser_version=PARSER_VERSION,
        allowed_paths=allowed_paths(PRISM_ORIGIN),
    )


@pytest.fixture
def importer(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PrismSnapshotImporter:
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "retained-evidence"))
    dao = RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))
    return PrismSnapshotImporter(RagPublicationService(dao=dao))


def _write(root: Path, path: str, value: object) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")


def _read(root: Path, path: str):
    return json.loads((root / path).read_bytes())


def _zip(root: Path) -> None:
    path = root / "results/zip/test-run.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = _read(root, _BUNDLE)
    with zipfile.ZipFile(path, "w") as archive:
        for index, entry in enumerate(bundle["entries"]):
            archive.writestr(
                f"export/benchmark_report_v0.2,_stage_{index}_lifecycle_metrics.json.yaml",
                json.dumps(entry["raw_report"]),
            )


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    archived = {
        "id": "historical",
        "model_name": "example-model",
        "timestamp": "20250923-201033",
        "metadata": {"hardware": "reported-hardware"},
        "workload": {"concurrency": None},
        "source_info": {"file_identifier": "original-history.json"},
        "metrics": {
            "throughput": 6131.06177725188,
            "output_tput": 6131.06177725188,
            "latency": {"mean": 12, "p50": 0},
            "e2e_latency": 12,
        },
    }
    load = {"stage": 0, "rate_qps": 6, "input_seq_len": {"value": 100}, "output_seq_len": {"value": 200}}
    report = {
        "version": "0.2",
        "run": {"uid": "stage"},
        "scenario": {"load": {"standardized": load}},
        "results": {
            "request_performance": {
                "aggregate": {
                    "latency": {
                        "request_latency": {"mean": 0.12345678901234567, "units": "s"},
                        "time_per_output_token": {"mean": 0.002, "units": "s/token"},
                    },
                    "throughput": {
                        "output_token_rate": {"mean": 5, "units": "tokens/s"},
                        "request_rate": {"mean": 4, "units": "queries/s"},
                    },
                    "requests": {"failures": 0, "total": 10},
                }
            }
        },
    }
    summary = deepcopy(report)
    summary["run"]["uid"] = "summary"
    bundle = {
        "runId": "test-run",
        "submitted_at": "2026-10-13T12:00:00Z",
        "model_name": "example-model",
        "entries": [
            {"filename": _STAGE_NAME, "run_id": "stage-entry", "raw_report": report},
            {"filename": _SUMMARY_NAME, "run_id": "summary-entry", "raw_report": summary},
        ],
    }
    normalized = {
        "id": "prism:test-run:stage:0",
        "run_id": "test-run",
        "source": "prism-results-store",
        "timestamp": bundle["submitted_at"],
        "model_name": bundle["model_name"],
        "metadata": {},
        "workload": {"input_tokens": 100, "output_tokens": 200, "target_qps": 6, "concurrency": None},
        "source_info": {"file_identifier": _STAGE_NAME, "stage_run_id": "stage-entry", "timestamp_kind": "submission"},
    }
    _write(root, _ARCHIVE, [archived])
    _write(root, "archives/llm-d-benchmarks.json", [])
    _write(root, _MERGED, [archived, normalized])
    _write(root, _BUNDLE, bundle)
    _write(
        root, "results/public-index.json", {"items": [{"runId": "test-run", "state": "public"}], "nextPageToken": None}
    )
    _write(root, "results/promoted-index.json", {"items": [], "nextPageToken": None})
    (root / "normalize-and-merge.jq").write_text("# synthetic normalization fixture\n")
    _zip(root)
    return root


def test_snapshot_preserves_exact_values_context_and_timestamp_semantics(
    importer: PrismSnapshotImporter,
    snapshot: Path,
):
    first = importer.import_snapshot(snapshot, INITIAL_REVISION, _hashes(snapshot), _approval(), expected_experiments=2)
    assert first.counts == {"source": 8, "experiment": 2, "measurement": 9, "passage": 1}
    experiments = importer.service.dao.list_published(PRISM_ORIGIN, kind="experiment")
    historical = next(item for item in experiments if item.source_path == _ARCHIVE)
    stage = next(item for item in experiments if item.source_path == _BUNDLE)
    assert historical.timestamp is None and historical.timestamp_kind is None
    assert historical.configuration["timestamp"] == "20250923-201033"
    assert stage.timestamp_kind == "submission" and stage.timestamp.isoformat() == "2026-10-13T12:00:00+00:00"
    assert stage.workload == {"input_tokens": 100, "output_tokens": 200, "target_qps": 6, "concurrency": None}
    measurements = importer.service.dao.list_published(PRISM_ORIGIN, kind="measurement")
    throughput = next(
        item for item in measurements if item.experiment_id == historical.item_id and item.metric == "throughput"
    )
    assert throughput.value == Decimal("6131.06177725188") and throughput.unit == "output tokens/s"
    assert throughput.statistic is None
    assert throughput.source_pointer["alias_json_pointers"] == ["/0/metrics/output_tput"]
    tpot = next(item for item in measurements if item.experiment_id == stage.item_id and item.metric == "tpot")
    assert (tpot.value, tpot.unit, tpot.normalized_value, tpot.normalized_unit) == (
        Decimal("0.002"),
        "s/token",
        Decimal("2"),
        "ms/token",
    )
    summaries = importer.service.dao.list_published(PRISM_ORIGIN, kind="passage")
    assert summaries[0].source_pointer["original_report"] == _SUMMARY_NAME
    assert summaries[0].source_pointer["original_zip_member"].endswith("_stage_1_lifecycle_metrics.json.yaml")
    repeat = importer.import_snapshot(
        snapshot, INITIAL_REVISION, _hashes(snapshot), _approval(), expected_experiments=2
    )
    assert not repeat.changed and repeat.manifest_id == first.manifest_id and repeat.generation == 1


@pytest.mark.parametrize("dependency", ["merged-metadata", "original-zip"])
def test_snapshot_refresh_reparses_unchanged_payload_dependencies(
    importer: PrismSnapshotImporter, snapshot: Path, postgres_engine: Engine, dependency: str
):
    original_hashes = _hashes(snapshot)
    first = importer.import_snapshot(snapshot, INITIAL_REVISION, original_hashes, _approval(), expected_experiments=2)
    old_items = importer.service.dao.list_published(PRISM_ORIGIN)
    old_experiment = next(item for item in old_items if item.kind == "experiment" and item.source_path == _BUNDLE)
    old_measurement = next(
        item for item in old_items if item.kind == "measurement" and item.experiment_id == old_experiment.item_id
    )
    old_payload = (snapshot / _BUNDLE).read_bytes()
    zip_path = "results/zip/test-run.zip"
    if dependency == "merged-metadata":
        merged = _read(snapshot, _MERGED)
        merged[1]["metadata"] = {"hardware": "corrected-hardware", "backend": "corrected-runtime"}
        _write(snapshot, _MERGED, merged)
    else:
        with zipfile.ZipFile(snapshot / zip_path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(snapshot / zip_path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name.replace("export/", "corrected-export/"), data)
    hashes = _hashes(snapshot)
    assert hashes[_BUNDLE] == original_hashes[_BUNDLE]
    assert (snapshot / _BUNDLE).read_bytes() == old_payload
    staged = importer.stage(snapshot, "2026-10-14", hashes, _approval(), expected_experiments=2)
    assert importer.service.dao.head(PRISM_ORIGIN).registry.published_manifest_id == first.manifest_id
    assert importer.service.dao.list_published(PRISM_ORIGIN) == old_items
    refreshed = importer.service.activate(PRISM_ORIGIN, staged.item_id)
    assert refreshed.counts == first.counts and refreshed.generation == first.generation + 1
    current = importer.service.dao.list_published(PRISM_ORIGIN)
    experiment = next(item for item in current if item.kind == "experiment" and item.source_path == _BUNDLE)
    assert experiment.item_id != old_experiment.item_id
    assert experiment.source_revision == "2026-10-14"
    assert experiment.content_sha256 == old_experiment.content_sha256
    assert experiment.source_pointer["merged_sha256"] == hashes[_MERGED]
    assert experiment.source_pointer["original_zip_sha256"] == hashes[zip_path]
    if dependency == "merged-metadata":
        assert experiment.hardware == "corrected-hardware"
        assert experiment.runtime == "corrected-runtime"
        assert experiment.configuration["metadata"] == merged[1]["metadata"]
    else:
        assert experiment.source_pointer["original_zip_member"].startswith("corrected-export/")
    measurements = [item for item in current if item.kind == "measurement" and item.experiment_id == experiment.item_id]
    assert measurements
    assert all(item.source_revision == experiment.source_revision for item in measurements)
    assert all(item.source_pointer["merged_sha256"] == hashes[_MERGED] for item in measurements)
    assert all(item.source_pointer["original_zip_sha256"] == hashes[zip_path] for item in measurements)
    assert importer.service.dao.get(old_experiment.item_id) == old_experiment
    assert importer.service.dao.get(old_measurement.item_id) == old_measurement
    assert importer.service.archive.read(old_experiment.content_sha256) == old_payload
    for path in (_MERGED, zip_path):
        retained = importer.service.archive.read(original_hashes[path])
        assert hashlib.sha256(retained).hexdigest() == original_hashes[path]
    with postgres_engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(RagItemRow))
    repeated = importer.import_snapshot(snapshot, "2026-10-14", hashes, _approval(), expected_experiments=2)
    assert not repeated.changed
    assert repeated.manifest_id == refreshed.manifest_id and repeated.generation == refreshed.generation
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(RagItemRow)) == count
    importer.import_snapshot(snapshot, "2026-10-15", hashes, _approval(), expected_experiments=2)
    assert importer.service.dao.list_published(PRISM_ORIGIN) == current


@pytest.mark.parametrize(
    "failure",
    [
        "pagination",
        "duplicate-id",
        "aliases",
        "unknown-metric",
        "invalid-number",
        "missing-stage",
        "wrong-workload",
        "wrong-version",
        "missing-zip",
        "duplicate-json-key",
        "nonfinite",
        "statistic-aliases",
        "zip-report-mismatch",
        "timezone",
        "count",
    ],
)
def test_invalid_refresh_preserves_old_release(
    importer: PrismSnapshotImporter,
    snapshot: Path,
    failure: str,
):
    old = importer.import_snapshot(snapshot, INITIAL_REVISION, _hashes(snapshot), _approval(), expected_experiments=2)
    if failure == "pagination":
        _write(snapshot, "results/public-index.json", {"items": [], "nextPageToken": "unfinished"})
    elif failure in ("duplicate-id", "missing-stage", "wrong-workload"):
        merged = _read(snapshot, _MERGED)
        if failure == "duplicate-id":
            merged.append(merged[0])
        elif failure == "missing-stage":
            merged.pop()
        else:
            merged[1]["workload"]["concurrency"] = 1
        _write(snapshot, _MERGED, merged)
    elif failure in ("aliases", "unknown-metric", "invalid-number"):
        archives = _read(snapshot, _ARCHIVE)
        metrics = archives[0]["metrics"]
        if failure == "aliases":
            metrics["output_tput"] = 1
        elif failure == "unknown-metric":
            metrics["unsupported"] = 1
        else:
            metrics["latency"]["mean"] = True
        _write(snapshot, _ARCHIVE, archives)
        merged = _read(snapshot, _MERGED)
        merged[0] = archives[0]
        _write(snapshot, _MERGED, merged)
    elif failure == "wrong-version":
        bundle = _read(snapshot, _BUNDLE)
        bundle["entries"][0]["raw_report"]["version"] = "0.3"
        _write(snapshot, _BUNDLE, bundle)
        _zip(snapshot)
    elif failure == "missing-zip":
        (snapshot / "results/zip/test-run.zip").unlink()
    elif failure == "duplicate-json-key":
        (snapshot / _MERGED).write_text('[{"id":"first","id":"second"}]')
    elif failure == "nonfinite":
        (snapshot / _ARCHIVE).write_text('[{"id":"historical","metrics":{"throughput":NaN}}]')
    elif failure in ("statistic-aliases", "zip-report-mismatch", "timezone"):
        bundle = _read(snapshot, _BUNDLE)
        if failure == "statistic-aliases":
            bundle["entries"][0]["raw_report"]["results"]["request_performance"]["aggregate"]["latency"][
                "request_latency"
            ]["avg"] = 999
        elif failure == "zip-report-mismatch":
            bundle["entries"][0]["raw_report"]["run"]["uid"] = "not-the-exported-report"
        else:
            bundle["submitted_at"] = "2026-10-13T12:00:00"
            merged = _read(snapshot, _MERGED)
            merged[1]["timestamp"] = bundle["submitted_at"]
            _write(snapshot, _MERGED, merged)
        _write(snapshot, _BUNDLE, bundle)
        if failure != "zip-report-mismatch":
            _zip(snapshot)
    expected = 3 if failure == "count" else 2
    with pytest.raises(PublicationError):
        importer.import_snapshot(snapshot, "2026-10-14", _hashes(snapshot), _approval(), expected_experiments=expected)
    assert importer.service.dao.head(PRISM_ORIGIN).registry.published_manifest_id == old.manifest_id
    assert importer.service.dao.count_published(PRISM_ORIGIN, kind="experiment") == 2


def test_unknown_or_missing_raw_units_are_not_inferred(importer: PrismSnapshotImporter, snapshot: Path):
    bundle = _read(snapshot, _BUNDLE)
    performance = bundle["entries"][0]["raw_report"]["results"]["request_performance"]["aggregate"]
    performance["throughput"]["output_token_rate"]["units"] = "widgets/hour"
    del performance["latency"]["request_latency"]["units"]
    _write(snapshot, _BUNDLE, bundle)
    _zip(snapshot)
    importer.import_snapshot(snapshot, INITIAL_REVISION, _hashes(snapshot), _approval(), expected_experiments=2)
    measurements = importer.service.dao.list_published(PRISM_ORIGIN, kind="measurement")
    throughput = next(item for item in measurements if item.source_path == _BUNDLE and item.metric == "throughput")
    assert throughput.unit == "widgets/hour" and throughput.normalized_value is None
    latency = next(item for item in measurements if item.source_path == _BUNDLE and item.metric == "latency")
    assert latency.unit is None and latency.normalized_value is None


@pytest.mark.parametrize("archive_alias", [False, True])
def test_statistic_aliases_do_not_duplicate_measurements(
    importer: PrismSnapshotImporter,
    snapshot: Path,
    archive_alias: bool,
):
    if archive_alias:
        archives = _read(snapshot, _ARCHIVE)
        distribution = archives[0]["metrics"]["latency"]
        distribution["avg"] = distribution.pop("mean")
        _write(snapshot, _ARCHIVE, archives)
        merged = _read(snapshot, _MERGED)
        merged[0] = archives[0]
        _write(snapshot, _MERGED, merged)
    bundle = _read(snapshot, _BUNDLE)
    latency = bundle["entries"][0]["raw_report"]["results"]["request_performance"]["aggregate"]["latency"][
        "request_latency"
    ]
    latency["avg"] = latency["mean"]
    _write(snapshot, _BUNDLE, bundle)
    _zip(snapshot)
    result = importer.import_snapshot(
        snapshot, INITIAL_REVISION, _hashes(snapshot), _approval(), expected_experiments=2
    )
    assert result.counts["measurement"] == 9
    measurements = [
        item
        for item in importer.service.dao.list_published(PRISM_ORIGIN, kind="measurement")
        if item.source_path == _BUNDLE and item.metric == "latency"
    ]
    assert len(measurements) == 1 and measurements[0].statistic == "mean"
    assert measurements[0].source_pointer["statistic_original"] == "mean"
    assert measurements[0].source_pointer["statistic_alias_json_pointers"][0].endswith("/avg")


@pytest.mark.skipif(not _SNAPSHOT.is_dir(), reason="local Git-ignored initial Prism snapshot is required")
def test_initial_snapshot_publishes_249_experiments_and_round_trips_every_measurement(
    importer: PrismSnapshotImporter,
    postgres_engine: Engine,
):
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "agentic_benchmark_records" in statement.lower():
            statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        result = importer.import_snapshot(
            _SNAPSHOT, INITIAL_REVISION, INITIAL_FILES, _approval(), expected_experiments=INITIAL_EXPERIMENTS
        )
        assert result.counts == {"source": 22, "experiment": 249, "measurement": 9060, "passage": 4}
        experiments = importer.service.dao.list_published(PRISM_ORIGIN, kind="experiment")
        assert len(experiments) == 249
        assert sum(item.source_path in (_ARCHIVE, "archives/llm-d-benchmarks.json") for item in experiments) == 177
        assert sum(item.timestamp_kind == "submission" for item in experiments) == 72
        assert len({item.source_pointer["record_id"] for item in experiments}) == 249
        assert result.counts["source"] == 22 and result.counts["passage"] == 4
        documents = {
            path: json.loads((_SNAPSHOT / path).read_bytes(), parse_float=Decimal)
            for path in INITIAL_FILES
            if path.endswith(".json")
        }
        measurements = importer.service.dao.list_published(PRISM_ORIGIN, kind="measurement")
        for measurement in measurements:
            value = documents[measurement.source_path]
            for component in measurement.source_pointer["json_pointer"].lstrip("/").split("/"):
                component = component.replace("~1", "/").replace("~0", "~")
                value = value[int(component)] if isinstance(value, list) else value[component]
            assert measurement.value == Decimal(value) == measurement.original_value
            assert measurement.content_sha256 == INITIAL_FILES[measurement.source_path]
        target = next(
            item for item in experiments if item.source_pointer["record_id"] == "02c8f038-1cad-4ee8-a72e-724affa923f4"
        )
        target_values = [
            item for item in measurements if item.experiment_id == target.item_id and item.metric == "throughput"
        ]
        assert len(target_values) == 1 and target_values[0].value == Decimal("6131.06177725188")
        assert target_values[0].unit == "output tokens/s"
        with postgres_engine.connect() as connection:
            before = connection.execute(select(func.count()).select_from(RagItemRow)).scalar_one()
        repeat = importer.import_snapshot(
            _SNAPSHOT, INITIAL_REVISION, INITIAL_FILES, _approval(), expected_experiments=INITIAL_EXPERIMENTS
        )
        assert (
            not repeat.changed and repeat.manifest_id == result.manifest_id and repeat.generation == result.generation
        )
        with postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(RagItemRow)).scalar_one() == before
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert statements == []
