"""`llmd scale`: a replica count kept in the instance, applied alone, and kept by later updates."""

import json
import shutil
import sys

import pytest

from lens import commands, inventory, lifecycle
from lens.cli import main
from lens.kube import Kube

from .fakes import FakeCluster
from .test_lifecycle import plan_for
from .test_preflight import STACK, prerequisites
from .test_rollout_plan import DS, NS, cluster, rollrepo  # noqa: F401
from .test_rollout_plan import STACK as PD_STACK

DEPLOYMENTS = "deployments.apps"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


@pytest.fixture
def team(llmrepo, monkeypatch):
    """The model-a stack stood up by llmd in team-a."""
    fake = FakeCluster()
    prerequisites(fake)
    monkeypatch.setattr(commands, "RUNNER", fake)
    lifecycle.standup(Kube(runner=fake), plan_for(llmrepo, fake))
    return fake


def llmd(*argv, source):
    return main(["-o", "json", *argv, "--source", str(source)])


def test_scale_applies_only_the_workload_and_keeps_the_count(llmrepo, team, capsys):
    service = json.dumps(team.get("team-a", "services", "model-a"), sort_keys=True)
    calls = len(team.calls)
    assert llmd("scale", "r1", "model-a=3", "-n", "team-a", source=llmrepo) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["steps"] == [
        {
            "workload": "model-a",
            "object": "Deployment/model-a",
            "resource": DEPLOYMENTS,
            "from": 1,
            "to": 3,
            "drain": False,
        }
    ]
    assert team.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 3
    applied = [call for call in team.calls[calls:] if call[0] == "apply" and "--dry-run=server" not in call]
    assert len(applied) == 2  # the Deployment, then the record
    assert json.dumps(team.get("team-a", "services", "model-a"), sort_keys=True) == service
    record = inventory.require(Kube(runner=team), "team-a", "r1")
    assert record["overrides"] == {"model-a": 3}
    # A later plan (and standup) of the instance keeps the count, and says it differs from the files.
    assert main(["plan", STACK, "--source", str(llmrepo), "-n", "team-a", "--name", "r1"]) == 0
    out = capsys.readouterr().out
    assert "model-a: 3 replicas, set by `llmd scale`; the stack's files say 1" in out
    assert main(["standup", STACK, "--source", str(llmrepo), "-n", "team-a", "--name", "r1"]) == 0
    assert team.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 3
    assert inventory.require(Kube(runner=team), "team-a", "r1")["overrides"] == {"model-a": 3}


def test_status_names_the_counts_set_by_scale(llmrepo, team, capsys):
    llmd("scale", "r1", "model-a=2", "-n", "team-a", source=llmrepo)
    capsys.readouterr()
    assert main(["status", "r1", "-n", "team-a"]) == 0
    assert "scaled       model-a to 2 with `llmd scale`" in capsys.readouterr().out


def test_scale_back_to_the_files_clears_the_count(llmrepo, team, capsys):
    for back in ("model-a=--", "model-a=1"):  # explicitly, or to the count the files say
        llmd("scale", "r1", "model-a=3", "-n", "team-a", source=llmrepo)
        assert llmd("scale", "r1", back, "-n", "team-a", source=llmrepo) == 0
        assert team.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 1
        assert "overrides" not in inventory.require(Kube(runner=team), "team-a", "r1")


def test_a_dry_run_changes_nothing(llmrepo, team, capsys):
    before = json.dumps(sorted(team.objects.items()), default=str)
    assert llmd("scale", "r1", "model-a=2", "-n", "team-a", "--dry-run", source=llmrepo) == 0
    assert json.loads(capsys.readouterr().out)["dryRun"] is True
    assert json.dumps(sorted(team.objects.items()), default=str) == before


def test_scale_to_zero_asks_first(llmrepo, team, capsys):
    assert llmd("scale", "r1", "model-a=0", "-n", "team-a", source=llmrepo) == 2  # no terminal to ask
    assert team.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 1
    assert llmd("scale", "r1", "model-a=0", "-n", "team-a", "-y", source=llmrepo) == 0
    assert team.get("team-a", DEPLOYMENTS, "model-a")["spec"]["replicas"] == 0


def test_an_unknown_workload_names_the_ones_there_are(llmrepo, team, capsys):
    assert llmd("scale", "r1", "router=2", "-n", "team-a", source=llmrepo) == 2
    assert "the stack has no workload 'router'" in capsys.readouterr().err


