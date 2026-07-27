# SPDX-License-Identifier: Apache-2.0
"""Pure-function clip-builder tests for ffmpeg_runner (no subprocess)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from clip_builder.ffmpeg_runner import (
    CLIP_POST_SEC,
    CLIP_PRE_SEC,
    parse_segment_filename,
    segments_covering,
)


def test_parse_segment_filename_valid() -> None:
    p = Path("/rec/t1c5h264/2026-07-26_13-00-00-123456.mp4")
    info = parse_segment_filename(p)
    assert info is not None
    assert info.start_utc.year == 2026
    assert info.start_utc.month == 7
    assert info.start_utc.day == 26
    assert info.start_utc.hour == 13


def test_parse_segment_filename_invalid_returns_none() -> None:
    assert parse_segment_filename(Path("/rec/x/not-a-valid-name.mp4")) is None


def test_segments_covering_no_segments(tmp_path: Path) -> None:
    assert segments_covering(tmp_path, "t1c5", datetime.now(UTC)) == []


def test_segments_covering_selects_window(tmp_path: Path) -> None:
    # Create fake segments at fixed offsets.
    cam_dir = tmp_path / "t1c5h264"
    cam_dir.mkdir()
    (cam_dir / "2026-07-26_13-00-00-000000.mp4").write_bytes(b"")
    (cam_dir / "2026-07-26_13-00-30-000000.mp4").write_bytes(b"")
    (cam_dir / "2026-07-26_13-01-00-000000.mp4").write_bytes(b"")
    event = datetime(2026, 7, 26, 13, 0, 35)  # 5s past segment-1 boundary
    picked = segments_covering(tmp_path, "t1c5h264", event)
    # Window [13:00:30, 13:00:40] overlaps the trailing edge of segment-0
    # AND the leading edge of segment-1 (boundary inclusive on both ends).
    # ffmpeg concat demuxer with `-ss 5 -t 10` cleanly resolves this.
    assert len(picked) == 2


def test_segments_covering_with_naive_event(tmp_path: Path) -> None:
    cam_dir = tmp_path / "t1c5h264"
    cam_dir.mkdir()
    (cam_dir / "2026-07-26_13-00-00-000000.mp4").write_bytes(b"")
    event = datetime(2026, 7, 26, 13, 0, 10)
    picked = segments_covering(tmp_path, "t1c5h264", event)
    assert len(picked) == 1


def test_default_clip_constants() -> None:
    assert CLIP_PRE_SEC == 5
    assert CLIP_POST_SEC == 5
    assert CLIP_PRE_SEC + CLIP_POST_SEC == 10
