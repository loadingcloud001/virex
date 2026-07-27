# SPDX-License-Identifier: Apache-2.0
"""Redis-backed cooldown deduplication.

Stateless per-frame emit by workers means rapid duplicate detections
(e.g. one person standing in frame at 5 fps = 5 msgs/s). The router
collapses these into a single alert event per `(tenant, camera, class)`
within a fixed cooldown window.

The cooldown key is `cd:<tenant>:<camera>:<class>`; redis SET ... NX EX 30
returns True (acquired) only on the first call within the window.
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

COOLDOWN_WINDOW_SEC: int = 30


def cooldown_key(tenant_id: int, camera_id: int, label: str) -> str:
    """Build the canonical Redis key for canopy dedup."""
    return f"cd:{tenant_id}:{camera_id}:{label}"


async def acquire_cooldown(
    redis: Redis,
    *,
    tenant_id: int,
    camera_id: int,
    label: str,
    window_sec: int = COOLDOWN_WINDOW_SEC,
) -> bool:
    """Atomically claim the cooldown slot.

    Returns:
        True if we are the first message in this window (caller should
        fire the alert). False means a sibling message within the window
        already fired; caller MUST drop.
    """
    result = await redis.set(
        cooldown_key(tenant_id, camera_id, label),
        "1",
        ex=window_sec,
        nx=True,
    )
    acquired = bool(result)
    logger.debug(
        "cooldown_check",
        tenant_id=tenant_id,
        camera_id=camera_id,
        label=label,
        acquired=acquired,
    )
    return acquired


async def release_cooldown(
    redis: Redis,
    *,
    tenant_id: int,
    camera_id: int,
    label: str,
) -> None:
    """Force-release the cooldown slot (for tests or operator replay)."""
    await redis.delete(cooldown_key(tenant_id, camera_id, label))


__all__: tuple[str, ...] = (
    "COOLDOWN_WINDOW_SEC",
    "acquire_cooldown",
    "release_cooldown",
    "cooldown_key",
)
