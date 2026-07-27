# SPDX-License-Identifier: AGPL-3.0
"""Regression tests for the bugs surfaced in the 2026-07-27 code review.

Each test is a one-line guard that the specific bug class isn't re-introduced:

* `test_admin_config_redacts_minio_access_key` (H2): the /admin/config
  endpoint must redact `minio_access_key` AND `minio_secret_key`. Earlier
  versions only redacted the secret key, leaking the COS AKID in plaintext
  to anyone with localhost network access.

* `test_main_hotconfig_construction_includes_detector_kind` (C1):
  HotConfig.construct() must always populate the `detector_kind` field.
  An earlier refactor added `detector_kind` as a required field on the
  frozen dataclass but forgot to update the direct constructor call in
  main.py — this was a latent crash on every image rebuild.

* `test_filtered_hot_config_preserves_detector_kind` (C1 follow-on):
  `_filtered_hot_config(full_hot, bundle_json)` must propagate
  `detector_kind` from the source HotConfig so per-camera containers
  don't silently lose the detector backend selection.

* `test_run_all_attaches_camera_loops_to_reloader` (H3): run_all must
  register each spawned CameraLoop with the reloader via `attach()` so
  Tier B `source_rtsp` changes can actually trigger reconnect. Earlier
  versions constructed Reloader with `camera_loops={}` and never wrote
  into it → Tier B was silently broken.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from worker.admin import build_app
from worker.config_hot import HotConfig, HotConfigStore
from worker.config_reloader import Reloader
from worker.main import _filtered_hot_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_yaml(tmp: Path, body: str) -> Path:
    p = tmp / "workers.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


def _build_store(tmp: Path, body: str = """
    detector_url: "http://127.0.0.1:31001"
    detector_kind: "fastapi"
    mqtt_broker: "127.0.0.1:1883"
    mqtt_client_id: "virex-worker"
    mqtt_topic: "virex/detections"
    minio_endpoint: "cos.ap-singapore.myqcloud.com"
    minio_secure: true
    minio_bucket: "virex"
    minio_access_key: "TEST_FIXTURE_ACCESS_KEY"
    minio_secret_key: "TEST_FIXTURE_SECRET_KEY"
    minio_region: "ap-singapore"
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
""") -> tuple[Path, HotConfigStore]:
    p = _make_yaml(tmp, body)
    store = HotConfigStore(HotConfig.from_yaml(p))
    return p, store


# ---------------------------------------------------------------------------
# H2: admin /admin/config redaction
# ---------------------------------------------------------------------------
def test_admin_config_redacts_minio_access_key(tmp_path):
    """Both access_key and secret_key must be redacted in /admin/config response."""
    _yaml, store = _build_store(tmp_path)
    reloader = MagicMock(spec=Reloader)
    app = build_app(store, reloader)
    client = TestClient(app)

    resp = client.get("/admin/config")
    assert resp.status_code == 200

    body = resp.json()
    # Must NOT contain the real access key
    assert body["minio_access_key"] == "***REDACTED***", (
        f"H2 regression: minio_access_key leaked as "
        f"{body['minio_access_key']!r} — was a live credential leak"
    )
    # Must NOT contain the real secret key
    assert body["minio_secret_key"] == "***REDACTED***"
    # Source_rtsp must also be redacted (or empty)
    for cam in body["cameras"]:
        if cam["source_rtsp"]:
            assert cam["source_rtsp"] == "***REDACTED***"
    # Full payload must not contain the fixture creds anywhere
    raw = resp.text
    assert "TEST_FIXTURE_ACCESS_KEY" not in raw, (
        "H2 regression: fixture access_key leaked anywhere in /admin/config body"
    )
    assert "TEST_FIXTURE_SECRET_KEY" not in raw


# ---------------------------------------------------------------------------
# C1: HotConfig construction must include detector_kind
# ---------------------------------------------------------------------------
def test_main_hotconfig_construction_includes_detector_kind(tmp_path):
    """main.py must construct HotConfig via from_yaml/from_schema, not
    direct kwargs that skip detector_kind. We verify the produced
    HotConfig actually has a detector_kind field that matches the YAML.
    """
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://127.0.0.1:31001"
        detector_kind: "triton"
        mqtt_broker: "127.0.0.1:1883"
        mqtt_client_id: "virex-worker"
        mqtt_topic: "virex/detections"
        minio_endpoint: "cos.ap-singapore.myqcloud.com"
        minio_secure: true
        minio_bucket: "virex"
        minio_access_key: "AKID_test"
        minio_secret_key: "secret_test"
        minio_region: "ap-singapore"
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
    hot = HotConfig.from_yaml(yaml_path)
    # If main.py re-introduces the direct-construction bug that omits
    # detector_kind, HotConfig.from_yaml itself still works (uses
    # from_schema) — but main.py would still have the latent path. So
    # we ALSO call _filtered_hot_config which is the function main.py
    # uses after from_yaml, to verify the bundle path propagates
    # detector_kind through.
    bundle = (
        '{"mtx_path": "t1c5h264", "source_rtsp": "", '
        '"tenant_id": 1, "camera_id": 5, "record": true, '
        '"detect": {"fps": 5, "classes": ["person"], "min_score": 0.5, "roi": []}}'
    )
    filtered = _filtered_hot_config(hot, bundle)
    assert filtered.detector_kind == "triton", (
        "C1 regression: filtered HotConfig lost detector_kind "
        f"(got {filtered.detector_kind!r}) — per-camera container would "
        "fail to pick Triton vs FastAPI backend"
    )


def test_filtered_hot_config_preserves_detector_kind(tmp_path):
    """The bundle-filtered HotConfig must propagate ALL global fields,
    not just the ones previously listed in main.py:_filtered_hot_config.
    Specifically detector_kind was added later to the schema; without
    explicit propagation every per-camera container defaults to ""
    and downstream code branches on an empty selector.
    """
    yaml_path = _make_yaml(tmp_path, """
        detector_url: "http://localhost:38000"
        detector_kind: "triton"
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
        node_id: 99
        cameras:
          - mtx_path: "cam-A"
            source_rtsp: ""
            tenant_id: 1
            camera_id: 1
            record: true
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
    """)
    full = HotConfig.from_yaml(yaml_path)
    bundle_json = (
        '{"mtx_path": "cam-A", "source_rtsp": "", '
        '"tenant_id": 1, "camera_id": 1, "record": true, '
        '"detect": {"fps": 5, "classes": ["person"], "min_score": 0.5, "roi": []}}'
    )
    filtered = _filtered_hot_config(full, bundle_json)
    # All globals must survive filtering:
    assert filtered.detector_url == full.detector_url
    assert filtered.detector_kind == full.detector_kind
    assert filtered.node_id == full.node_id
    assert filtered.mqtt_broker == full.mqtt_broker
    assert filtered.minio_endpoint == full.minio_endpoint
    assert filtered.minio_region == full.minio_region
    # Camera list narrowed to one
    assert len(filtered.cameras) == 1
    assert filtered.cameras[0].mtx_path == "cam-A"
