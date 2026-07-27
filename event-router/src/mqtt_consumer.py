# SPDX-License-Identifier: Apache-2.0
"""Bridged paho-mqtt async consumer.

paho runs its network loop on its own thread; we hand messages to the
asyncio loop owned by the main router. This keeps the HTTP / DB / Redis
work on a single coroutine pool so SQLAlchemy sessions stay coherent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import paho.mqtt.client as mqtt
import structlog
from paho.mqtt.enums import CallbackAPIVersion

logger = structlog.get_logger(__name__)


MessageHandler = Callable[[bytes, str], Awaitable[None]]


class AsyncMqttConsumer:
    """Subscribes once; dispatches messages to an async handler."""

    def __init__(
        self,
        *,
        broker: str,
        client_id: str,
        topic: str,
        handler: MessageHandler,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._topic = topic
        self._handler = handler
        self._loop = loop or asyncio.get_event_loop()
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        host, _, port_str = broker.partition(":")
        self._host = host
        self._port = int(port_str) if port_str else 1883

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        if reason_code == 0:
            logger.info("mqtt_connected", topic=self._topic)
            client.subscribe(self._topic, qos=1)
        else:
            logger.error("mqtt_connect_refused", reason=str(reason_code))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        if reason_code != 0:
            logger.warning("mqtt_unexpected_dc", reason=str(reason_code))

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:  # noqa: ANN001
        if not self._loop.is_running():
            logger.warning("mqtt_msg_no_loop", topic=msg.topic)
            return
        asyncio.run_coroutine_threadsafe(self._safe_handle(msg.payload, msg.topic), self._loop)

    async def _safe_handle(self, payload: bytes, topic: str) -> None:
        try:
            await self._handler(payload, topic)
        except Exception:  # noqa: BLE001
            logger.exception("mqtt_handle_failed", topic=topic, payload_preview=payload[:64])

    def connect(self) -> None:
        self._client.connect(self._host, port=self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("mqtt_consumer_stopped")


def parse_json_payload(payload: bytes) -> dict[str, object] | None:
    """Parse raw MQTT body; None on invalid JSON."""
    try:
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


__all__: tuple[str, ...] = ("AsyncMqttConsumer", "parse_json_payload")
