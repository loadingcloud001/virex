# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for the events API surface (Phase 2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EventOut(BaseModel):
    """Single event record.

    `bbox_parsed` is the JSON-parsed version of the DB's `bbox` text column.
    Stored as text in the DB (SQLite portability + simple replication),
    surfaced as a typed list[float] for API consumers. Falls back to
    `None` when the DB row's bbox string is malformed JSON.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    tenant_id: int
    camera_id: int
    event_uuid: str
    class_label: str
    score: float = Field(ge=0.0, le=1.0)
    bbox: str
    bbox_parsed: list[float] | None = None
    snapshot_url: str | None = None
    clip_url: str | None = None
    clip_built: bool
    event_time: datetime
    created_at: datetime


class EventListResponse(BaseModel):
    """Wrapper returned by `/api/events/table` (HTMX-targeted endpoint).

    The standard `/api/events` endpoint returns a flat list to match the
    `/api/cameras` shape; the table endpoint returns metadata + the same
    rows so the UI can render "Showing X of Y" + pagination.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[EventOut]
    total: int
    limit: int
    offset: int


class HlsUrlResponse(BaseModel):
    """Response for `/api/cameras/{id}/hls_url`.

    `hls_url` is the current HLS endpoint MediaMTX exposes on port 8888.
    `webrtc_url` is the WebRTC WHEP endpoint on port 8889 — supported in
    Phase 3, returned here as a placeholder so the API surface doesn't
    change when we ship the WHEP player.
    """

    model_config = ConfigDict(extra="forbid")

    hls_url: str
    webrtc_url: str
    mtx_path: str


__all__: tuple[str, ...] = ("EventOut", "EventListResponse", "HlsUrlResponse")