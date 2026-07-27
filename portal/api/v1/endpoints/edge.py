# SPDX-License-Identifier: Apache-2.0
"""Edge ↔ portal endpoints (Phase 1).

Authentication: `require_edge_jwt` (bearer JWT) for /config and
/heartbeat; `require_edge_bootstrap_secret` for first-time
registration. The JWT subject is the `node_id`; the portal filters
cameras by `Camera.node_id` matching the JWT subject.

Endpoints:
- POST /api/edge/nodes/register   — bootstrap (shared secret) → returns JWT
- POST /api/edge/nodes/{id}/rotate — refresh JWT (uses old JWT for auth)
- GET  /api/edge/config           — bundle for this node
- POST /api/edge/heartbeat        — health telemetry
- PATCH /internal/events/{id}/clip — clip-builder marks clip built
"""

from __future__ import annotations

import socket
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.deps import (
    require_edge_bootstrap_secret,
    require_edge_jwt,
)
from core.config import settings
from core.database import get_db
from core.security import create_edge_token
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
# /api/edge/nodes/register — first-time bootstrap
# ---------------------------------------------------------------------------
class NodeRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=255)
    tailscale_ip: str = Field(min_length=1, max_length=45)
    gpu_model: str | None = Field(default=None, max_length=100)
    max_cameras: int = Field(default=50, ge=1, le=5000)


class NodeRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: int
    jwt_token: str
    ttl_sec: int
    tenant_id: int


@router.post(
    "/api/edge/nodes/register",
    response_model=NodeRegisterResponse,
    summary="First-time edge registration (uses shared bootstrap secret).",
)
async def register_node(
    body: NodeRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _auth: None = Depends(require_edge_bootstrap_secret),  # noqa: B008
) -> NodeRegisterResponse:
    """Issue a JWT for an edge node. Idempotent on `hostname`."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="portal not seeded",
        )

    # Idempotent on hostname within tenant.
    stmt = select(Node).where(
        Node.tenant_id == tenant_id,
        Node.hostname == body.hostname,
    )
    node = (await db.execute(stmt)).scalar_one_or_none()

    if node is None:
        node = Node(
            tenant_id=tenant_id,
            hostname=body.hostname,
            tailscale_ip=body.tailscale_ip,
            gpu_model=body.gpu_model,
            max_cameras=body.max_cameras,
            status="pending",
        )
        db.add(node)
        await db.commit()
        await db.refresh(node)
        logger.info(
            "edge_node_registered",
            node_id=node.id,
            hostname=body.hostname,
            tenant_id=tenant_id,
        )
    else:
        # Update liveness info; don't reset status (heartbeat does that).
        node.last_heartbeat_at = datetime.utcnow()
        await db.commit()

    token = create_edge_token(
        jwt_secret=settings.jwt_secret,
        node_id=node.id,
        tenant_id=node.tenant_id,
        hostname=node.hostname,
        ttl_sec=settings.jwt_ttl_sec,
    )

    return NodeRegisterResponse(
        node_id=node.id,
        jwt_token=token,
        ttl_sec=settings.jwt_ttl_sec,
        tenant_id=node.tenant_id,
    )


# ---------------------------------------------------------------------------
# /api/edge/nodes/{id}/rotate — refresh JWT
# ---------------------------------------------------------------------------
@router.post(
    "/api/edge/nodes/{node_id}/rotate",
    response_model=NodeRegisterResponse,
    summary="Rotate JWT (requires current valid JWT).",
)
async def rotate_node_token(
    node_id: int,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    node: Node = Depends(require_edge_jwt),  # noqa: B008
) -> NodeRegisterResponse:
    """Issue a fresh JWT for the calling node."""
    if node.id != node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot rotate a token for a different node",
        )

    token = create_edge_token(
        jwt_secret=settings.jwt_secret,
        node_id=node.id,
        tenant_id=node.tenant_id,
        hostname=node.hostname,
        ttl_sec=settings.jwt_ttl_sec,
    )
    logger.info("edge_node_token_rotated", node_id=node.id)

    return NodeRegisterResponse(
        node_id=node.id,
        jwt_token=token,
        ttl_sec=settings.jwt_ttl_sec,
        tenant_id=node.tenant_id,
    )


# ---------------------------------------------------------------------------
# /api/edge/config — JWT-protected, returns cameras for the JWT's node
# ---------------------------------------------------------------------------
@router.get(
    "/api/edge/config",
    response_model=EdgeConfigBundle,
    summary="Cameras-per-node bundle (JWT-required).",
)
async def get_edge_config(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    node: Node = Depends(require_edge_jwt),  # noqa: B008
    since: int = 0,
) -> EdgeConfigBundle:
    """Return active cameras for `node` (from JWT subject)."""
    cams_stmt = (
        select(Camera)
        .where(Camera.node_id == node.id, Camera.status == "active")
        .order_by(Camera.id)
    )
    cams = (await db.execute(cams_stmt)).scalars().all()

    bundle = EdgeConfigBundle(
        node_id=node.id,
        config_version=node.current_config_version,
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

    if since == node.current_config_version:
        logger.debug(
            "edge_config_unchanged",
            node_id=node.id,
            config_version=node.current_config_version,
        )
    else:
        logger.info(
            "edge_config_emitted",
            node_id=node.id,
            config_version=node.current_config_version,
            camera_count=len(bundle.cameras),
            since=since,
        )

    return bundle


# ---------------------------------------------------------------------------
# /api/edge/heartbeat — JWT-protected
# ---------------------------------------------------------------------------
@router.post(
    "/api/edge/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Update node health status.",
)
async def post_heartbeat(
    payload: HeartbeatPayload,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    node: Node = Depends(require_edge_jwt),  # noqa: B008
) -> None:
    """Update node heartbeat timestamp + status. Idempotent."""
    now = datetime.utcnow()
    healthy = "healthy" if payload.healthy else "unhealthy"

    await db.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(last_heartbeat_at=now, status=healthy)
    )
    await db.commit()

    logger.info(
        "heartbeat_recorded",
        node_id=node.id,
        gpu_percent=payload.gpu_percent,
        gpu_mem_mb=payload.gpu_mem_mb,
        healthy=payload.healthy,
    )


# ---------------------------------------------------------------------------
# /internal/events/{event_id}/clip — JWT-protected
# ---------------------------------------------------------------------------
@router.patch(
    "/internal/events/{event_id}/clip",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark an event clip as built (JWT-required).",
)
async def patch_event_clip(
    event_id: int,
    body: ClipPatchDTO,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _auth: Node = Depends(require_edge_jwt),  # noqa: B008
) -> None:
    """Clip-builder notifies the portal that a clip is ready."""
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


# Suppress unused-import warning.
_ = socket