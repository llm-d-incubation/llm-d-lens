from llm_d_lens.deploy.runtime_verification import correlate_transfer, verify_runtime


def test_correlated_transfer_requires_both_roles_ranks_and_no_fallback():
    from copy import deepcopy

    report = {
        "pods": [
            {"uid": role, "role": role, "workload": "ds", "slice": "one", "domain": "rack", "revision": "rev"}
            for role in ("prefill", "decode")
        ]
    }
    snapshots = []
    for role, event, request_id in [
        ("prefill", "send-acknowledged", "prefill-id"),
        ("decode", "received", "cmpl-one-0-a1b2c3d4"),
    ]:
        engine = {"engine_id": role, "rank": 0, "world_size": 1, "supported": True, "vllm_version": "0.26.0"}
        row = {
            **engine,
            "event": event,
            "request_id": request_id,
            "timestamp": 2,
            "blocks": 2,
            "remote_engine_id": "prefill",
            "remote_request_id": "prefill-id",
        }
        snapshots.append((role, {"engines": [engine], "engines_end": [engine], "start": 1, "end": 3, "events": [row]}))
    assert correlate_transfer(report, snapshots, "cmpl-one", True)["kvTransfer"]["status"] == "pass"
    assert correlate_transfer(report, snapshots, "other", True)["kvTransfer"]["status"] == "unknown"
    assert correlate_transfer(report, snapshots, "cmpl-one", False)["kvTransfer"]["status"] == "unknown"
    missing_rank = deepcopy(snapshots)
    for item in missing_rank[1][1]["engines"] + missing_rank[1][1]["engines_end"] + missing_rank[1][1]["events"]:
        item["world_size"] = 2
    assert correlate_transfer(report, missing_rank, "cmpl-one", True)["kvTransfer"]["status"] == "unknown"
    snapshots[1][1]["events"].append({**snapshots[1][1]["events"][0], "event": "failed"})
    assert correlate_transfer(report, snapshots, "cmpl-one", True)["kvTransfer"]["status"] == "fail"


def test_missing_set_and_unattributed_transfer_never_pass():
    desired = [
        {
            "kind": "DisaggregatedSet",
            "metadata": {"name": "pd"},
            "spec": {
                "roles": [
                    {
                        "name": "decode",
                        "spec": {"leaderWorkerTemplate": {"workerTemplate": {"spec": {"containers": []}}}},
                    }
                ]
            },
        }
    ]
    report = verify_runtime(desired, [], [], [], [])
    assert report["kubernetes"]["status"] == "fail"
    assert report["kvTransfer"]["status"] == "unknown"
    assert report["routing"]["status"] == "unknown"


