# SPDX-License-Identifier: Apache-2.0
"""clip-builder service entry point.

Subscribes to `virex/events_created` (emitted by `event-router`),
builds a 10 s clip from the MediaMTX fMP4 recording covering the event
timestamp, uploads to MinIO, and PATCHes the portal event row with the
`clip_url`.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from minio import Minio
from minio.error import S3Error
from paho.mqtt.client import CallbackAPIVersion, Client, MQTTMessage
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from clip_builder.ffmpeg_runner import build_clip

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MQTT_TOPIC_VIREX_EVENTS_CREATED: str = "virex/events_created"
HTTP_PATCH_TIMEOUT_SEC: float = 15.0
CLIP_OBJECT_CONTENT_TYPE: str = "video/mp4"


@dataclass(slots=True)
class ClipBuilderConfig:
    recording_dir: Path
    mqtt_broker: str
    mqtt_client_id: str
    mqtt_topic: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    portal_internal_url: str
    portal_jwt_path: str
    # Optional fields with defaults (must come last).
    minio_region: str = ""


def load_env_config() -> ClipBuilderConfig:
    """Load all configuration from environment variables."""
    required = [
        "RECORDING_DIR",
        "MQTT_BROKER",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_BUCKET",
        "MINIO_REGION",
        "PORTAL_INTERNAL_URL",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required env: {', '.join(missing)}")
    return ClipBuilderConfig(
        recording_dir=Path(os.environ["RECORDING_DIR"]),
        mqtt_broker=os.environ["MQTT_BROKER"],
        mqtt_client_id=(
            f"{os.environ.get('MQTT_CLIENT_ID', 'virex-clip-builder')}"
            f"-{os.getpid()}-{secrets.token_hex(4)}"
        ),
        mqtt_topic=os.environ.get("MQTT_TOPIC", MQTT_TOPIC_VIREX_EVENTS_CREATED),
        minio_endpoint=os.environ["MINIO_ENDPOINT"],
        minio_access_key=os.environ["MINIO_ACCESS_KEY"],
        minio_secret_key=os.environ["MINIO_SECRET_KEY"],
        minio_bucket=os.environ["MINIO_BUCKET"],
        minio_secure=os.environ.get("MINIO_SECURE", "true").lower() == "true",
        minio_region=os.environ["MINIO_REGION"],
        portal_internal_url=os.environ["PORTAL_INTERNAL_URL"].rstrip("/"),
        portal_jwt_path=os.environ.get(
            "PORTAL_JWT_PATH", "/etc/virex/edge.jwt"
        ),
    )


def _parse_event_created(payload: bytes) -> Mapping[str, object]:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.error("clip_event_parse_failed", error=str(e))
        raise


def _build_object_path(tenant_id: int, event_id: int) -> str:
    return f"tenants/{tenant_id}/clips/{event_id}.mp4"


class ClipBuilder:
    """One service per edge node; shares the MinIO client across events."""

    def __init__(self, cfg: ClipBuilderConfig) -> None:
        self._cfg = cfg
        self._minio = Minio(
            endpoint=cfg.minio_endpoint.split("://", 1)[-1],
            access_key=cfg.minio_access_key,
            secret_key=cfg.minio_secret_key,
            secure=cfg.minio_secure,
            region=cfg.minio_region or None,
        )
        # Force virtual-host style addressing for cloud providers whose
        # hostnames are not in the SDK's auto-detect list (Tencent COS
        # `*.myqcloud.com`, Cloudflare R2). Default is path-style which
        # COS rejects with `PathStyleDomainForbidden`.
        self._minio._base_url.virtual_style_flag = True
        try:
            if not self._minio.bucket_exists(cfg.minio_bucket):
                self._minio.make_bucket(cfg.minio_bucket)
                logger.info("minio_bucket_created", bucket=cfg.minio_bucket)
        except S3Error as e:
            # Bucket may be pre-provisioned (Tencent COS, AWS S3, etc.)
            # and policy may forbid create. Worker continues; upload
            # will surface the error per call.
            logger.warning(
                "minio_init_unavailable", error=str(e), bucket=cfg.minio_bucket
            )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=cfg.mqtt_client_id,
            clean_session=True,
        )
        self._client.on_message = self._on_mqtt_message
        self._client.on_connect = self._on_mqtt_connect
        self._client.on_disconnect = self._on_mqtt_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------------------------
    # MQTT callbacks (run on paho's network thread — bridge to asyncio)
    # ------------------------------------------------------------------
    def _on_mqtt_connect(  # noqa: ANN001
        self,
        client,
        userdata,
        flags,
        reason_code,
        properties,
    ) -> None:
        if reason_code == 0:
            logger.info("clip_builder_mqtt_connected", topic=self._cfg.mqtt_topic)
            client.subscribe(self._cfg.mqtt_topic, qos=1)
        else:
            logger.error("clip_builder_mqtt_connect_refused", reason=str(reason_code))

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        if reason_code != 0:
            logger.warning("clip_builder_mqtt_unexpected_dc", reason=str(reason_code))

    def _on_mqtt_message(self, client, userdata, msg: MQTTMessage) -> None:  # noqa: ANN001
        if self._loop is None or not self._loop.is_running():
            logger.warning("clip_event_no_event_loop")
            return
        # Schedule the async handler on the event loop owned by the main task.
        asyncio.run_coroutine_threadsafe(self._handle_event(msg.payload), self._loop)

    # ------------------------------------------------------------------
    # The actual clipboard work
    # ------------------------------------------------------------------
    async def _handle_event(self, payload: bytes) -> None:
        try:
            event = _parse_event_created(payload)
        except Exception:  # noqa: BLE001
            return

        try:
            event_id: int = int(event["event_id"])
            tenant_id: int = int(event["tenant_id"])
            event_ts_str: str = str(event["event_ts"])
            mtx_path: str = str(event["mtx_path"])
        except KeyError as e:
            logger.error("clip_event_missing_field", missing=str(e))
            return

        event_ts = datetime.fromisoformat(event_ts_str)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_out = Path(tmp_dir) / f"{uuid.uuid4().hex}.mp4"
            clip_path = await build_clip(
                recording_dir=self._cfg.recording_dir,
                mtx_path=mtx_path,
                event_ts=event_ts,
                out_path=tmp_out,
            )
            if clip_path is None:
                return

            obj_key = _build_object_path(tenant_id, event_id)
            try:
                await asyncio.to_thread(
                    self._minio.fput_object,
                    self._cfg.minio_bucket,
                    obj_key,
                    str(clip_path),
                    content_type=CLIP_OBJECT_CONTENT_TYPE,
                )
                logger.info("clip_uploaded", object_key=obj_key, size=clip_path.stat().st_size)
            except S3Error as e:
                logger.error("clip_upload_failed", error=str(e))
                return

        await self._patch_portal(event_id, obj_key)

    async def _patch_portal(self, event_id: int, clip_url: str) -> None:
        url = f"{self._cfg.portal_internal_url}/internal/events/{event_id}/clip"
        # Read the JWT issued by edge-agent. If absent (older deployment
        # without portal), fall back to PORTAL_BEARER for compatibility.
        jwt_path = Path(self._cfg.portal_jwt_path)
        if jwt_path.is_file():
            token = jwt_path.read_text(encoding="utf-8").strip()
        else:
            token = os.environ.get("PORTAL_BEARER", "")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=HTTP_PATCH_TIMEOUT_SEC) as client:
            try:
                for attempt in Retrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=0.5, max=4),
                    retry=retry_if_exception_type(httpx.HTTPError),
                    reraise=True,
                ):
                    with attempt:
                        resp = await client.patch(url, headers=headers, json={"clip_url": clip_url})
                        resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("portal_patch_failed", event_id=event_id, error=str(e))
                return
            logger.info("portal_patched", event_id=event_id, clip_url=clip_url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Block until Ctrl-C — bridges paho sync callbacks onto asyncio."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        host, _, port_str = self._cfg.mqtt_broker.partition(":")
        port = int(port_str) if port_str else 1883
        self._client.connect(host, port=port, keepalive=60)
        self._client.loop_start()

        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            logger.info("clip_builder_shutdown")
        finally:
            self._client.loop_stop()
            self._client.disconnect()
            self._loop.stop()
            self._loop.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv
    cfg = load_env_config()
    ClipBuilder(cfg).run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
