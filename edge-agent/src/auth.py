# SPDX-License-Identifier: Apache-2.0
"""Edge-agent JWT lifecycle.

edge-agent boots with no JWT. On first start (or if the cached JWT has
expired), it POSTs to `${portal_url}/api/edge/nodes/register` with
the shared bootstrap secret (`VIREX_EDGE_BOOTSTRAP_SECRET` in env)
and its `hostname` / `tailscale_ip`. The portal responds with a
long-lived JWT (30-day TTL) that edge-agent persists to disk and
uses for every subsequent `/api/edge/config` and `/api/edge/heartbeat`
call.

If the JWT is rejected (401 with `X-Token-Expired: 1` header), we
auto-rotate via `/api/edge/nodes/{id}/rotate` using the current token
as the bearer. If that's also rejected, fall back to a full re-register.

The persisted token file is `state/edge.jwt` (raw text). `state/` is
bind-mounted into the edge-agent container, so clip-builder and other
co-located containers can read it directly.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


# Cache hit for the current process; avoids re-reading the JWT file
# on every API call.
_TOKEN_CACHE: dict[str, str] = {}


def _jwt_path(state_dir: Path) -> Path:
    return state_dir / "edge.jwt"


def _read_cached_token(state_dir: Path) -> str | None:
    """Return the cached JWT, loading from disk on first call."""
    if "token" in _TOKEN_CACHE:
        return _TOKEN_CACHE["token"]
    p = _jwt_path(state_dir)
    if not p.is_file():
        return None
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("auth_token_read_failed", path=str(p), error=str(e))
        return None
    if not token:
        return None
    _TOKEN_CACHE["token"] = token
    return token


def _persist_token(state_dir: Path, token: str) -> None:
    p = _jwt_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    # Group-readable so clip-builder (same docker compose project) can read.
    try:
        os.chmod(p, 0o640)
    except OSError:
        pass
    _TOKEN_CACHE["token"] = token


def _clear_token(state_dir: Path) -> None:
    p = _jwt_path(state_dir)
    if p.exists():
        p.unlink()
    _TOKEN_CACHE.clear()


def _register(portal_url: str, bootstrap_secret: str, body: dict[str, Any]) -> str:
    """POST /api/edge/nodes/register and return the JWT."""
    url = f"{portal_url.rstrip('/')}/api/edge/nodes/register"
    headers = {"Authorization": f"Bearer {bootstrap_secret}"}
    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        body_out = r.json()
        return body_out["jwt_token"]


def _rotate(portal_url: str, current_token: str, node_id: int) -> str:
    """POST /api/edge/nodes/{node_id}/rotate and return the new JWT."""
    url = f"{portal_url.rstrip('/')}/api/edge/nodes/{node_id}/rotate"
    headers = {"Authorization": f"Bearer {current_token}"}
    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, headers=headers)
        r.raise_for_status()
        body_out = r.json()
        return body_out["jwt_token"]


def load_or_register_jwt(
    *,
    portal_url: str,
    bootstrap_secret: str,
    state_dir: Path,
    hostname: str,
    tailscale_ip: str,
    gpu_model: str | None,
    max_cameras: int,
) -> str:
    """Return a fresh JWT, registering if no cached token exists.

    Idempotent: re-runs return the cached token without contacting
    the portal unless the cache is missing.
    """
    cached = _read_cached_token(state_dir)
    if cached:
        return cached

    body = {
        "hostname": hostname,
        "tailscale_ip": tailscale_ip,
        "gpu_model": gpu_model,
        "max_cameras": max_cameras,
    }
    token = _register(portal_url, bootstrap_secret, body)
    _persist_token(state_dir, token)
    logger.info("edge_jwt_registered", hostname=hostname)
    return token


def refresh_jwt(
    *,
    portal_url: str,
    current_token: str,
    state_dir: Path,
    node_id: int,
) -> str:
    """Force a JWT rotation (used after a 401 with X-Token-Expired)."""
    new_token = _rotate(portal_url, current_token, node_id)
    _persist_token(state_dir, new_token)
    logger.info("edge_jwt_rotated", node_id=node_id)
    return new_token


def reset_cache() -> None:
    """Drop the in-process token cache (used by tests)."""
    _TOKEN_CACHE.clear()


def token_for_host(
    *,
    portal_url: str,
    bootstrap_secret: str,
    state_dir: Path,
    hostname: str | None = None,
    tailscale_ip: str | None = None,
    gpu_model: str | None = None,
    max_cameras: int = 50,
) -> str:
    """Convenience wrapper: register if missing, return current token.

    `hostname` / `tailscale_ip` are auto-detected when not provided
    (via socket.gethostname() and a UDP-connect trick respectively).
    """
    if hostname is None:
        hostname = socket.gethostname()
    if tailscale_ip is None:
        tailscale_ip = _detect_outbound_ip()
    return load_or_register_jwt(
        portal_url=portal_url,
        bootstrap_secret=bootstrap_secret,
        state_dir=state_dir,
        hostname=hostname,
        tailscale_ip=tailscale_ip,
        gpu_model=gpu_model,
        max_cameras=max_cameras,
    )


def _detect_outbound_ip() -> str:
    """Best-effort outbound IP detection (no DNS lookup required)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


__all__: tuple[str, ...] = (
    "load_or_register_jwt",
    "refresh_jwt",
    "token_for_host",
    "reset_cache",
    "_clear_token",  # exported for tests
)