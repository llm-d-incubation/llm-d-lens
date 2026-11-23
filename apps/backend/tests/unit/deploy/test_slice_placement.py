import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from llm_d_lens.deploy import slice_placement
from llm_d_lens.deploy import source_composition as composition
from llm_d_lens.deploy.guide_planning import GuideError
from llm_d_lens.deploy.providers.configuration_manifest import prepare_single_slice_disaggregated_set_manifest
from llm_d_lens.deploy.providers.target_node import apply_placement, pin_manifest_to_node


def node(name, free, *, rack=None, occupied=False):
    labels = {"kubernetes.io/hostname": name}
    if rack:
        labels["example.com/rack"] = rack
    return {"name": name, "labels": labels, "free": free, "occupied": occupied}


def disaggregated_set(prefill=2, decode=2, *, tp=1, annotations=None):
    def role(name, replicas):
        return {
            "name": name,
            "metadata": {"annotations": annotations or {}},
            "spec": {
                "replicas": replicas,
                "leaderWorkerTemplate": {
                    "size": 1,
                    "workerTemplate": {
                        "metadata": {"labels": {"llm-d.ai/role": name}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "modelserver",
                                    "image": "engine:pinned",
                                    "args": ["--tensor-parallel-size", str(tp)],
                                    "resources": {"limits": {"nvidia.com/gpu": tp}},
                                }
                            ]
                        },
                    },
                },
            },
        }

    return {
        "apiVersion": "disaggregatedset.x-k8s.io/v1",
        "kind": "DisaggregatedSet",
        "metadata": {"name": "pd", "namespace": "serving"},
        "spec": {"slices": 1, "roles": [role("prefill", prefill), role("decode", decode)]},
    }


def test_recommendation_splits_slices_to_keep_each_slice_on_one_node():
    nodes = [node("a", 4), node("b", 4)]
    layout = slice_placement.recommend_layout(nodes, prefill_replicas=2, decode_replicas=2, slice_devices=8)
    assert layout["slices"] == 2
    assert (layout["prefillReplicas"], layout["replicas"]) == (1, 1)
    assert layout["placementPolicy"] == {"type": "ExclusiveSlice", "topology": "kubernetes.io/hostname"}


def test_recommendation_prefers_narrowest_domain_and_reserves_for_latency():
    nodes = [node("a", 4, rack="r1"), node("b", 4, rack="r1"), node("c", 4, rack="r2"), node("d", 4, rack="r2")]
    layout = slice_placement.recommend_layout(
        nodes, prefill_replicas=1, decode_replicas=1, slice_devices=8, objective="latency"
    )
    assert layout["slices"] == 1
    assert layout["placementPolicy"] == {"type": "ExclusiveTopology", "topology": "example.com/rack"}
    occupied = [{**item, "occupied": True} for item in nodes]
    shared = slice_placement.recommend_layout(
        occupied, prefill_replicas=1, decode_replicas=1, slice_devices=8, objective="latency"
    )
    assert shared["placementPolicy"]["type"] == "ExclusiveSlice"


def test_recommendation_leaves_placement_to_scheduler_when_no_domain_fits():
    layout = slice_placement.recommend_layout(
        [node("a", 2), node("b", 2)], prefill_replicas=1, decode_replicas=1, slice_devices=4
    )
    assert layout == {**layout, "slices": 1, "placementPolicy": {"type": "None"}}
    explicit = slice_placement.recommend_layout(
        [node("a", 4), node("b", 4), node("c", 4)], prefill_replicas=1, decode_replicas=1, slice_devices=2, slices=3
    )
    assert (explicit["slices"], explicit["prefillReplicas"]) == (3, 1)


