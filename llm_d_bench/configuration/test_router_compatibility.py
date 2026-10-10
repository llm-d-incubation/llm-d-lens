"""Regression coverage for saved P/D configurations with Router 0.11."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from llm_d_bench.configuration.guide_settings import validate_guide_settings
from llm_d_bench.configuration.router_compatibility import compatible_router_values
from llm_d_bench.deploy.providers.deployment_bundle import install_deployment_bundle, text_checksum


def _values():
    return {
        "router": {
            "epp": {
                "pluginsConfigFile": "pd.yaml",
                "pluginsCustomConfig": {
                    "pd.yaml": yaml.safe_dump(
                        {
                            "plugins": [
                                {"type": "disagg-headers-handler"},
                                {"type": "always-disagg-pd-decider", "name": "force-prefill"},
                                {
                                    "type": "disagg-profile-handler",
                                    "parameters": {"deciderPluginName": "force-prefill"},
                                },
                                {"type": "prefill-filter"},
                            ],
                            "schedulingProfiles": [{"name": "prefill", "plugins": [{"pluginRef": "prefill-filter"}]}],
                        }
                    ),
                    "unused.yaml": "unchanged: true",
                },
            }
        }
    }


def test_compatibility_preserves_pd_routing_source_and_unrelated_configuration():
    values = _values()
    before = deepcopy(values)
    result = compatible_router_values(values, "v0.11.0")
    plugins = yaml.safe_load(result["router"]["epp"]["pluginsCustomConfig"]["pd.yaml"])
    assert [p["type"] for p in plugins["plugins"]] == [
        "always-disagg-pd-decider",
        "disagg-profile-handler",
        "prefill-filter",
    ]
    assert plugins["plugins"][1]["parameters"] == {"deciders": {"prefill": "force-prefill"}}
    assert plugins["schedulingProfiles"] == [{"name": "prefill", "plugins": [{"pluginRef": "prefill-filter"}]}]
    assert result["router"]["epp"]["pluginsCustomConfig"]["unused.yaml"] == "unchanged: true"
    assert values == before
    assert compatible_router_values(result, "v0.11.0") == result


@pytest.mark.parametrize(
    "version,image",
    [
        ("v0.10.0", {}),
        ("v0.12.0", {}),
        ("main", {}),
        ("v0.11.0", {"tag": "v0.10.0"}),
        ("v0.11.0", {"repository": "custom-epp", "tag": "v0.11.0"}),
    ],
)
def test_other_runtime_contracts_are_unchanged(version, image):
    values = _values()
    values["router"]["epp"]["image"] = image
    assert compatible_router_values(values, version) == values


def test_conflicting_deciders_are_rejected_without_mutating_input():
    values = _values()
    config = yaml.safe_load(values["router"]["epp"]["pluginsCustomConfig"]["pd.yaml"])
    config["plugins"][2]["parameters"]["deciders"] = {"prefill": "different"}
    values["router"]["epp"]["pluginsCustomConfig"]["pd.yaml"] = yaml.safe_dump(config)
    before = deepcopy(values)
    with pytest.raises(ValueError, match="Conflicting"):
        compatible_router_values(values, "v0.11.0")
    assert values == before


def _bundle():
    def asset(name, content):
        return {"name": name, "content": content, "checksum": text_checksum(content)}

    return {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "pd-disaggregation",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
            "version": "v0.11.0",
            "releaseName": "pd-disaggregation",
            "values": [
                asset("router-base.yaml", "router: {}"),
                asset("router-guide.yaml", yaml.safe_dump(_values())),
                asset("router-effective.yaml", yaml.safe_dump(_values())),
            ],
        },
        "resources": [],
    }


@pytest.mark.parametrize("normalized", [False, True])
def test_validation_accepts_old_and_new_bundles_but_rejects_changed_routing(normalized):
    bundle = _bundle()
    asset = bundle["helm"]["values"][-1]
    if normalized:
        asset["content"] = yaml.safe_dump(compatible_router_values(_values(), "v0.11.0"))
        asset["checksum"] = text_checksum(asset["content"])
    content = {"officialGuide": {"deploymentBundle": bundle}}
    validate_guide_settings(content, "", "pd-disaggregation")
    changed = yaml.safe_load(asset["content"])
    changed["router"]["epp"]["replicas"] = 7
    asset["content"] = yaml.safe_dump(changed)
    with pytest.raises(ValueError, match="do not match"):
        validate_guide_settings(content, "", "pd-disaggregation")


@pytest.mark.asyncio
@pytest.mark.parametrize("derived", [False, True])
async def test_retry_installs_compatible_values_and_retains_immutable_snapshot(tmp_path, derived):
    bundle = _bundle()
    before = deepcopy(bundle)
    captured = []
    values = _values()
    values["router"]["proxy"] = {"enabled": False}

    async def runner(command):
        if "--values" in command:
            captured.append(Path(command[command.index("--values") + 1]).read_text())
        return 0, "kind: Deployment\n", ""

    result = await install_deployment_bundle(
        bundle,
        guide=bundle["guide"],
        source_commit=bundle["sourceCommit"],
        namespace="test-pd",
        command_runner=runner,
        output_root=tmp_path,
        effective_values_content=yaml.safe_dump(values) if derived else None,
    )
    assert captured[0] == captured[1] == result["router_effective_values"]
    assert "disagg-headers-handler" not in captured[0]
    assert "deciderPluginName" not in captured[0]
    installed = yaml.safe_load(captured[0])
    assert installed == compatible_router_values(values if derived else _values(), "v0.11.0")
    assert bundle == before
    assert (Path(result["router_values_path"]).parent / "router-effective.yaml").read_text() == before["helm"][
        "values"
    ][-1]["content"]
