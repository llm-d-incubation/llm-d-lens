"""Same-origin proxy for the in-browser speech-to-text model assets.

The browser runs Whisper locally through transformers.js. Fetching model files
directly from huggingface.co fails on networks where only the Lens host has
outbound proxy access, so this route streams the allow-listed model through the
authenticated Lens backend instead.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/v1/speech-models", tags=["speech-models"])

_HF_HOST = "https://huggingface.co"
_ALLOWED_PREFIXES = ("onnx-community/whisper-tiny/", "onnx-community/whisper-base/")
_PASSTHROUGH_HEADERS = {"content-type", "cache-control", "etag", "last-modified"}


@router.get("/{rest:path}", include_in_schema=False)
async def speech_model_asset(rest: str) -> StreamingResponse:
    if not rest.startswith(_ALLOWED_PREFIXES) or ".." in rest:
        raise HTTPException(status_code=404, detail="speech model asset not found")
    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, read=None))
    request = client.build_request("GET", f"{_HF_HOST}/{rest}")
    try:
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="could not fetch speech model asset") from exc
    if upstream.status_code >= 400:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=status, detail="speech model asset is unavailable")

    headers = {name: value for name, value in upstream.headers.items() if name.lower() in _PASSTHROUGH_HEADERS}

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), headers=headers, media_type=upstream.headers.get("content-type"))
