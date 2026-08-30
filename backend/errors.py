"""Application errors and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .metrics import record_element_creation_failure


class APIError(Exception):
    """An error that is safe to expose as the contract's error response."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


def _validation_message(exc: RequestValidationError) -> str:
    errors: list[dict[str, Any]] = exc.errors()
    if not errors:
        return "Request validation failed."
    first = errors[0]
    message = str(first.get("msg", "Request validation failed."))
    location = first.get("loc", ())
    field = next((str(part) for part in location if part not in {"body", "query", "path"}), None)
    if field and message:
        return f"{field}: {message}"
    return message


def _is_canvas_write(request: Request) -> bool:
    """Was this the request that saves a canvas document?

    Counting rejected canvas writes here catches every layer that can refuse
    one, the request body validation FastAPI does before the route runs
    included, without a try block around each handler.
    """
    return request.method == "POST" and request.url.path.endswith("/canvas")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        if _is_canvas_write(request):
            record_element_creation_failure(exc.code)
        return _error_response(exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if _is_canvas_write(request):
            record_element_creation_failure("validation_error")
        if any(
            "token" in error.get("loc", ())
            for error in exc.errors()
        ):
            return _error_response(
                "token_invalid",
                "This link is not valid. Ask your interviewer for a new one.",
                404,
            )
        return _error_response("validation_error", _validation_message(exc), 400)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error_response("not_found", "The requested resource was not found.", 404)
        if exc.status_code == 405:
            return _error_response("method_not_allowed", "The requested method is not allowed.", 405)
        return _error_response("http_error", str(exc.detail), exc.status_code)
