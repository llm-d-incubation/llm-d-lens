import copy
import json

import pytest

from lens.catalog import find_stack
from lens.cli import main
from lens.plan import Options, discover_endpoints, make_plan, preferred_inference_endpoint, revalidate_endpoint
from lens.process import subprocess_runner
from lens.sources import resolve_source

STACK = "examples/model-a/cluster-a"


def test_auto_discovery_matches_serving_ports_without_declarations():
    documents = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "model"},
            "spec": {
                "selector": {"app": "model"},
                "ports": [{"name": "metrics", "port": 9090}, {"name": "http", "port": 80, "targetPort": "serve"}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "model"}},
                    "spec": {
                        "containers": [{"name": "modelserver", "ports": [{"name": "serve", "containerPort": 8000}]}]
                    },
                }
            },
        },
    ]
    endpoints, findings = discover_endpoints(documents, "team-a")
    assert not findings
    assert endpoints[0]["purpose"] == "metrics"
    assert endpoints[1]["preferred"]
    assert endpoints[1]["url"] == "http://model.team-a.svc.cluster.local:80"
    assert endpoints[1]["backends"][0]["targetPort"] == 8000


def test_a_plan_digest_follows_what_would_be_applied(llmrepo):
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    first = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=subprocess_runner)
    again = make_plan(stack, Options(namespace="team-a", instance="r1"), runner=subprocess_runner)
    elsewhere = make_plan(stack, Options(namespace="team-b", instance="r1"), runner=subprocess_runner)
    assert first.digest == again.digest != elsewhere.digest
    assert first.objects() == [
        {"apiVersion": "v1", "kind": "Service", "name": "model-a"},
        {"apiVersion": "apps/v1", "kind": "Deployment", "name": "model-a"},
    ]


def test_discovered_inference_endpoint_is_resolved_and_covered_by_digest(llmrepo):
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    options = Options(namespace="team-a", instance="r1")
    plan = make_plan(stack, options, runner=subprocess_runner)
    endpoint = plan.summary()["inferenceEndpoint"]
    assert endpoint["purpose"] == "inference"
    assert endpoint["portSelector"] == "http"
    assert endpoint["preferred"]
    assert endpoint["url"] == "http://model-a.team-a.svc.cluster.local:8000"
    assert endpoint["discoverySource"] == "service-selector"
    assert plan.summary()["endpointDetails"] == [endpoint]
    assert plan.stack_record()["endpoints"] == [endpoint]
    digest = plan.digest
    endpoint["reason"] = "Changed discovery evidence."
    assert plan.digest != digest
    assert not plan.blocking


def endpoint_resources(name="model", container="modelserver", port=8000):
    return [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": 80, "targetPort": "serve"}]},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": container, "ports": [{"name": "serve", "containerPort": port}]}]},
                }
            },
        },
    ]


def test_auto_discovery_prefers_router_and_refuses_equal_candidates():
    resources = endpoint_resources() + endpoint_resources("router", "envoy-proxy", 8081)
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert not findings
    assert preferred_inference_endpoint(endpoints)["service"] == "router"
    endpoints, findings = discover_endpoints(resources + endpoint_resources("other", "agentgateway", 8081), "team-a")
    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-ambiguous"


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "LeaderWorkerSet", "DisaggregatedSet"])
def test_auto_discovery_supports_controller_templates_and_decode_roles(kind):
    resources = endpoint_resources()
    workload = resources[1]
    template = workload["spec"]["template"]
    workload["kind"] = kind
    if kind == "LeaderWorkerSet":
        workload["spec"] = {"leaderWorkerTemplate": {"workerTemplate": template}}
        resources[0]["spec"]["selector"] = {"leaderworkerset.sigs.k8s.io/name": "model"}
    elif kind == "DisaggregatedSet":
        workload["spec"] = {
            "roles": [
                {"name": "prefill", "spec": {"leaderWorkerTemplate": {"workerTemplate": copy.deepcopy(template)}}},
                {"name": "decode", "spec": {"leaderWorkerTemplate": {"workerTemplate": template}}},
            ]
        }
        resources[0]["spec"]["selector"] = {
            "disaggregatedset.x-k8s.io/name": "model",
            "disaggregatedset.x-k8s.io/role": "decode",
        }
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert not findings
    assert preferred_inference_endpoint(endpoints)["service"] == "model"
    if kind == "DisaggregatedSet":
        resources[0]["spec"]["selector"]["disaggregatedset.x-k8s.io/role"] = "prefill"
        endpoints, findings = discover_endpoints(resources, "team-a")
        assert preferred_inference_endpoint(endpoints) is None
        assert findings[0].code == "endpoint-not-found"


