# SPDX-License-Identifier: Apache-2.0
"""Tests for `src.reconcile` rendering logic.

These tests pin the template structure so a careless edit to a Jinja2
file doesn't accidentally break live deploys. They render against an
in-memory bundle and assert against the resulting YAML/Compose text.
"""

from __future__ import annotations

from src.config import Settings
from src.config_pull import CameraEdgeDTO, DetectParamsDTO, EdgeConfigBundle
from src.reconcile import (
    render_mediamtx_yml,
    render_transcoder_compose,
    render_worker_compose,
)


def _bundle(cameras: list[CameraEdgeDTO]) -> EdgeConfigBundle:
    return EdgeConfigBundle(
        node_id=1,
        config_version=1,
        cameras=cameras,
    )


def _cam(
    mtx_path: str = "t1c5h264",
    source_rtsp: str = "rtsp://user:pass@10.0.0.5:554/Streaming/channels/0501",
    tenant_id: int = 1,
    camera_id: int = 5,
    record: bool = True,
) -> CameraEdgeDTO:
    return CameraEdgeDTO(
        mtx_path=mtx_path,
        source_rtsp=source_rtsp,
        tenant_id=tenant_id,
        camera_id=camera_id,
        detect=DetectParamsDTO(),
        record=record,
    )


def test_render_mediamtx_includes_raw_and_h264_paths() -> None:
    """Each camera produces both a `_raw` (with source URL) and `_h264` (record) path."""
    settings = Settings()
    bundle = _bundle([_cam()])
    text = render_mediamtx_yml(bundle, settings)

    assert "t1c5h264raw:" in text
    assert "t1c5h264h264:" in text
    assert "source: \"rtsp://user:pass@10.0.0.5:554" in text
    assert "record: true" in text


def test_render_mediamtx_omits_source_when_empty() -> None:
    """Cameras without a source_rtsp still get both paths, just no source: line."""
    settings = Settings()
    cam = _cam()
    cam_obj = cam.model_copy(update={"source_rtsp": ""})
    text = render_mediamtx_yml(_bundle([cam_obj]), settings)

    assert "t1c5h264raw:" in text
    assert "t1c5h264h264:" in text
    assert "source: \"rtsp://" not in text


def test_render_transcoder_produces_one_ffmpeg_per_camera() -> None:
    """N cameras → N `transcoder-<mtx_path>` services."""
    settings = Settings()
    bundle = _bundle(
        [
            _cam(mtx_path="t1c5h264"),
            _cam(mtx_path="t1c6h264"),
        ]
    )
    text = render_transcoder_compose(bundle, settings)

    assert "transcoder-t1c5h264:" in text
    assert "transcoder-t1c6h264:" in text
    assert text.count("-i rtsp://localhost:") == 2
    # Both publish back to their sibling `_h264` path
    assert "rtsp://localhost:19554/t1c5h264h264" in text
    assert "rtsp://localhost:19554/t1c6h264h264" in text


def test_render_transcoder_uses_configured_rtsp_port() -> None:
    """Custom RTSP port (e.g. 19554) flows into the FFmpeg -i URL."""
    settings = Settings(mediamtx_rtsp_port=29554)
    bundle = _bundle([_cam()])
    text = render_transcoder_compose(bundle, settings)
    assert "rtsp://localhost:29554/t1c5h264raw" in text


def test_render_worker_compose_unchanged() -> None:
    """Smoke test that worker-compose still renders correctly."""
    settings = Settings()
    bundle = _bundle([_cam()])
    text = render_worker_compose(bundle, settings)
    assert "worker-t1c5h264:" in text
    assert "virex-camera: \"t1c5h264\"" in text
    # Worker must inherit MINIO_*/MQTT_* from deploy/edge/.env via env_file.
    # The .env at state/ is provided by the deploy/edge/docker-compose.yml
    # bind mount (./.env:/etc/virex/.env:ro), so docker compose's
    # `env_file: .env` resolves correctly relative to the project dir.
    assert "env_file:" in text
    assert ".env" in text


def test_render_worker_compose_uses_relative_env_file() -> None:
    """env_file must be the bare `.env` (relative to project dir) — not
    an absolute host path. The bind mount at deploy/edge/docker-compose.yml
    provides state/.env = host's deploy/edge/.env so docker compose's
    `env_file: .env` resolves correctly.
    """
    settings = Settings()
    bundle = _bundle([_cam()])
    text = render_worker_compose(bundle, settings)
    # No {{ state_dir_host }} leftover
    assert "{{" not in text, f"Unrendered Jinja in worker compose:\n{text}"
    # The rendered env_file directive itself must use the bare relative
    # name. Comments are allowed to mention ../.env as documentation.
    env_file_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip().startswith("- ") and ".env" in ln
    ]
    assert any(ln == "- .env" for ln in env_file_lines), (
        f"env_file must include bare relative `.env` — got {env_file_lines!r}"
    )
    # No absolute host paths sneaking into env_file:
    for ln in env_file_lines:
        assert not ln.startswith("- /"), f"env_file must be relative — got {ln!r}"


def test_full_reconcile_text_for_two_cameras() -> None:
    """E2E render: 2 cameras produce coherent mediamtx + transcoder + worker compose."""
    settings = Settings()
    bundle = _bundle(
        [
            _cam(
                mtx_path="t1c5h264",
                source_rtsp="rtsp://admin:pwd@10.0.0.5:554/Streaming/channels/0501",
                camera_id=5,
            ),
            _cam(
                mtx_path="t1c6h264",
                source_rtsp="rtsp://admin:pwd@10.0.0.6:554/Streaming/channels/0601",
                camera_id=6,
            ),
        ]
    )
    mtx = render_mediamtx_yml(bundle, settings)
    txc = render_transcoder_compose(bundle, settings)
    wrk = render_worker_compose(bundle, settings)

    # Both cameras present in all three artifacts
    for cam_id in ("t1c5h264", "t1c6h264"):
        assert f"{cam_id}raw:" in mtx
        assert f"{cam_id}h264:" in mtx
        assert f"transcoder-{cam_id}:" in txc
        assert f"worker-{cam_id}:" in wrk
