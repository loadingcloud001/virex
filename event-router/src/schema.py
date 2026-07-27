# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for the event-router's inbound and outbound MQTT traffic."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DetectionInBox(BaseModel):
    """One detection entry inside a DetectionEvent."""

    model_config = ConfigDict(extra="forbid")

    label: str
    score: float
    box: list[float]  # [x1, y1, x2, y2] in [0, 1]


class DetectionEvent(BaseModel):
    """Inbound MQTT payload on `virex/detections` from per-camera workers.

    Schema mirror of `ai-backend/worker/schema.py`. Kept duplicated (not
    imported) so event-router does not need the ai-backend Python
    package on its image; the JSON wire contract is the only coupling.
    """

    model_config = ConfigDict(extra="forbid")

    v: int = 1
    event_uuid: str
    ts: str  # ISO-8601 with tz
    node_id: int | None = None
    tenant_id: int
    camera_id: int
    mtx_path: str
    frame_id: int
    detections: list[DetectionInBox]
    snapshot_url: str
    snapshot_size: int


class EventCreated(BaseModel):
    """Outbound MQTT payload on `virex/events_created` for clip-builder."""

    model_config = ConfigDict(extra="forbid")

    v: int = 1
    event_id: int
    tenant_id: int
    mtx_path: str
    event_ts: str  # ISO-8601 with tz


__all__: tuple[str, ...] = ("DetectionEvent", "DetectionInBox", "EventCreated")
