# SPDX-License-Identifier: Apache-2.0
"""`workers.yaml` watcher that drives Tier-A/B reloads inside the worker.

The reloader runs as an asyncio task alongside `run_all(cameras)`. Every
`poll_interval_sec` seconds (default 5 s) it stats the file; if mtime
changed it re-parses, diffs against the previous `HotConfig` snapshot,
and:

* swaps `HotConfigStore.set(new_cfg)` (Tier-A fields update atomically,
  no main-loop lock) → CameraLoop reads the new fps/min_score/classes
  on its next frame.
* for Tier-B fields (source_rtsp change, mqtt_broker/endpoint change),
  posts a sentinel `None` to each affected `CameraLoop._queue` so
  `run()` breaks out of `_loop_once` (PyAV container closes + reopens
  with the new RTSP URL on the next iteration).
* keeps `_previous_cfg` so `admin.rollback()` can revert instantly.

If the YAML is malformed (operator typo), Pydantic raises and the
reloader logs an error WITHOUT swapping — last-known-good stays live.
That mirrors Frigate's "validate, then apply" pattern.

The HTTP-driven entrypoint (`worker/admin.py`) calls `apply_now()` to
trigger an immediate reload without waiting for the next poll tick —
used by edge-agent's `POST /api/internal/edge/<id>/apply`.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from worker.config_hot import (
    HotConfig,
    HotConfigStore,
)

if TYPE_CHECKING:
    from worker.camera_worker import CameraLoop

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Change classification — re-exported so `admin.py` can return it as JSON.
# Mirror of edge-agent tier model but per-process; the per-camera RTSP
# URL change is what triggers a sentinel-post to that camera's queue.
# ---------------------------------------------------------------------------
class ChangeKind(enum.StrEnum):
    TIER_A_FPS = "tier_a_fps"
    TIER_A_MIN_SCORE = "tier_a_min_score"
    TIER_A_CLASSES = "tier_a_classes"
    TIER_A_ROI = "tier_a_roi"
    TIER_A_SNAPSHOT_QUALITY = "tier_a_snapshot_qual"
    TIER_B_SOURCE_RTSP = "tier_b_source_rtsp"
    TIER_B_MQTT = "tier_b_mqtt"
    TIER_B_MINIO = "tier_b_minio"


@dataclass(frozen=True, slots=True)
class ReloadReport:
    """Summary of what changed during one reload cycle."""

    version: int
    tier_a: tuple[str, ...]
    tier_b_per_camera: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tier_b_global: tuple[str, ...] = ()
    error: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.tier_a or self.tier_b_per_camera or self.tier_b_global)


def diff_configs(
    old: HotConfig,
    new: HotConfig,
) -> ReloadReport:
    """Pure function: classify each difference between two HotConfig snapshots.

    Tier-A fields are atomic ref swaps; we don't need per-field granularity
    for that, but listing them helps observability. Per-camera Tier-B
    changes (RTSP URL) drive the sentinel-post to that camera's queue.
    """
    tier_a: list[str] = []
    tier_b_global: list[str] = []
    tier_b_per_camera: dict[str, list[str]] = {}

    # ----- global -----
    if old.snapshot_quality != new.snapshot_quality:
        tier_a.append(ChangeKind.TIER_A_SNAPSHOT_QUALITY.value)

    def _mqtt_diff(o: HotConfig, n: HotConfig) -> list[str]:
        out: list[str] = []
        if o.mqtt_broker != n.mqtt_broker:
            out.append("mqtt_broker")
        if o.mqtt_topic != n.mqtt_topic:
            out.append("mqtt_topic")
        return out

    def _minio_diff(o: HotConfig, n: HotConfig) -> list[str]:
        out: list[str] = []
        if o.minio_endpoint != n.minio_endpoint:
            out.append("minio_endpoint")
        if o.minio_secure != new.minio_secure:
            out.append("minio_secure")
        if o.minio_bucket != n.minio_bucket:
            out.append("minio_bucket")
        if o.minio_access_key != n.minio_access_key:
            out.append("minio_access_key")
        if o.minio_secret_key != n.minio_secret_key:
            out.append("minio_secret_key")
        if o.minio_region != n.minio_region:
            out.append("minio_region")
        return out

    tier_b_global.extend(_mqtt_diff(old, new))
    if any(_minio_diff(old, new)):
        tier_b_global.append(ChangeKind.TIER_B_MINIO.value)
    if _mqtt_diff(old, new):
        tier_b_global.append(ChangeKind.TIER_B_MQTT.value)

    # ----- per-camera -----
    old_by_path = old.cameras_by_path
    new_by_path = new.cameras_by_path

    for path, new_cam in new_by_path.items():
        old_cam = old_by_path.get(path)
        if old_cam is None:
            # New camera — that's Tier D, handled by edge-agent reconcile,
            # not by the worker. The hot-reloader ignores additions.
            continue
        tier_b_for_path: list[str] = []
        if old_cam.source_rtsp != new_cam.source_rtsp:
            tier_b_for_path.append(ChangeKind.TIER_B_SOURCE_RTSP.value)
        if old_cam.fps != new_cam.fps:
            tier_a.append(f"{path}.fps")
        if old_cam.min_score != new_cam.min_score:
            tier_a.append(f"{path}.min_score")
        if old_cam.classes != new_cam.classes:
            tier_a.append(f"{path}.classes")
        if old_cam.roi != new_cam.roi:
            tier_a.append(f"{path}.roi")
        if tier_b_for_path:
            tier_b_per_camera[path] = tier_b_for_path

    return ReloadReport(
        version=-1,  # filled by caller
        tier_a=tuple(tier_a),
        tier_b_per_camera={k: tuple(v) for k, v in tier_b_per_camera.items()},
        tier_b_global=tuple(set(tier_b_global)),
    )


# ---------------------------------------------------------------------------
# The reloader task — polls mtime, applies diffs, posts sentinels.
# ---------------------------------------------------------------------------
@dataclass
class Reloader:
    """Async task that polls workers.yaml and applies Tier A/B reloads."""

    yaml_path: str
    store: HotConfigStore
    camera_loops: dict[str, CameraLoop]
    poll_interval_sec: float = 5.0

    def __post_init__(self) -> None:
        self._last_mtime_ns: int | None = None
        self._last_content_hash: str | None = None
        self._last_yaml_text: str | None = None
        self._running: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._previous_cfg: HotConfig | None = None
        self._consecutive_validation_errors: int = 0

    def attach(self, mtx_path: str, camera_loop: CameraLoop) -> None:
        """Called by main.py after spawning each CameraLoop."""
        self.camera_loops[mtx_path] = camera_loop

    async def run(self) -> None:
        """Poll loop. Cancel cleanly on shutdown."""
        self._running = True
        logger.info(
            "config_reloader_started",
            path=self.yaml_path,
            poll_interval_sec=self.poll_interval_sec,
        )
        while self._running:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("config_reloader_poll_failed")
            await asyncio.sleep(self.poll_interval_sec)

    def stop(self) -> None:
        self._running = False

    async def poll_once(self) -> ReloadReport | None:
        """Single poll: stat, reload if mtime changed. Idempotent.

        Detection uses mtime AND content-hash to defeat Docker bind-mount
        page-cache staleness — `stat()` may return the same mtime after
        a host-side edit while `open()` reads fresh content. We track the
        last read payload's hash and reload when either signal flips.
        """
        async with self._lock:
            try:
                p = Path(self.yaml_path)
                st = p.stat()
            except FileNotFoundError:
                logger.warning("yaml_missing", path=self.yaml_path)
                return None

            # Always read the file (cheap; small YAML) and hash it. If
            # either the mtime OR the content hash differs from what we
            # last saw, treat the file as changed.
            raw_bytes = p.read_bytes()
            content_hash = hashlib.sha256(raw_bytes).hexdigest()

            if (
                self._last_mtime_ns is not None
                and st.st_mtime_ns == self._last_mtime_ns
                and content_hash == self._last_content_hash
            ):
                return None

            self._last_mtime_ns = st.st_mtime_ns
            self._last_content_hash = content_hash
            try:
                new_cfg = HotConfig.from_yaml(p)
            except Exception as e:  # noqa: BLE001
                self._consecutive_validation_errors += 1
                logger.error(
                    "yaml_validation_failed",
                    error=str(e)[:500],
                    consecutive=self._consecutive_validation_errors,
                )
                return ReloadReport(
                    version=self.store.version(),
                    tier_a=(),
                    error=str(e)[:500],
                )

            self._consecutive_validation_errors = 0
        # Lock released — do the actual apply outside the lock so
        # CameraLoop._process_frame never blocks on the reloader.

        report = self._apply(self.store.get(), new_cfg)
        report_dict = ReloadReport(
            version=self.store.version(),
            tier_a=report.tier_a,
            tier_b_per_camera=report.tier_b_per_camera,
            tier_b_global=report.tier_b_global,
        )
        if report_dict.has_changes:
            logger.info(
                "config_reloaded",
                version=report_dict.version,
                tier_a=report_dict.tier_a,
                tier_b_global=report_dict.tier_b_global,
                tier_b_per_camera=report_dict.tier_b_per_camera,
            )
        return report_dict

    async def apply_now(self) -> ReloadReport:
        """Force an apply (used by admin endpoint). Reads YAML fresh.

        Mirrors `poll_once`: on validation failure returns a report with
        `error` populated rather than raising — so the admin HTTP layer
        can respond 200 with the error field, keeping last-known-good live.
        """
        async with self._lock:
            try:
                new_cfg = HotConfig.from_yaml(self.yaml_path)
            except Exception as e:  # noqa: BLE001
                self._consecutive_validation_errors += 1
                logger.error(
                    "yaml_validation_failed", error=str(e)[:500],
                    consecutive=self._consecutive_validation_errors,
                )
                return ReloadReport(
                    version=self.store.version(),
                    tier_a=(),
                    error=str(e)[:500],
                )
            self._consecutive_validation_errors = 0
            self._last_mtime_ns = Path(self.yaml_path).stat().st_mtime_ns
        report = self._apply(self.store.get(), new_cfg)
        return report._replace(version=self.store.version())

    async def rollback(self) -> ReloadReport | None:
        """Restore the previous HotConfig (one-deep snapshot)."""
        if self._previous_cfg is None:
            return None
        prev = self._previous_cfg
        async with self._lock:
            self._apply(self.store.get(), prev)
        return ReloadReport(
            version=self.store.version(),
            tier_a=("rollback",),
        )

    # ----- internal -----
    def _apply(
        self,
        old: HotConfig,
        new: HotConfig,
    ) -> ReloadReport:
        report = diff_configs(old, new)
        if not report.has_changes:
            return report

        # 1. Save the old as the rollback target BEFORE swapping.
        self._previous_cfg = old
        # 2. Atomic swap.
        self.store.set(new)
        # 3. Tier B side-effect: signal each affected camera to reconnect.
        for path, fields in report.tier_b_per_camera.items():
            if ChangeKind.TIER_B_SOURCE_RTSP.value in fields:
                loop = self.camera_loops.get(path)
                if loop is not None:
                    loop.request_reconnect()
                    logger.info("tier_b_reconnect_signaled", path=path)
        return report


# ---------------------------------------------------------------------------
# A tiny ergonomic adder for the "version bump" path used by Reloader.
# ---------------------------------------------------------------------------
def _replace(  # type: ignore[no-untyped-def]
    self_,
    version: int,
) -> ReloadReport:
    return ReloadReport(
        version=version,
        tier_a=self_.tier_a,
        tier_b_per_camera=self_.tier_b_per_camera,
        tier_b_global=self_.tier_b_global,
    )


# Patch a `_replace` helper into ReloadReport for the apply_now path.
# Using a module-level function keeps ReloadReport frozen+slots.
ReloadReport._replace = _replace  # type: ignore[attr-defined]


__all__: tuple[str, ...] = (
    "ChangeKind",
    "ReloadReport",
    "Reloader",
    "diff_configs",
)
