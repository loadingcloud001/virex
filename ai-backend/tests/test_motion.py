# SPDX-License-Identifier: AGPL-3.0
"""Tests for the cv2 motion detector (Frigate-style pre-filter)."""

from __future__ import annotations

import numpy as np

from worker.motion import (
    DEFAULT_LIGHTNING_THRESHOLD,
    DEFAULT_RESIZE_HEIGHT,
    DEFAULT_RESIZE_WIDTH,
    MotionDetector,
    MotionResult,
)


def _frame(value: int = 100, size: tuple[int, int] = (640, 360)) -> np.ndarray:
    """Build a solid-colour BGR frame."""
    return np.full((size[1], size[0], 3), value, dtype=np.uint8)


def _frame_with_blob(
    color_bg: int = 100,
    blob_color: int = 200,
    blob_top_left: tuple[int, int] = (100, 80),
    blob_size: tuple[int, int] = (40, 80),
    size: tuple[int, int] = (640, 360),
) -> np.ndarray:
    """BGR frame with a small bright blob — simulates a moving object."""
    frame = _frame(color_bg, size)
    x0, y0 = blob_top_left
    x1 = min(x0 + blob_size[0], size[0])
    y1 = min(y0 + blob_size[1], size[1])
    frame[y0:y1, x0:x1] = blob_color
    return frame


# Backwards-compat alias (so the body uses the same name as the tests do).
def _solid(value: int = 100, size: tuple[int, int] = (640, 360)) -> np.ndarray:
    return _frame(value=value, size=size)


def test_default_constants_sane() -> None:
    assert DEFAULT_RESIZE_WIDTH == 320
    assert DEFAULT_RESIZE_HEIGHT == 180
    assert 0.0 < DEFAULT_LIGHTNING_THRESHOLD < 1.0


def test_disabled_motion_returns_no_change() -> None:
    """`enabled=False` short-circuits — every frame is unchanged."""
    det = MotionDetector(enabled=False)
    f2 = _frame_with_blob()
    r = det.update(f2)
    assert r.changed is False
    assert r.contour_count == 0


def test_first_frame_returns_no_change() -> None:
    """The first frame after construction has no `prev_gray` to compare."""
    det = MotionDetector()
    r = det.update(_frame_with_blob())
    assert r.changed is False


def test_static_frames_return_no_change() -> None:
    """Two identical frames → no contours above threshold."""
    det = MotionDetector()
    det.update(_frame(100))
    r = det.update(_frame(100))
    assert r.changed is False
    assert r.contour_count == 0


def test_motion_detected_when_blob_moves() -> None:
    """A frame where the blob moves → motion is detected."""
    det = MotionDetector(contour_area=5, threshold=20)
    det.update(_frame_with_blob(blob_top_left=(100, 80)))
    # Same colour, blob moved to the right by 20 px.
    r = det.update(_frame_with_blob(blob_top_left=(120, 80)))
    assert r.changed is True
    assert r.contour_count >= 1


def test_lightning_filter_rejects_global_brightness_change() -> None:
    """Whole-frame brightness flip (sun glare, IR cut) is suppressed."""
    det = MotionDetector(contour_area=5, threshold=20, lightning_threshold=0.5)
    det.update(_frame(0))   # black
    r = det.update(_frame(255))  # suddenly all-white
    assert r.changed is False
    assert r.lightning_ratio > 0.5


def test_motion_can_be_disabled_via_property() -> None:
    det = MotionDetector(enabled=True)
    det.update(_frame_with_blob())
    det.enabled = False
    r = det.update(_frame_with_blob())
    assert r.changed is False


def test_reset_clears_prev_frame() -> None:
    det = MotionDetector()
    det.update(_frame_with_blob(blob_top_left=(100, 80)))
    det.reset()
    # After reset, next frame has no previous to compare against.
    r = det.update(_frame_with_blob(blob_top_left=(120, 80)))
    assert r.changed is False


def test_apply_mask_zeros_polygon_region() -> None:
    """`apply_mask()` zeros the polygon region with cv2.fillPoly."""
    det = MotionDetector()
    frame = _frame(value=200)
    masks = (((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)),)
    masked = det.apply_mask(frame, masks)
    assert masked.shape == frame.shape
    # Inside the polygon (top-left quadrant): should be 0.
    assert (masked[:180, :320] == 0).all()
    # Outside the polygon (bottom-right): unchanged.
    assert (masked[200:, 350:] == 200).all()


def test_apply_mask_no_policies_returns_same_frame() -> None:
    det = MotionDetector()
    frame = _frame()
    masked = det.apply_mask(frame, [])
    # Even with no masks, we copy the frame so callers can safely
    # treat `masked` as their own.
    assert (masked == frame).all()


def test_small_motion_below_contour_area_is_ignored() -> None:
    """A tiny blob moves by 1 px → no contour above `contour_area`."""
    det = MotionDetector(contour_area=2000, threshold=20)
    det.update(_frame_with_blob(blob_top_left=(100, 80)))
    r = det.update(_frame_with_blob(blob_top_left=(101, 80)))
    assert r.changed is False


def test_motion_result_dataclass_fields() -> None:
    r = MotionResult(
        changed=True, contour_count=3, largest_contour=500, lightning_ratio=0.1
    )
    assert r.changed is True
    assert r.contour_count == 3
    assert r.largest_contour == 500
    assert r.lightning_ratio == 0.1
