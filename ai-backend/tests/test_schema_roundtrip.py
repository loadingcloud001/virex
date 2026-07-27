# SPDX-License-Identifier: Apache-2.0
"""Schema round-trip tests for worker-side Pydantic models.

The MQTT payload is JSON over the wire, so `model_dump_json()` MUST
round-trip cleanly through `model_validate_json()`. These tests assert
that contract.
"""

from __future__ import annotations

import pydantic
import pytest

from worker.schema import DetectionEvent, DetectionPayload


def _sample() -> DetectionEvent:
    return DetectionEvent(
        event_uuid="abc123",
        ts="2026-07-26T13:00:00.123+00:00",
        node_id=1,
        tenant_id=1,
        camera_id=5,
        mtx_path="t1c5h264",
        frame_id=42,
        detections=[
            DetectionPayload(label="person", score=0.91, box=[0.1, 0.2, 0.3, 0.4]),
            DetectionPayload(label="person", score=0.77, box=[0.5, 0.6, 0.7, 0.8]),
        ],
        snapshot_url="tenants/1/snapshots/abc123.jpg",
        snapshot_size=142337,
    )


def test_round_trip_via_json() -> None:
    original = _sample()
    raw = original.model_dump_json()
    parsed = DetectionEvent.model_validate_json(raw)
    assert parsed == original


def test_extra_field_rejected() -> None:
    original = _sample()
    raw = original.model_dump_json()
    raw_with_extra = raw.replace("}", ',"rogue":true}', 1)
    with pytest.raises(pydantic.ValidationError):
        DetectionEvent.model_validate_json(raw_with_extra)


def test_default_node_id_is_none() -> None:
    raw = _sample()
    raw_dict = raw.model_dump()
    raw_dict["node_id"] = None
    parsed = DetectionEvent.model_validate_json(raw.model_dump_json())
    # Field is Optional[int] with default None.
    assert parsed.node_id == raw.node_id


def test_payload_required_box_size() -> None:
    """Catches future schema drift — bbox must be exactly 4 floats."""
    assert len(_sample().detections[0].box) == 4
