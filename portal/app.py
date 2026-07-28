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

Frontend stack (Phase 1):
- HTMX v2 + Alpine.js v3 (CDN-loaded for now, see plan)
- Tailwind CSS v4 + DaisyUI v5 for components & theming
- Server-rendered hypermedia; no Node.js build step
"""

from __future__ import annotations

import structlog
from pathlib import Path as _Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from api.v1.endpoints import api_router
from middleware.tenant import install_tenant_middleware
from templates import ui_router

logger = structlog.get_logger(__name__)


# Security headers — kept here (not in a separate middleware module) because
# they're tied to the frontend stack decisions in base.html. The CSP and
# Permissions-Policy values are referenced from the same plan that picked
# HTMX + Alpine + DaisyUI; loosening/tightening them should be a single-file
# change.
_CSP_DIRECTIVES: dict[str, str] = {
    "default-src": "'self'",
    "script-src": "'self' cdn.jsdelivr.net 'unsafe-inline'",
    # 'unsafe-inline' is required for Alpine's @click / x-data attribute
    # eval; the alternative (per-event CSP nonces) is out of scope.
    "style-src": "'self' cdn.jsdelivr.net 'unsafe-inline'",
    # DaisyUI ships a small inline <style> for theme vars; tightening this
    # requires migrating to a hashed-CSS build, which is Phase 3.
    "font-src": "'self' data:",
    "img-src": "'self' data: blob:",
    "frame-ancestors": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
}


def _build_csp_header() -> str:
    return "; ".join(f"{k} {v}" for k, v in _CSP_DIRECTIVES.items())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach permissive-but-pinned security headers to every response.

    Notes:
    - We don't set HSTS here: in dev the portal is HTTP-only. The CF Tunnel
      adds HSTS at the edge in prod.
    - We DO set CSP, Permissions-Policy, X-Content-Type-Options, and
      Referrer-Policy on every response, including /docs and /openapi.json,
      to make accidental info disclosure louder.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _build_csp_header())
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response


def install_security_headers(app) -> None:
    """Register the security-headers middleware.

    FastAPI applies middleware in reverse-declaration order: install LAST so
    SecurityHeadersMiddleware is the OUTERMOST wrapper. (The tenant
    middleware is also installed last in `install_tenant_middleware`, but
    they cooperate — tenant sets ``request.state``, security headers wrap the
    response, which is what we want.)
    """
    app.add_middleware(SecurityHeadersMiddleware)


def create_app() -> FastAPI:
    from fastapi import FastAPI

    app = FastAPI(
        title="Virex Portal",
        version="0.1.0",
        description=(
            "B2B multi-tenant CCTV monitoring control plane.\n\n"
            "Phase 1 ships:\n"
            "- Auth (UI session cookies + edge bearer JWTs)\n"
            "- Cameras CRUD (tenant-scoped)\n"
            "- Edge registration + config pull + heartbeat\n"
            "- Login / Cameras UI (Jinja2 + DaisyUI components)\n"
        ),
    )

    # Middleware order: tenant resolution must run BEFORE everything
    # else so that auth deps can read request.state.tenant_id. FastAPI
    # applies middleware in reverse-declaration order, so we add it last
    # to make it the outermost wrapper.
    app.include_router(api_router)
    app.include_router(ui_router)
    _static_dir = _Path(__file__).resolve().parent / "static"
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    install_tenant_middleware(app)
    install_security_headers(app)

    @app.get("/healthz", tags=["meta"], summary="Liveness probe.")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
