# SPDX-License-Identifier: Apache-2.0
"""Async-friendly Mosquitto publisher wrapping paho-mqtt v2.

The per-camera worker calls `publish(event_dict)` to emit `virex/detections`
messages. QoS=1 gives broker-side at-least-once delivery; event-router
must dedup via Redis cooldown.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import paho.mqtt.client as mqtt
import structlog
from paho.mqtt.enums import CallbackAPIVersion

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_KEEPALIVE: int = 60
QOS_AT_LEAST_ONCE: int = 1
CONNECT_TIMEOUT_SEC: float = 10.0


class MqttPublisher:
    """Connects once on construction; reconnects automatically via paho.

    Not thread-safe for concurrent `publish()` from multiple async tasks —
    each worker's camera loop owns its own publisher.
    """

    def __init__(
        self,
        *,
        broker: str,
        client_id: str,
        topic: str,
        keepalive: int = DEFAULT_KEEPALIVE,
    ) -> None:
        self._topic = topic
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_disconnect = self._on_disconnect
        self._client.on_connect = self._on_connect

        host, _, port_str = broker.partition(":")
        port = int(port_str) if port_str else 1883
        self._client.connect_async(host, port=port, keepalive=keepalive)
        self._client.loop_start()

    # ------------------------------------------------------------------
    # paho callbacks (sync — executed by paho's network thread)
    # ------------------------------------------------------------------
    def _on_connect(  # noqa: ANN001
        self,
        client: mqtt.Client,
        userdata: object | None,
        flags,  # noqa: ANN001
        reason_code,  # noqa: ANN001
        properties,  # noqa: ANN001
    ) -> None:
        if reason_code == 0:
            logger.info("mqtt_connected", broker=client._host if hasattr(client, "_host") else "?")
        else:
            logger.error("mqtt_connect_refused", reason=str(reason_code))

    def _on_disconnect(  # noqa: ANN001
        self,
        client: mqtt.Client,
        userdata: object | None,
        flags,  # noqa: ANN001
        reason_code,  # noqa: ANN001
        properties,  # noqa: ANN001
    ) -> None:
        if reason_code != 0:
            logger.warning("mqtt_unexpected_disconnect", reason=str(reason_code))
        else:
            logger.info("mqtt_clean_disconnect")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def publish(self, payload: Mapping[str, object]) -> None:
        """Serialize `payload` to JSON and publish to the configured topic.

        Call is non-blocking; paho queues internally. Failure to enqueue
        returns an `MQTT_ERR_*` info but is logged, not raised — the
        snapshot was already accepted by MinIO and the next emit can
        also succeed.
        """
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        info = self._client.publish(self._topic, payload=body, qos=QOS_AT_LEAST_ONCE)
        if info.rc == mqtt.MQTT_ERR_SUCCESS or info.rc == mqtt.MQTT_ERR_NO_CONN:
            logger.debug("mqtt_publish_queued", mid=info.mid, bytes=len(body))
        else:
            logger.warning("mqtt_publish_failed", rc=info.rc, mid=info.mid)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("mqtt_closed")
