"""`llmd migrate`: an instance moved to another namespace, or another cluster, one replica at a time,
with the source never lowered before the target's new replica is Ready."""

import json
import shutil

import pytest

from lens import commands, inventory, lifecycle
from lens.cli import main
from lens.kube import Kube, Target

from .fakes import FakeCluster, FakeClusters
from .test_lifecycle import plan_for
from .test_preflight import STACK, prerequisites

A, B = "team-a", "team-b"
DEPLOYMENTS = "deployments.apps"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def deployed(cluster: FakeCluster, llmrepo, *, replicas=2):
    """model-a stood up by llmd in team-a with 2 replicas (a scale override)."""
    prerequisites(cluster)
    lifecycle.standup(Kube(runner=cluster), plan_for(llmrepo, cluster))
    assert main(["scale", "r1", f"model-a={replicas}", "-n", A, "--source", str(llmrepo)]) == 0


def target_ready(cluster: FakeCluster, namespace=B):
    cluster.put(namespace, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-token"}})
    cluster.put(namespace, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}})
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "model-a-cache"}}
    claim["status"] = {"phase": "Bound"}
    cluster.put(namespace, claim)


def replicas(cluster, namespace, name="model-a"):
    deployment = cluster.get(namespace, DEPLOYMENTS, name)
    return None if deployment is None else deployment["spec"]["replicas"]


@pytest.fixture
def one(llmrepo, monkeypatch, capsys):
    """Two namespaces on one cluster."""
    cluster = FakeCluster(namespaces=(A, B))
    monkeypatch.setattr(commands, "RUNNER", cluster)
    deployed(cluster, llmrepo)
    target_ready(cluster)
    capsys.readouterr()
    return cluster


def migrate(action, *extra, source):
    return main(["migrate", action, "r1", "--from", A, "--to", B, "--source", str(source), *extra])


def test_plan_counts_the_steps_and_changes_nothing(llmrepo, one, capsys):
    before = json.dumps(sorted(one.objects.items()), default=str)
    assert migrate("plan", source=llmrepo) == 0
    out = capsys.readouterr().out
    assert "Migrate r1: team-a -> team-b" in out and "workload model-a: 2 replica(s), 2 step(s)" in out
    assert "Result ready to start" in out
    assert json.dumps(sorted(one.objects.items()), default=str) == before


def test_a_whole_migration_never_lowers_the_source_first(llmrepo, one, capsys):
    assert migrate("start", source=llmrepo) == 0
    assert (replicas(one, B), replicas(one, A)) == (1, 2)
    target = inventory.require(Kube(runner=one), B, "r1")
    assert target["overrides"] == {"model-a": 1} and target["migration"]["role"] == "target"
    seen = []
    original = one._dispatch

    def watch(args, stdin):
        result = original(args, stdin)
        seen.append((" ".join(args[:3]), replicas(one, B), replicas(one, A)))
        return result

    one._dispatch = watch
    for after in ((2, 1), (2, 0)):  # (target, source) after each step
        assert migrate("step", "--interval", "0", source=llmrepo) == 0
        assert (replicas(one, B), replicas(one, A)) == after
    one._dispatch = original
    # The target is raised (and Ready) before the source is lowered, so at every call the two together
    # run at least the 2 replicas the source had: 1+2 after start, 2+2 within the first step, 2+1 after
    # it, 2+0 at the end.
    totals = [b + a for _, b, a in seen]
    assert min(totals) == 2 and max(totals) == 4
    assert all(b >= 2 for _, b, a in seen if a < 2)
    capsys.readouterr()
    assert migrate("status", source=llmrepo) == 0
    assert "done: finish it" in capsys.readouterr().out
    assert migrate("finish", "-y", source=llmrepo) == 0
    out = capsys.readouterr().out
    assert "Tore down r1 in team-a" in out and "finished" in out
    assert inventory.read(Kube(runner=one), A, "r1") is None
    record = inventory.require(Kube(runner=one), B, "r1")
    assert "migration" not in record and record["overrides"] == {"model-a": 2}  # the files say 1
    assert one.get(A, "persistentvolumeclaims", "model-a-cache") is not None  # claims stay


