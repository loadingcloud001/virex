# SPDX-License-Identifier: Apache-2.0
"""Tests for hot-reload config plumbing.

`config_hot` tests focus on the immutable-swap invariant: per-frame
readers never see a half-applied change.

`config_reloader` tests focus on the diff classifier (which fields are
Tier A vs Tier B) and on Reloader.poll_once atomicity.
"""

from __future__ import annotations

import asyncio
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from worker.config_hot import HotCameraCfg, HotConfig, HotConfigStore, HotPipelineStage
from worker.config_reloader import Reloader, diff_configs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_yaml(tmp: Path, body: str) -> Path:
    """Write a workers.yaml fixture (env expansion in this file uses os.environ)."""
    p = tmp / "workers.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


def _hot(
    fps: int = 5,
    min_score: float = 0.5,
    classes=("person",),
    source_rtsp: str = "",
    motion_enabled: bool = True,
) -> HotConfig:
    """Build a tiny HotConfig in-memory with sensible defaults."""
    return HotConfig(
        detector_url="http://127.0.0.1:31001",
        detector_kind="fastapi",
        snapshot_quality=80,
        layer_suffix="h264",
        node_id=1,
        mqtt_broker="127.0.0.1:1883",
        mqtt_topic="virex/detections",
        mqtt_client_id="virex-worker",
        minio_endpoint="cos.example.com",
        minio_secure=True,
        minio_bucket="virex",
        minio_access_key="AK",
        minio_secret_key="SK",
        minio_region="us-east-1",
        cameras=(
            HotCameraCfg(
                mtx_path="t1c5h264",
                source_rtsp=source_rtsp,
                tenant_id=1,
                camera_id=5,
                min_score=min_score,
                fps=fps,
                classes=classes,
                roi=(),
                motion_enabled=motion_enabled,
                motion_threshold=30,
                motion_contour_area=10,
                motion_lightning_threshold=0.8,
                masks=(),
                zones=(),
                pipeline=(
                    HotPipelineStage(
                        stage="detect", trigger="always",
                        target_objects=(), zone_id=None,
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# HotConfig / HotConfigStore
# ---------------------------------------------------------------------------
def test_hotconfig_is_immutable() -> None:
    """`HotConfig` and `HotCameraCfg` use `frozen=True`."""
    from dataclasses import FrozenInstanceError

    cfg = _hot()
    with pytest.raises(FrozenInstanceError):
        cfg.snapshot_quality = 90  # type: ignore[misc]


def test_hotconfig_lookup_by_path_is_constant_time() -> None:
    cfg = _hot()
    assert cfg.get_camera("t1c5h264") is not None
    assert cfg.get_camera("nope") is None
    assert cfg.camera_paths() == ("t1c5h264",)


def test_hotconfigstore_swap_is_atomic() -> None:
    """A reader mid-frame always sees a coherent snapshot."""
    store = HotConfigStore(_hot(fps=5))
    assert store.version() == 0
    assert store.get().get_camera("t1c5h264").fps == 5

    store.set(_hot(fps=10))
    assert store.version() == 1
    assert store.get().get_camera("t1c5h264").fps == 10

    # Concurrent reads are lock-free — simulate with many reads during
    # multiple swaps. The contract: a read returns a coherent cfg each time.
    for _ in range(50):
        store.set(_hot(fps=15 if store.version() % 2 else 25))
        snap = store.get()
        assert snap.get_camera("t1c5h264").fps in (15, 25)


# ---------------------------------------------------------------------------
# diff_configs
# ---------------------------------------------------------------------------
def test_diff_no_changes_returns_empty_report() -> None:
    a = _hot()
    b = _hot()
    r = diff_configs(a, b)
    assert not r.has_changes
    assert r.tier_a == ()
    assert r.tier_b_global == ()
    assert r.tier_b_per_camera == {}


def test_diff_fps_is_tier_a_only() -> None:
    a = _hot(fps=5)
    b = _hot(fps=10)
    r = diff_configs(a, b)
    assert "t1c5h264.fps" in r.tier_a
    assert r.tier_b_per_camera == {}


def test_diff_min_score_classes_roi_are_tier_a() -> None:
    a = _hot()
    r1 = diff_configs(a, _hot(min_score=0.7))
    assert "t1c5h264.min_score" in r1.tier_a
    r2 = diff_configs(a, _hot(classes=("person", "car")))
    assert "t1c5h264.classes" in r2.tier_a
    r3 = diff_configs(
        a,
        HotConfig(
            **{**{k: getattr(a, k) for k in (
                "detector_url", "detector_kind",
                "snapshot_quality", "layer_suffix", "node_id",
                "mqtt_broker", "mqtt_topic", "mqtt_client_id",
                "minio_endpoint", "minio_secure", "minio_bucket",
                "minio_access_key", "minio_secret_key", "minio_region",
            )},
              "cameras": (HotCameraCfg(
                  mtx_path="t1c5h264", source_rtsp="", tenant_id=1, camera_id=5,
                  min_score=0.5, fps=5, classes=("person",),
                  roi=((0.0, 0.0), (1.0, 1.0)),
                  motion_enabled=True, motion_threshold=30,
                  motion_contour_area=10, motion_lightning_threshold=0.8,
                  masks=(), zones=(),
                  pipeline=(HotPipelineStage(
                      stage="detect", trigger="always",
                      target_objects=(), zone_id=None,
                  ),),
              ),)}
        ),
    )
    assert "t1c5h264.roi" in r3.tier_a


def test_diff_source_rtsp_is_tier_b_per_camera() -> None:
    a = _hot(source_rtsp="")
    b = _hot(source_rtsp="rtsp://new@10.0.0.5/Streaming/channels/0501")
    r = diff_configs(a, b)
    assert "t1c5h264" in r.tier_b_per_camera
    assert "tier_b_source_rtsp" in r.tier_b_per_camera["t1c5h264"]
    # Tier A unchanged
    assert r.tier_a == ()


def test_diff_mqtt_change_is_tier_b_global() -> None:
    a = _hot()
    b = HotConfig(
        **{**{k: getattr(a, k) for k in (
            "detector_url", "detector_kind",
            "snapshot_quality", "layer_suffix", "node_id",
            "mqtt_topic", "mqtt_client_id",
            "minio_endpoint", "minio_secure", "minio_bucket",
            "minio_access_key", "minio_secret_key", "minio_region",
        )},
          "mqtt_broker": "192.168.0.1:1883",
          "cameras": a.cameras}
    )
    r = diff_configs(a, b)
    assert "mqtt_broker" in r.tier_b_global
    assert "tier_b_mqtt" in r.tier_b_global


def test_diff_minio_change_is_tier_b_global() -> None:
    a = _hot()
    b = HotConfig(
        **{**{k: getattr(a, k) for k in (
            "detector_url", "detector_kind",
            "snapshot_quality", "layer_suffix", "node_id",
            "mqtt_broker", "mqtt_topic", "mqtt_client_id",
            "minio_secure", "minio_bucket", "minio_access_key",
            "minio_secret_key", "minio_region",
        )},
          "minio_endpoint": "cos.ap-singapore.myqcloud.com",
          "cameras": a.cameras}
    )
    r = diff_configs(a, b)
    assert "tier_b_minio" in r.tier_b_global


def test_diff_ignores_added_camera() -> None:
    """New cameras are Tier D (reconcile), out of scope for worker hot-reload."""
    a = _hot()
    new_cam = HotCameraCfg(
        mtx_path="t1c6h264", source_rtsp="", tenant_id=1, camera_id=6,
        min_score=0.5, fps=5, classes=("person",), roi=(),
        motion_enabled=True, motion_threshold=30,
        motion_contour_area=10, motion_lightning_threshold=0.8,
        masks=(), zones=(),
        pipeline=(HotPipelineStage(
            stage="detect", trigger="always",
            target_objects=(), zone_id=None,
        ),),
    )
    b = HotConfig(
        **{**{k: getattr(a, k) for k in (
            "detector_url", "detector_kind",
            "snapshot_quality", "layer_suffix", "node_id",
            "mqtt_broker", "mqtt_topic", "mqtt_client_id",
            "minio_endpoint", "minio_secure", "minio_bucket",
            "minio_access_key", "minio_secret_key", "minio_region",
        )},
          "cameras": a.cameras + (new_cam,)}
    )
    r = diff_configs(a, b)
    # The new camera should NOT appear in tier_b_per_camera.
    assert r.tier_b_per_camera == {}


# ---------------------------------------------------------------------------
# Reloader
# ---------------------------------------------------------------------------
def test_hotconfig_from_yaml_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_OVERRIDE", "192.168.0.5:1883")
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "${MQTT_OVERRIDE}"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 5
            record: true
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    cfg = HotConfig.from_yaml(yaml_path)
    assert cfg.mqtt_broker == "192.168.0.5:1883"
    assert cfg.cameras[0].fps == 5


@dataclass
class _FakeCamera:
    path: str
    reconnects: int = 0

    def request_reconnect(self) -> None:
        self.reconnects += 1


@dataclass
class _Loops:
    d: dict[str, _FakeCamera] = field(default_factory=dict)


def test_reloader_yaml_save_no_changes_skips_apply(tmp_path: Path) -> None:
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 5
            record: true
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loop = _FakeCamera("t1c5h264")
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={"t1c5h264": loop},  # type: ignore[arg-type]
        poll_interval_sec=0.1,
    )

    async def go() -> None:
        r1 = await reloader.poll_once()
        # First poll reads the file → no prior version → no diff applied
        assert r1 is not None
        assert loop.reconnects == 0
        # Same mtime → no re-read
        r2 = await reloader.poll_once()
        assert r2 is None

    asyncio.run(go())


def test_reloader_fps_change_applies_tier_a_atomically(tmp_path: Path) -> None:
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 5
            record: true
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loop = _FakeCamera("t1c5h264")
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={"t1c5h264": loop},  # type: ignore[arg-type]
        poll_interval_sec=0.1,
    )

    async def go() -> None:
        # First poll loads the file.
        await reloader.poll_once()
        v0 = store.version()
        # Save new file with fps=10. Need a different mtime — uvloop's
        # monotonic time may not advance on quick successive writes, so
        # we sleep briefly between saves.
        await asyncio.sleep(0.05)
        yaml_path.write_text(textwrap.dedent("""
            detector_url: "http://127.0.0.1:31001"
            mqtt_broker: "127.0.0.1:1883"
            mqtt_topic: "virex/detections"
            snapshot_quality: 80
            layer_suffix: "h264"
            node_id: 1
            cameras:
              - mtx_path: "t1c5h264"
                source_rtsp: ""
                tenant_id: 1
                camera_id: 5
                record: true
                detect:
                  fps: 10
                  classes: ["person"]
                  min_score: 0.5
                  roi: []
        """).lstrip(), encoding="utf-8")
        await asyncio.sleep(0.05)
        r = await reloader.poll_once()
        assert r is not None
        assert store.version() > v0
        assert "t1c5h264.fps" in r.tier_a
        # FPS is Tier A — NO reconnect requested.
        assert loop.reconnects == 0
        # The new cfg is visible immediately.
        assert store.get().get_camera("t1c5h264").fps == 10

    asyncio.run(go())


def test_reloader_rtsp_change_requests_reconnect(tmp_path: Path) -> None:
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: "rtsp://old@10.0.0.5/Streaming/channels/0501"
            tenant_id: 1
            camera_id: 5
            record: true
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loop = _FakeCamera("t1c5h264")
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={"t1c5h264": loop},  # type: ignore[arg-type]
        poll_interval_sec=0.1,
    )

    async def go() -> None:
        await reloader.poll_once()
        await asyncio.sleep(0.05)
        yaml_path.write_text(textwrap.dedent("""
            detector_url: "http://127.0.0.1:31001"
            mqtt_broker: "127.0.0.1:1883"
            mqtt_topic: "virex/detections"
            snapshot_quality: 80
            layer_suffix: "h264"
            node_id: 1
            cameras:
              - mtx_path: "t1c5h264"
                source_rtsp: "rtsp://new@10.0.0.5/Streaming/channels/0501"
                tenant_id: 1
                camera_id: 5
                record: true
                detect:
                  fps: 5
                  classes: ["person"]
                  min_score: 0.5
                  roi: []
        """).lstrip(), encoding="utf-8")
        await asyncio.sleep(0.05)
        r = await reloader.poll_once()
        assert r is not None
        assert "t1c5h264" in r.tier_b_per_camera
        assert "tier_b_source_rtsp" in r.tier_b_per_camera["t1c5h264"]
        # Tier B DID request a reconnect.
        assert loop.reconnects == 1

    asyncio.run(go())


def test_reloader_invalid_yaml_does_not_swap(tmp_path: Path) -> None:
    """Operator typo → reloader logs error, does NOT swap; keeps last-known-good."""
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    initial = HotConfig.from_yaml(yaml_path)
    store = HotConfigStore(initial)
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={},
        poll_interval_sec=0.1,
    )

    async def go() -> None:
        await reloader.poll_once()
        v0 = store.version()
        await asyncio.sleep(0.05)
        yaml_path.write_text("not: valid: yaml: at all:\n  - oops\n", encoding="utf-8")
        await asyncio.sleep(0.05)
        r = await reloader.poll_once()
        assert r is not None
        assert r.error is not None
        # Version unchanged because the swap did not happen.
        assert store.version() == v0
        # Last-known-good stays live.
        assert store.get() is initial

    asyncio.run(go())


def test_reloader_rollback_restores_previous(tmp_path: Path) -> None:
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_topic: "virex/detections"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={},
        poll_interval_sec=0.1,
    )

    async def go() -> None:
        r1 = await reloader.poll_once()
        assert r1 is not None
        # Change fps
        await asyncio.sleep(0.05)
        yaml_path.write_text(textwrap.dedent("""
            detector_url: "http://127.0.0.1:31001"
            mqtt_broker: "127.0.0.1:1883"
            mqtt_topic: "virex/detections"
            snapshot_quality: 80
            layer_suffix: "h264"
            node_id: 1
            cameras:
              - mtx_path: "t1c5h264"
                tenant_id: 1
                camera_id: 5
                detect:
                  fps: 15
                  classes: ["person"]
                  min_score: 0.5
                  roi: []
        """).lstrip(), encoding="utf-8")
        await asyncio.sleep(0.05)
        r2 = await reloader.poll_once()
        assert r2 is not None
        assert store.get().get_camera("t1c5h264").fps == 15
        # Rollback
        rr = await reloader.rollback()
        assert rr is not None
        assert store.get().get_camera("t1c5h264").fps == 5

    asyncio.run(go())
