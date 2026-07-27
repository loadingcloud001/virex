# SPDX-License-Identifier: Apache-2.0
"""event-router main entry point.

Binds the building blocks together:

    MQTT virex/detections
       → AsyncMqttConsumer
       → parse DetectionEvent
       → Redis cooldown (per tenant/camera/label)
       → PostgreSQL INSERT events row
       → rule match → n8n webhook
       → publish MQTT virex/events_created (clip-builder reacts)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime

import paho.mqtt.client as mqtt
import structlog
from paho.mqtt.enums import CallbackAPIVersion

from src.cooldown import COOLDOWN_WINDOW_SEC, acquire_cooldown
from src.db import (
    AlertRule,
    insert_event,
    list_rules_for_tenant,
    make_engine,
)
from src.mqtt_consumer import AsyncMqttConsumer, parse_json_payload
from src.notification_dispatcher import dispatch as n8n_dispatch
from src.rule_matcher import match_rules
from src.schema import DetectionEvent

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MQTT_INPUT_TOPIC: str = "virex/detections"
MQTT_OUTPUT_TOPIC: str = "virex/events_created"
N8N_ENV: str = "N8N_WEBHOOK_URL"
N8N_TIMEOUT_SEC: float = 10.0


class EventRouter:
    def __init__(self) -> None:
        self.db_url = os.environ["DATABASE_URL"]
        self.redis_url = os.environ.get("REDIS_URL", "redis://default@127.0.0.1:6379/0")
        self.mqtt_broker = os.environ["MQTT_BROKER"]
        self.mqtt_client_id = os.environ.get("MQTT_CLIENT_ID", "virex-event-router")
        self.n8n_url = os.environ.get(N8N_ENV, "")
        self._engine = make_engine(self.db_url)
        self._publisher_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"{self.mqtt_client_id}-publisher",
            clean_session=True,
        )
        self._publisher_client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._consumer: AsyncMqttConsumer | None = None
        self._redis = None  # redis.asyncio.Redis set in start()

    async def start(self) -> None:
        # Lazy import keeps the sync redis dependency list affect-free.
        from redis.asyncio import Redis

        self._redis = Redis.from_url(self.redis_url, decode_responses=False)
        await self._publisher_connect()
        self._consumer = AsyncMqttConsumer(
            broker=self.mqtt_broker,
            client_id=self.mqtt_client_id,
            topic=MQTT_INPUT_TOPIC,
            handler=self._on_detection,
        )
        self._consumer.connect()
        logger.info("event_router_started", input=MQTT_INPUT_TOPIC, output=MQTT_OUTPUT_TOPIC)

    def _publisher_connect(self) -> None:
        host, _, port_str = self.mqtt_broker.partition(":")
        port = int(port_str) if port_str else 1883
        self._publisher_client.connect(host, port=port, keepalive=60)
        self._publisher_client.loop_start()

    async def stop(self) -> None:
        if self._consumer:
            self._consumer.stop()
        self._publisher_client.loop_stop()
        with contextlib.suppress(Exception):
            self._publisher_client.disconnect()
        if self._redis is not None:
            await self._redis.aclose()

    # ------------------------------------------------------------------
    # Core handler
    # ------------------------------------------------------------------
    async def _on_detection(self, payload: bytes, topic: str) -> None:
        del topic  # Unused; topic is fixed for us.
        data = parse_json_payload(payload)
        if data is None:
            logger.warning("detection_invalid_json")
            return

        try:
            event = DetectionEvent.model_validate(data)
        except Exception as e:  # noqa: BLE001
            logger.error("detection_schema_failed", error=str(e))
            return

        if not event.detections:
            return  # Empty payloads are fine — worker emits only when at least one kept.

        # Pick highest-scoring person detection for the alert summary.
        chosen = max(event.detections, key=lambda d: d.score)
        label = chosen.label

        # ---- 1. Cooldown (per tenant/camera/label) ----------------------
        if not await acquire_cooldown(
            self._redis,
            tenant_id=event.tenant_id,
            camera_id=event.camera_id,
            label=label,
            window_sec=COOLDOWN_WINDOW_SEC,
        ):
            logger.debug(
                "detection_skipped_cooldown",
                tenant_id=event.tenant_id,
                camera_id=event.camera_id,
                label=label,
            )
            return

        # ---- 2. DB insert ---------------------------------------------------
        event_time = datetime.fromisoformat(event.ts)
        bbox_json = json.dumps(chosen.box)
        async with self._engine() as session:
            rules: list[AlertRule] = await list_rules_for_tenant(session, event.tenant_id)
            event_id = await insert_event(
                session,
                tenant_id=event.tenant_id,
                camera_id=event.camera_id,
                event_uuid=event.event_uuid,
                class_label=label,
                score=chosen.score,
                bbox=bbox_json,
                snapshot_url=event.snapshot_url,
                event_time=event_time,
            )
            await session.commit()

        # ---- 3. Publish virex/events_created -------------------------------
        created = {
            "v": 1,
            "event_id": event_id,
            "tenant_id": event.tenant_id,
            "mtx_path": event.mtx_path,
            "event_ts": event.ts,
        }
        self._publisher_client.publish(
            MQTT_OUTPUT_TOPIC,
            payload=json.dumps(created, separators=(",", ":")).encode("utf-8"),
            qos=1,
        )

        # ---- 4. Rule match + n8n fan-out -----------------------------------
        matched = match_rules(rules, label=label, score=chosen.score)
        if not matched or not self.n8n_url:
            logger.info("no_matched_rule_or_no_n8n", event_id=event_id)
            return

        await n8n_dispatch(
            self.n8n_url,
            {
                "event_id": event_id,
                "event_uuid": event.event_uuid,
                "tenant_id": event.tenant_id,
                "camera_id": event.camera_id,
                "label": label,
                "score": chosen.score,
                "bbox": chosen.box,
                "snapshot_url": event.snapshot_url,
                "matches": matched,
                "triggered_at": event.ts,
            },
        )


async def amain() -> int:
    router = EventRouter()
    await router.start()
    try:
        # Block forever — event loop is driven by paho's threads + asyncio.run_coroutine_threadsafe.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await router.stop()
    return 0


def main() -> int:
    required = ["DATABASE_URL", "MQTT_BROKER"]
    if missing := [k for k in required if not os.environ.get(k)]:
        raise SystemExit(f"Missing required env: {', '.join(missing)}")
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("event_router_shutdown_keyboard")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__: tuple[str, ...] = ("EventRouter", "main")
