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
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
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
from models import Camera, Event, User

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


# Phase 2: tiny relative-time filter ("5 min ago" / "in 2 days" / ISO date
# for >=7d). Used by the events list as a fallback when Alpine's
# `relativeTime()` JS helper isn't running (no-JS clients).
def _relative_time_filter(value: object) -> str:
    if not isinstance(value, (str, datetime)):
        return str(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    diff = int((now - value).total_seconds())
    if abs(diff) < 60:
        return "just now" if diff >= 0 else "in a moment"
    mins = abs(diff) // 60
    if mins < 60:
        return f"{mins} min ago" if diff >= 0 else f"in {mins} min"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago" if diff >= 0 else f"in {hrs}h"
    days = hrs // 24
    if days < 7:
        return f"{days}d ago" if diff >= 0 else f"in {days}d"
    return value.date().isoformat()


_env.filters["relative_time"] = _relative_time_filter


# Phase 2.5 polish: class-label severity colour. Maps YOLO/RT-DETR class
# names to DaisyUI badge colours. Unknown classes fall back to neutral.
_CLASS_BADGE_COLORS: dict[str, str] = {
    "person": "badge-info",
    "vehicle": "badge-warning",
    "car": "badge-warning",
    "truck": "badge-warning",
    "bus": "badge-warning",
    "motorcycle": "badge-warning",
    "bicycle": "badge-warning",
}


def _class_badge_filter(value: object, size: str = "badge-sm") -> str:
    """Jinja filter: render a class_label string as a styled DaisyUI badge.

    Output is mark-up-safe HTML; the template will need `| class_badge | safe`
    to render it. Returns a plain `<span class="badge ...">{label}</span>`.

    For unknown class labels we render a neutral `badge-ghost` so the UI
    stays consistent — operators always see a badge, not an em-dash.
    """
    label = str(value)
    color = _CLASS_BADGE_COLORS.get(label, "badge-ghost")
    # autoescape is on, so we re-build via Markup to avoid double-escaping.
    from markupsafe import Markup

    return Markup(f'<span class="badge {color} {size}">{label}</span>')


_env.filters["class_badge"] = _class_badge_filter


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
        "debug_login_helper": settings.debug_login_helper,
        "bootstrap_admin_email": settings.bootstrap_admin_email,
        "bootstrap_admin_password": settings.bootstrap_admin_password,
        "flash": None,
        "flash_ok": False,
    }
    ctx.update(context)
    html = _env.get_template(template_name).render(**ctx)
    return HTMLResponse(html)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=status.HTTP_303_SEE_OTHER)


