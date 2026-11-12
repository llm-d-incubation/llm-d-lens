"""In-place rollouts end to end on the test cluster, which rolls like lws v0.9 and holds a
scale-down until the stack's drain tool announced the group (like a stack's admission policy)."""

import json
import sys

import pytest

from lens import commands, inventory, lifecycle
from lens.cli import main
from lens.errors import ConflictError, NotReadyError, UsageError
from lens.kube import Kube
from lens.rollouts import engine
from lens.rollouts.plan import plan_rollout

from .conftest import git
from .fakes import FakeCluster
from .test_lifecycle import plan_for
from .test_preflight import prerequisites
from .test_rollout_plan import DS, NS, POOL, STACK, cluster, new_image, rollrepo, stack_plan  # noqa: F401

#: A stand-in for the stack's drain tool: it runs until stopped and announces nothing itself; the
#: tests decide what the guards admit.
DRAIN = f"{sys.executable} -c 'import time; time.sleep(600)' {{role}} {{old_revision}} {{max_unavailable}}"


def rollout(repo, cluster):  # noqa: F811
    return plan_rollout(Kube(runner=cluster), stack_plan(repo, cluster, "import"), floors={}, pool_label=POOL)


def started(rollrepo, cluster, tmp_path, drain=DRAIN):  # noqa: F811
    new_image(rollrepo)
    planned = rollout(rollrepo, cluster)
    return planned, engine.start(Kube(runner=cluster), planned, drain=drain, state=tmp_path)


@pytest.fixture(autouse=True)
def stop_drainers(tmp_path):
    yield
    engine.Drainers(tmp_path / "rollouts" / NS / "pd").stop()


def test_start_applies_only_the_change_and_starts_one_drainer_per_role(rollrepo, cluster, tmp_path):  # noqa: F811
    before_service = json.dumps(cluster.get(NS, "services", "model-p"), sort_keys=True)
    planned, result = started(rollrepo, cluster, tmp_path)
    assert result["applied"] == ["DisaggregatedSet/model-p"] and result["rolls"] == "model-p"
    assert [item["role"] for item in result["drainers"]] == ["prefill", "decode"]
    drainer = result["drainers"][1]["command"]
    assert drainer.endswith(f"decode {planned.before.target} 1")
    assert json.dumps(cluster.get(NS, "services", "model-p"), sort_keys=True) == before_service
    live = cluster.get(NS, DS, "model-p")
    assert "kubectl.kubernetes.io/last-applied-configuration" in live["metadata"]["annotations"]  # client-side
    assert "lens.llm-d.ai/instance" not in json.dumps(live)
    record = inventory.require(Kube(runner=cluster), NS, "pd")
    assert record["rollout"]["phase"] == "Rolling" and record["rollout"]["fromRevision"] == planned.before.target


def test_a_roll_needs_a_drain_command_or_an_explicit_no_drain(rollrepo, cluster, tmp_path):  # noqa: F811
    new_image(rollrepo)
    with pytest.raises(UsageError, match="say how its groups drain"):
        engine.start(Kube(runner=cluster), rollout(rollrepo, cluster), drain="", state=tmp_path)
    result = engine.start(Kube(runner=cluster), rollout(rollrepo, cluster), drain="-", state=tmp_path)
    assert result["drainers"] == []


