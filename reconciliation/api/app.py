"""
FastAPI application factory for the Concord reconciliation API.

Creates and configures the app with a SQLite-backed batch store. The
store is created once at startup and shared across all route handlers
via closure.

The API pipeline does not execute Layer 2 from arbitrary uploaded CSV
batches. Layer 2 execution state is recorded as ``not_executed`` in
responses so consumers never confuse it with real AI processing.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from reconciliation.api.routes import create_router
from reconciliation.api.store import BatchStore

logger = logging.getLogger("concord.api")


def create_app(db_path: Path | str = "data/concord.db") -> FastAPI:
    """Create and configure the Concord FastAPI application.

    Parameters
    ----------
    db_path : Path or str
        Path to the SQLite database file.  Parent directories are created
        automatically if they do not exist.
    """
    store = BatchStore(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="Concord",
        description="Intelligent settlement reconciliation API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unexpected exceptions that escape route handlers.

        Logs the full exception server-side and returns a safe, generic
        error to the client.  Never exposes stack traces, file paths,
        API keys, or other implementation details.
        """
        logger.exception(
            "Unhandled exception: %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "error_type": "internal_error",
            },
        )

    @app.get("/health")
    async def health():
        """Liveness probe: returns 200 when the API is running.

        This endpoint verifies the application process is alive and
        able to serve HTTP requests.  It does NOT check external
        dependencies such as the database, AI providers, or downstream
        services.
        """
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # CORS — configurable via CONCORD_CORS_ORIGINS environment variable.
    # ------------------------------------------------------------------
    # When the env var is unset or empty, no CORS middleware is added
    # (the safest default: cross-origin browser requests are blocked).
    #
    # Set CONCORD_CORS_ORIGINS="*" to allow all origins (development).
    # Set CONCORD_CORS_ORIGINS="https://app.example.com" for production.
    # Multiple origins: comma-separated,
    #   e.g. "https://app.example.com,https://admin.example.com"
    cors_origins_raw = os.environ.get("CONCORD_CORS_ORIGINS", "").strip()
    if cors_origins_raw:
        if cors_origins_raw == "*":
            allowed_origins = ["*"]
        else:
            allowed_origins = [
                o.strip() for o in cors_origins_raw.split(",") if o.strip()
            ]

        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    router = create_router(
        store,
        layer2_mode="not_executed",
    )
    app.include_router(router, prefix="/batches", tags=["batches"])

    return app
