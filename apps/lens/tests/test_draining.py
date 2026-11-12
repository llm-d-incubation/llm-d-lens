"""`llmd scale --drain-idle`: pods leave the router's pool, go idle, then the count is lowered."""

import json

import pytest

from lens import commands, draining, inventory, lifecycle
from lens.cli import main
from lens.errors import CommandError, ConflictError, UsageError
from lens.kube import Kube

from .conftest import git
from .fakes import DELETION_COST, FakeCluster
from .test_lifecycle import plan_for
from .test_preflight import prerequisites
from .test_rollout_plan import DS, NS, POOL, cluster, rollrepo  # noqa: F401

DEPLOYMENTS = "deployments.apps"
PATH = "examples/model-a/base/model-server"
POOL_YAML = """apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata:
  name: model-a
spec:
  selector:
    matchLabels:
      app: model-a
      llm-d.ai/in-pool: "true"
  targetPorts:
    - number: 8000
"""


def engine(fake, namespace, pod, busy=2, kv=0.4):
    """vLLM's /metrics for one pod: requests run until the pod leaves the pool."""

    def serve():
        live = fake.get(namespace, "pods", pod) or {}
        out = (live.get("metadata") or {}).get("labels", {}).get(POOL) == "false"
        running, cache = (0, 0.0) if out else (busy, kv)
        return (
            "# HELP vllm:num_requests_running Number of requests running.\n"
            f'vllm:num_requests_running{{engine="0",model_name="m"}} {running}\n'
            f'vllm:num_requests_waiting{{engine="0",model_name="m"}} 0\n'
            f'vllm:kv_cache_usage_perc{{engine="0",model_name="m"}} {cache}\n'
            f'vllm:request_success_total{{engine="0",finished_reason="stop",model_name="m"}} 7\n'
        )

    return serve


@pytest.fixture
def pooled(llmrepo, monkeypatch):
    """model-a, whose pods the InferencePool selects by llm-d.ai/in-pool, stood up at 3 replicas."""
    path = llmrepo / PATH
    deployment = path / "deployment.yaml"
    text = deployment.read_text().replace(
        "      labels:\n        app: model-a\n    spec:",
        '      labels:\n        app: model-a\n        llm-d.ai/in-pool: "true"\n    spec:',
    )
    deployment.write_text(text.replace("  replicas: 2", "  replicas: 3"))
    (path / "pool.yaml").write_text(POOL_YAML)
    kustomization = path / "kustomization.yaml"
    kustomization.write_text(kustomization.read_text() + "  - pool.yaml\n")
    overlay = llmrepo / "examples/model-a/cluster-a/kustomization.yaml"
    overlay.write_text(overlay.read_text().replace("count: 1", "count: 3"))
    git(llmrepo, "add", "-A")
    git(llmrepo, "commit", "-q", "-m", "pool label")
    fake = FakeCluster()
    prerequisites(fake)
    monkeypatch.setattr(commands, "RUNNER", fake)
    lifecycle.standup(Kube(runner=fake), plan_for(llmrepo, fake))
    for index in range(3):
        fake.metrics[(f"model-a-{index}", 8000)] = engine(fake, "team-a", f"model-a-{index}", busy=index + 1)
    return fake


def scale(llmrepo, *argv):
    base = ["-o", "json", "scale", "r1", *argv, "-n", "team-a", "--source", str(llmrepo)]
    return main([*base, "--pool-label", POOL, "--metrics-port", "8000", "--drain-interval", "0"])


def pods(fake, namespace="team-a", prefix="model-a-"):
    return sorted(
        name for (ns, kind, name) in fake.objects if ns == namespace and kind == "pods" and name.startswith(prefix)
    )


def test_parse_metrics_sums_every_series():
    text = 'a{x="1"} 2\na{x="2"} 3.5\n# a comment\nb 1\nc{y="}"} 4\nbroken{\n'
    assert draining.parse_metrics(text) == {"a": 5.5, "b": 1.0, "c": 4.0}


def test_ports_default_to_what_the_containers_declare():
    pod = {"spec": {"containers": [{"ports": [{"containerPort": 8200}, {"containerPort": 9090, "protocol": "UDP"}]}]}}
    assert draining._ports(pod, draining.Settings(pool_label=POOL)) == [8200]
    assert draining._ports(pod, draining.Settings(pool_label=POOL, ports=(8000,))) == [8000]


