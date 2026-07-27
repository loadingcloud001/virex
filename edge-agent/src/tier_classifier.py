# SPDX-License-Identifier: Apache-2.0
"""Tier classification for edge-agent reconcile.

When `config_watcher` notices a workers.yaml change, we diff the new
bundle against the last-applied one and assign each change to a tier:

  A — worker-side atomic swap (zero downtime, no MediaMTX change)
  B — per-camera RTSP reconnect / MQTT / COS endpoint change
  C — MediaMTX path config change (`record`) or transcoder config
  D — add / remove / rename camera (full compose up)

The classifier is a pure function so it's trivially testable without
any subprocess or filesystem state.

Edge-agent's apply layer then routes each tier to the right action:
  A → trigger `POST /admin/reload` on each worker (they swap locally)
  B → trigger `POST /admin/reload` on each worker (their Reloader reconnects)
  C → `docker compose -f transcoder up -d --force-recreate` (per sidecar)
       or `safe_write mediamtx.yml + docker restart mediamtx`
  D → existing `run_reconcile()` path
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Importing EdgeConfigBundle / CameraEdgeDTO from config_pull keeps the
# single source of truth on the wire schema. Tier classification works
# on Pydantic models, then translates to worker-side classifiers.
from pydantic import BaseModel

from src.config_pull import CameraEdgeDTO, EdgeConfigBundle


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TierReport:
    """What changed between two bundles, grouped by tier."""

    added: tuple[CameraEdgeDTO, ...] = ()
    removed: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    tier_a_per_camera: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tier_b_per_camera: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tier_c_paths: tuple[str, ...] = ()
    tier_c_transcoders: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.renamed
            or self.tier_a_per_camera
            or self.tier_b_per_camera
            or self.tier_c_paths
            or self.tier_c_transcoders
        )


# ---------------------------------------------------------------------------
# workers.yaml schema (different from EdgeConfigBundle — workers.yaml
# lacks config_version; it is the FILE, not the wire payload).
# ---------------------------------------------------------------------------
class _WorkersYamlSchema(BaseModel):
    """Mirror of `workers.yaml` for parsing local file changes.

    Edge-agent's config watcher uses this (not EdgeConfigBundle)
    because `workers.yaml` is a flat file: it has no `config_version`
    (versions are inferred from mtime) and no node_id wrapper. Only the
    `cameras:` block is meaningful for tier classification.
    """

    cameras: list[CameraEdgeDTO] = []
    node_id: int | None = None


def parse_workers_yaml(path: str) -> EdgeConfigBundle:
    """Parse `workers.yaml` (or any local flat file) into an EdgeConfigBundle.

    `config_version` is synthesised from the file's mtime (nanoseconds
    since epoch, mod 2^31) so successive writes produce monotonically
    increasing versions. `node_id` is read from the YAML, or defaults to
    whatever the bundle schema requires.
    """
    import yaml  # noqa: PLC0415

    from src.config import Settings  # noqa: PLC0415

    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    parsed = _WorkersYamlSchema.model_validate(data)
    settings = Settings()
    mtime_ns = Path(path).stat().st_mtime_ns
    return EdgeConfigBundle(
        node_id=parsed.node_id or settings.node_id,
        config_version=int(mtime_ns & 0x7FFFFFFF),
        cameras=parsed.cameras,
    )


# ---------------------------------------------------------------------------
# Detection field set — anything under `detect.*` is Tier A. Shared with
# the worker so both ends classify the same way.
# ---------------------------------------------------------------------------
_DETECT_FIELDS: frozenset[str] = frozenset({"fps", "min_score", "classes", "roi"})


def classify(
    old: EdgeConfigBundle | None,
    new: EdgeConfigBundle,
) -> TierReport:
    """Pure: diff `old` vs `new` and return a TierReport.

    Pass `None` for `old` when this is the first reconciliation; the
    classifier treats every camera as `added`.
    """
    new_by_path = {c.mtx_path: c for c in new.cameras}
    old_by_path: dict[str, CameraEdgeDTO] = {}
    if old is not None:
        old_by_path = {c.mtx_path: c for c in old.cameras}

    added: list[CameraEdgeDTO] = []
    removed: list[str] = []
    renamed: list[tuple[str, str]] = []
    tier_a: dict[str, list[str]] = {}
    tier_b: dict[str, list[str]] = {}
    tier_c_paths: list[str] = []

    # ----- D-tier: cameras in new but not in old (or first reconcile) -----
    for path, cam in new_by_path.items():
        if path not in old_by_path:
            added.append(cam)

    # ----- D-tier: cameras in old but not in new -----
    for path in old_by_path:
        if path not in new_by_path:
            removed.append(path)

    # ----- For matching paths, diff fields -----
    for path in sorted(set(new_by_path) & set(old_by_path)):
        old_cam = old_by_path[path]
        new_cam = new_by_path[path]

        # Renames: mtx_path changed → not implemented at the wire level
        # (mtx_path is the identity key). If two different paths have the
        # same camera_id but different mtx_path, that's a true rename via
        # add+remove.
        if old_cam.mtx_path != new_cam.mtx_path:
            renamed.append((old_cam.mtx_path, new_cam.mtx_path))
            continue

        a_fields: list[str] = []
        b_fields: list[str] = []
        if old_cam.source_rtsp != new_cam.source_rtsp:
            b_fields.append("source_rtsp")
        if old_cam.detect.fps != new_cam.detect.fps:
            a_fields.append("detect.fps")
        if old_cam.detect.min_score != new_cam.detect.min_score:
            a_fields.append("detect.min_score")
        if old_cam.detect.classes != new_cam.detect.classes:
            a_fields.append("detect.classes")
        if old_cam.detect.roi != new_cam.detect.roi:
            a_fields.append("detect.roi")
        if old_cam.record != new_cam.record:
            tier_c_paths.append(path)

        if a_fields:
            tier_a[path] = a_fields
        if b_fields:
            tier_b[path] = b_fields

    return TierReport(
        added=tuple(added),
        removed=tuple(removed),
        renamed=tuple(renamed),
        tier_a_per_camera={k: tuple(v) for k, v in tier_a.items()},
        tier_b_per_camera={k: tuple(v) for k, v in tier_b.items()},
        tier_c_paths=tuple(tier_c_paths),
    )


def tier_requires_worker_reload(report: TierReport) -> bool:
    """Return True when ANY camera needs worker Tier A/B reload.

    Edge-agent uses this flag to decide whether to call
    `POST /admin/reload` on each worker.
    """
    return bool(report.tier_a_per_camera or report.tier_b_per_camera)


def cameras_to_reload(report: TierReport) -> Iterable[str]:
    """Yield the mtx_paths of cameras whose workers need a reload call."""
    yield from report.tier_a_per_camera
    yield from report.tier_b_per_camera


__all__: tuple[str, ...] = (
    "TierReport",
    "classify",
    "tier_requires_worker_reload",
    "cameras_to_reload",
)