def test_a_step_waits_for_the_target_and_lowers_nothing_when_it_is_not_ready(llmrepo, one, capsys):
    migrate("start", source=llmrepo)
    one.controllers = False  # nothing starts the new pod
    assert migrate("step", "--timeout", "0", "--interval", "0", source=llmrepo) == 7
    assert "not Ready at 2" in capsys.readouterr().err
    assert replicas(one, A) == 2  # the source is unchanged


def test_finish_refuses_while_the_source_still_serves(llmrepo, one, capsys):
    migrate("start", source=llmrepo)
    assert migrate("finish", "-y", source=llmrepo) == 4
    assert "still runs 2" in capsys.readouterr().err
    assert inventory.read(Kube(runner=one), A, "r1") is not None


def test_a_target_namespace_with_another_instance_is_refused(llmrepo, one, capsys):
    assert main(["standup", STACK, "--source", str(llmrepo), "-n", B, "--name", "r1"]) == 0
    capsys.readouterr()
    assert migrate("plan", source=llmrepo) == 3
    assert "team-b already has an instance r1" in capsys.readouterr().out


def test_between_two_clusters(llmrepo, monkeypatch, capsys):
    first, second = FakeCluster(namespaces=(A,)), FakeCluster(namespaces=(A,))
    clusters = FakeClusters(default=first, east=first, west=second)
    monkeypatch.setattr(commands, "RUNNER", clusters)
    deployed(first, llmrepo)
    target_ready(second, namespace=A)
    capsys.readouterr()
    args = ["--from", A, "--to", A, "--from-context", "east", "--to-context", "west", "--source", str(llmrepo)]
    assert main(["migrate", "plan", "r1", *args]) == 0
    assert main(["migrate", "start", "r1", *args]) == 0
    assert replicas(second, A) == 1 and replicas(first, A) == 2
    assert main(["migrate", "step", "r1", *args, "--interval", "0"]) == 0
    assert main(["migrate", "step", "r1", *args, "--interval", "0"]) == 0
    assert replicas(second, A) == 2 and replicas(first, A) == 0
    assert main(["migrate", "finish", "r1", *args, "-y"]) == 0
    assert inventory.read(Kube(Target(context="east"), runner=clusters), A, "r1") is None
    assert inventory.read(Kube(Target(context="west"), runner=clusters), A, "r1") is not None


@needs_helm
def test_a_guide_moves_with_the_values_it_was_stood_up_with(llmrepo, monkeypatch, capsys):
    """The target is rendered with the source's --set, so the router's ClusterRole stays out there too."""
    cluster = FakeCluster(namespaces=(A, B))
    monkeypatch.setattr(commands, "RUNNER", cluster)
    for namespace in (A, B):
        cluster.put(namespace, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "llm-d-hf-token"}})
    off = ["--set", "router.monitoring.prometheus.auth.enabled=false"]
    assert main(["standup", "demo-baseline", "--source", str(llmrepo), "-n", A, *off]) == 0
    capsys.readouterr()
    args = ["demo-baseline", "--from", A, "--to", B, "--source", str(llmrepo)]
    assert main(["migrate", "plan", *args]) == 0
    assert "workload demo-baseline-decode: 2 replica(s), 2 step(s)" in capsys.readouterr().out
    assert main(["migrate", "start", *args]) == 0
    record = inventory.require(Kube(runner=cluster), B, "demo-baseline")
    assert record["stack"]["sets"] == ["router.monitoring.prometheus.auth.enabled=false"]
    assert replicas(cluster, B, "demo-baseline-decode") == 1 and replicas(cluster, A, "demo-baseline-decode") == 2
    assert replicas(cluster, B, "demo-baseline-epp") == 1  # the router is stood up whole
