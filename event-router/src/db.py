# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy 2.0 async access for the event-router.

This module contains ONLY the event-router's slice of the Virex schema
(tables it reads from or writes to): `cameras`, `events`, and
`alert_rules`. The full portal schema lives in `portal/models/`.
Keeping a local mirror avoids cross-repo Python imports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base — used by the event-router's mirror tables."""


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    mtx_path: Mapped[str] = mapped_column(String(63), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(Text, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    camera_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    event_uuid: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    class_label: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[float] = mapped_column(nullable=False)
    bbox: Mapped[str] = mapped_column(Text, nullable=False)  # JSON string
    snapshot_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    clip_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    clip_built: Mapped[bool] = mapped_column(default=False, nullable=False)
    event_time: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)


Index("idx_events_tenant_camera", Event.tenant_id, Event.camera_id)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    class_filter: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notification_channels: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


def make_engine(db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return async_sessionmaker(engine, expire_on_commit=False)


async def insert_event(
    session: AsyncSession,
    *,
    tenant_id: int,
    camera_id: int,
    event_uuid: str,
    class_label: str,
    score: float,
    bbox: str,
    snapshot_url: str | None,
    event_time: datetime,
) -> int:
    """INSERT a new event row; returns the generated id."""
    row = Event(
        tenant_id=tenant_id,
        camera_id=camera_id,
        event_uuid=event_uuid,
        class_label=class_label,
        score=score,
        bbox=bbox,
        snapshot_url=snapshot_url,
        event_time=event_time,
    )
    session.add(row)
    await session.flush()
    return row.id


async def get_camera_for_event(
    session: AsyncSession,
    tenant_id: int,
    camera_id: int,
) -> Camera | None:
    """Resolve the single active camera for (tenant, camera_id)."""
    stmt = select(Camera).where(
        Camera.tenant_id == tenant_id,
        Camera.id == camera_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_rules_for_tenant(
    session: AsyncSession,
    tenant_id: int,
) -> list[AlertRule]:
    stmt = select(AlertRule).where(
        AlertRule.tenant_id == tenant_id,
        AlertRule.enabled.is_(True),
    )
    result = await session.execute(stmt)
    return list(result.scalars())


def model_to_dict(model: Base) -> dict[str, Any]:
    """Tiny helper for tests/logging."""
    return {c.name: getattr(model, c.name) for c in model.__table__.columns}


__all__: tuple[str, ...] = (
    "Base",
    "Camera",
    "Event",
    "AlertRule",
    "make_engine",
    "insert_event",
    "get_camera_for_event",
    "list_rules_for_tenant",
    "model_to_dict",
)
