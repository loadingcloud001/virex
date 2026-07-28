# SPDX-License-Identifier: Apache-2.0
"""Jinja2 template helpers + UI routes (Phase 1).

Phase 1 UI surface:
- GET /login, POST /login  (form-based, sets session cookie)
- POST /logout              (clears session cookie)
- GET /cameras              (list)
- GET /cameras/new          (form)
- POST /cameras/new         (create)
- GET /cameras/{id}/edit    (form)
- POST /cameras/{id}/edit   (update)
- POST /cameras/{id}/delete (delete)
- GET /admin/seed           (one-time bootstrap)

All non-exempt UI routes require an admin session.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.deps import require_admin_session
from api.v1.endpoints.cameras import (
    CameraCreate,
    CameraOut,
    CameraUpdate,
)
from core.config import settings
from core.database import get_db
from core.security import UI_SESSION_COOKIE, UI_SESSION_TTL_SEC, create_session_token
from models import Camera, User

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Jinja2 environment — single shared instance
# ---------------------------------------------------------------------------
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# Cookie name used for the DaisyUI theme. The value must be a valid DaisyUI
# v5 theme name. Restricting to the two we ship (corporate / business) keeps
# the data-theme attribute tightly bound to our component definitions.
_THEME_COOKIE = "virex_theme"
_VALID_THEMES = frozenset({"corporate", "business"})


def _resolve_theme(request: Request) -> str:
    """Read the theme from the cookie. Falls back to 'corporate'."""
    cookie_value = request.cookies.get(_THEME_COOKIE, "corporate")
    return cookie_value if cookie_value in _VALID_THEMES else "corporate"


def _render(
    request: Request,
    template_name: str,
    **context: object,
) -> HTMLResponse:
    """Render a Jinja2 template with the standard layout context."""
    user = getattr(request.state, "user", None)
    ctx: dict[str, object] = {
        "user": user,
        "tenant_slug": getattr(request.state, "tenant_slug", settings.default_tenant_slug),
        "tenant_id": getattr(request.state, "tenant_id", None),
        "theme": _resolve_theme(request),
        "flash": None,
        "flash_ok": False,
    }
    ctx.update(context)
    html = _env.get_template(template_name).render(**ctx)
    return HTMLResponse(html)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=status.HTTP_303_SEE_OTHER)


def _current_user_or_none(request: Request, db: AsyncSession) -> User | None:
    """Helper for UI: decode the session cookie if present and load the user.

    UI routes that need auth use `require_admin_session` directly; this
    helper is for layout-level "are we logged in?" rendering only.
    """
    from core.security import decode_jwt
    from jwt import PyJWTError

    cookie = request.cookies.get(UI_SESSION_COOKIE)
    if not cookie:
        return None
    try:
        claims = decode_jwt(cookie, secret=settings.session_secret)
    except PyJWTError:
        return None
    if claims.get("kind") != "ui_session":
        return None
    # NB: this is sync but we don't actually need the user object for layout
    # — we only need to know they exist. The full lookup happens in
    # require_admin_session when a protected route is hit.
    return None  # placeholder; the layout just checks the cookie presence.


def _ui_user_present(request: Request) -> bool:
    return request.cookies.get(UI_SESSION_COOKIE) is not None


# ---------------------------------------------------------------------------
# UI router
# ---------------------------------------------------------------------------
ui_router = APIRouter(tags=["ui"])


@ui_router.get("/login", response_class=HTMLResponse)
async def ui_login_get(request: Request) -> HTMLResponse:
    return _render(request, "login.html")


@ui_router.post("/login", response_class=HTMLResponse)
async def ui_login_post(
    request: Request,
    email: str = Form(...),  # noqa: B008
    password: str = Form(...),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> HTMLResponse:
    """Form-encoded login. Sets the session cookie on success."""
    from core.security import verify_password

    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_slug = getattr(request.state, "tenant_slug", settings.default_tenant_slug)
    if tenant_id is None:
        # Try default tenant (only if not seeded yet).
        from models import Tenant
        t = (
            await db.execute(
                select(Tenant).where(Tenant.slug == settings.default_tenant_slug)
            )
        ).scalar_one_or_none()
        if t is None:
            return _render(
                request,
                "login.html",
                flash="Portal not seeded — run python -m portal.seed first.",
            )
        tenant_id = t.id
        tenant_slug = t.slug

    user = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _render(
            request,
            "login.html",
            flash="Invalid email or password.",
            tenant_slug=tenant_slug,
        )

    token = create_session_token(
        session_secret=settings.session_secret,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )
    resp = _redirect("/cameras")
    resp.set_cookie(
        key=UI_SESSION_COOKIE,
        value=token,
        max_age=UI_SESSION_TTL_SEC,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return resp


@ui_router.post("/logout")
async def ui_logout() -> RedirectResponse:
    resp = _redirect("/login")
    resp.delete_cookie(UI_SESSION_COOKIE, path="/")
    return resp


@ui_router.get("/cameras", response_class=HTMLResponse)
async def ui_cameras_list(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    tenant_id = user.tenant_id
    rows = (
        await db.execute(
            select(Camera)
            .where(Camera.tenant_id == tenant_id)
            .order_by(Camera.id)
        )
    ).scalars().all()
    cameras = [
        CameraOut(
            id=c.id,
            tenant_id=c.tenant_id,
            node_id=c.node_id,
            name=c.name,
            location=c.location,
            rtsp_url=c.rtsp_url,
            mtx_path=c.mtx_path,
            status=c.status,
            recording_enabled=c.recording_enabled,
            retention_days=c.retention_days,
        )
        for c in rows
    ]
    return _render(request, "cameras/list.html", cameras=cameras)


@ui_router.get("/cameras/new", response_class=HTMLResponse)
async def ui_cameras_new_get(
    request: Request,
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    return _render(request, "cameras/form.html", camera=None)


@ui_router.post("/cameras/new")
async def ui_cameras_new_post(
    request: Request,
    name: str = Form(...),  # noqa: B008
    mtx_path: str = Form(...),  # noqa: B008
    rtsp_url: str = Form(...),  # noqa: B008
    location: str = Form(""),  # noqa: B008
    node_id: int = Form(1),  # noqa: B008
    retention_days: int = Form(7),  # noqa: B008
    recording_enabled: str = Form("true"),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> RedirectResponse:
    """Create camera via form. Validates via the same DTO as the API."""
    try:
        body = CameraCreate(
            name=name,
            mtx_path=mtx_path,
            rtsp_url=rtsp_url,
            location=location or None,
            node_id=node_id,
            recording_enabled=recording_enabled.lower() == "true",
            retention_days=retention_days,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    existing = (
        await db.execute(select(Camera).where(Camera.mtx_path == body.mtx_path))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"mtx_path {body.mtx_path} already exists")

    cam = Camera(
        tenant_id=user.tenant_id,
        node_id=body.node_id,
        name=body.name,
        location=body.location,
        rtsp_url=body.rtsp_url,
        mtx_path=body.mtx_path,
        status="active",
        recording_enabled=body.recording_enabled,
        retention_days=body.retention_days,
    )
    db.add(cam)
    if body.node_id:
        from sqlalchemy import update as _update
        from models import Node as _Node
        await db.execute(
            _update(_Node)
            .where(_Node.id == body.node_id)
            .values(current_config_version=_Node.current_config_version + 1)
        )
    await db.commit()
    return _redirect("/cameras")


@ui_router.get("/cameras/{camera_id}/edit", response_class=HTMLResponse)
async def ui_cameras_edit_get(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return _render(request, "cameras/form.html", camera=cam)


@ui_router.post("/cameras/{camera_id}/edit")
async def ui_cameras_edit_post(
    camera_id: int,
    name: str = Form(...),  # noqa: B008
    mtx_path: str = Form(...),  # noqa: B008
    rtsp_url: str = Form(...),  # noqa: B008
    location: str = Form(""),  # noqa: B008
    node_id: int = Form(1),  # noqa: B008
    retention_days: int = Form(7),  # noqa: B008
    recording_enabled: str = Form("true"),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> RedirectResponse:
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    cam.name = name
    cam.mtx_path = mtx_path
    cam.rtsp_url = rtsp_url
    cam.location = location or None
    cam.node_id = node_id
    cam.retention_days = retention_days
    cam.recording_enabled = recording_enabled.lower() == "true"
    if cam.node_id:
        from sqlalchemy import update as _update
        from models import Node as _Node
        await db.execute(
            _update(_Node)
            .where(_Node.id == cam.node_id)
            .values(current_config_version=_Node.current_config_version + 1)
        )
    await db.commit()
    return _redirect("/cameras")


@ui_router.post("/cameras/{camera_id}/delete")
async def ui_cameras_delete(
    camera_id: int,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> RedirectResponse:
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    node_id = cam.node_id
    cam.status = "deleted"
    if node_id:
        from sqlalchemy import update as _update
        from models import Node as _Node
        await db.execute(
            _update(_Node)
            .where(_Node.id == node_id)
            .values(current_config_version=_Node.current_config_version + 1)
        )
    await db.commit()
    return _redirect("/cameras")


__all__: tuple[str, ...] = ("ui_router",)