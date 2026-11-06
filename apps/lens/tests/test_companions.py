"""Companions: what a stack's namespace needs besides the stack, at paths the source's layout names,
stood up as their own instances before the stack and kept when it is torn down; and the file of the
PersistentVolume a claim waits for, named for an administrator."""

import json

import pytest

from lens import commands, inventory
from lens.catalog import find_stack
from lens.cli import main
from lens.companions import expand, for_stack, pv_files
from lens.errors import UsageError
from lens.kube import Kube
from lens.sources import Layout, SourceRegistry, resolve_source

from .fakes import FakeCluster

NS = "model-a-serving"
STACK = "examples/model-a/cluster-a"
POLICIES = "examples/namespaces/cluster-a/policies/model-a-serving"
CLAIM = "examples/model-a/base/model-cache-pvc.yaml"
PV = "examples/model-a/admin/model-cache-pv.yaml"
#: The policies of the stack's own cluster (the overlay's name), then the claim file beside its base.
TEMPLATES = ("examples/namespaces/{name}/policies/{namespace}", "{stack}/../base/model-cache-pvc.yaml")


@pytest.fixture
def registered(llmrepo, tmp_path):
    """A source with the companion templates of its layout."""
    state = tmp_path / "state"
    SourceRegistry(state).add("team", str(llmrepo), layout=Layout(companions=TEMPLATES))
    return ["--state-dir", str(state)]


@pytest.fixture
def cluster(monkeypatch):
    cluster = FakeCluster(namespaces=(NS,))
    cluster.serve("networking.k8s.io/v1", "NetworkPolicy", "networkpolicies")
    cluster.put(NS, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-token"}})
    cluster.put(NS, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}})
    monkeypatch.setattr(commands, "RUNNER", cluster)
    return cluster


def test_templates_name_paths_relative_to_the_stack():
    assert expand(TEMPLATES[0], STACK, NS) == POLICIES
    assert expand(TEMPLATES[1], STACK, NS) == CLAIM
    with pytest.raises(UsageError, match="leaves the source"):
        expand("{stack}/../../../../outside", STACK, NS)
    with pytest.raises(UsageError, match="unknown field"):
        expand("{cluster}/policies", STACK, NS)


def test_a_stack_finds_the_companions_that_exist(llmrepo, cluster):
    source = resolve_source(str(llmrepo))
    stack = find_stack(source, STACK)
    assert for_stack(stack, cluster, NS).chosen == []  # the source registered no templates
    layout = Layout(companions=(*TEMPLATES, "{stack}/../missing/*"))
    stack = find_stack(type(source)(**{**vars(source), "layout": layout}), STACK)
    found = for_stack(stack, cluster, NS)
    assert [item.path for item in found.chosen] == [POLICIES, CLAIM] and found.ambiguous == []
    assert [item.instance() for item in found.chosen] == ["model-a-serving-companion", "model-cache-pvc-companion"]
    # A template that matches several paths picks none.
    wide = Layout(companions=("examples/namespaces/*/policies/{namespace}",))
    found = for_stack(find_stack(type(source)(**{**vars(source), "layout": wide}), STACK), cluster, NS)
    assert found.chosen == [] and [len(tied) for tied in found.ambiguous] == [2]


def test_plan_says_what_the_namespace_lacks_and_names_the_pv_file(registered, cluster, capsys):
    assert main([*registered, "-o", "json", "plan", STACK, "-n", NS, "--name", "r1"]) == 3
    summary = json.loads(capsys.readouterr().out)
    missing = [finding for finding in summary["findings"] if finding["code"] == "companion-missing"]
    assert [finding["message"].split(" ")[0] for finding in missing] == [POLICIES, CLAIM]
    assert "--with-companions" in missing[0]["hint"]
    assert summary["cluster"]["companions"] == {POLICIES: "missing", CLAIM: "missing"}
    [pv] = [finding for finding in summary["findings"] if finding["code"] == "pv-file"]
    assert PV in pv["message"] and f"kubectl apply -f {PV}" in pv["hint"]
    assert "its claimRef must name" not in pv["hint"]  # the stack is written for this namespace


def test_standup_with_companions_stands_them_up_first_and_teardown_keeps_them(registered, cluster, capsys):
    target = [STACK, "-n", NS, "--name", "r1"]
    assert main([*registered, "standup", *target, "--with-companions"]) == 0
    out = capsys.readouterr().out
    assert out.index("Stood up companion model-a-serving-companion") < out.index("Stood up r1")
    assert "Stood up companion model-cache-pvc-companion (1 objects" in out
    kube = Kube(runner=cluster)
    assert {record["instance"] for record in inventory.list_records(kube, NS)} == {
        "r1",
        "model-a-serving-companion",
        "model-cache-pvc-companion",
    }
    assert cluster.get(NS, "networkpolicies.networking.k8s.io", "default-deny-egress") is not None
    # A second standup finds them there and stands nothing up again.
    assert main([*registered, "standup", *target, "--with-companions"]) == 0
    assert "companion" not in capsys.readouterr().out
    assert main([*registered, "teardown", "r1", "-n", NS, "-y"]) == 0
    assert cluster.get(NS, "networkpolicies.networking.k8s.io", "allow-dns") is not None
    assert cluster.get(NS, "persistentvolumeclaims", "model-a-cache") is not None


def test_a_companion_that_cannot_be_stood_up_stops_before_the_stack(registered, cluster, capsys):
    cluster.denied = {("create", "networkpolicies.networking.k8s.io")}
    assert main([*registered, "standup", STACK, "-n", NS, "--name", "r1", "--with-companions"]) == 3
    assert "companion examples/namespaces/cluster-a" in capsys.readouterr().err
    assert inventory.read(Kube(runner=cluster), NS, "r1") is None
    assert cluster.get(NS, "deployments.apps", "model-a") is None


def test_a_moved_claim_says_its_claimref_must_follow(llmrepo, cluster):
    source = resolve_source(str(llmrepo))
    assert pv_files(source, cluster, claim="model-a-cache", namespace=NS) == [PV]
    assert pv_files(source, cluster, claim="model-a-cache", namespace="other") == []
    assert pv_files(source, cluster, claim="x", namespace=NS, volume="model-a-cache-pv") == [PV]


def test_stacks_lists_the_companions_of_a_stack(registered, cluster, capsys):
    assert main([*registered, "stacks", STACK, "--companions", NS]) == 0
    out = capsys.readouterr().out
    assert f"Companions of {STACK} in {NS} (2)" in out and POLICIES in out and CLAIM in out
    assert main([*registered, "sources", "list"]) == 0
    assert f"companion  {TEMPLATES[0]}" in capsys.readouterr().out


def test_a_stack_blocked_by_something_else_stands_no_companion_up(registered, cluster, llmrepo, capsys):
    """Only a missing prerequisite can be a companion's to provide; any other block stops before them."""
    overlay = llmrepo / STACK
    (overlay / "priority.yaml").write_text(
        "apiVersion: scheduling.k8s.io/v1\nkind: PriorityClass\nmetadata: {name: serving}\nvalue: 1000\n"
    )
    kustomization = overlay / "kustomization.yaml"
    kustomization.write_text(kustomization.read_text().replace("- ../base", "- ../base\n  - priority.yaml"))
    assert main([*registered, "standup", STACK, "-n", NS, "--name", "r1", "--with-companions"]) == 3
    assert "Stood up companion" not in capsys.readouterr().out
    assert inventory.list_records(Kube(runner=cluster), NS) == []
