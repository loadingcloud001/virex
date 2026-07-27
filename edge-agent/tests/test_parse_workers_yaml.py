# SPDX-License-Identifier: AGPL-3.0
"""Regression tests for `parse_workers_yaml`.

Background
----------
The v1 pilot workers.yaml template uses `${VAR}` and `${VAR:-default}`
placeholders that resolve to values in deploy/edge/.env. The ai-backend
worker's loader (`worker._yaml.expand_env`) runs env-substitution before
pydantic validation. Edge-agent's loader was originally a thin wrapper
that called `yaml.safe_load` directly, which left `${NODE_ID:-1}` as a
literal string and broke pydantic int validation — silently dropping
every config change in the config_watcher.

These tests pin both the env-substitution behaviour and the full
parse_workers_yaml round-trip so the bug can't regress.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.tier_classifier import parse_workers_yaml


def _write_workers_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "workers.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


def test_parse_workers_yaml_resolves_simple_var(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("MQTT_BROKER", "10.0.0.5:1883")
    monkeypatch.setenv("MINIO_BUCKET", "virex-snapshots-1308927282")
    monkeypatch.delenv("NODE_ID", raising=False)

    p = _write_workers_yaml(
        tmp_path,
        """
        node_id: ${NODE_ID:-1}
        mqtt_broker: "${MQTT_BROKER}"
        minio_bucket: "${MINIO_BUCKET}"
        cameras:
          - mtx_path: cam1
            source_rtsp: "rtsp://x"
            tenant_id: 1
            camera_id: 1
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
        """,
    )
    bundle = parse_workers_yaml(str(p))
    assert bundle.node_id == 1, f"default override broken: got {bundle.node_id!r}"
    assert len(bundle.cameras) == 1
    assert bundle.cameras[0].mtx_path == "cam1"


def test_parse_workers_yaml_resolves_env_override(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("NODE_ID", "7")

    p = _write_workers_yaml(
        tmp_path,
        """
        node_id: ${NODE_ID:-1}
        cameras:
          - mtx_path: cam1
            source_rtsp: "rtsp://x"
            tenant_id: 1
            camera_id: 1
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
        """,
    )
    bundle = parse_workers_yaml(str(p))
    assert bundle.node_id == 7, f"env override broken: got {bundle.node_id!r}"


def test_parse_workers_yaml_regression_int_parsing(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Regression test for the original bug.

    Pre-fix, parse_workers_yaml ran yaml.safe_load on the raw text,
    leaving `${NODE_ID:-1}` as a literal string. Pydantic's int
    validator then raised `int_parsing` and the config_watcher
    silently dropped every subsequent change with
    `config_watcher_yaml_invalid`. The fix runs expand_env() first.
    """
    monkeypatch.delenv("NODE_ID", raising=False)

    p = _write_workers_yaml(
        tmp_path,
        """
        node_id: ${NODE_ID:-1}
        cameras:
          - mtx_path: cam1
            source_rtsp: "rtsp://x"
            tenant_id: 1
            camera_id: 1
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
        """,
    )
    # If we get here without raising, the fix is in place.
    bundle = parse_workers_yaml(str(p))
    assert isinstance(bundle.node_id, int)


def test_parse_workers_yaml_missing_var_left_literal(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Missing env vars (no default) should NOT silently turn into empty.

    They stay literal `${MISSING}` so the schema validator surfaces a
    useful error rather than accepting an empty string.
    """
    monkeypatch.delenv("MISSING_VAR", raising=False)

    p = _write_workers_yaml(
        tmp_path,
        """
        node_id: ${MISSING_VAR}
        cameras: []
        """,
    )
    # The literal `${MISSING_VAR}` should survive through yaml.safe_load
    # as the string "${MISSING_VAR}", which pydantic's int validator then
    # rejects — exactly as worker._yaml.expand_env() behaves.
    import pytest
    with pytest.raises(Exception):
        parse_workers_yaml(str(p))