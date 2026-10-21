"""Browser entry-point compatibility for the consolidated Python service."""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path

from fastapi import APIRouter
from limits import RateLimitItemPerMinute
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import FixedWindowRateLimiter
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

router = APIRouter(include_in_schema=False)
MAX_JSON_BYTES = 50 * 1024 * 1024


@router.get("/api/config")
async def config():
    return {
        "deploymentDefaults": {
            "repository": os.environ.get("LLM_D_ROOT", ""),
            "branch": os.environ.get("LLM_D_BRANCH", "tpc-llmdbench"),
            "httpProxy": os.environ.get("HTTP_PROXY", ""),
            "httpsProxy": os.environ.get("HTTPS_PROXY", ""),
            "noProxy": os.environ.get("NO_PROXY", ""),
            "mountPath": os.environ.get("LLM_D_MOUNT_PATH", ""),
            "mountModelName": os.environ.get("LLM_D_MOUNT_MODEL_NAME", ""),
        }
    }


class WebEntryMiddleware:
    def __init__(self, app, *, frontend_dist=None, rate_limit=1000, max_json_bytes=MAX_JSON_BYTES):
        self.app = app
        self.frontend_dist = (
            Path(frontend_dist) if frontend_dist else Path(__file__).resolve().parents[5] / "apps/frontend/dist"
        )
        self.static = StaticFiles(directory=self.frontend_dist, check_dir=False)
        self.rate = RateLimitItemPerMinute(rate_limit)
        self.max_json_bytes = max_json_bytes
        self.limiter = None
        self.loop = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        for alias, canonical in (("/api/v1/cluster", "/api/cluster"), ("/api/v1/simulation", "/api/simulation")):
            if path == alias or path.startswith(alias + "/"):
                scope = dict(scope, path=canonical + path[len(alias) :])
                scope["raw_path"] = scope["path"].encode()
                path = scope["path"]
                break
        request = Request(scope)
        api = (
            path in {"/api", "/v1", "/healthz", "/docs/oauth2-redirect"}
            or path.startswith("/api/")
            or path.startswith("/v1/")
        )
        if not api and request.method in {"GET", "HEAD"} and self.frontend_dist.is_dir():
            try:
                response = await self.static.get_response(self.static.get_path(scope), scope)
                if response.status_code != 404:
                    await response(scope, receive, send)
                    return
            except HTTPException as error:
                if error.status_code != 404:
                    raise
        if api or (not api and request.method == "GET"):
            loop = asyncio.get_running_loop()
            if self.limiter is None or self.loop is not loop:
                self.limiter = FixedWindowRateLimiter(MemoryStorage())
                self.loop = loop
            client = request.client.host if request.client else "unknown"
            allowed = await self.limiter.hit(self.rate, client)
            reset_at, remaining = await self.limiter.get_window_stats(self.rate, client)
            headers = {
                "ratelimit-policy": f"{self.rate.amount};w=60",
                "ratelimit-limit": str(self.rate.amount),
                "ratelimit-remaining": str(remaining),
                "ratelimit-reset": str(max(0, math.ceil(reset_at - time.time()))),
            }
            if not allowed:
                headers["retry-after"] = headers["ratelimit-reset"]
                await PlainTextResponse("Too many requests, please try again later.", status_code=429, headers=headers)(
                    scope, receive, send
                )
                return
            original_send = send

            async def with_headers(message):
                if message["type"] == "http.response.start":
                    message = dict(
                        message,
                        headers=[
                            *message.get("headers", []),
                            *[(key.encode(), value.encode()) for key, value in headers.items()],
                        ],
                    )
                await original_send(message)

            send = with_headers
        if not api and request.method in {"GET", "HEAD"}:
            index = self.frontend_dist / "index.html"
            response = (
                FileResponse(index) if index.is_file() else PlainTextResponse("Internal Server Error", status_code=500)
            )
            await response(scope, receive, send)
            return
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type in {"application/json", "application/x-www-form-urlencoded"}:
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_json_bytes:
                    await JSONResponse({"error": "request entity too large"}, status_code=413)(scope, receive, send)
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            original_receive, replayed = receive, False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)
