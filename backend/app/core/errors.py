"""Application error tree + FastAPI exception handlers.

The error tree mirrors ``API_CONTRACT.md`` §1.2 error codes one-to-one.
The handlers serialise each error into the envelope defined in
``API_CONTRACT.md`` §1.1.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class ApplicationError(Exception):
    """Base for all application-layer errors.

    Subclasses set ``code`` (API_CONTRACT §1.2) and ``http_status``.
    ``details`` is serialised into the envelope unchanged.
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(ApplicationError):
    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class ConflictError(ApplicationError):
    code = "CONFLICT"
    http_status = status.HTTP_409_CONFLICT


class UnauthorizedError(ApplicationError):
    code = "UNAUTHENTICATED"
    http_status = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(ApplicationError):
    code = "FORBIDDEN"
    http_status = status.HTTP_403_FORBIDDEN


class ValidationFailedError(ApplicationError):
    code = "VALIDATION_FAILED"
    # Starlette 1.x renamed this constant to match RFC 9110 wording
    # ("Unprocessable Content"). The HTTP status code is still 422.
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT


def _envelope(code: str, message: str, details: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
        }
    }


async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    logger.warning(
        "application_error",
        code=exc.code,
        message=exc.message,
        details=exc.details,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=_envelope(exc.code, exc.message, exc.details, request_id),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=_envelope(
            "VALIDATION_FAILED",
            "request validation failed",
            {"errors": exc.errors()},
            request_id,
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    logger.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope("INTERNAL_ERROR", "an internal error occurred", {}, request_id),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the three handlers onto a FastAPI app."""
    # mypy: the FastAPI typing for handler params is `Exception`; our
    # subclass-specific signatures are stricter. Standard ignore comment.
    app.add_exception_handler(ApplicationError, application_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
