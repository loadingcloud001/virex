# SPDX-License-Identifier: AGPL-3.0
"""Hot-reloadable config snapshot for the worker process.

The worker reads per-frame inference parameters (fps, min_score, classes,
roi, snapshot_quality, motion, zones, masks, pipeline) on every
iteration; making those reads go through an in-memory `HotConfig`
object — which the reloader can swap atomically on workers.yaml change
— gives us Tier-A reloads with zero downtime.

Tier-B fields (source_rtsp, mqtt_broker, mqtt_topic, minio_*) live in the
same `HotConfig`; reads are still per-frame, but a change in those fields
implies the affected subsystem (RTSP feeder / MQTT publisher / S3
uploader) should reconnect or re-init. The reloader posts a sentinel to
the affected `CameraLoop` queue so it picks up the new RTSP URL on the
next iteration.

Tier-C/D (recording flag, transcoder params, add/remove camera) are NOT
handled here — that lives in `edge-agent/src/reconcile.py` because it
requires container-level actions.

The atomic-swap pattern uses immutable `HotConfig` instances so a slow
`CameraLoop._process_frame` always sees a coherent snapshot — never a
half-applied change. Read overhead is one pointer copy.

`HotConfig.from_yaml(path)` parses + validates + constructs the
`HotCameraCfg` lookup keyed by `mtx_path`. Empty `source_rtsp` is
preserved as `""`; `CameraLoop` then falls back to the MediaMTX-derived
URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from worker._yaml import expand_env


# ---------------------------------------------------------------------------
# Validation schema (mirror of worker.config but lighter — only the fields
# that participate in hot-reload live here).
# ---------------------------------------------------------------------------
class DetectSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fps: int = Field(default=5, ge=1, le=30)
    classes: list[str] = Field(default_factory=lambda: ["person"])
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    roi: list[list[float]] = Field(default_factory=list)


class MotionSchema(BaseModel):
    """Frigate-style motion detector pre-filter (worker-side, CPU)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    threshold: int = Field(default=30, ge=1, le=255)
    contour_area: int = Field(default=10, ge=1, le=10_000)
    lightning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


def _validate_polygon(name: str, coords: list[list[float]]) -> list[tuple[float, float]]:
    """Normalise a polygon to a tuple of `(x, y)` pairs in `[0, 1]`.

    Used by both masks (polygon to erase) and zones (polygon to detect
    detections inside). Empty list is allowed. Coordinates outside
    `[0, 1]` are clamped.
    """
    if not coords:
        return ()
    out: list[tuple[float, float]] = []
    for point in coords:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(
                f"{name} coordinates must be list of [x, y] pairs; got {point!r}"
            )
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name} coordinate non-numeric: {point!r}") from e
        out.append((max(0.0, min(1.0, x)), max(0.0, min(1.0, y))))
    if len(out) < 3:
        raise ValueError(f"{name} polygon must have at least 3 points; got {len(out)}")
    return tuple(out)


class MaskSchema(BaseModel):
    """Static polygon where motion + detect are disabled (cv2.fillPoly BLACK).

    `name` is informational; `coordinates` are normalised `[0, 1]` xy pairs.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="mask")
    coordinates: list[list[float]] = Field(default_factory=list)

    @classmethod
    def normalised_coordinates(cls, v: list[list[float]]) -> list[tuple[float, float]]:
        return list(_validate_polygon("mask", v))


class ZoneSchema(BaseModel):
    """Polygon filter applied after detect. Triggers alerts on inertia.

    The worker's `apply_zones()` checks each detection's bottom-center
    against this polygon. `inertia` = consecutive frames the bbox must
    stay inside before the tag fires (Frigate-style).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    friendly_name: str = Field(default="")
    objects: list[str] = Field(default_factory=lambda: ["person"])
    inertia: int = Field(default=3, ge=1, le=60)
    coordinates: list[list[float]] = Field(default_factory=list)


class PipelineStageSchema(BaseModel):
    """One stage in the per-camera AI pipeline.

    Each stage corresponds to a Triton ensemble (`detect_only`,
    `detect_segment`, etc.) or the same model alone.
    """

    model_config = ConfigDict(extra="forbid")

    stage: Literal["detect", "segment", "depth"] = Field(...)
    trigger: Literal["always", "on_motion", "on_high_conf", "on_zone_enter"] = Field(
        default="always"
    )
    target_objects: list[str] = Field(default_factory=list)
    zone_id: str | None = Field(default=None)


class CameraSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mtx_path: str = Field(min_length=1)
    source_rtsp: str = Field(default="")
    tenant_id: int = Field(ge=1)
    camera_id: int = Field(ge=1)
    record: bool = Field(default=True)
    detect: DetectSchema = Field(default_factory=DetectSchema)
    motion: MotionSchema = Field(default_factory=MotionSchema)
    masks: list[MaskSchema] = Field(default_factory=list)
    zones: list[ZoneSchema] = Field(default_factory=list)
    pipeline: list[PipelineStageSchema] = Field(
        default_factory=lambda: [PipelineStageSchema(stage="detect")]
    )


class GlobalSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Inference backend. Defaults to FastAPI detector; can be Triton
    # (`http://127.0.0.1:38000`) or any KServe-compatible endpoint.
    detector_url: str = Field(default="http://127.0.0.1:31001")
    detector_kind: Literal["fastapi", "triton"] = Field(default="triton")
    mqtt_broker: str = Field(default="")
    mqtt_client_id: str = Field(default="virex-worker")
    mqtt_topic: str = Field(default="virex/detections")
    minio_endpoint: str = Field(default="")
    minio_secure: bool = Field(default=True)
    minio_bucket: str = Field(default="virex")
    minio_access_key: str = Field(default="")
    minio_secret_key: str = Field(default="")
    minio_region: str = Field(default="")
    snapshot_quality: int = Field(default=80, ge=10, le=95)
    layer_suffix: Literal["h264", "raw"] = Field(default="h264")
    node_id: int | None = Field(default=None)
    cameras: list[CameraSchema]


# ---------------------------------------------------------------------------
# In-memory hot data classes (immutable — swap, don't mutate).
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HotCameraCfg:
    """Per-camera fields reloaded at Tier A (read per frame) or B (reconnect)."""

    mtx_path: str
    source_rtsp: str
    tenant_id: int
    camera_id: int
    min_score: float
    fps: int
    classes: tuple[str, ...]
    roi: tuple[tuple[float, float], ...]
    motion_enabled: bool
    motion_threshold: int
    motion_contour_area: int
    motion_lightning_threshold: float
    masks: tuple[tuple[tuple[float, float], ...], ...]  # tuple of polygons
    zones: tuple[HotZoneCfg, ...]
    pipeline: tuple[HotPipelineStage, ...]

    @classmethod
    def from_schema(cls, schema: CameraSchema) -> HotCameraCfg:
        return cls(
            mtx_path=schema.mtx_path,
            source_rtsp=schema.source_rtsp,
            tenant_id=schema.tenant_id,
            camera_id=schema.camera_id,
            min_score=schema.detect.min_score,
            fps=schema.detect.fps,
            classes=tuple(schema.detect.classes),
            roi=tuple(tuple(p) for p in schema.detect.roi),
            motion_enabled=schema.motion.enabled,
            motion_threshold=schema.motion.threshold,
            motion_contour_area=schema.motion.contour_area,
            motion_lightning_threshold=schema.motion.lightning_threshold,
            masks=tuple(
                tuple(_validate_polygon("mask", m.coordinates))
                for m in schema.masks
            ),
            zones=tuple(HotZoneCfg.from_schema(z) for z in schema.zones),
            pipeline=tuple(HotPipelineStage.from_schema(s) for s in schema.pipeline),
        )


@dataclass(frozen=True, slots=True)
class HotZoneCfg:
    """Hot-reloadable view of one zone."""

    id: str
    friendly_name: str
    objects: tuple[str, ...]
    inertia: int
    coordinates: tuple[tuple[float, float], ...]

    @classmethod
    def from_schema(cls, schema: ZoneSchema) -> HotZoneCfg:
        return cls(
            id=schema.id,
            friendly_name=schema.friendly_name,
            objects=tuple(schema.objects),
            inertia=schema.inertia,
            coordinates=_validate_polygon("zone", schema.coordinates),
        )


