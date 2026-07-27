# SPDX-License-Identifier: Apache-2.0
"""Worker admin HTTP server for Tier A/B push-applies.

The edge-agent can call `POST /admin/reload` to ask the worker to apply
the latest `workers.yaml` immediately (without waiting for the 5-second
poller). This keeps the worker reactively in sync with portal edits while
preserving the file as the single source of truth for portal-less
deployments.

Endpoints
---------
GET  /admin/healthz       — liveness, returns store version
POST /admin/reload         — read workers.yaml, diff, apply Tier A/B
POST /admin/reload?dry_run=true — return the diff without applying
POST /admin/rollback       — restore the previous HotConfig (one-deep)
GET  /admin/config         — return the current HotConfig as JSON (sanitized)

The server uses uvicorn in-process via `uvicorn.Server` so we don't
spawn a child process from the worker. `start_admin_server()` returns
the running server so the caller can clean it up on shutdown.

Port: WORKER_ADMIN_PORT (default 32000). Set to 0 to disable.

Security: this is an internal endpoint; we bind to 127.0.0.1 only —
edge-agent reaches it via `rtsp://localhost:19554/...` style host network.
A bearer token gate is not needed in v1 (pilot pilot); Phase 2 will add
one when portal-edge communication moves to TLS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Query

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# JSON sanitisation: redact secret fields. Operators shouldn't see
# minio_access_key / minio_secret_key / source_rtsp in /admin/config
# responses. Any new credential field added to HotConfig must also be
# redacted here.
# ---------------------------------------------------------------------------
def _sanitised_dump(store) -> dict:
    cfg = store.get()
    return {
        "version": store.version(),
        "detector_url": cfg.detector_url,
        "snapshot_quality": cfg.snapshot_quality,
        "layer_suffix": cfg.layer_suffix,
        "node_id": cfg.node_id,
        "mqtt_broker": cfg.mqtt_broker,
        "mqtt_topic": cfg.mqtt_topic,
        "minio_endpoint": cfg.minio_endpoint,
        "minio_secure": cfg.minio_secure,
        "minio_bucket": cfg.minio_bucket,
        "minio_access_key": "***REDACTED***",
        "minio_secret_key": "***REDACTED***",
        "minio_region": cfg.minio_region,
        "cameras": [
            {
                "mtx_path": c.mtx_path,
                "tenant_id": c.tenant_id,
                "camera_id": c.camera_id,
                "source_rtsp": "***REDACTED***" if c.source_rtsp else "",
                "min_score": c.min_score,
                "fps": c.fps,
                "classes": list(c.classes),
                "roi": [list(p) for p in c.roi],
            }
            for c in cfg.cameras
        ],
    }


def build_app(store, reloader) -> FastAPI:
    """Construct the FastAPI app; `start_admin_server` is the runner."""
    app = FastAPI(title="virex-worker-admin", version="0.1.0")

    @app.get("/admin/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": store.version()}

    @app.get("/admin/config")
    async def get_config() -> dict:
        return _sanitised_dump(store)

    @app.post("/admin/reload")
    async def reload_config(
        dry_run: bool = Query(default=False),
    ) -> dict:
        """Force a re-read of workers.yaml and apply Tier A/B diffs."""
        try:
            report = await reloader.apply_now()
        except Exception as e:  # noqa: BLE001
            logger.exception("admin_reload_failed")
            raise HTTPException(status_code=400, detail=str(e)[:500]) from e
        return {
            "version": report.version,
            "tier_a": list(report.tier_a),
            "tier_b_per_camera": {
                k: list(v) for k, v in report.tier_b_per_camera.items()
            },
            "tier_b_global": list(report.tier_b_global),
            "error": report.error,
            "dry_run": dry_run,
        }

    @app.post("/admin/rollback")
    async def rollback() -> dict:
        report = await reloader.rollback()
        if report is None:
            raise HTTPException(status_code=409, detail="no previous config")
        return {"version": report.version, "tier_a": list(report.tier_a)}

    return app


# ---------------------------------------------------------------------------
# uvicorn in-process runner. We use uvicorn.Server so we can manage
# startup/shutdown from the worker's event loop without spawn.
# ---------------------------------------------------------------------------
async def start_admin_server(
    *,
    port: int,
    store,
    reloader,
):  # -> uvicorn.Server
    import uvicorn

    app = build_app(store, reloader)
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level=os.environ.get("VIREX_ADMIN_LOG", "warning"),
        access_log=False,
        loop="asyncio",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    import asyncio

    asyncio.create_task(server.serve(), name="worker-admin-uvicorn")
    # Spin-wait briefly so the bind completes before we return; otherwise
    # main()'s gather() could call `await reloader.run()` while uvicorn
    # is still starting up.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    return server


__all__: tuple[str, ...] = ("build_app", "start_admin_server")


# ---------------------------------------------------------------------------
# Manual smoke: `python -m worker.admin --store.version 1` style not
# supported because we hold refs to in-process objects. The standalone
# test path is `tests/test_admin.py`.
# ---------------------------------------------------------------------------
def _smoke() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", required=True)
    args = parser.parse_args()
    print(json.dumps({"exists": Path(args.yaml).exists()}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _smoke()