@pytest.mark.parametrize("explicit_leader", [False, True])
@pytest.mark.parametrize("worker_index", ["0", "1", "2", "-1", "invalid"])
def test_auto_discovery_matches_a_leader_only_lws_service(explicit_leader, worker_index):
    resources = endpoint_resources()
    resources[1]["kind"] = "LeaderWorkerSet"
    resources[1]["spec"] = {
        "replicas": 2,
        "leaderWorkerTemplate": {"size": 2, "workerTemplate": resources[1]["spec"]["template"]},
    }
    if explicit_leader:
        resources[1]["spec"]["leaderWorkerTemplate"]["leaderTemplate"] = copy.deepcopy(
            resources[1]["spec"]["leaderWorkerTemplate"]["workerTemplate"]
        )
    resources[0]["spec"]["selector"] = {
        "leaderworkerset.sigs.k8s.io/name": "model",
        "leaderworkerset.sigs.k8s.io/worker-index": worker_index,
    }

    endpoints, findings = discover_endpoints(resources, "team-a")

    if worker_index not in {"0", "1"}:
        assert preferred_inference_endpoint(endpoints) is None
        assert findings[0].code == "endpoint-not-found"
        return
    assert not findings
    expected = "leaderTemplate" if explicit_leader and worker_index == "0" else "workerTemplate"
    assert preferred_inference_endpoint(endpoints)["backends"][0]["template"] == expected


@pytest.mark.parametrize("args", [["--port", "8088"], ["--port=8088"], ["sh", "-c", "vllm serve model --port 8088"]])
def test_auto_discovery_infers_model_port_from_invocation(args):
    resources = endpoint_resources()
    container = resources[1]["spec"]["template"]["spec"]["containers"][0]
    container.pop("ports")
    container["args"] = args
    resources[0]["spec"]["ports"][0]["targetPort"] = 8088
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert not findings
    assert endpoints[0]["backends"][0]["targetPort"] == 8088


def test_auto_discovery_infers_model_port_when_other_container_ports_are_declared():
    resources = endpoint_resources()
    container = resources[1]["spec"]["template"]["spec"]["containers"][0]
    container["ports"] = [{"name": "metrics", "containerPort": 9090}]
    container["args"] = ["vllm", "serve", "model", "--port", "8088"]
    resources[0]["spec"]["ports"][0]["targetPort"] = 8088

    endpoints, findings = discover_endpoints(resources, "team-a")

    assert not findings
    assert preferred_inference_endpoint(endpoints)["backends"][0]["targetPort"] == 8088


@pytest.mark.parametrize("change", ["udp", "metrics", "grpc", "unmatched", "unknown", "prefill", "headless_only"])
def test_auto_discovery_does_not_guess_an_unverified_port(change):
    resources = endpoint_resources()
    port = resources[0]["spec"]["ports"][0]
    if change == "udp":
        port["protocol"] = "UDP"
    elif change in {"metrics", "grpc"}:
        port["name"] = change
    elif change == "unmatched":
        port["targetPort"] = 12345
    elif change == "unknown":
        resources[1]["spec"]["template"]["spec"]["containers"][0]["name"] = "unrecognized"
    elif change == "prefill":
        resources[1]["spec"]["template"]["metadata"]["labels"]["role"] = "prefill"
    else:
        resources[0]["spec"]["selector"] = {}
        resources[0]["spec"]["clusterIP"] = "None"
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-not-found"


def test_auto_discovery_revalidates_named_and_numeric_service_ports():
    resources = endpoint_resources()
    endpoints, _ = discover_endpoints(resources, "team-a")
    recorded = copy.deepcopy(endpoints[0])
    resources[0]["spec"]["ports"][0]["port"] = 8080
    candidates, _ = discover_endpoints(resources, "team-a")
    verified, findings = revalidate_endpoint(recorded, candidates, {"Deployment/model"})
    assert not findings
    assert verified["port"] == 8080
    recorded["portSelector"] = 80
    verified, findings = revalidate_endpoint(recorded, candidates, {"Deployment/model"})
    assert verified is None
    assert findings[0].code == "endpoint-unresolved"


def test_auto_discovery_revalidation_preserves_disaggregated_role_identity():
    resources = endpoint_resources()
    template = resources[1]["spec"]["template"]
    resources[1]["kind"] = "DisaggregatedSet"
    resources[1]["spec"] = {
        "roles": [
            {"name": role, "spec": {"leaderWorkerTemplate": {"workerTemplate": copy.deepcopy(template)}}}
            for role in ("decode-small", "decode-large")
        ]
    }
    resources[0]["spec"]["selector"] = {
        "disaggregatedset.x-k8s.io/name": "model",
        "disaggregatedset.x-k8s.io/role": "decode-small",
    }
    recorded, _ = discover_endpoints(resources, "team-a")
    resources[0]["spec"]["selector"]["disaggregatedset.x-k8s.io/role"] = "decode-large"
    candidates, _ = discover_endpoints(resources, "team-a")

    verified, findings = revalidate_endpoint(recorded[0], candidates, {"DisaggregatedSet/model"})

    assert verified is None
    assert findings[0].code == "endpoint-unresolved"


@pytest.mark.parametrize(
    "pod_status", [None, {"phase": "Pending", "conditions": [{"type": "Ready", "status": "True"}]}]
)
def test_auto_discovery_requires_a_ready_running_selected_backend(pod_status):
    resources = endpoint_resources()
    pods = []
    if pod_status is not None:
        pods.append(
            {
                "metadata": {"labels": {"app": "model"}},
                "status": pod_status,
            }
        )

    endpoints, findings = discover_endpoints(resources, "team-a", available_pods=pods)

    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-not-found"


