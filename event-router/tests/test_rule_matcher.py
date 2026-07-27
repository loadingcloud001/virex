# SPDX-License-Identifier: Apache-2.0
"""Rule matcher tests using plain Python objects (no DB required)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.rule_matcher import match_rules


@dataclass
class _RuleStub:
    """Minimal stand-in for `AlertRule` SQLAlchemy row."""

    id: int
    enabled: bool
    class_filter: str | None
    notification_channels: str  # JSON-encoded list[str]


def test_match_wildcard_rule() -> None:
    rule = _RuleStub(id=1, enabled=True, class_filter=None, notification_channels='["telegram"]')
    out = match_rules([rule], label="person", score=0.9)
    assert len(out) == 1
    assert out[0] == {
        "rule_id": 1,
        "channels": ["telegram"],
        "label": "person",
        "score": 0.9,
    }


def test_match_explicit_class() -> None:
    rule = _RuleStub(id=2, enabled=True, class_filter="person", notification_channels="[]")
    out = match_rules([rule], label="person", score=0.5)
    assert len(out) == 1


def test_no_match_on_class_mismatch() -> None:
    rule = _RuleStub(id=3, enabled=True, class_filter="car", notification_channels="[]")
    out = match_rules([rule], label="person", score=0.5)
    assert out == []


def test_invalid_json_channels_falls_back_to_empty() -> None:
    rule = _RuleStub(id=4, enabled=True, class_filter=None, notification_channels="not json")
    out = match_rules([rule], label="person", score=0.5)
    assert out and out[0]["channels"] == []


def test_invalid_json_channels_object_treated_as_empty() -> None:
    rule = _RuleStub(
        id=5, enabled=True, class_filter=None, notification_channels=json.dumps({"a": 1})
    )
    out = match_rules([rule], label="person", score=0.5)
    assert out and out[0]["channels"] == []


def test_multiple_rules_wildcard_match() -> None:
    rules = [
        _RuleStub(id=1, enabled=True, class_filter=None, notification_channels='["telegram"]'),
        _RuleStub(id=2, enabled=True, class_filter=None, notification_channels='["email"]'),
        _RuleStub(id=3, enabled=True, class_filter="car", notification_channels="[]"),
    ]
    out = match_rules(rules, label="person", score=0.5)
    assert {r["rule_id"] for r in out} == {1, 2}
