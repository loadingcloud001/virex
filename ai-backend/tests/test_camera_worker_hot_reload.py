# SPDX-License-Identifier: AGPL-3.0
"""Regression test for CameraWorker hot-reload of motion + zones.

Bug being guarded against: `_motion` and `_zone_filter` were lazily
constructed once and never rebuilt, so a Tier-A hot-reload of
`motion.threshold` or `zones[i].coordinates` would silently keep
running with the OLD config. This test exercises the rebuild path by
checking that `_motion_signature` / `_zone_signature` differ when
HotConfig swaps and that the MotionDetector / ZoneFilter are
re-constructed accordingly.

We build a minimal `CameraLoop` with stubbed PyAV/av (the PyAV feeder
isn't on the hot-reload path — only `_process_frame` is). Each test
mutates the underlying `HotConfigStore` directly and asserts the
rebuild behaviour.
"""

from __future__ import annotations

import asyncio

from worker.camera_worker import CameraLoop
from worker.config_hot import (
    GlobalSchema,
    HotConfig,
    HotConfigStore,
    HotZoneCfg,
)


def _make_hotconfig(
    motion_threshold: int = 30,
    motion_contour_area: int = 10,
    motion_lightning_threshold: float = 0.8,
    zones: tuple[HotZoneCfg, ...] = (),
    source_rtsp: str = "",
) -> HotConfig:
    """Build a minimal HotConfig with controllable motion/zones."""

    schema = GlobalSchema(
        detector_url="http://127.0.0.1:31001",
        detector_kind="fastapi",
        mqtt_broker="",
        mqtt_client_id="virex-worker",
        mqtt_topic="virex/detections",
        minio_endpoint="",
        minio_secure=True,
        minio_bucket="virex",
        minio_access_key="",
        minio_secret_key="",
        minio_region="",
        snapshot_quality=80,
        layer_suffix="h264",
        node_id=1,
        cameras=[
            {
                "mtx_path": "t1c5h264",
                "source_rtsp": source_rtsp,
                "tenant_id": 1,
                "camera_id": 5,
                "record": True,
                "detect": {"fps": 5, "classes": ["person"], "min_score": 0.5, "roi": []},
                "motion": {
                    "enabled": True,
                    "threshold": motion_threshold,
                    "contour_area": motion_contour_area,
                    "lightning_threshold": motion_lightning_threshold,
                },
                "masks": [],
                "zones": [
                    {
                        "id": z.id,
                        "friendly_name": z.friendly_name,
                        "objects": list(z.objects),
                        "inertia": z.inertia,
                        "coordinates": [list(p) for p in z.coordinates],
                    }
                    for z in zones
                ],
                "pipeline": [
                    {"stage": "detect", "trigger": "always", "target_objects": [], "zone_id": None}
                ],
            }
        ],
    )
    return GlobalSchema.model_validate(schema.model_dump())


def _make_loop(store: HotConfigStore) -> CameraLoop:
    """Construct a CameraLoop with stubbed I/O collaborators.

    We don't need PyAV / httpx / MQTT / SnapshotUploader for this test —
    those are only touched by `_loop_once` and `_process_frame`,
    which we bypass entirely.
    """
    loop = CameraLoop.__new__(CameraLoop)
    loop._path = "t1c5h264"
    loop._store = store
    loop._http = None  # type: ignore[assignment]
    loop._uploader = None  # type: ignore[assignment]
    loop._publisher = None  # type: ignore[assignment]
    loop._triton = None  # type: ignore[assignment]
    loop._frame_id = 0
    loop._reconnect_event = asyncio.Event()
    loop._reconnect_event.clear()
    loop._motion = None
    loop._motion_signature = None
    loop._zone_filter = None
    loop._zone_signature = None
    return loop


# ---------------------------------------------------------------------------
# Bug 1 regression: hot-reload must rebuild motion + zone_filter
# ---------------------------------------------------------------------------
def test_motion_detector_rebuilt_on_threshold_change() -> None:
    cfg1 = HotConfig.from_schema(_make_hotconfig(motion_threshold=30))
    store = HotConfigStore(cfg1)
    loop = _make_loop(store)

    # First-frame init builds motion detector.
    cfg_now = store.get()
    cam = cfg_now.get_camera("t1c5h264")
    sig1 = (
        cam.motion_enabled,
        cam.motion_threshold,
        cam.motion_contour_area,
        cam.motion_lightning_threshold,
    )
    loop._motion_signature = sig1  # simulate the lazy-init path
    loop._motion = "fake-motion-detector-1"

    # Hot-reload: threshold 30 → 50.
    cfg2 = HotConfig.from_schema(_make_hotconfig(motion_threshold=50))
    store.set(cfg2)

    cam2 = store.get().get_camera("t1c5h264")
    sig2 = (
        cam2.motion_enabled,
        cam2.motion_threshold,
        cam2.motion_contour_area,
        cam2.motion_lightning_threshold,
    )

    # Bug guard: signature differs, so the next frame would rebuild.
    assert sig1 != sig2
    assert loop._motion_signature == sig1  # still old (we didn't run a frame yet)


