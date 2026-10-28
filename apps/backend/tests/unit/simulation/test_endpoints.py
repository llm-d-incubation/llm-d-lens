"""Exercise model discovery against real local HTTP responses."""

from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from llm_d_lens.simulation.endpoints import discover_models
from llm_d_lens.simulation.errors import SimulationConfigurationError


@asynccontextmanager
async def endpoint(handler):
    app = web.Application()
    app.router.add_get("/v1/models", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_authenticated_discovery_succeeds_and_missing_token_fails():
    async def handler(request):
        if request.headers.get("Authorization") != "Bearer test-token":
            return web.Response(status=401)
        return web.json_response({"data": [{"id": "model"}]})

    async with endpoint(handler) as url:
        with pytest.raises(SimulationConfigurationError, match="HTTP 401"):
            await discover_models(url)
        result = await discover_models(url, api_key="test-token")
        assert result.endpoint == url
        assert result.models == ["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,message",
    [
        (503, "", "HTTP 503"),
        (302, "", "HTTP 302"),
        (200, "invalid", "invalid JSON"),
        (200, '{"data":[]}', "no models"),
    ],
)
async def test_invalid_endpoint_responses_fail(status, body, message):
    async def handler(_request):
        return web.Response(status=status, text=body)

    async with endpoint(handler) as url:
        with pytest.raises(SimulationConfigurationError, match=message):
            await discover_models(url)
