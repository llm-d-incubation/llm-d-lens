"""Reviewed published-table evidence, not a general Markdown number extractor.

The allowlist and layouts describe the reports at llm-d 080c14d9. Unknown
reports produce no evidence; changed layouts/metric rows fail closed. Configuration,
superseded GLM overlay campaigns, comparisons, and non-exact cells are returned as
auditable context passages. Experiments retain the report verbatim because workload
and operational qualifications cannot safely be inferred from table cells alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import JsonValue

from llm_d_lens.benchmark_rag.contracts import RagItem, normalize_unit
from llm_d_lens.benchmark_rag.publication_contracts import InventoryFile, PublicationError, item_id
from llm_d_lens.benchmark_rag.sources import LLM_D_ORIGIN, validate_revision

REVIEWED_REVISION = "080c14d957755da4cc363745638619fc748558f1"
_ROOT = "guides/"
_FMA = "fast-model-actuation/benchmark-results/qwen3-32b-h100/README.md"
_CORE = "modelexpress-p2p/benchmark-results/coreweave-h200-llama-3.3-70b.md"
_MX = "modelexpress-p2p/benchmark-results/h200-ib-gpt-oss-120b.md"
_GLM = "p2p-kv-cache-sharing/benchmark-results/glm-5.2-h200.md"
_GPT = "p2p-kv-cache-sharing/benchmark-results/gpt-oss-120b-h200.md"
_QPD = "p2p-kv-cache-sharing/benchmark-results/qwen3-30b-h200-pd-agentic.md"
_DATA = "p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/README.md"
_TIER = "tiered-prefix-cache/benchmark-results/"
_LUSTRE = _TIER + "vllm-qwen3-32b-h100-lustre.md"
_AGENT_QWEN = "agentic-serving/qwen3-coder-480b-tpu.md"
_AGENT_NEMO = "agentic-serving/nemotron-3-ultra-550b-h200.md"
_AGENT_GLM = "agentic-serving/glm-5-2-h200.md"
_MX_GUIDE = "modelexpress-p2p/README.md"
_MM = "multimodal-serving/aggregation/README.md"
_PREDICTED = "predicted-latency-routing/README.md"
_P2P_GUIDE = "p2p-kv-cache-sharing/README.md"
_P2P_METHOD = "p2p-kv-cache-sharing/benchmarking/README.md"
_PD = "pd-disaggregation/README.md"
_WIDE = "wide-ep-lws/README.md"
_WVA = "workload-autoscaling/wva/README.md"
_SLO = "workload-autoscaling/slo-aware/benchmark-templates/BENCHMARK.md"
_TPU_YAML = "pd-disaggregation/README.tpu.md"


@dataclass(frozen=True)
class _Report:
    model: str | None
    hardware: str | None
    runtime: str | None
    layouts: tuple[str, ...]


# Model metadata is reviewed per report, never inferred by filename matching.
_REPORTS = {
    _FMA: _Report("Qwen/Qwen3-32B", "H100", "vLLM", ("context", "context", "fma", "context", "fma", "context")),
    _CORE: _Report("meta-llama/Llama-3.3-70B-Instruct", "H200", "vLLM", ("weights", "weights")),
    _MX: _Report("openai/gpt-oss-120b", "H200", "vLLM v0.25.0", ("weights", "compile")),
    _GLM: _Report(
        "zai-org/GLM-5.2-FP8", "32x H200", "vLLM", ("probe", "historical", "spill", "repeat", "four", "historical")
    ),
    _GPT: _Report("openai/gpt-oss-120b", "H200", "vLLM", ("probe", "qa", "uniform", "hot")),
    _QPD: _Report("Qwen/Qwen3-30B-A3B-Thinking", "H200", "vLLM", ("metrics",)),
    _DATA: _Report(None, None, None, ("context", "context")),
    "optimized-baseline/benchmark-results/sglang-qwen3-32b-h100/README.md": _Report(
        "Qwen/Qwen3-32B", "H100", "SGLang", ("metrics", "rate")
    ),
    "optimized-baseline/benchmark-results/vllm-gpt-oss-120b-h100/README.md": _Report(
        "openai/gpt-oss-120b", "H100", "vLLM", ("metrics", "rate")
    ),
    "optimized-baseline/benchmark-results/vllm-qwen3-32b-h100/README.md": _Report(
        "Qwen/Qwen3-32B", "H100", "vLLM", ("metrics", "rate")
    ),
    "precise-prefix-cache-routing/benchmark-results/sglang-qwen3-32b-h100.md": _Report(
        "Qwen/Qwen3-32B", "16x H100", "SGLang", ("metrics", "rate")
    ),
    "precise-prefix-cache-routing/benchmark-results/vllm-qwen3-32b-h100.md": _Report(
        "Qwen/Qwen3-32B", "16x H100", "vLLM", ("metrics", "rate")
    ),
    _TIER + "sglang-qwen3-32b-h100-lustre.md": _Report("Qwen/Qwen3-32B", "16x H100", "SGLang", ("tier",) * 3),
    _TIER + "sglang-qwen3-32b-h100.md": _Report("Qwen/Qwen3-32B", "16x H100", "SGLang", ("tier",)),
    _TIER + "vllm-qwen3-32b-h100.md": _Report("Qwen/Qwen3-32B", "16x H100", "vLLM", ("tier",)),
    _TIER + "vllm-qwen3-32b-b60-xpu.md": _Report("Qwen/Qwen3-32B", "4x Intel B60 XPU", "vLLM", ("tier",)),
    _TIER + "vllm-qwen3-8b-b60-xpu.md": _Report("Qwen/Qwen3-8B", "Intel B60 XPU", "vLLM", ("tier",)),
    _TIER + "vllm-qwen3-32b-tpuv7.md": _Report("Qwen/Qwen3-32B", "Google TPU v7", None, ("tier",)),
    _TIER + "vllm-gpt-oss-120b-h100.md": _Report(
        "openai/gpt-oss-120b", "16x H100", "vLLM", ("metrics", "metrics", "metrics", "tier")
    ),
    _LUSTRE: _Report(None, None, "vLLM", ("tier",) * 4),
    _AGENT_QWEN: _Report(
        "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
        "TPU 7x",
        "vLLM",
        ("reference", "reference", "reference", "metrics", "metrics"),
    ),
    _AGENT_NEMO: _Report(
        "RedHatAI/NVIDIA-Nemotron-3-Ultra-550B-A55B-FP8-block",
        "H200",
        "vLLM",
        ("reference", "reference", "reference", "metrics"),
    ),
    _AGENT_GLM: _Report(
        "zai-org/GLM-5.2-FP8", "64 GPUs H200", "vLLM", ("reference", "reference", "glm64", "reference", "reference")
    ),
    _MX_GUIDE: _Report("openai/gpt-oss-120b", "H200", "vLLM", ("reference", "reference", "weights")),
    _MM: _Report("Qwen/Qwen3-VL-32B-Instruct", "16x H200", "vLLM", ("reference", "reference", "rate")),
    _PREDICTED: _Report("Qwen/Qwen3-VL-32B-Instruct", "16x H200", "vLLM", ("reference", "predicted", "reference")),
    _P2P_GUIDE: _Report(None, None, None, ("reference", "delta", "reference")),
    _P2P_METHOD: _Report("openai/gpt-oss-120b", "H200", "vLLM", ("reference", "hot", "unsupported", "hot")),
    _PD: _Report(
        "openai/gpt-oss-120b",
        None,
        "vLLM",
        ("reference", "reference", "reference", "reference", "reference", "metrics"),
    ),
    _WIDE: _Report(None, "32 GPUs H200", None, ("reference", "reference", "reference", "metrics")),
    _WVA: _Report(None, None, None, ("reference", "reference", "wva", "wva", "wva", "wva", "reference")),
    _SLO: _Report("Qwen/Qwen3-32B", "H100 80 GB", "vLLM", ("reference", "slo")),
    _TPU_YAML: _Report(None, None, None, ()),
}

# Remaining table-bearing sources were reviewed across all 97 pinned guide files.
# These are configuration/reference/example tables, never measured observations.
_REFERENCE_GUIDES = {
    "agentic-serving/README.md": 1,
    "batch-serving/README.md": 1,
    "batch-serving/batch-gateway/README.md": 1,
    "coord-disaggregation/README.md": 2,
    "diffusion-serving/image-to-image/README.md": 2,
    "diffusion-serving/text-to-image/README.md": 2,
    "fast-model-actuation/README.md": 1,
    "flow-control/README.md": 2,
    "flow-control/tuning.md": 1,
    "multimodal-serving/README.md": 1,
    "multimodal-serving/e-disaggregation/README.md": 2,
    "multimodal-serving/e-disaggregation/profiles/sglang-xpu-encode-gpu-pd.md": 3,
    "no-kubernetes-deployment/README.md": 2,
    "optimized-baseline/README.md": 3,
    "pd-disaggregation/README.ds.md": 1,
    "precise-prefix-cache-routing/README.md": 2,
    "recipes/router/calibration/README.md": 2,
    "recipes/router/calibration/configuration-matrix.md": 1,
    "templates/README.md": 2,
    "tiered-prefix-cache/README.md": 5,
    "wide-ep-lws/README.precise-prefix-cache-routing.md": 1,
    "wide-ep-lws/modelserver/gpu/vllm-deepseek-r1-0528/gke/README.md": 1,
    "wide-ep-lws/modelserver/gpu/vllm-deepseek-v4/README.md": 1,
    "wide-ep-lws/modelserver/gpu/vllm-glm-5.2/README.md": 7,
    "workload-autoscaling/README.md": 2,
    "workload-autoscaling/keda-epp-queue/README.md": 2,
    "workload-autoscaling/keda-epp-saturation/README.md": 1,
    "workload-autoscaling/replica-rebalancing/README.md": 3,
    "workload-autoscaling/slo-aware/README.md": 2,
}
_REPORTS.update({path: _Report(None, None, None, ("reference",) * count) for path, count in _REFERENCE_GUIDES.items()})
_DUPLICATE_OF = {
    _MX_GUIDE: _MX,
    _P2P_METHOD: _GPT,
}
_TABLE_SCENARIOS: dict[tuple[str, int], dict[str, JsonValue]] = {
    (_AGENT_QWEN, 3): {"concurrent_coding_sessions": 40, "topology": "8 aggregated replicas"},
    (_AGENT_QWEN, 4): {"concurrent_coding_sessions": 40, "topology": "prefill/decode disaggregated"},
    (_AGENT_NEMO, 3): {"concurrent_coding_sessions": 60},
    (_AGENT_GLM, 2): {"deployment": "recommended 64-GPU p3w2d1w2"},
}
_NARRATIVE_RESULTS = {
    _PREDICTED: (
        "Code Generation (GPU · Qwen3-32B)",
        "Agentic Serving (TPU · Qwen3-Coder-480B-A35B-Instruct-FP8 with KV-cache offloading)",
        "Prefill/Decode Disaggregation (GPU · gpt-oss-120b)",
    ),
    _TPU_YAML: ("Benchmarking Report (TPU v7x Example)",),
}


@dataclass(frozen=True)
class _Table:
    header: tuple[str, ...]
    rows: tuple[tuple[int, tuple[str, ...]], ...]
    heading: tuple[str, ...]
    line: int


def _cells(line: str) -> tuple[str, ...]:
    # Escaped pipes are not column separators. Keep cell markup and whitespace.
    return tuple(re.split(r"(?<!\\)\|", line.strip().strip("|")))


def _clean(text: str) -> str:
    return text.replace("*", "").replace("`", "").replace("_", "").strip()


def _tables(text: str) -> tuple[_Table, ...]:
    lines = text.splitlines()
    result: list[_Table] = []
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            i += 1
            continue
        if fence is not None:
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level = len(heading[1])
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, heading[2]))
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and all(re.fullmatch(r"\s*:?-{3,}:?\s*", c) for c in _cells(lines[i + 1]))
        ):
            header = tuple(c.strip() for c in _cells(line))
            if len(_cells(lines[i + 1])) != len(header):
                raise PublicationError("published table separator width changed")
            start = i
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = _cells(lines[i])
                if len(cells) != len(header):
                    raise PublicationError("published table row width changed")
                rows.append((i + 1, cells))
                i += 1
            result.append(_Table(header, tuple(rows), tuple(h for _, h in headings), start + 1))
            continue
        i += 1
    return tuple(result)


# metric, statistic, explicitly established unit (cell suffix takes precedence).
_METRICS: dict[str, tuple[str, str | None, str | None]] = {
    "Output tokens/sec": ("throughput", None, "output tokens/s"),
    "Output tokens/s": ("throughput", None, "output tokens/s"),
    "Peak output tokens/s": ("throughput", "peak", "output tokens/s"),
    "Requests/sec": ("request_rate", None, "requests/s"),
    "Requests/sec (@ rate 60)": ("request_rate", None, "requests/s"),
    "Requests/sec (successful)": ("successful_request_rate", None, "requests/s"),
    "Total tokens/sec": ("total_throughput", None, "tokens/s"),
    "Input tokens/sec": ("input_throughput", None, "input tokens/s"),
    "Failed requests": ("error_count", None, "count"),
    "Throughput": ("request_rate", None, "requests/s"),
    "Run duration": ("duration", None, "s"),
    "Duration (s)": ("duration", None, "s"),
    "Total requests": ("requests_total", None, "count"),
    "Avg replicas": ("replicas", "mean", "count"),
    "Max replicas": ("replicas", "max", "count"),
    "Avg KV cache utilization": ("kv_cache_utilization", "mean", "%"),
    "Avg queue depth (EPP)": ("queue_depth", "mean", "count"),
    "Avg flow-control pool saturation (EPP)": ("flow_control_pool_saturation", "mean", None),
    "Avg flow-control queue (EPP)": ("flow_control_queue", "mean", "count"),
    "Avg running requests (EPP)": ("running_requests", "mean", "count"),
    "Avg pod startup (s)": ("pod_startup", "mean", "s"),
    "Hot hit rate": ("hot_hit_rate", None, "%"),
    "Warm hit rate": ("warm_hit_rate", None, "%"),
    "Failures": ("error_count", None, "count"),
    "Internal GPU cache hit rate": ("internal_cache_hit_rate", None, "%"),
    "Internal cache hits (tokens)": ("internal_cache_hits", None, "M tokens"),
    "Internal cache queries (tokens)": ("internal_cache_queries", None, "M tokens"),
    "External (CPU offload) hit rate": ("external_cache_hit_rate", None, "%"),
    "External cache hits (tokens)": ("external_cache_hits", None, "M tokens"),
    "External cache queries (tokens)": ("external_cache_queries", None, "M tokens"),
    "torch.compile": ("torch_compile", None, "s"),
    "Engine init (profile, KV cache, warmup)": ("engine_init", None, "s"),
    "Receiver pod-Ready (1→2 scale-out wall clock)": ("pod_ready", None, "s"),
    "Weight-load time": ("weight_load", None, "s"),
    "Weight-load time per rank": ("weight_load", None, "s"),
    "Total pod-Ready (1→2)": ("pod_ready", None, "s"),
    "req/s": ("request_rate", None, "requests/s"),
    "successful req/s": ("successful_request_rate", None, "requests/s"),
    "input Ktok/s": ("input_throughput", None, "Ktok/s"),
    "wall per rep (s)": ("duration_per_repetition", None, "s"),
    # Unqualified "Throughput" does not establish an input/output denominator.
    "Throughput (tok/s)": ("token_throughput", None, "tokens/s"),
    "Overall Throughput (tok/s)": ("total_throughput", None, "tokens/s"),
    "Input (tok/s)": ("input_throughput", None, "input tokens/s"),
    "Output (tok/s)": ("throughput", None, "output tokens/s"),
    "Overall (tok/s)": ("total_throughput", None, "tokens/s"),
    "ITL (s)": ("itl", None, "s"),
    "TTFT P50 (ms)": ("ttft", "p50", "ms"),
    "TTFT Median (s)": ("ttft", "p50", "s"),
    "TTFT Mean (s)": ("ttft", "mean", "s"),
    "TTFT P90 (s)": ("ttft", "p90", "s"),
    "TPOT Median (ms)": ("tpot", "p50", "ms"),
    "Total tokens / sec": ("total_throughput", None, "tokens/s"),
    "Input tokens / sec": ("input_throughput", None, "input tokens/s"),
    "Output tokens / sec": ("throughput", None, "output tokens/s"),
    "Input Throughput (tok/s)": ("input_throughput", None, "input tokens/s"),
    "Output Throughput (tok/s)": ("throughput", None, "output tokens/s"),
    "Error Rate": ("error_rate", None, "%"),
    "E2E Latency (Mean)": ("latency", "mean", None),
    "E2E Latency (P95)": ("latency", "p95", None),
    "ITL (Mean)": ("itl", "mean", None),
    "ITL (P95)": ("itl", "p95", None),
    "TTFT (Mean)": ("ttft", "mean", None),
    "TTFT (P95)": ("ttft", "p95", None),
    "Input tokens/s": ("input_throughput", None, "input tokens/s"),
    "Total tokens/s": ("total_throughput", None, "tokens/s"),
    "Requests/s": ("request_rate", None, "requests/s"),
    "P99 TTFT (ms)": ("ttft", "p99", "ms"),
    "P99 ITL (ms/tok)": ("itl", "p99", "ms/tok"),
    "Avg Replicas": ("replicas", "mean", "count"),
    "Max Replicas": ("replicas", "max", "count"),
    "Avg KV Cache": ("kv_cache_utilization", "mean", "%"),
    "Avg Queue Depth": ("queue_depth", "mean", "count"),
    "Errors": ("error_count", None, "count"),
    "Pod Startup (s)": ("pod_startup", "mean", "s"),
    "Duration": ("duration", None, "s"),
}
for _label, _metric in (
    ("TTFT", "ttft"),
    ("ITL", "itl"),
    ("TPOT", "tpot"),
    ("request latency", "latency"),
    ("Request latency", "latency"),
    ("E2E Latency", "latency"),
):
    for _stat in ("mean", "median", "p50", "p90", "p95", "p99"):
        for _unit in ("", " (s)", " (ms)", " (ms/tok)"):
            _spec = (_metric, "p50" if _stat == "median" else _stat, _unit[2:-1] or None)
            _METRICS[f"{_label} {_stat}{_unit}"] = _spec
            _METRICS[f"{_stat.capitalize()} {_label}{_unit}"] = _spec
            _METRICS[f"{_stat.upper()} {_label}{_unit}"] = _spec

_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_VALUE = re.compile(
    rf"^({_NUMBER})\s*(ms/tok|ms|s|%|M|req/s|tok/s)?"
    r"(?:\s*\((?:[+−-]?\d+(?:\.\d+)?%|direct AOT cache load)\))?$"
)


def _metric(label: str) -> tuple[str, str | None, str | None]:
    try:
        return _METRICS[_clean(label)]
    except KeyError:
        raise PublicationError(f"unreviewed published metric: {label}") from None


def _headers(layout: str, table: _Table) -> None:
    h = table.header
    first = h[0].lower()
    valid = False
    if layout in {"metrics", "fma", "compile"}:
        valid = first == "metric" and (
            h[1:]
            in (
                ("k8s service (RR)", "llm-d Optimized", "Δ% vs k8s"),
                ("k8s service (RR)", "llm-d Precise", "Δ% vs k8s"),
                ("plain NIXL P/D", "+ P2P pull", "delta"),
                ("llm-d baseline", "KV cache offload", "Delta"),
                ("llm-d baseline", "KV cache offload"),
                ("Baseline", "Warm", "Hot", "Δ% Hot vs Baseline"),
                ("Cold receiver", "With artifact transfer"),
                ("k8s Service", "llm-d-optimized", "Δ Improvement"),
                ("2:6 (2 Prefill, 6 Decode)", "5:3 (5 Prefill, 3 Decode)", "6:2 (6 Prefill, 2 Decode)"),
                ("aggregated", "llm-d", "Δ%"),
                ("DP Supervisor",),
            )
        )
    elif layout == "rate":
        valid = h in (
            ("Rate", "k8s Output", "llm-d Output", "k8s TTFT p90", "llm-d TTFT p90"),
            ("Rate", "k8s Output", "llm-d Output", "k8s TTFT p50", "llm-d TTFT p50", "k8s TTFT p90", "llm-d TTFT p90"),
        )
    elif layout == "tier":
        latency = ("Mean TTFT (s)", "P90 TTFT (s)", "Mean E2E Latency (s)", "P90 E2E Latency (s)")
        rates = ("Input (tok/s)", "Output (tok/s)", "Overall (tok/s)")
        valid = h in (
            ("Target Rate", "Configuration", *latency, "Throughput (tok/s)"),
            ("Configuration", "Target Rate", *latency, "Throughput (tok/s)"),
            ("Configuration", *latency, "Overall Throughput (tok/s)"),
            ("Configuration", *latency, *rates),
            ("Configuration", *latency, *rates, "ITL (s)"),
            (
                "Target Rate",
                "Configuration",
                "Mean TTFT",
                "P90 TTFT",
                "Mean E2E Latency",
                "P90 E2E Latency",
                "Throughput (tok/s)",
            ),
        )
    elif layout == "weights":
        valid = h in (
            (
                "Path",
                "Weight-load time",
                "Effective rate",
                "Total pod-Ready (1→2)",
                "Transport",
                "Egress",
                "Shared storage",
            ),
            ("Path", "Weight-load time", "Notes"),
            ("Path", "Weight-load time per rank", "Effective rate"),
        )
    elif layout == "probe":
        valid = h in (
            ("prefix tokens", "recompute", "P2P pull", "delta", "pulled in"),
            ("prefix tokens", "recompute", "P2P pull", "delta", "superseded (2026-07-17)"),
        )
    elif layout == "spill":
        valid = h == ("mode", "TTFT mean (s)", "TTFT p90 (s)", "req/s", "wall per rep (s)")
    elif layout == "repeat":
        valid = h == (
            "observation",
            "arm order",
            "approximate successful req/s",
            "precise+P2P successful req/s",
            "successful req/s change",
            "input Ktok/s change",
            "E2E p90 change",
        )
    elif layout == "four":
        valid = h == ("routing", "P2P", "successful req/s", "input Ktok/s", "TTFT p50/p90 (s)", "E2E p50/p90 (s)")
    elif layout == "qa":
        valid = h == ("arm", "run", "ok/fail", "p50", "p95", "p99", "turns/s")
    elif layout in {"uniform", "hot"}:
        valid = h in (
            ("offered", "Affinity", "Load, no P2P", "Load + P2P"),
            ("offered", "affinity", "load, no P2P", "load + P2P"),
            ("offered", "`affinity`", "`load` - no P2P", "`load + P2P`"),
        )
    elif layout == "glm64":
        valid = h == (
            "Concurrency",
            "Requests",
            "TTFT p50 / p90",
            "ITL p50 / p90",
            "Turn completion p50",
            "Output throughput",
        )
    elif layout == "wva":
        valid = h == (
            "Model",
            "Duration",
            "Load Gen",
            "P99 TTFT (ms)",
            "P99 ITL (ms/tok)",
            "Avg Replicas",
            "Max Replicas",
            "Avg KV Cache",
            "Avg Queue Depth",
            "Errors",
            "Pod Startup (s)",
        )
    elif layout == "predicted":
        valid = h == ("rate (req/s)", "LP out tok/s", "k8s out tok/s", "LP TTFT p50 / p90", "k8s TTFT p50 / p90")
    elif layout == "slo":
        valid = h == ("Configuration", "Combined SLO attainment", "Avg replicas (full episode)")
    elif layout == "delta":
        valid = h == ("prefix tokens", "latency delta")
    elif layout == "unsupported":
        valid = h == ("hot set", "vs one pod's cache", "what happens")
    elif layout == "reference":
        # Explicitly reviewed configuration/reference sources are context-only.
        valid = True
    elif layout == "historical":
        valid = h in (
            ("prefix tokens", "recompute", "P2P pull", "delta"),
            ("conc", "approx", "approx + P2P", "precise", "precise + P2P"),
        )
    elif layout == "context":
        valid = h in (
            ("Pass", "Model load"),
            ("Component", "Parameter", "Value"),
            ("Metric", "Definition"),
            ("data", "campaign"),
            ("file", "SHA-256"),
        )
    if not valid:
        raise PublicationError(f"unreviewed published table layout {layout}: {h}")


def parse_published_tables(file: InventoryFile, revision: str, data: bytes) -> tuple[RagItem, ...]:
    """Extract reviewed observations only at REVIEWED_REVISION; audit exclusions.

    Source-specific model/scenario metadata requires renewed review before any
    other revision can produce measurements. Context-only guides carry no such
    metadata and can use another valid pinned revision.
    """
    if hashlib.sha256(data).hexdigest() != file.sha256:
        raise PublicationError("published Markdown hash does not match inventory")
    path = file.path.removeprefix(_ROOT)
    if not file.path.startswith(_ROOT) or path not in _REPORTS:
        return ()
    if file.format != "markdown":
        raise PublicationError("published table input must be Markdown")
    validate_revision(LLM_D_ORIGIN, revision)
    report = _REPORTS[path]
    context_layouts = {"context", "historical", "reference", "delta", "unsupported"}
    if any(layout not in context_layouts for layout in report.layouts) and revision != REVIEWED_REVISION:
        raise PublicationError("published table metadata requires review before extracting a new source revision")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise PublicationError("published Markdown is not UTF-8") from error
    tables = _tables(text)
    if len(tables) != len(report.layouts):
        raise PublicationError(f"published table count changed: {file.path}")
    output: list[RagItem] = []

    def make(key: str, kind: str, **fields: object) -> RagItem:
        logical = f"{file.path}#{key}"
        return RagItem(
            item_id=item_id(LLM_D_ORIGIN, logical, revision),
            kind=kind,
            origin_id=LLM_D_ORIGIN,
            logical_key=logical,
            source_revision=revision,
            content_sha256=file.sha256,
            access_classification="verified_public",
            source_path=file.path,
            source_url=f"{LLM_D_ORIGIN}/blob/{revision}/{file.path}",
            format="markdown",
            **fields,
        )

    for heading in _NARRATIVE_RESULTS.get(path, ()):
        match = re.search(rf"^(#{{1,6}}) {re.escape(heading)}\s*$", text, re.MULTILINE)
        if match is None:
            raise PublicationError(f"reviewed result section disappeared: {heading}")
        following = re.search(rf"^#{{1,{len(match[1])}}} ", text[match.end() :], re.MULTILINE)
        end = match.end() + following.start() if following else len(text)
        line = text.count("\n", 0, match.start()) + 1
        output.append(
            make(
                f"published-narrative:{line}",
                "passage",
                parent_id=item_id(LLM_D_ORIGIN, f"file:{file.path}", revision),
                passage_text=text[match.start() : end].strip(),
                heading_path=[heading],
                source_pointer={
                    "line": line,
                    "heading_path": [heading],
                    "reason": "published result narrative or fenced YAML report, not a reviewed numeric Markdown table",
                },
            )
        )

    for ti, (table, layout) in enumerate(zip(tables, report.layouts, strict=True)):
        _headers(layout, table)
        base = f"published-table:{ti}"
        pointer: dict[str, JsonValue] = {
            "table_index": ti,
            "line": table.line,
            "heading_path": list(table.heading),
        }

        def context(
            key: str,
            content: str,
            reason: str,
            extra: dict[str, JsonValue] | None = None,
            *,
            base: str = base,
            table: _Table = table,
            pointer: dict[str, JsonValue] = pointer,
        ) -> None:
            output.append(
                make(
                    f"{base}:context:{key}",
                    "passage",
                    parent_id=item_id(LLM_D_ORIGIN, f"file:{file.path}", revision),
                    passage_text=content,
                    heading_path=list(table.heading),
                    source_pointer={**pointer, **(extra or {}), "reason": reason},
                )
            )

        if layout in {"context", "historical", "reference", "delta", "unsupported"}:
            reason = (
                "superseded overlay-era campaign: index-sizing failure, not current feature evidence"
                if layout == "historical"
                else "configuration, definitions, or provenance catalog; not observations"
            )
            if layout == "reference":
                reason = (
                    "reviewed configuration, workload distribution, topology, or reference table; not measured results"
                )
            elif layout == "delta":
                reason = "derived latency delta table; comparisons are not independent observations"
            elif layout == "unsupported":
                reason = "measured cache-fit regime descriptions and capacity ratios; not direct metric observations"
            context("table", "\n".join(text.splitlines()[table.line - 1 : table.line + len(table.rows) + 1]), reason)
            continue
        experiments: dict[str, RagItem] = {}

        def experiment(
            arm: str,
            row: int | None,
            scenario: dict[str, JsonValue],
            *,
            base: str = base,
            table: _Table = table,
            ti: int = ti,
            pointer: dict[str, JsonValue] = pointer,
            experiments: dict[str, RagItem] = experiments,
        ) -> RagItem:
            key = f"{base}:row:{row if row is not None else 'all'}:arm:{arm}"
            if key not in experiments:
                model = report.model
                if path == _WVA:
                    model = str(scenario["model"])
                if path == _CORE and ti == 1:
                    model = "meta-llama/Llama-4-Scout-17B-16E-Instruct"
                if path == _LUSTRE:
                    # The first connector's model is not stated locally.
                    model = {2: "Qwen/Qwen3-32B", 3: "Llama-3.3-70B"}.get(ti)
                cell = "2P2D" if path == _GLM and ti in {3, 4} else "1P1D" if path == _GLM else None
                duplicate = (
                    {"citation_of": "guides/" + _DUPLICATE_OF[path], "independent_run": False}
                    if path in _DUPLICATE_OF
                    else {}
                )
                experiments[key] = make(
                    key,
                    "experiment",
                    parent_id=item_id(LLM_D_ORIGIN, f"file:{file.path}", revision),
                    model=model,
                    hardware="16x H200" if path == _PD and arm == "llm-d" else report.hardware,
                    runtime=report.runtime,
                    configuration={
                        "published_report": text,
                        "arm": arm,
                        "table_index": ti,
                        "heading_path": list(table.heading),
                        "row": row,
                        "cell": cell,
                        **_TABLE_SCENARIOS.get((path, ti), {}),
                        **duplicate,
                        **scenario,
                    },
                    source_pointer={**pointer, "row": row, "arm": arm},
                )
                output.append(experiments[key])
            return experiments[key]

        def emit(
            ri: int,
            ci: int,
            arm: str,
            spec: tuple[str, str | None, str | None],
            raw: str | None = None,
            part: int = 0,
            scenario: dict[str, JsonValue] | None = None,
            shared: bool = False,
            offset: int = 0,
            *,
            table: _Table = table,
            pointer: dict[str, JsonValue] = pointer,
            layout: str = layout,
            base: str = base,
        ) -> None:
            line, cells = table.rows[ri]
            original = cells[ci]
            selected = original if raw is None else raw
            loc: dict[str, JsonValue] = {
                **pointer,
                "line": line,
                "row": ri,
                "column": ci,
                "header": table.header[ci],
                "cell": original,
            }
            clean = _clean(selected)
            matched = _VALUE.fullmatch(clean)
            if matched is None:
                if (
                    any(
                        x in clean.lower() for x in ("~", "–", "n/a", "failed", "download-bound", "cannot", "bandwidth")
                    )
                    or clean in {"", "—", "-"}
                    or re.match(r"^[<>≤≥]", clean)
                ):
                    context(
                        f"{ri}:{ci}:{part}",
                        original,
                        "non-exact, range, approximate, missing, or failed observation",
                        loc,
                    )
                    return
                raise PublicationError(f"unreviewed published numeric cell: {file.path}:{line}: {original}")
            number, suffix = matched.groups()
            metric, statistic, unit = spec
            value = Decimal(number.replace(",", ""))
            if (
                path == "optimized-baseline/benchmark-results/vllm-qwen3-32b-h100/README.md"
                and layout == "rate"
                and metric == "ttft"
                and value == 0
            ):
                context(
                    f"{ri}:{ci}:{part}",
                    original,
                    "report declares zero an empty latency histogram, not zero latency",
                    loc,
                )
                return
            unit = (
                {"req/s": "requests/s", "M": "M tokens", "tok/s": unit or "tokens/s"}.get(suffix, suffix)
                if suffix
                else unit
            )
            start = original.find(number, offset)
            if start < 0:
                raise PublicationError("numeric subvalue is not in original cell")
            loc["subvalue"] = {"index": part, "start": start, "end": start + len(number), "text": number}
            exp = experiment(arm, None if shared else ri, scenario or {})
            normalized = normalize_unit(value, unit) if unit else None
            output.append(
                make(
                    f"{base}:row:{ri}:column:{ci}:subvalue:{part}",
                    "measurement",
                    parent_id=exp.item_id,
                    experiment_id=exp.item_id,
                    metric=metric,
                    statistic=statistic,
                    value=value,
                    original_value=value,
                    unit=unit,
                    original_unit=unit,
                    normalized_value=normalized[0] if normalized else None,
                    normalized_unit=normalized[1] if normalized else None,
                    normalization_rule=normalized[2] if normalized else None,
                    evidence_type="published_table",
                    series={"arm": arm, **(scenario or {})},
                    source_pointer=loc,
                )
            )
            if re.search(r"\([+−-]?\d+(?:\.\d+)?%\)", clean):
                context(
                    f"{ri}:{ci}:{part}:delta",
                    original,
                    "parenthetical relative change is a comparison, not an independent observation",
                    loc,
                )

        carried_rate: str | None = None
        for ri, (line, cells) in enumerate(table.rows):
            row_label = _clean(cells[0])
            if layout in {"metrics", "fma", "compile"}:
                if row_label == "Completed" and path == _QPD:
                    for ci in (1, 2):
                        match = re.fullmatch(r"\s*(\d+)/(\d+), (\d+) failures\s*", cells[ci])
                        if not match:
                            raise PublicationError("unreviewed completed-request compound")
                        for part, metric in enumerate(("requests_completed", "requests_total", "error_count")):
                            emit(
                                ri,
                                ci,
                                table.header[ci],
                                (metric, None, "count"),
                                match[part + 1],
                                part,
                                shared=True,
                                offset=match.start(part + 1),
                            )
                    context(
                        f"{ri}:3",
                        cells[3],
                        "derived comparison column, not independent observation",
                        {"line": line, "row": ri, "column": 3, "header": table.header[3], "cell": cells[3]},
                    )
                    continue
                spec = _metric(row_label)
                for ci in range(1, len(cells)):
                    if re.search(r"Δ|delta", table.header[ci], re.I):
                        context(
                            f"{ri}:{ci}",
                            cells[ci],
                            "derived comparison column, not independent observation",
                            {"line": line, "row": ri, "column": ci, "header": table.header[ci], "cell": cells[ci]},
                        )
                    else:
                        emit(ri, ci, table.header[ci], spec, shared=True)
            elif layout == "wva":
                if row_label not in {"Qwen3-32B", "Qwen3-0.6B"} or _clean(cells[2]) not in {
                    "GuideLLM",
                    "inference-perf",
                }:
                    raise PublicationError("unreviewed WVA model or load generator")
                scenario = {
                    "model": row_label,
                    "duration": _clean(cells[1]),
                    "load_generator": _clean(cells[2]),
                    "aggregation": "3-run averages",
                }
                arm = f"{row_label}; {_clean(cells[1])}; {_clean(cells[2])}"
                for ci in (1, *range(3, len(cells))):
                    emit(ri, ci, arm, _metric(table.header[ci]), scenario=scenario)
            elif layout == "glm64":
                if row_label not in {"64", "128"}:
                    raise PublicationError("unreviewed GLM recommended deployment concurrency")
                scenario = {"concurrency": row_label, "deployment": "p3w2d1w2", "native_mtp": True}
                arm = "recommended 64-GPU deployment"
                emit(ri, 1, arm, ("requests_total", None, "count"), scenario=scenario)
                for ci, metric in ((2, "ttft"), (3, "itl")):
                    parts = cells[ci].split("/")
                    if len(parts) != 2:
                        raise PublicationError("unreviewed GLM deployment p50/p90 compound")
                    for part, selected in enumerate(parts):
                        emit(
                            ri,
                            ci,
                            arm,
                            (metric, ("p50", "p90")[part], None),
                            selected,
                            part,
                            offset=0 if part == 0 else cells[ci].index("/") + 1,
                            scenario=scenario,
                        )
                emit(ri, 4, arm, ("turn_completion", "p50", None), scenario=scenario)
                emit(ri, 5, arm, ("throughput", None, "output tokens/s"), scenario=scenario)
            elif layout == "predicted":
                if not re.fullmatch(_NUMBER, row_label):
                    raise PublicationError("unreviewed predicted-latency multimodal offered rate")
                scenario = {
                    "offered_rate": row_label,
                    "offered_rate_unit": "requests/s",
                    "predictor": "warm second pass",
                }
                for ci, arm in ((1, "predicted-latency"), (2, "k8s Service")):
                    emit(ri, ci, arm, ("throughput", None, "output tokens/s"), scenario=scenario)
                for ci, arm in ((3, "predicted-latency"), (4, "k8s Service")):
                    parts = cells[ci].split("/")
                    if len(parts) != 2:
                        raise PublicationError("unreviewed predicted-latency p50/p90 compound")
                    for part, selected in enumerate(parts):
                        emit(
                            ri,
                            ci,
                            arm,
                            ("ttft", ("p50", "p90")[part], "s"),
                            selected,
                            part,
                            offset=0 if part == 0 else cells[ci].index("/") + 1,
                            scenario=scenario,
                        )
            elif layout == "slo":
                if row_label not in {"Static pool sized for peak (8)", "KEDA loop (this guide)"}:
                    raise PublicationError("unreviewed SLO benchmark configuration")
                scenario = {"aggregation": "repeated scored episodes", "scope": "full episode including drain"}
                emit(ri, 1, row_label, ("combined_slo_attainment", None, "%"), scenario=scenario)
                emit(ri, 2, row_label, ("replicas", "mean", "count"), scenario=scenario)
            elif layout == "rate":
                if not re.fullmatch(_NUMBER, row_label):
                    raise PublicationError("unreviewed rate-row scenario")
                for ci in range(1, len(cells)):
                    header = table.header[ci]
                    spec = ("throughput", None, "output tokens/s") if "Output" in header else ("ttft", header[-3:], "s")
                    emit(
                        ri,
                        ci,
                        "k8s" if header.startswith("k8s") else "llm-d",
                        spec,
                        scenario={"target_rate": row_label, "target_rate_unit": "requests/s"},
                    )
            elif layout == "tier":
                config_col = table.header.index("Configuration")
                arm = _clean(cells[config_col])
                if not arm:
                    raise PublicationError("missing configuration arm")
                scenario: dict[str, JsonValue] = {}
                if "Target Rate" in table.header:
                    if table.header[0] == "Target Rate":
                        pairs = {
                            _TIER + "sglang-qwen3-32b-h100.md": ("HBM-only", "HBM + CPU RAM"),
                            _TIER + "vllm-qwen3-32b-h100.md": ("HBM-only", "HBM + CPU RAM"),
                            _TIER + "vllm-qwen3-32b-b60-xpu.md": ("VRAM-only", "VRAM + CPU RAM"),
                            _TIER + "vllm-qwen3-8b-b60-xpu.md": ("VRAM-only", "VRAM + CPU RAM"),
                            _TIER + "vllm-gpt-oss-120b-h100.md": ("optimized-baseline", "cpu-offload"),
                        }
                        if path not in pairs or arm != pairs[path][ri % 2]:
                            raise PublicationError("unreviewed alternating target-rate configuration")
                    rate = _clean(cells[table.header.index("Target Rate")])
                    if not rate:
                        # Reviewed alternating baseline/offload convention only.
                        if table.header[0] != "Target Rate" or ri % 2 != 1 or carried_rate is None:
                            raise PublicationError("unreviewed blank target rate")
                        rate = carried_rate
                        scenario["target_rate_carried_from_row"] = ri - 1
                    elif not re.fullmatch(rf"{_NUMBER} QPS", rate):
                        raise PublicationError("unreviewed target-rate scenario")
                    carried_rate = rate
                    scenario["target_rate"] = rate
                for ci, header in enumerate(table.header):
                    if header not in {"Configuration", "Target Rate"}:
                        emit(ri, ci, arm, _metric(header), scenario=scenario)
            elif layout == "weights":
                for ci in range(1, len(cells)):
                    if table.header[ci].startswith("Weight-load") or table.header[ci].startswith("Total pod"):
                        emit(ri, ci, row_label, _metric(table.header[ci]))
                    else:
                        context(
                            f"{ri}:{ci}",
                            cells[ci],
                            "storage/transport qualification or approximate effective rate; not exact observation",
                            {"line": line, "row": ri, "column": ci, "header": table.header[ci], "cell": cells[ci]},
                        )
            elif layout == "probe":
                if not re.fullmatch(_NUMBER, row_label):
                    raise PublicationError("unreviewed prefix-token scenario")
                for ci in (1, 2):
                    emit(
                        ri,
                        ci,
                        table.header[ci],
                        ("ttft", "p50" if path == _GLM else None, None),
                        scenario={
                            "prefix_tokens": row_label,
                            "consumer": "prefill leader" if path == _GLM else "aggregated",
                        },
                    )
                for ci in range(3, len(cells)):
                    if table.header[ci] == "pulled in":
                        match = re.fullmatch(rf"\s*({_NUMBER}) MB\s*", cells[ci])
                        if not match:
                            raise PublicationError("unreviewed pulled-byte cell")
                        emit(
                            ri,
                            ci,
                            "P2P pull",
                            ("pulled_bytes", None, "MB"),
                            match[1],
                            offset=match.start(1),
                            scenario={"prefix_tokens": row_label},
                        )
                    else:
                        context(
                            f"{ri}:{ci}",
                            cells[ci],
                            "derived or superseded comparison, not independent observation",
                            {"line": line, "row": ri, "column": ci, "header": table.header[ci], "cell": cells[ci]},
                        )
            elif layout == "spill":
                if row_label == "change":
                    context(
                        f"row:{ri}",
                        " | ".join(cells),
                        "derived change row, not independent observation",
                        {"line": line, "row": ri},
                    )
                elif row_label in {"precise, no pull", "precise + P2P"}:
                    for ci in range(1, len(cells)):
                        emit(ri, ci, row_label, _metric(table.header[ci]), shared=True)
                else:
                    raise PublicationError("unreviewed GLM spill arm")
            elif layout == "repeat":
                if row_label not in {"1", "2", "3", "paired mean", "paired median"}:
                    raise PublicationError("unreviewed GLM repeat observation")
                for ci in (2, 3):
                    emit(
                        ri,
                        ci,
                        table.header[ci],
                        (
                            "successful_request_rate",
                            row_label if row_label.startswith("paired") else None,
                            "requests/s",
                        ),
                        scenario={"observation": row_label, "arm_order": _clean(cells[1])},
                    )
                for ci in range(4, len(cells)):
                    context(
                        f"{ri}:{ci}",
                        cells[ci],
                        "derived paired-policy comparison, not independent observation",
                        {"line": line, "row": ri, "column": ci, "header": table.header[ci], "cell": cells[ci]},
                    )
            elif layout == "four":
                if row_label not in {"approximate", "precise"} or _clean(cells[1]) not in {"yes", "no"}:
                    raise PublicationError("unreviewed GLM four-arm scenario")
                arm = f"{row_label}; P2P={_clean(cells[1])}"
                for ci in (2, 3):
                    emit(ri, ci, arm, _metric(table.header[ci]))
                for ci, metric in ((4, "ttft"), (5, "latency")):
                    parts = cells[ci].split("/")
                    if len(parts) != 2:
                        raise PublicationError("unreviewed p50/p90 compound")
                    for part, selected in enumerate(parts):
                        emit(
                            ri,
                            ci,
                            arm,
                            (metric, ("p50", "p90")[part], "s"),
                            selected,
                            part,
                            offset=0 if part == 0 else cells[ci].index("/") + 1,
                        )
            elif layout == "qa":
                arm = f"{row_label}; run={_clean(cells[1])}"
                match = re.fullmatch(r"\s*(\d+)/(\d+)\s*", cells[2])
                if not match:
                    raise PublicationError("unreviewed ok/fail compound")
                for part, metric in enumerate(("requests_completed", "error_count")):
                    emit(ri, 2, arm, (metric, None, "count"), match[part + 1], part, offset=match.start(part + 1))
                for ci in (3, 4, 5, 6):
                    emit(ri, ci, arm, ("turn_rate", None, "turns/s") if ci == 6 else ("ttft", table.header[ci], "s"))
            elif layout in {"uniform", "hot"}:
                if not re.fullmatch(rf"{_NUMBER} req/s", row_label):
                    raise PublicationError("unreviewed offered-rate scenario")
                for ci in (1, 2, 3):
                    parts = cells[ci].split("/")
                    specs = [("request_rate", None, "requests/s"), ("latency", "p50", None)]
                    if layout == "hot":
                        specs.insert(1, ("ttft", "p50", None))
                    if len(parts) != len(specs):
                        raise PublicationError("unreviewed uniform/hot compound")
                    offset = 0
                    for part, (selected, spec) in enumerate(zip(parts, specs, strict=True)):
                        failure = re.fullmatch(rf"(.*),\s*\**({_NUMBER}) failures\**\s*", selected)
                        if failure:
                            emit(
                                ri,
                                ci,
                                table.header[ci],
                                ("error_count", None, "count"),
                                failure[2],
                                len(parts),
                                offset=offset + failure.start(2),
                                scenario={"offered_rate": row_label},
                            )
                            selected = failure[1]
                        emit(
                            ri,
                            ci,
                            table.header[ci],
                            spec,
                            selected,
                            part,
                            offset=offset,
                            scenario={"offered_rate": row_label},
                        )
                        offset += len(parts[part]) + 1
    return tuple(output)
