"""The Argo CD backend: an Application built from the stack's plan, checked against its project, and
delivered, followed and torn down through the in-memory cluster acting as the Argo CD controller."""

import json
import re

import pytest
import yaml

from lens import inventory
from lens.argocd import application as app
from lens.argocd import checks, delivery, origin
from lens.catalog import find_stack
from lens.cli import main
from lens.errors import PolicyError, UsageError
from lens.kube import Kube
from lens.plan import Options, make_plan
from lens.process import subprocess_runner
from lens.render import render
from lens.sources import resolve_source

from .conftest import git
from .fakes import FakeCluster

STACK = "examples/model-a/cluster-a"
#: The namespace the stack's files are written for: Argo CD applies them there.
HOME = "model-a-serving"
REMOTE = "https://git.example.invalid/team/stacks.git"
TEMPLATE = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Application",
    "metadata": {"name": "{destination}-{instance}", "namespace": "argocd"},
    "spec": {
        "project": "team",
        "destination": {"name": "cluster-a"},
        "syncPolicy": {"syncOptions": ["ServerSideApply=true"]},
    },
}
PROJECT = {
    "sourceRepos": [REMOTE],
    "destinations": [{"name": "cluster-a", "namespace": HOME}],
}


@pytest.fixture
def pushed(llmrepo):
    """The test checkout with a remote branch that holds its commit, as after a push."""
    git(llmrepo, "remote", "add", "origin", REMOTE)
    git(llmrepo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return llmrepo


@pytest.fixture
def cluster():
    cluster = FakeCluster(namespaces=(HOME,))
    cluster.argocd()
    cluster.project("team", PROJECT)
    for document in (
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-token"}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "model-a"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "model-a-cache"}},
    ):
        cluster.put(HOME, document)
    return cluster


def plan_of(repo, stack=STACK, namespace=HOME, **options):
    return make_plan(
        find_stack(resolve_source(str(repo)), stack),
        Options(namespace=namespace, instance="r1", **options),
        runner=subprocess_runner,
    )


def settings(**changes):
    return delivery.Settings(template=json.loads(json.dumps(TEMPLATE)), **changes)


def attach(plan, template=None, revision=""):
    """The Application for ``plan``, as ``--backend argocd`` builds it, with its findings on the plan."""
    chosen = delivery.Settings(template=json.loads(json.dumps(template or TEMPLATE)), revision=revision)
    return delivery.attach(plan, chosen, subprocess_runner)


def renders_for(cluster, repo, application):
    """What Argo CD renders for the Application: the stack's own files, as `kubectl apply -k` would."""
    documents = render(find_stack(resolve_source(str(repo)), STACK), runner=subprocess_runner).documents
    cluster.argocd_renders[(application["metadata"]["namespace"], application["metadata"]["name"])] = documents


def codes(plan, severity="error"):
    return sorted({finding.code for finding in plan.findings if finding.severity == severity})


# -- the Application -----------------------------------------------------------------------------------
def test_a_kustomize_stack_is_one_git_source_at_the_reviewed_commit(pushed):
    plan = plan_of(pushed)
    application = attach(plan)
    revision = plan.stack.source.revision
    assert application["metadata"]["name"] == "cluster-a-r1"
    assert application["metadata"]["namespace"] == "argocd"
    assert application["metadata"]["finalizers"] == [app.FINALIZER]
    assert application["metadata"]["labels"]["lens.llm-d.ai/instance"] == "r1"
    assert application["spec"]["source"] == {"repoURL": REMOTE, "targetRevision": revision, "path": STACK}
    assert application["spec"]["destination"] == {"name": "cluster-a", "namespace": HOME}
    assert application["spec"]["syncPolicy"] == TEMPLATE["spec"]["syncPolicy"]
    assert not plan.blocking
    assert plan.summary()["application"] == application


def test_the_digest_covers_the_application(pushed):
    plain = plan_of(pushed)
    first, again = plan_of(pushed), plan_of(pushed)
    attach(first)
    attach(again, revision="main")
    assert len({plain.digest, first.digest, again.digest}) == 3


def test_a_guide_becomes_its_charts_and_overlays_with_values_from_the_checkout(pushed):
    plan = plan_of(pushed, stack="guides/demo-baseline", sets=["router.replicas=2,router.name=a\\,b"])
    application = attach(plan)
    revision = plan.stack.source.revision
    chart, overlay, values = application["spec"]["sources"]
    assert chart["path"] == "charts/router" and chart["targetRevision"] == revision
    assert chart["helm"]["releaseName"] == "demo-baseline" and chart["helm"]["skipTests"] is True
    assert chart["helm"]["valueFiles"] == [
        "$values/guides/recipes/router/base.values.yaml",
        "$values/guides/demo-baseline/router/demo-baseline.values.yaml",
    ]
    assert chart["helm"]["parameters"] == [
        {"name": "router.replicas", "value": "2"},
        {"name": "router.name", "value": "a\\,b"},
    ]
    assert overlay == {
        "repoURL": REMOTE,
        "targetRevision": revision,
        "path": "guides/demo-baseline/modelserver/gpu/vllm/base",
    }
    assert values == {"repoURL": REMOTE, "targetRevision": revision, "ref": "values"}
    # The router's metrics RBAC is cluster-scoped: Argo CD would apply it from the chart.
    assert "argocd-applies-admin-part" in codes(plan, "warning")


def test_an_oci_chart_is_a_helm_source_pinned_to_the_version_it_resolves_to(pushed, monkeypatch):
    unit = app.Unit("helm", "deploy.standalone", chart="oci://registry.example.invalid/charts/router", version="v0")
    asked = []

    def runner(argv, **options):
        asked.append(argv)
        from lens.process import Result

        return Result(tuple(argv), 0, "apiVersion: v2\nname: router\nversion: v0.11.2\n", "")

    plan = plan_of(pushed)
    found = app.GitOrigin(REMOTE, "a" * 40, "a" * 40)
    source, values, findings = app._helm_source(plan, found, unit, [], runner)
    assert source["repoURL"] == "registry.example.invalid/charts" and source["chart"] == "router"
    assert source["targetRevision"] == "v0.11.2" and not values
    assert asked == [["helm", "show", "chart", unit.chart, "--version", "v0"]]
    assert [finding.code for finding in findings] == ["argocd-chart-pinned"]


def test_manifest_files_are_one_directory_source_that_includes_them(pushed):
    plan = plan_of(pushed, stack="examples/model-c/plain", namespace="team-a")
    application = attach(plan)
    source = application["spec"]["source"]
    assert source["path"] == "examples/model-c/plain"
    assert source["directory"] == {"recurse": True, "include": "deployment.yaml"}


def test_a_checkout_argo_cd_cannot_see_blocks(llmrepo):
    plan = plan_of(llmrepo)
    attach(plan)
    assert "argocd-no-remote" in codes(plan) and not plan.delivery
    git(llmrepo, "remote", "add", "origin", REMOTE)
    plan = plan_of(llmrepo)
    attach(plan)
    assert "argocd-unpushed" in codes(plan)
    git(llmrepo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (llmrepo / STACK / "extra.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    kustomization = llmrepo / "examples/model-a/base/kustomization.yaml"
    kustomization.write_text(kustomization.read_text() + "# edited\n")
    plan = plan_of(llmrepo)
    attach(plan)
    assert {"argocd-dirty", "argocd-untracked"} <= set(codes(plan))
    # An uncommitted change elsewhere in the checkout does not reach the render.
    git(llmrepo, "checkout", "--", ".")
    (llmrepo / STACK / "extra.yaml").unlink()
    (llmrepo / "examples/model-c/plain/deployment.yaml").write_text("# changed\n")
    plan = plan_of(llmrepo)
    attach(plan)
    assert not plan.blocking


def test_objects_written_for_another_namespace_block_since_argo_cd_does_not_move_them(pushed):
    plan = plan_of(pushed, namespace="team-a")
    attach(plan)
    [finding] = [finding for finding in plan.blocking if finding.code == "argocd-namespace-written"]
    assert "model-a-serving" in finding.message and "team-a" in finding.message


def test_the_template_names_a_project_and_a_destination_cluster(pushed):
    for path, message in ((("spec", "project"), "spec.project"), (("spec", "destination"), "destination cluster")):
        template = json.loads(json.dumps(TEMPLATE))
        template[path[0]].pop(path[1])
        with pytest.raises(UsageError, match=message):
            attach(plan_of(pushed), template)


def test_a_local_checkout_below_the_repository_root_keeps_its_prefix():
    found = app.GitOrigin(REMOTE, "a" * 40, "a" * 40, prefix="team/stacks")
    assert found.path("examples/model-a") == "team/stacks/examples/model-a"
    assert found.path(".") == "team/stacks"
    assert app.GitOrigin(REMOTE, "", "").path(".") == "."


def test_credentials_never_reach_the_application():
    assert app.strip_credentials("https://user:token@git.example.invalid/a.git") == "https://git.example.invalid/a.git"
    assert app.strip_credentials("git@git.example.invalid:a.git") == "git@git.example.invalid:a.git"


def test_a_moving_target_revision_is_a_warning_and_another_commit_an_error(pushed):
    plan = plan_of(pushed)
    attach(plan, revision="main")
    assert plan.delivery["spec"]["source"]["targetRevision"] == "main"
    assert "argocd-revision-moves" in codes(plan, "warning")
    plan = plan_of(pushed)
    attach(plan, revision="b" * 40)
    assert "argocd-revision" in codes(plan)


def test_origin_resolves_a_registered_url_source_as_it_is(pushed):
    stack = find_stack(resolve_source(str(pushed)), STACK)
    found, findings = origin.resolve(stack, subprocess_runner, paths=[STACK])
    assert found.repo_url == REMOTE and found.target == stack.source.revision and not findings


def test_scale_counts_become_the_kustomize_sources_own_overrides(pushed):
    plan = plan_of(pushed, overrides={"model-a": 3})
    application = attach(plan)
    assert application["spec"]["source"]["kustomize"] == {"replicas": [{"name": "model-a", "count": 3}]}
    assert not plan.blocking


def test_a_scale_count_argo_cd_cannot_keep_blocks(pushed):
    plan = plan_of(pushed, stack="examples/model-c/plain", namespace="team-a", overrides={"model-c": 2})
    attach(plan)
    assert "argocd-scale-unsupported" in codes(plan)


def test_a_patch_target_admits_the_name_less_any_prefix():
    pattern = app._name_pattern("top-pre-group")
    assert [bool(re.match(pattern, name)) for name in ("group", "pre-group", "top-pre-group", "pre-groupie")] == [
        True,
        True,
        True,
        False,
    ]


# -- the project -----------------------------------------------------------------------------------------
def test_project_rules_follow_argo_cd():
    assert checks.source_permitted(["https://git.example.invalid/team/*"], "https://git.example.invalid/team/a.git")
    assert not checks.source_permitted(["https://git.example.invalid/*"], "https://git.example.invalid/team/a")
    assert checks.source_permitted(["https://git.example.invalid/**"], "https://git.example.invalid/team/a")
    assert not checks.source_permitted(
        ["*", "!https://git.example.invalid/team/a"], "https://git.example.invalid/team/a"
    )
    assert checks.source_permitted(["GIT@git.example.invalid:team/a.git"], "git@git.example.invalid:team/a")

    entries = [{"name": "cluster-a", "namespace": "team-*"}]
    assert checks.destination_permitted(entries, {"name": "cluster-a", "namespace": "team-b"}) is True
    assert checks.destination_permitted(entries, {"name": "cluster-a", "namespace": "other"}) is False
    # The project names the cluster by server, the template by name: not judged here.
    assert checks.destination_permitted([{"server": "*", "namespace": "a"}], {"name": "c", "namespace": "a"}) is None

    spec = {}
    assert checks.kind_permitted(spec, "apps", "Deployment", "m", namespaced=True)
    assert not checks.kind_permitted(spec, "rbac.authorization.k8s.io", "ClusterRole", "r", namespaced=False)
    spec = {"namespaceResourceWhitelist": [{"group": "apps", "kind": "*"}]}
    assert not checks.kind_permitted(spec, "", "Service", "s", namespaced=True)
    assert not checks.kind_permitted({"namespaceResourceWhitelist": []}, "apps", "Deployment", "m", namespaced=True)
    assert checks.app_namespace_permitted({"sourceNamespaces": ["team-*"]}, "team-a", "argocd")
    assert checks.app_namespace_permitted({"sourceNamespaces": ["/^team-[a-z]$/"]}, "team-a", "argocd")
    assert not checks.app_namespace_permitted({}, "team-a", "argocd")


def test_the_plan_checks_the_application_against_its_project(pushed, cluster):
    kube = Kube(runner=cluster)
    plan = plan_of(pushed)
    delivery.check(kube, plan, settings(), subprocess_runner)
    assert not plan.blocking, plan.blocking
    assert plan.cluster["argocd"] == {"application": "argocd/cluster-a-r1", "project": "checked"}
    assert plan.cluster["dryRun"] == "skipped"  # Argo CD applies; llmd only reads

    cluster.project("team", {**PROJECT, "sourceRepos": ["https://git.example.invalid/other/*"]})
    cluster.denied = {("create", "applications.argoproj.io")}
    plan = plan_of(pushed)
    delivery.check(kube, plan, settings(), subprocess_runner)
    assert {"argocd-source-refused", "argocd-permission-denied"} <= set(codes(plan))


def test_the_projects_the_argocd_install_holds_are_read_where_argo_cd_runs(pushed, cluster):
    kube = Kube(runner=cluster)
    template = json.loads(json.dumps(TEMPLATE))
    template["metadata"]["namespace"] = HOME
    plan = plan_of(pushed)
    delivery.check(kube, plan, delivery.Settings(template=template, namespace="argocd"), subprocess_runner)
    assert "argocd-app-namespace-refused" in codes(plan)
    cluster.project("team", {**PROJECT, "sourceNamespaces": [HOME]})
    plan = plan_of(pushed)
    delivery.check(kube, plan, delivery.Settings(template=template, namespace="argocd"), subprocess_runner)
    assert not plan.blocking


def test_a_cluster_without_argo_cd_blocks(pushed):
    cluster = FakeCluster(namespaces=(HOME,))
    plan = plan_of(pushed)
    delivery.check(Kube(runner=cluster), plan, settings(), subprocess_runner)
    assert "argocd-missing" in codes(plan)


# -- delivery --------------------------------------------------------------------------------------------
def stood_up(pushed, cluster, **changes):
    kube = Kube(runner=cluster)
    plan = plan_of(pushed)
    delivery.check(kube, plan, settings(**changes), subprocess_runner)
    renders_for(cluster, pushed, plan.delivery)
    return kube, plan, delivery.standup(kube, plan, plan.delivery, actor="alice", timeout=5)


def test_standup_records_then_syncs_the_application_once(pushed, cluster):
    kube, plan, result = stood_up(pushed, cluster)
    assert result["application"] == "argocd/cluster-a-r1" and result["sync"] == "started"
    assert result["argocd"]["state"] == "Synced" and result["objects"] == 2
    record = inventory.require(kube, HOME, "r1")
    assert record["phase"] == "Applied" and record["planDigest"] == plan.digest
    assert record["delivery"] == {
        "backend": "argocd",
        "application": "cluster-a-r1",
        "namespace": "argocd",
        "project": "team",
        "revision": plan.stack.source.revision,
        "createdBy": "llmd",
    }
    assert sorted((item["kind"], item["name"]) for item in record["objects"]) == [
        ("Deployment", "model-a"),
        ("Service", "model-a"),
    ]
    stored = cluster.get("argocd", "applications.argoproj.io", "cluster-a-r1")
    operation = stored["status"]["operationState"]["operation"]
    assert operation["sync"]["revision"] == plan.stack.source.revision
    assert operation["sync"]["syncOptions"] == ["ServerSideApply=true"]  # the Application's own
    assert "operation" not in stored
    # llmd applied none of the objects: Argo CD did, and tracks them.
    deployment = cluster.get(HOME, "deployments.apps", "model-a")
    assert app.tracked_by(deployment, stored)
    assert not [call for call in cluster.calls if call[0] == "apply" and "deployments" in " ".join(call)]


def test_an_automated_application_is_not_given_a_sync(pushed, cluster):
    template = json.loads(json.dumps(TEMPLATE))
    template["spec"]["syncPolicy"]["automated"] = {"prune": True}
    kube = Kube(runner=cluster)
    plan = plan_of(pushed)
    delivery.check(kube, plan, delivery.Settings(template=template), subprocess_runner)
    renders_for(cluster, pushed, plan.delivery)
    result = delivery.standup(kube, plan, plan.delivery, timeout=5)
    assert result["sync"] == "automated"
    stored = cluster.get("argocd", "applications.argoproj.io", "cluster-a-r1")
    assert stored["status"]["operationState"]["operation"] == {}


def test_status_adds_the_applications_sync_and_health(pushed, cluster):
    kube, plan, _ = stood_up(pushed, cluster)
    deployment = cluster.get(HOME, "deployments.apps", "model-a")
    deployment["status"] = {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1}
    from lens.status import instance_status

    status = instance_status(kube, HOME, "r1")
    assert status["health"] == "Ready"
    assert status["argocd"]["state"] == "Synced" and status["argocd"]["application"] == "argocd/cluster-a-r1"
    stored = cluster.get("argocd", "applications.argoproj.io", "cluster-a-r1")
    stored["status"]["sync"]["status"] = "OutOfSync"
    assert instance_status(kube, HOME, "r1")["health"] == "Progressing"
    stored["status"]["conditions"] = [{"type": "ComparisonError", "message": "kustomize build failed"}]
    status = instance_status(kube, HOME, "r1")
    assert status["health"] == "Failed" and "kustomize build failed" in status["argocd"]["message"]


def test_teardown_deletes_the_application_and_argo_cd_deletes_what_it_applied(pushed, cluster):
    kube, _, _ = stood_up(pushed, cluster)
    record = inventory.require(kube, HOME, "r1")
    result = delivery.teardown(kube, record)
    assert result["deleted"] == ["Application/cluster-a-r1"]
    assert cluster.get("argocd", "applications.argoproj.io", "cluster-a-r1") is None
    assert cluster.get(HOME, "deployments.apps", "model-a") is None
    assert inventory.read(kube, HOME, "r1") is None
    assert cluster.get(HOME, "persistentvolumeclaims", "model-a-cache") is not None  # not the stack's


def test_teardown_keeps_a_claim_only_when_it_says_delete_false(pushed, cluster):
    kube, _, _ = stood_up(pushed, cluster)
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "cache"}}
    stored = cluster.put(HOME, claim)
    record = inventory.require(kube, HOME, "r1")
    entry = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": "cache", "resource": "persistentvolumeclaims"}
    record = {**record, "objects": [*record["objects"], {**entry, "uid": stored["metadata"]["uid"]}]}
    inventory.write(kube, record)
    with pytest.raises(PolicyError, match="would delete PersistentVolumeClaim/cache"):
        delivery.teardown(kube, record)
    stored["metadata"]["annotations"] = {app.SYNC_OPTIONS_ANNOTATION: "Delete=false"}
    assert delivery.teardown(kube, record)["kept"] == ["PersistentVolumeClaim/cache"]


def test_forget_leaves_every_object_running(pushed, cluster):
    kube, _, _ = stood_up(pushed, cluster)
    result = delivery.teardown(kube, inventory.require(kube, HOME, "r1"), forget=True)
    assert result["forgot"] and cluster.get(HOME, "deployments.apps", "model-a") is not None
    assert cluster.get("argocd", "applications.argoproj.io", "cluster-a-r1") is None
    assert inventory.read(kube, HOME, "r1") is None


def test_a_second_standup_moves_the_application_to_the_new_commit(pushed, cluster):
    kube, first, _ = stood_up(pushed, cluster)
    service = pushed / "examples/model-a/base/model-server/kustomization.yaml"
    service.write_text(service.read_text().replace("  - service.yaml\n", ""))
    git(pushed, "commit", "-qam", "drop the service")
    git(pushed, "update-ref", "refs/remotes/origin/main", "HEAD")
    plan = plan_of(pushed)
    delivery.check(kube, plan, settings(), subprocess_runner)
    assert not plan.blocking and "prune" not in codes(plan, "warning")  # Argo CD prunes, not llmd
    renders_for(cluster, pushed, plan.delivery)
    delivery.standup(kube, plan, plan.delivery, timeout=5)
    record = inventory.require(kube, HOME, "r1")
    assert record["delivery"]["revision"] == plan.stack.source.revision != first.stack.source.revision
    assert [item["kind"] for item in record["objects"]] == ["Deployment"]


def test_an_object_another_application_tracks_blocks(pushed, cluster):
    kube = Kube(runner=cluster)
    cluster.put(
        HOME,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "model-a",
                "annotations": {app.TRACKING_ANNOTATION: f"other:/Service:{HOME}/model-a"},
            },
        },
    )
    plan = plan_of(pushed)
    delivery.check(kube, plan, settings(), subprocess_runner)
    assert "argocd-shared-object" in codes(plan)


