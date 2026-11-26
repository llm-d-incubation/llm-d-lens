import copy
import json

import pytest

from lens.catalog import find_stack
from lens.cli import main
from lens.errors import UsageError
from lens.plan import Options, _apply_model_pvc, _set_model_argument, make_plan
from lens.process import subprocess_runner
from lens.sources import resolve_source

STACK = "examples/model-a/cluster-a"


@pytest.mark.parametrize("shell_flag", ["-c", "-lc", "-ec", "-euc"])
def test_model_pvc_refuses_shell_scripts_without_changing_them(shell_flag):
    container = {"command": ["/bin/bash", shell_flag], "args": ["exec vllm serve original"]}
    with pytest.raises(UsageError, match="shell"):
        _set_model_argument(container, "/model-cache/model")
    assert container["args"] == ["exec vllm serve original"]


def test_model_pvc_preserves_the_api_model_name():
    container = {"command": ["vllm", "serve"], "args": ["original", "--port=8000"]}
    _set_model_argument(container, "/model-cache/model")
    assert container["args"] == ["/model-cache/model", "--port=8000", "--served-model-name", "original"]


@pytest.mark.parametrize(
    "container",
    [
        {"args": ["original"]},
        {"command": ["vllm"], "args": ["serve", "original"]},
        {"command": ["vllm", "serve", "original"]},
        {"command": ["python3", "-m", "vllm.entrypoints.openai.api_server"], "args": ["--model", "original"]},
        {"command": ["python", "-m", "sglang.launch_server"], "args": ["--model-path=original"]},
        {"command": ["sglang", "serve"], "args": ["--model-path", "original"]},
    ],
)
def test_model_pvc_supports_direct_server_argument_layouts(container):
    _set_model_argument(container, "/model-cache/model")
    values = [*container.get("command", []), *container.get("args", [])]
    assert "/model-cache/model" in values or "--model-path=/model-cache/model" in values
    assert values[-2:] == ["--served-model-name", "original"]


@pytest.mark.parametrize("alias", [["--served-model-name", "public"], ["--served-model-name=public"]])
def test_model_pvc_keeps_explicit_api_model_names(alias):
    container = {"command": ["vllm", "serve"], "args": ["original", *alias]}
    _set_model_argument(container, "/model-cache/model")
    assert container["args"] == ["/model-cache/model", *alias]


@pytest.mark.parametrize(
    "container",
    [
        {"command": ["wrapper"], "args": ["original"]},
        {"command": ["vllm", "serve"], "args": ["--port=8000"]},
        {"args": ["--model"]},
        {"args": ["--model", "--port=8000"]},
        {"args": ["--model="]},
        {"args": ["--model=one", "--model-path=two"]},
    ],
)
def test_model_pvc_refuses_ambiguous_or_missing_model_arguments(container):
    original = copy.deepcopy(container)
    with pytest.raises(UsageError):
        _set_model_argument(container, "/model-cache/model")
    assert container == original


@pytest.mark.parametrize("kind", ["Pod", "Deployment", "LeaderWorkerSet", "DisaggregatedSet"])
@pytest.mark.parametrize("names", [None, ["prefill", "decode"]])
def test_model_pvc_selects_containers_across_pod_templates(kind, names):
    templates = []
    for name in names or ["modelserver-leader", "modelserver-worker"]:
        templates.append(
            {
                "spec": {
                    "containers": [
                        {"name": name, "command": ["vllm", "serve"], "args": ["original"]},
                        {"name": "metrics", "command": ["sh", "-c"], "args": ["metrics"]},
                    ]
                }
            }
        )
    if kind == "Pod":
        templates[0]["spec"]["containers"].extend(templates.pop()["spec"]["containers"][:1])
        document = {"kind": kind, **templates[0]}
    elif kind == "Deployment":
        templates[0]["spec"]["containers"].extend(templates.pop()["spec"]["containers"][:1])
        document = {"kind": kind, "spec": {"template": templates[0]}}
    else:
        spec = {"leaderWorkerTemplate": {"size": 2, "leaderTemplate": templates[0], "workerTemplate": templates[1]}}
        document = {"kind": kind, "spec": spec if kind == "LeaderWorkerSet" else {"roles": [{"spec": spec}]}}
    _apply_model_pvc([document], "weights", "model", names)
    for template in templates:
        assert template["spec"]["volumes"] == [
            {"name": "model-cache", "persistentVolumeClaim": {"claimName": "weights"}}
        ]
        for container in template["spec"]["containers"]:
            if container["name"] == "metrics":
                assert container == {"name": "metrics", "command": ["sh", "-c"], "args": ["metrics"]}
            else:
                assert container["args"] == ["/model-cache/model", "--served-model-name", "original"]
                assert {"name": "HF_HUB_OFFLINE", "value": "1"} in container["env"]
    applied = copy.deepcopy(document)
    _apply_model_pvc([document], "weights", "model", names)
    assert document == applied


@pytest.mark.parametrize("names", [["missing"], ["modelserver", "missing"], ["Bad_Name"]])
def test_model_pvc_refuses_missing_or_invalid_container_selections(names):
    document = {"kind": "Pod", "spec": {"containers": [{"name": "modelserver", "args": ["original"]}]}}
    with pytest.raises(UsageError, match="container"):
        _apply_model_pvc([document], "weights", "model", names)


def test_model_container_selection_requires_a_claim():
    with pytest.raises(UsageError, match="requires? --model-pvc-claim-name"):
        _apply_model_pvc([], "", "", ["inference"])


def test_model_pvc_refuses_a_mount_path_used_by_another_volume():
    document = {
        "kind": "Pod",
        "spec": {
            "containers": [
                {
                    "name": "modelserver",
                    "args": ["original"],
                    "volumeMounts": [{"name": "other", "mountPath": "/model-cache"}],
                }
            ],
            "volumes": [{"name": "other", "emptyDir": {}}],
        },
    }
    with pytest.raises(UsageError, match="mount"):
        _apply_model_pvc([document], "weights", "model")


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
