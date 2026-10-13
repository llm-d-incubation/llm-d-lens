"""Deployment and telemetry consumers follow plugin data, including new hardware."""

from pathlib import Path

import pytest

from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter
from llm_d_bench.deploy.providers.hardware_profile import guide_overlays, pin_runtime_image, set_accelerator_request
from llm_d_bench.hardware import registry
from llm_d_bench.hardware.models import HardwareProfile
from llm_d_bench.hardware.resolver import configuration_resource_request, resolve_configuration_profile
from llm_d_bench.hardware.telemetry import parse_device_metrics


@pytest.fixture
def plugin():
    registry.ensure_loaded()
    profile = HardwareProfile.from_dict(
        {
            "id": "contract-device",
            "vendor": "contract",
            "display_name": "Contract device",
            "accelerator_keys": ["contract"],
            "upstream_variant": "nested/device",
            "request_model": "extended-resource",
            "resource_prefixes": ["contract.example/cards"],
            "device_classes": ["contract.example"],
            "deployment": {
                "overlay_root": "guides/{guide}/modelserver/{variant}/{model_server}",
                "arch": "nested/device",
                "resource_name": "contract.example/cards",
                "runtime_image": "registry.example/engine:v2",
                "managed_image_repositories": ["registry.example/engine"],
            },
            "telemetry": {
                "provider_id": "contract",
                "label_schema": {"node": "host", "pci": "slot", "device": "tile"},
                "device_metric_sources": {
                    "utilization": {
                        "metric": "contract_busy",
                        "match": {"engine": "compute"},
                        "unit": "ratio",
                        "output_unit": "percent",
                        "scale": 100,
                    },
                    "framebuffer_used": {
                        "metric": "contract_memory",
                        "match": {"location": "device"},
                        "unit": "bytes",
                        "output_unit": "bytes",
                        "scale": 1024,
                        "tile_aggregation": "sum",
                    },
                },
            },
        }
    )
    registry.register_profile(profile)
    yield profile
    registry._reset_for_tests()


def test_new_profile_drives_image_resource_and_nested_guide_discovery(plugin, tmp_path: Path):
    overlay = tmp_path / "guides/new-guide/modelserver/nested/device/vllm/custom/deep"
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text("resources: []")
    (overlay.parent / "patch.yaml").write_text("kind: Deployment")
    assert guide_overlays(tmp_path, "new-guide", accelerator=plugin.id) == {"custom/deep": overlay}
    container = {"resources": {"limits": {"cpu": "2"}}}
    set_accelerator_request(container, None, 3, accelerator=plugin.id)
    assert container["resources"]["limits"] == {"cpu": "2", "contract.example/cards": "3"}
    assert pin_runtime_image("registry.example/engine:old", accelerator=plugin.id) == "registry.example/engine:v2"
    assert pin_runtime_image("custom.example/engine:v1", accelerator=plugin.id) == "custom.example/engine:v1"


def test_direct_metrics_apply_all_matchers_scale_and_node_identity(plugin):
    payload = "\n".join(
        [
            'contract_busy{host="a",slot="01",engine="compute"} 0.4',
            'contract_busy{host="b",slot="01",engine="compute"} 0.8',
            'contract_busy{host="a",slot="01",engine="copy"} 1',
            'contract_memory{host="a",slot="01",location="device"} 2',
            'contract_memory{host="a",slot="01",location="host"} 999',
        ]
    )
    result = parse_device_metrics(payload, plugin)
    assert result["device_count"] == 2
    assert result["devices"][0]["memory_used_bytes"] == 2048
    assert result["devices"][0]["utilization_ratio"] == 0.4
    assert parse_device_metrics(payload, None) is None


