"""Reviewed initial Git inventories; later refresh counts require their own review."""

from llm_d_lens.benchmark_rag.contracts import ItemKind
from llm_d_lens.benchmark_rag.repository_corpus import FigureReview
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN

INITIAL_REVISIONS = {
    LLM_D_ORIGIN: "080c14d957755da4cc363745638619fc748558f1",
    BENCHMARK_ORIGIN: "6e081de16d2debc7b29436ad53f2c62e8d475f2e",
}
INITIAL_INVENTORY_DIGESTS = {
    LLM_D_ORIGIN: "6eed79d81434e6dc3f8396ba0fa6af5f9c7c836642558db68ff1c6159fba1fb9",
    BENCHMARK_ORIGIN: "61bb48a103c2be43dba125cd9802dc5ea0333927f83a08cf6783131236a64c1e",
}
INITIAL_FORMAT_COUNTS = {
    LLM_D_ORIGIN: {"markdown": 97, "json": 5, "png": 37},
    BENCHMARK_ORIGIN: {"markdown": 28},
}
INITIAL_ITEM_COUNTS: dict[str, dict[ItemKind, int]] = {
    LLM_D_ORIGIN: {
        "source": 139,
        "experiment": 445,
        "measurement": 2153,
        "passage": 1468,
        "figure": 37,
        "figure_link": 480,
    },
    BENCHMARK_ORIGIN: {"source": 28, "passage": 392},
}
_CONTEXT_ONLY_FIGURES = (
    "agentic-serving/benchmark-results/latency_vs_throughput.png",
    "agentic-serving/benchmark-results/latency_vs_throughput_nemotron-3-ultra.png",
    "agentic-serving/benchmark-results/throughput_vs_concurrency.png",
    "agentic-serving/benchmark-results/throughput_vs_concurrency_nemotron-3-ultra.png",
    "agentic-serving/benchmark-results/ttft_vs_concurrency.png",
    "agentic-serving/benchmark-results/ttft_vs_concurrency_nemotron-3-ultra.png",
    "coord-disaggregation/benchmark-results/6Dx4GPU_4Px8GPU_DeepSeek-V2_concurrent/request_latency_distribution.png",
    "coord-disaggregation/benchmark-results/6Dx4GPU_4Px8GPU_DeepSeek-V2_concurrent/ttft_distribution.png",
    "coord-disaggregation/benchmark-results/inconcurrent_var_output_always_disaggr_pinned/request_latency_distribution.png",
    "coord-disaggregation/benchmark-results/inconcurrent_var_output_always_disaggr_pinned/ttft_distribution.png",
    "coord-disaggregation/benchmark-results/inconcurrent_var_prompt_always_disaggr_pinned/request_latency_distribution.png",
    "coord-disaggregation/benchmark-results/inconcurrent_var_prompt_always_disaggr_pinned/ttft_distribution.png",
    "multimodal-serving/aggregation/benchmark-results/latency_vs_qps.png",
    "multimodal-serving/aggregation/benchmark-results/throughput_vs_qps.png",
    "multimodal-serving/aggregation/benchmark-results/ttft_p90_vs_qps.png",
    "p2p-kv-cache-sharing/benchmark-results/gptoss-crossover.png",
    "p2p-kv-cache-sharing/benchmark-results/gptoss-docqa.png",
    "predicted-latency-routing/benchmark-results/agentic_serving_tpu_qwen480.png",
    "predicted-latency-routing/benchmark-results/code_generation_gpu_qwen32.png",
    "predicted-latency-routing/benchmark-results/multimodal_gpu_qwen3vl32b.png",
    "predicted-latency-routing/benchmark-results/pd_disaggregation_gpu_gptoss120b.png",
    "workload-autoscaling/slo-aware/benchmark-results/scored-run-overview.png",
)


def initial_figure_reviews() -> dict[str, FigureReview]:
    reviews = {
        f"guides/{path}": FigureReview(
            coverage="unknown",
            reason=(
                "No verified mapping covers the plot's numeric series. Linked text remains context; "
                "do not reconstruct unreported points or infer experiment identity from a model name."
            ),
        )
        for path in _CONTEXT_ONLY_FIGURES
    }
    for family, scenario, report in (
        ("optimized-baseline", "sglang-qwen3-32b-h100", "README.md"),
        ("optimized-baseline", "vllm-gpt-oss-120b-h100", "README.md"),
        ("optimized-baseline", "vllm-qwen3-32b-h100", "README.md"),
        ("precise-prefix-cache-routing", "sglang-qwen3-32b-h100", "../sglang-qwen3-32b-h100.md"),
        ("precise-prefix-cache-routing", "vllm-qwen3-32b-h100", "../vllm-qwen3-32b-h100.md"),
    ):
        prefix = f"guides/{family}/benchmark-results"
        report_path = f"{prefix}/{scenario}/{report}" if report == "README.md" else f"{prefix}/{scenario}.md"
        for figure in ("latency_vs_qps.png", "throughput_vs_qps.png", "ttft_p90_vs_qps.png"):
            reviews[f"{prefix}/{scenario}/{figure}"] = FigureReview(
                coverage="partial",
                report_path=report_path,
                table_indices=(1,),
                reason=(
                    "The pinned report explicitly references this image and publishes its per-rate comparison table. "
                    "Only those table observations are linked; no claim covers every plotted series or point."
                ),
            )
    for figure in ("latency_vs_throughput.png", "throughput_vs_concurrency.png", "ttft_vs_concurrency.png"):
        reviews[f"guides/agentic-serving/benchmark-results/{figure}"] = FigureReview(
            coverage="summary_only",
            report_path="guides/agentic-serving/qwen3-coder-480b-tpu.md",
            reason=(
                "The linked guide gives a 40-session summary and separate P/D comparisons, "
                "not a verified full numeric inventory of these plot series. No exact missing points are inferred."
            ),
        )
    return reviews
