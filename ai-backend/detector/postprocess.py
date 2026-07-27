# SPDX-License-Identifier: AGPL-3.0
"""Postprocessing for Ultralytics YOLO outputs.

`ultralytics.YOLO.predict()` returns a `Results` object per input image
with `.boxes.data` containing rows of
`[x1, y1, x2, y2, score, class_id]` in pixel coordinates. We:

1. Threshold by `min_score`.
2. Filter to `keep_classes` (defaults to `person`).
3. Normalise xyxy to [0, 1] using the input image height/width.
4. Sort descending by score.

Output schema is the same frozen `Detection` dataclass the rest of the
codebase consumes, so the swap from RT-DETR → YOLO is invisible
upstream.

The COCO label list (`COCO_LABELS`) and `label_for()` are re-exported
from `worker/_coco.py` to keep a single source of truth — both
`detector/postprocess.py` (this file) and `worker/camera_worker.py`
need to map Ultralytics integer class ids to human-readable names.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from worker._coco import COCO_NAMES as COCO_LABELS
from worker._coco import label_for

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COCO_PERSON_ID: int = 0  # Ultralytics uses contiguous 0-indexed COCO ids
DEFAULT_MIN_SCORE: float = 0.5
PERSON_LABEL: str = "person"


@dataclass(frozen=True, slots=True)
class Detection:
    """A single detection in normalised image coordinates."""

    label: str
    score: float
    box: tuple[float, float, float, float]  # (x1, y1, x2, y2) in [0, 1]


def parse_yolo_results(
    results,  # ultralytics.engine.results.Results (avoid hard import at module load)
    *,
    image_hw: tuple[int, int],
    keep_classes: set[int] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[Detection]:
    """Convert one Ultralytics `Results` (single image) into a flat `Detection` list.

    Args:
        results: A single-element list from `model.predict()`, or a
            single `Results` object.
        image_hw: (height, width) of the original frame.
        keep_classes: COCO class ids to keep. `None` → keep all classes.
        min_score: Minimum confidence to retain.

    Returns:
        Detections sorted by descending score, in normalised coordinates.
    """
    h, w = image_hw
    if h <= 0 or w <= 0:
        logger.warning("postprocess_invalid_hw", h=h, w=w)
        return []

    results_iter = (
        [results] if hasattr(results, "boxes") else list(results)
    )

    out: list[Detection] = []
    for r in results_iter:
        if r.boxes is None or len(r.boxes) == 0:
            continue
        for box in r.boxes:
            try:
                row = box.data
                # Ultralytics tensor has `.tolist()`; tests + numpy fallback to
                # plain list iteration. Both supported.
                values = (
                    row[0].tolist() if hasattr(row, "tolist") else list(row[0])
                )
                x1, y1, x2, y2, score, cls_id = (float(v) for v in values)
            except (ValueError, TypeError, IndexError):
                continue
            cls_id_int = int(cls_id)
            if score < min_score:
                continue
            if keep_classes is not None and cls_id_int not in keep_classes:
                continue
            out.append(
                Detection(
                    label=label_for(cls_id_int),
                    score=round(score, 4),
                    box=(
                        max(0.0, x1 / w),
                        max(0.0, min(1.0, y1 / h)),
                        min(1.0, x2 / w),
                        min(1.0, y2 / h),
                    ),
                )
            )
    out.sort(key=lambda d: d.score, reverse=True)
    return out


__all__: tuple[str, ...] = (
    "Detection",
    "COCO_LABELS",
    "COCO_PERSON_ID",
    "DEFAULT_MIN_SCORE",
    "PERSON_LABEL",
    "label_for",
    "parse_yolo_results",
)
