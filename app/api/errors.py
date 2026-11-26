"""Global exception handler: safe error responses with request_id."""

from typing import Any, cast

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException handler: wrap in our error format."""
    request_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": exc.detail,
                "request_id": request_id,
            }
        },
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError | ValidationError
) -> JSONResponse:
    """Validation errors (422) keep field name and message; never the input."""
    request_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")
    errors = []
    for error in exc.errors():
        errors.append({
            "field": ".".join(str(x) for x in error["loc"][1:]),
            "message": error["msg"],
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Validation failed",
                "request_id": request_id,
                "details": errors,
            }
        },
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Generic exception handler: never leak internals."""
    request_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")

    # Generic 500 error: safe message
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An internal error occurred",
                "request_id": request_id,
            }
        },
    )


def add_exception_handlers(app: FastAPI) -> None:
    """Register exception handlers on the FastAPI app."""
    app.add_exception_handler(HTTPException, cast(Any, http_exception_handler))
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error_handler))
    app.add_exception_handler(ValidationError, cast(Any, validation_error_handler))
    app.add_exception_handler(Exception, generic_exception_handler)
