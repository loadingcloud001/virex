# SPDX-License-Identifier: AGPL-3.0
"""Frigate-style motion detector (CPU pre-filter).

Runs per-frame on each camera before any expensive GPU inference. The
worker pre-decodes each frame to a small grayscale buffer (320x180 by
default), then:

1. `cv2.absdiff(prev_gray, curr_gray)` → per-pixel delta.
2. `cv2.threshold(..., THRESH_BINARY)` → black/white mask of changed pixels.
3. `cv2.findContours(..., RETR_EXTERNAL)` → connected regions.
4. Sum the area of each contour; if any contour's area exceeds
   `contour_area`, motion is considered significant.

Returns a `MotionResult`:
- `changed`: True if any significant contour was found.
- `contour_count`: number of contours above the size threshold.
- `largest_contour`: largest contour area (px²).
- `lightning_ratio`: fraction of bright pixels in the frame; if this
  exceeds `lightning_threshold` (default 0.8) we treat the frame as a
  global brightness change (sun flare, headlight) and ignore motion
  for this frame (avoids false positives).

If `motion_enabled=False` in the camera config, the worker skips the
detector entirely (and bypasses motion gating).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass

import cv2
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults — overridden per-camera via HotConfig.
# ---------------------------------------------------------------------------
DEFAULT_RESIZE_WIDTH: int = 320
DEFAULT_RESIZE_HEIGHT: int = 180
DEFAULT_LIGHTNING_THRESHOLD: float = 0.8


@dataclass(frozen=True, slots=True)
class MotionResult:
    changed: bool
    contour_count: int
    largest_contour: int
    lightning_ratio: float  # fraction of bright (255) pixels


class MotionDetector:
    """Per-camera stateful motion detector. Cheap; pure CPU."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: int = 30,
        contour_area: int = 10,
        lightning_threshold: float = DEFAULT_LIGHTNING_THRESHOLD,
        resize_width: int = DEFAULT_RESIZE_WIDTH,
        resize_height: int = DEFAULT_RESIZE_HEIGHT,
    ) -> None:
        self._enabled = enabled
        self._threshold = threshold
        self._contour_area = contour_area
        self._lightning_threshold = lightning_threshold
        self._resize_width = resize_width
        self._resize_height = resize_height
        self._prev_gray: np.ndarray | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Allow operators to disable motion at runtime via Tier-A config."""
        self._enabled = bool(value)
        if not value:
            self.reset()

    def update(self, bgr_frame: np.ndarray) -> MotionResult:
        """Update detector state with a new BGR frame and return result.

        `bgr_frame` is the full-resolution frame from PyAV. We resize to
        ~320x180 for cheap comparison. Returned `MotionResult` says
        whether the worker should run GPU inference this frame.
        """
        if not self._enabled:
            return MotionResult(False, 0, 0, 0.0)

        if bgr_frame is None or bgr_frame.size == 0:
            logger.warning("motion_empty_frame")
            return MotionResult(False, 0, 0, 0.0)

        # Resize to small grayscale for cheap comparison.
        try:
            small = cv2.resize(
                bgr_frame,
                (self._resize_width, self._resize_height),
                interpolation=cv2.INTER_AREA,
            )
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            # Mild blur to suppress single-pixel sensor noise.
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
        except cv2.error as e:  # pragma: no cover
            logger.warning("motion_resize_failed", error=str(e))
            return MotionResult(False, 0, 0, 0.0)

        prev = self._prev_gray
        self._prev_gray = gray

        if prev is None:
            # First frame: no comparison possible, treat as unchanged.
            return MotionResult(False, 0, 0, 0.0)

        diff = cv2.absdiff(prev, gray)
        # `THRESH_BINARY` keeps pixels brighter than `threshold` as 255.
        _, mask = cv2.threshold(diff, self._threshold, 255, cv2.THRESH_BINARY)

        bright_pixels = int(cv2.countNonZero(mask))
        total_pixels = mask.shape[0] * mask.shape[1]
        lightning_ratio = bright_pixels / total_pixels if total_pixels else 0.0

        if lightning_ratio > self._lightning_threshold:
            # Whole-frame brightness change (sun glare, headlight, IR
            # cut-filter flip) — treat as "no motion" to avoid noise.
            logger.debug("motion_skipped_lightning", ratio=lightning_ratio)
            return MotionResult(False, 0, 0, lightning_ratio)

        # External contours only — internal ones are noise from camera
        # JPEG artifacts.
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return MotionResult(False, 0, 0, lightning_ratio)

        sizes = sorted((cv2.contourArea(c) for c in contours), reverse=True)
        largest = int(sizes[0])
        significant = [s for s in sizes if s >= self._contour_area]

        changed = bool(significant)
        return MotionResult(
            changed=changed,
            contour_count=len(significant),
            largest_contour=largest,
            lightning_ratio=lightning_ratio,
        )

    def reset(self) -> None:
        """Forget the previous frame (e.g. on RTSP reconnect)."""
        self._prev_gray = None

    def apply_mask(
        self,
        bgr_frame: np.ndarray,
        masks: Iterable[tuple[tuple[float, float], ...]],
    ) -> np.ndarray:
        """Erase polygons (set to BLACK) before motion detection.

        `masks` is an iterable of normalised-xy polygons; we convert to
        pixel coordinates and `cv2.fillPoly(BLACK)`. Returns a new frame;
        the original is not modified.
        """
        if not masks:
            return bgr_frame
        h, w = bgr_frame.shape[:2]
        out = bgr_frame.copy()
        for polygon in masks:
            if len(polygon) < 3:
                continue
            pts = np.array(
                [(int(x * w), int(y * h)) for (x, y) in polygon],
                dtype=np.int32,
            )
            with contextlib.suppress(cv2.error):
                cv2.fillPoly(out, [pts], color=(0, 0, 0))
        return out

    def close(self) -> None:
        """Release state."""
        self._prev_gray = None


__all__: tuple[str, ...] = (
    "DEFAULT_RESIZE_WIDTH",
    "DEFAULT_RESIZE_HEIGHT",
    "DEFAULT_LIGHTNING_THRESHOLD",
    "MotionResult",
    "MotionDetector",
)