def test_smoke_flow_persists_report_without_manufacturing_evidence(monkeypatch, tmp_path):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from llm_d_lens.deploy import runtime_verification as module
    from llm_d_lens.deploy.run_store import JsonDeploymentRunStore

    monkeypatch.setattr(module, "inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "storage_path", lambda *args: tmp_path)
    monkeypatch.setattr(module, "_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(
        module,
        "verify_runtime",
        lambda *args, **kwargs: {"pods": [], "kubernetes": {"status": "pass"}, "placement": {"status": "pass"}},
    )
    monkeypatch.setattr(module, "_smoke_request", AsyncMock())
    persisted = Mock()
    monkeypatch.setattr(JsonDeploymentRunStore, "update_execution_provenance", persisted)

    @asynccontextmanager
    async def capture(*args):
        yield {"snapshots": [], "errors": []}

    monkeypatch.setattr(module, "transfer_capture", capture)
    report = asyncio.run(
        module.verify_execution(
            SimpleNamespace(namespace="ns", cluster_id="cluster", execution_id="execution"), [], smoke=True
        )
    )
    assert report["kvTransfer"]["status"] == "unknown"
    persisted.assert_called_once_with("execution", {"runtime_verification": report})


def test_ready_owned_pods_and_observed_generation_are_required():
    desired = [
        {
            "kind": "DisaggregatedSet",
            "metadata": {"name": "pd"},
            "spec": {
                "roles": [
                    {
                        "name": "decode",
                        "spec": {"leaderWorkerTemplate": {"workerTemplate": {"spec": {"containers": []}}}},
                    }
                ]
            },
        }
    ]
    actual = {"metadata": {"name": "pd", "uid": "ds", "generation": 2}, "status": {"observedGeneration": 1}}
    group = {
        "metadata": {
            "name": "group",
            "uid": "lws",
            "generation": 1,
            "ownerReferences": [{"uid": "ds", "controller": True}],
            "labels": {"disaggregatedset.x-k8s.io/role": "decode", "disaggregatedset.x-k8s.io/slice": "slice-a"},
        },
        "status": {"observedGeneration": 1},
    }
    pod = {
        "metadata": {"name": "pod", "uid": "pod", "ownerReferences": [{"uid": "lws", "controller": True}]},
        "spec": {"nodeName": "node"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    assert verify_runtime(desired, [actual], [group], [pod], [])["kubernetes"]["status"] == "fail"
    actual["status"]["observedGeneration"] = 2
    stateful_set = {
        "metadata": {"name": "leaders", "uid": "sts", "ownerReferences": [{"uid": "lws", "controller": True}]}
    }
    pod["metadata"]["ownerReferences"] = [{"uid": "sts", "controller": True}]
    nodes = [{"metadata": {"name": "node", "labels": {}}}]
    report = verify_runtime(desired, [actual], [group], [pod], nodes, stateful_sets=[stateful_set])
    assert report["kubernetes"]["status"] == "pass"
    assert report["kvTransfer"]["status"] == "unknown"
    group["status"]["observedGeneration"] = 0
    assert (
        verify_runtime(desired, [actual], [group], [pod], nodes, stateful_sets=[stateful_set])["kubernetes"]["status"]
        == "fail"
    )
    pod["metadata"]["ownerReferences"] = []
    assert verify_runtime(desired, [actual], [group], [pod], [])["kubernetes"]["status"] == "fail"


def test_deployment_backed_roles_are_attributed_to_their_slice():
    desired = [
        {
            "kind": "DisaggregatedSet",
            "metadata": {"name": "pd"},
            "spec": {"slices": 1, "roles": [{"name": "decode", "spec": {"replicas": 1}}]},
        }
    ]
    actual = {"metadata": {"name": "pd", "uid": "ds", "generation": 1}, "status": {"observedGeneration": 1}}
    deployment = {
        "metadata": {
            "name": "pd-0-decode",
            "uid": "deploy",
            "ownerReferences": [{"uid": "ds", "controller": True}],
            "labels": {"disaggregatedset.x-k8s.io/role": "decode", "disaggregatedset.x-k8s.io/slice": "0"},
        }
    }
    replica_set = {"metadata": {"name": "rs", "uid": "rs", "ownerReferences": [{"uid": "deploy", "controller": True}]}}
    pod = {
        "metadata": {"name": "pod", "uid": "pod", "ownerReferences": [{"uid": "rs", "controller": True}]},
        "spec": {"nodeName": "node"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    nodes = [{"metadata": {"name": "node", "labels": {}}}]
    assert verify_runtime(desired, [actual], [], [pod], nodes)["kubernetes"]["status"] == "fail"
    report = verify_runtime(desired, [actual], [], [pod], nodes, deployments=[deployment], replica_sets=[replica_set])
    assert report["kubernetes"]["status"] == "pass"
    assert report["pods"][0]["slice"] == "0"


def test_metrics_only_reads_counters_without_smoke_or_persistence(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from llm_d_lens.deploy import runtime_verification as verification

    async def fake_inventory(cluster_id, resource, namespace=None):
        return [{"metadata": {"uid": "u1", "name": "pod-a"}}] if resource == "pods" else []

    async def fake_metrics(cluster_id, namespace, pods):
        assert [pod["metadata"]["name"] for pod in pods] == ["pod-a"]
        return {"u1": {"vllm:prompt_tokens_total": 7.0}}

    def no_persist(*args):
        raise AssertionError("metrics_only must not persist a report")

    monkeypatch.setattr(verification, "inventory", fake_inventory)
    monkeypatch.setattr(verification, "_metrics", fake_metrics)
    monkeypatch.setattr(verification, "persist_verification", no_persist)
    monkeypatch.setattr(
        verification, "verify_runtime", lambda *args, **kwargs: {"pods": [{"uid": "u1", "name": "pod-a"}]}
    )
    context = SimpleNamespace(namespace="ns", cluster_id="cluster", execution_id="execution")
    report = asyncio.run(verification.verify_execution(context, [], metrics_only=True))
    assert report["metrics"] == {"pod-a": {"vllm:prompt_tokens_total": 7.0}}
    assert report["metricsCollectedAt"]
    assert "inference" not in report and "lastProbe" not in report
