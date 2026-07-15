"""Standalone entrypoint for the isolated Legal Operations Sandbox.

Run with:
    LEGAL_OPS_SANDBOX_ENABLED=true LEGAL_OPS_SANDBOX_TOKEN=... \
      uvicorn app.legal_ops.dev_app:app --port 8765

This entrypoint intentionally does not import the production database or bot stack.
"""

from fastapi import FastAPI

from app.config import get_settings
from app.legal_ops.api import build_runtime, router


def create_legal_ops_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Legal Operations Middle Platform Sandbox",
        version="0.1.0-phase0",
    )
    app.state.legal_ops_runtime = build_runtime(settings)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "sandbox_enabled": bool(app.state.legal_ops_runtime.enabled),
        }

    return app


app = create_legal_ops_app()