def test_auto_discovery_requires_the_selected_disaggregated_role_to_be_ready():
    resources = endpoint_resources()
    template = resources[1]["spec"]["template"]
    resources[1]["kind"] = "DisaggregatedSet"
    resources[1]["spec"] = {
        "roles": [
            {"name": role, "spec": {"leaderWorkerTemplate": {"workerTemplate": copy.deepcopy(template)}}}
            for role in ("prefill", "decode")
        ]
    }
    resources[0]["spec"]["selector"] = {
        "disaggregatedset.x-k8s.io/name": "model",
        "disaggregatedset.x-k8s.io/role": "decode",
    }
    prefill_pod = {
        "metadata": {
            "labels": {
                "app": "model",
                "disaggregatedset.x-k8s.io/name": "model",
                "disaggregatedset.x-k8s.io/role": "prefill",
            }
        },
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }

    endpoints, findings = discover_endpoints(resources, "team-a", available_pods=[prefill_pod])

    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-not-found"


@pytest.mark.parametrize(
    "change", ["metrics_target", "admin_target", "foreign_workload", "foreign_service", "unresolved_port"]
)
def test_auto_discovery_rejects_internal_targets_and_cross_namespace_resources(change):
    resources = endpoint_resources()
    container = resources[1]["spec"]["template"]["spec"]["containers"][0]
    if change in {"metrics_target", "admin_target"}:
        container["name"] = "envoy-proxy"
        container["ports"][0]["name"] = "metrics" if change == "metrics_target" else "admin"
        resources[0]["spec"]["ports"][0]["targetPort"] = 8000
    elif change == "unresolved_port":
        container.pop("ports")
        container["args"] = ["--port", "${PORT}"]
        resources[0]["spec"]["ports"][0]["targetPort"] = 8000
    else:
        resources[1 if change == "foreign_workload" else 0]["metadata"]["namespace"] = "other-team"
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-not-found"


def test_auto_discovery_does_not_default_when_only_internal_ports_are_declared():
    resources = endpoint_resources()
    container = resources[1]["spec"]["template"]["spec"]["containers"][0]
    container["ports"] = [{"name": "admin", "containerPort": 8000}]
    resources[0]["spec"]["ports"][0]["targetPort"] = 8000
    endpoints, findings = discover_endpoints(resources, "team-a")
    assert preferred_inference_endpoint(endpoints) is None
    assert findings[0].code == "endpoint-not-found"


def test_auto_discovery_prefers_non_headless_service_without_using_names():
    resources = endpoint_resources()
    headless = copy.deepcopy(resources[0])
    headless["metadata"]["name"] = "a-model-headless"
    headless["spec"]["clusterIP"] = "None"
    endpoints, findings = discover_endpoints([headless, *resources], "team-a")
    assert not findings
    assert preferred_inference_endpoint(endpoints)["service"] == "model"


def test_offline_plan_output_and_exit_codes(llmrepo, tmp_path, capsys):
    base = ["--state-dir", str(tmp_path), "plan", STACK, "--source", str(llmrepo), "--offline"]
    assert main([*base, "-n", "team-a"]) == 0
    out = capsys.readouterr().out
    assert "Target team-a/model-a-cluster-a" in out and "Result ready to stand up" in out
    assert "Secret/hf-token" in out and "PersistentVolumeClaim/model-a-cache" in out
    assert "nvidia.com/gpu x1/pod" in out
    assert main([*base]) == 2
    assert "no target namespace" in capsys.readouterr().err
    assert main([*base, "-n", "Bad_NS"]) == 2


def test_a_plan_writes_the_manifest_and_the_administrator_parts(llmrepo, tmp_path, capsys):
    overlay = llmrepo / STACK
    (overlay / "priority.yaml").write_text(
        "apiVersion: scheduling.k8s.io/v1\nkind: PriorityClass\nmetadata: {name: serving}\nvalue: 1000\n"
    )
    kustomization = overlay / "kustomization.yaml"
    kustomization.write_text(kustomization.read_text().replace("- ../base", "- ../base\n  - priority.yaml"))
    manifest, admin = tmp_path / "out.yaml", tmp_path / "admin.yaml"
    argv = [
        "--state-dir",
        str(tmp_path),
        "-o",
        "json",
        "plan",
        STACK,
        "--source",
        str(llmrepo),
        "-n",
        "team-a",
    ]
    assert main([*argv, "--offline", "--manifest", str(manifest), "--admin-yaml", str(admin)]) == 3
    summary = json.loads(capsys.readouterr().out)
    assert summary["adminParts"] == [{"object": "PriorityClass/serving", "reason": "cluster-scoped"}]
    assert "kind: PriorityClass" in admin.read_text() and "PriorityClass" not in manifest.read_text()
    assert main([*argv, "--offline", "--skip-admin-parts"]) == 0
