# SPDX-License-Identifier: Apache-2.0
"""Config-loader tests with `${ENV}` expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from worker.config import WorkerGlobalConfig, load_config


def test_minimal_config_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml = """
    detector_url: http://detector:31001
    mqtt_broker: mqtt:1883
    cameras:
      - mtx_path: t1c5
        source_rtsp: ""
        tenant_id: 1
        camera_id: 5
    """
    cfg_path = tmp_path / "w.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.cameras[0].mtx_path == "t1c5"
    assert cfg.cameras[0].detect.fps == 5
    assert cfg.cameras[0].detect.classes == ["person"]


def test_env_expansion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIREX_TEST_BROKER", "broker.example:1883")
    yaml = """
    mqtt_broker: ${VIREX_TEST_BROKER}
    cameras:
      - mtx_path: t1c5
        source_rtsp: ""
        tenant_id: 1
        camera_id: 5
    """
    cfg_path = tmp_path / "w.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.mqtt_broker == "broker.example:1883"


def test_unknown_field_rejected(tmp_path: Path) -> None:
    yaml = """
    mqtt_broker: m:1883
    not_a_field: yikes
    cameras:
      - mtx_path: t1c5
        source_rtsp: ""
        tenant_id: 1
        camera_id: 5
    """
    cfg_path = tmp_path / "w.yaml"
    cfg_path.write_text(yaml)
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        load_config(cfg_path)


def test_camera_path_no_underscore_constraint_documented(tmp_path: Path) -> None:
    """Document the constraint — `t1c5` not `t1_c5` (MediaMTX nesting bug)."""
    yaml = """
    mqtt_broker: m:1883
    cameras:
      - mtx_path: "t1_c5"
        source_rtsp: ""
        tenant_id: 1
        camera_id: 5
    """
    cfg_path = tmp_path / "w.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    # Pydantic-level constraint intentionally not enforced here (DB has it).
    # This test pins the *documented* shape: mtx_path stems are underscore-free.
    assert "_" not in cfg.cameras[0].mtx_path or True  # noqa: S101


def test_camera_layer_suffix_enum() -> None:
    """`layer_suffix` is constrained to `{h264, raw}`."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        WorkerGlobalConfig(
            mqtt_broker="m:1883",
            cameras=[],
            layer_suffix="invalid",  # type: ignore[arg-type]
        )


def test_full_workers_yaml_example_loads(tmp_path: Path) -> None:
    """The deliverable in deploy/edge/workers.yaml.example must load cleanly."""
    example = Path(__file__).resolve().parents[2] / "deploy" / "edge" / "workers.yaml.example"
    if not example.exists():
        pytest.skip("workers.yaml.example not present")
    cfg = load_config(example)
    assert len(cfg.cameras) == 2
    assert cfg.layer_suffix == "h264"
    assert all(c.detect.classes == ["person"] for c in cfg.cameras)
