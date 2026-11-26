import json
from copy import deepcopy

import pytest

from llm_d_lens.configuration.guide_composition import compose_native_cpu_cache


def manifest(connector=None, image="runtime:1"):
    args = ["model"]
    if connector:
        args += ["--kv-transfer-config", json.dumps(connector)]
    return [
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "modelserver",
                                "image": image,
                                "args": args,
                                "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "2Gi"}},
                            }
                        ]
                    }
                }
            },
        }
    ]


def donor():
    return manifest(
        {
            "kv_connector": "OffloadingConnector",
            "kv_role": "kv_both",
            "kv_connector_extra_config": {"cpu_bytes_to_use": 1024**3},
        }
    )


def test_composition_preserves_source_and_adds_host_budget():
    base = manifest()
    original = deepcopy(base)
    result = compose_native_cpu_cache(base, donor(), guide="optimized-baseline", capacity_gib=4)
    assert base == original
    server = result[0]["spec"]["template"]["spec"]["containers"][0]
    assert json.loads(server["args"][-1])["kv_connector_extra_config"]["cpu_bytes_to_use"] == 4 * 1024**3
    assert server["resources"]["requests"]["memory"] == str(5 * 1024**3)
    assert server["resources"]["limits"]["memory"] == str(6 * 1024**3)


@pytest.mark.parametrize(
    "guide,base,message",
    [
        ("pd-disaggregation", manifest(), "transport"),
        ("optimized-baseline", manifest({"kv_connector": "NixlConnector"}), "already owns"),
        ("precise-prefix-cache-routing", manifest(image="runtime:2"), "identical"),
    ],
)
def test_composition_rejects_unproven_or_conflicting_combinations(guide, base, message):
    with pytest.raises(ValueError, match=message):
        compose_native_cpu_cache(base, donor(), guide=guide, capacity_gib=4)


@pytest.mark.parametrize("capacity", [0, -1, True, float("nan"), float("inf"), 1025])
def test_invalid_budget_is_rejected(capacity):
    with pytest.raises(ValueError, match="capacity"):
        compose_native_cpu_cache(manifest(), donor(), guide="optimized-baseline", capacity_gib=capacity)


def test_composition_respects_an_explicit_prefix_cache_exclusion():
    base = manifest()
    base[0]["spec"]["template"]["spec"]["containers"][0]["args"].append("--no-enable-prefix-caching")
    with pytest.raises(ValueError, match="explicitly disabled"):
        compose_native_cpu_cache(base, donor(), guide="optimized-baseline", capacity_gib=4)


def pd_manifest():
    result = []
    for role in ("prefill", "decode"):
        docs = manifest(
            {
                "kv_connector": "NixlConnector",
                "kv_role": "kv_both",
                "engine_id": role,
                "kv_buffer_device": "cuda",
                "kv_connector_extra_config": {"num_threads": 2},
            }
        )
        docs[0]["metadata"]["name"] = role
        result.extend(docs)
    return result


def test_pd_preserves_transport_identity_environment_and_reserves_each_role():
    import yaml

    from llm_d_lens.configuration.guide_composition import runtime_probe
    from llm_d_lens.configuration.guide_settings import validate_configuration_extensions

    base = pd_manifest()
    original = deepcopy(base)
    result = compose_native_cpu_cache(base, donor(), guide="pd-disaggregation", capacity_gib=4)
    assert base == original
    for doc in result:
        pod = doc["spec"]["template"]["spec"]
        server = pod["containers"][0]
        config = json.loads(server["args"][-1])
        assert config["kv_connector"] == "MultiConnector"
        assert config["engine_id"] == doc["metadata"]["name"]
        transport, cache = config["kv_connector_extra_config"]["connectors"]
        assert transport == {
            "kv_connector": "NixlConnector",
            "kv_role": "kv_both",
            "kv_buffer_device": "cuda",
            "kv_connector_extra_config": {"num_threads": 2},
        }
        assert cache["kv_connector_extra_config"]["cpu_bytes_to_use"] == 4 * 1024**3
        assert server["resources"]["requests"]["memory"] == str(5 * 1024**3)
        assert pod["initContainers"] == [runtime_probe(server, config)]
    content = {
        "officialGuide": {"source": {"guide": "pd-disaggregation"}},
        "guideComposition": {"nativeCpuCacheGiB": 4},
    }
    validate_configuration_extensions(content, yaml.safe_dump_all(result))
    result[0]["spec"]["template"]["spec"]["initContainers"] = []
    with pytest.raises(ValueError, match="probe"):
        validate_configuration_extensions(content, yaml.safe_dump_all(result))


@pytest.mark.parametrize("connector", ["LMCacheConnectorV1", "MultiConnector", "UnknownConnector"])
def test_pd_unknown_transport_requires_an_adapter(connector):
    base = pd_manifest()
    base[0]["spec"]["template"]["spec"]["containers"][0]["args"][-1] = json.dumps(
        {"kv_connector": connector, "kv_role": "kv_both"}
    )
    with pytest.raises(ValueError, match="not yet supported"):
        compose_native_cpu_cache(base, donor(), guide="pd-disaggregation", capacity_gib=4)


def test_pd_preserves_split_transport_roles_without_mutating_source():
    base = pd_manifest()
    for doc, role in zip(base, ("kv_producer", "kv_consumer"), strict=True):
        server = doc["spec"]["template"]["spec"]["containers"][0]
        connector = json.loads(server["args"][-1])
        connector["kv_role"] = role
        server["args"][-1] = json.dumps(connector)
    original = deepcopy(base)
    result = compose_native_cpu_cache(base, donor(), guide="pd-disaggregation", capacity_gib=4)
    assert base == original
    for doc, role in zip(result, ("kv_producer", "kv_consumer"), strict=True):
        server = doc["spec"]["template"]["spec"]["containers"][0]
        config = json.loads(server["args"][-1])
        transport, cache = config["kv_connector_extra_config"]["connectors"]
        assert config["kv_role"] == role
        assert transport["kv_role"] == role
        assert cache["kv_role"] == "kv_both"


def test_pd_connector_in_command_is_replaced_once():
    from llm_d_lens.configuration.manifest_facts import _option

    base = pd_manifest()
    for doc in base:
        server = doc["spec"]["template"]["spec"]["containers"][0]
        server["command"] = ["vllm", "serve", *server.pop("args")]
    result = compose_native_cpu_cache(base, donor(), guide="pd-disaggregation", capacity_gib=2)
    for doc in result:
        server = doc["spec"]["template"]["spec"]["containers"][0]
        assert server["command"] == ["vllm", "serve"]
        assert server["args"].count("--kv-transfer-config") == 1
        assert json.loads(_option(server["args"], {"--kv-transfer-config"}, "test"))["kv_connector"] == "MultiConnector"


def test_pd_grouped_workloads_include_explicit_model_leaders():
    from llm_d_lens.configuration.guide_composition import composition_servers

    base = pd_manifest()
    for doc in base:
        pod = doc["spec"].pop("template")
        doc["kind"] = "LeaderWorkerSet"
        doc["spec"]["leaderWorkerTemplate"] = {"leaderTemplate": deepcopy(pod), "workerTemplate": pod}
    result = compose_native_cpu_cache(base, donor(), guide="pd-disaggregation", capacity_gib=4)
    assert len(composition_servers(result)) == 4
    for workload, server in composition_servers(result):
        assert json.loads(server["args"][-1])["kv_connector"] == "MultiConnector"
        assert workload["pod"]["initContainers"][0]["resources"] == server["resources"]
