"""A rollout to another stack path (``--switch-stack``): a team that keeps the overlay it runs and the one
it prepares side by side (``model-a/cluster-a`` and ``staged/model-a/cluster-a``) rolls one into the other."""

import json

import pytest

from lens import inventory
from lens.cli import main
from lens.importing import import_stack
from lens.kube import Kube

from .fakes import FakeCluster
from .test_import import deployed_with_kubectl_apply, import_plan

NS = "model-a-serving"
STAGED = "examples/staged/model-a/cluster-a"


@pytest.fixture
def cluster(llmrepo):
    """``examples/model-a/cluster-a`` deployed with ``kubectl apply -k`` and imported."""
    cluster = FakeCluster(namespaces=())
    deployed_with_kubectl_apply(llmrepo, cluster)
    import_stack(Kube(runner=cluster), import_plan(llmrepo, cluster), manager="kubectl")
    return cluster


def rollouts(tmp_path, llmrepo, action, *extra):
    return main(
        ["-o", "json", "--state-dir", str(tmp_path), "rollouts", action, STAGED, "--source", str(llmrepo)]
        + ["-n", NS, "--name", "team-model", *extra]
    )


def test_a_rollout_to_another_stack_path_needs_switch_stack(llmrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    assert rollouts(tmp_path, llmrepo, "plan") != 0
    assert "--switch-stack" in capsys.readouterr().err
    assert rollouts(tmp_path, llmrepo, "plan", "--switch-stack") == 0
    summary = json.loads(capsys.readouterr().out)
    # The staged overlay leaves out the running one's replicas patch: only the Deployment changes.
    assert summary["objects"] == {"Deployment/model-a": "changed"} and summary["unchanged"] == 1
    assert "stack-switched" in [finding["code"] for finding in summary["findings"]]
    assert not summary["blocking"]


def test_start_records_the_new_stack_and_the_one_it_came_from(llmrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    assert rollouts(tmp_path, llmrepo, "start", "--switch-stack") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] == ["Deployment/model-a"] and result["phase"] == "Finished"
    assert cluster.get(NS, "deployments.apps", "model-a")["spec"]["replicas"] == 2
    record = inventory.require(Kube(runner=cluster), NS, "team-model")
    assert record["stack"]["id"] == STAGED
    assert record["previousStack"]["id"] == "examples/model-a/cluster-a"
    assert rollouts(tmp_path, llmrepo, "plan") == 0  # the instance now runs the staged path
    assert "nothing-to-do" in [finding["code"] for finding in json.loads(capsys.readouterr().out)["findings"]]


def test_a_claim_being_deleted_blocks_the_rollout(llmrepo, cluster, tmp_path, capsys, monkeypatch):
    """The running pods keep the claim, but every pod the roll creates would stay Pending."""
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    cluster.get(NS, "persistentvolumeclaims", "model-a-cache")["metadata"]["deletionTimestamp"] = "2026-01-02T03:04:05Z"
    assert rollouts(tmp_path, llmrepo, "start", "--switch-stack") != 0
    summary = json.loads(capsys.readouterr().out)
    assert [finding["code"] for finding in summary["findings"] if finding["severity"] == "error"] == ["claim-unusable"]
    assert cluster.get(NS, "deployments.apps", "model-a")["spec"]["replicas"] == 1  # nothing applied