@dataclass(frozen=True, slots=True)
class HotPipelineStage:
    stage: str
    trigger: str
    target_objects: tuple[str, ...]
    zone_id: str | None

    @classmethod
    def from_schema(cls, schema: PipelineStageSchema) -> HotPipelineStage:
        return cls(
            stage=schema.stage,
            trigger=schema.trigger,
            target_objects=tuple(schema.target_objects),
            zone_id=schema.zone_id,
        )


@dataclass(frozen=True, slots=True)
class HotConfig:
    """Immutable snapshot of all hot-reloadable fields. Swap, never mutate."""

    detector_url: str
    detector_kind: str
    snapshot_quality: int
    layer_suffix: str
    node_id: int | None
    mqtt_broker: str
    mqtt_topic: str
    mqtt_client_id: str
    minio_endpoint: str
    minio_secure: bool
    minio_bucket: str
    minio_access_key: str
    minio_secret_key: str
    minio_region: str
    cameras: tuple[HotCameraCfg, ...]
    cameras_by_path: dict[str, HotCameraCfg] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Pre-build lookup table so CameraLoop reads are O(1) lock-free.
        if not self.cameras_by_path:
            object.__setattr__(
                self,
                "cameras_by_path",
                {c.mtx_path: c for c in self.cameras},
            )

    def get_camera(self, mtx_path: str) -> HotCameraCfg | None:
        return self.cameras_by_path.get(mtx_path)

    def camera_paths(self) -> tuple[str, ...]:
        return tuple(c.mtx_path for c in self.cameras)

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> HotConfig:
        """Load and validate workers.yaml, returning an immutable HotConfig."""
        raw = Path(path).read_text(encoding="utf-8")
        expanded = expand_env(raw)
        data = yaml.safe_load(expanded)
        schema = GlobalSchema.model_validate(data)
        return cls.from_schema(schema)

    @classmethod
    def from_schema(cls, schema: GlobalSchema) -> HotConfig:
        cams = tuple(HotCameraCfg.from_schema(c) for c in schema.cameras)
        return cls(
            detector_url=schema.detector_url,
            detector_kind=schema.detector_kind,
            snapshot_quality=schema.snapshot_quality,
            layer_suffix=schema.layer_suffix,
            node_id=schema.node_id,
            mqtt_broker=schema.mqtt_broker,
            mqtt_topic=schema.mqtt_topic,
            mqtt_client_id=schema.mqtt_client_id,
            minio_endpoint=schema.minio_endpoint,
            minio_secure=schema.minio_secure,
            minio_bucket=schema.minio_bucket,
            minio_access_key=schema.minio_access_key,
            minio_secret_key=schema.minio_secret_key,
            minio_region=schema.minio_region,
            cameras=cams,
        )


# ---------------------------------------------------------------------------
# Atomic swap wrapper. Readers (per-frame hot path) call `.get()` lock-free;
# writers (config reloader) call `.set(new_cfg)` which swaps under a
# short Lock and bumps a monotonic version.
# ---------------------------------------------------------------------------
class HotConfigStore:
    """Thread-safe holder for the latest `HotConfig`.

    Readers in the hot path (per frame) never block; the swap is a single
    pointer assignment under a brief Lock. Version increments let the
    ConfigReloader know which tier-B fields changed (it then signals the
    affected CameraLoop to reconnect).
    """

    def __init__(self, initial: HotConfig) -> None:
        self._cfg: HotConfig = initial
        self._version: int = 0
        self._lock: Lock = Lock()

    def get(self) -> HotConfig:
        """Return the current snapshot. Lock-free, copies the ref."""
        return self._cfg

    def version(self) -> int:
        return self._version

    def set(self, new_cfg: HotConfig) -> int:
        """Swap to `new_cfg`. Returns new version."""
        with self._lock:
            self._cfg = new_cfg
            self._version += 1
            return self._version

    def paths(self) -> Iterable[str]:
        return self._cfg.camera_paths()


__all__: tuple[str, ...] = (
    "CameraSchema",
    "DetectSchema",
    "GlobalSchema",
    "HotCameraCfg",
    "HotConfig",
    "HotConfigStore",
    "HotPipelineStage",
    "HotZoneCfg",
    "MaskSchema",
    "MotionSchema",
    "PipelineStageSchema",
    "ZoneSchema",
)