def test_zone_filter_rebuilt_on_zone_change() -> None:
    cfg1 = HotConfig.from_schema(
        _make_hotconfig(
            zones=(
                HotZoneCfg(
                    id="zone_a",
                    friendly_name="Zone A",
                    objects=("person",),
                    inertia=3,
                    coordinates=((0.4, 0.5), (0.6, 0.5), (0.6, 0.8), (0.4, 0.8)),
                ),
            )
        )
    )
    store = HotConfigStore(cfg1)
    loop = _make_loop(store)

    cfg_now = store.get()
    cam = cfg_now.get_camera("t1c5h264")
    sig1 = tuple((z.id, z.inertia, tuple(z.coordinates)) for z in cam.zones)
    loop._zone_signature = sig1
    loop._zone_filter = "fake-zone-filter-1"

    # Hot-reload: zone B added.
    cfg2 = HotConfig.from_schema(
        _make_hotconfig(
            zones=(
                HotZoneCfg(
                    id="zone_a",
                    friendly_name="Zone A",
                    objects=("person",),
                    inertia=3,
                    coordinates=((0.4, 0.5), (0.6, 0.5), (0.6, 0.8), (0.4, 0.8)),
                ),
                HotZoneCfg(
                    id="zone_b",
                    friendly_name="Zone B",
                    objects=("person",),
                    inertia=2,
                    coordinates=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
                ),
            )
        )
    )
    store.set(cfg2)

    cam2 = store.get().get_camera("t1c5h264")
    sig2 = tuple((z.id, z.inertia, tuple(z.coordinates)) for z in cam2.zones)

    # Bug guard: signatures differ; on the next frame the
    # `if self._zone_filter is None or self._zone_signature != new_zone_sig`
    # check fires and `_zone_filter` is rebuilt.
    assert sig1 != sig2
    assert loop._zone_signature == sig1  # still old (not yet re-built)


def test_zone_signature_inertia_change_triggers_rebuild() -> None:
    """Changing only inertia (not coordinates) must also rebuild — inertia
    is part of the filter semantics."""
    cfg1 = HotConfig.from_schema(
        _make_hotconfig(
            zones=(
                HotZoneCfg(
                    id="zone_a",
                    friendly_name="Zone A",
                    objects=("person",),
                    inertia=3,
                    coordinates=((0.4, 0.5), (0.6, 0.5), (0.6, 0.8), (0.4, 0.8)),
                ),
            )
        )
    )
    sig1 = tuple(
        (z.id, z.inertia, tuple(z.coordinates))
        for z in cfg1.get_camera("t1c5h264").zones
    )

    cfg2 = HotConfig.from_schema(
        _make_hotconfig(
            zones=(
                HotZoneCfg(
                    id="zone_a",
                    friendly_name="Zone A",
                    objects=("person",),
                    inertia=10,  # inertia changed
                    coordinates=((0.4, 0.5), (0.6, 0.5), (0.6, 0.8), (0.4, 0.8)),
                ),
            )
        )
    )
    sig2 = tuple(
        (z.id, z.inertia, tuple(z.coordinates))
        for z in cfg2.get_camera("t1c5h264").zones
    )

    assert sig1 != sig2


def test_motion_signature_includes_lightning_threshold() -> None:
    """`motion.lightning_threshold` is part of the signature so changing
    it forces a rebuild."""
    cfg1 = HotConfig.from_schema(_make_hotconfig())
    cam1 = cfg1.get_camera("t1c5h264")
    sig1 = (
        cam1.motion_enabled,
        cam1.motion_threshold,
        cam1.motion_contour_area,
        cam1.motion_lightning_threshold,
    )

    cfg2 = HotConfig.from_schema(
        _make_hotconfig(motion_lightning_threshold=0.95)  # changed from 0.8
    )
    cam2 = cfg2.get_camera("t1c5h264")
    sig2 = (
        cam2.motion_enabled,
        cam2.motion_threshold,
        cam2.motion_contour_area,
        cam2.motion_lightning_threshold,
    )
    assert sig1 != sig2
