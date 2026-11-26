# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Engine replay estimation/search and separately named legacy experiment support."""

from __future__ import annotations

import asyncio
import logging
import math
from time import monotonic
from typing import Any

import yaml

from .models import (
    AICEstimateRequest,
    AICExperimentRequest,
    AICExperimentResponse,
    AICRequest,
    AICSearchResponse,
    AICSupportResponse,
)

logger = logging.getLogger(__name__)


class AICError(RuntimeError):
    """An AISimulate failure with an optional safe, actionable UI explanation."""

    def __init__(self, message: str, *, public: bool = False):
        super().__init__(message)
        self.public_message = message if public else None


class _SearchBudgetExceededError(Exception):
    pass


def _cli():
    try:
        from aisimulate.legacy_cli import cli_default, cli_exp, cli_support
    except ImportError as error:
        raise AICError(
            "AISimulate is unavailable; install the project dependencies with python -m pip install -e .",
            public=True,
        ) from error
    except Exception as error:
        logger.exception("AISimulate initialization failed")
        raise AICError(
            "AISimulate could not initialize. Check the backend dependency installation and server logs.",
            public=True,
        ) from error
    return cli_default, cli_exp, cli_support


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _support_flags(result: Any) -> tuple[bool, bool]:
    agg = getattr(result, "agg_supported", None)
    disagg = getattr(result, "disagg_supported", None)
    if agg is not None or disagg is not None:
        return bool(agg), bool(disagg)
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), bool(result[1])
    raise AICError("AISimulate returned an unsupported capability result")


def check_support_sync(request: AICRequest) -> AICSupportResponse:
    _cli_default, _cli_exp, cli_support = _cli()
    try:
        result = cli_support(
            model_path=request.model_name,
            system=request.aic_system_name,
            backend=request.aic_backend_name.lower(),
        )
        agg_supported, disagg_supported = _support_flags(result)
    except Exception as error:
        raise AICError(str(error)) from error
    supported = agg_supported or disagg_supported
    return AICSupportResponse(
        supported=supported,
        agg_supported=agg_supported,
        disagg_supported=disagg_supported,
        reason=None
        if supported
        else (f"{request.model_name} is not supported on {request.aic_system_name} with {request.aic_backend_name}"),
        constraints={"gpu_count": request.gpu_count},
    )


async def check_support(request: AICRequest) -> AICSupportResponse:
    return await asyncio.to_thread(check_support_sync, request)


