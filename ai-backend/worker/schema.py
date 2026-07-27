# SPDX-License-Identifier: AGPL-3.0
"""Wire schemas emitted by the per-camera worker.

`DetectionEvent` is the canonical MQTT payload published to
`virex/detections`. Schema version is pinned via `v=1`: bump in lockstep
with `event-router` and `clip-builder` if the payload changes.

`v=2` (this version) adds `zone_ids: list[str]` to `DetectionPayload` so
downstream consumers can match detections against the configured
zones without re-querying the portal DB.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DetectionPayload(BaseModel):
    """Single detection box inside a DetectionEvent."""

    model_config = ConfigDict(extra="forbid")

    label: str
    score: float
    box: list[float]  # [x1, y1, x2, y2] in [0, 1]
    zone_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Zone ids whose polygon contains this detection's "
            "bottom-center (after inertia). Empty list if no zones are "
            "configured or none fired."
        ),
    )


class DetectionEvent(BaseModel):
    """One MQTT message payload published to `virex/detections`."""

    model_config = ConfigDict(extra="forbid")

    v: int = 2
    event_uuid: str = Field(description="UUIDv4 hex; also the MinIO snapshot object key.")
    ts: str = Field(description="ISO-8601 UTC timestamp with timezone offset.")
    node_id: int | None = Field(default=None)
    tenant_id: int
    camera_id: int
    mtx_path: str  # full path including the layer suffix (e.g. `t1c5h264`)
    frame_id: int
    detections: list[DetectionPayload]
    snapshot_url: str  # MinIO object key `tenants/<tid>/snapshots/<uuid>.jpg`
    snapshot_size: int  # bytes


__all__: tuple[str, ...] = ("DetectionPayload", "DetectionEvent")
