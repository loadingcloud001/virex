# SPDX-License-Identifier: Apache-2.0
"""Tests for `src.tier_classifier` — pure-function diff logic.

We deliberately test only the pure classifier (no I/O), so these tests
are fast and run on every CI tick. The DiffReport is fed by
`config_watcher` and consumed by `reconcile.apply_tier_report`; both
of those modules have their own tests.
"""

from __future__ import annotations

from src.config_pull import CameraEdgeDTO, DetectParamsDTO, EdgeConfigBundle
from src.tier_classifier import (
    TierReport,
    cameras_to_reload,
    classify,
    tier_requires_worker_reload,
)


def _cam(
    mtx_path: str = "t1c5h264",
    source_rtsp: str = "",
    *,
    fps: int = 5,
    min_score: float = 0.5,
    classes=("person",),
    record: bool = True,
    tenant_id: int = 1,
    camera_id: int = 5,
) -> CameraEdgeDTO:
    return CameraEdgeDTO(
        mtx_path=mtx_path,
        source_rtsp=source_rtsp,
        tenant_id=tenant_id,
        camera_id=camera_id,
        detect=DetectParamsDTO(fps=fps, classes=list(classes), min_score=min_score, roi=[]),
        record=record,
    )


def _bundle(cameras: list[CameraEdgeDTO], version: int = 1) -> EdgeConfigBundle:
    return EdgeConfigBundle(node_id=1, config_version=version, cameras=cameras)


# ---------------------------------------------------------------------------
# First reconcile (old is None)
# ---------------------------------------------------------------------------
def test_first_reconcile_marks_everything_as_added() -> None:
    bundle = _bundle([_cam()])
    r = classify(None, bundle)
    assert r.added == (bundle.cameras[0],)
    assert r.removed == ()
    assert not r.renamed
    assert not r.tier_a_per_camera
    assert not r.tier_b_per_camera
    assert not r.tier_c_paths
    # No diffs implied — first reconcile becomes a Tier-D full reconcile.
    assert r.has_changes


# ---------------------------------------------------------------------------
# Tier-A: per-camera detection params
# ---------------------------------------------------------------------------
def test_fps_min_score_classes_roi_are_tier_a() -> None:
    old = _bundle([_cam(fps=5, min_score=0.5)])
    new = _bundle([_cam(fps=10, min_score=0.5)])
    r = classify(old, new)
    assert r.tier_a_per_camera.get("t1c5h264") == ("detect.fps",)
    assert r.tier_b_per_camera == {}
    assert r.tier_c_paths == ()


def test_multiple_tier_a_changes_for_one_camera() -> None:
    old = _bundle([_cam(fps=5, min_score=0.5, classes=("person",))])
    new = _bundle([_cam(fps=10, min_score=0.6, classes=("person", "car"))])
    r = classify(old, new)
    fields = r.tier_a_per_camera["t1c5h264"]
    assert "detect.fps" in fields
    assert "detect.min_score" in fields
    assert "detect.classes" in fields


# ---------------------------------------------------------------------------
# Tier-B: per-camera source_rtsp
# ---------------------------------------------------------------------------
def test_source_rtsp_change_is_tier_b() -> None:
    old = _bundle([_cam(source_rtsp="")])
    new = _bundle([_cam(source_rtsp="rtsp://new@10.0.0.5/Streaming/channels/0501")])
    r = classify(old, new)
    assert r.tier_b_per_camera == {"t1c5h264": ("source_rtsp",)}
    # Tier A untouched
    assert r.tier_a_per_camera == {}


# ---------------------------------------------------------------------------
# Tier-C: record flag
# ---------------------------------------------------------------------------
def test_record_change_is_tier_c() -> None:
    old = _bundle([_cam(record=True)])
    new = _bundle([_cam(record=False)])
    r = classify(old, new)
    assert r.tier_c_paths == ("t1c5h264",)
    # Record toggle is Tier C only.
    assert r.tier_a_per_camera == {}
    assert r.tier_b_per_camera == {}


# ---------------------------------------------------------------------------
# Tier-D: add / remove camera
# ---------------------------------------------------------------------------
def test_added_camera_is_tier_d() -> None:
    old = _bundle([_cam()])
    new = _bundle([_cam(), _cam(mtx_path="t1c6h264", tenant_id=1, camera_id=6)])
    r = classify(old, new)
    assert len(r.added) == 1
    assert r.added[0].mtx_path == "t1c6h264"
    assert r.removed == ()
    assert not r.tier_a_per_camera
    assert not r.tier_b_per_camera


def test_removed_camera_is_tier_d() -> None:
    old = _bundle([_cam(), _cam(mtx_path="t1c6h264", tenant_id=1, camera_id=6)])
    new = _bundle([_cam()])
    r = classify(old, new)
    assert r.removed == ("t1c6h264",)
    assert r.added == ()


def test_no_changes_means_empty_report() -> None:
    old = _bundle([_cam()])
    new = _bundle([_cam()])
    r = classify(old, new)
    assert not r.has_changes
    assert r.added == ()
    assert r.removed == ()


# ---------------------------------------------------------------------------
# Mixed: same bundle going through Tier A + Tier D
# ---------------------------------------------------------------------------
def test_mixed_tier_a_and_d_in_one_diff() -> None:
    old = _bundle([_cam(fps=5)])
    new = _bundle([_cam(fps=10), _cam(mtx_path="t1c6h264", tenant_id=1, camera_id=6)])
    r = classify(old, new)
    assert "t1c5h264.fps" not in r.tier_a_per_camera.get("t1c5h264", ())
    assert "detect.fps" in r.tier_a_per_camera.get("t1c5h264", ())
    assert len(r.added) == 1
    assert r.added[0].mtx_path == "t1c6h264"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def test_tier_requires_worker_reload_when_a_or_b_present() -> None:
    a_only = TierReport(tier_a_per_camera={"t1c5h264": ("detect.fps",)})
    b_only = TierReport(tier_b_per_camera={"t1c5h264": ("source_rtsp",)})
    neither = TierReport(tier_c_paths=("t1c5h264",))
    assert tier_requires_worker_reload(a_only)
    assert tier_requires_worker_reload(b_only)
    assert not tier_requires_worker_reload(neither)


def test_cameras_to_reload_unions_a_and_b() -> None:
    report = TierReport(
        tier_a_per_camera={"t1c5h264": ("detect.fps",)},
        tier_b_per_camera={"t1c6h264": ("source_rtsp",)},
    )
    # Order isn't important; both paths should be present.
    paths = set(cameras_to_reload(report))
    assert paths == {"t1c5h264", "t1c6h264"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_empty_old_and_new_bundles_no_changes() -> None:
    old = _bundle([])
    new = _bundle([])
    assert not classify(old, new).has_changes


def test_renamed_is_detected_by_mtx_path_mismatch() -> None:
    """If the portal changes a camera's mtx_path, classify treats it as Tier D
    (remove + add). Real renames aren't a single-shot wire operation;
    the camera ID could legitimately be the same."""
    old = _bundle([_cam(mtx_path="t1c5h264")])
    new = _bundle([_cam(mtx_path="t1c5h264_v2")])
    r = classify(old, new)
    # Classifier detects this as old-removed + new-added (Tier D), not a rename.
    assert r.removed == ("t1c5h264",)
    assert len(r.added) == 1
    assert r.added[0].mtx_path == "t1c5h264_v2"