def test_the_roll_waits_on_the_guards_and_finishes_once_the_old_revision_is_gone(rollrepo, cluster, tmp_path):  # noqa: F811
    planned, _ = started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    cluster.refuse_scale_down = lambda lws, replicas: False  # nothing drained yet
    cluster.roll_step(NS, "model-p")
    status = engine.status(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    assert status["progress"]["rolling"] and not status["done"]
    assert len(status["drainers"]["running"]) == 2
    with pytest.raises(NotReadyError, match="not finished rolling"):
        engine.finish(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    cluster.refuse_scale_down = None  # the drainers announce each old group as they drain it
    while cluster.roll_step(NS, "model-p"):
        pass
    status = engine.status(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    assert status["done"] and status["progress"]["target"] != planned.before.target
    assert engine.watch(kube, NS, "pd", state=tmp_path, pool_label=POOL, timeout=5, interval=0)["phase"] == "Finished"
    assert inventory.require(kube, NS, "pd")["rollout"]["phase"] == "Finished"
    assert status["drainers"]["running"] and not engine.Drainers(tmp_path / "rollouts" / NS / "pd").running()


def test_pause_stops_the_drainers_and_resume_starts_them_again(rollrepo, cluster, tmp_path):  # noqa: F811
    started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    assert sorted(engine.pause(kube, NS, "pd", state=tmp_path)["stopped"]) == ["decode", "prefill"]
    assert inventory.require(kube, NS, "pd")["rollout"]["phase"] == "Paused"
    assert len(engine.resume(kube, NS, "pd", state=tmp_path)["drainers"]) == 2
    with pytest.raises(ConflictError, match="already run"):
        engine.resume(kube, NS, "pd", state=tmp_path)


def test_watch_stops_when_the_drainers_exit_early(rollrepo, cluster, tmp_path):  # noqa: F811
    started(rollrepo, cluster, tmp_path, drain=f"{sys.executable} -c pass {{role}}")
    cluster.refuse_scale_down = lambda lws, replicas: False
    cluster.roll_step(NS, "model-p")
    kube = Kube(runner=cluster)
    with pytest.raises(NotReadyError, match="drainer exited"):
        engine.watch(kube, NS, "pd", state=tmp_path, pool_label=POOL, timeout=10, interval=0.2)


def test_watch_stops_when_one_role_has_no_drainer_left(rollrepo, cluster, tmp_path):  # noqa: F811
    script = 'import sys, time; time.sleep(600 if sys.argv[1] == "prefill" else 0)'
    one = f"{sys.executable} -c '{script}' {{role}}"
    started(rollrepo, cluster, tmp_path, drain=one)
    cluster.refuse_scale_down = lambda lws, replicas: False
    cluster.roll_step(NS, "model-p")
    kube = Kube(runner=cluster)
    with pytest.raises(NotReadyError, match="the decode drainer exited while old decode groups remain"):
        engine.watch(kube, NS, "pd", state=tmp_path, pool_label=POOL, timeout=10, interval=0.2)


def test_abort_progress_counts_toward_the_restored_revision(rollrepo, cluster, tmp_path):  # noqa: F811
    planned, _ = started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    cluster.roll_step(NS, "model-p")
    engine.abort(kube, NS, "pd", server_side=False)
    current = engine.status(kube, NS, "pd", state=tmp_path, pool_label=POOL)
    assert current["progress"]["target"] == planned.before.target and not current["done"]


def test_abort_restores_the_previous_template(rollrepo, cluster, tmp_path):  # noqa: F811
    planned, _ = started(rollrepo, cluster, tmp_path)
    kube = Kube(runner=cluster)
    cluster.roll_step(NS, "model-p")
    result = engine.abort(kube, NS, "pd", server_side=False)
    assert result["restored"] == ["DisaggregatedSet/model-p"]
    assert cluster.revision(cluster.get(NS, DS, "model-p")) == planned.before.target
    while cluster.roll_step(NS, "model-p"):
        pass
    assert engine.finish(kube, NS, "pd", state=tmp_path, pool_label=POOL)["phase"] == "Aborted"


def test_one_rollout_at_a_time_and_a_reviewed_digest(rollrepo, cluster, tmp_path):  # noqa: F811
    new_image(rollrepo)
    planned = rollout(rollrepo, cluster)
    with pytest.raises(ConflictError, match="changed since it was reviewed"):
        engine.start(Kube(runner=cluster), planned, drain="-", state=tmp_path, expected_digest="0" * 64)
    engine.start(Kube(runner=cluster), planned, drain="-", state=tmp_path)
    with pytest.raises(ConflictError, match="already has a rollout in progress"):
        rollout(rollrepo, cluster)


def test_rollout_commands(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    new_image(rollrepo)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", NS, "--name", "pd"]
    assert main([*state, "rollouts", "start", *target]) == 2  # no --drain
    assert "--no-drain when each pod drains as it stops" in capsys.readouterr().err
    assert main([*state, "rollouts", "start", *target, "--drain", DRAIN]) == 0
    assert "model-p is rolling" in capsys.readouterr().out
    assert main([*state, "rollouts", "status", "pd", "-n", NS]) == 0
    assert "rollout Rolling" in capsys.readouterr().out
    assert main([*state, "rollouts", "pause", "pd", "-n", NS]) == 0
    while cluster.roll_step(NS, "model-p"):
        pass
    assert main([*state, "rollouts", "finish", "pd", "-n", NS]) == 0
    assert "finished at revision" in capsys.readouterr().out


def test_a_privileged_imported_stack_rolls_through_the_cli(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    """A stack that runs privileged pods (hostPath model caches, RDMA devices) was acknowledged
    when it was deployed; an imported stack's findings describe what runs and do not block its rollout."""
    path = rollrepo / "guides/demo-pd/modelserver/gpu/vllm/base/disaggregatedset.yaml"
    text = path.read_text().replace(
        "                - name: vllm\n",
        "                - name: vllm\n                  securityContext: {privileged: true}\n",
    )
    path.write_text(text)
    git_commit(rollrepo)
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", NS, "--name", "pd"]
    assert main([*state, "rollouts", "plan", *target]) == 0
    assert "Result ready to start" in capsys.readouterr().out
    assert main([*state, "rollouts", "start", *target, "--no-drain"]) == 0


def git_commit(repo):
    git(repo, "commit", "-q", "-am", "privileged")


def test_a_drain_command_with_another_context_is_refused(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    new_image(rollrepo)
    argv = ["--state-dir", str(tmp_path), "rollouts", "start", STACK, "--source", str(rollrepo), "-n", NS]
    assert main([*argv, "--name", "pd", "--context", "other", "--drain", DRAIN]) == 2
    assert "use-context other" in capsys.readouterr().err


def test_an_instance_llmd_stood_up_rolls_with_server_side_apply(rollrepo, tmp_path, capsys, monkeypatch):  # noqa: F811
    from .fakes import FakeCluster

    fresh = FakeCluster(namespaces=("team-a",))
    monkeypatch.setattr("lens.commands.RUNNER", fresh)
    state = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(rollrepo), "-n", "team-a", "--name", "pd"]
    assert main([*state, "standup", *target]) == 0
    new_image(rollrepo)
    assert main([*state, "rollouts", "plan", *target]) == 0
    assert "model-p: roll" in capsys.readouterr().out
    assert main([*state, "rollouts", "start", *target, "--no-drain"]) == 0
    live = fresh.get("team-a", DS, "model-p")
    assert live["metadata"]["labels"]["lens.llm-d.ai/instance"] == "pd"  # still llmd's
    assert "kubectl.kubernetes.io/last-applied-configuration" not in live["metadata"].get("annotations", {})
    while fresh.roll_step("team-a", "model-p"):
        pass
    assert main([*state, "rollouts", "finish", "pd", "-n", "team-a"]) == 0
    assert main([*state, "teardown", "pd", "-n", "team-a", "-y"]) == 0
    assert fresh.get("team-a", DS, "model-p") is None


def test_a_rollout_never_records_a_secret(rollrepo, cluster, tmp_path):  # noqa: F811
    from lens.errors import PolicyError

    base = rollrepo / "guides/demo-pd/modelserver/gpu/vllm/base"
    (base / "secret.yaml").write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: token}\nstringData: {t: x}\n")
    kustomization = base / "kustomization.yaml"
    kustomization.write_text(
        kustomization.read_text().replace("  - service.yaml\n", "  - service.yaml\n  - secret.yaml\n")
    )
    with pytest.raises(PolicyError, match="Secret/token"):
        engine.start(Kube(runner=cluster), rollout(rollrepo, cluster), drain="-", state=tmp_path)
    assert "rollout" not in inventory.require(Kube(runner=cluster), NS, "pd")


def test_the_pool_label_stays_with_the_rollout(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    """A group out of the pool is not serving. The label is the source's, recorded by `start`, so
    `status` and `finish` judge groups the same way without --pool-label or --source."""
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "sources", "add", "demo", str(rollrepo), "--pool-label", POOL]) == 0
    assert f"pool label {POOL}" in capsys.readouterr().out
    pod = next(item for key, item in cluster.objects.items() if key[1] == "pods" and "-decode-" in key[2])
    pod["metadata"]["labels"][POOL] = "false"
    new_image(rollrepo)
    target = [STACK, "-n", NS, "--name", "pd"]
    assert main([*state, "rollouts", "plan", *target]) == 3
    assert "decode: 2 of 3 group(s) are serving" in capsys.readouterr().out
    assert main([*state, "rollouts", "plan", *target, "--pool-label", ""]) == 0  # the flag wins
    capsys.readouterr()
    pod["metadata"]["labels"][POOL] = "true"
    assert main([*state, "rollouts", "start", *target, "--drain", DRAIN]) == 0
    record = inventory.require(Kube(runner=cluster), NS, "pd")
    assert record["rollout"]["poolLabel"] == POOL
    assert main([*state, "sources", "remove", "demo"]) == 0
    capsys.readouterr()
    while cluster.roll_step(NS, "model-p"):
        pass
    for key, item in cluster.objects.items():
        if key[1] == "pods" and "-decode-" in key[2]:
            item["metadata"]["labels"][POOL] = "false"  # the new groups are not in the pool yet
            break
    assert main([*state, "-o", "json", "rollouts", "status", "pd", "-n", NS]) == 0
    assert json.loads(capsys.readouterr().out)["done"] is False
    assert main([*state, "rollouts", "finish", "pd", "-n", NS]) == 7


@pytest.fixture
def router(llmrepo, monkeypatch):
    """model-a stood up by llmd, on a test cluster that reports Deployment rollouts."""
    fake = FakeCluster()
    fake.deployment_status = True
    prerequisites(fake)
    monkeypatch.setattr(commands, "RUNNER", fake)
    lifecycle.standup(Kube(runner=fake), plan_for(llmrepo, fake))
    return fake


def new_template(repo):
    path = repo / "examples/model-a/base/model-server/deployment.yaml"
    path.write_text(path.read_text().replace("sha256:0000", "sha256:1111"))
    git(repo, "commit", "-q", "-am", "new image")


def image(fake):
    return fake.get("team-a", "deployments.apps", "model-a")["spec"]["template"]["spec"]["containers"][0]["image"]


def test_a_deployment_rollout_is_followed_until_its_pods_settle(llmrepo, router, tmp_path, capsys):
    new_template(llmrepo)
    router.rolling.add("model-a")
    argv = ["--state-dir", str(tmp_path), "rollouts"]
    target = ["-n", "team-a", "--source", str(llmrepo)]
    assert main([*argv, "plan", "examples/model-a/cluster-a", "--name", "r1", *target]) == 0
    assert main([*argv, "start", "examples/model-a/cluster-a", "--name", "r1", *target]) == 0
    assert "Deployment/model-a is rolling" in capsys.readouterr().out
    assert inventory.require(Kube(runner=router), "team-a", "r1")["rollout"]["phase"] == "Rolling"
    assert main([*argv, "finish", "r1", "-n", "team-a"]) == 7  # not done yet
    capsys.readouterr()
    assert main([*argv, "status", "r1", "-n", "team-a"]) == 0
    assert "Deployment/model-a progressing" in capsys.readouterr().out
    # A pod of the old template still finishing its requests keeps the rollout going.
    router.rolling.discard("model-a")
    router.settle()
    old = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "model-a-old", "labels": {"app": "model-a"}}}
    old["metadata"]["deletionTimestamp"] = "2026-11-12T00:00:00Z"
    router.put("team-a", old)
    current = engine.status(Kube(runner=router), "team-a", "r1", state=tmp_path, pool_label="")
    assert current["workloads"][0]["terminating"] == 1 and not current["done"]
    del router.objects[("team-a", "pods", "model-a-old")]
    assert main([*argv, "status", "r1", "-n", "team-a", "--watch", "--interval", "0"]) == 0
    assert main([*argv, "finish", "r1", "-n", "team-a"]) == 0
    assert inventory.require(Kube(runner=router), "team-a", "r1")["rollout"]["phase"] == "Finished"


def test_a_deployment_rollout_can_be_aborted_until_it_is_done(llmrepo, router, tmp_path, capsys):
    before = image(router)
    new_template(llmrepo)
    router.rolling.add("model-a")
    argv = ["--state-dir", str(tmp_path), "rollouts"]
    target = ["-n", "team-a", "--source", str(llmrepo)]
    assert main([*argv, "start", "examples/model-a/cluster-a", "--name", "r1", *target]) == 0
    assert image(router) != before
    router.rolling.discard("model-a")
    assert main([*argv, "abort", "r1", "-n", "team-a", "-y"]) == 0
    assert image(router) == before
    assert main([*argv, "finish", "r1", "-n", "team-a"]) == 0
    assert inventory.require(Kube(runner=router), "team-a", "r1")["rollout"]["phase"] == "Aborted"


def test_a_change_that_replaces_no_pods_finishes_at_once(llmrepo, router, tmp_path):
    path = llmrepo / "examples/model-a/base/model-server/service.yaml"
    path.write_text(path.read_text() + "  sessionAffinity: None\n")
    git(llmrepo, "commit", "-q", "-am", "service only")
    argv = ["--state-dir", str(tmp_path), "rollouts", "start", "examples/model-a/cluster-a", "--name", "r1"]
    assert main([*argv, "-n", "team-a", "--source", str(llmrepo)]) == 0
    assert inventory.require(Kube(runner=router), "team-a", "r1")["rollout"]["phase"] == "Finished"


def test_the_plan_says_which_pods_a_template_change_replaces(llmrepo, router, tmp_path, capsys):
    new_template(llmrepo)
    argv = ["-o", "json", "rollouts", "plan", "examples/model-a/cluster-a", "--name", "r1", "-n", "team-a"]
    assert main([*argv, "--source", str(llmrepo)]) == 0
    [finding] = [item for item in json.loads(capsys.readouterr().out)["findings"] if item["code"] == "pods-restart"]
    assert finding["message"] == (
        "Deployment/model-a: the pod template changes, so its 1 pod(s) are replaced, 1 extra pod(s) at a time; "
        "the surge needs 1 nvidia.com/gpu more"
    )
    assert finding["severity"] == "warning"


def drop(repo, name, add=None):
    """Take ``name`` out of model-a's model-server kustomization (and add ``add``), and commit."""
    path = repo / "examples/model-a/base/model-server/kustomization.yaml"
    text = path.read_text().replace(f"  - {name}\n", f"  - {add}\n" if add else "")
    path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"drop {name}")


