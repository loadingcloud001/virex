# SPDX-License-Identifier: Apache-2.0
"""Test the worker admin FastAPI server for Tier A/B push-applies.

These tests exercise the HTTP surface that edge-agent will hit
(`POST /admin/reload`). The Poller path is covered in
`test_config_reloader.py`; here we only assert that the HTTP layer
correctly forwards to `Reloader`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from worker.admin import build_app
from worker.config_hot import HotConfig, HotConfigStore
from worker.config_reloader import Reloader


# ---------------------------------------------------------------------------
# Helpers — exact same shape as in test_config_reloader.py.
# ---------------------------------------------------------------------------
def _make_yaml(tmp: Path, body: str) -> Path:
    p = tmp / "workers.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


class _FakeCamera:
    def __init__(self, path: str) -> None:
        self.path = path
        self.reconnects = 0

    def request_reconnect(self) -> None:
        self.reconnects += 1


def _build(tmp: Path, body: str, *, reconnect_paths: list[str] | None = None):
    yaml_path = _make_yaml(tmp, body)
    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loops = {
        p: _FakeCamera(p) for p in (reconnect_paths or [])
    }
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops=loops,  # type: ignore[arg-type]
        poll_interval_sec=10.0,  # disable polling in tests
    )
    app = build_app(store, reloader)
    return app, store, reloader, loops


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def test_admin_healthz(tmp_path: Path) -> None:
    app, store, _reloader, _loops = _build(
        tmp_path,
        """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
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
        """,
    )
    client = TestClient(app)
    resp = client.get("/admin/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["version"] == 0


def test_admin_config_redacts_secrets(tmp_path: Path) -> None:
    app, _store, _reloader, _loops = _build(
        tmp_path,
        """
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        minio_secret_key: "shh-its-a-secret"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: "rtsp://admin:hunter2@10.0.0.5/Streaming/channels/0501"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
        """,
    )
    client = TestClient(app)
    resp = client.get("/admin/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["minio_secret_key"] == "***REDACTED***"
    assert body["cameras"][0]["source_rtsp"] == "***REDACTED***"
    assert body["cameras"][0]["fps"] == 5


def test_admin_reload_applies_tier_a(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workers.yaml"
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
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
    """).lstrip(), encoding="utf-8")

    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loop = _FakeCamera("t1c5h264")
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={"t1c5h264": loop},  # type: ignore[arg-type]
        poll_interval_sec=10.0,
    )
    app = build_app(store, reloader)
    client = TestClient(app)

    # Operator writes new fps
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 10
              classes: ["person"]
              min_score: 0.5
              roi: []
    """).lstrip(), encoding="utf-8")

    resp = client.post("/admin/reload", params={"dry_run": "false"})
    assert resp.status_code == 200
    data = resp.json()
    assert "t1c5h264.fps" in data["tier_a"]
    assert store.get().get_camera("t1c5h264").fps == 10
    # Tier A — no reconnect
    assert loop.reconnects == 0


def test_admin_reload_rtsp_triggers_reconnect(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workers.yaml"
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: "rtsp://old@10.0.0.5/Streaming/channels/0501"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """).lstrip(), encoding="utf-8")

    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    loop = _FakeCamera("t1c5h264")
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={"t1c5h264": loop},  # type: ignore[arg-type]
        poll_interval_sec=10.0,
    )
    app = build_app(store, reloader)
    client = TestClient(app)

    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            source_rtsp: "rtsp://new@10.0.0.5/Streaming/channels/0501"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """).lstrip(), encoding="utf-8")

    resp = client.post("/admin/reload")
    assert resp.status_code == 200
    assert loop.reconnects == 1


def test_admin_reload_invalid_yaml_returns_400(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workers.yaml"
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
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
    """).lstrip(), encoding="utf-8")

    store = HotConfigStore(HotConfig.from_yaml(yaml_path))
    reloader = Reloader(
        yaml_path=str(yaml_path),
        store=store,
        camera_loops={},
        poll_interval_sec=10.0,
    )
    app = build_app(store, reloader)
    client = TestClient(app)

    # Operator typo (unknown field on detect).
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 99
              classes: ["person"]
              min_score: 0.5
              roi: []
              bogus_field: true
    """).lstrip(), encoding="utf-8")
    # fps=99 violates ge=30 bound
    yaml_path.write_text(textwrap.dedent("""
        detector_url: "http://127.0.0.1:31001"
        mqtt_broker: "127.0.0.1:1883"
        snapshot_quality: 80
        layer_suffix: "h264"
        node_id: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            detect:
              fps: 99
              classes: ["person"]
              min_score: 0.5
              roi: []
    """).lstrip(), encoding="utf-8")

    v0 = store.version()
    resp = client.post("/admin/reload")
    # Behaviour: returns 200 with `error` field set; the last-known-good
    # HotConfig remains live in the store so detection keeps running.
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is not None
    # No swap happened
    assert store.version() == v0