def test_drain_idle_removes_the_least_busy_pod_after_it_is_idle(llmrepo, pooled, capsys):
    assert scale(llmrepo, "model-a=2", "--drain-idle") == 0
    [step] = json.loads(capsys.readouterr().out)["steps"]
    assert step["drain"] is True and (step["from"], step["to"]) == (3, 2)
    assert pods(pooled) == ["model-a-1", "model-a-2"]  # model-a-0 held the fewest requests
    assert pooled.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 2
    order = [call[0] for call in pooled.calls if call[0] in ("patch", "apply")]
    # Out of the pool, then the deletion cost, then the count, then the record (as reached, and at the end).
    assert order[-5:] == ["patch", "patch", "apply", "apply", "apply"]
    assert inventory.require(Kube(runner=pooled), "team-a", "r1")["overrides"] == {"model-a": 2}


def test_the_output_says_llmd_drained_the_pods(llmrepo, pooled, capsys):
    argv = ["scale", "r1", "model-a=2", "--drain-idle", "-n", "team-a", "--source", str(llmrepo)]
    assert main([*argv, "--pool-label", POOL, "--metrics-port", "8000", "--drain-interval", "0"]) == 0
    assert "Scaled model-a of r1: 3 -> 2 (drained first)" in capsys.readouterr().out


def test_a_batch_drains_pods_together_and_lowers_once(llmrepo, pooled, monkeypatch, capsys):
    applied = []
    apply = Kube.apply
    monkeypatch.setattr(
        Kube, "apply", lambda self, docs, *a, **k: (applied.extend(docs), apply(self, docs, *a, **k))[1]
    )
    assert scale(llmrepo, "model-a=1", "--drain-idle", "--batch", "2") == 0
    assert pods(pooled) == ["model-a-2"]
    lowered = [doc["spec"]["replicas"] for doc in applied if doc["kind"] == "Deployment"]
    assert lowered[-1:] == [1] and lowered.count(1) == 1 and 2 not in lowered


def test_one_pod_at_a_time_records_each_count(llmrepo, pooled, monkeypatch, capsys):
    written = []
    write = inventory.write
    monkeypatch.setattr(
        inventory, "write", lambda kube, record: (written.append(record.get("overrides")), write(kube, record))
    )
    assert scale(llmrepo, "model-a=1", "--drain-idle") == 0
    assert pods(pooled) == ["model-a-2"]
    assert written == [{"model-a": 2}, {"model-a": 1}, {"model-a": 1}]  # each count reached, then the end


def test_a_pod_that_does_not_go_idle_goes_back_in_the_pool(llmrepo, pooled, capsys):
    pooled.metrics[("model-a-0", 8000)] = (
        "vllm:num_requests_running 1\nvllm:num_requests_waiting 0\nvllm:kv_cache_usage_perc 0.1\n"
    )
    assert scale(llmrepo, "model-a=2", "--drain-idle", "--drain-timeout", "0") == 7
    assert "did not go idle" in capsys.readouterr().err
    pod = pooled.get("team-a", "pods", "model-a-0")
    assert pod["metadata"]["labels"][POOL] == "true" and DELETION_COST not in pod["metadata"]["annotations"]
    assert pooled.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 3
    assert "overrides" not in inventory.require(Kube(runner=pooled), "team-a", "r1")


def test_metrics_that_cannot_be_read_stop_the_drain_before_anything_changes(llmrepo, pooled, capsys):
    pooled.denied.add(("get", "pods/proxy"))
    before = json.dumps(sorted(pooled.objects.items()), default=str)
    assert scale(llmrepo, "model-a=2", "--drain-idle") == 5
    assert "cannot read the vLLM metrics of pod model-a-0" in capsys.readouterr().err
    assert json.dumps(sorted(pooled.objects.items()), default=str) == before


def test_a_pool_that_does_not_select_on_the_label_is_refused(llmrepo, pooled, capsys):
    pool = pooled.get("team-a", "inferencepools.inference.networking.k8s.io", "model-a")
    del pool["spec"]["selector"]["matchLabels"][POOL]
    assert scale(llmrepo, "model-a=2", "--drain-idle") == 2
    assert "selects pod model-a-0 without llm-d.ai/in-pool=true" in capsys.readouterr().err
    assert pooled.get("team-a", "pods", "model-a-0")["metadata"]["labels"][POOL] == "true"


