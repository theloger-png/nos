"""NOS REST API — FastAPI application and uvicorn entrypoint."""
from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from nos.api.routers import config, interfaces, routing, system, vlans

logger = logging.getLogger(__name__)

_HOST = os.environ.get("NOS_API_HOST", "127.0.0.1")
_PORT = int(os.environ.get("NOS_API_PORT", "8080"))

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Create and return the configured FastAPI application."""
    app = FastAPI(
        title="NOS REST API",
        description="Network Operating System — REST API for automation and integration",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    _register_routers(app)
    _register_exception_handlers(app)

    return app


def _register_routers(app: FastAPI) -> None:
    prefix = "/api/v1"
    app.include_router(interfaces.router, prefix=prefix)
    app.include_router(vlans.router, prefix=prefix)
    app.include_router(routing.router, prefix=prefix)
    app.include_router(system.router, prefix=prefix)
    app.include_router(config.router, prefix=prefix)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def generic_exception_handler(request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

app = create_app()


def main() -> None:
    """Start the uvicorn server."""
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "nos.api.app:app",
        host=_HOST,
        port=_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
