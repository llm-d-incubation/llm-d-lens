"""Following an llm-d guide's ``guide.yaml`` steps without a shell."""

import pytest

from lens.errors import UsageError
from lens.guides import Environment, follow, load_guide

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
