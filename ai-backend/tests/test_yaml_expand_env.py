# SPDX-License-Identifier: AGPL-3.0
"""Tests for the ${VAR} and ${VAR:-default} env-substitution helper."""

from __future__ import annotations

import os

from worker._yaml import expand_env


def test_expand_simple_var(monkeypatch: object) -> None:
    monkeypatch.setenv("MY_VAR", "hello")
    assert expand_env("minio_access_key: \"${MY_VAR}\"") == 'minio_access_key: "hello"'


def test_expand_missing_var_left_literal(monkeypatch: object) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    # Missing var should surface as literal ${MISSING_VAR} for the
    # pydantic validator to flag.
    assert expand_env("${MISSING_VAR}") == "${MISSING_VAR}"


def test_expand_with_default(monkeypatch: object) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    assert expand_env("${MISSING_VAR:-fallback}") == "fallback"


def test_expand_with_default_overridden(monkeypatch: object) -> None:
    monkeypatch.setenv("MY_VAR", "real-value")
    assert expand_env("${MY_VAR:-fallback}") == "real-value"


def test_expand_empty_default(monkeypatch: object) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    assert expand_env("${MISSING_VAR:-}") == ""


def test_expand_multiple_in_one_string(monkeypatch: object) -> None:
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    monkeypatch.delenv("C", raising=False)
    assert (
        expand_env("a=${A} b=${B} c=${C:-3} d=${C}")
        == "a=1 b=2 c=3 d=${C}"
    )


def test_expand_no_dollar_signs() -> None:
    assert expand_env("plain text") == "plain text"


def test_expand_unclosed_brace_left_literal() -> None:
    # An unclosed `${VAR` should not crash; leave literal.
    assert expand_env("unclosed: ${UNCLOSED") == "unclosed: ${UNCLOSED"