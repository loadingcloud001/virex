# SPDX-License-Identifier: AGPL-3.0
"""Detector service for Virex — Ultralytics YOLOv8m wrapped behind FastAPI.

This replaces the earlier RT-DETR-R18 (HuggingFace transformers)
implementation. Ultralytics YOLO is the dominant open-source CV
ecosystem; using it lets Virex tap into:

- The huge pretrained zoo (YOLOv8/v11/v26, classification,
  segmentation, pose).
- A single SDK for inference + training + export + visualisation.
- Easy fine-tuning per tenant via Ultralytics CLI / Roboflow Hub.

The HTTP surface is exposed via `detector.app:VirexDetectorApp` and run:

    uvicorn detector.app:VirexDetectorApp --host 0.0.0.0 --port 31001

The response schema is identical to the previous RT-DETR version:
`{"detections": [{"label", "score", "box"}]}` so worker + test code is
unchanged.

For production deployments, the inference backend is **NVIDIA Triton
Inference Server** (`nvcr.io/nvidia/tritonserver`); the worker talks
to Triton directly via KServe HTTP. `Detector` here is still useful
for:

- Local dev (no Triton needed)
- A thin HTTP shim for browser / curl testing
- A fallback if Triton is down
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import structlog
from PIL import Image

from detector.postprocess import DEFAULT_MIN_SCORE, Detection, parse_yolo_results

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults — overridable via env so a single image can serve many model sizes
# without code changes.
# ---------------------------------------------------------------------------
DEFAULT_MODEL_ID: str = "yolov8m.pt"  # Ultralytics uses bare name or path
DEFAULT_IMGSZ: int = 640
DEFAULT_DEVICE: str = "cuda:0" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"


class Detector:
    """Pure inference wrapper around Ultralytics YOLOv8m.

    `VirexDetectorApp` (FastAPI) owns the lifecycle and delegates each
    `/detect` request to `Detector.detect_bytes()` so the model class
    stays trivially testable in isolation.
    """

    def __init__(
        self,
        model_id: str | None = None,
        *,
        device: str | None = None,
        imgsz: int | None = None,
        confidence_threshold: float | None = None,
        iou_threshold: float = 0.45,
        max_detections: int = 100,
    ) -> None:
        # Heavy import is lazy so unit tests can import this module
        # without torch + ultralytics installed.
        from ultralytics import YOLO  # noqa: PLC0415

        model_id = model_id or os.environ.get("YOLO_MODEL", DEFAULT_MODEL_ID)
        device = device or os.environ.get("YOLO_DEVICE", DEFAULT_DEVICE)
        imgsz = imgsz or int(os.environ.get("YOLO_IMGSZ", DEFAULT_IMGSZ))
        confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else float(os.environ.get("YOLO_CONF", DEFAULT_MIN_SCORE))
        )

        logger.info(
            "detector_init_start",
            model=model_id,
            device=device,
            imgsz=imgsz,
            confidence_threshold=confidence_threshold,
        )

        # Ultralytics auto-downloads weights from its hub on first
        # construction. The /weights directory is mounted at build
        # time; if a local `.pt` file exists, use it; otherwise let
        # Ultralytics fetch from its CDN.
        model_path = self._resolve_model_path(model_id)
        self.model: YOLO = YOLO(model_path)
        self._device = device
        self._imgsz = imgsz
        self._conf = confidence_threshold
        self._iou = iou_threshold
        self._max_det = max_detections
        logger.info(
            "detector_init_done",
            model=str(model_path),
            classes=self.model.names,
        )

    @staticmethod
    def _resolve_model_path(model_id: str) -> str:
        """If a local weights file exists, use it; otherwise pass the
        Ultralytics identifier through (it downloads from its hub)."""
        # Operator can mount weights at /weights/<name>.pt for offline use.
        local_candidate = Path(f"/weights/{model_id}")
        if local_candidate.exists():
            return str(local_candidate)
        return model_id

    def detect_bytes(
        self,
        image: bytes,
        *,
        min_score: float | None = None,
        keep_classes: set[int] | None = None,
    ) -> list[Detection]:
        """Run inference on raw JPEG bytes; return normalised detections."""
        img = Image.open(io.BytesIO(image)).convert("RGB")
        # `verbose=False` suppresses Ultralytics' per-image log spam
        # which would otherwise dominate structlog output.
        results = self.model.predict(
            img,
            conf=min_score if min_score is not None else self._conf,
            iou=self._iou,
            imgsz=self._imgsz,
            max_det=self._max_det,
            device=self._device,
            verbose=False,
        )
        return parse_yolo_results(
            results,
            image_hw=(img.height, img.width),
            keep_classes=keep_classes,
            min_score=min_score if min_score is not None else self._conf,
        )

    def detect_to_xyxy_pixels(
        self,
        image: bytes,
        *,
        min_score: float | None = None,
        keep_classes: set[int] | None = None,
    ) -> tuple[list[Detection], int, int]:
        """Convenience: returns detections + image height/width for callers
        that need pixel coordinates (e.g. annotation overlay rendering).
        """
        img = Image.open(io.BytesIO(image)).convert("RGB")
        results = self.model.predict(
            img,
            conf=min_score if min_score is not None else self._conf,
            iou=self._iou,
            imgsz=self._imgsz,
            max_det=self._max_det,
            device=self._device,
            verbose=False,
        )
        return (
            parse_yolo_results(
                results,
                image_hw=(img.height, img.width),
                keep_classes=keep_classes,
                min_score=min_score if min_score is not None else self._conf,
            ),
            img.height,
            img.width,
        )


__all__: tuple[str, ...] = (
    "Detector",
    "DEFAULT_MODEL_ID",
    "DEFAULT_IMGSZ",
    "DEFAULT_DEVICE",
)
