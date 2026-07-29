# SPDX-License-Identifier: Apache-2.0
"""Event list + detail endpoints (Phase 2).

All endpoints are tenant-scoped via `request.state.tenant_id`. Admin
session required. The `/api/events/table` endpoint returns raw HTML
fragments for HTMX consumption; the JSON `/api/events` endpoint returns
the same data as a flat list.

Query parameters (shared):
- `camera_id` (int, optional): filter to one camera
- `window` (str, optional): one of `1h`, `6h`, `24h`, `7d`, `all`. Default `24h`.
- `limit` (int, optional): 1-100, default 50.
- `offset` (int, optional): >=0, default 0.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.deps import require_admin_session
from core.database import get_db
from models import Camera, Event, User
from schemas.events import EventListResponse, EventOut
from services.events import event_to_out

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["events"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_WINDOW_DELTAS: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "all": None,
}


def _resolve_window(window: str) -> datetime | None:
    """Translate `?window=1h` → cutoff datetime. `None` means no filter."""
    delta = _WINDOW_DELTAS.get(window)
    if delta is None and window != "all":
        raise HTTPException(
            status_code=422,
            detail=f"window must be one of {sorted(_WINDOW_DELTAS.keys())}",
        )
    if delta is None:
        return None
    return datetime.now(timezone.utc) - delta


def _event_to_out(e: Event) -> EventOut:
    """Backwards-compat alias.

    Older callers (and the existing `templates/__init__.py` import below)
    reference `_event_to_out`. New code should import `event_to_out`
    from `services.events` directly — that module also has unit-test
    surface for bbox parsing.
    """
    return event_to_out(e)


async def _query_events(
    db: AsyncSession,
    tenant_id: int,
    camera_id: int | None,
    window: str,
    limit: int,
    offset: int,
) -> tuple[list[EventOut], int]:
    cutoff = _resolve_window(window)
    stmt = select(Event).where(Event.tenant_id == tenant_id)
    count_stmt = (
        select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id)
    )
    if camera_id is not None:
        stmt = stmt.where(Event.camera_id == camera_id)
        count_stmt = count_stmt.where(Event.camera_id == camera_id)
    if cutoff is not None:
        stmt = stmt.where(Event.event_time >= cutoff)
        count_stmt = count_stmt.where(Event.event_time >= cutoff)

    stmt = stmt.order_by(Event.event_time.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar_one()

    return [_event_to_out(r) for r in rows], int(total)


# ---------------------------------------------------------------------------
# Inline Jinja env for HTMX fragment endpoint
# ---------------------------------------------------------------------------
_TEMPLATES_DIR = (
    __import__("pathlib").Path(__file__).resolve().parent.parent.parent.parent
    / "templates"
)
_fragment_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _install_filters(env: Environment) -> None:
    """Register the shared Jinja filters on a fragment env."""
    from templates import _class_badge_filter, _relative_time_filter
    env.filters["relative_time"] = _relative_time_filter
    env.filters["class_badge"] = _class_badge_filter


_install_filters(_fragment_env)


# ---------------------------------------------------------------------------
# JSON endpoints (public-ish, used by API consumers)
# ---------------------------------------------------------------------------
@router.get(
    "/api/events",
    response_model=list[EventOut],
    summary="List events in the current tenant (JSON, flat list).",
)
async def list_events_json(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
    camera_id: int | None = Query(default=None, ge=1),
    window: str = Query(default="24h"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[EventOut]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        return []
    items, _total = await _query_events(
        db, tenant_id, camera_id, window, limit, offset
    )
    return items


@router.get(
    "/api/events/table",
    response_class=HTMLResponse,
    summary="HTMX fragment — returns <tr>… rows for the filtered event list.",
)
async def list_events_table(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
    camera_id: int | None = Query(default=None, ge=1),
    window: str = Query(default="24h"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> HTMLResponse:
    """Returns a fragment that HTMX swaps into `#events-tbody`.

    Uses a tiny in-memory Jinja2 environment scoped to this endpoint so
    we don't depend on the global templates env (which is wired for full
    page renders with base.html).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        html = '<tr><td colspan="6" class="text-center text-base-content/60">No tenant context.</td></tr>'
        return HTMLResponse(html)

    items, total = await _query_events(
        db, tenant_id, camera_id, window, limit, offset
    )

    # Look up camera names for the cells.
    cam_ids = sorted({e.camera_id for e in items})
    cam_name_map: dict[int, str] = {}
    if cam_ids:
        cam_rows = (
            await db.execute(
                select(Camera.id, Camera.name, Camera.mtx_path).where(
                    Camera.tenant_id == tenant_id, Camera.id.in_(cam_ids)
                )
            )
        ).all()
        cam_name_map = {
            cid: (cname or mtx) for (cid, cname, mtx) in cam_rows
        }

    # Always emit an OOB (out-of-band) swap element so the parent page's
    # "Showing X events" footer reflects the current count, even when
    # the filtered result is empty. Without this the count stays stale.
    oob_count = f'<span id="events-count-value" hx-swap-oob="true">{total}</span>'

    if not items:
        html = oob_count + _fragment_env.get_template("events/_empty.html").render(
            message="No events match the current filter."
        )
        return HTMLResponse(html)

    html = (
        oob_count
        + _fragment_env.get_template("events/_rows.html").render(
            events=items,
            cam_name_map=cam_name_map,
            now=datetime.now(timezone.utc),
            total=total,
        )
    )
    return HTMLResponse(html)


@router.get(
    "/api/events/{event_id}",
    response_model=EventOut,
    summary="Get a single event by id.",
)
async def get_event(
    event_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> EventOut:
    tenant_id = getattr(request.state, "tenant_id", None)
    event = (
        await db.execute(
            select(Event).where(
                Event.id == event_id, Event.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return _event_to_out(event)


__all__: tuple[str, ...] = ("router",)