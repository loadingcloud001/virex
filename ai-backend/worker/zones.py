# SPDX-License-Identifier: AGPL-3.0
"""Polygon zone filter with Frigate-style inertia.

A `Zone` is a closed polygon (normalised xy coordinates). Each
detection's `bottom_center` (a single point) is tested against the
polygon; if inside, the detection is tagged with the zone id. The
`inertia` setting requires the detection to be inside the zone for N
consecutive frames before the tag fires — this avoids spurious
single-frame events.

For multi-zone scenes (one detection may be inside two overlapping
zones), every zone the detection is inside is recorded; downstream
rules can decide whether that matters.

The zone tracker is per-camera and lives for the lifetime of the
worker process; it does NOT survive worker restart. That's fine for v1
because the worker only loses ~5 s of detection history on restart and
zones just take longer to re-arm.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

from worker.config_hot import HotZoneCfg

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ZoneHit:
    """A detection that landed inside one or more zones."""

    mtx_path: str
    zone_id: str
    count: int  # how many zones this detection is in


@dataclass(slots=True)
class ZoneFilter:
    """Per-camera zone state.

    Holds:
    - `zones`: static polygons (from HotConfig).
    - `streaks`: per-zone counter of consecutive frames a bbox was
      inside; resets to 0 if it leaves. When the counter reaches the
      zone's `inertia`, the tag fires (and the detection event carries
      the zone_id).

    Designed to be called once per frame per camera, after detect.
    """

    mtx_path: str
    zones: tuple[HotZoneCfg, ...]
    _streaks: dict[str, deque[bool]] = field(default_factory=lambda: defaultdict(deque))

    def __post_init__(self) -> None:
        # Pre-size each zone's streak deque to its inertia length so
        # we don't dynamically grow during a frame burst.
        for z in self.zones:
            self._streaks[z.id] = deque([False] * z.inertia, maxlen=z.inertia)

    def apply(
        self,
        bbox_norm_xyxy: tuple[float, float, float, float],
        track_id: int | None = None,
    ) -> list[str]:
        """Tag the detection with zone_ids it is inside (after inertia).

        `bbox_norm_xyxy` is normalised xyxy in `[0, 1]`. `track_id` is
        optional — if the caller has object tracking, the inertia is
        tracked per (track_id, zone_id); otherwise per (camera, zone_id).
        Returns the list of zone_ids whose inertia threshold is met.
        """
        if not self.zones:
            return []
        x1, y1, x2, y2 = bbox_norm_xyxy
        bx = (x1 + x2) / 2.0
        by = y2  # bottom-center
        fired: list[str] = []
        for z in self.zones:
            inside_now = self._point_in_polygon(bx, by, z.coordinates)
            key = (track_id, z.id) if track_id is not None else z.id
            streak = self._streaks.setdefault(
                key, deque([False] * z.inertia, maxlen=z.inertia)
            )
            streak.append(inside_now)
            if inside_now and all(streak):
                fired.append(z.id)
        return fired

    def reset(self) -> None:
        """Forget all streaks (e.g. on RTSP reconnect)."""
        for k in list(self._streaks.keys()):
            self._streaks[k] = deque(
                [False] * self._zones_max_inertia(),
                maxlen=self._zones_max_inertia(),
            )

    def _zones_max_inertia(self) -> int:
        return max((z.inertia for z in self.zones), default=1)

    @staticmethod
    def _point_in_polygon(
        x: float, y: float, polygon: tuple[tuple[float, float], ...]
    ) -> bool:
        """Even-odd rule (ray casting). Works for any non-self-intersecting
        polygon. Coordinates are normalised `[0, 1]`."""
        if len(polygon) < 3:
            return False
        inside = False
        n = len(polygon)
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]
            # Edge crosses the horizontal line at y if `y` is strictly
            # between the y-coordinates and the x at that y is to the
            # right of the test point.
            if (y1 > y) != (y2 > y):
                x_intersect = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
                if x < x_intersect:
                    inside = not inside
        return inside


def build_zone_filter(mtx_path: str, zones: Iterable[HotZoneCfg]) -> ZoneFilter:
    """Construct a ZoneFilter from config."""
    z = tuple(zones)
    if not z:
        return ZoneFilter(mtx_path=mtx_path, zones=())
    return ZoneFilter(mtx_path=mtx_path, zones=z)


__all__: tuple[str, ...] = (
    "ZoneHit",
    "ZoneFilter",
    "build_zone_filter",
)
