# SPDX-License-Identifier: Apache-2.0
"""ffmpeg subprocess wrapper for cutting a clip from MediaMTX fMP4 segments.

v1 keeps it rock-simple: concatenate every segment file whose time
window covers `(event_ts - PRE_SEC, event_ts + POST_SEC)`, then trim
via `ffmpeg -c copy -t 10` to the requested window. We never re-encode
so the cut is lossless and uses ~10× less CPU than the recorder itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLIP_PRE_SEC: int = 5
CLIP_POST_SEC: int = 5
CLIP_TOTAL_SEC: int = CLIP_PRE_SEC + CLIP_POST_SEC
SEGMENT_SEAL_WAIT_SEC: int = 35  # 30 s segment length + 5 s safety.
SEGMENT_DURATION_SEC: int = 30  # Matches mediamtx.yml `recordSegmentDuration`.
SEGMENT_FILENAME_SUFFIX = ".mp4"


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """A sealed MediaMTX recording segment on disk."""

    path: Path
    start_utc: datetime  # timestamp encoded in the filename


def parse_segment_filename(path: Path) -> SegmentInfo | None:
    """Parse `2026-07-26_13-00-00-123456.mp4` into a UTC datetime.

    MediaMTX writes wall-clock UTC into segment names.
    """
    stem = path.stem
    try:
        date_part, time_part = stem.split("_", 1)
        hms, us_str = time_part.rsplit("-", 1)
        us = int(us_str) if us_str.isdigit() else 0
        year, month, day = (int(x) for x in date_part.split("-"))
        hh, mm, ss = (int(x) for x in hms.split("-"))
        start = datetime(year, month, day, hh, mm, ss, us, tzinfo=None)
        return SegmentInfo(path=path, start_utc=start)
    except (ValueError, IndexError):
        logger.warning("bad_segment_filename", path=str(path))
        return None


def list_segments_for_path(recording_dir: Path, mtx_path: str) -> list[SegmentInfo]:
    """Return all sealed segments for `mtx_path` sorted by start time."""
    folder = recording_dir / mtx_path
    if not folder.is_dir():
        return []
    out: list[SegmentInfo] = []
    for p in folder.iterdir():
        if p.suffix != SEGMENT_FILENAME_SUFFIX or not p.is_file():
            continue
        info = parse_segment_filename(p)
        if info is not None:
            out.append(info)
    out.sort(key=lambda s: s.start_utc)
    return out


def segments_covering(
    recording_dir: Path,
    mtx_path: str,
    event_ts: datetime,
    *,
    pre_sec: int = CLIP_PRE_SEC,
    post_sec: int = CLIP_POST_SEC,
    segment_duration_sec: int = SEGMENT_DURATION_SEC,
) -> list[Path]:
    """Return segment paths whose time range overlaps the clip window."""
    segs = list_segments_for_path(recording_dir, mtx_path)
    if not segs:
        return []
    # MediaMTX writes segment names in naive UTC, so normalise event_ts too.
    ev_naive = event_ts.replace(tzinfo=None) if event_ts.tzinfo else event_ts
    window_start = ev_naive - timedelta(seconds=pre_sec)
    window_end = ev_naive + timedelta(seconds=post_sec)
    seg_len = timedelta(seconds=segment_duration_sec)
    out: list[Path] = []
    for seg in segs:
        seg_end = seg.start_utc + seg_len
        if seg.start_utc <= window_end and seg_end >= window_start:
            out.append(seg.path)
    return out


async def build_clip(
    recording_dir: Path,
    mtx_path: str,
    event_ts: datetime,
    *,
    out_path: Path,
    pre_sec: int = CLIP_PRE_SEC,
    post_sec: int = CLIP_POST_SEC,
) -> Path | None:
    """Concatenate MediaMTX segments and trim to the requested window.

    Uses the `ffmpeg concat demuxer` with `-c copy -t 10` so we pay no
    decode/encode cost. The output clip carries its moov atom at the
    start (`+faststart`) for browser streaming.
    """
    segs = segments_covering(
        recording_dir, mtx_path, event_ts, pre_sec=pre_sec, post_sec=post_sec
    )
    if not segs:
        logger.warning("no_segments_for_event", mtx_path=mtx_path, ts=event_ts.isoformat())
        return None

    # If the most recently-listed segment is still being appended to by
    # MediaMTX (file mtime younger than SEGMENT_DURATION_SEC), wait for it
    # to seal before concatenating — otherwise its moov atom may be
    # incomplete and ffmpeg will reject the concat input.
    most_recent = segs[-1]
    try:
        mtime_age = datetime.now().timestamp() - most_recent.stat().st_mtime
        if mtime_age < SEGMENT_DURATION_SEC:
            logger.info("waiting_for_segment_seal", seg=most_recent.name, age=mtime_age)
            await asyncio.sleep(SEGMENT_SEAL_WAIT_SEC)
    except OSError:
        pass

    concat_path = out_path.parent / f"{out_path.stem}.concat.txt"
    concat_lines = [f"file '{p.resolve()}'" for p in segs]
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-ss",
            str(pre_sec),
            "-t",
            str(pre_sec + post_sec),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "ffmpeg_clip_failed",
                rc=proc.returncode,
                stderr=stderr.decode("utf-8", "replace")[:500],
            )
            return None
        logger.info("clip_built", mtx_path=mtx_path, out=str(out_path), secs=post_sec + pre_sec)
        return out_path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(concat_path)


__all__: tuple[str, ...] = (
    "SegmentInfo",
    "parse_segment_filename",
    "list_segments_for_path",
    "segments_covering",
    "build_clip",
    "CLIP_PRE_SEC",
    "CLIP_POST_SEC",
    "CLIP_TOTAL_SEC",
)
