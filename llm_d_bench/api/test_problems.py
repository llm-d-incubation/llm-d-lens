"""Problem-details handlers do not expose unhandled errors to clients."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_d_bench.api.problems import install_problem_handlers
from llm_d_bench.utils.problems import problem


def _client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/boom")
    async def boom() -> dict:
        raise ValueError("something specific went wrong")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_error_hides_detail_and_returns_request_id():
    response = _client().get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["title"] == "Internal server error"
    assert body["detail"] == "An unexpected error occurred."
    assert "something specific" not in str(body)
    assert body["requestId"]


def test_problem_hides_server_details_but_preserves_client_validation():
    assert b"private host" not in problem(502, "Upstream failed", "private host", "upstream_failed").body
    assert b"invalid field" in problem(422, "Invalid request", "invalid field", "invalid_request").body
