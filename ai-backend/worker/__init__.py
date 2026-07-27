# SPDX-License-Identifier: Apache-2.0
"""Virex AI edge workers.

Each worker process is bound to exactly one camera. It:
  1. Reads RTSP from MediaMTX over a normalised H.264 path.
  2. Frame-skips to the configured detection fps (default 5).
  3. POSTs JPEG bytes to the BentoML detector service on the same host.
  4. On a person detection, uploads the JPEG to MinIO (over Tailscale)
     and publishes an MQTT `virex/detections` message.

See `worker/camera_worker.py` for the entry point and `worker/config.py`
for configuration schema.
"""
