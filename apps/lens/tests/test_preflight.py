import json

import pytest

from lens.catalog import find_stack
from lens.cli import main
from lens.commands import plan as plan_command
from lens.kube import Kube
from lens.plan import Options, make_plan
from lens.policy import PolicyContext, apply_policy
from lens.preflight import preflight
from lens.sources import resolve_source

from .fakes import FakeCluster

STACK = "examples/model-a/cluster-a"


@pytest.fixture
def cluster():
    return FakeCluster()


def prerequisites(cluster):
    cluster.put("team-a", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-token"}})
    cluster.put("team-a", {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}})
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "model-a-cache"}}
    cluster.put("team-a", claim)


def checked(llmrepo, cluster, namespace="team-a"):
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    plan = make_plan(stack, Options(namespace=namespace, instance="r1"), runner=cluster)
    preflight(Kube(runner=cluster), plan)
    return plan


def codes(plan, severity="error"):
    return sorted({finding.code for finding in plan.findings if finding.severity == severity})


def test_a_ready_namespace_passes_and_nothing_changes(llmrepo, cluster):
    prerequisites(cluster)
    before = dict(cluster.objects)
    plan = checked(llmrepo, cluster)
    assert not plan.blocking
    assert plan.cluster["dryRun"] == "passed"
    assert plan.cluster["objects"] == {"create": 2, "update": 0, "foreign": 0}
    assert cluster.objects == before
    assert all(item["state"] == "present" for item in plan.policy.prerequisites)


def test_missing_prerequisites_block_before_any_change(llmrepo, cluster):
    plan = checked(llmrepo, cluster)
    assert codes(plan) == ["prerequisite-missing"]
    assert plan.cluster["dryRun"] == "skipped"
    missing = {item["name"] for item in plan.policy.prerequisites if item["state"] == "missing"}
    assert missing == {"hf-token", "model-a", "model-a-cache"}


def test_a_missing_scrape_secret_warns_and_does_not_block(llmrepo, cluster):
    """The stack serves without it; only its metrics stay empty. A Secret a pod also needs still blocks."""
    prerequisites(cluster)
    cluster.serve("monitoring.coreos.com/v1", "PodMonitor", "podmonitors")
    plan = checked(llmrepo, cluster)
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": "model-a", "namespace": "team-a"},
        "spec": {
            "podMetricsEndpoints": [
                {"port": "metrics", "authorization": {"credentials": {"name": "scrape-token", "key": "t"}}},
                {"port": "other", "bearerTokenSecret": {"name": "hf-token", "key": "t"}},
            ]
        },
    }
    plan.policy = apply_policy([*plan.documents, monitor], PolicyContext(namespace="team-a", instance="r1"))
    preflight(Kube(runner=cluster), plan)
    assert not plan.blocking
    assert codes(plan, "warning") == ["monitor-secret-missing"]
    states = {item["name"]: item["state"] for item in plan.policy.prerequisites}
    assert states["scrape-token"] == "missing" and states["hf-token"] == "present"
    cluster.objects.pop(("team-a", "secrets", "hf-token"))
    plan.findings[:] = [finding for finding in plan.findings if finding.severity == "info"]
    preflight(Kube(runner=cluster), plan)
    assert codes(plan) == ["prerequisite-missing"]  # the pods need hf-token too


def test_a_missing_namespace_or_api_blocks(llmrepo, cluster):
    assert codes(checked(llmrepo, cluster, namespace="not-there")) == ["namespace-missing"]
    del cluster.served["apps/v1"]
    assert codes(checked(llmrepo, cluster)) == ["api-missing"]


def test_denied_permissions_are_reported(llmrepo, cluster):
    prerequisites(cluster)
    cluster.denied = {("create", "deployments.apps"), ("delete", "services")}
    plan = checked(llmrepo, cluster)
    assert codes(plan) == ["permission-denied"]
    assert codes(plan, "warning") == ["permission-delete"]


