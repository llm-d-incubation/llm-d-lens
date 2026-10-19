"""Regression tests for `_readiness_is_transient`.

A brief inability to reach the cluster's control plane (connection refused,
DNS/dial errors, timeouts, ...) says nothing about whether the workload itself
is healthy. These reasons must be classified as transient so a momentary
network blip doesn't permanently flip a healthy deployment to `failed`.
"""

import pytest

from llm_d_lens.deploy.service import _readiness_is_transient


@pytest.mark.parametrize(
    "reason",
    [
        "The connection to the server 10.112.110.140:33481 was refused - did you specify the right host or port?",
        "dial tcp 10.0.0.1:6443: connect: connection refused",
        "Unable to connect to the server: net/http: TLS handshake timeout",
        "Unable to connect to the server: dial tcp: lookup api.cluster: no such host",
        "read: connection reset by peer",
        "no route to host",
        "context deadline exceeded",
        "kubectl command timed out",
        "helm command timed out",
        "endpoint smoke test timed out",
        'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found',
        # The model server still loading rejects connections (not yet
        # listening); the smoke test embeds that detail in its reason string
        # (see composition.py's `_smoke_test_failure_detail`).
        "endpoint smoke test failed: urllib.error.URLError: <urlopen error [Errno 111] Connection refused>",
    ],
)
def test_connectivity_and_timeout_reasons_are_transient(reason):
    assert _readiness_is_transient([reason]) is True


@pytest.mark.parametrize(
    "reason",
    [
        "pod modelserver-0 is in CrashLoopBackOff",
        "ImagePullBackOff: repository does not exist",
        'Error from server (NotFound): secrets "llm-d-hf-token" not found',
        # A response arrived but failed the smoke test's assertion -- a real
        # workload problem, not a startup race.
        "endpoint smoke test failed: AssertionError",
    ],
)
def test_workload_failure_reasons_are_not_transient(reason):
    assert _readiness_is_transient([reason]) is False


def test_empty_reasons_are_not_transient():
    assert _readiness_is_transient([]) is False


def test_resource_shortage_waits_but_does_not_mask_crashed_workload():
    assert _readiness_is_transient(["0/1 nodes are available: Insufficient nvidia.com/gpu"])
    assert _readiness_is_transient(["waiting for ResourceClaim allocation"])
    assert not _readiness_is_transient(["Insufficient nvidia.com/gpu", "CrashLoopBackOff"])


def test_rollout_deadline_with_pending_resource_claim_keeps_waiting():
    assert _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {
            "pods": "model-0 0/1 Pending",
            "events": "FailedScheduling Insufficient nvidia.com/gpu",
        },
    )
    assert not _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {
            "pods": "model-0 0/1 CrashLoopBackOff",
            "events": "FailedScheduling Insufficient nvidia.com/gpu",
        },
    )


@pytest.mark.parametrize(
    "event",
    [
        "0/1 nodes are available: 1 cannot allocate all claims. still not schedulable, "
        "preemption: 0/1 nodes are available: 1 Preemption is not helpful for scheduling.",
        "0/2 nodes are available: 2 cannot allocate all claims.",
    ],
)
def test_pending_dra_allocation_is_transient(event):
    assert _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {"pods": "model-0 0/1 Pending", "events": f"FailedScheduling {event}"},
    )


@pytest.mark.parametrize("status", ["Running", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"])
def test_old_dra_event_does_not_hide_current_workload_failure(status):
    assert not _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {"pods": f"model-0 0/1 {status}", "events": "FailedScheduling cannot allocate all claims"},
    )


def test_pending_dra_wait_does_not_hide_another_crashed_pod():
    assert not _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {
            "pods": "model-0 0/1 Pending\nmodel-1 0/1 CrashLoopBackOff",
            "events": "FailedScheduling cannot allocate all claims",
        },
    )


def test_pending_without_resource_evidence_is_not_transient():
    assert not _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {"pods": "model-0 0/1 Pending", "events": 'persistentvolumeclaim "missing" not found'},
    )
