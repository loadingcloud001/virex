# SPDX-License-Identifier: AGPL-3.0
"""FastAPI app wrapping `Detector` (Ultralytics YOLOv8m).

Endpoints:
  GET  /healthz   — liveness probe; returns 200 once the model loads.
  POST /detect    — multipart/form-data: `image=<JPEG>`; returns JSON.

Run:
    uvicorn detector.app:VirexDetectorApp --host 0.0.0.0 --port 31001

In production, the worker talks to Triton directly via KServe HTTP;
this FastAPI shim remains for local dev + browser testing.
"""

from __future__ import annotations

import threading

import structlog
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from detector.postprocess import DEFAULT_MIN_SCORE
from detector.service import Detector

logger = structlog.get_logger(__name__)


def create_app(detector: Detector | None = None) -> FastAPI:
    """Factory; tests pass a pre-loaded detector to avoid CUDA init."""
    app = FastAPI(
        title="Virex Detector",
        version="0.2.0",
        description="Ultralytics YOLOv8m person/vehicle detector (AGPL-3.0 weights).",
    )
    app.state.detector = detector  # None → lazy init on first /detect

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "model": "ultralytics-yolov8m"}

    def _ensure_detector() -> Detector:
        """Lazy singleton init (thread-safe so uvicorn workers don't race)."""
        if app.state.detector is None:
            with _init_lock:
                if app.state.detector is None:
                    app.state.detector = Detector()
        return app.state.detector

    @app.post("/detect")
    async def detect(
        image: UploadFile = File(...),  # noqa: B008
        min_score: float = Form(DEFAULT_MIN_SCORE),
    ) -> dict[str, list[dict[str, float | list[float] | str]]]:
        raw = await image.read()
        if not raw:
            raise HTTPException(status_code=400, detail="empty image")
        det = _ensure_detector()
        detections = det.detect_bytes(raw, min_score=min_score)
        return {
            "detections": [
                {"label": d.label, "score": d.score, "box": list(d.box)}
                for d in detections
            ]
        }

    return app


_init_lock = threading.Lock()


def _default_app() -> FastAPI:
    """Lazy constructor used at module import."""
    return create_app()


VirexDetectorApp = _default_app()


__all__: tuple[str, ...] = ("VirexDetectorApp", "create_app")
