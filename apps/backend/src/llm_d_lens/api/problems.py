"""Uniform Problem Details responses for Prism API modules."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from llm_d_lens.core.exceptions import DomainError

__all__ = ["install_problem_handlers", "problem"]

logger = logging.getLogger(__name__)


def problem(
    status: int,
    title: str,
    detail: object,
    code: str,
    extra: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "type": "about:blank",
        "status": status,
        "title": title,
        "detail": "An unexpected error occurred." if status >= 500 else detail,
        "code": code,
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status,
        content=content,
        media_type="application/problem+json",
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_problem(_request: Request, error: DomainError) -> JSONResponse:
        return problem(error.status_code, "Domain request failed", str(error), error.code)

    @app.exception_handler(HTTPException)
    async def http_problem(_request: Request, error: HTTPException) -> JSONResponse:
        return problem(error.status_code, "Request failed", error.detail, "request_failed")

    @app.exception_handler(RequestValidationError)
    async def validation_problem(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = []
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            message = str(item.get("msg") or "invalid value")
            details.append(f"{location}: {message}" if location else message)
        return problem(422, "Request validation failed", "; ".join(details), "validation_failed")

    @app.exception_handler(Exception)
    async def unhandled_problem(request: Request, error: Exception) -> JSONResponse:
        from llm_d_lens.auth.context import current_audit_context  # noqa: PLC0415

        context = current_audit_context()
        request_id = context.request_id if context is not None else uuid.uuid4().hex
        logger.exception(
            "unhandled_error request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )
        return problem(
            500,
            "Internal server error",
            "An unexpected error occurred.",
            "internal_error",
            extra={"requestId": request_id},
        )
