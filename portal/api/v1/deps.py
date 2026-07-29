# SPDX-License-Identifier: Apache-2.0
"""FastAPI dependencies for the portal.

Two flavours of auth:

- `require_admin_session`: for UI routes. Reads `virex_session` cookie,
  decodes it via `core.security.decode_jwt`, looks up the user,
  attaches `request.state.user` (a User row) and ensures role=admin.

- `require_edge_jwt`: for service-to-service calls from edge-agent,
  clip-builder. Reads `Authorization: Bearer <jwt>`, decodes via the
  portal's JWT secret, looks up the matching `Node` row, attaches
  `request.state.node` and the resolved `tenant_id`.

Both depend on `request.state.tenant_id` having been set by
`TenantMiddleware` earlier in the chain.

`tenant_id_dep` is a cheap FastAPI dependency that raises 400 if the
TenantMiddleware did not populate `request.state.tenant_id`. Prefer
this over `getattr(request.state, "tenant_id", None)` in endpoints.
"""

from __future__ import annotations

from datetime import datetime

import jwt
import structlog
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jwt import PyJWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from core.security import (
    UI_SESSION_COOKIE,
    decode_jwt,
    verify_password,
)
from models import Node, User

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# UI session (cookie)
# ---------------------------------------------------------------------------
async def require_admin_session(
    request: Request,
    virex_session: str | None = Cookie(default=None),  # noqa: B008
    db: AsyncSession = Depends(get_db),
) -> User:
    """Verify the UI session cookie and return the active User.

    Raises 401 if cookie missing/invalid, 403 if user inactive, 403 if
    role != admin (Phase 1 only admin has any privileges).
    """
    if not virex_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"Location": "/login"},
        )

    try:
        claims = decode_jwt(virex_session, secret=settings.session_secret)
    except PyJWTError as e:
        logger.warning("session_jwt_invalid", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session expired",
            headers={"Location": "/login"},
        ) from e

    if claims.get("kind") != "ui_session":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not a session token",
        )

    user_id = int(claims["sub"])
    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )

    # Cross-tenant guard: the user's tenant must match the resolved
    # request tenant (set by TenantMiddleware).
    request_tenant_id = getattr(request.state, "tenant_id", None)
    if request_tenant_id is not None and user.tenant_id != request_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-tenant access denied",
        )

    request.state.user = user
    return user


# ---------------------------------------------------------------------------
# Edge JWT (bearer)
# ---------------------------------------------------------------------------
async def require_edge_jwt(
    request: Request,
    authorization: str | None = Header(default=None),  # noqa: B008
    db: AsyncSession = Depends(get_db),
) -> Node:
    """Verify the JWT bearer token and return the matching Node.

    Raises 401 on missing / invalid / expired JWT, 404 on unknown node.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )

    try:
        claims = decode_jwt(token, secret=settings.jwt_secret)
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"X-Token-Expired": "1"},
        ) from e
    except PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        ) from e

    if claims.get("kind") != "edge_token":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not an edge token",
        )

    node_id = int(claims["sub"])
    stmt = select(Node).where(Node.id == node_id)
    node = (await db.execute(stmt)).scalar_one_or_none()
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="node not found",
        )

    # Cross-tenant guard: the JWT's tid must match the request tenant.
    request_tenant_id = getattr(request.state, "tenant_id", None)
    jwt_tenant_id = claims.get("tid")
    if (
        request_tenant_id is not None
        and jwt_tenant_id is not None
        and int(jwt_tenant_id) != request_tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token tenant mismatch",
        )

    # Touch last_heartbeat as a side-effect-free signal that the node
    # is currently active. The /api/edge/heartbeat endpoint is still
    # the canonical place for full health telemetry.
    node.last_heartbeat_at = datetime.utcnow()
    await db.commit()

    request.state.node = node
    request.state.node_tenant_id = node.tenant_id
    return node


# ---------------------------------------------------------------------------
# Optional: edge registration via shared bootstrap secret
# ---------------------------------------------------------------------------
async def require_edge_bootstrap_secret(
    authorization: str | None = Header(default=None),  # noqa: B008
) -> None:
    """Verify the shared bootstrap secret for /api/edge/nodes/register.

    Used only at first registration — once the edge has its JWT,
    subsequent calls use `require_edge_jwt`.
    """
    expected = f"Bearer {settings.edge_bootstrap_secret}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap secret",
        )


# ---------------------------------------------------------------------------
# Cheap tenant_id dependency
# ---------------------------------------------------------------------------
async def tenant_id_dep(request: Request) -> int:
    """Return the resolved `tenant_id` or raise 400.

    Use this in any endpoint that depends on TenantMiddleware having
    populated `request.state.tenant_id`. Replaces the silent-fallback
    `getattr(request.state, "tenant_id", None)` pattern, which masked
    misconfigured middleware.

    Returns:
        The current tenant_id (int).

    Raises:
        HTTPException(400): if the tenant middleware did not resolve a
            tenant. Indicates the Host header wasn't recognized AND the
            fallback to `default_tenant_slug` is disabled.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Tenant not resolved. The request reached an endpoint "
                "expecting a tenant context, but TenantMiddleware did not "
                "populate request.state.tenant_id. Check that the Host "
                "header is one of the configured tenant subdomains, or "
                "that the dev fallback to default_tenant_slug is enabled."
            ),
        )
    return int(tenant_id)


# Re-export verify_password for convenience in tests
__all__: tuple[str, ...] = (
    "require_admin_session",
    "require_edge_jwt",
    "require_edge_bootstrap_secret",
    "tenant_id_dep",
    "verify_password",
)


# Avoid unused-import warning.
_ = UI_SESSION_COOKIE