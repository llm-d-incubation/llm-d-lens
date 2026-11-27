"""Shared progress must retain request totals and Pod readiness gating."""

import asyncio
from types import SimpleNamespace

import pytest

from llm_d_lens.simulation.backends.aiperf import _error_message as aiperf_error
from llm_d_lens.simulation.progress import report_request_progress


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts,percent,text",
    [
        ((4, 8), 50, "4 / 8 requests completed"),
        ((10, 8), 99, "10 / 8 requests completed"),
        ((4, None), 0, "4 requests completed"),
        ((4, 0), 0, "4 / 0 requests completed"),
    ],
)
async def test_progress_waits_for_readiness_and_preserves_totals(counts, percent, text):
    ready, observed = asyncio.Event(), asyncio.Event()
    calls, updates = [], []

    def read_counts():
        calls.append(True)
        return counts

    def update(value, message):
        updates.append((value, message))
        observed.set()

    task = asyncio.create_task(
        report_request_progress(
            SimpleNamespace(progress=update),
            read_counts,
            ready_event=ready,
        )
    )
    try:
        await asyncio.sleep(0)
        assert not calls
        ready.set()
        await asyncio.wait_for(observed.wait(), 2)
        assert updates[0][0] == percent
        assert text in updates[0][1]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_artifact_error_field_precedence():
    assert aiperf_error({"error": {}, "timeout": "timed out"}) == "{}"
    assert aiperf_error({"error": {"detail": "failure"}}) == "failure"
    assert aiperf_error({"message": "x" * 1100}) == "x" * 1000
    assert aiperf_error({}) is None
