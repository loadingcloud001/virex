# SPDX-License-Identifier: Apache-2.0
"""Portal ↔ edge config-pull loop.

Every `config_pull_period_sec` we GET `/api/edge/config?since=<local_vsn>`.
If the returned `config_version` differs from what we last applied, we
ask `reconcile.run_reconcile()` to bring the local docker-compose
worker pool and the MediaMTX paths fragment in line with the new bundle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from pydantic import BaseModel

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
    while True:
        try:
            url = (
                f"{settings.portal_url.rstrip('/')}/api/edge/config"
                f"?node_id={settings.node_id}&since={last_seen}"
            )
            headers = {"Authorization": f"Bearer {settings.portal_bearer}"}
            resp = await fetch_fn(url, headers)
            if resp.status_code == 404:
                logger.warning("config_pull_node_unknown", node_id=settings.node_id)
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
