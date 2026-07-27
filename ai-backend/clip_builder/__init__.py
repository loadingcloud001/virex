# SPDX-License-Identifier: Apache-2.0
"""Clip builder service.

Subscribes to MQTT `virex/events_created` (emitted by `event-router`
when an event row is committed). On receipt, cuts a 10 s window
(5 s before + 5 s after the event timestamp) from the MediaMTX fMP4
recording, uploads to MinIO, and PATCHes the portal event row with the
`clip_url`.

v1 strategy:
    * MediaMTX records every `_h264` path as sealed 30 s fMP4 segments under
      `/recordings/<path>/<YYYY-MM-DD>_<HH-MM-SS>-<f>.mp4`.
    * clip-builder reads segments that have *already sealed*; if the event
      falls inside the currently-open segment, it waits up to
      `SEGMENT_SEAL_WAIT_SEC` for that segment to close.
"""