def test_baseline_uses_source_extended_resource_on_a_dra_default_profile():
    profile = registry.get_profile("intel-xpu")
    content = {
        "hardware_profile": profile.id,
        "officialGuide": {"renderedManifest": "resources:\n  limits:\n    gpu.intel.com/xe: 2\n"},
    }
    binding = configuration_resource_request(content, profile)
    parameters = BaselineVllmAdapter._parameters(
        {
            **content,
            "hardware_request": binding,
            "model": {"name": "Test/Model"},
            "decode": {"replicaCount": 1, "tensorParallelSize": 2},
            "runtime": {"image": "custom/model:v1"},
        }
    )
    documents = BaselineVllmAdapter._resources(parameters)
    assert not any(doc["kind"] == "ResourceClaimTemplate" for doc in documents)
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["gpu.intel.com/xe"] == "2"


def test_device_count_patch_does_not_change_network_claim(plugin):
    claim = {
        "spec": {
            "spec": {
                "devices": {
                    "requests": [
                        {"exactly": {"deviceClassName": "network.example", "count": 1}},
                        {"exactly": {"deviceClassName": "contract.example", "count": 1}},
                    ]
                }
            }
        }
    }
    set_accelerator_request({}, claim, 4, accelerator=plugin.id)
    assert [request["exactly"]["count"] for request in claim["spec"]["spec"]["devices"]["requests"]] == [1, 4]


def test_extended_resource_execution_resolves_without_image_guess(plugin):
    assert (
        resolve_configuration_profile(
            {"officialGuide": {"renderedManifest": "resources:\n  limits:\n    contract.example/cards: 2\n"}}
        )
        == plugin
    )


def test_access_mode_settings_override_legacy_top_level_values(plugin):
    from llm_d_bench.deploy.providers.hardware_profile import claim_request_name, device_class

    data = plugin.to_dict()
    data["deployment"]["modes"] = {
        "custom-dra": {
            "request_model": "dra",
            "device_class": "contract.example",
            "claim_request_name": "custom-request",
        }
    }
    data["request_model"] = "dra"
    registry.register_profile(HardwareProfile.from_dict(data), replace=True)
    assert device_class(accelerator=plugin.id, access_mode="custom-dra") == "contract.example"
    assert claim_request_name(accelerator=plugin.id, access_mode="custom-dra") == "custom-request"


def test_plugin_telemetry_uses_pod_resource_identity_and_rejects_shared_devices():
    from llm_d_bench.hardware.telemetry import workload_telemetry_profiles
    from llm_d_bench.monitoring.profiling.xpu_metrics import device_allocations

    profile = registry.get_profile("intel-xpu")
    pod = {
        "metadata": {"name": "model", "namespace": "bench"},
        "spec": {"nodeName": "node-a", "containers": [{"resources": {"limits": {"gpu.intel.com/xe": 1}}}]},
        "status": {
            "containerStatuses": [
                {
                    "allocatedResourcesStatus": [
                        {"name": "gpu.intel.com/xe", "resources": [{"resourceID": "0000:03:00.0"}]}
                    ]
                }
            ]
        },
    }
    resolved = workload_telemetry_profiles([profile], [pod], [])[0]
    assert resolved.telemetry.allocation_join == "pod-status"
    assert device_allocations([pod], [], [], "bench", resolved)[("node-a", "0000:03:00.0")]["pod"] == "model"
    import copy

    other = copy.deepcopy(pod)
    other["metadata"]["name"] = "shared"
    assert device_allocations([pod, other], [], [], "bench", resolved) == {}
    pod["status"] = {}
    assert device_allocations([pod], [], [], "bench", resolved) == {}


def test_source_default_and_nested_runtime_entries_are_discovered(plugin, tmp_path):
    from llm_d_bench.deploy.providers.hardware_profile import default_guide_variant
    from llm_d_bench.deploy.providers.pd_disaggregation import PdDisaggregationAdapter

    for path in ["vllm/custom/deep", "vllm-special/other/deep"]:
        directory = tmp_path / "guides/pd-disaggregation/modelserver/nested/device" / path
        directory.mkdir(parents=True)
        (directory / "kustomization.yaml").write_text("resources: []")
    sources = PdDisaggregationAdapter._guide_sources(tmp_path, plugin.id)
    assert set(sources) == {"custom/deep", "vllm-special/other/deep"}
    assert default_guide_variant(sources) == "custom/deep"
