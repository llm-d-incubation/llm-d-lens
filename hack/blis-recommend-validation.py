"""Re-run the archived aggregated recommendation cases with horizon validation."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import hashlib
import importlib.metadata
import io
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from aisimulate.legacy_cli.api import cli_recommend
from llm_d_lens.estimate_tools.aic.service import _plain
from llm_d_lens.estimate_tools.blis import service as blis
from llm_d_lens.estimate_tools.models import Problem

logger = logging.getLogger(__name__)


def encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, default=encode, ensure_ascii=True, allow_nan=False, indent=2)
        + "\n"
    )


async def run(args) -> None:
    source = json.loads(args.source.read_text())["cross_version_experiment"]
    profile = blis.RuntimeProfile.model_validate(source["profile"])
    provenance = {
        "source_sha256": digest(args.source),
        "implementation_sha256": digest(Path(blis.__file__)),
        "driver_sha256": digest(Path(__file__)),
        "aisimulate_version": importlib.metadata.version("aisimulate"),
    }
    if args.output.exists():
        data = json.loads(args.output.read_text())
        if data["provenance"] != provenance:
            raise ValueError(
                "Cannot resume results produced with different inputs or code"
            )
    else:
        data = {
            "created_at": datetime.now(UTC).isoformat(),
            "provenance": provenance,
            "profile": profile.model_dump(mode="json"),
            "policy": {
                "screening_requests": 256,
                "screening_horizon_s": 900,
                "validation_requests": 0,
                "validation_horizon_s": 2000,
                "validation_min_throughput_fraction": 0.99,
                "phases": False,
                "minimum_measured_duration_fraction": 0.99,
                "completion_rate_formula": "completed / (horizon_s - mean_e2e_s), to allow normal in-flight work",
                "search_wall_budget_s": 1200,
                "search_max_evaluations": 256,
                "limitations": [
                    "All validation metrics include startup and horizon censoring",
                    "No warmup, steady-state window, drain, or backlog-slope certification",
                    "Economic recommendations are limited to screened candidate families",
                    "AIC cross-evaluation uses BLIS, not independent hardware ground truth",
                ],
            },
            "cases": [],
        }
    raw_path = args.output.with_suffix(".raw.jsonl.gz")
    original_execute = blis._execute_bundle
    current = {}

    async def capture(request, runtime, destination, *positional, **kwargs):
        execution = await original_execute(
            request, runtime, destination, *positional, **kwargs
        )
        record = {
            **current,
            "stage": "validation" if request.run.num_requests == 0 else "screening",
            "request": request.model_dump(mode="json"),
            "execution": execution,
            "files": {},
        }
        for name in (
            "stdout.log",
            "stderr.log",
            "manifest.json",
            "effective-input.json",
        ):
            path = destination / name
            if path.exists():
                record["files"][name] = path.read_text(errors="replace")
        with gzip.open(raw_path, "at", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=encode, allow_nan=False) + "\n")
        return execution

    blis._execute_bundle = capture
    limits = blis._SearchLimits(
        max_evaluations=256, parallelism=4, wall_duration_s=1200
    )
    completed = {record["case"]["id"] for record in data["cases"]}
    try:
        with TemporaryDirectory(prefix="blis-recommend-validation-") as temporary:
            directory = Path(temporary)
            for old in source["cases"]:
                case = old["case"]
                if case["id"] in completed or (
                    args.cases and case["id"] not in args.cases
                ):
                    continue
                record = {"case": case}
                for kind in ("blis_native", "blis_controlled"):
                    payload = old[kind]["request"]
                    request = blis._JointSweepRequest.model_validate(payload)
                    request.run.num_requests = 256
                    request.run.horizon_s = 900
                    request.budget.max_evaluations = 256
                    request.budget.wall_duration_s = 1200
                    current.update(case_id=case["id"], origin=kind)
                    started = time.monotonic()
                    result = await blis._search_joint(
                        request,
                        profile,
                        directory,
                        asyncio.Event(),
                        limits=limits,
                    )
                    record[kind] = {
                        "request": request.model_dump(mode="json"),
                        "elapsed_s": time.monotonic() - started,
                        "result": result,
                    }
                    if isinstance(result, Problem):
                        print(case["id"], kind, result.code, flush=True)
                    else:
                        best = (
                            result.recommendations[0]
                            if result.recommendations
                            else None
                        )
                        print(
                            case["id"],
                            kind,
                            "GPUs",
                            best.device_count if best else None,
                            "screened",
                            len(result.points),
                            "validated",
                            len(result.validation_points),
                            "complete",
                            result.validation_complete,
                            flush=True,
                        )
                log = io.StringIO()
                started = time.monotonic()
                try:
                    with (
                        contextlib.redirect_stdout(log),
                        contextlib.redirect_stderr(log),
                    ):
                        native = cli_recommend(**old["aic"]["request"])
                    rows = {
                        mode: _plain(frame.to_dict(orient="records"))
                        for mode, frame in native.best_configs.items()
                    }
                    eligible = [
                        row
                        for row in rows.get("agg", [])
                        if row.get("pp", 1) == 1
                        and row.get("dp", 1) == 1
                        and row["ttft"] <= case["ttft_ms"]
                        and row["tpot"] <= case["tpot_ms"]
                        and row["num_total_gpus"] * row.get("replicas_needed", 1)
                        <= source["gpu_budget"]
                    ]
                    selected = eligible[0] if eligible else None
                    record["aic"] = {
                        "request": old["aic"]["request"],
                        "rows": rows,
                        "selected_agg": selected,
                        "elapsed_s": time.monotonic() - started,
                        "log": log.getvalue(),
                    }
                    if selected is not None:
                        validation = blis._JointSweepRequest.model_validate(
                            old["blis_controlled"]["request"]
                        )
                        role = validation.configuration.roles[0]
                        role.tp = int(selected["tp"])
                        role.replicas = int(selected.get("replicas_needed", 1))
                        validation.space.tensor_parallel = [role.tp]
                        validation.space.replicas = [role.replicas]
                        validation.run.num_requests = 0
                        validation.run.horizon_s = 2000
                        validation.constraints.require_complete = False
                        validation.constraints.max_failure_fraction = None
                        validation.budget.max_evaluations = 1
                        validation.budget.wall_duration_s = 1200
                        for family, formula, statistic, threshold in (
                            ("ttft", "ttft", "mean", case["ttft_ms"]),
                            ("itl", "tpot_excl_first", "mean", case["tpot_ms"]),
                        ):
                            validation.constraints.metrics.append(
                                blis.MetricConstraint(
                                    metric=blis.MetricDefinition(
                                        name=f"{family}_{statistic}_ms",
                                        unit="ms",
                                        statistic=statistic,
                                        formula=formula,
                                        population="per_request",
                                        window="simulated:first_arrival_after_warmup:last_completion",
                                    ),
                                    comparison="le",
                                    threshold=threshold,
                                )
                            )
                        current.update(case_id=case["id"], origin="aic")
                        checked = await blis._search_grid(
                            validation,
                            profile,
                            directory,
                            asyncio.Event(),
                            limits=limits,
                        )
                        record["aic"]["validation"] = checked
                        record["aic"]["validation_passed"] = (
                            not isinstance(checked, Problem)
                            and bool(checked.points)
                            and blis._assess_joint_validation(
                                checked.points[0], validation
                            )
                        )
                except Exception as error:
                    logger.exception("AIC validation failed for case %s", case["id"])
                    record["aic"] = {"error": str(error), "log": log.getvalue()}
                data["cases"].append(json.loads(json.dumps(record, default=encode)))
                save(args.output, data)
                print(
                    case["id"],
                    "saved; AIC validation",
                    record["aic"].get("validation_passed"),
                    flush=True,
                )
    finally:
        blis._execute_bundle = original_execute
    if raw_path.exists():
        data["raw_archive"] = str(raw_path)
        data["raw_archive_sha256"] = digest(raw_path)
    save(args.output, data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="*")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