def test_automatic_slice_limits_first_pod_to_domains_with_capacity():
    nodes = [node("small", 2), node("large", 8)]
    layout = slice_placement.recommend_layout(nodes, prefill_replicas=2, decode_replicas=2, slice_devices=8)
    assert layout["eligibleDomains"] == ["large"]
    parsed = [disaggregated_set(tp=2)]
    template = parsed[0]["spec"]["roles"][0]["spec"]["leaderWorkerTemplate"]["workerTemplate"]
    template["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "pool", "operator": "In", "values": ["serving"]}]},
                    {"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["large"]}]},
                ],
            }
        }
    }
    slice_placement.apply_layout(parsed, layout)
    expected = {"key": slice_placement.HOSTNAME, "operator": "In", "values": ["large"]}
    for role in parsed[0]["spec"]["roles"]:
        spec = role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
        terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        assert all(expected in term["matchExpressions"] for term in terms)
    terms = template["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ]
    assert terms[0]["matchExpressions"][0]["key"] == "pool"
    assert terms[1]["matchFields"][0]["values"] == ["large"]


def test_apply_layout_writes_policy_and_rejects_lws_exclusive_annotation():
    parsed = [disaggregated_set()]
    layout = {"slices": 2, "placementPolicy": {"type": "ExclusiveSlice", "topology": "kubernetes.io/hostname"}}
    slice_placement.apply_layout(parsed, layout, split=2)
    spec = parsed[0]["spec"]
    assert spec["slices"] == 2
    assert spec["placementPolicy"] == layout["placementPolicy"]
    assert [role["spec"]["replicas"] for role in spec["roles"]] == [1, 1]
    annotated = [disaggregated_set(annotations={slice_placement.EXCLUSIVE_TOPOLOGY_ANNOTATION: "x"})]
    with pytest.raises(GuideError, match="exclusive-topology"):
        slice_placement.apply_layout(annotated, layout)
    with pytest.raises(GuideError, match="topology key"):
        slice_placement.validate_policy({"type": "ExclusiveSlice"})
    with pytest.raises(GuideError, match="from 1 to"):
        slice_placement.validate_slices(0)


def test_resolve_layout_uses_explicit_values_and_recommends_the_rest(monkeypatch):
    async def session(request, session_id):
        return SimpleNamespace(server_id="cluster")

    async def inventory(session_id):
        return [node("a", 2), node("b", 2)]

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(slice_placement, "node_inventory", inventory)
    body = {"clusterId": "cluster", "clusterSessionId": "session", "runtimeConfiguration": {"tp": 1}}
    parsed = [disaggregated_set()]
    layout = asyncio.run(composition.resolve_slice_layout(None, body, parsed))
    assert (layout["slices"], layout["replicas"], layout["explicit"]["slices"]) == (2, 1, False)
    assert parsed[0]["spec"]["placementPolicy"]["topology"] == "kubernetes.io/hostname"

    body["runtimeConfiguration"] = {"tp": 1, "slices": 1, "placementPolicy": {"type": "None"}}
    parsed = [disaggregated_set()]
    layout = asyncio.run(composition.resolve_slice_layout(None, body, parsed))
    assert layout["slices"] == 1 and "placementPolicy" not in parsed[0]["spec"]
    assert [role["spec"]["replicas"] for role in parsed[0]["spec"]["roles"]] == [2, 2]

    body["runtimeConfiguration"] = {"tp": 1, "placementPolicy": {"type": "ExclusiveSlice", "topology": "missing/key"}}
    with pytest.raises(GuideError, match="No ready node"):
        asyncio.run(composition.resolve_slice_layout(None, body, [disaggregated_set()]))


def test_pod_placement_applies_to_disaggregated_roles_and_rejects_slice_conflicts():
    document = disaggregated_set()
    rendered, changed = apply_placement(yaml.safe_dump(document), {"replicas": "spread"})
    assert changed == 2
    role = yaml.safe_load(rendered)["spec"]["roles"][0]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
    assert role["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    document["spec"]["placementPolicy"] = {"type": "ExclusiveSlice", "topology": "kubernetes.io/hostname"}
    with pytest.raises(ValueError, match="conflicts with slice placement"):
        apply_placement(yaml.safe_dump(document), {"replicas": "spread"})
    document["spec"]["slices"] = 2
    with pytest.raises(ValueError, match="different topology domain"):
        apply_placement(yaml.safe_dump(document), {"replicas": "same-node"})
    document["spec"]["placementPolicy"] = {"type": "ExclusiveSlice", "topology": "topology.kubernetes.io/zone"}
    with pytest.raises(ValueError, match="different topology domain"):
        apply_placement(yaml.safe_dump(document), {"replicas": "same-node"})
    apply_placement(yaml.safe_dump(document), {"replicas": "spread"})


def test_target_node_pins_all_role_templates_and_rejects_conflicts():
    document = disaggregated_set()
    for role in document["spec"]["roles"]:
        group = role["spec"]["leaderWorkerTemplate"]
        group["leaderTemplate"] = deepcopy(group["workerTemplate"])
    rendered, count = pin_manifest_to_node(yaml.safe_dump(document), "chosen")
    assert count == 4
    pinned = yaml.safe_load(rendered)
    for role in pinned["spec"]["roles"]:
        for template in ("leaderTemplate", "workerTemplate"):
            assert (
                role["spec"]["leaderWorkerTemplate"][template]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
                == "chosen"
            )
    with pytest.raises(ValueError, match="existing node selection"):
        pin_manifest_to_node(rendered, "another")
    document["spec"]["slices"] = 2
    document["spec"]["placementPolicy"] = {"type": "ExclusiveSlice", "topology": "topology.kubernetes.io/zone"}
    with pytest.raises(ValueError, match="conflicts with slice placement"):
        pin_manifest_to_node(yaml.safe_dump(document), "chosen")


def test_manifest_keeps_its_declared_slice_count():
    document = disaggregated_set()
    document["spec"]["slices"] = 3
    manifest = yaml.safe_dump(document)
    rendered, report = prepare_single_slice_disaggregated_set_manifest(manifest)
    assert report == {"slices": 3, "disaggregatedSets": 1, "adjusted": 0}
    assert rendered == manifest


def test_recommendation_and_planning_respect_pod_placement(monkeypatch):
    nodes = [node("a", 4, rack="r1"), node("b", 4, rack="r1")]
    spread = slice_placement.recommend_layout(
        nodes, prefill_replicas=1, decode_replicas=1, slice_devices=2, pod_placement="spread"
    )
    assert spread["placementPolicy"]["topology"] == "example.com/rack"
    pinned = slice_placement.recommend_layout(
        [node("a", 2), node("b", 2)], prefill_replicas=2, decode_replicas=2, slice_devices=4, pod_placement="same-node"
    )
    assert pinned["slices"] == 1 and pinned["placementPolicy"] == {"type": "None"}

    async def session(request, session_id):
        return SimpleNamespace(server_id="cluster")

    async def inventory(session_id):
        return nodes

    monkeypatch.setattr(composition, "validate_planning_session", session)
    monkeypatch.setattr(slice_placement, "node_inventory", inventory)
    body = {
        "clusterId": "cluster",
        "clusterSessionId": "session",
        "runtimeConfiguration": {
            "podPlacement": "balanced",
            "placementPolicy": {"type": "ExclusiveSlice", "topology": "kubernetes.io/hostname"},
        },
    }
    with pytest.raises(GuideError, match="separates model-server Pods"):
        asyncio.run(composition.resolve_slice_layout(None, body, [disaggregated_set(1, 1)]))
