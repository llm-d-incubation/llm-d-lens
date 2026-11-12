"""`llmd label`: labels on a workload's running pods, kept in the instance, with no restart."""

import json

import pytest

from lens import commands, inventory, lifecycle
from lens.cli import main
from lens.commands import label
from lens.kube import Kube

from .fakes import FakeCluster
from .test_lifecycle import plan_for
from .test_preflight import prerequisites
from .test_rollout_plan import NS, POOL, cluster, rollrepo  # noqa: F401

ROUTER = "router.example.com/serves"


@pytest.fixture
def team(llmrepo, monkeypatch):
    """model-a stood up by llmd in team-a, at 2 replicas."""
    fake = FakeCluster()
    prerequisites(fake)
    monkeypatch.setattr(commands, "RUNNER", fake)
    lifecycle.standup(Kube(runner=fake), plan_for(llmrepo, fake))
    main(["scale", "r1", "model-a=2", "-n", "team-a", "--source", str(llmrepo)])
    return fake


def pod_labels(fake, prefix="model-a-", namespace="team-a"):
    return {
        name: dict(pod["metadata"].get("labels") or {})
        for (ns, kind, name), pod in sorted(fake.objects.items())
        if ns == namespace and kind == "pods" and name.startswith(prefix)
    }


def test_label_patches_the_running_pods_and_keeps_the_labels(team, capsys):
    uids = {name: pod["metadata"]["uid"] for (_, kind, name), pod in team.objects.items() if kind == "pods"}
    template = json.dumps(team.get("team-a", "deployments.apps", "model-a")["spec"], sort_keys=True)
    assert main(["label", "r1", "model-a", f"{ROUTER}=pd", "role=both", "-n", "team-a"]) == 0
    assert "Labelled 2 of 2 pod(s) of model-a" in capsys.readouterr().out
    assert all(labels[ROUTER] == "pd" and labels["role"] == "both" for labels in pod_labels(team).values())
    # The same pods, and the template is unchanged: nothing restarts.
    assert {name: pod["metadata"]["uid"] for (_, kind, name), pod in team.objects.items() if kind == "pods"} == uids
    assert json.dumps(team.get("team-a", "deployments.apps", "model-a")["spec"], sort_keys=True) == template
    record = inventory.require(Kube(runner=team), "team-a", "r1")
    assert record["podLabels"] == {"model-a": {ROUTER: "pd", "role": "both"}}
    # Removing a label removes it from the pods and the record.
    assert main(["-o", "json", "label", "r1", "model-a", "role-", "-n", "team-a"]) == 0
    assert json.loads(capsys.readouterr().out)["labels"] == {ROUTER: "pd"}
    assert all("role" not in labels for labels in pod_labels(team).values())


def test_a_dry_run_changes_nothing(team, capsys):
    before = json.dumps(sorted(team.objects.items()), default=str)
    assert main(["-o", "json", "label", "r1", "model-a", f"{ROUTER}=pd", "--dry-run", "-n", "team-a"]) == 0
    assert json.loads(capsys.readouterr().out)["patched"] == ["model-a-0", "model-a-1"]
    assert json.dumps(sorted(team.objects.items()), default=str) == before


def test_keys_the_controller_selects_on_are_refused(team, capsys):
    assert main(["label", "r1", "model-a", "app=other", "-n", "team-a"]) == 3
    assert "owns its pods by app" in capsys.readouterr().err
    assert main(["label", "r1", "model-a", "pod-template-hash=x", "-n", "team-a"]) == 3
    assert all(labels["app"] == "model-a" for labels in pod_labels(team).values())


def test_the_pool_label_is_refused(team, capsys):
    assert main(["label", "r1", "model-a", f"{POOL}=false", "--pool-label", POOL, "-n", "team-a"]) == 3
    assert "a drain sets it" in capsys.readouterr().err


def test_an_unknown_workload_names_the_ones_there_are(team, capsys):
    assert main(["label", "r1", "router", "a=b", "-n", "team-a"]) == 2
    assert "Its workloads: model-a" in capsys.readouterr().err


def test_status_names_pods_without_the_kept_labels_and_scale_wait_labels_new_ones(llmrepo, team, capsys):
    main(["label", "r1", "model-a", f"{ROUTER}=pd", "-n", "team-a"])
    argv = ["scale", "r1", "model-a=3", "-n", "team-a", "--source", str(llmrepo)]
    assert main(argv) == 0  # without --wait, the new pod starts with the template's labels only
    capsys.readouterr()
    assert main(["status", "r1", "-n", "team-a"]) == 0
    assert f"1 pod(s) of model-a lack {ROUTER}=pd (kept by `llmd label`): model-a-2" in capsys.readouterr().out
    assert (
        main(["scale", "r1", "model-a=4", "--wait", "--interval", "0", "-n", "team-a", "--source", str(llmrepo)]) == 0
    )
    assert all(labels.get(ROUTER) == "pd" for labels in pod_labels(team).values())
    assert len(pod_labels(team)) == 4


def test_role_pods_keep_lws_labels(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    assert main(["label", "pd", "model-p/decode", "disaggregatedset.x-k8s.io/role=x", "-n", NS]) == 3
    assert main(["label", "pd", "model-p/decode", f"{ROUTER}=pd", "-n", NS]) == 0
    decode = pod_labels(cluster, prefix="model-p-", namespace=NS)
    assert {name for name, labels in decode.items() if labels.get(ROUTER) == "pd"} == {
        name for name, labels in decode.items() if labels.get("disaggregatedset.x-k8s.io/role") == "decode"
    }


def test_relabel_touches_only_the_named_workloads(team):
    kube = Kube(runner=team)
    record = {**inventory.require(kube, "team-a", "r1"), "podLabels": {"model-a": {ROUTER: "pd"}}}
    assert label.relabel(kube, "team-a", record, ["other"]) == []
    assert label.relabel(kube, "team-a", record, ["model-a"]) == ["model-a-0", "model-a-1"]
