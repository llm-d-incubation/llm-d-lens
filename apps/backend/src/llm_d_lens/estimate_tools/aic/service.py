# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""In-process adapter around AISimulate's AIC-compatible Python API."""

from __future__ import annotations

import asyncio
import math
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


class AICError(RuntimeError):
    """A user-visible AISimulate integration failure."""


def _cli():
    try:
        from aisimulate.legacy_cli import cli_default, cli_exp, cli_support
    except ImportError as error:
        raise AICError(
            "AISimulate is unavailable; install the project dependencies with python -m pip install -e ."
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
    scale = max(1, int(row.get("replicas_needed") or 1))
    row = dict(row)
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
        normalized_mode = "agg" if mode == "agg" else "disagg"
        records = frame.head(limit).to_dict(orient="records")
        candidates.extend(_plain(_candidate(normalized_mode, row)) for row in records)
    return candidates


def search_sync(request: AICRequest) -> AICSearchResponse:
    cli_default, _cli_exp, _cli_support = _cli()
    kwargs: dict[str, Any] = {
        "model_path": request.model_name,
        "total_gpus": request.gpu_count,
        "system": request.aic_system_name,
        "backend": request.aic_backend_name.lower(),
        "database_mode": request.aic_database_mode.upper(),
        "isl": request.mean_input_tokens,
        "osl": request.mean_output_tokens,
        "top_n": request.max_candidates,
    }
    if request.ttft_target_ms is not None:
        kwargs["ttft"] = request.ttft_target_ms
    if request.tpot_target_ms is not None:
        kwargs["tpot"] = request.tpot_target_ms
    try:
        if request.target_concurrency is not None or request.target_request_rate is not None:
            from aisimulate.legacy_cli.api import cli_recommend

            kwargs.pop("total_gpus")
            kwargs["target_concurrency"] = request.target_concurrency
            kwargs["target_request_rate"] = request.target_request_rate
            result = cli_recommend(**kwargs)
            configs = _result_candidates(result, request.max_candidates)
            fitting = [item for item in configs if item["num_total_gpus"] <= request.gpu_count]
            if not fitting:
                required = min((item["num_total_gpus"] for item in configs), default=None)
                raise AICError(
                    f"AISimulate cannot serve the requested load within the selected {request.gpu_count}-GPU budget. "
                    f"Minimum recommended GPUs: {required if required is not None else 'unavailable'}. "
                    "Reduce the load target, select more accelerator capacity, or relax the latency SLOs."
                )
            compliant = [
                item
                for item in fitting
                if item.get("ttft_ms") is not None
                and item.get("tpot_ms") is not None
                and 0 < item["ttft_ms"] <= (request.ttft_target_ms or 2000)
                and 0 < item["tpot_ms"] <= (request.tpot_target_ms or 30)
            ]
            if not compliant:
                raise AICError(
                    "AISimulate found no configuration meeting the TTFT/TPOT SLOs at the requested load. "
                    "Reduce the load target, relax the latency SLOs, or select more compatible accelerator capacity."
                )
            return AICSearchResponse(configs=compliant, chosen_mode=result.chosen_exp)
        result = cli_default(**kwargs)
    except Exception as error:
        raise AICError(str(error)) from error
    return AICSearchResponse(
        configs=_result_candidates(result, request.max_candidates),
        chosen_mode=result.chosen_exp,
    )


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
    experiments: list[dict[str, Any]] = []
    for mode, frame in result.best_configs.items():
        for index, row in enumerate(frame.to_dict(orient="records")):
            item = _candidate("agg" if mode == "agg" else "disagg", row)
            item.update({"name": f"{mode}-{index + 1}", "is_chosen": mode == result.chosen_exp})
            experiments.append(_plain(item))
    return AICExperimentResponse(experiments=experiments)


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
