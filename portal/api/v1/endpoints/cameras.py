# SPDX-License-Identifier: Apache-2.0
"""Camera CRUD endpoints (Phase 1).

All endpoints are tenant-scoped (via TenantMiddleware). Admin session
required. Mutations on the cameras table bump `current_config_version`
on the relevant node so edge-agent picks up the change within its
`config_pull_period_sec` interval (default 60 s).
"""

from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.deps import require_admin_session
from core.config import settings
from core.database import get_db
from models import MTX_PATH_RE, Camera, Event, Node, User
from schemas.events import EventOut, HlsUrlResponse

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


# ---------------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------------
class CameraCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    mtx_path: str = Field(min_length=1, max_length=63)
    rtsp_url: str = Field(min_length=1)
    location: str | None = Field(default=None, max_length=255)
    node_id: int | None = None
    recording_enabled: bool = True
    retention_days: int = Field(default=7, ge=1, le=365)


class CameraUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    rtsp_url: str | None = None
    location: str | None = None
    node_id: int | None = None
    recording_enabled: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)


class CameraOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    tenant_id: int
    node_id: int | None
    name: str
    location: str | None
    rtsp_url: str
    mtx_path: str
    status: str
    recording_enabled: bool
    retention_days: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_MTX_RE = re.compile(MTX_PATH_RE)


def _validate_mtx_path(mtx_path: str) -> None:
    if not _MTX_RE.match(mtx_path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"mtx_path must match regex {MTX_PATH_RE!r}",
        )


async def _bump_config_version(db: AsyncSession, node_id: int | None) -> None:
    if node_id is None:
        return
    await db.execute(
        update(Node)
        .where(Node.id == node_id)
        .values(current_config_version=Node.current_config_version + 1)
    )


