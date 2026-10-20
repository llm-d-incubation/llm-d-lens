# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from importlib import import_module
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.configuration import router
from llm_d_lens.configuration.app import app as compatibility_app


def test_shared_api_registers_configuration_router():
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200
    assert client.post("/api/v1/configurations/resolve", json={}).status_code == 422
    assert client.post("/api/v1/configurations/render", json={}).status_code == 422
    assert client.post("/api/v1/configurations/save", json={}).status_code == 422


def test_configuration_router_owns_domain_prefix():
    assert router.prefix == "/api/v1/configurations"
    assert compatibility_app is app


@pytest.mark.parametrize("operation", ["resolve", "render", "save"])
def test_legacy_configuration_validation_preserves_error_field(operation):
    client = TestClient(app)
    canonical = client.post(f"/api/v1/configurations/{operation}", json={})
    legacy = client.post(f"/api/configurations/{operation}", json={})
    assert legacy.status_code == canonical.status_code == 422
    assert legacy.json() == {**canonical.json(), "error": canonical.json()["detail"]}


def test_legacy_configuration_resolve_reuses_service(monkeypatch):
    module = import_module("llm_d_lens.configuration.router")
    service = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(module, "resolve_configurations", service)
    payload = {
        "candidate_source": {"name": "baseline_search"},
        "configurations": [{"configuration_id": "candidate-1", "payload": {"model": "example/model"}}],
    }
    client = TestClient(app)
    response = client.post("/api/configurations/resolve", json=payload)
    assert response.status_code == 200
    assert response.json()["results"] == []
    service.assert_awaited_once()


def test_legacy_configuration_service_failure_preserves_error_field(monkeypatch):
    module = import_module("llm_d_lens.configuration.router")
    monkeypatch.setattr(module, "resolve_configurations", AsyncMock(side_effect=module.ConfigurationError("invalid")))
    payload = {
        "candidate_source": {"name": "baseline_search"},
        "configurations": [{"configuration_id": "candidate-1", "payload": {"model": "example/model"}}],
    }
    response = TestClient(app).post("/api/configurations/resolve", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == response.json()["detail"] == "invalid"
