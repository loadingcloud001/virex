# SPDX-License-Identifier: Apache-2.0
"""Event-router: MQTT `virex/detections` → Redis cooldown → DB insert → n8n
webhook → MQTT `virex/events_created`."""
from src.main import main  # noqa: F401