def _candidate(mode: str, row: dict[str, Any]) -> dict[str, Any]:
    # Native experiment keys are user-defined names, not serving modes.
    if mode not in {"agg", "disagg"}:
        if "(p)tp" in row and "(d)tp" in row:
            mode = "disagg"
        elif "tp" in row:
            mode = "agg"
        else:
            raise AICError("AISimulate returned a result without a serving topology", public=True)
    topology_fields = (
        ("tp", "num_total_gpus") if mode == "agg" else ("(p)tp", "(d)tp", "(p)workers", "(d)workers", "num_total_gpus")
    )
    for field in topology_fields:
        if row.get(field) is not None:
            value = float(row[field])
            if not math.isfinite(value) or not value.is_integer() or value < 1:
                raise AICError(
                    "AISimulate returned an invalid topology; try another model or hardware target.", public=True
                )
    row = _plain(dict(row))
    scale = max(1, int(row.get("replicas_needed") or 1))
    row["num_total_gpus"] = int(row.get("num_total_gpus") or row.get("tp") or 1) * scale
    if row.get("tokens/s") is not None:
        row["tokens/s"] *= scale
    if mode == "agg":
        tp = int(row.get("tp") or 1)
        replicas = max(1, int(row.get("num_total_gpus") or tp) // max(tp, 1))
        return {
            "mode": "agg",
            "tp": tp,
            "replicas": replicas,
            "num_total_gpus": int(row.get("num_total_gpus") or tp * replicas),
            "ttft_ms": row.get("ttft"),
            "tpot_ms": row.get("tpot"),
            "throughput_tokens_per_sec": row.get("tokens/s"),
        }
    prefill_tp = int(row.get("(p)tp") or 1)
    decode_tp = int(row.get("(d)tp") or 1)
    return {
        "mode": "disagg",
        "prefill_tp": prefill_tp,
        "prefill_replicas": int(row.get("(p)workers") or 1) * scale,
        "decode_tp": decode_tp,
        "decode_replicas": int(row.get("(d)workers") or 1) * scale,
        "num_total_gpus": int(row.get("num_total_gpus") or 1),
        "ttft_ms": row.get("ttft"),
        "tpot_ms": row.get("tpot"),
        "throughput_tokens_per_sec": row.get("tokens/s"),
    }


def _result_candidates(result: Any, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for mode, frame in result.best_configs.items():
        records = frame.head(limit).to_dict(orient="records")
        candidates.extend(_plain(_candidate(mode, row)) for row in records)
    return candidates


def search_sync(request: AICRequest) -> AICSearchResponse:
    """Recommend only configurations evaluated by the native engine replay sweeper."""
    deadline = monotonic() + 60.0
    try:
        from aisimulate.runner import EngineReplayRunnerFactory
        from aisimulate.sweeper import SmartSearchConfig, Sweeper

        has_load = request.target_concurrency is not None or request.target_request_rate is not None
        concurrency = request.target_concurrency
        if concurrency is not None and not concurrency.is_integer():
            raise AICError("Engine replay requires an integer concurrency target")
        request_count = max(request.num_requests, 2 * int(concurrency or 1))
        if request_count > 100000:
            raise AICError("Engine replay request count exceeds 100000")
        workload = {
            "isl": request.mean_input_tokens,
            "osl": request.mean_output_tokens,
            "request_count": request_count,
            "random_range_ratio": 1.0,
            **(
                {"request_rate": request.target_request_rate}
                if request.target_request_rate is not None
                else {"concurrency": int(concurrency or 1)}
            ),
        }
        config = SmartSearchConfig.model_validate(
            {
                "search_space": {
                    "model_name": request.model_name,
                    "hardware_sku": request.aic_system_name,
                    "backend": [request.aic_backend_name.lower()],
                    "deployment_mode": ["agg", "disagg"] if request.gpu_count > 1 else ["agg"],
                    "database_mode": request.aic_database_mode.upper(),
                    "gpu_budget": request.gpu_count,
                },
                "adapters": {},
                "workload": workload,
                "goal": {
                    "target": "min_gpus" if has_load else "throughput",
                    "strict_sla": True,
                    "sla": {"ttft_ms": request.ttft_target_ms or 2000.0},
                    **(
                        {"min_goodput_rps": request.target_request_rate}
                        if request.target_request_rate is not None
                        else {}
                    ),
                },
                "sweep": {
                    "algorithm": "random",
                    "parallel_evals": 1,
                    "max_rounds": request.max_evaluations,
                    "max_trials": request.max_evaluations,
                    "candidates_per_round": 1,
                    "max_eval_seconds": 20.0,
                    "seed": 42,
                },
            }
        )
        records = []
        budget_exceeded = False

        def on_candidate(record):
            records.append(record)
            if monotonic() >= deadline:
                raise _SearchBudgetExceededError

        try:
            result = Sweeper(
                runner_factory=EngineReplayRunnerFactory(determinism="canonical_v1"),
                show_progress=False,
            ).run(config, top_n=None, on_candidate=on_candidate)
            records = result.candidates
        except _SearchBudgetExceededError:
            budget_exceeded = True
        configs = []
        for record in records:
            if record.status != "feasible":
                continue
            metrics = record.metrics
            required = ("mean_ttft_ms", "mean_tpot_ms", "output_throughput_tok_s", "duration_ms")
            if any(
                isinstance(metrics.get(name), bool)
                or not isinstance(metrics.get(name), (int, float))
                or not math.isfinite(metrics[name])
                or metrics[name] <= 0
                for name in required
            ):
                continue
            if (
                metrics.get("completed_requests") != request_count
                or metrics.get("num_requests") != request_count
                or metrics.get("num_ttft_samples", 0) <= 0
                or metrics.get("num_tpot_samples", 0) <= 0
                or metrics["mean_ttft_ms"] > (request.ttft_target_ms or 2000)
                or metrics["mean_tpot_ms"] > (request.tpot_target_ms or 30)
                or not record.used_gpus
                or record.used_gpus > request.gpu_count
            ):
                continue
            topology = record.config
            mode = record.config["deployment_mode"]
            prefixes = ("",) if mode == "agg" else ("prefill_", "decode_")
            if any(
                topology.get(f"{prefix}{axis}", 1) != 1
                for prefix in prefixes
                for axis in ("pp", "attention_dp", "moe_ep")
            ):
                continue
            item = {
                "mode": mode,
                "execution_mode": "engine_replay",
                "num_total_gpus": record.used_gpus,
                "ttft_ms": metrics["mean_ttft_ms"],
                "tpot_ms": metrics["mean_tpot_ms"],
                "throughput_tokens_per_sec": metrics["output_throughput_tok_s"],
                "replay_metrics": dict(metrics),
                "engine_configuration": {
                    key: value
                    for key, value in topology.items()
                    if key == "backend_version"
                    or any(
                        key.startswith(f"{role}_")
                        and key.removeprefix(f"{role}_")
                        in {
                            "max_num_seqs",
                            "max_num_batched_tokens",
                            "block_size",
                            "gpu_memory_utilization",
                            "enable_prefix_caching",
                        }
                        for role in ("agg", "prefill", "decode")
                    )
                },
            }
            if mode == "agg":
                item.update(tp=topology["tp"], replicas=topology["replicas"])
            else:
                item.update(
                    prefill_tp=topology["prefill_tp"],
                    prefill_replicas=topology["prefill_replicas"],
                    decode_tp=topology["decode_tp"],
                    decode_replicas=topology["decode_replicas"],
                )
            configs.append(item)
        configs.sort(
            key=lambda item: (
                item["num_total_gpus"] if has_load else -item["throughput_tokens_per_sec"],
                -item["throughput_tokens_per_sec"],
                item["ttft_ms"],
            )
        )
        if not configs:
            raise AICError(
                "AISimulate engine replay found no complete configuration meeting the TTFT/TPOT SLOs "
                f"within the {request.gpu_count}-GPU budget and {request.max_evaluations}-evaluation search limit. "
                + ("The 60-second wall-clock budget was exhausted. " if budget_exceeded else "")
                + "This bounded search does not prove that no feasible configuration exists."
            )
        return AICSearchResponse(
            configs=configs[: request.max_candidates],
            chosen_mode=configs[0]["mode"],
            evaluated_candidates=len(records),
            warnings=[
                "Bounded engine-replay search; optimality and real-world accuracy are not established. "
                "Latency limits apply to means, not percentiles. Reproduce the returned engine_configuration."
            ]
            + (
                ["The 60-second wall-clock budget was exhausted; returning completed feasible predictions only."]
                if budget_exceeded
                else []
            ),
        )
    except AICError:
        raise
    except Exception as error:
        raise AICError(str(error)) from error


async def search(request: AICRequest) -> AICSearchResponse:
    return await asyncio.to_thread(search_sync, request)


def experiments_sync(request: AICExperimentRequest) -> AICExperimentResponse:
    _cli_default, cli_exp, _cli_support = _cli()
    try:
        parsed = yaml.safe_load(request.yaml_text)
        if not isinstance(parsed, dict):
            raise AICError("AISimulate experiment YAML must contain an object")
        result = cli_exp(config=parsed, top_n=request.top_n)
    except AICError:
        raise
    except Exception as error:
        raise AICError(str(error)) from error
    try:
        experiments: list[dict[str, Any]] = []
        for mode, frame in result.best_configs.items():
            for index, row in enumerate(frame.to_dict(orient="records")):
                item = _candidate(mode, row)
                item.update({"name": f"{mode}-{index + 1}", "is_chosen": mode == result.chosen_exp})
                experiments.append(_plain(item))
        return AICExperimentResponse(experiments=experiments)
    except Exception as error:
        raise AICError("AISimulate returned an invalid experiment result", public=True) from error


async def experiments(request: AICExperimentRequest) -> AICExperimentResponse:
    return await asyncio.to_thread(experiments_sync, request)


def estimate_sync(payload: AICEstimateRequest) -> dict[str, Any]:
    """Replay one fixed deployment without analytical prediction or topology search."""
    is_agg = payload.scenario == "inference_scheduling"
    if payload.pp != 1:
        raise AICError("Engine replay does not support pipeline parallelism")
    required_gpus = (
        payload.tp * payload.pp * payload.replicas
        if is_agg
        else payload.prefill_tp * payload.prefill_replicas + payload.decode_tp * payload.decode_replicas
    )
    if payload.gpu_count != required_gpus:
        raise AICError(f"Requested topology requires {required_gpus} GPUs, not {payload.gpu_count}")
    try:
        from aisimulate.runner import EngineReplayRunner
        from aisimulate.sweeper.replay import BackendDeploymentSpec, ReplaySpec, RunnerCapabilities

        backend = payload.aic_backend_name.lower()
        mode = "agg" if is_agg else "disagg"

        def engine_args(tp: int) -> dict[str, Any]:
            return {
                "aic_model_path": payload.model_name,
                "aic_system": payload.aic_system_name,
                "aic_backend": backend,
                "aic_database_mode": payload.aic_database_mode.upper(),
                "tensor_parallel_size": tp,
            }

        deployment = BackendDeploymentSpec(
            deployment_mode=mode,
            backend=backend,
            backend_version="",
            agg_engine_args=engine_args(payload.tp) if is_agg else None,
            prefill_engine_args=None if is_agg else engine_args(payload.prefill_tp),
            decode_engine_args=None if is_agg else engine_args(payload.decode_tp),
            num_workers=payload.replicas if is_agg else 0,
            num_prefill_workers=0 if is_agg else payload.prefill_replicas,
            num_decode_workers=0 if is_agg else payload.decode_replicas,
        )
        spec = ReplaySpec(
            backend_deployment=deployment,
            workload={
                "isl": payload.mean_input_tokens,
                "osl": payload.mean_output_tokens,
                "request_count": payload.num_requests,
                **(
                    {"request_rate": payload.target_request_rate}
                    if payload.target_request_rate is not None
                    else {"concurrency": payload.target_concurrency or 1}
                ),
                "random_range_ratio": 1.0,
            },
            goal={},
            concurrency=None if payload.target_request_rate is not None else payload.target_concurrency or 1,
        )
        runner = EngineReplayRunner(
            worker_id=0,
            capabilities=RunnerCapabilities(supported_backend_topologies=((backend, mode),)),
            determinism="canonical_v1",
        )
        try:
            report = runner.run(spec)
        finally:
            runner.close()
        metrics = report.metrics
        for name in ("num_requests", "completed_requests", "num_ttft_samples", "num_tpot_samples", "duration_ms"):
            value = metrics.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise AICError(f"Engine replay returned invalid {name}")
        if metrics["completed_requests"] != payload.num_requests or metrics["num_requests"] != payload.num_requests:
            raise AICError("Engine replay did not complete every requested request")
        for name in ("mean_ttft_ms", "mean_tpot_ms", "output_throughput_tok_s"):
            value = metrics.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise AICError(f"Engine replay returned invalid {name}")
        prediction = {
            "mode": mode,
            "execution_mode": "engine_replay",
            "num_total_gpus": required_gpus,
            "ttft_ms": metrics.get("mean_ttft_ms"),
            "tpot_ms": metrics.get("mean_tpot_ms"),
            "throughput_tokens_per_sec": metrics.get("output_throughput_tok_s"),
            "replay_metrics": dict(metrics),
        }
        if is_agg:
            prediction.update(tp=payload.tp, replicas=payload.replicas)
        else:
            prediction.update(
                prefill_tp=payload.prefill_tp,
                prefill_replicas=payload.prefill_replicas,
                decode_tp=payload.decode_tp,
                decode_replicas=payload.decode_replicas,
            )
        return prediction
    except Exception as error:
        raise AICError(str(error)) from error


async def estimate(payload: AICEstimateRequest) -> dict[str, Any]:
    return await asyncio.to_thread(estimate_sync, payload)
