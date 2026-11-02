"""Deterministic, label-free terminology and explicit-publisher query enhancement."""

from __future__ import annotations

import re

SOURCE_HINT_MULTIPLIER = 1.25
GLOSSARY = (
    ("\u7cbe\u786e\u524d\u7f00", "precise prefix matching"),
    ("\u524d\u7f00\u7f13\u5b58", "prefix cache"),
    ("\u9884\u6d4b\u5ef6\u8fdf", "predicted latency"),
    ("\u8d1f\u8f7d\u5747\u8861", "load balancing"),
    ("\u591a\u8f6e", "multi-turn conversation"),
    ("\u7f29\u5bb9", "scale down"),
    ("\u6269\u5bb9", "scale up"),
    ("\u5378\u8f7d", "offloading"),
    ("\u9884\u586b\u5145", "prefill"),
    ("\u89e3\u7801", "decode"),
    ("\u526f\u672c", "replica"),
    ("\u541e\u5410", "throughput"),
    ("\u5e76\u53d1", "concurrency"),
    ("\u4eb2\u548c", "affinity"),
    ("\u961f\u5217", "queue"),
    ("\u6307\u6807", "metrics"),
    ("\u5bb9\u5668", "container"),
    ("\u8def\u7531", "routing"),
)
SOURCE_NAMES = {
    "https://aws.amazon.com": ("AWS Amazon", r"\bAWS\b|\bAmazon\b"),
    "https://research.ibm.com": ("IBM Research", r"\bIBM\b"),
    "https://www.amd.com": ("AMD", r"\bAMD\b"),
    "https://cloud.google.com": ("Google Cloud", r"\bGoogle\s+Cloud\b|\bGCP\b"),
    "https://llm-d.ai": ("llm-d blog", r"llm-d\.ai"),
    "https://github.com": ("GitHub articles", r"(?!)"),
    "https://github.com/llm-d/llm-d": ("llm-d guides", r"(?!)"),
    "https://github.com/llm-d/llm-d-benchmark.git": ("llm-d-benchmark documentation", r"\bllm-d-benchmark\b"),
}


def expand_query(question: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not question.strip():
        raise ValueError("enhancement query must not be blank")
    additions = tuple(
        english
        for chinese, english in GLOSSARY
        if chinese in question and english.casefold() not in question.casefold()
    )[:8]
    hints = tuple(origin for origin, (_, pattern) in SOURCE_NAMES.items() if re.search(pattern, question, re.I))
    expanded = question + ("\nTerminology: " + "; ".join(additions) if additions else "")
    return expanded, additions, hints
