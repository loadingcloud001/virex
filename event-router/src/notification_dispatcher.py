# SPDX-License-Identifier: Apache-2.0
"""Notification dispatcher — POST to n8n webhook with retry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import structlog
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)

N8N_TIMEOUT_SEC: float = 10.0
N8N_RETRY_ATTEMPTS: int = 3


async def dispatch(
    n8n_url: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """POST payload to the n8n webhook URL.

    Returns the webhook response body parsed as JSON; Pydantic shapes can
    be added downstream if n8n ever needs strict contracts. Retries use
    exponential backoff (0.5s, 1s, 2s) on any `httpx.HTTPError`.
    """
    async with httpx.AsyncClient(timeout=N8N_TIMEOUT_SEC) as client:
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(N8N_RETRY_ATTEMPTS),
                wait=wait_exponential(multiplier=0.5, max=4),
                retry=retry_if_exception_type(httpx.HTTPError),
                reraise=True,
            ):
                with attempt:
                    resp = await client.post(n8n_url, json=payload)
                    resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(
                "n8n_dispatch_failed",
                url=n8n_url,
                error=str(e),
            )
            raise
        try:
            return resp.json()
        except (ValueError, httpx.DecodingError):
            return {"raw": resp.text}


__all__: tuple[str, ...] = ("dispatch",)
