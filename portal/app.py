# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory for the Virex portal.

Phase E scope: only the `/healthz` and edge endpoints (edge/config,
edge/heartbeat, internal/events/{id}/clip). Other endpoints (auth,
cameras CRUD, events list) are added in later phases.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from api.v1.endpoints import edge

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Virex Portal",
        version="0.1.0",
        description=(
            "B2B multi-tenant CCTV monitoring control plane.\n\n"
            "Phase E (this revision): only `/api/edge/*` + `/internal/*` "
            "+ `/healthz` endpoints are implemented. Authentication, "
            "tenant CRUD, cameras mgmt UI, and event listing come in later phases."
        ),
    )
    app.include_router(edge.router)
    app.add_api_route(
        "/healthz",
        lambda: {"status": "ok"},
        methods=["GET"],
        tags=["meta"],
        summary="Liveness probe.",
    )
    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
