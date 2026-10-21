"""Pure pressure coverage/rule tests."""

from __future__ import annotations

import time

from llm_d_lens.monitoring.profiling.pressure import (
    _instance_from_points,
    _instance_waiting_sustained,
    _level,
)


def _instance(*, waiting: list[tuple[float, float]], kv: list[tuple[float, float]] | None = None, expected: int = 21):
    return _instance_from_points(
        pod="decode-0",
        role="decode",
        waiting=waiting,
        kv_cache=kv or [],
        expected_samples=expected,
        latest_boundary=time.time(),
    )


def test_sustained_waiting_requires_window_coverage_and_fresh_sample():
    now = time.time()
    positive = [(now - 300 + index * 15, 4.0) for index in range(20)] + [(now, 4.0)]
    instance = _instance(waiting=positive)
    assert _instance_waiting_sustained(instance) is True

    sparse = [(now - 300 + index * 30, 4.0) for index in range(10)] + [(now, 4.0)]
    assert _instance_waiting_sustained(_instance(waiting=sparse)) is False

    zero_current = [(timestamp, 4.0) for timestamp, _ in positive[:-1]] + [(now, 0.0)]
    assert _instance_waiting_sustained(_instance(waiting=zero_current)) is False


def test_kv_saturation_is_evidence_not_an_incident_by_itself():
    now = time.time()
    waiting = [(now - 300 + index * 15, 0.0) for index in range(20)] + [(now, 0.0)]
    kv = [(now - 300 + index * 15, 96.0) for index in range(20)] + [(now, 96.0)]
    instance = _instance(waiting=waiting, kv=kv)
    assert _level([instance], None, None) == "normal"


def test_partial_instance_coverage_does_not_become_a_healthy_service():
    now = time.time()
    complete = _instance(waiting=[(now - 300 + index * 15, 0.0) for index in range(20)] + [(now, 0.0)])
    sparse = _instance(waiting=[(now - 300 + index * 30, 0.0) for index in range(10)] + [(now, 0.0)])
    assert _level([complete, sparse], None, None) == "unknown"


def test_error_rate_requires_enough_outcomes():
    assert _level([], 0.02, 99) == "unknown"
    assert _level([], 0.02, 100) == "elevated"
    assert _level([], 0.004, 1000) == "unknown"
