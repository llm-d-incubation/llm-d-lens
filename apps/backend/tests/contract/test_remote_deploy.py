from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_d_lens.deploy import remote_deploy as remote
from llm_d_lens.infrastructure.ssh import SshTarget


def test_remote_routes_declare_concrete_response_models():
    routes = [route for route in remote.router.routes if getattr(route, "methods", None)]
    assert routes
    assert all(getattr(route, "response_model", None) is not None for route in routes)


def target(**overrides):
    return {
        "host": "host.example",
        "username": "operator",
        "authMethod": "password",
        "cards": ["0"],
        "hostFingerprint": "SHA256:" + "a" * 43,
        **overrides,
    }


@pytest.mark.parametrize("value", ["", "unconfirmed", "SHA256:short"])
def test_remote_requires_confirmed_fingerprint(value):
    with pytest.raises(remote.RemoteError, match="fingerprint"):
        remote.parse_target(target(hostFingerprint=value))


def test_production_host_allowlist(monkeypatch):
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.setenv("PRISM_REMOTE_HOST_ALLOWLIST", "other.example")
    with pytest.raises(remote.RemoteError, match="ALLOWLIST") as caught:
        remote.parse_target(target())
    assert caught.value.status == 403


def test_private_key_symlink_cannot_escape_allowed_directory(monkeypatch, tmp_path):
    directory = tmp_path / "keys"
    directory.mkdir()
    secret = tmp_path / "outside"
    secret.write_text("not a real key")
    link = directory / "key"
    link.symlink_to(secret)
    monkeypatch.setenv("PRISM_SSH_KEY_DIR", str(directory))
    parsed = remote.parse_target(target(authMethod="key", keyPath=str(link)))
    with pytest.raises(remote.RemoteError, match="inside"):
        remote.connection_target(parsed, {})


def test_agent_is_opt_in(monkeypatch):
    assert SshTarget("host", 22, "user").allow_agent is False
    monkeypatch.setenv("SSH_AUTH_SOCK", "/agent.sock")
    parsed = remote.parse_target(target(authMethod="agent"))
    assert remote.connection_target(parsed, {}).allow_agent is True


def test_invalid_guide_cannot_inject_shell():
    with pytest.raises(remote.RemoteError, match="Invalid deployment guide"):
        remote.guide_for({"guide": "optimized-baseline; touch /tmp/injected"})
    assert remote.guide_for({"guide": "optimized-baseline"}) == "optimized-baseline"


def test_remote_repository_uses_remote_cache_and_quotes_paths():
    command = remote.prepare_repository_command(
        {"deploymentTarget": {"repository": "https://github.com/llm-d/llm-d.git"}}
    )["command"]
    assert "${LENS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lens}" in command
    assert "deploy-repos/llm-d/llm-d" in command
    command = remote.prepare_repository_command({"deploymentTarget": {"repository": "~/path $(touch injected)"}})[
        "command"
    ]
    assert "\"$HOME\"/'path $(touch injected)'" in command


def test_fingerprint_probe_does_not_authenticate(monkeypatch):
    monkeypatch.setenv("PRISM_REMOTE_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("NODE_ENV", "development")
    inspect = Mock(return_value=SimpleNamespace(fingerprint="SHA256:" + "a" * 43))
    execute = AsyncMock()
    monkeypatch.setattr(remote, "inspect_host_key", inspect)
    monkeypatch.setattr(remote, "execute", execute)
    app = FastAPI()
    app.include_router(remote.router)
    response = TestClient(app).post("/api/v1/remote-deploy/fingerprint", json={"target": {"host": "host.example"}})
    assert response.status_code == 200
    assert response.json()["host"] == "host.example"
    inspect.assert_called_once_with("host.example", 22, 10)
    execute.assert_not_awaited()


def test_remote_start_preserves_response(monkeypatch):
    monkeypatch.setenv("PRISM_REMOTE_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("NODE_ENV", "development")
    execute = AsyncMock(return_value={"code": 0, "stdout": "created\n", "stderr": ""})
    monkeypatch.setattr(remote, "execute", execute)
    app = FastAPI()
    app.include_router(remote.router)
    response = TestClient(app).post(
        "/api/v1/remote-deploy/start",
        json={
            "target": target(),
            "credentials": {"password": "test-only"},
            "plan": {
                "accelerator": "Intel Data Center GPU Max",
                "model": "example/model",
                "deploymentTarget": {"repository": "/workspace/llm-d"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deploying"
    assert response.json()["cluster"] == "operator@host.example:22"
    assert "machineType" not in response.json()
    assert response.json()["accelerator"] == "Intel Data Center GPU Max"
    command = execute.call_args.args[2]
    assert '--llm-d-root "$LLM_D_ROOT"' in command
    assert "--no-wait --prefill-replicas 0" in command
    assert "--model hf://example/model" in command


def test_unknown_hardware_does_not_default_to_supported_vendor():
    with pytest.raises(remote.RemoteError, match="supports"):
        remote.deployment_command({"accelerator": "unknown"}, target(), "namespace", "service")


@pytest.mark.parametrize("error_type", [OSError, remote.paramiko.SSHException, EOFError])
def test_remote_internal_errors_are_logged_not_exposed(monkeypatch, caplog, error_type):
    detail = "internal traceback /private/ssh/key credential-value"
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.setattr(remote, "inspect_host_key", Mock(side_effect=error_type(detail)))
    app = FastAPI()
    app.include_router(remote.router)
    response = TestClient(app).post("/api/v1/remote-deploy/fingerprint", json={"target": {"host": "host.example"}})
    assert response.status_code == 502
    assert response.json() == {"error": "Remote deployment failed"}
    assert detail not in response.text
    assert detail in caplog.text


def test_remote_command_errors_do_not_expose_stderr(monkeypatch, caplog):
    detail = "internal command failure with credential-value"
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.setattr(remote, "execute", AsyncMock(return_value={"code": 1, "stdout": "", "stderr": detail}))
    app = FastAPI()
    app.include_router(remote.router)
    response = TestClient(app).post("/api/v1/remote-deploy/teardown", json={"target": target(), "namespace": "test"})
    assert response.status_code == 502
    assert response.json() == {"error": "Remote deployment failed"}
    assert detail not in response.text
    assert detail in caplog.text


def test_remote_validation_keeps_actionable_message(monkeypatch):
    monkeypatch.setenv("NODE_ENV", "development")
    app = FastAPI()
    app.include_router(remote.router)
    response = TestClient(app).post("/api/v1/remote-deploy/fingerprint", json={"target": {"host": "invalid;host"}})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid SSH host"}
