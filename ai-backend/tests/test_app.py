# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the FastAPI detector app (no CUDA, fake Detector)."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from detector.app import create_app
from detector.postprocess import DEFAULT_MIN_SCORE


class _FakeDetector:
    """Stand-in for the real Detector; returns a fixed person detection."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def detect_bytes(self, image: bytes, *, min_score: float = DEFAULT_MIN_SCORE):  # noqa: ANN001
        self.calls.append(image)
        from detector.postprocess import Detection

        return [
            Detection(label="person", score=0.91, box=(0.1, 0.2, 0.3, 0.4)),
        ]


@pytest.fixture
def fake_detector() -> _FakeDetector:
    return _FakeDetector()


@pytest.fixture
def client(fake_detector: _FakeDetector) -> Iterator[TestClient]:
    app = create_app(detector=fake_detector)  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c


def _jpeg_bytes() -> bytes:
    img = Image.new("RGB", (640, 360), (40, 40, 40))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_detect_returns_person(client: TestClient, fake_detector: _FakeDetector) -> None:
    jpeg = _jpeg_bytes()
    resp = client.post(
        "/detect",
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["detections"]) == 1
    assert body["detections"][0]["label"] == "person"
    assert body["detections"][0]["score"] == 0.91
    assert fake_detector.calls == [jpeg]


def test_detect_empty_image_returns_400(client: TestClient) -> None:
    # Empty UploadFile is rejected by FastAPI; multipart upload with empty bytes yields 400.
    resp = client.post(
        "/detect",
        files={"image": ("empty.jpg", b"", "image/jpeg")},
    )
    assert resp.status_code == 400


def test_detect_min_score_passed_through(client: TestClient) -> None:
    """Verify the form field is forwarded to the detector."""
    seen: list[float] = []

    class _Recording(_FakeDetector):
        def detect_bytes(self, image: bytes, *, min_score: float = DEFAULT_MIN_SCORE):  # noqa: ANN001
            seen.append(min_score)
            return super().detect_bytes(image, min_score=min_score)

    app = create_app(detector=_Recording())  # type: ignore[arg-type]
    with TestClient(app) as c:
        c.post(
            "/detect",
            files={"image": ("frame.jpg", _jpeg_bytes(), "image/jpeg")},
            data={"min_score": "0.77"},
        )
    assert seen == [0.77]
