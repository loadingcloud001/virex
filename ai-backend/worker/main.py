# SPDX-License-Identifier: Apache-2.0
"""CLI entry point for the per-camera worker.

Usage:
    python -m worker.main --config /etc/virex/workers.yaml

If the `CAMERAS_BUNDLE` env var is set (set by the rendered worker-
compose service from edge-agent), the worker filters down to the single
camera in that bundle — preventing one container from reading every
camera in the YAML file.

Hot-reload pipeline
-------------------
On startup we:
  1. read workers.yaml → `WorkerGlobalConfig` (for env-var resolved strings),
  2. construct an immutable `HotConfig` and wrap it in `HotConfigStore`,
  3. filter cameras to the bundle (if any),
  4. spawn one `CameraLoop` per (filtered) camera,
  5. spawn `ConfigReloader` which polls workers.yaml every 5 s and
     applies Tier A/B diffs (atomic HotConfig swap + reconnect sentinel
     to the affected camera's queue),
  6. optionally spawn the admin FastAPI server (`worker/admin.py`) on
     `WORKER_ADMIN_PORT` (default 32000) so edge-agent can `POST
     /admin/reload` for instant push instead of waiting for the poll.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import structlog

from worker.admin import start_admin_server
from worker.camera_worker import run_all
from worker.config import load_config
from worker.config_hot import HotConfig, HotConfigStore
from worker.config_reloader import Reloader

logger = structlog.get_logger(__name__)


def _filter_to_bundle_camera(cfg, bundle_json: str):
    """Return a copy of cfg with only the camera in CAMERAS_BUNDLE."""
    try:
        bundle = json.loads(bundle_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"CAMERAS_BUNDLE is not valid JSON: {e}") from e

    mtx = bundle.get("mtx_path", "")
    keep = [c for c in cfg.cameras if c.mtx_path == mtx]
    if not keep:
        from worker.config import CameraConfig, DetectConfig

        try:
            detect = DetectConfig(**bundle.get("detect", {}))
            keep = [
                CameraConfig(
                    mtx_path=bundle["mtx_path"],
                    source_rtsp=bundle.get("source_rtsp", ""),
                    tenant_id=int(bundle.get("tenant_id", 1)),
                    camera_id=int(bundle.get("camera_id", 1)),
                    record=bundle.get("record", True),
                    detect=detect,
                )
            ]
        except (KeyError, ValueError) as e:
            raise SystemExit(f"CAMERAS_BUNDLE missing fields: {e}") from e
    cfg.cameras = keep
    return cfg


def _filtered_hot_config(
    full_hot: HotConfig, bundle_json: str | None
) -> HotConfig:
    """If CAMERAS_BUNDLE is set, narrow `cameras` to that one entry."""
    if not bundle_json:
        return full_hot

    try:
        bundle = json.loads(bundle_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"CAMERAS_BUNDLE is not valid JSON: {e}") from e

    target = bundle.get("mtx_path", "")
    filtered = [c for c in full_hot.cameras if c.mtx_path == target]
    return HotConfig(
        detector_url=full_hot.detector_url,
        detector_kind=full_hot.detector_kind,
        snapshot_quality=full_hot.snapshot_quality,
        layer_suffix=full_hot.layer_suffix,
        node_id=full_hot.node_id,
        mqtt_broker=full_hot.mqtt_broker,
        mqtt_topic=full_hot.mqtt_topic,
        mqtt_client_id=full_hot.mqtt_client_id,
        minio_endpoint=full_hot.minio_endpoint,
        minio_secure=full_hot.minio_secure,
        minio_bucket=full_hot.minio_bucket,
        minio_access_key=full_hot.minio_access_key,
        minio_secret_key=full_hot.minio_secret_key,
        minio_region=full_hot.minio_region,
        cameras=tuple(filtered),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="virex-worker",
        description="Per-camera RTSP ingestion → detector → MQTT/MinIO.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to workers.yaml (rendered by edge-agent from portal DB).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Config reload poll interval in seconds (default 5).",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=int(os.environ.get("WORKER_ADMIN_PORT", "32000")),
        help="Port for the admin HTTP server (default 32000; 0 = disabled).",
    )
    args = parser.parse_args(argv)

    # First read uses the legacy `WorkerGlobalConfig` so we get env-var
    # expansion of `${...}` placeholders in the YAML (the `HotConfig`
    # loader shares this code path via `_expand_env`). Then we build an
    # immutable `HotConfig` from the parsed data.
    legacy = load_config(args.config)
    bundle = os.environ.get("CAMERAS_BUNDLE")
    if bundle:
        legacy = _filter_to_bundle_camera(legacy, bundle)

    # Build the immutable HotConfig from the parsed schema. We use
    # HotConfig.from_yaml (which goes via GlobalSchema.from_schema) so
    # every required HotConfig field (including detector_kind) is
    # populated. Direct dataclass construction would silently skip
    # detector_kind and crash on next rebuild. If CAMERAS_BUNDLE is
    # set, narrow the camera list to the single bundled camera while
    # preserving all global fields (incl. detector_kind).
    full_hot = HotConfig.from_yaml(args.config)
    # `legacy` is only used to surface env-var validation errors early;
    # from_yaml re-runs expand_env internally (see worker._yaml.expand_env)
    # so the HotConfig is authoritative.
    _ = legacy
    hot = _filtered_hot_config(full_hot, bundle)
    store = HotConfigStore(hot)

    logger.info(
        "worker_config_loaded",
        cameras=len(hot.cameras),
        mtx_paths=[c.mtx_path for c in hot.cameras],
        detector=hot.detector_url,
        mqtt=hot.mqtt_broker,
        minio=hot.minio_endpoint,
    )

    if not hot.cameras:
        logger.error("worker_no_cameras", bundle=bool(bundle))
        return 1

    reloader = Reloader(
        yaml_path=args.config,
        store=store,
        camera_loops={},
        poll_interval_sec=args.poll_interval,
    )

    try:
        asyncio.run(
            _orchestrate(
                args=args,
                store=store,
                reloader=reloader,
                bundle=bool(bundle),
            ),
            debug=False,
        )
    except KeyboardInterrupt:
        logger.info("worker_shutdown_keyboard")
        return 0
    return 0


async def _orchestrate(
    *,
    args: argparse.Namespace,
    store: HotConfigStore,
    reloader: Reloader,
    bundle: bool,
) -> None:
    """Boot camera loops, the reloader, and (optionally) admin HTTP server.

    Runs until cancelled or unrecoverable error. Camera loops are owned by
    `run_all(store)`; the reloader task does the Tier A/B apply.
    """
    # Camera loops write to reloader.camera_loops via `attach()`, so we
    # need to share the reloader instance. We start both concurrently
    # and let the camera loops register themselves in `__post_init__`…
    # but the simpler solution is: start reloader first (registering
    # callbacks), then start camera loops and have run_all do the
    # attach. Both run as gather'd tasks; on cancel they're all killed.

    # Start the admin server (non-blocking). It calls store.set and
    # reloader.apply_now() directly; safe to start before the loops.
    admin_runner = None
    if args.admin_port > 0:
        admin_runner = await start_admin_server(
            port=args.admin_port,
            store=store,
            reloader=reloader,
        )
        logger.info("admin_server_started", port=args.admin_port)

    camera_task = asyncio.create_task(run_all(store, reloader=reloader), name="camera-loops")
    reloader_task = asyncio.create_task(reloader.run(), name="config-reloader")

    try:
        await asyncio.gather(camera_task, reloader_task)
    finally:
        reloader.stop()
        for t in (camera_task, reloader_task):
            t.cancel()
        if admin_runner is not None:
            admin_runner.cleanup()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
