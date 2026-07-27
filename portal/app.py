# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory for the Virex portal.

Phase 1 scope:
- `/api/auth/*` — login / logout / me / register (UI sessions)
- `/api/cameras/*` — tenant-scoped camera CRUD
- `/api/edge/nodes/register` — first-time edge registration
- `/api/edge/nodes/{id}/rotate` — JWT rotation
- `/api/edge/config` — bundle (JWT)
- `/api/edge/heartbeat` — health telemetry (JWT)
- `/internal/events/{id}/clip` — clip-builder callback (JWT)
- `/login`, `/logout`, `/cameras`, `/cameras/{new,id/edit,id/delete}` — UI
- `/healthz`, `/docs`, `/redoc`, `/openapi.json` — meta
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from api.v1.endpoints import api_router
from middleware.tenant import install_tenant_middleware
from templates import ui_router

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Virex Portal",
        version="0.1.0",
        description=(
            "B2B multi-tenant CCTV monitoring control plane.\n\n"
            "Phase 1 ships:\n"
            "- Auth (UI session cookies + edge bearer JWTs)\n"
            "- Cameras CRUD (tenant-scoped)\n"
            "- Edge registration + config pull + heartbeat\n"
            "- Login / Cameras UI (Jinja2)\n"
        ),
    )

    # Middleware order: tenant resolution must run BEFORE everything
    # else so that auth deps can read request.state.tenant_id. FastAPI
    # applies middleware in reverse-declaration order, so we add it last
    # to make it the outermost wrapper.
    app.include_router(api_router)
    app.include_router(ui_router)
    install_tenant_middleware(app)

    @app.get("/healthz", tags=["meta"], summary="Liveness probe.")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")