# -- the commands ----------------------------------------------------------------------------------------
def test_plan_standup_status_teardown_commands(pushed, cluster, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    template = tmp_path / "application.yaml"
    template.write_text(yaml.safe_dump(TEMPLATE))
    plan = plan_of(pushed)
    delivery.attach(plan, settings(), subprocess_runner)
    renders_for(cluster, pushed, plan.delivery)
    base = ["--state-dir", str(tmp_path)]
    target = [STACK, "--source", str(pushed), "-n", HOME, "--name", "r1"]
    backend = ["--backend", "argocd", "--argocd-app", str(template)]
    assert main([*base, "plan", *target, *backend]) == 0
    out = capsys.readouterr().out
    assert "Argo CD Application argocd/cluster-a-r1 (project team)" in out and "started once by standup" in out
    assert main([*base, "plan", *target, *backend, "--offline"]) == 0
    capsys.readouterr()
    assert main([*base, "standup", *target, *backend, "--timeout", "5"]) == 0
    out = capsys.readouterr().out
    assert "through Argo CD Application argocd/cluster-a-r1" in out
    assert main([*base, "-o", "json", "status", "r1", "-n", HOME]) == 0
    assert json.loads(capsys.readouterr().out)["argocd"]["state"] == "Synced"
    assert main([*base, "teardown", "r1", "-n", HOME, "-y"]) == 0
    assert "Deleted Argo CD Application argocd/cluster-a-r1" in capsys.readouterr().out
    assert main([*base, "plan", *target, "--backend", "argocd"]) == 2
    assert "--argocd-app" in capsys.readouterr().err
