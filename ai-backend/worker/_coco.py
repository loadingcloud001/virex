# SPDX-License-Identifier: AGPL-3.0
"""Canonical COCO-80 class labels (Ultralytics 0-indexed contiguous IDs).

Used by:
- `worker/camera_worker.py` to map Triton integer class ids back to
  strings before the zone / class filter.
- `detector/postprocess.py` to add `Detection.label` strings.

Single source of truth so the two never drift apart.

Naming convention: do NOT prefix with underscore — `_COCO_NAMES` would
mean "private to this module". The canonical name is `COCO_NAMES`.
"""

from __future__ import annotations

COCO_NAMES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus",
    "train", "truck", "boat", "traffic light", "fire hydrant",
    "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse",
    "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis",
    "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass",
    "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
    "donut", "cake", "chair", "couch", "potted plant", "bed",
    "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


def label_for(class_id: int) -> str:
    """Return the COCO label for a class id, or `object:<id>` if unknown."""
    if 0 <= class_id < len(COCO_NAMES):
        return COCO_NAMES[class_id]
    return f"object:{class_id}"


__all__: tuple[str, ...] = ("COCO_NAMES", "label_for")
