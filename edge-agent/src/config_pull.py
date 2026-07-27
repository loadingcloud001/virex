# SPDX-License-Identifier: Apache-2.0
"""Portal ↔ edge config-pull loop.

Every `config_pull_period_sec` we GET `/api/edge/config?since=<local_vsn>`.
If the returned `config_version` differs from what we last applied, we
ask `reconcile.run_reconcile()` to bring the local docker-compose
worker pool and the MediaMTX paths fragment in line with the new bundle.

Authentication: JWT bearer token obtained from `src.auth`. If the JWT
is rejected with 401 + `X-Token-Expired: 1`, we auto-rotate via
`/api/edge/nodes/{id}/rotate`. If the bootstrap secret is also bad
(unrecoverable), we re-register.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from pydantic import BaseModel

from src.auth import (
    load_or_register_jwt,
    refresh_jwt,
)
from src.config import Settings

logger = structlog.get_logger(__name__)


class DetectParamsDTO(BaseModel):
    fps: int = 5
    classes: list[str] = []
    min_score: float = 0.5
    roi: list[list[float]] = []


class CameraEdgeDTO(BaseModel):
    mtx_path: str
    source_rtsp: str
    tenant_id: int
    camera_id: int
    detect: DetectParamsDTO = DetectParamsDTO()
    record: bool = True


class EdgeConfigBundle(BaseModel):
    node_id: int
    config_version: int
    cameras: list[CameraEdgeDTO]


Reconciler = Callable[[EdgeConfigBundle], Awaitable[None]]


async def _fetch_config(
    *,
    fetch_fn,
    url: str,
    token: str,
) -> tuple[int, object]:
    """Call fetch_fn and return (status_code, parsed_body_or_text)."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = await fetch_fn(url, headers)
    return resp.status_code, resp


def _obtain_token(
    *,
    settings: Settings,
    state_dir: Path,
) -> str:
    """Get a fresh JWT — register if absent, else use the cached one.

    Sync (called via asyncio.to_thread). The portal call is a blocking
    HTTP request so we don't want it on the event loop.
    """
    return load_or_register_jwt(
        portal_url=settings.portal_url,
        bootstrap_secret=settings.portal_bootstrap_secret,
        state_dir=state_dir,
        hostname=settings.node_hostname or "",
        tailscale_ip="127.0.0.1",  # set by main.py if a Tailscale interface exists
        gpu_model=None,
        max_cameras=settings.node_max_cameras,
    )


async def config_pull_loop(
    *,
    settings: Settings,
    fetch_fn,  # type: ignore[no-untyped-def]
    reconcile_fn: Reconciler,
    current_version: int = 0,
) -> None:
    """Pull loop. Updates local config version on successful reconciles."""
    period = settings.config_pull_period_sec
    last_seen: int = current_version
    state_dir = Path(settings.state_dir)
    token: str = ""

    # Initial registration / load.
    try:
        token = await asyncio.to_thread(
            _obtain_token, settings=settings, state_dir=state_dir
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "config_pull_jwt_bootstrap_failed", error=str(e)
        )
        await asyncio.sleep(period)
        return  # outer `while True` will retry on next call

    while True:
        try:
            url = (
                f"{settings.portal_url.rstrip('/')}/api/edge/config"
                f"?node_id={settings.node_id}&since={last_seen}"
            )
            status_code, resp = await _fetch_config(
                fetch_fn=fetch_fn, url=url, token=token
            )

            if status_code == 401:
                # Try to rotate.
                expired = resp.headers.get("x-token-expired") == "1"
                try:
                    token = await asyncio.to_thread(
                        refresh_jwt,
                        portal_url=settings.portal_url,
                        current_token=token,
                        state_dir=state_dir,
                        node_id=settings.node_id,
                    )
                    logger.info(
                        "config_pull_token_rotated",
                        expired=expired,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "config_pull_rotate_failed",
                        error=str(e),
                    )
                    # Fall back to full re-register.
                    token = await asyncio.to_thread(
                        _obtain_token,
                        settings=settings,
                        state_dir=state_dir,
                    )
                await asyncio.sleep(period)
                continue

            if status_code == 404:
                logger.warning(
                    "config_pull_node_unknown", node_id=settings.node_id
                )
                await asyncio.sleep(period)
                continue

            resp.raise_for_status()
            bundle = EdgeConfigBundle.model_validate(resp.json())

            if bundle.config_version == last_seen:
                logger.debug("config_pull_unchanged", version=last_seen)
            else:
                logger.info(
                    "config_pull_new",
                    version=bundle.config_version,
                    camera_count=len(bundle.cameras),
                )
                await reconcile_fn(bundle)
                last_seen = bundle.config_version
        except Exception as e:  # noqa: BLE001
            logger.warning("config_pull_failed", error=str(e))
        await asyncio.sleep(period)


__all__: tuple[str, ...] = ("config_pull_loop", "EdgeConfigBundle", "CameraEdgeDTO")