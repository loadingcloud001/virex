# SPDX-License-Identifier: Apache-2.0
"""Internal + edge endpoints exposed by the portal for use by the edge-side
services (`edge-agent`, `clip-builder`) over the Tailscale tunnel.

These endpoints intentionally use a shared bearer-token scheme for v1
(rather than per-tenant JWT). The actual token is delivered out-of-band
to the edge node via `deploy/edge/.env`.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from models import Camera, Event, Node
from schemas.edge import (
    CameraEdgeDTO,
    ClipPatchDTO,
    DetectParamsDTO,
    EdgeConfigBundle,
    HeartbeatPayload,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["edge"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEALTHY_TIMEOUT_SEC: int = 300  # Over this age, the node is `unhealthy`.


# ---------------------------------------------------------------------------
# Tiny bearer dependency (Phase-1 simple; upgrade to mTLS or JWT when needed).
# ---------------------------------------------------------------------------
async def require_edge_bearer(
    authorization: str = Header(default=""),
) -> None:
    """Validate `Authorization: Bearer <edge_bearer>` for /api/edge + /internal."""
    expected = f"Bearer {settings.edge_bearer}"
    if authorization != expected:
        logger.warning("edge_auth_rejected", authorization=authorization[:32])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid edge bearer token.",
        )


BearerDep = Depends(require_edge_bearer)


# ---------------------------------------------------------------------------
# /api/edge/config — what the edge-agent pulls every 60s
# ---------------------------------------------------------------------------
@router.get(
    "/api/edge/config",
    response_model=EdgeConfigBundle,
    dependencies=[BearerDep],
    summary="Cameras-per-node bundle",
)
async def get_edge_config(
    node_id: int,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    since: int = 0,
) -> EdgeConfigBundle | None:
    """Return active cameras for `node_id`.

    Edge-agent can include `since=<known_config_version>` for cheap 304
    short-circuiting; we keep the implementation simple and always
    return the fresh payload (Phase 2 can fix the cache).
    """
    stmt = select(Node).where(Node.id == node_id)
    node_row = (await db.execute(stmt)).scalar_one_or_none()
    if node_row is None:
        raise HTTPException(status_code=404, detail="node not found")

    cams_stmt = select(Camera).where(Camera.node_id == node_id, Camera.status == "active")
    cams = (await db.execute(cams_stmt)).scalars().all()

    bundle = EdgeConfigBundle(
        node_id=node_row.id,
        config_version=node_row.current_config_version,
        cameras=[
            CameraEdgeDTO(
                mtx_path=c.mtx_path,
                source_rtsp=c.rtsp_url,
                tenant_id=c.tenant_id,
                camera_id=c.id,
                detect=DetectParamsDTO(),
                record=c.recording_enabled,
            )
            for c in cams
        ],
    )

    # If `since` matches the latest config_version, signal no-change.
    if since == node_row.current_config_version:
        logger.debug(
            "edge_config_unchanged",
            node_id=node_id,
            config_version=node_row.current_config_version,
        )
        return bundle
    logger.info(
        "edge_config_emitted",
        node_id=node_id,
        config_version=node_row.current_config_version,
        camera_count=len(bundle.cameras),
        since=since,
    )
    return bundle


# ---------------------------------------------------------------------------
# /api/edge/heartbeat — nvidia-smi stats from edge-agent
# ---------------------------------------------------------------------------
@router.post(
    "/api/edge/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[BearerDep],
    summary="Update node lehealth status",
)
async def post_heartbeat(
    payload: HeartbeatPayload,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    """Update node heartbeat timestamp + status. Idempotent."""
    now = datetime.utcnow()
    healthy = "healthy" if payload.healthy else "unhealthy"

    stmt = (
        update(Node)
        .where(Node.id == payload.node_id)
        .values(last_heartbeat_at=now, status=healthy)
    )
    await db.execute(stmt)
    await db.commit()

    logger.info(
        "heartbeat_recorded",
        node_id=payload.node_id,
        gpu_percent=payload.gpu_percent,
        gpu_mem_mb=payload.gpu_mem_mb,
        healthy=payload.healthy,
    )


# ---------------------------------------------------------------------------
# /internal/events/{event_id}/clip — clip-builder PATCHes the row
# ---------------------------------------------------------------------------
@router.patch(
    "/internal/events/{event_id}/clip",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[BearerDep],
    summary="Mark an event clip as built",
)
async def patch_event_clip(
    event_id: int,
    body: ClipPatchDTO,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    stmt = (
        update(Event)
        .where(Event.id == event_id)
        .values(clip_url=body.clip_url, clip_built=True)
    )
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(404, detail="event not found")
    await db.commit()
    logger.info("event_clip_patched", event_id=event_id, clip_url=body.clip_url)


__all__: tuple[str, ...] = ("router",)
