"""The Argo CD backend: an Application built from the stack's plan, checked against its project, and
delivered, followed and torn down through the in-memory cluster acting as the Argo CD controller."""

import json
import re

import pytest

from lens.argocd import application as app
from lens.argocd import origin
from lens.catalog import find_stack
from lens.errors import UsageError
from lens.plan import Options, make_plan
from lens.process import subprocess_runner
from lens.sources import resolve_source

from .conftest import git

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


def plan_of(repo, stack=STACK, namespace=HOME, **options):
    return make_plan(
        find_stack(resolve_source(str(repo)), stack),
        Options(namespace=namespace, instance="r1", **options),
        runner=subprocess_runner,
    )


def attach(plan, template=None, revision=""):
    """The Application for ``plan``, as ``--backend argocd`` builds it, with its findings on the plan."""
    template = json.loads(json.dumps(template or TEMPLATE))
    found, findings = origin.resolve(plan.stack, subprocess_runner, paths=origin.stack_paths(plan), target=revision)
    plan.policy.findings.extend(findings)
    if found is None:
        return None
    application, more = app.build(plan, template, found, subprocess_runner)
    plan.policy.findings.extend(more)
    plan.delivery = application
    return application


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
