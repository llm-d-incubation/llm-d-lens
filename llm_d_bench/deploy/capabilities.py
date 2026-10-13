"""Stable deployment provider capabilities shared by Configuration and Evaluate."""

from __future__ import annotations


def shared_prefix_routing_workload(*, full: bool = False) -> dict:
    """Fresh routing-comparison workload; provider goals and baselines stay local."""
    return {
        "num_groups": 150 if full else 4,
        "num_prompts_per_group": 5,
        "system_prompt_len": 6000 if full else 512,
        "question_len": 1200 if full else 128,
        "output_len": 1000 if full else 64,
        "enable_multi_turn_chat": False,
        "interval": 0,
        "stages": [{"rate": rate, "duration": 60 if full else 20} for rate in ((3, 10) if full else (0.4, 0.8))],
    }


# This flag declares whether a provider exposes a second endpoint on the same warmed pods
# for a routing-bypassing Kubernetes Service comparison. Evaluate consumes this registry;
# adding a provider capability must not require provider-name checks in the router or UI.
_PROVIDER_CAPABILITIES = {
    "optimized-baseline": {
        "label": "Optimized baseline",
        "variants": [],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "required_baselines": ["kubernetes-service", "load-only", "affinity-only"],
            "experiment_variable": "routing policy",
            "default_goal": "quick-check",
            "goals": [
                {
                    "id": "quick-check",
                    "label": "Lightweight routing check",
                    "recommended": True,
                    "description": "Small shared-prefix workload for initial validation, not a capacity measurement.",
                    "scenarios": [
                        {
                            "id": "quick-prefix",
                            "name": "Lightweight shared-prefix check",
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        }
                    ],
                },
                {
                    "id": "full-evaluation",
                    "label": "Full routing evaluation",
                    "recommended": False,
                    "description": (
                        "Kubernetes RR, load-only, affinity-only, and full routing arms under a shared-prefix "
                        "saturation sweep."
                    ),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Shared-prefix routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and measure throughput, TTFT, and "
                                "prefix locality against the selected references."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload(full=True)},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": (
                        "Run the shared-prefix workload across RR, load-only, affinity-only, and full optimized "
                        "routing."
                    ),
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "load-only", "affinity-only", "optimized-baseline"],
            "evidence": ["Prefix locality", "Per-endpoint token load", "Queue growth", "TTFT and SLO capacity"],
            "validation": [
                "Expected EPP plugins",
                "Endpoint discovery",
                "Prefill calibration",
                "Router and model metrics",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
            "full_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload(full=True)},
        },
    },
    "pd-disaggregation": {
        "label": "Prefill/decode disaggregation",
        "variants": [],
        "supports_pd": True,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "serving topology",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full P/D evaluation",
                    "recommended": True,
                    "description": (
                        "Resource-parity aggregate control plus long-input, negative-control, and geometry workloads."
                    ),
                    "scenarios": [
                        {
                            "id": "long-input",
                            "name": "P/D long-prompt performance",
                            "description": (
                                "Run long-input requests at increasing concurrency to measure the TTFT and throughput "
                                "benefit of separating prefill and decode."
                            ),
                            "benchmark": {
                                "matrix": [{"isl": 5000, "osl": 250}],
                                "concurrency_stages": [
                                    {"concurrency": 1, "num_requests": 16},
                                    {"concurrency": 8, "num_requests": 32},
                                ],
                            },
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Run the reference long-input workload and aggregate control.",
                    "scenario_ids": ["long-input"],
                },
                {
                    "id": "capacity",
                    "label": "Capacity / SLO",
                    "description": "Focus on the long-input load sweep and stable serving boundary.",
                    "scenario_ids": ["long-input"],
                },
            ],
            "comparison_arms": ["aggregated", "selected-pd-topologies"],
            "evidence": ["P/D routing decisions", "KV handoff", "Prefill and Decode pressure", "TTFT/ITL trade-off"],
            "validation": [
                "Prefill role discovery",
                "Decode role discovery",
                "KV transfer path",
                "Phase-separated metrics",
            ],
            "recommended_workload": {
                "kind": "matrix",
                "matrix": [
                    {"isl": 1024, "osl": 128},
                    {"isl": 8192, "osl": 128},
                    {"isl": 16384, "osl": 128},
                ],
                "concurrency_stages": [
                    {"concurrency": 1, "num_requests": 16},
                    {"concurrency": 8, "num_requests": 32},
                ],
            },
        },
    },
    "tiered-prefix-cache": {
        "label": "Tiered prefix cache",
        "variants": [],
        "variant_description": (
            "Each selected deployment overlay becomes an independent configuration and is "
            "evaluated with the same workload."
        ),
        "supports_pd": False,
        "supports_kubernetes_service_baseline": False,
        "evaluation": {
            "required_baselines": ["direct-vllm"],
            "experiment_variable": "cache-tier",
            "default_goal": "full-evaluation",
            "goals": [
                {
                    "id": "full-evaluation",
                    "label": "Full tiered-cache evaluation",
                    "recommended": True,
                    "description": "Compare HBM-only and tiered candidates under cache pressure.",
                }
            ],
            "comparison_arms": ["hbm-only", "selected-cache-tier"],
            "evidence": ["Working-set pressure", "Cache hit/offload", "Queue growth", "TTFT and capacity"],
            "validation": ["HBM cache", "Offload activity", "Metrics availability", "Baseline parity"],
            "recommended_workload": {
                "kind": "shared-prefix",
                "shared_prefix": {
                    "num_groups": 15,
                    "num_prompts_per_group": 5,
                    "system_prompt_len": 4000,
                    "question_len": 256,
                    "output_len": 256,
                    "enable_multi_turn_chat": False,
                    "interval": 60,
                    "stages": [
                        {"rate": 1.0, "duration": 60},
                        {"rate": 1.5, "duration": 60},
                    ],
                },
            },
        },
    },
    "precise-prefix-cache-routing": {
        "label": "Precise prefix-cache routing",
        "variants": [],
        "supports_pd": False,
        "supports_kubernetes_service_baseline": True,
        "evaluation": {
            "required_baselines": ["kubernetes-service", "optimized-baseline"],
            "experiment_variable": "routing-policy",
            "default_goal": "quick-check",
            "goals": [
                {
                    "id": "quick-check",
                    "label": "Lightweight routing check",
                    "recommended": True,
                    "description": "Small shared-prefix workload for initial validation, not a capacity measurement.",
                    "scenarios": [
                        {
                            "id": "quick-prefix",
                            "name": "Lightweight shared-prefix check",
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload()},
                        }
                    ],
                },
                {
                    "id": "full-evaluation",
                    "label": "Full precise-routing evaluation",
                    "recommended": False,
                    "description": ("RR, approximate, and precise routing under reuse, load, and a low-reuse control."),
                    "scenarios": [
                        {
                            "id": "shared-prefix",
                            "name": "Precise prefix-routing performance",
                            "description": (
                                "Increase request rate for repeated long prompts and compare precise KV-aware routing "
                                "with Kubernetes round-robin."
                            ),
                            "benchmark": {"shared_prefix": shared_prefix_routing_workload(full=True)},
                        },
                    ],
                },
                {
                    "id": "guide-reproduction",
                    "label": "Guide reproduction",
                    "description": "Compare Kubernetes RR with precise routing on the guide workload.",
                    "scenario_ids": ["shared-prefix"],
                },
                {
                    "id": "precise-value",
                    "label": "Precise value isolation",
                    "description": "Compare approximate and precise prefix knowledge with the same serving shape.",
                    "scenario_ids": ["shared-prefix"],
                },
            ],
            "comparison_arms": ["kubernetes-service", "optimized-baseline", "precise-prefix-cache-routing"],
            "evidence": [
                "KV-event/index health",
                "Cached prompt fraction",
                "Recomputed prompt tokens",
                "Warm-route locality",
            ],
            "validation": [
                "Model/tokenizer identity",
                "Block-size alignment",
                "KV publishers/subscribers",
                "Index lookup activity",
            ],
            "recommended_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload()},
            "full_workload": {"kind": "shared-prefix", "shared_prefix": shared_prefix_routing_workload(full=True)},
        },
    },
}