async def _camera_to_out(c: Camera) -> CameraOut:
    return CameraOut(
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[CameraOut], summary="List cameras in current tenant.")
async def list_cameras(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> list[CameraOut]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        return []
    stmt = select(Camera).where(Camera.tenant_id == tenant_id).order_by(Camera.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _camera_to_out(c) for c in rows]


@router.post(
    "",
    response_model=CameraOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a camera.",
)
async def create_camera(
    body: CameraCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> CameraOut:
    tenant_id = getattr(request.state, "tenant_id")
    _validate_mtx_path(body.mtx_path)

    # Uniqueness on (tenant_id, mtx_path) — globally unique mtx_path is
    # too strict for multi-tenant (different tenants may want the same
    # `t1c5` stem). We rely on the global unique constraint on
    # mtx_path for Phase 1 (single-tenant pilot) but verify here too.
    existing = (
        await db.execute(select(Camera).where(Camera.mtx_path == body.mtx_path))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"mtx_path {body.mtx_path!r} already exists",
        )

    new_cam = Camera(
        tenant_id=tenant_id,
        node_id=body.node_id,
        name=body.name,
        location=body.location,
        rtsp_url=body.rtsp_url,
        mtx_path=body.mtx_path,
        status="active",
        recording_enabled=body.recording_enabled,
        retention_days=body.retention_days,
    )
    db.add(new_cam)
    await _bump_config_version(db, body.node_id)
    await db.commit()
    await db.refresh(new_cam)

    logger.info(
        "camera_created",
        camera_id=new_cam.id,
        tenant_id=tenant_id,
        mtx_path=body.mtx_path,
        by_user_id=user.id,
    )

    return await _camera_to_out(new_cam)


@router.get("/{camera_id}", response_model=CameraOut, summary="Get a camera by id.")
async def get_camera(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> CameraOut:
    tenant_id = getattr(request.state, "tenant_id")
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return await _camera_to_out(cam)


@router.put("/{camera_id}", response_model=CameraOut, summary="Update a camera.")
async def update_camera(
    camera_id: int,
    body: CameraUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> CameraOut:
    tenant_id = getattr(request.state, "tenant_id")
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    old_node_id = cam.node_id
    # Apply updates.
    if body.name is not None:
        cam.name = body.name
    if body.rtsp_url is not None:
        cam.rtsp_url = body.rtsp_url
    if body.location is not None:
        cam.location = body.location
    if body.node_id is not None:
        cam.node_id = body.node_id
    if body.recording_enabled is not None:
        cam.recording_enabled = body.recording_enabled
    if body.retention_days is not None:
        cam.retention_days = body.retention_days

    # Bump config_version on both old and new nodes (in case the
    # camera moved between nodes).
    await _bump_config_version(db, old_node_id)
    if cam.node_id != old_node_id:
        await _bump_config_version(db, cam.node_id)
    await db.commit()
    await db.refresh(cam)

    logger.info(
        "camera_updated",
        camera_id=cam.id,
        tenant_id=tenant_id,
        by_user_id=user.id,
    )

    return await _camera_to_out(cam)


@router.delete(
    "/{camera_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a camera (status='deleted', edge-agent removes worker).",
)
async def delete_camera(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(require_admin_session),  # noqa: B008
) -> None:
    tenant_id = getattr(request.state, "tenant_id")
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    node_id = cam.node_id
    cam.status = "deleted"
    await _bump_config_version(db, node_id)
    await db.commit()

    logger.info(
        "camera_deleted",
        camera_id=cam.id,
        tenant_id=tenant_id,
        by_user_id=user.id,
    )


# ---------------------------------------------------------------------------
# HLS / WebRTC playback URL endpoints (Phase 2)
# ---------------------------------------------------------------------------
@router.get(
    "/{camera_id}/hls_url",
    response_model=HlsUrlResponse,
    summary="Return the MediaMTX playback URLs for a camera.",
)
async def get_camera_hls_url(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> HlsUrlResponse:
    """Return `{hls_url, webrtc_url, mtx_path}` for the camera.

    The `hls_url` is built from `settings.mediamtx_public_url` (default
    `http://mediamtx:8888`) + `/<mtx_path>/index.m3u8`. The WebRTC URL
    uses the WHEP endpoint at `:8889/<mtx_path>/whep` — surfaced here
    even though the UI doesn't play it yet (Phase 3).
    """
    tenant_id = getattr(request.state, "tenant_id")
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    base = settings.mediamtx_public_url.rstrip("/")
    return HlsUrlResponse(
        hls_url=f"{base}/{cam.mtx_path}/index.m3u8",
        webrtc_url=f"{base.rsplit(':', 1)[0]}:8889/{cam.mtx_path}/whep",
        mtx_path=cam.mtx_path,
    )


@router.get(
    "/{camera_id}/events",
    response_model=list[EventOut],
    summary="Recent events for a single camera (last 50, tenant-scoped).",
)
async def list_camera_events(
    camera_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _user: User = Depends(require_admin_session),  # noqa: B008
) -> list[EventOut]:
    """Returns the 50 most recent events for one camera, newest first.

    Used by the camera detail page right rail. For the full event list
    with filters, use `/api/events` instead.
    """
    import json as _json

    tenant_id = getattr(request.state, "tenant_id")
    cam = (
        await db.execute(
            select(Camera).where(
                Camera.id == camera_id, Camera.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    rows = (
        await db.execute(
            select(Event)
            .where(Event.tenant_id == tenant_id, Event.camera_id == camera_id)
            .order_by(Event.event_time.desc())
            .limit(50)
        )
    ).scalars().all()

    out: list[EventOut] = []
    for e in rows:
        bbox_parsed: list[float] | None = None
        try:
            bbox_parsed = _json.loads(e.bbox)
            if not isinstance(bbox_parsed, list):
                bbox_parsed = None
        except (ValueError, TypeError):
            bbox_parsed = None
        out.append(
            EventOut(
                id=e.id,
                tenant_id=e.tenant_id,
                camera_id=e.camera_id,
                event_uuid=e.event_uuid,
                class_label=e.class_label,
                score=e.score,
                bbox=e.bbox,
                bbox_parsed=bbox_parsed,
                snapshot_url=e.snapshot_url,
                clip_url=e.clip_url,
                clip_built=e.clip_built,
                event_time=e.event_time,
                created_at=e.created_at,
            )
        )
    return out


__all__: tuple[str, ...] = ("router",)