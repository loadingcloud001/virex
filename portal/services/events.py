# SPDX-License-Identifier: Apache-2.0
"""Service-layer helpers for the events module.

This exists so multiple endpoints (events list, cameras.py, future
dashboard) can share the ORM → schema conversion logic without each
endpoint defining its own private copy.
"""

from __future__ import annotations

import json

from models import Event
from schemas.events import EventOut


def event_to_out(e: Event) -> EventOut:
    """Convert an `Event` ORM row to the public `EventOut` schema.

    The DB column `bbox` is stored as a JSON-encoded string for SQLite
    portability + simple replication; this helper parses it into the
    typed `bbox_parsed: list[float] | None` field. Falls back to None on
    malformed JSON so the API surface stays valid even if the DB has
    legacy bad rows.
    """
    bbox_parsed: list[float] | None = None
    try:
        parsed = json.loads(e.bbox)
        if isinstance(parsed, list):
            bbox_parsed = parsed
    except (json.JSONDecodeError, TypeError):
        bbox_parsed = None

    return EventOut(
        id=e.id,
        tenant_id=e.tenant_id,
        camera_id=e.camera_id,
        event_uuid=e.event_uuid,
        class_label=e.class_label,
        score=e.score,
        bbox=e.bbox,
        bbox_parsed=bbox_parsed,
        snapshot_url=e.snapshot_url,
        clip_url=e.clip_url,
        clip_built=e.clip_built,
        event_time=e.event_time,
        created_at=e.created_at,
    )


__all__: tuple[str, ...] = ("event_to_out",)