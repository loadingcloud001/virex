# SPDX-License-Identifier: Apache-2.0
"""Security primitives for the portal.

Two authentication mechanisms live side-by-side:

1. **Session cookies** (UI) — signed JWT in an HttpOnly cookie named
   `virex_session`. Stateless, no Redis dependency, 7-day TTL. The
   cookie's signature prevents tampering; the JWT payload (sub=user_id,
   tid=tenant_id, role, exp) carries all the auth context.

2. **Bearer tokens** (service APIs) — long-lived JWTs (30-day TTL)
   issued via `POST /api/edge/nodes/register` after a successful
   `edge_bootstrap_secret` handshake. edge-agent, clip-builder, and
   event-router persist these to disk (`state/edge.jwt`) and present
   them as `Authorization: Bearer <jwt>`.

Password hashing uses `passlib[bcrypt]` (already a dependency). JWT
signing uses `pyjwt` (already a dependency). Both are imported lazily
inside the functions so the module is cheap to import for code paths
that don't need crypto (e.g. the `core.database` module).
"""

from __future__ import annotations

import time
from typing import Any

import bcrypt
import jwt

# We use the `bcrypt` package directly (NOT passlib). passlib 1.7.x is
# pinned to a bcrypt API that newer bcrypt 4.x broke; the direct API is
# simpler and avoids the dependency surface.
# Cost factor 12 matches the passlib default (~250 ms per hash on a
# modern x86 core — acceptable for an interactive login).
_BCRYPT_ROUNDS: int = 12


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Return a bcrypt hash of `plain`. Never store the plaintext."""
    pw = plain.encode("utf-8")[:72]  # bcrypt hard limit
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(pw, salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt verify. Returns False on any mismatch."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT helpers — shared by session cookies and edge bearer tokens
# ---------------------------------------------------------------------------
JWT_ALGO = "HS256"


def _now() -> int:
    return int(time.time())


def create_jwt(
    *,
    secret: str,
    subject: str,
    claims: dict[str, Any],
    ttl_sec: int,
) -> str:
    """Sign a JWT with `subject` (sub claim) and additional `claims`.

    Standard claims added: `iat`, `exp`. Caller supplies domain-specific
    claims like `tid` (tenant_id), `role`, `node_id`.
    """
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": _now(),
        "exp": _now() + ttl_sec,
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGO)


def decode_jwt(token: str, *, secret: str) -> dict[str, Any]:
    """Verify signature + exp, return claims. Raises on any failure.

    Use `jwt.PyJWTError` (or its subclasses) to catch — the caller
    decides whether the exception maps to 401 or to "token expired,
    please rotate".
    """
    return jwt.decode(token, secret, algorithms=[JWT_ALGO])


# ---------------------------------------------------------------------------
# UI session cookies (signed JWT, stateless)
# ---------------------------------------------------------------------------
UI_SESSION_TTL_SEC = 7 * 24 * 3600  # 7 days
UI_SESSION_COOKIE = "virex_session"


def create_session_token(
    *,
    session_secret: str,
    user_id: int,
    tenant_id: int,
    role: str,
) -> str:
    return create_jwt(
        secret=session_secret,
        subject=str(user_id),
        claims={"tid": tenant_id, "role": role, "kind": "ui_session"},
        ttl_sec=UI_SESSION_TTL_SEC,
    )


# ---------------------------------------------------------------------------
# Edge bearer tokens (long-lived JWT)
# ---------------------------------------------------------------------------
def create_edge_token(
    *,
    jwt_secret: str,
    node_id: int,
    tenant_id: int,
    hostname: str,
    ttl_sec: int,
) -> str:
    return create_jwt(
        secret=jwt_secret,
        subject=str(node_id),
        claims={
            "tid": tenant_id,
            "host": hostname,
            "kind": "edge_token",
        },
        ttl_sec=ttl_sec,
    )


__all__: tuple[str, ...] = (
    "hash_password",
    "verify_password",
    "create_jwt",
    "decode_jwt",
    "create_session_token",
    "create_edge_token",
    "UI_SESSION_TTL_SEC",
    "UI_SESSION_COOKIE",
    "JWT_ALGO",
)