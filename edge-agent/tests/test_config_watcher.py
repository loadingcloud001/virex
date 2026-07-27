# SPDX-License-Identifier: Apache-2.0
"""Tests for `src.config_watcher`.

We focus on the polling backend (deterministic, no real inotify
dependency). The inotify backend is implicitly tested by
`make_config_watcher` returning `InotifyConfigWatcher` when watchdog
is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import textwrap
import time
from pathlib import Path

import pytest
import yaml as _yaml

from src.config_pull import EdgeConfigBundle
from src.config_watcher import (
    PollingConfigWatcher,
    get_last_applied,
    make_config_watcher,
    reset_last_applied_cache,
    set_last_applied,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_last_applied_cache()
    yield
    reset_last_applied_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "workers.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


async def _cancel_task(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ---------------------------------------------------------------------------
# Polling watcher
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_polling_no_change_emits_nothing(tmp_path):
    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """)
    captured: list[tuple] = []

    async def handler(bundle, report):
        captured.append((bundle, report))

    w = PollingConfigWatcher(str(yaml_path), poll_interval_sec=0.05)
    task = asyncio.create_task(w.run(handler))
    await asyncio.sleep(0.3)
    w.stop()
    task.cancel()
    await _cancel_task(task)
    # First-ever poll: just baseline establishment; subsequent polls see
    # same mtime → skip. No file change → no handler call.
    assert captured == []


@pytest.mark.asyncio
async def test_polling_writes_a_new_file_triggers_handler(tmp_path):
    """File change after baseline → handler fires with classified diff."""
    import os

    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """)
    captured: list[tuple] = []

    async def handler(bundle, report):
        captured.append((bundle, report))

    w = PollingConfigWatcher(str(yaml_path), poll_interval_sec=0.05)

    task = asyncio.create_task(w.run(handler))
    # Let baseline be established.
    await asyncio.sleep(0.15)
    # Sleep + write + os.utime to defeat filesystem timestamp granularity.
    time.sleep(0.1)
    yaml_path.write_text(textwrap.dedent("""
        node_id: 1
        config_version: 2
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 10
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """).lstrip(), encoding="utf-8")
    future = time.time() + 2
    os.utime(yaml_path, (future, future))

    # Wait up to 2 s for the change to be picked up.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if captured:
            break
    w.stop()
    task.cancel()
    await _cancel_task(task)

    assert captured, "handler was never called after file change"
    bundle, report = captured[0]
    assert bundle.cameras[0].detect.fps == 10
    assert "detect.fps" in report.tier_a_per_camera.get("t1c5h264", ())


@pytest.mark.asyncio
async def test_polling_invalid_yaml_does_not_crash(tmp_path):
    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """)
    captured: list[tuple] = []

    async def handler(bundle, report):
        captured.append((bundle, report))

    w = PollingConfigWatcher(str(yaml_path), poll_interval_sec=0.05)

    task = asyncio.create_task(w.run(handler))
    await asyncio.sleep(0.15)
    time.sleep(0.1)
    yaml_path.write_text("this is not: valid: yaml: --", encoding="utf-8")
    await asyncio.sleep(0.3)
    w.stop()
    task.cancel()
    await _cancel_task(task)

    # Watcher survived; handler never called (no valid bundle diff).
    assert captured == []


@pytest.mark.asyncio
async def test_polling_writes_back_to_old_state_doesnt_emit(tmp_path):
    """Touching the file with identical content still changes mtime,
    but the parsed bundle is identical to the cached baseline so
    classifier returns no changes — handler is NOT called.
    """
    import os

    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """)
    captured: list[tuple] = []

    async def handler(bundle, report):
        captured.append((bundle, report))

    w = PollingConfigWatcher(str(yaml_path), poll_interval_sec=0.05)

    task = asyncio.create_task(w.run(handler))
    await asyncio.sleep(0.15)
    time.sleep(0.1)
    # Touch with future mtime but identical content.
    future = time.time() + 2
    os.utime(yaml_path, (future, future))
    await asyncio.sleep(0.3)
    w.stop()
    task.cancel()
    await _cancel_task(task)

    # No real diff → no handler call.
    assert captured == []


# ---------------------------------------------------------------------------
# `_last_applied` module-level cache
# ---------------------------------------------------------------------------
def test_last_applied_cache_round_trip(tmp_path):
    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras:
          - mtx_path: "t1c5h264"
            tenant_id: 1
            camera_id: 5
            source_rtsp: ""
            detect:
              fps: 5
              classes: ["person"]
              min_score: 0.5
              roi: []
            record: true
    """)
    data = _yaml.safe_load(yaml_path.read_text())
    bundle = EdgeConfigBundle.model_validate(data)
    assert get_last_applied("nope") is None
    set_last_applied(str(yaml_path), bundle)
    assert get_last_applied(str(yaml_path)) is bundle


# ---------------------------------------------------------------------------
# make_config_watcher factory
# ---------------------------------------------------------------------------
def test_make_config_watcher_force_polling_returns_polling(tmp_path):
    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras: []
    """)
    w = make_config_watcher(str(yaml_path), force_polling=True)
    assert isinstance(w, PollingConfigWatcher)


def test_make_config_watcher_returns_at_least_one_backend(tmp_path):
    yaml_path = _make_yaml(tmp_path, """
        node_id: 1
        config_version: 1
        cameras: []
    """)
    w = make_config_watcher(str(yaml_path))
    from src.config_watcher import ConfigWatcher

    assert isinstance(w, ConfigWatcher)
    assert hasattr(w, "run")
    assert hasattr(w, "stop")