def _decode_session_cookie(request: Request) -> dict | None:
    """Decode the UI session cookie and return its claims, or None.

    This is the single source of truth for "is this user logged in?" —
    used by `ui_root` (the redirect from `/`) and the layout-level
    rendering. It performs JWT decode + kind check + PyJWTError handling
    in one place so any future format change lives in one spot.

    Returns:
        - None if the cookie is missing, malformed, expired, or wrong kind.
        - The decoded claims dict otherwise (caller should validate
          sub / tid / role against the DB if it cares about currency).

    NB: This does NOT do a database lookup, so a stale JWT for a
    disabled/deleted user will still validate here. Endpoints that need
    a live `User` row should use the `require_admin_session` FastAPI
    dependency, which queries the DB.
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
    return claims


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
            # Fatal: portal not seeded. Return 503 so the operator
            # sees a clear server error in the browser rather than a
            # confusing "invalid credentials" alert.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Portal not seeded — run `python -m portal.seed` first.",
            )
        tenant_id = t.id
        tenant_slug = t.slug

    user = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        # HTMX path (request header HX-Request: true): swap the inline
        # error slot only (not the whole page) — keeps the DOM stable.
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                f'<div id="login-error">'
                f'<div role="alert" class="alert alert-error text-sm py-2">'
                f'<span>Invalid email or password.</span>'
                f'</div>'
                f'</div>'
            )
        # No-JS path: render the full login page with `flash` so the
        # message appears in the form on a native POST + 303 redirect.
        return _render(
            request,
            "login.html",
            flash="Invalid email or password.",
            tenant_slug=tenant_slug,
            email_value=email,
        )

    token = create_session_token(
        session_secret=settings.session_secret,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )
    resp = _redirect("/dashboard")
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


# ---------------------------------------------------------------------------
# Phase 2 UI routes
# ---------------------------------------------------------------------------
_WINDOW_TO_DELTA: dict[str, "datetime | None"] = {
    "1h": None,  # populated in handler via timedelta
    "6h": None,
    "24h": None,
    "7d": None,
    "all": None,
}


@ui_router.get("/events", response_class=HTMLResponse)
async def ui_events_list(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    """Event list page with toolbar + auto-refresh table."""
    from api.v1.endpoints.events import _resolve_window as _rw

    cutoff = _rw("24h")
    cam_rows = (
        await db.execute(
            select(Camera)
            .where(Camera.tenant_id == user.tenant_id)
            .order_by(Camera.id)
        )
    ).scalars().all()
    cameras_out = [
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
        for c in cam_rows
    ]

    # Initial table render: last 24h, all cameras, latest 50.
    # Use event_to_out so the template is decoupled from ORM attribute names.
    from services.events import event_to_out

    stmt = select(Event).where(Event.tenant_id == user.tenant_id)
    if cutoff is not None:
        stmt = stmt.where(Event.event_time >= cutoff)
    stmt = stmt.order_by(Event.event_time.desc()).limit(50)
    rows = (await db.execute(stmt)).scalars().all()
    events = [event_to_out(r) for r in rows]

    cam_name_map = {c.id: c.name for c in cam_rows}
    total = len(events)

    return _render(
        request,
        "events/list.html",
        events=events,
        cameras=cameras_out,
        cam_name_map=cam_name_map,
        total=total,
        now=datetime.now(timezone.utc),
    )


@ui_router.get("/cameras/{camera_id}", response_class=HTMLResponse)
async def ui_camera_detail(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    """Camera detail page with HLS player + recent events."""
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    base = settings.mediamtx_public_url.rstrip("/")
    hls_url = f"{base}/{cam.mtx_path}/index.m3u8"
    webrtc_url = f"{base.rsplit(':', 1)[0]}:8889/{cam.mtx_path}/whep"

    # Initial 50 recent events for the right rail.
    # Uses `event_to_out` (EventOut schema) so the template doesn't
    # depend on ORM attribute names.
    from services.events import event_to_out

    recent_rows = (
        await db.execute(
            select(Event)
            .where(
                Event.tenant_id == user.tenant_id,
                Event.camera_id == camera_id,
            )
            .order_by(Event.event_time.desc())
            .limit(50)
        )
    ).scalars().all()
    recent = [event_to_out(r) for r in recent_rows]

    camera_out = CameraOut(
        id=cam.id,
        tenant_id=cam.tenant_id,
        node_id=cam.node_id,
        name=cam.name,
        location=cam.location,
        rtsp_url=cam.rtsp_url,
        mtx_path=cam.mtx_path,
        status=cam.status,
        recording_enabled=cam.recording_enabled,
        retention_days=cam.retention_days,
    )
    return _render(
        request,
        "cameras/detail.html",
        camera=camera_out,
        hls_url=hls_url,
        webrtc_url=webrtc_url,
        recent_events=recent,
    )


@ui_router.get(
    "/cameras/{camera_id}/events/fragment",
    response_class=HTMLResponse,
    summary="HTMX fragment — recent events for the camera detail right rail.",
)
async def ui_camera_events_fragment(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    """Returns just the `_recent_events.html` partial so HTMX can swap it.

    Uses the public `EventOut` schema (not the ORM row) so the template
    is decoupled from SQLAlchemy attribute names.
    """
    from services.events import event_to_out

    tenant_id = getattr(request.state, "tenant_id", None) or user.tenant_id
    rows = (
        await db.execute(
            select(Event)
            .where(Event.tenant_id == tenant_id, Event.camera_id == camera_id)
            .order_by(Event.event_time.desc())
            .limit(50)
        )
    ).scalars().all()
    events_out = [event_to_out(r) for r in rows]
    html = _env.get_template("cameras/_recent_events.html").render(
        recent_events=events_out,
    )
    return HTMLResponse(html)


@ui_router.get("/alerts", response_class=HTMLResponse)
async def ui_alerts_coming_soon(
    request: Request,
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    """Placeholder for Phase 2.5 alert-rules CRUD."""
    return _render(request, "alerts/coming_soon.html")


# ---------------------------------------------------------------------------
# Home + dashboard
# ---------------------------------------------------------------------------
@ui_router.get("/")
async def ui_root(request: Request) -> RedirectResponse:
    """Redirect GET / to /dashboard if authed, /login otherwise.

    We deliberately avoid the FastAPI dep machinery here so an
    unauthenticated visitor sees a clean redirect rather than a 401
    with a Location: /login header (which would be ugly in a browser).

    Note: this performs JWT decode only (no DB lookup). A user with a
    valid JWT but deleted/disabled in the DB will land on /dashboard
    and immediately get a 401 from require_admin_session. Acceptable
    edge case for a top-level redirect.
    """
    if _decode_session_cookie(request) is not None:
        return _redirect("/dashboard")
    return _redirect("/login")


@ui_router.get("/dashboard", response_class=HTMLResponse)
async def ui_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> HTMLResponse:
    """Operator home: stat tiles + recent events + camera snapshot.

    Performance note: combines the four counting queries into TWO
    conditional-aggregate queries (`func.count()` + `func.sum(case(...))`)
    so the dashboard takes 4 round-trips to Postgres instead of 7.
    The rows (recent events + camera snapshot) keep their own queries.
    """
    from datetime import timedelta as _td
    from sqlalchemy import case, func as _func

    tenant_id = user.tenant_id
    today_cutoff = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    hb_cutoff = datetime.now(timezone.utc) - _td(minutes=5)

    # Query 1: camera counts in one round-trip.
    cam_row = (
        await db.execute(
            select(
                _func.count().label("total"),
                _func.sum(
                    case((Camera.status == "active", 1), else_=0)
                ).label("active"),
            ).where(Camera.tenant_id == tenant_id)
        )
    ).one()
    cam_count = int(cam_row.total or 0)
    active_cams = int(cam_row.active or 0)

    # Query 2: node counts in one round-trip.
    from models import Node as _Node
    node_row = (
        await db.execute(
            select(
                _func.count().label("total"),
                _func.sum(
                    case((_Node.last_heartbeat_at >= hb_cutoff, 1), else_=0)
                ).label("online"),
            ).where(_Node.tenant_id == tenant_id)
        )
    ).one()
    total_nodes = int(node_row.total or 0)
    nodes_online = int(node_row.online or 0)

    # Query 3: events today.
    events_today = (
        await db.execute(
            select(_func.count()).select_from(Event).where(
                Event.tenant_id == tenant_id,
                Event.event_time >= today_cutoff,
            )
        )
    ).scalar_one()

    # Query 4: recent events (last 10) — for the dashboard panel.
    # Uses EventOut so the template doesn't couple to ORM attribute names.
    from services.events import event_to_out
    recent_rows = (
        await db.execute(
            select(Event)
            .where(Event.tenant_id == tenant_id)
            .order_by(Event.event_time.desc())
            .limit(10)
        )
    ).scalars().all()

    # Query 5: cameras by status — for the camera snapshot panel.
    cams_by_status = (
        await db.execute(
            select(Camera)
            .where(Camera.tenant_id == tenant_id)
            .order_by(Camera.id)
            .limit(8)
        )
    ).scalars().all()

    cam_name_map = {c.id: c.name for c in cams_by_status}
    recent_events = [event_to_out(r) for r in recent_rows]

    # Render-time context: use a typed schema (UserOut) for `user` so
    # the template is decoupled from the SQLAlchemy User model. The
    # tenant_slug needs a separate fetch because TenantMiddleware
    # resolves from Host header, not from the user row.
    from api.v1.endpoints.auth import UserOut
    from models import Tenant as _T

    tenant_row = (
        await db.execute(
            select(_T).where(_T.id == user.tenant_id)
        )
    ).scalar_one_or_none()
    user_view = UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=tenant_row.slug if tenant_row else settings.default_tenant_slug,
        last_login_at=user.last_login_at,
    )

    return _render(
        request,
        "dashboard.html",
        stat_cameras_total=cam_count,
        stat_cameras_active=active_cams,
        stat_events_today=events_today,
        stat_nodes_online=nodes_online,
        stat_nodes_total=total_nodes,
        recent_events=recent_events,
        cam_name_map=cam_name_map,
        cameras=cams_by_status,
        user=user_view,
    )


__all__: tuple[str, ...] = ("ui_router",)