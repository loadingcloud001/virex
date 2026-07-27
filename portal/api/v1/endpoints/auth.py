# SPDX-License-Identifier: Apache-2.0
"""Auth endpoints — login, logout, register, me.

Phase 1:
- /api/auth/login (POST, form-encoded): validates credentials, sets
  `virex_session` HttpOnly cookie, returns 200 with the user info.
- /api/auth/logout (POST): clears the cookie. Idempotent.
- /api/auth/me (GET): returns the current user (requires session).
- /api/auth/register (POST, admin-only): creates a new user under
  the same tenant. Phase 1 only admins can create users.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.deps import require_admin_session
from core.config import settings
from core.database import get_db
from core.security import (
    UI_SESSION_COOKIE,
    UI_SESSION_TTL_SEC,
    create_session_token,
    hash_password,
    verify_password,
)
from models import Tenant, User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    role: str
    tenant_id: int
    tenant_slug: str
    last_login_at: datetime | None


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=512)
    role: str = Field(default="admin", pattern="^(admin|member|viewer)$")


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------
@router.post(
    "/login",
    response_model=UserOut,
    summary="Validate credentials + set session cookie.",
)
async def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserOut:
    """Validate credentials and set the `virex_session` cookie.

    Cross-tenant guard: the user must belong to the tenant resolved
    from the Host header (set by TenantMiddleware).
    """
    request_tenant_id = getattr(request.state, "tenant_id", None)
    request_tenant_slug = getattr(request.state, "tenant_slug", None)

    # If the tenant middleware couldn't resolve (no seed yet), we
    # allow login only against the default-tenant admin.
    if request_tenant_id is None:
        # Try to look up the default tenant anyway.
        stmt = select(Tenant).where(Tenant.slug == settings.default_tenant_slug)
        tenant = (await db.execute(stmt)).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="portal not seeded; run python -m portal.seed first",
            )
        request_tenant_id = tenant.id
        request_tenant_slug = tenant.slug

    stmt = select(User).where(
        User.tenant_id == request_tenant_id,
        User.email == body.email,
    )
    user = (await db.execute(stmt)).scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        logger.warning("auth_login_failed", email=body.email, tenant_id=request_tenant_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    # Issue session JWT and set cookie.
    token = create_session_token(
        session_secret=settings.session_secret,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value=token,
        max_age=UI_SESSION_TTL_SEC,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )

    user.last_login_at = datetime.utcnow()
    await db.commit()

    logger.info("auth_login_ok", user_id=user.id, tenant_id=user.tenant_id)

    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=request_tenant_slug or "",
        last_login_at=user.last_login_at,
    )


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------
@router.post(
    "/logout",
    summary="Clear session cookie.",
)
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(UI_SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=UserOut,
    summary="Current user info (requires session).",
)
async def me(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> UserOut:
    """Return the current user + tenant info from the session."""
    tenant_slug = getattr(request.state, "tenant_slug", "")
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=tenant_slug or "",
        last_login_at=user.last_login_at,
    )


# ---------------------------------------------------------------------------
# /register (admin-only; creates another user under the same tenant)
# ---------------------------------------------------------------------------
@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user under the current tenant (admin-only).",
)
async def register_user(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    admin: User = Depends(require_admin_session),  # noqa: B008
) -> UserOut:
    """Admin-only endpoint to provision additional users within the tenant.

    Phase 2: invite-by-email flow with email verification. For now,
    just returns the new user record.
    """
    request_tenant_id = admin.tenant_id

    # Check for existing email within tenant.
    existing = (
        await db.execute(
            select(User).where(
                User.tenant_id == request_tenant_id,
                User.email == body.email,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already exists in this tenant",
        )

    new_user = User(
        tenant_id=request_tenant_id,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    logger.info(
        "auth_user_created",
        new_user_id=new_user.id,
        tenant_id=request_tenant_id,
        role=body.role,
        by_user_id=admin.id,
    )

    return UserOut(
        id=new_user.id,
        email=new_user.email,
        role=new_user.role,
        tenant_id=new_user.tenant_id,
        tenant_slug=getattr(request.state, "tenant_slug", "") or "",
        last_login_at=None,
    )


__all__: tuple[str, ...] = ("router",)