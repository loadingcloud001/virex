# SPDX-License-Identifier: Apache-2.0
"""Pydantic request/response models for `/api/edge/*` and `/internal/*`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DetectParamsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fps: int = Field(default=5, ge=1, le=30)
    classes: list[str] = Field(default_factory=lambda: ["person"])
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    roi: list[list[float]] = Field(default_factory=list)


class CameraEdgeDTO(BaseModel):
    """One camera entry inside the GET /api/edge/config bundle."""

    model_config = ConfigDict(extra="forbid")

    mtx_path: str = Field(description="MediaMTX stem WITHOUT the layer suffix.")
    source_rtsp: str = Field(
        description=(
            "Upstream RTSP URL of the camera (the MediaMTX recorder ingest). "
            "Workers normally ingest `rtsp://127.0.0.1:19554/<mtx_path>h264` "
            "instead; the edge-agent renders that URL into the worker config."
        )
    )
    tenant_id: int
    camera_id: int
    detect: DetectParamsDTO = Field(default_factory=DetectParamsDTO)
    record: bool = True


class EdgeConfigBundle(BaseModel):
    """Body returned by GET /api/edge/config."""

    model_config = ConfigDict(extra="forbid")

    node_id: int
    config_version: int
    cameras: list[CameraEdgeDTO]


class HeartbeatPayload(BaseModel):
    """Body accepted by POST /api/edge/heartbeat."""

    model_config = ConfigDict(extra="forbid")

    node_id: int
    gpu_percent: float = Field(ge=0.0, le=100.0)
    gpu_mem_mb: int = Field(ge=0)
    cpu_percent: float = Field(ge=0.0, le=100.0)
    ram_percent: float = Field(ge=0.0, le=100.0)
    active_cameras: int = Field(ge=0)
    healthy: bool = True


class ClipPatchDTO(BaseModel):
    """Body accepted by PATCH /internal/events/{event_id}/clip."""

    model_config = ConfigDict(extra="forbid")

    clip_url: str = Field(description="MinIO object key, e.g. tenants/1/clips/887.mp4")


__all__: tuple[str, ...] = (
    "DetectParamsDTO",
    "CameraEdgeDTO",
    "EdgeConfigBundle",
    "HeartbeatPayload",
    "ClipPatchDTO",
)
