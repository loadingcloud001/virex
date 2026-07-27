# SPDX-License-Identifier: AGPL-3.0
"""Tests for the polygon zone filter."""

from __future__ import annotations

import pytest

from worker.config_hot import HotZoneCfg
from worker.zones import ZoneFilter, build_zone_filter


def _zone(
    zone_id: str = "entrance",
    inertia: int = 3,
    coordinates: tuple[tuple[float, float], ...] = (
        (0.4, 0.5),
        (0.6, 0.5),
        (0.6, 0.8),
        (0.4, 0.8),
    ),
) -> HotZoneCfg:
    return HotZoneCfg(
        id=zone_id,
        friendly_name=f"Zone {zone_id}",
        objects=("person",),
        inertia=inertia,
        coordinates=coordinates,
    )


# ---------------------------------------------------------------------------
# Polygon point-in-bbox test (ray casting)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "x,y,inside",
    [
        (0.5, 0.65, True),    # dead centre
        (0.5, 0.5, True),     # on the bottom edge → inside (boundary)
        (0.4, 0.65, True),    # on the left edge → inside
        (0.39, 0.65, False),  # just outside left edge
        (0.61, 0.65, False),  # just outside right edge
        (0.5, 0.81, False),   # above the top edge
        (0.5, 0.49, False),   # below the bottom edge
        (0.0, 0.0, False),    # origin
        (1.0, 1.0, False),    # far corner
    ],
)
def test_point_in_polygon_rectangle(x: float, y: float, inside: bool) -> None:
    poly = ((0.4, 0.5), (0.6, 0.5), (0.6, 0.8), (0.4, 0.8))
    assert ZoneFilter._point_in_polygon(x, y, poly) is inside


def test_point_in_polygon_triangle() -> None:
    """Triangle vertices (0,0)-(1,0)-(0.5,1): apex on top.

    At y=0.6 the triangle's horizontal extent is x ∈ [0.3, 0.7]
    (left edge slopes up-right from (0,0) to (0.5,1); right edge
    slopes up-left from (1,0) to (0.5,1)). So (0.5, 0.6) is INSIDE.
    """
    tri = ((0.0, 0.0), (1.0, 0.0), (0.5, 1.0))
    # Centroid (≈ 0.33, 0.33): inside.
    assert ZoneFilter._point_in_polygon(0.5, 0.2, tri) is True
    # Above centroid but within triangle bounds: inside.
    assert ZoneFilter._point_in_polygon(0.5, 0.6, tri) is True
    # Above the apex (y > 1): outside.
    assert ZoneFilter._point_in_polygon(0.5, 1.5, tri) is False


def test_point_in_polygon_degenerate() -> None:
    """Polygon with < 3 points → always outside."""
    assert ZoneFilter._point_in_polygon(0.5, 0.5, ()) is False
    assert ZoneFilter._point_in_polygon(0.5, 0.5, ((0.0, 0.0),)) is False
    assert ZoneFilter._point_in_polygon(0.5, 0.5, ((0.0, 0.0), (1.0, 0.0))) is False


# ---------------------------------------------------------------------------
# ZoneFilter.apply — inertia + tracking
# ---------------------------------------------------------------------------
def test_zone_filter_no_zones_returns_empty() -> None:
    zf = build_zone_filter("t1c5h264", [])
    assert zf.apply((0.5, 0.5, 0.6, 0.6)) == []


def test_zone_filter_applies_zone_after_inertia_frames() -> None:
    """Default inertia is 3 frames; detection inside zone 3 frames in a
    row should fire; under 3 should not fire."""
    zf = build_zone_filter("t1c5h264", [_zone(inertia=3)])
    # bbox bottom-center (0.5, 0.65) is inside the zone.
    bbox = (0.4, 0.5, 0.6, 0.65)
    # Frame 1: inside, but inertia not met.
    assert zf.apply(bbox) == []
    # Frame 2: still inside.
    assert zf.apply(bbox) == []
    # Frame 3: inertia met → zone fires.
    assert zf.apply(bbox) == ["entrance"]


def test_zone_filter_resets_when_leaves_zone() -> None:
    zf = build_zone_filter("t1c5h264", [_zone(inertia=3)])
    inside = (0.4, 0.5, 0.6, 0.65)
    outside = (0.0, 0.0, 0.1, 0.1)
    zf.apply(inside)
    zf.apply(inside)
    # Now leave → counter resets.
    zf.apply(outside)
    # 3 frames back inside → still needs 3 consecutive inside frames.
    assert zf.apply(inside) == []
    assert zf.apply(inside) == []
    assert zf.apply(inside) == ["entrance"]


def test_zone_filter_with_inertia_1_fires_immediately() -> None:
    zf = build_zone_filter("t1c5h264", [_zone(inertia=1)])
    bbox = (0.4, 0.5, 0.6, 0.65)
    assert zf.apply(bbox) == ["entrance"]


def test_zone_filter_multiple_zones() -> None:
    """A detection inside 2 zones → both fire."""
    zf = build_zone_filter(
        "t1c5h264",
        [
            _zone(zone_id="zone_a", coordinates=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))),
            _zone(zone_id="zone_b", coordinates=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))),
        ],
    )
    # Same inertia = 3 for both.
    bbox = (0.4, 0.4, 0.6, 0.6)
    zf.apply(bbox)
    zf.apply(bbox)
    fired = zf.apply(bbox)
    assert set(fired) == {"zone_a", "zone_b"}


def test_zone_filter_per_track_inertia() -> None:
    """When `track_id` is provided, each track has its own streak counter."""
    zf = build_zone_filter("t1c5h264", [_zone(inertia=3)])
    bbox = (0.4, 0.5, 0.6, 0.65)
    # Track 1 gets 3 consecutive inside → fires.
    assert zf.apply(bbox, track_id=1) == []
    assert zf.apply(bbox, track_id=1) == []
    assert zf.apply(bbox, track_id=1) == ["entrance"]
    # Track 2 is fresh → not yet 3 frames.
    assert zf.apply(bbox, track_id=2) == []
    assert zf.apply(bbox, track_id=2) == []


def test_zone_filter_reset_clears_streaks() -> None:
    zf = build_zone_filter("t1c5h264", [_zone(inertia=3)])
    bbox = (0.4, 0.5, 0.6, 0.65)
    zf.apply(bbox)
    zf.apply(bbox)
    zf.reset()
    # After reset, must start over.
    assert zf.apply(bbox) == []
    assert zf.apply(bbox) == []
    assert zf.apply(bbox) == ["entrance"]


# ---------------------------------------------------------------------------
# Build helper
# ---------------------------------------------------------------------------
def test_build_zone_filter_with_zones() -> None:
    zf = build_zone_filter(
        "t1c5h264",
        [_zone(zone_id="a"), _zone(zone_id="b", inertia=2)],
    )
    assert zf.mtx_path == "t1c5h264"
    assert len(zf.zones) == 2
    assert zf.zones[0].id == "a"
    assert zf.zones[1].id == "b"


def test_build_zone_filter_no_zones() -> None:
    zf = build_zone_filter("t1c5h264", [])
    assert zf.mtx_path == "t1c5h264"
    assert zf.zones == ()
