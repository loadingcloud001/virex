# SPDX-License-Identifier: Apache-2.0
"""Match a detection event against a tenant's alert rules.

For v1 (Phase-1 MVP, person detection only) matching is intentionally
simple: a rule matches if it is `enabled` and either:
  * `class_filter` is NULL/empty (wildcard), OR
  * `class_filter` equals the detected `label`.

Future extensions (zones / ROI, score bands, time schedules) live in
the `zones` JSONB column per ARCHITECTURE.md §3.3.2 but are deferred
to Phase 2 (per the implementation plan).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from src.db import AlertRule

logger = structlog.get_logger(__name__)


def match_rules(
    rules: list[AlertRule],
    *,
    label: str,
    score: float,
) -> list[dict[str, Any]]:
    """Return a list of matched rule descriptors (rule_id, channels)."""
    out: list[dict[str, Any]] = []
    for rule in rules:
        if rule.class_filter and rule.class_filter != label:
            continue
        # Default Phase-2 shape: `["telegram","email"]` JSON-encoded list.
        channels: list[str]
        try:
            channels = json.loads(rule.notification_channels or "[]")
            if not isinstance(channels, list):
                channels = []
        except (json.JSONDecodeError, TypeError):
            channels = []
        out.append(
            {
                "rule_id": rule.id,
                "channels": channels,
                "label": label,
                "score": score,
            }
        )
    logger.debug(
        "rule_match_done",
        label=label,
        score=score,
        rule_count=len(rules),
        matched=len(out),
    )
    return out


__all__: tuple[str, ...] = ("match_rules",)