def _supported_accelerators() -> list[str]:
    """Upstream guide variants the deploy providers can render.

    Derived from the registered hardware profiles (``upstream_variant``: xpu,
    gpu, ...) so a new vendor does not need this list edited.
    """
    try:
        from llm_d_bench.hardware.registry import all_profiles

        variants = {profile.upstream_variant for profile in all_profiles() if profile.upstream_variant}
    except Exception:  # pragma: no cover - capabilities must not fail on discovery errors
        variants = set()
    return sorted(variants)


def _hardware_variants(metadata: dict) -> dict:
    """Guide entry points are discovered from the selected source checkout."""
    from copy import deepcopy

    result = deepcopy(metadata)
    result["variants"] = []
    result.pop("variant_labels", None)
    result.get("evaluation", {}).pop("unavailable_variants", None)
    result.get("evaluation", {}).pop("default_variant", None)
    return result


def deployment_capabilities() -> list[dict]:
    return [
        {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(metadata),
        }
        for provider, metadata in _PROVIDER_CAPABILITIES.items()
    ]


def provider_capability(provider: str) -> dict | None:
    capability = _PROVIDER_CAPABILITIES.get(provider)
    return (
        None
        if capability is None
        else {
            "id": provider,
            "supported": True,
            "accelerators": _supported_accelerators(),
            **_hardware_variants(capability),
        }
    )


def provider_supports(provider: str, capability: str) -> bool:
    """Return a boolean capability without exposing the mutable registry entry."""
    return bool(_PROVIDER_CAPABILITIES.get(provider, {}).get(capability, False))
