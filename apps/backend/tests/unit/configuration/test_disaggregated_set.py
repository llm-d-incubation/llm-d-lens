from copy import deepcopy

import pytest

from llm_d_lens.configuration.disaggregated_set import (
    apply_disaggregated_set,
    normalize_disaggregated_set,
    resource_requests,
    validate_disaggregated_sets,
    workload_summary,
)


def workload(slices=2):
    return {
        "kind": "DisaggregatedSet",
        "spec": {
            "slices": slices,
            "roles": [
                {"name": "decode", "spec": {"replicas": 1}},
                {"name": "prefill", "spec": {"replicas": 2}},
            ],
        },
    }


def test_validation_preserves_multiple_slices_and_role_order():
    documents = [workload()]
    original = deepcopy(documents)
    assert validate_disaggregated_sets(documents)["slices"] == 2
    assert documents == original


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "2", None])
def test_invalid_slice_counts_are_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_disaggregated_sets([workload(value)])


def test_external_scaling_conflicts_only_with_multiple_slices():
    document = workload()
    document["spec"]["roles"][0]["scaling"] = {"mode": "External"}
    with pytest.raises(ValueError, match="External"):
        validate_disaggregated_sets([document])
    document["spec"]["slices"] = 1
    validate_disaggregated_sets([document])


def test_patch_conflicts_are_not_silently_overwritten():
    with pytest.raises(ValueError, match="disagrees"):
        validate_disaggregated_sets([workload(3)], {"slices": 2})


def test_intent_applies_without_mutating_the_input_settings():
    documents = [workload(1)]
    settings = {"slices": 2, "placementPolicy": {"type": "ExclusiveSlice", "topology": "example.com/fabric"}}
    apply_disaggregated_set(documents, settings)
    assert documents[0]["spec"]["slices"] == 2
    documents[0]["spec"]["placementPolicy"]["topology"] = "other"
    assert settings["placementPolicy"]["topology"] == "example.com/fabric"


def test_strict_routing_and_missing_topology_are_rejected():
    with pytest.raises(ValueError, match="sliceRouting"):
        normalize_disaggregated_set({"sliceRouting": "require"})
    with pytest.raises(ValueError, match="topology is required"):
        normalize_disaggregated_set({"placementPolicy": {"type": "ExclusiveSlice"}})


def test_leader_worker_resource_counts_include_slices_and_groups():
    document = workload(2)
    for role in document["spec"]["roles"]:
        role["spec"]["leaderWorkerTemplate"] = {
            "size": 3,
            "leaderTemplate": {"spec": {"containers": [{"resources": {"requests": {"example.com/device": 2}}}]}},
            "workerTemplate": {"spec": {"containers": [{"resources": {"limits": {"example.com/device": 4}}}]}},
        }
    summary = workload_summary([document])
    assert summary["podCount"] == 18
    assert summary["resourceRequests"]["example.com/device"] == 60


def test_native_sidecars_and_init_peak_are_counted():
    pod = {
        "containers": [{"resources": {"requests": {"cpu": "500m"}}}],
        "initContainers": [
            {"restartPolicy": "Always", "resources": {"requests": {"cpu": "250m"}}},
            {"resources": {"requests": {"cpu": "2"}}},
        ],
        "overhead": {"cpu": "100m"},
    }
    assert resource_requests(pod)["cpu"] == pytest.approx(2.35)


@pytest.mark.parametrize("mode", ["prefer", "filter"])
def test_slice_routing_registers_prefill_affinity_without_changing_original(mode):
    import yaml

    from llm_d_lens.configuration.router_compatibility import configure_slice_routing

    config = {
        "plugins": [{"type": "disagg-profile-handler"}],
        "schedulingProfiles": [{"name": "prefill", "plugins": [{"pluginRef": "existing"}]}],
    }
    values = {
        "router": {"epp": {"pluginsConfigFile": "pd.yaml", "pluginsCustomConfig": {"pd.yaml": yaml.safe_dump(config)}}}
    }
    assert normalize_disaggregated_set({"sliceRouting": mode}) == {"sliceRouting": mode}
    assert configure_slice_routing(values, "inherit", "v0.11.0") == values
    result = configure_slice_routing(values, mode, "v1.0.0")
    actual = yaml.safe_load(result["router"]["epp"]["pluginsCustomConfig"]["pd.yaml"])
    assert actual["plugins"][-2]["parameters"]["hostname"] == "disaggregatedset.x-k8s.io/slice"
    expected_parameters = {"topologyProducerName": "lens-slice-topology"}
    expected_reference = {"pluginRef": "lens-slice-affinity"}
    if mode == "filter":
        expected_parameters["minAffinity"] = "host"
    else:
        expected_reference["weight"] = 1
    assert actual["plugins"][-1] == {
        "type": "topology-affinity-filter" if mode == "filter" else "topology-affinity-scorer",
        "name": "lens-slice-affinity",
        "parameters": expected_parameters,
    }
    assert actual["schedulingProfiles"][0]["plugins"] == [{"pluginRef": "existing"}, expected_reference]
    assert result["router"]["epp"]["flags"] == {"allow-experimental-plugins": True}
    assert "flags" not in values["router"]["epp"]
    assert yaml.safe_load(values["router"]["epp"]["pluginsCustomConfig"]["pd.yaml"]) == config
    assert configure_slice_routing(result, mode, "v1.0.0") == result
    with pytest.raises(ValueError, match="validated Router"):
        configure_slice_routing(values, mode, "v0.11.0")
