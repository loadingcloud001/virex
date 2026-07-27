# SPDX-License-Identifier: Apache-2.0
"""edge-agent main entry point.

Three background tasks run for the life of the process:
  * `heartbeat_loop`     — POSTs node health every 30s.
  * `config_pull_loop`   — GETs `/api/edge/config` every 60s, reconciles.
  * `watcher_loop`       — inotify on `workers.yaml`; routes changes
                            through `apply_tier_report` for hot-reload.

Bootstrap behaviour: at startup, `bootstrap_local()` waits PORTAL_GRACE_SEC
seconds, and if the portal is still unreachable, applies the local
`workers.yaml` as a one-shot reconcile. Once the portal becomes
available, the pull loop takes over and overrides.

The file watcher exists so portal-less operators can `vim workers.yaml`
and have the same hot-reload semantics as the portal-driven path.

`HTTP traffic uses httpx.AsyncClient` here so we share a connection
pool and avoid spawning a new one each call.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from src.config import Settings
from src.config_pull import (
    CameraEdgeDTO,
    DetectParamsDTO,
    EdgeConfigBundle,
    config_pull_loop,
)
from src.config_watcher import make_config_watcher
from src.heartbeat import GpuSampler, heartbeat_loop
from src.reconcile import apply_tier_report, run_reconcile

logger = structlog.get_logger(__name__)

PORTAL_GRACE_SEC: int = 120


def _local_bundle(settings: Settings) -> EdgeConfigBundle | None:
    """Parse `workers.yaml` into an EdgeConfigBundle for bootstrap mode."""
    path = Path(settings.workers_yaml_path)
    if not path.exists():
        return None
    import yaml  # noqa: PLC0415

    from src._yaml import expand_env  # noqa: PLC0415

    raw = path.read_text(encoding="utf-8")
    expanded = expand_env(raw)
    data = yaml.safe_load(expanded) or {}
    cameras = [
        CameraEdgeDTO(
            mtx_path=c["mtx_path"],
            source_rtsp=c.get("source_rtsp", ""),
            tenant_id=int(c["tenant_id"]),
            camera_id=int(c["camera_id"]),
            detect=DetectParamsDTO(**c.get("detect", {})),
            record=c.get("record", True),
        )
        for c in data.get("cameras", [])
    ]
    return EdgeConfigBundle(
        node_id=settings.node_id,
        config_version=int(data.get("node_id", 0)),
        cameras=cameras,
    )


async def bootstrap_local(
    settings: Settings,
    reconcile_fn,
) -> None:
    """Wait PORTAL_GRACE_SEC, then reconcile from local if portal still down."""
    logger.info("bootstrap_local_wait", grace_sec=PORTAL_GRACE_SEC)
    await asyncio.sleep(PORTAL_GRACE_SEC)
    bundle = _local_bundle(settings)
    if bundle is None:
        logger.warning("bootstrap_local_no_yaml")
        return
    logger.warning(
        "bootstrap_local_apply",
        cameras=len(bundle.cameras),
        ts=datetime.now(UTC).isoformat(),
    )
    await reconcile_fn(bundle)


async def amain() -> int:
    settings = Settings()
    gpu = GpuSampler()

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def post_fn(
            url: str, headers: dict[str, str], body: dict[str, object]
        ) -> None:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()

        async def fetch_fn(url: str, headers: dict[str, str]) -> httpx.Response:
            return await client.get(url, headers=headers)

        # Hot-reload entry point: receives a parsed bundle and a tier
        # report, returns when the apply step is done.
        async def tier_apply(bundle: EdgeConfigBundle, _report) -> None:
            await apply_tier_report(bundle, _report)

        watcher = make_config_watcher(
            settings.workers_yaml_path,
            # Polling is reliable on Docker bind mounts; inotify events
            # for /etc/virex may not propagate from the host filesystem
            # to the container through Docker's bind mount.
            force_polling=True,
        )

        async def watcher_loop() -> None:
            """Drive the watcher until cancelled; reconcile on each event."""
            try:
                await watcher.run(handler=tier_apply)
            except asyncio.CancelledError:
                watcher.stop()
                raise

        tasks = [
            asyncio.create_task(
                heartbeat_loop(settings=settings, gpu_sampler=gpu, post_fn=post_fn)
            ),
            asyncio.create_task(
                config_pull_loop(
                    settings=settings,
                    fetch_fn=fetch_fn,
                    reconcile_fn=run_reconcile,
                ),
            ),
            asyncio.create_task(
                bootstrap_local(settings, reconcile_fn=run_reconcile)
            ),
            asyncio.create_task(watcher_loop(), name="config-watcher"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("edge_agent_shutdown")
            watcher.stop()
        finally:
            for t in tasks:
                t.cancel()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain(), debug=False)
    except KeyboardInterrupt:
        logger.info("edge_agent_keyboard_interrupt")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__: tuple[str, ...] = ("amain", "main")
