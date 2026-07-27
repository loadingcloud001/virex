# SPDX-License-Identifier: AGPL-3.0
"""Unit tests for the Ultralytics YOLO postprocessor.

`parse_yolo_results()` is a pure function over Ultralytics `Results`
shapes. Tests construct minimal stub objects that mimic `.boxes.data`
so we can run without torch / ultralytics installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from detector.postprocess import (
    DEFAULT_MIN_SCORE,
    Detection,
    label_for,
    parse_yolo_results,
)


@dataclass
class _FakeBoxRow:
    """Mimics a single Ultralytics detection row (`tensor([…], shape=(6,))`)."""

    values: list[float]

    def __getitem__(self, idx: int) -> _FakeBoxRow | float:
        return self.values[idx]

    def tolist(self) -> list[float]:
        return list(self.values)

    def __iter__(self):
        return iter(self.values)


@dataclass
class _FakeBox:
    """Mimics `Results.boxes[i]` — has `.data` of shape (1, 6)."""

    row: list[float] = field(default_factory=list)

    @property
    def data(self) -> _FakeBoxData:
        return _FakeBoxData(self.row)


@dataclass
class _FakeBoxData:
    """Mimics `ultralytics.engine.results.Boxes.data` shape (1, 6)."""

    row: list[float]

    def __getitem__(self, idx: int) -> _FakeBoxRow:
        return _FakeBoxRow(self.row)

    def __iter__(self):
        return iter([self])


@dataclass
class _FakeBoxes:
    """Mimics `ultralytics.engine.results.Results.boxes`."""

    rows: list[list[float]] = field(default_factory=list)

    def __iter__(self):
        return iter([_FakeBox(row=row) for row in self.rows])

    def __len__(self) -> int:
        return len(self.rows)


@dataclass
class _FakeResult:
    boxes: _FakeBoxes | None = None


def _result(rows: list[list[float]]) -> list[_FakeResult]:
    """Helper to wrap a list of detection rows as a Results-like iterable."""
    boxes = _FakeBoxes(rows=list(rows))
    return [_FakeResult(boxes=boxes)]


def _approx_box(x1: float, y1: float, x2: float, y2: float) -> tuple:
    return (
        pytest.approx(x1, abs=1e-3),
        pytest.approx(y1, abs=1e-3),
        pytest.approx(x2, abs=1e-3),
        pytest.approx(y2, abs=1e-3),
    )


def test_single_high_confidence_person_detection() -> None:
    """One person box at score 0.92 → single Detection with normalised box."""
    # COCO class 0 = person in Ultralytics contiguous indexing.
    rows = [[64.0, 80.0, 192.0, 252.0, 0.92, 0]]
    results = _result(rows)
    h, w = 360, 320

    detections = parse_yolo_results(results, image_hw=(h, w))

    assert len(detections) == 1
    d = detections[0]
    assert isinstance(d, Detection)
    assert d.label == "person"
    assert d.score == 0.92
    # x=64/320=0.2, y=80/360≈0.2222, x2=192/320=0.6, y2=252/360=0.7.
    assert d.box == _approx_box(0.2, 0.2222, 0.6, 0.7)


def test_filters_below_threshold() -> None:
    rows = [[0.0, 0.0, 10.0, 10.0, 0.3, 0]]
    out = parse_yolo_results(_result(rows), image_hw=(100, 100), min_score=0.5)
    assert out == []


def test_filters_non_person_class_when_keep_classes_set() -> None:
    """With `keep_classes={0}` (person), car (cls=2) is dropped."""
    rows = [[0.0, 0.0, 50.0, 50.0, 0.95, 2]]  # COCO 'car'
    out = parse_yolo_results(
        _result(rows), image_hw=(100, 100), keep_classes={0}
    )
    assert out == []


def test_keeps_all_classes_when_keep_classes_none() -> None:
    rows = [
        [0.0, 0.0, 10.0, 10.0, 0.9, 0],   # person
        [0.0, 0.0, 20.0, 20.0, 0.85, 2],  # car
    ]
    out = parse_yolo_results(_result(rows), image_hw=(100, 100))
    assert len(out) == 2
    assert {d.label for d in out} == {"person", "car"}


def test_clamps_out_of_frame() -> None:
    rows = [[-10.0, -10.0, 9999.0, 9999.0, 0.7, 0]]
    out = parse_yolo_results(_result(rows), image_hw=(100, 100))
    assert out[0].box == (0.0, 0.0, 1.0, 1.0)


def test_sorted_descending_score() -> None:
    rows = [
        [0, 0, 1, 1, 0.6, 0],
        [0, 0, 1, 1, 0.9, 0],
        [0, 0, 1, 1, 0.7, 0],
    ]
    out = parse_yolo_results(_result(rows), image_hw=(1, 1))
    assert [d.score for d in out] == [0.9, 0.7, 0.6]


def test_rejects_zero_hw() -> None:
    rows = [[0, 0, 1, 1, 0.9, 0]]
    out = parse_yolo_results(_result(rows), image_hw=(0, 0))
    assert out == []


def test_handles_empty_boxes() -> None:
    """An Ultralytics Results with no boxes yields zero detections."""
    results = [_FakeResult(boxes=_FakeBoxes(rows=[]))]
    out = parse_yolo_results(results, image_hw=(640, 640))
    assert out == []


def test_handles_none_boxes() -> None:
    """Some model variants set `results.boxes = None` (no detections)."""
    results = [_FakeResult(boxes=None)]
    out = parse_yolo_results(results, image_hw=(640, 640))
    assert out == []


def test_label_for_known_classes() -> None:
    assert label_for(0) == "person"
    assert label_for(2) == "car"
    assert label_for(7) == "truck"


def test_label_for_unknown_class() -> None:
    assert label_for(999) == "object:999"


def test_default_min_score_is_half() -> None:
    assert DEFAULT_MIN_SCORE == 0.5


def test_multiple_persons_with_some_filtered() -> None:
    rows = [
        [0, 0, 10, 10, 0.4, 0],
        [100, 100, 200, 200, 0.95, 0],
        [150, 150, 300, 300, 0.85, 0],
    ]
    out = parse_yolo_results(
        _result(rows), image_hw=(1000, 1000), min_score=0.5
    )
    assert len(out) == 2
    assert {d.score for d in out} == {0.95, 0.85}


def test_accepts_single_result_object_not_list() -> None:
    """Some callers pass a single Results, not a list."""
    boxes = _FakeBoxes(rows=[[0, 0, 1, 1, 0.9, 0]])
    result = _FakeResult(boxes=boxes)
    out = parse_yolo_results(result, image_hw=(1, 1))
    assert len(out) == 1


def test_detector_module_imports() -> None:
    """Sanity: the Detector class is importable (does NOT load the model)."""
    from detector.service import Detector

    assert hasattr(Detector, "detect_bytes")
