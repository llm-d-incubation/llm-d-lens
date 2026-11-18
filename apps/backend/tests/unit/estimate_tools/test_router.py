"""Shared HTTP contracts, catalog metadata and disconnect propagation."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from llm_d_lens.estimate_tools import router as routes
from llm_d_lens.estimate_tools.models import BatchResult, EffectiveConfiguration, EstimateRequest, Evaluation
from llm_d_lens.estimate_tools.service import EstimateService


def payload(**changes):
    return {
        "model": {"name": "example/model"},
        "hardware": {"profile_id": "nvidia", "model": "H100", "nodes": 1, "devices_per_node": 8},
        **changes,
    }


@pytest.fixture
def app(monkeypatch):
    from llm_d_lens.estimate_tools.aic import adapter

    async def dispatch(request, operation):
        return await getattr(adapter, operation)(request)

    application = FastAPI()
    application.include_router(routes.router)
    monkeypatch.setattr(routes, "default_estimator", lambda: EstimateService())
    monkeypatch.setattr(EstimateService, "_aisimulate", staticmethod(dispatch))
    return application


@pytest.mark.asyncio
async def test_shared_discovery_and_blis_unavailable(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        discovery = await client.get("/api/v1/estimate-tools")
        assert discovery.status_code == 200
        assert [item["tool"] for item in discovery.json()] == ["aisimulate", "blis"]
        supported = await client.post("/api/v1/estimate-tools/support", json=payload(tools=["blis"]))
        assert supported.status_code == 200
        assert supported.json()[0]["reason"]["code"] == "runtime_not_configured"
        response = await client.post("/api/v1/estimate-tools/estimate", json=payload(tools=["blis"]))
        assert response.status_code == 200
        assert response.json()["evaluations"][0]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_default_and_invalid_selection(app, monkeypatch):
    from llm_d_lens.estimate_tools.aic import adapter

    request = EstimateRequest.model_validate(payload())
    result = Evaluation(
        tool="aisimulate",
        strategy="native_exact",
        status="succeeded",
        configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
    )
    estimate = AsyncMock(return_value=result)
    monkeypatch.setattr(adapter, "estimate", estimate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/estimate-tools/estimate", json=payload())
        assert response.status_code == 200
        assert response.json() == BatchResult(outcome="succeeded", evaluations=[result]).model_dump(mode="json")
        invalid = await client.post("/api/v1/estimate-tools/estimate", json=payload(tools=[]))
        assert invalid.status_code == 422
    estimate.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_disconnect_cancels_and_waits_for_cleanup():
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    request = Request({"type": "http"}, receive=receive)
    with pytest.raises(HTTPException) as error:
        await routes._connected(request, operation())
    assert error.value.status_code == 499
    assert cleaned.is_set()


def test_routes_have_permission_and_mcp_metadata(app):
    from llm_d_lens.auth.routes import resolve_route_permission

    for route in routes.router.routes:
        assert route.operation_id and route.summary and route.description
        for method in route.methods:
            assert resolve_route_permission(method, route.path) == (True, "candidate:candidate:search")
    assert "/api/v1/estimate-tools/estimate" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_saved_artifact_read_access_is_required(app, monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.configuration import service as configurations
    from llm_d_lens.core.exceptions import ForbiddenError

    artifact = SimpleNamespace(
        artifact_id="saved",
        deployable_configuration=SimpleNamespace(
            provenance={"cluster_ref": {"id": "other-cluster"}},
        ),
    )
    monkeypatch.setattr(configurations, "get_configuration_artifact", lambda _artifact_id: artifact)
    monkeypatch.setattr(routes, "resource_readable", lambda *args, **kwargs: False)
    estimator = AsyncMock()
    monkeypatch.setattr(routes, "default_estimator", lambda: estimator)
    with pytest.raises(ForbiddenError):
        await routes.estimate_saved_endpoint(
            "saved", EstimateRequest.model_validate(payload()), Request({"type": "http"})
        )
    estimator.estimate.assert_not_awaited()


@pytest.mark.parametrize("assessment", ["unknown", "not_satisfied"])
def test_agentic_exact_evidence_keeps_unknown_and_conflicting_outcomes(assessment):
    from llm_d_lens.agentic.service import exact_evaluation_evidence

    identity = EffectiveConfiguration.from_request(EstimateRequest.model_validate(payload())).fingerprint
    evaluations = [
        Evaluation(
            tool="blis",
            strategy="exact",
            status="succeeded",
            constraint_assessment="satisfied",
            configuration_fingerprint=identity,
        ),
        Evaluation(
            tool="aisimulate",
            strategy="native_exact",
            status="succeeded",
            constraint_assessment=assessment,
            configuration_fingerprint=identity,
        ),
    ]
    result = exact_evaluation_evidence("saved", BatchResult(outcome="succeeded", evaluations=evaluations))
    assert [item.source for item in result.evidence] == ["blis", "aisimulate"]
    assert result.evidence[1].details["constraint_assessment"] == assessment
    assert any(("Unknown" if assessment == "unknown" else "conflicting") in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_agentic_exact_route_does_not_generate_candidates(monkeypatch):
    from llm_d_lens.agentic import router as agentic_routes
    from llm_d_lens.agentic.models import AgenticExactEvaluationRequest

    batch = await EstimateService().estimate(payload(tools=["blis"]))
    saved = AsyncMock(return_value=batch)
    generator = AsyncMock(side_effect=AssertionError("Exact evidence must not enter recommendation planning"))
    monkeypatch.setattr(routes, "estimate_saved_endpoint", saved)
    monkeypatch.setattr(agentic_routes.service, "create", generator)
    request = AgenticExactEvaluationRequest(configuration_artifact_id="saved", estimate_request=payload(tools=["blis"]))
    response = await agentic_routes.estimate_agentic_configuration(request, Request({"type": "http"}))
    assert response.result.evaluations[0].tool == "blis"
    assert response.result.evaluations[0].status == "unavailable"
    saved.assert_awaited_once()
    generator.assert_not_awaited()
