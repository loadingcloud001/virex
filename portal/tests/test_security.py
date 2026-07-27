# SPDX-License-Identifier: Apache-2.0
"""Tests for the security helpers (password hashing + JWT)."""

from __future__ import annotations

import time

import jwt
import pytest
from jwt import ExpiredSignatureError, InvalidSignatureError

from core.security import (
    JWT_ALGO,
    UI_SESSION_COOKIE,
    UI_SESSION_TTL_SEC,
    create_edge_token,
    create_jwt,
    create_session_token,
    decode_jwt,
    hash_password,
    verify_password,
)


SECRET = "test-secret-for-unit-tests"


def test_hash_password_produces_non_plaintext() -> None:
    h = hash_password("hunter2")
    assert h != "hunter2"
    assert h.startswith("$2")  # bcrypt


def test_verify_password_roundtrip() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True
    assert verify_password("wrong", h) is False


def test_verify_password_handles_garbage() -> None:
    # Garbage hashes must return False, never raise.
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


def test_create_and_decode_jwt_roundtrip() -> None:
    token = create_jwt(
        secret=SECRET,
        subject="42",
        claims={"tid": 7, "role": "admin"},
        ttl_sec=60,
    )
    claims = decode_jwt(token, secret=SECRET)
    assert claims["sub"] == "42"
    assert claims["tid"] == 7
    assert claims["role"] == "admin"
    assert claims["exp"] > claims["iat"]


def test_decode_jwt_rejects_wrong_secret() -> None:
    token = create_jwt(secret=SECRET, subject="x", claims={}, ttl_sec=60)
    with pytest.raises(InvalidSignatureError):
        decode_jwt(token, secret="wrong-secret")


def test_decode_jwt_rejects_expired() -> None:
    token = create_jwt(secret=SECRET, subject="x", claims={}, ttl_sec=-1)
    with pytest.raises(ExpiredSignatureError):
        decode_jwt(token, secret=SECRET)


def test_create_session_token_kinds_are_distinct() -> None:
    """Edge tokens and UI session tokens must be distinguishable.

    `decode_jwt` doesn't enforce kinds; the auth deps do. We just verify
    the kinds are spelled out so a misuse (edge token at session
    endpoint or vice versa) is caught by the dependency.
    """
    sess = create_session_token(
        session_secret=SECRET, user_id=1, tenant_id=7, role="admin"
    )
    edge = create_edge_token(
        jwt_secret=SECRET, node_id=42, tenant_id=7, hostname="node-x", ttl_sec=60
    )
    assert decode_jwt(sess, secret=SECRET)["kind"] == "ui_session"
    assert decode_jwt(edge, secret=SECRET)["kind"] == "edge_token"


def test_ui_session_cookie_name_is_stable() -> None:
    """UI cookie name is a contract — don't change it without a migration."""
    assert UI_SESSION_COOKIE == "virex_session"
    assert UI_SESSION_TTL_SEC > 0


def test_jwt_algo_is_hs256() -> None:
    """Pin the algorithm to prevent algorithm-confusion attacks."""
    assert JWT_ALGO == "HS256"


def test_session_token_iat_exp_window() -> None:
    """Sanity-check that iat/exp are within a sane window."""
    before = int(time.time())
    token = create_session_token(
        session_secret=SECRET, user_id=1, tenant_id=1, role="admin"
    )
    claims = decode_jwt(token, secret=SECRET)
    after = int(time.time())
    assert before <= claims["iat"] <= after
    assert claims["exp"] - claims["iat"] == UI_SESSION_TTL_SEC