@needs_helm
def test_a_guide_is_scaled_with_the_values_it_was_stood_up_with(llmrepo, monkeypatch, capsys):
    """The guide's --set left out the router's ClusterRole; a scale renders it the same way, so it
    neither blocks on that ClusterRole nor changes the router."""
    fake = FakeCluster()
    fake.put("team-a", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "llm-d-hf-token"}})
    monkeypatch.setattr(commands, "RUNNER", fake)
    off = ["--set", "router.monitoring.prometheus.auth.enabled=false"]
    assert main(["standup", "demo-baseline", "--source", str(llmrepo), "-n", "team-a", *off]) == 0
    epp = json.dumps(fake.get("team-a", DEPLOYMENTS, "demo-baseline-epp"), sort_keys=True)
    capsys.readouterr()
    assert llmd("scale", "demo-baseline", "demo-baseline-decode=1", "-n", "team-a", source=llmrepo) == 0
    assert fake.get("team-a", DEPLOYMENTS, "demo-baseline-decode")["spec"]["replicas"] == 1
    assert json.dumps(fake.get("team-a", DEPLOYMENTS, "demo-baseline-epp"), sort_keys=True) == epp


def test_raising_a_role_is_applied_client_side_for_an_imported_stack(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    assert main(["scale", "pd", "model-p/decode=4", "-n", NS, "--source", str(rollrepo)]) == 0
    out = capsys.readouterr().out
    assert "Scaled model-p/decode of pd: 3 -> 4" in out and "record it in the stack's repository" in out
    live = cluster.get(NS, DS, "model-p")
    assert [role["spec"]["replicas"] for role in live["spec"]["roles"]] == [4, 4]
    assert "kubectl.kubernetes.io/last-applied-configuration" in live["metadata"]["annotations"]
    assert "lens.llm-d.ai/instance" not in json.dumps(live)  # an imported stack is never labelled


def test_lowering_a_role_says_how_its_groups_drain(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    lower = ["scale", "pd", "model-p/decode=2", "-n", NS, "--source", str(rollrepo)]
    assert main(lower) == 2
    assert "removes serving groups" in capsys.readouterr().err
    assert "overrides" not in inventory.require(Kube(runner=cluster), NS, "pd")
    # --no-drain: the pods drain as they stop, and llmd lowers the role itself.
    assert main([*lower, "--no-drain"]) == 0
    live = cluster.get(NS, DS, "model-p")
    assert [role["spec"]["replicas"] for role in live["spec"]["roles"]] == [4, 2]
    assert inventory.require(Kube(runner=cluster), NS, "pd")["overrides"] == {"model-p/decode": 2}


def test_a_drain_tool_lowers_a_guarded_role_itself(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    marker = rollrepo / "drained.txt"
    write = "import sys; open(sys.argv[1], 'w').write(' '.join(sys.argv[2:]))"
    drain = f'{sys.executable} -c "{write}" {marker} {{set}} {{role}} {{count}}'
    lower = ["scale", "pd", "model-p/decode=2", "-n", NS, "--source", str(rollrepo)]
    assert main([*lower, "--drain", drain]) == 0
    assert marker.read_text() == "model-p decode 2"
    assert "by the drain command" in capsys.readouterr().out
    assert inventory.require(Kube(runner=cluster), NS, "pd")["overrides"] == {"model-p/decode": 2}
    # llmd applied nothing itself: the count on the set is the drain tool's business.
    live = cluster.get(NS, DS, "model-p")
    assert [role["spec"]["replicas"] for role in live["spec"]["roles"]] == [4, 3]


def test_a_failing_drain_records_nothing(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    drain = f"{sys.executable} -c 'raise SystemExit(3)'"
    lower = ["scale", "pd", "model-p/decode=2", "-n", NS, "--source", str(rollrepo)]
    assert main([*lower, "--drain", drain]) == 5
    assert "exited with 3" in capsys.readouterr().err
    assert "overrides" not in inventory.require(Kube(runner=cluster), NS, "pd")


def test_a_rollout_keeps_the_count(rollrepo, cluster, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(commands, "RUNNER", cluster)
    assert main(["scale", "pd", "model-p/decode=4", "-n", NS, "--source", str(rollrepo)]) == 0
    capsys.readouterr()
    args = ["-o", "json", "rollouts", "plan", PD_STACK, "-n", NS, "--name", "pd", "--source", str(rollrepo)]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["objects"] == {}  # the files say 3, the instance runs 4: nothing to roll back
    assert any(finding["code"] == "scale-override" for finding in summary["findings"])
