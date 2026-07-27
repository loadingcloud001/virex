# SPDX-License-Identifier: AGPL-3.0
"""`workers.yaml` loader (legacy schema used at boot).

`worker/main.py` calls `load_config()` once at startup to build the
initial `HotConfig` snapshot. After that, all hot-reloads go through
`config_hot.HotConfig.from_yaml()` (called by `ConfigReloader`).

The two schemas intentionally mirror each other so a single
`workers.yaml` validates against either one. New fields
(motion / zones / masks / pipeline) are **required to be added in
both places** — this is a deliberate fail-safe: if `config.py` is
out of date, the boot path fails loudly via Pydantic validation
rather than silently starting with a partial config.

`worker/config.py` is kept as the legacy loader; the canonical
runtime schema is `worker/config_hot.py`. Both validate the same
YAML; the hot-reload module constructs `HotConfig` directly from
Pydantic models for atomic swap.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from worker._yaml import expand_env


class DetectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fps: int = Field(default=5, ge=1, le=30, description="Frames per second sent to inference.")
    classes: list[str] = Field(default_factory=lambda: ["person"])
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    roi: list[list[float]] = Field(
        default_factory=list,
        description="Optional polygon in normalised [0,1] coords. Empty = full frame.",
    )


class MotionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    threshold: int = Field(default=30, ge=1, le=255)
    contour_area: int = Field(default=10, ge=1, le=10_000)
    lightning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class MaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="mask")
    coordinates: list[list[float]] = Field(default_factory=list)


class ZoneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    friendly_name: str = Field(default="")
    objects: list[str] = Field(default_factory=lambda: ["person"])
    inertia: int = Field(default=3, ge=1, le=60)
    coordinates: list[list[float]] = Field(default_factory=list)


class PipelineStageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["detect", "segment", "depth"]
    trigger: Literal["always", "on_motion", "on_high_conf", "on_zone_enter"] = Field(
        default="always"
    )
    target_objects: list[str] = Field(default_factory=list)
    zone_id: str | None = Field(default=None)


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mtx_path: str = Field(
        description="MediaMTX path stem WITHOUT the layer suffix (e.g. `t1c5`); "
        "the worker appends `h264` (default) or `raw` to form the full path."
    )
    source_rtsp: str = Field(
        default="",
        description=(
            "RTSP URL to ingest. Empty string means: derive from `mtx_path` "
            "via `rtsp://127.0.0.1:19554/<mtx_path><layer_suffix>` (the "
            "MediaMTX-normalised H.264 path on the same edge node)."
        ),
    )
    tenant_id: int = Field(ge=1)
    camera_id: int = Field(ge=1)
    record: bool = Field(default=True, description="Hint to mediamtx whether to record the path.")

    detect: DetectConfig = Field(default_factory=DetectConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    masks: list[MaskConfig] = Field(default_factory=list)
    zones: list[ZoneConfig] = Field(default_factory=list)
    pipeline: list[PipelineStageConfig] = Field(
        default_factory=lambda: [PipelineStageConfig(stage="detect")]
    )


class WorkerGlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detector_url: str = Field(default="http://127.0.0.1:31001")
    detector_kind: Literal["fastapi", "triton"] = Field(default="triton")
    mqtt_broker: str = Field(default="", description="host:port of Mosquitto over Tailscale.")
    mqtt_client_id: str = Field(default="virex-worker")
    mqtt_topic: str = Field(default="virex/detections", description="Outbound publish topic.")
    minio_endpoint: str = Field(default="", description="host:port of MinIO over Tailscale.")
    minio_secure: bool = Field(default=True)
    minio_bucket: str = Field(default="virex")
    minio_access_key: str = Field(default="")
    minio_secret_key: str = Field(default="")
    minio_region: str = Field(
        default="",
        description=(
            "Optional S3/COS region (e.g. `ap-singapore`). Required by some "
            "cloud providers so the SDK signs the request to the correct "
            "regional endpoint."
        ),
    )
    snapshot_quality: int = Field(default=80, ge=10, le=95)
    layer_suffix: Literal["h264", "raw"] = Field(default="h264")
    node_id: int | None = Field(default=None)
    cameras: list[CameraConfig]


def load_config(path: str | os.PathLike[str]) -> WorkerGlobalConfig:
    """Load and validate `workers.yaml` from disk.

    Secret placeholders (${VAR}) are expanded via `os.environ`. The
    edge-agent is responsible for pre-rendering secrets into the file
    before the container starts.
    """
    raw = Path(path).read_text(encoding="utf-8")
    expanded = expand_env(raw)
    data = yaml.safe_load(expanded)
    return WorkerGlobalConfig.model_validate(data)


__all__: tuple[str, ...] = (
    "CameraConfig",
    "DetectConfig",
    "MotionConfig",
    "MaskConfig",
    "ZoneConfig",
    "PipelineStageConfig",
    "WorkerGlobalConfig",
    "load_config",
)
