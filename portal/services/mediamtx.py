# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for building MediaMTX playback URLs.

Centralises the HLS / WebRTC URL construction so every caller (camera
detail endpoint, UI camera-detail route, future WHEP player) uses the
same logic. The previous code duplicated a fragile ``rsplit(':', 1)``
trick that broke when ``mediamtx_public_url`` had no explicit port.
"""

from __future__ import annotations

from urllib.parse import urlparse

from core.config import settings
from schemas.events import HlsUrlResponse

# MediaMTX WHEP (WebRTC) listens on a separate port from HLS.
_WHEP_PORT = "8889"


def build_playback_urls(mtx_path: str) -> HlsUrlResponse:
    """Build the HLS + WebRTC playback URLs for a given ``mtx_path``.

    Uses ``urllib.parse.urlparse`` so the base URL may carry or omit a
    port (and may be IPv6, HTTPS, or a bare hostname) without breaking.

    Args:
        mtx_path: The MediaMTX path segment (e.g. ``"cam01"``).

    Returns:
        ``HlsUrlResponse`` with ``hls_url``, ``webrtc_url``, ``mtx_path``.
    """
    parsed = urlparse(settings.mediamtx_public_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"

    # Preserve the original port for the HLS URL; default to 8888.
    hls_port = f":{parsed.port}" if parsed.port else ":8888"
    hls_base = f"{scheme}://{host}{hls_port}"

    # WebRTC WHEP runs on a fixed separate port.
    webrtc_base = f"{scheme}://{host}:{_WHEP_PORT}"

    return HlsUrlResponse(
        hls_url=f"{hls_base}/{mtx_path}/index.m3u8",
        webrtc_url=f"{webrtc_base}/{mtx_path}/whep",
        mtx_path=mtx_path,
    )


__all__: tuple[str, ...] = ("build_playback_urls",)
