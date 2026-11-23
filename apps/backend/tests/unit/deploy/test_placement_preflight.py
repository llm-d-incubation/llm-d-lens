from llm_d_lens.deploy.placement_preflight import max_feasible_slices, placement_preflight, slice_capacity


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


def test_two_slices_fit_two_domains_without_claiming_full_scheduler_validation():
    report = placement_preflight(
        [document()], [node("a1", "a", 4), node("a2", "a", 2), node("b1", "b", 4), node("b2", "b", 2)], []
    )
    assert report["modeledConstraints"] == "pass"
    assert report["status"] == "pass"
    assert "Physical topology source is unverified" in report["unmodeled"]
    assert any("quota" in reason for reason in report["unmodeled"])
    assert len(report["assignments"]) == 6
    domains = {(item["slice"], item["domain"]) for item in report["assignments"]}
    assert len(domains) == 2 and {domain for _, domain in domains} == {"a", "b"}


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
    assert max_feasible_slices(capacity, {"type": "ExclusiveSlice", "topology": "example.com/fabric"}) == 2
    assert max_feasible_slices(capacity, {"type": "ExclusiveTopology", "topology": "example.com/fabric"}) == 1
    assert max_feasible_slices(capacity, {"type": "ExclusiveSlice", "topology": "missing"}) is None
    assert max_feasible_slices(capacity, None) == 2
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


def test_fragmented_domain_and_one_domain_are_rejected():
    for nodes in ([node("a", "a", 12)], [node("a", "a", 6), node("b1", "b", 3), node("b2", "b", 3)]):
        assert placement_preflight([document()], nodes, [])["status"] == "fail"


def test_incomplete_inventory_and_search_timeout_are_unknown():
    assert placement_preflight([document()], [], [], complete=False)["status"] == "unknown"
    report = placement_preflight([document()], [node("a", "a", 6), node("b", "b", 6)], [], search_limit=0)
    assert report["status"] == "unknown"
    assert not report["errors"]


def test_terminating_pods_still_consume_resources():
    pods = [
        {
            "metadata": {"deletionTimestamp": "now"},
            "spec": {"nodeName": "a", "containers": [{"resources": {"requests": {"example.com/device": 4}}}]},
            "status": {"phase": "Running"},
        }
    ]
    assert placement_preflight([document(1)], [node("a", "a", 6)], pods)["status"] == "fail"


def test_exclusive_topology_is_symmetric_across_different_policy_types():
    exclusive, shared = document(1), document(1)
    exclusive["spec"]["placementPolicy"]["type"] = "ExclusiveTopology"
    shared["metadata"]["name"] = "other"
    for documents in ([exclusive, shared], [shared, exclusive]):
        assert placement_preflight(documents, [node("a", "a", 20)], [])["status"] == "fail"


def test_runtime_class_constraints_and_overhead_are_included():
    workload = document(1)
    for role in workload["spec"]["roles"]:
        role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["runtimeClassName"] = "sandbox"
    runtime = {
        "metadata": {"name": "sandbox"},
        "scheduling": {"nodeSelector": {"example.com/fabric": "a"}},
        "overhead": {"podFixed": {"example.com/device": "1"}},
    }
    report = placement_preflight([workload], [node("a", "a", 8)], [], runtime_classes=[runtime])
    assert report["status"] == "fail"
    assert report["summary"]["resourceRequests"]["example.com/device"] == 9
    assert placement_preflight([workload], [node("b", "b", 20)], [], runtime_classes=[runtime])["status"] == "fail"
    report = placement_preflight([workload], [node("a", "a", 20)], [], runtime_classes=[runtime])
    assert report["modeledConstraints"] == "pass"
    assert "RuntimeClass scheduling and overhead" not in report["unmodeled"]
    report = placement_preflight([workload], [node("a", "a", 20)], [], runtime_classes=[])
    assert any(item["code"] == "RUNTIMECLASS_UNAVAILABLE" for item in report["errors"])