def test_an_object_another_tool_deployed_is_never_overwritten(llmrepo, cluster):
    prerequisites(cluster)
    cluster.put("team-a", {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model-a"}, "spec": {}})
    plan = checked(llmrepo, cluster)
    assert codes(plan) == ["object-exists"]
    assert "llmd import" in next(f.hint for f in plan.findings if f.code == "object-exists")
    assert plan.cluster["objects"]["foreign"] == 1


def test_a_plan_another_tool_applies_needs_only_reads(llmrepo, cluster):
    """With `applies=False` (Argo CD applies the stack), llmd writes only the record: no create rights on
    the stack's kinds, no dry run, no prune list; `owns` says which live objects are the instance's."""
    prerequisites(cluster)
    cluster.denied = {("create", "deployments.apps"), ("patch", "deployments.apps")}
    cluster.put("team-a", {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "model-a"}, "spec": {}})
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    plan = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=cluster)
    preflight(Kube(runner=cluster), plan, applies=False, owns=lambda live: live["kind"] == "Service")
    assert not plan.blocking, plan.blocking
    assert plan.cluster["dryRun"] == "skipped" and "prune" not in plan.cluster
    assert plan.cluster["objects"] == {"create": 1, "update": 1, "foreign": 0}
    assert not [call for call in cluster.calls if "--dry-run=server" in call]


def test_discovery_decides_what_is_cluster_scoped(llmrepo, cluster):
    overlay = llmrepo / STACK
    (overlay / "widget.yaml").write_text("apiVersion: example.invalid/v1\nkind: Widget\nmetadata: {name: w}\n")
    kustomization = overlay / "kustomization.yaml"
    kustomization.write_text(kustomization.read_text().replace("- ../base", "- ../base\n  - widget.yaml"))
    cluster.serve("example.invalid/v1", "Widget", "widgets", namespaced=False)
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    offline = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=cluster)
    assert "cluster-scoped" not in codes(offline)  # unknown offline, so it stays in the plan
    scope = plan_command._discovered_scope(Kube(runner=cluster))
    online = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=cluster, scope=scope)
    assert "cluster-scoped" in codes(online)


def test_plan_command_against_a_cluster(llmrepo, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    prerequisites(cluster)
    argv = ["--state-dir", str(tmp_path), "-o", "json", "plan", STACK, "--source", str(llmrepo)]
    assert main(argv) == 0  # the namespace comes from the kube context
    summary = json.loads(capsys.readouterr().out)
    assert summary["namespace"] == "team-a" and summary["cluster"]["dryRun"] == "passed"
    assert summary["cluster"]["user"] == "alice"


def test_a_plan_reads_each_kind_once_and_finds_the_same(llmrepo, cluster, capsys):
    prerequisites(cluster)
    service = llmrepo / "examples/model-a/base/model-server/service.yaml"
    service.write_text(
        service.read_text() + "---\n" + service.read_text().replace("name: model-a\n", "name: model-a-metrics\n", 1)
    )
    plan = checked(llmrepo, cluster)
    # One read per kind for the stack's objects and its prerequisites (the Service and the
    # Deployment, the Secret, the ServiceAccount, the claim), not one read per object.
    gets = [call for call in cluster.calls if call[0] == "get" and call[1] not in ("--raw", "namespace")]
    reads = [call[1] for call in gets if call[1] != "configmaps"]
    assert sorted(reads) == sorted(set(reads))
    names = {call[1]: [arg for arg in call[2:] if not arg.startswith("-")][0] for call in gets}
    assert len(gets) == 6 and names["configmaps"] == "lens-instance-r1"
    assert not plan.blocking and plan.cluster["objects"] == {"create": 3, "update": 0, "foreign": 0}
    assert {item["state"] for item in plan.policy.prerequisites} == {"present"}


def test_the_command_line_says_what_it_does_on_stderr(llmrepo, cluster, capsys, monkeypatch):
    from lens import commands

    prerequisites(cluster)
    monkeypatch.setattr(commands, "RUNNER", cluster)
    assert main(["-o", "json", "plan", STACK, "--source", str(llmrepo), "-n", "team-a", "--name", "r1"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["digest"]
    assert "[llmd] rendering " + STACK in out.err
    assert "[llmd] reading " in out.err and "[llmd] checking 2 object(s) against team-a" in out.err


def test_status_and_teardown_read_each_kind_once(llmrepo, cluster):
    from lens import lifecycle
    from lens.status import instance_status

    prerequisites(cluster)
    kube = Kube(runner=cluster)
    lifecycle.standup(kube, checked(llmrepo, cluster))
    cluster.calls.clear()
    instance_status(kube, "team-a", "r1")
    single = [call for call in cluster.calls if call[0] == "get" and call[1] in ("deployments.apps", "services")]
    assert len(single) == 2  # one read per kind, not one per object
    cluster.calls.clear()
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["deleted"] == ["Deployment/model-a", "Service/model-a"]
    reads = [call for call in cluster.calls if call[0] == "get" and call[1] in ("deployments.apps", "services")]
    assert len(reads) == 2
