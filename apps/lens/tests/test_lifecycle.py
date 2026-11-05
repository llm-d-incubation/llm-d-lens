import pytest

from lens import inventory, lifecycle
from lens.catalog import find_stack
from lens.cli import main
from lens.errors import ConflictError, PolicyError
from lens.kube import Kube
from lens.plan import Options, make_plan
from lens.preflight import preflight
from lens.sources import resolve_source

from .fakes import FakeCluster
from .test_preflight import STACK, prerequisites


@pytest.fixture
def cluster():
    cluster = FakeCluster()
    prerequisites(cluster)
    return cluster


def plan_for(llmrepo, cluster, instance="r1", stack=STACK):
    planned = make_plan(
        find_stack(resolve_source(str(llmrepo)), stack),
        Options(namespace="team-a", instance=instance),
        runner=cluster,
    )
    preflight(Kube(runner=cluster), planned)
    return planned


def test_standup_records_then_applies(llmrepo, cluster):
    plan = plan_for(llmrepo, cluster)
    result = lifecycle.standup(Kube(runner=cluster), plan, actor="alice")
    assert result["applied"] == 2 and result["pruned"] == []
    record = inventory.require(Kube(runner=cluster), "team-a", "r1")
    assert record["phase"] == "Applied" and record["management"] == {"mode": "llmd"}
    assert record["stack"]["id"] == STACK and record["planDigest"] == plan.digest
    assert {(item["kind"], item["name"]) for item in record["objects"]} == {
        ("Service", "model-a"),
        ("Deployment", "model-a"),
    }
    assert all(item["uid"] for item in record["objects"])
    deployment = cluster.get("team-a", "deployments.apps", "model-a")
    assert deployment["metadata"]["labels"]["lens.llm-d.ai/instance"] == "r1"
    assert "lens.llm-d.ai/instance" not in deployment["spec"]["template"]["metadata"]["labels"]
    # The record went in before the objects.
    applies = [call for call in cluster.calls if call[0] == "apply" and "--dry-run=server" not in call]
    assert len(applies) == 3


def test_a_second_standup_updates_and_prunes(llmrepo, cluster):
    lifecycle.standup(Kube(runner=cluster), plan_for(llmrepo, cluster))
    service = llmrepo / "examples/model-a/base/model-server/kustomization.yaml"
    service.write_text(service.read_text().replace("  - service.yaml\n", ""))
    again = plan_for(llmrepo, cluster)
    assert not again.blocking and again.cluster["objects"] == {"create": 0, "update": 1, "foreign": 0}
    assert again.cluster["prune"] == ["Service/model-a"]
    assert [f.message for f in again.findings if f.code == "prune"] == [
        "standup deletes 1 object(s) of instance r1 the stack no longer renders: Service/model-a"
    ]
    result = lifecycle.standup(Kube(runner=cluster), again)
    assert result["pruned"] == ["Service/model-a"]
    assert cluster.get("team-a", "services", "model-a") is None
    record = inventory.require(Kube(runner=cluster), "team-a", "r1")
    assert [item["kind"] for item in record["objects"]] == ["Deployment"]


def test_a_pruned_object_someone_else_took_over_is_left_alone(llmrepo, cluster):
    lifecycle.standup(Kube(runner=cluster), plan_for(llmrepo, cluster))
    cluster.put("team-a", {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model-a"}, "spec": {}})
    service = llmrepo / "examples/model-a/base/model-server/kustomization.yaml"
    service.write_text(service.read_text().replace("  - service.yaml\n", ""))
    again = plan_for(llmrepo, cluster)
    assert again.cluster["prune"] == [] and "prune" not in {f.code for f in again.findings}
    result = lifecycle.standup(Kube(runner=cluster), again)
    assert result["pruned"] == [] and cluster.get("team-a", "services", "model-a") is not None


def test_standup_refuses_a_changed_plan_and_blocking_findings(llmrepo, cluster):
    plan = plan_for(llmrepo, cluster)
    with pytest.raises(ConflictError, match="changed since the plan was reviewed"):
        lifecycle.standup(Kube(runner=cluster), plan, expected_digest="0" * 64)
    cluster.objects.pop(("team-a", "secrets", "hf-token"))
    with pytest.raises(PolicyError, match="blocking"):
        lifecycle.standup(Kube(runner=cluster), plan_for(llmrepo, cluster))
    assert inventory.read(Kube(runner=cluster), "team-a", "r1") is None


def test_one_instance_runs_one_stack(llmrepo, cluster):
    lifecycle.standup(Kube(runner=cluster), plan_for(llmrepo, cluster))
    other = plan_for(llmrepo, cluster, stack="examples/model-c/plain")
    [finding] = [finding for finding in other.blocking if finding.code == "stack-differs"]
    assert finding.message == f"instance r1 runs stack {STACK}, not examples/model-c/plain"
    with pytest.raises(PolicyError, match="blocking"):
        lifecycle.standup(Kube(runner=cluster), other)
    other.policy.findings[:] = [finding for finding in other.findings if finding.code != "stack-differs"]
    with pytest.raises(ConflictError, match="runs stack"):
        lifecycle.standup(Kube(runner=cluster), other)


def test_standup_command(llmrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    base = ["--state-dir", str(tmp_path), "standup", STACK, "--source", str(llmrepo), "--name", "r1"]
    assert main(base) == 0
    assert "Stood up r1 in team-a (2 objects" in capsys.readouterr().out
    cluster.objects.pop(("team-a", "secrets", "hf-token"))
    assert main(base) == 3
    assert "BLOCKED" in capsys.readouterr().out


def test_standup_takes_the_digest_plan_prints(llmrepo, cluster):
    plan = plan_for(llmrepo, cluster)
    with pytest.raises(ConflictError, match="changed since the plan was reviewed"):
        lifecycle.standup(Kube(runner=cluster), plan, expected_digest=plan.digest[:8])
    result = lifecycle.standup(Kube(runner=cluster), plan, expected_digest=plan.digest[:16].upper())
    assert result["digest"] == plan.digest
