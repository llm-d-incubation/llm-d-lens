from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from llm_d_lens.api.web import WebEntryMiddleware


def application(tmp_path, **options):
    app = FastAPI()
    app.add_middleware(WebEntryMiddleware, frontend_dist=tmp_path, **options)
    (tmp_path / "index.html").write_text("<html>Lens</html>")
    return app


def test_static_spa_and_unknown_api_are_distinct(tmp_path):
    app = application(tmp_path)
    (tmp_path / "asset.js").write_text("const asset = true;")
    with TestClient(app) as client:
        assert client.get("/models").text == "<html>Lens</html>"
        assert client.get("/asset.js").text == "const asset = true;"
        missing = client.get("/api/no-such-endpoint")
        assert missing.status_code == 404
        assert "json" in missing.headers["content-type"]


def test_rate_limit_rejects_excess_requests(tmp_path):
    with TestClient(application(tmp_path, rate_limit=2)) as client:
        assert client.get("/api/missing").status_code == 404
        assert client.get("/api/missing").status_code == 404
        rejected = client.get("/api/missing")
        assert rejected.status_code == 429
        assert "retry-after" in rejected.headers


def test_json_limit_does_not_buffer_or_limit_multipart(tmp_path):
    app = application(tmp_path, max_json_bytes=10)

    @app.post("/api/upload")
    async def upload(request: Request):
        return {"size": len(await request.body())}

    with TestClient(app) as client:
        assert client.post("/api/upload", json={"long": "a" * 50}).status_code == 413
        response = client.post("/api/upload", files={"file": ("input.txt", b"a" * 100)})
        assert response.status_code == 200
        assert response.json()["size"] > 100


def test_legacy_proxy_alias_reaches_canonical_backend(tmp_path):
    app = application(tmp_path)

    @app.get("/api/cluster/example")
    async def example():
        return {"canonical": True}

    with TestClient(app) as client:
        assert client.get("/api/v1/cluster/example").json() == {"canonical": True}


def test_config_preserves_browser_defaults(monkeypatch):
    from llm_d_lens.api.main import app

    monkeypatch.setenv("LLM_D_ROOT", "/opt/llm-d")
    monkeypatch.delenv("LLM_D_BRANCH", raising=False)
    response = TestClient(app).get("/api/config")
    assert response.status_code == 200
    assert response.json()["deploymentDefaults"]["repository"] == "/opt/llm-d"
    assert response.json()["deploymentDefaults"]["branch"] == "tpc-llmdbench"


def test_root_backend_routes_are_not_replaced_with_the_spa(tmp_path):
    app = application(tmp_path)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        redirect = client.get("/docs/oauth2-redirect")
        assert redirect.status_code == 200
        assert "swaggerUIRedirectOauth2" in redirect.text
        assert client.get("/models").text == "<html>Lens</html>"
