"""FastAPI application entry point."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .errors import register_exception_handlers
from .routers import collaboration, guest, sessions
from .routers import auth as auth_router
from .routers import canvas, review
from .store import InMemoryStore


FRONTEND_DIRECTORY = Path(__file__).resolve().parents[1] / "frontend"


def create_app(store: InMemoryStore | None = None) -> FastAPI:
    application = FastAPI(
        title="Interview Share Canvas API",
        version="1.0.0",
        description="Backend for collaborative system-design interviews.",
    )
    application.state.store = store if store is not None else InMemoryStore()

    configured_origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:8091,http://127.0.0.1:8091",
    )
    origins = [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Session-Token"],
    )

    register_exception_handlers(application)
    application.include_router(auth_router.router)
    application.include_router(sessions.router)
    application.include_router(guest.router)
    application.include_router(canvas.router)
    application.include_router(review.router)
    application.include_router(collaboration.router)

    @application.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/", include_in_schema=False)
    def frontend_index() -> RedirectResponse:
        return RedirectResponse(url="/interview-platform.dc.html")

    @application.get("/join/{token}", include_in_schema=False)
    def guest_frontend(token: str) -> FileResponse:
        """Serve the candidate lobby for links created by the API."""
        return FileResponse(FRONTEND_DIRECTORY / "interview-platform.dc.html")

    application.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIRECTORY),
        name="frontend",
    )

    return application


app = create_app()
store: InMemoryStore = app.state.store
