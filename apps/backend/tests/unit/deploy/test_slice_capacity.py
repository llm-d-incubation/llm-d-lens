from llm_d_lens.deploy.slice_capacity import slice_capacity


def node(name, domain, devices):
    return {
        "metadata": {"name": name, "labels": {"example.com/fabric": domain}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"example.com/device": devices, "pods": 100},
        },
    }


def document(slices=2):
    return {
        "kind": "DisaggregatedSet",
        "metadata": {"name": "pd"},
        "spec": {
            "slices": slices,
            "placementPolicy": {"type": "ExclusiveSlice", "topology": "example.com/fabric"},
            "roles": [
                {
                    "name": role,
                    "spec": {
                        "replicas": replicas,
                        "leaderWorkerTemplate": {
                            "size": 1,
                            "workerTemplate": {
                                "spec": {"containers": [{"resources": {"requests": {"example.com/device": devices}}}]}
                            },
                        },
                    },
                }
                for role, replicas, devices in [("prefill", 2, 1), ("decode", 1, 4)]
            ],
        },
    }


def test_slice_capacity_reports_per_policy_ceilings_and_occupied_domains():
    nodes = [node("a1", "a", 6), node("b1", "b", 6), node("c1", "c", 3)]
    other = {
        "metadata": {"labels": {"disaggregatedset.x-k8s.io/name": "other"}},
        "spec": {"nodeName": "b1", "containers": []},
        "status": {"phase": "Running"},
    }
    capacity = slice_capacity([document(1)], nodes, [other])
    assert capacity["sliceRequests"] == {"example.com/device": 6}
    assert capacity["podsPerSlice"] == 3
    assert capacity["maxSlices"] == {"None": 2}
    fabric = capacity["topologyKeys"][0]
    assert fabric["key"] == "example.com/fabric" and fabric["wellKnown"] is False
    assert (fabric["feasibleDomains"], fabric["exclusiveTopologyDomains"]) == (2, 1)
    assert capacity["unmodeled"] == []


def test_slice_capacity_flags_dra_claims_as_unmodeled():
    claimed = document(1)
    for role in claimed["spec"]["roles"]:
        role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"] = {
            "containers": [{"resources": {"requests": {"cpu": "1"}}}],
            "resourceClaims": [{"name": "xpu", "resourceClaimTemplateName": "xpu-claim"}],
        }
    capacity = slice_capacity([claimed], [node("a1", "a", 6)], [])
    assert capacity["acceleratorResources"] == []
    assert capacity["unmodeled"] == ["DRA"]


def test_unschedulable_and_finished_pods_do_not_change_free_capacity():
    nodes = [node("a1", "a", 6), {**node("b1", "b", 6), "spec": {"unschedulable": True}}]
    finished = {
        "spec": {"nodeName": "a1", "containers": [{"resources": {"requests": {"example.com/device": 6}}}]},
        "status": {"phase": "Succeeded"},
    }
    capacity = slice_capacity([document(1)], nodes, [finished])
    assert capacity["schedulableNodes"] == 1
    assert capacity["maxSlices"] == {"None": 1}