def test_prune_deletes_what_the_stack_no_longer_renders_once_unused(llmrepo, router, tmp_path, capsys):
    drop(llmrepo, "service.yaml")
    argv = ["--state-dir", str(tmp_path), "rollouts"]
    source = ["-n", "team-a", "--source", str(llmrepo)]
    assert main([*argv, "prune", "r1", *source]) == 4  # the instance still runs the older ref
    assert "but instance r1 runs" in capsys.readouterr().err
    assert main([*argv, "start", "examples/model-a/cluster-a", "--name", "r1", *source]) == 0
    out = capsys.readouterr().out
    assert "Nothing to apply" in out and "`llmd rollouts prune r1`" in out
    assert main(["-o", "json", *argv, "prune", "r1", "--dry-run", *source]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == ["Service/model-a"]
    assert router.get("team-a", "services", "model-a") is not None
    assert main([*argv, "prune", "r1", *source]) == 1  # no terminal to confirm
    assert main([*argv, "prune", "r1", "-y", *source]) == 0
    assert "delete  Service/model-a" in capsys.readouterr().out
    assert router.get("team-a", "services", "model-a") is None
    recorded = [item["kind"] for item in inventory.require(Kube(runner=router), "team-a", "r1")["objects"]]
    assert "Service" not in recorded


def test_prune_keeps_a_workload_until_it_runs_nothing(llmrepo, router, tmp_path, capsys):
    main(["scale", "r1", "model-a=2", "-n", "team-a", "--source", str(llmrepo)])
    main(["label", "r1", "model-a", "example.com/router=pd", "-n", "team-a"])
    config = llmrepo / "examples/model-a/base/model-server/config.yaml"
    config.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: model-a-config\ndata:\n  a: b\n")
    overlay = llmrepo / "examples/model-a/cluster-a/kustomization.yaml"
    overlay.write_text(overlay.read_text().replace("replicas:\n  - name: model-a\n    count: 1\n", ""))
    drop(llmrepo, "deployment.yaml", add="config.yaml")
    argv = ["--state-dir", str(tmp_path), "rollouts"]
    source = ["-n", "team-a", "--source", str(llmrepo)]
    assert main([*argv, "start", "examples/model-a/cluster-a", "--name", "r1", *source]) == 0
    capsys.readouterr()
    assert main(["-o", "json", *argv, "prune", "r1", "-y", *source]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["deleted"] == [] and result["kept"] == [
        {
            "object": "Deployment/model-a",
            "reason": "asks for 2 replica(s): lower it to 0 (`llmd scale`) while the stack still renders it",
        }
    ]
    router.get("team-a", "deployments.apps", "model-a")["spec"]["replicas"] = 0
    router.settle()
    assert main(["-o", "json", *argv, "prune", "r1", "-y", *source]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == ["Deployment/model-a"]
    record = inventory.require(Kube(runner=router), "team-a", "r1")
    assert "overrides" not in record and "podLabels" not in record


def test_what_a_pod_refers_to_is_kept():
    pod = {
        "metadata": {"name": "p"},
        "spec": {
            "serviceAccountName": "sa",
            "imagePullSecrets": [{"name": "pull"}],
            "volumes": [
                {"name": "c", "configMap": {"name": "mounted"}},
                {"name": "s", "secret": {"secretName": "certs"}},
                {"name": "x", "projected": {"sources": [{"configMap": {"name": "projected"}}]}},
            ],
            "containers": [
                {
                    "envFrom": [{"secretRef": {"name": "env-secret"}}],
                    "env": [{"name": "A", "valueFrom": {"configMapKeyRef": {"name": "one-key", "key": "a"}}}],
                }
            ],
        },
    }
    for kind, name in (
        ("ServiceAccount", "sa"),
        ("Secret", "pull"),
        ("ConfigMap", "mounted"),
        ("Secret", "certs"),
        ("ConfigMap", "projected"),
        ("Secret", "env-secret"),
        ("ConfigMap", "one-key"),
    ):
        assert engine._refers(pod, kind, name), (kind, name)
    assert not engine._refers(pod, "ConfigMap", "other") and not engine._refers(pod, "Service", "sa")


def test_a_claim_or_an_object_made_again_is_kept():
    claim = {"kind": "PersistentVolumeClaim", "name": "c", "uid": "u1"}
    live = {"kind": "PersistentVolumeClaim", "metadata": {"uid": "u1"}}
    options = {"instance": "r1", "imported": True}
    assert "--delete-volumes" in engine._still_needed(claim, live, [], delete_volumes=False, **options)
    assert engine._still_needed(claim, live, [], delete_volumes=True, **options) == ""
    made_again = {"kind": "ConfigMap", "metadata": {"uid": "u2"}}
    item = {"kind": "ConfigMap", "name": "c", "uid": "u1"}
    assert engine._still_needed(item, made_again, [], delete_volumes=True, **options) == (
        "deleted and created again outside llmd"
    )


def test_an_object_labelled_as_another_instances_is_kept(rollrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    """An imported stack can record an object another llm-d-lens instance made (a Route, say); neither
    prune nor teardown deletes it."""
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    service = cluster.get(NS, "services", "model-p")
    service["metadata"]["labels"] = {"lens.llm-d.ai/instance": "made-by-lens"}
    path = rollrepo / "guides/demo-pd/modelserver/gpu/vllm/base/kustomization.yaml"
    path.write_text(path.read_text().replace("  - service.yaml\n", ""))
    git(rollrepo, "commit", "-q", "-am", "drop the Service")
    argv = ["--state-dir", str(tmp_path), "rollouts"]
    source = ["-n", NS, "--source", str(rollrepo)]
    assert main([*argv, "start", STACK, "--name", "pd", *source]) == 0
    capsys.readouterr()
    assert main(["-o", "json", *argv, "prune", "pd", "-y", *source]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kept"] == [{"object": "Service/model-p", "reason": "labelled as llm-d-lens instance made-by-lens's"}]
    assert lifecycle.teardown(Kube(runner=cluster), NS, "pd")["skipped"] == ["Service/model-p"]
    assert cluster.get(NS, "services", "model-p") is not None
