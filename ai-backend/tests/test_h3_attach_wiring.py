# SPDX-License-Identifier: AGPL-3.0
"""Regression test for H3: run_all must attach spawned CameraLoops to the
reloader so Tier B source_rtsp changes propagate.

Earlier versions constructed Reloader(camera_loops={}) and never wrote
into it; the reloader's _apply step iterated an empty dict and silently
no-op'd `loop.request_reconnect()` calls for Tier B RTSP URL changes.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from worker import camera_worker
from worker.config_hot import HotConfig, HotConfigStore


def _make_yaml(tmp: Path) -> Path:
    p = tmp / "workers.yaml"
    p.write_text(
        textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        detector_kind: "fastapi"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_client_id: "virex-worker"
        mqtt_topic: "virex/detections"
        minio_endpoint: "cos"
        minio_secure: true
        minio_bucket: "virex"
        minio_access_key: "a"
        minio_secret_key: "s"
        minio_region: "r"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "cam-A"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 1
            record: true
            detect: {fps: 5, classes: ["person"], min_score: 0.5, roi: []}
          - mtx_path: "cam-B"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 2
            record: true
            detect: {fps: 5, classes: ["person"], min_score: 0.5, roi: []}
        """).lstrip(),
        encoding="utf-8",
    )
    return p


def test_run_all_attaches_camera_loops_to_reloader(tmp_path):
    yaml_path = _make_yaml(tmp_path)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))

    # Mock reloader to record attach() calls
    reloader = MagicMock()
    reloader.attach = MagicMock()

    # Mock CameraLoop to immediately return an awaitable that finishes
    # so the gather doesn't actually run real RTSP/detection logic.
    captured_loops = []

    class _FakeLoop:
        def __init__(self, mtx_path, *_args, **_kwargs):
            self.mtx_path = mtx_path
            captured_loops.append(self)

        async def run(self):
            await asyncio.sleep(0)  # let event loop tick, then return

    # Patch CameraLoop class so no RTSP / network happens.
    # Patch the heavy clients in the camera_worker module so __init__
    # doesn't actually connect to MQTT / MinIO / http.
    with (
        patch.object(camera_worker, "CameraLoop", _FakeLoop),
        patch.object(camera_worker, "httpx"),
        patch.object(camera_worker, "TritonClient"),
        patch.object(camera_worker, "SnapshotUploader"),
        patch.object(camera_worker, "MqttPublisher"),
    ):
        # The above replaced httpx module entirely; we need
        # AsyncClient.aclose() to be awaitable when run_all's finally
        # block runs. Patch it directly.
        real_httpx = camera_worker.httpx  # it's now a MagicMock thanks to patch
        real_httpx.AsyncClient.return_value.aclose = AsyncMock()
        asyncio.run(camera_worker.run_all(store, reloader=reloader))

    # Both cameras must have been attached to the reloader
    attached_paths = {call.args[0] for call in reloader.attach.call_args_list}
    assert attached_paths == {"cam-A", "cam-B"}, (
        f"H3 regression: reloader.attach() called for {attached_paths}; "
        "expected both cameras. Without attach(), Tier B source_rtsp "
        "changes never propagate to the affected loop."
    )
    # Attach must have been called with (mtx_path, loop) — i.e. each
    # attached loop is the actual CameraLoop instance, not None.
    for call in reloader.attach.call_args_list:
        assert len(call.args) == 2
        loop_arg = call.args[1]
        assert isinstance(loop_arg, _FakeLoop), (
            f"H3 regression: reloader.attach() second arg is "
            f"{type(loop_arg).__name__}, must be the CameraLoop instance"
        )
