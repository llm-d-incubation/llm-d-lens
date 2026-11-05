"""Following an llm-d guide's ``guide.yaml`` steps without a shell."""

import shutil

import pytest

from lens.catalog import find_stack
from lens.cli import main
from lens.errors import UsageError
from lens.guides import Environment, follow, load_guide
from lens.process import subprocess_runner
from lens.render import render
from lens.sources import resolve_source

needs_tools = pytest.mark.skipif(
    shutil.which("helm") is None or (shutil.which("kustomize") is None and shutil.which("kubectl") is None),
    reason="needs helm and kustomize",
)

GUIDE = "guides/demo-baseline"


def followed(llmrepo, **overrides):
    guide = load_guide(llmrepo / GUIDE, GUIDE)
    return follow(guide, llmrepo, namespace="team-a", overrides=overrides)


def steps(result):
    return {item["step"]: item["state"] for item in result.steps}


def test_the_standalone_router_and_the_variant_overlay_are_followed(llmrepo):
    result = followed(llmrepo)
    assert [unit.kind for unit in result.units] == ["helm", "kustomize"]
    router, overlay = result.units
    assert router.release == "demo-baseline" and router.version == "0.1.0"
    assert router.chart == str((llmrepo / "charts/router").resolve())
    assert [path.name for path in router.values] == ["base.values.yaml", "demo-baseline.values.yaml"]
    assert overlay.path == (llmrepo / "guides/demo-baseline/modelserver/gpu/vllm/base").resolve()
    assert steps(result) == {
        "prerequisites.clone": "not needed",
        "prerequisites.gaie": "administrator",
        "prerequisites.namespace": "yours",
        "prerequisites.secrets": "yours",
        "deploy.router_values": "variables",
        "deploy.monitoring_values": "when",
        "deploy.standalone": "followed",
        "deploy.gateway": "mode",
        "deploy.modelserver.standard": "followed",
        "deploy.modelserver.dynamic_slice": "other variant",
    }
    assert result.findings == []
    assert result.variables["GAIE_URL"] == "releases/latest/download"  # env.sh's if block


def test_variables_pick_the_variant(llmrepo):
    result = followed(llmrepo, INFRA_PROVIDER="ocp", GAIE_VERSION="v1.2.0")
    assert result.units[1].path.name == "ocp"
    assert result.variables["GAIE_URL"] == "releases/download/v1.2.0"
    missing = followed(llmrepo, INFRA_PROVIDER="aws")
    [finding] = [finding for finding in missing.findings if finding.severity == "error"]
    assert finding.code == "guide-variant-missing" and "deploy.modelserver" in finding.message
    assert "INFRA_PROVIDER=aws" in finding.message
    assert {finding.code for finding in missing.findings} >= {"guide-variable-value"}


def test_unknown_or_fixed_variables_are_refused(llmrepo):
    with pytest.raises(UsageError, match="has no variable NOPE"):
        followed(llmrepo, NOPE="x")
    with pytest.raises(UsageError, match="set by llmd"):
        followed(llmrepo, NAMESPACE="other")


def test_a_step_llmd_cannot_follow_blocks_unless_ci_skips_it(llmrepo):
    path = llmrepo / GUIDE / "guide.yaml"
    text = path.read_text()
    piped = (
        "  patch:\n    - run: |\n        kubectl kustomize ${REPO_ROOT}/guides/${GUIDE_NAME}/router "
        '| sed "s/a/b/" | kubectl apply -n ${NAMESPACE} -f -\n'
    )
    path.write_text(text.replace("verify:\n", piped + "\nverify:\n"))
    [finding] = [finding for finding in followed(llmrepo).findings if finding.severity == "error"]
    assert finding.code == "guide-step-not-followed" and "deploy.patch" in finding.message
    path.write_text(
        text.replace("verify:\n", piped.replace("    - run:", "    - skip_in: [ci]\n      run:") + "\nverify:\n")
    )
    assert [finding.code for finding in followed(llmrepo).findings] == ["guide-step-left-out"]


def test_expansion_follows_the_shell():
    env = Environment()
    env.set("A", "1")
    assert env.expand("x${A}y$A ${B:-two} '${C}'") == "x1y1 two '${C}'"
    env.assign("D", "$(date)")
    assert env.get("D") is None and "command's output" in env.unknown["D"]


@needs_tools
def test_a_guide_renders_what_its_readme_would_apply(llmrepo):
    stack = find_stack(resolve_source(str(llmrepo)), GUIDE)
    rendered = render(stack, runner=subprocess_runner, namespace="team-a")
    kinds = sorted(f"{doc['kind']}/{doc['metadata']['name']}" for doc in rendered.documents)
    assert kinds == [
        "ClusterRole/demo-baseline-team-a-epp",
        "ClusterRoleBinding/demo-baseline-team-a-epp",
        "Deployment/demo-baseline-decode",
        "Deployment/demo-baseline-epp",
        "InferencePool/demo-baseline",
        "Role/demo-baseline-epp",
        "RoleBinding/demo-baseline-epp",
        "Service/demo-baseline-epp",
        "ServiceAccount/demo-baseline-epp",
        "ServiceAccount/demo-baseline-sa",
    ]
    assert rendered.command.startswith("helm template demo-baseline charts/router --version 0.1.0")


@needs_tools
def test_stacks_and_render_commands_for_a_guide(llmrepo, tmp_path, capsys):
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "stacks", "demo-baseline", "--source", str(llmrepo)]) == 0
    out = capsys.readouterr().out
    assert "INFRA_PROVIDER" in out and "one of: base, ocp" in out and "HF_TOKEN               (sensitive)" in out
    assert "kustomize build guides/demo-baseline/modelserver/gpu/vllm/base" in out
    argv = [*state, "render", "demo-baseline", "--source", str(llmrepo), "-n", "team-a", "--var", "INFRA_PROVIDER=ocp"]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert "hostPath" in captured.out and "10 object(s)" in captured.err
    assert main([*state, "render", "demo-baseline", "--source", str(llmrepo), "--var", "INFRA_PROVIDER=aws"]) == 2
    overlay = "guides/demo-baseline/modelserver/gpu/vllm/base"
    assert main([*state, "render", overlay, "--source", str(llmrepo), "--var", "A=b"]) == 2