def test_a_pod_that_is_not_ready_refuses_the_drain(llmrepo, pooled, capsys):
    pooled.get("team-a", "pods", "model-a-2")["status"]["conditions"][0]["status"] = "False"
    assert scale(llmrepo, "model-a=2", "--drain-idle") == 4
    assert "removes a pod that is not Ready before it looks at the deletion cost" in capsys.readouterr().err
    assert pooled.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 3


def test_drain_idle_needs_a_pool_label(llmrepo, pooled, capsys):
    argv = ["scale", "r1", "model-a=2", "--drain-idle", "-n", "team-a", "--source", str(llmrepo)]
    assert main(argv) == 2
    assert "pass --pool-label" in capsys.readouterr().err.lower()


def test_the_controller_removing_another_pod_is_reported():
    """A ReplicaSet that ignores the deletion cost (none is set on its pods here) removes another pod."""
    fake = FakeCluster()
    kube = Kube(runner=fake)
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "m"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "m"}},
            "template": {"metadata": {"labels": {"app": "m", POOL: "true"}}},
        },
    }
    fake.put("team-a", deployment)
    fake.settle()
    live = kube.require(DEPLOYMENTS, "m", "team-a")
    drained = [kube.get("pods", "m-0", "team-a")]
    present = draining._names(kube, "team-a", live)
    lowered = {**deployment, "spec": {**deployment["spec"], "replicas": 1}}
    kube.apply([lowered], "team-a")  # the fake removes the highest pod, m-1
    with pytest.raises(CommandError, match="removed m-1, not only the drained pods"):
        draining._wait_removed(kube, "team-a", live, present, drained, draining.Settings(pool_label=POOL))


def test_a_role_drains_its_highest_group(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    decode = sorted(name for (ns, kind, name) in cluster.objects if kind == "pods" and "-decode-" in name)
    for name in decode:
        cluster.metrics[(name, 8000)] = engine(cluster, NS, name)
    argv = ["scale", "pd", "model-p/decode=2", "--drain-idle", "--pool-label", POOL, "--metrics-port", "8000"]
    assert main([*argv, "--drain-interval", "0", "-n", NS, "--source", str(rollrepo)]) == 0
    live = cluster.get(NS, DS, "model-p")
    assert [role["spec"]["replicas"] for role in live["spec"]["roles"]] == [4, 2]
    left = sorted(name for (ns, kind, name) in cluster.objects if kind == "pods" and "-decode-" in name)
    assert left == decode[:4]  # group 2 (its two pods) went
    drained = [call[2] for call in cluster.calls if call[0] == "patch"]
    assert drained == decode[4:]  # only that group left the pool; no deletion cost for a role
    assert inventory.require(Kube(runner=cluster), NS, "pd")["overrides"] == {"model-p/decode": 2}


def test_lowering_one_role_keeps_the_other_roles_running_count(rollrepo, cluster):  # noqa: F811
    live = cluster.get(NS, DS, "model-p")
    rendered = json.loads(json.dumps(live))
    for role in rendered["spec"]["roles"]:
        role["spec"]["replicas"] = 1  # what the plan's other overrides say, applied later
    lowered = draining._lowered(rendered, live, "model-p/decode", 2)
    assert [role["spec"]["replicas"] for role in lowered["spec"]["roles"]] == [4, 2]


def test_a_rolling_set_is_refused(rollrepo, cluster):  # noqa: F811
    pod = next(item for key, item in cluster.objects.items() if key[1] == "pods" and "-decode-" in key[2])
    pod["metadata"]["labels"]["disaggregatedset.x-k8s.io/revision"] = "other"
    with pytest.raises(ConflictError, match="is rolling"):
        draining._role_victims(Kube(runner=cluster), NS, cluster.get(NS, DS, "model-p"), "decode", 1)


def test_settings_check_runs_before_any_change():
    with pytest.raises(UsageError, match="has none"):
        draining.lower(
            Kube(runner=FakeCluster()),
            "team-a",
            {},
            DEPLOYMENTS,
            "m",
            2,
            1,
            draining.Settings(pool_label=""),
            server_side=True,
            on_lowered=lambda count: None,
        )
