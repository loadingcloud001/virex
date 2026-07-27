# SPDX-License-Identifier: Apache-2.0
"""Tenant resolution middleware.

Maps the inbound HTTP `Host` header to a tenant, attaching
`request.state.tenant_id` (and `tenant_slug`) so downstream endpoints
can filter by tenant without re-parsing the host.

Modes:
- **Production**: `{slug}.portal.{root_domain}` → look up `Tenant.slug`.
  Anything else returns 404 before any business logic runs.
- **Local dev**: `Host` is `127.0.0.1`, `localhost`, or empty. We fall
  back to `settings.default_tenant_slug` so the operator can hit
  `http://127.0.0.1:8000/cameras` without DNS gymnastics. This is the
  single-tenant pilot mode; multi-tenant subdomains are only exercised
  in prod.

Exempt paths: `/healthz`, `/static/*`, `/docs`, `/openapi.json`,
`/redoc` — these are always reachable regardless of host.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from core.config import settings
from core.database import SessionLocal
from models import Tenant

logger = structlog.get_logger(__name__)


# Subdomain pattern: {slug}.portal.{anything}. Anything else is dev.
_PROD_HOST_RE: Final[str] = r"^(?P<slug>[a-z0-9-]+)\.portal\..+$"


# Endpoints that are not tenant-scoped. Anything matching these
# prefixes is exempt from the Host-header lookup.
_EXEMPT_PREFIXES: Final[tuple[str, ...]] = (
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/static/",
)


def _is_exempt(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in _EXEMPT_PREFIXES)


async def _resolve_tenant_id(slug: str) -> int | None:
    """Look up tenant id by slug. Returns None if not found."""
    async with SessionLocal() as session:
        stmt = select(Tenant.id).where(Tenant.slug == slug)
        row = (await session.execute(stmt)).scalar_one_or_none()
        return row


class TenantMiddleware(BaseHTTPMiddleware):
    """Parse Host header → resolve tenant → attach to request.state.

    Exempt paths skip resolution entirely.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):  # type: ignore[override]
        path = request.url.path
        if _is_exempt(path):
            return await call_next(request)

        host = request.headers.get("host", "")
        # Strip port if present.
        host_no_port = host.split(":", 1)[0].lower()

        # Production subdomain routing.
        match = re.match(_PROD_HOST_RE, host_no_port)
        if match is not None:
            slug = match.group("slug")
            tenant_id = await _resolve_tenant_id(slug)
            if tenant_id is None:
                logger.warning("tenant_unknown_host", host=host, slug=slug)
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"unknown tenant slug '{slug}'"},
                )
            request.state.tenant_slug = slug
            request.state.tenant_id = tenant_id
            request.state.tenant_resolved_via = "host_subdomain"
            return await call_next(request)

        # Local dev / single-tenant fallback.
        # Accept: localhost / 127.0.0.1 / empty / 0.0.0.0 / a host that
        # doesn't match the prod subdomain pattern (test runners use
        # host="test" for instance, and bare IPs in development).
        is_dev_host = (
            host_no_port == ""
            or host_no_port in {"127.0.0.1", "localhost", "0.0.0.0"}
            or re.match(_PROD_HOST_RE, host_no_port) is None
        )
        if is_dev_host:
            slug = settings.default_tenant_slug
            tenant_id = await _resolve_tenant_id(slug)
            if tenant_id is None:
                # No seed has been run yet — let the request continue with
                # tenant_id=None so the operator can hit /admin/seed.
                request.state.tenant_slug = slug
                request.state.tenant_id = None
                request.state.tenant_resolved_via = "dev_unseeded"
                logger.warning("tenant_dev_unseeded", host=host)
                return await call_next(request)
            request.state.tenant_slug = slug
            request.state.tenant_id = tenant_id
            request.state.tenant_resolved_via = "dev_default"
            return await call_next(request)

        # Shouldn't reach here — the prod-subdomain regex doesn't match
        # anything else. Defensive fallback.
        logger.warning("tenant_unknown_host", host=host)
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown host '{host}'"},
        )


def install_tenant_middleware(app: FastAPI) -> None:
    """Install the tenant middleware on a FastAPI app."""
    app.add_middleware(TenantMiddleware)


__all__: tuple[str, ...] = ("TenantMiddleware", "install_tenant_middleware")