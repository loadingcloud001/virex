# SPDX-License-Identifier: Apache-2.0
"""Portal SQLAlchemy 2.0 ORM models.

Phase E scope: `Tenant`, `Node`, `Camera` (with the renamed `mtx_path`
column), `Event`, `AlertRule`. Phase-1 user/refresh-token models are
intentionally stubbed here for completeness but kept simple.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.database import Base

# ---------------------------------------------------------------------------
# Camera path regex — `t<tenant_id>c<camera_id>` (`t1c5`, `t10c42`)
# NOT `t1_c5` (underscores trigger MediaMTX nesting bug, see docs).
# ---------------------------------------------------------------------------
MTX_PATH_RE: str = r"^t\d+c\d+$"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subdomain: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    branding: Mapped[dict[str, Any]] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cameras: Mapped[list[Camera]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Node(Base):
    """GPU edge node that runs MediaMTX + workers.

    `current_config_version` is bumped by portal whenever a camera add/
    update causes the edge-agent to need to reconcile.
    """

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    tailscale_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    gpu_model: Mapped[str | None] = mapped_column(String(100))
    max_cameras: Mapped[int] = mapped_column(Integer, default=50)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_config_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cameras: Mapped[list[Camera]] = relationship(back_populates="node")


class Camera(Base):
    """A camera registered for a tenant and bound to a node.

    v1 rename: the previously-published schema column `frigate_name` is
    replaced by `mtx_path` — a single string of the form `t{t}c{c}`
    that both MediaMTX and MQTT routing read directly.
    """

    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("nodes.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    rtsp_url: Mapped[str] = mapped_column(Text, nullable=False)
    mtx_path: Mapped[str] = mapped_column(
        String(63),
        nullable=False,
        unique=True,
        comment="MediaMTX path stem t{tenant}c{camera}; no layer suffix.",
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    zones: Mapped[list[Any]] = mapped_column(Text, default="[]")
    object_filters: Mapped[dict[str, Any]] = mapped_column(Text, default="{}")
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(f"mtx_path ~ '{MTX_PATH_RE}'", name="ck_mtx_path_format"),
        Index("idx_cameras_tenant", "tenant_id"),
        Index("idx_cameras_node", "node_id"),
        Index("idx_cameras_mtx_path", "mtx_path", unique=True),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="cameras")
    node: Mapped[Node] = relationship(back_populates="cameras")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    camera_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    event_uuid: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    class_label: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[float] = mapped_column(nullable=False)
    bbox: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_url: Mapped[str | None] = mapped_column(Text)
    clip_url: Mapped[str | None] = mapped_column(Text)
    clip_built: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_events_tenant_camera", "tenant_id", "camera_id"),)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    class_filter: Mapped[str | None] = mapped_column(String(40))
    notification_channels: Mapped[str] = mapped_column(Text, default="[]")
    schedule: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


__all__: tuple[str, ...] = (
    "Tenant",
    "Node",
    "Camera",
    "Event",
    "AlertRule",
    "MTX_PATH_RE",
)
