import shlex
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.deploy import legacy_poc as poc


@pytest.fixture(autouse=True)
def clear_jobs():
    poc.jobs.clear()
    yield
    poc.jobs.clear()


def test_smoke_start_preserves_response_and_manifest(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(poc, "launch", launch)
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "worker-1")
    response = TestClient(app).post("/api/deploy-poc/start", json={"config": "smoke-pause"})
    assert response.json() == {"namespace": "prism-smoke-test", "config": "smoke-pause", "phase": "deploying"}
    arguments = launch.call_args.args
    namespace, pod = yaml.safe_load_all(arguments[5])
    assert namespace["metadata"]["name"] == "prism-smoke-test"
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "worker-1"}
    assert arguments[3] == ["apply", "-f", "-"]


@pytest.mark.parametrize("operation", ["start", "teardown"])
def test_invalid_session_never_falls_back(monkeypatch, operation):
    launch = Mock()
    monkeypatch.setattr(poc, "launch", launch)
    response = TestClient(app).post(
        f"/api/deploy-poc/{operation}",
        json={
            "config": "smoke-pause",
            "clusterSessionId": "not-a-session",
        },
    )
    assert response.status_code == 409
    launch.assert_not_called()


def test_running_deploy_returns_conflict(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(poc, "launch", launch)
    poc.jobs["prism-smoke-test"] = {"action": "deploy", "phase": "running"}
    response = TestClient(app).post("/api/deploy-poc/start", json={"config": "smoke-pause"})
    assert response.status_code == 409
    launch.assert_not_called()


def test_validation_and_status_preserve_contract(monkeypatch):
    state = {"status": "ready", "pods_total": 2, "pods_ready": 2, "gateway_endpoint": "http://gateway"}
    monkeypatch.setattr(poc, "cluster_status", AsyncMock(return_value=(state, None)))
    client = TestClient(app)
    validation = client.get("/api/deploy-poc/validate").json()
    assert validation["ready"] is True
    assert validation["passed"] == validation["total"] == 4
    status = client.get("/api/deploy-poc/status").json()
    assert status["k8s"] == state
    assert status["job"] is None
    assert status["k8sError"] is None


def test_cli_arguments_remain_literal():
    value = "; touch /tmp/should-not-exist; #"
    command = poc.build_cli_deploy_command(value, "prism-deploy-poc", value, value)
    assert command.count(shlex.quote(value)) == 4


@pytest.mark.asyncio
async def test_spawn_error_finishes_job(monkeypatch):
    spawn = AsyncMock(side_effect=FileNotFoundError("missing executable"))
    monkeypatch.setattr(poc.ShellClient, "spawn_with_executable", spawn)
    job = {"phase": "running", "logTail": []}
    await poc.run_job(job, "missing", ["user-argument"], {}, None)
    assert spawn.call_args.args == ("missing", ["user-argument"])
    assert job["phase"] == "failed"
    assert job["exitCode"] == -1
    assert job["finishedAt"]
    assert "spawn error:" in job["logTail"][0]


@pytest.mark.parametrize("operation", ["start", "teardown"])
@pytest.mark.parametrize("config", ["smoke-pause", "optimized-baseline-xpu"])
def test_request_cannot_select_executable(monkeypatch, operation, config):
    launch = Mock()
    monkeypatch.setattr(poc, "launch", launch)
    monkeypatch.delenv("PRISM_K8S_TARGET_NODE", raising=False)
    monkeypatch.setenv("KUBECTL_BIN", "/trusted/kubectl")
    monkeypatch.setenv("OPTIMALBENCH_BIN", "/trusted/llm-d-bench")
    response = TestClient(app).post(
        f"/api/deploy-poc/{operation}",
        json={"config": config, "namespace": "user-selected", "executable": "/untrusted/program"},
    )
    assert response.status_code == 200
    program, arguments = launch.call_args.args[2:4]
    expected = (
        "/trusted/kubectl" if config == "smoke-pause" else "bash" if operation == "start" else "/trusted/llm-d-bench"
    )
    assert program == expected
    assert "/untrusted/program" not in arguments
