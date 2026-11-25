import json

import pytest

from lens.catalog import find_stack
from lens.cli import main
from lens.plan import Options, make_plan, resolve_endpoints
from lens.process import subprocess_runner
from lens.sources import resolve_source

STACK = "examples/model-a/cluster-a"


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


def test_declared_inference_endpoint_is_resolved_and_covered_by_digest(llmrepo):
    stack = find_stack(resolve_source(str(llmrepo)), STACK)
    options = Options(namespace="team-a", instance="r1")
    original = make_plan(stack, options, runner=subprocess_runner)
    (stack.path / "lens.yaml").write_text(
        "endpoints:\n  - purpose: inference\n    service: model-a\n    port: 8000\n    preferred: true\n"
    )
    plan = make_plan(stack, options, runner=subprocess_runner)
    endpoint = plan.summary()["inferenceEndpoint"]
    assert endpoint == {
        "purpose": "inference",
        "service": "model-a",
        "namespace": "team-a",
        "port": 8000,
        "portName": "http",
        "portSelector": 8000,
        "scheme": "http",
        "preferred": True,
        "url": "http://model-a.team-a.svc.cluster.local:8000",
    }
    assert plan.summary()["endpointDetails"] == [endpoint]
    assert plan.stack_record()["endpoints"] == [endpoint]
    assert plan.digest != original.digest
    assert not plan.blocking


@pytest.mark.parametrize(
    ("declarations", "code"),
    [
        (None, "endpoint-invalid"),
        (["not a mapping"], "endpoint-invalid"),
        ([{"purpose": "inference", "service": "model-a", "port": True}], "endpoint-invalid"),
        ([{"purpose": "inference", "service": "model-a", "port": "http", "scheme": "ftp"}], "endpoint-invalid"),
        ([{"purpose": "metrics", "service": "model-a", "port": "http", "preferred": True}], "endpoint-invalid"),
        ([{"purpose": "inference", "service": "missing", "port": "http"}], "endpoint-unresolved"),
        ([{"purpose": "inference", "service": "model-a", "port": "missing"}], "endpoint-unresolved"),
        ([{"purpose": "inference", "service": "model-a", "port": "udp"}], "endpoint-unresolved"),
        (
            [
                {"purpose": "inference", "service": "model-a", "port": "http"},
                {"purpose": "inference", "service": "model-a", "port": "https"},
            ],
            "endpoint-ambiguous",
        ),
        (
            [
                {"purpose": "inference", "service": "model-a", "port": "http", "preferred": True},
                {"purpose": "inference", "service": "model-a", "port": "https", "preferred": True},
            ],
            "endpoint-ambiguous",
        ),
    ],
)
def test_invalid_endpoint_declarations_are_blocking(declarations, code):
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "model-a"},
        "spec": {
            "ports": [
                {"name": "http", "port": 8000},
                {"name": "https", "port": 8443},
                {"name": "udp", "port": 9000, "protocol": "UDP"},
            ]
        },
    }
    _, findings = resolve_endpoints(declarations, [service], "team-a")
    assert any(finding.severity == "error" and finding.code == code for finding in findings)


def test_named_ports_resolve_to_service_ports_not_target_ports():
    declarations = [
        {"purpose": "metrics", "service": "router", "port": "metrics"},
        {"purpose": "inference", "service": "router", "port": "https", "scheme": "https", "preferred": True},
    ]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "router"},
        "spec": {
            "ports": [
                {"name": "metrics", "port": 9090},
                {"name": "https", "port": 443, "targetPort": 8443},
            ]
        },
    }
    endpoints, findings = resolve_endpoints(declarations, [service], "team-a")
    assert not findings
    assert endpoints[1]["port"] == 443
    assert endpoints[1]["url"] == "https://router.team-a.svc.cluster.local:443"


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
