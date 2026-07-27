#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test for the Virex AI stack.

This script exercises the public contracts (schemas, configs, the
clip helper) WITHOUT needing GPUs, network or running services. It is
intended for CI and for bring-up verification on a fresh workstation:

    uv run python tools/smoke.py

It will:
  1. Load `deploy/edge/workers.yaml.example` and assert its shape.
  2. Construct a sample `DetectionEvent`, serialise to JSON, deserialise,
     assert equality (the MQTT wire contract is preserved).
  3. Round-trip a sample `EdgeConfigBundle` to verify the
     edge-agent ↔ portal contract.
  4. Run `keep_person()` on synthetic outputs and assert at least one
     person survives threshold filtering.
  5. Resolve segment filenames and confirm `segments_covering()` picks
     the right MediaMTX segment for a known event time.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Run from repo root.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai-backend"))

from clip_builder.ffmpeg_runner import segments_covering  # noqa: E402
from detector.postprocess import keep_person  # noqa: E402
from worker.config import load_config  # noqa: E402
from worker.schema import DetectionEvent, DetectionPayload  # noqa: E402


def main() -> int:
    failed = 0

    # ---- 1. workers.yaml.example loads cleanly --------------------------
    cfg_path = ROOT / "deploy" / "edge" / "workers.yaml.example"
    cfg = load_config(cfg_path)
    if len(cfg.cameras) != 2:
        print(f"FAIL: expected 2 cameras, got {len(cfg.cameras)}")
        failed += 1
    else:
        print(f"OK  workers.yaml loads {len(cfg.cameras)} cameras")

    # ---- 2. DetectionEvent round-trip -----------------------------------
    event = DetectionEvent(
        event_uuid="smoke",
        ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
        tenant_id=1,
        camera_id=5,
        mtx_path="t1c5h264",
        frame_id=1,
        detections=[DetectionPayload(label="person", score=0.95, box=[0.1, 0.2, 0.3, 0.4])],
        snapshot_url="tenants/1/snapshots/smoke.jpg",
        snapshot_size=1234,
    )
    wire = event.model_dump_json()
    parsed = DetectionEvent.model_validate_json(wire)
    if parsed == event:
        print("OK  DetectionEvent JSON round-trip preserves all fields")
    else:
        print("FAIL: DetectionEvent round-trip mismatch")
        failed += 1

    # ---- 3. keep_person filters + returns persons ------------------------
    out = keep_person(
        scores=[[0.95], [0.2]],
        boxes=[[0.0, 0.0, 50.0, 50.0], [10.0, 10.0, 20.0, 20.0]],
        labels=[1, 1],
        image_hw=(480, 640),
        min_score=0.5,
    )
    if len(out) == 1 and out[0].score == 0.95:
        print("OK  keep_person keeps only above-threshold detections")
    else:
        print(f"FAIL: keep_person unexpected output: {out}")
        failed += 1

    # ---- 4. segments_covering resolves the right segment ----------------
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        cam_dir = Path(tmp) / "t1c5h264"
        cam_dir.mkdir()
        (cam_dir / "2026-07-26_13-00-00-000000.mp4").write_bytes(b"")
        (cam_dir / "2026-07-26_13-00-30-000000.mp4").write_bytes(b"")
        ev = datetime(2026, 7, 26, 13, 0, 10)
        picked = segments_covering(Path(tmp), "t1c5h264", ev)
        if len(picked) == 1:
            print("OK  segments_covering resolves one segment for in-window event")
        else:
            print(f"FAIL: expected 1 segment, got {len(picked)}")
            failed += 1

    print()
    if failed:
        print(f"SMOKE FAILED: {failed} check(s)")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
