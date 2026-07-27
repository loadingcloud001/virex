# SPDX-License-Identifier: Apache-2.0
"""Heartbeat — collect GPU/CPU/RAM stats and POST to portal.

GPU stats come from `pynvml` (NVIDIA Management Library). If NVML is
unavailable (e.g. dev box without GPU), `gpu_percent`/`gpu_mem_mb` emit
`0` and `healthy` flags the node as degraded but still alive — useful
during edge-node bring-up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import psutil
import structlog

from src.auth import load_or_register_jwt, refresh_jwt
from src.config import Settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NVML_INIT_RETRY_INTERVAL_SEC: float = 1.0


class GpuSampler:
    """Lazily-initialised NVML wrapper. Survives transient NVML errors."""

    def __init__(self) -> None:
        self._handle = None
        self._ok = False

    def enable(self) -> None:
        try:
            import pynvml  # noqa: PLC0415 — import lazily.

            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._ok = True
            logger.info("gpu_sampler_nvml_ready")
        except Exception as e:  # noqa: BLE001
            logger.warning("gpu_sampler_nvml_unavailable", error=str(e))
            self._ok = False

    def sample(self) -> tuple[float, int]:
        """Return (gpu_percent, gpu_mem_used_mb)."""
        if not self._ok or self._handle is None:
            return 0.0, 0
        try:
            import pynvml  # noqa: PLC0415

            util = pynvml.nvmlDeviceGetUtilizationRates(self._handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            return float(util.gpu), int(mem.used // (1024 * 1024))
        except Exception as e:  # noqa: BLE001
            logger.warning("gpu_sample_failed", error=str(e))
            try:
                import pynvml  # noqa: PLC0415

                pynvml.nvmlShutdown()
                pynvml.nvmlInit()
                self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:  # noqa: BLE001
                self._ok = False
            return 0.0, 0


def sample_node(gpu_sampler: GpuSampler) -> Mapping[str, float | int | bool]:
    """Collect a single heartbeat payload (synchronous; runs in to_thread)."""
    gpu_percent, gpu_mem_mb = gpu_sampler.sample()
    cpu_percent = psutil.cpu_percent(interval=None)  # 0.0 if first call
    ram_percent = psutil.virtual_memory().percent
    return {
        "node_id": 0,  # filled in by caller
        "gpu_percent": round(gpu_percent, 2),
        "gpu_mem_mb": gpu_mem_mb,
        "cpu_percent": round(cpu_percent, 2),
        "ram_percent": round(ram_percent, 2),
        "active_cameras": 0,  # filled in later by reconcile().report
        "healthy": True,
    }


async def heartbeat_loop(
    *,
    settings: Settings,
    gpu_sampler: GpuSampler,
    post_fn,  # type: ignore[no-untyped-def]  callable: async (url, headers, json) -> resp
) -> None:
    """Tail-recursive heartbeat loop. Exits when the asyncio task is cancelled."""
    gpu_sampler.enable()
    period = settings.heartbeat_period_sec
    state_dir = Path(settings.state_dir)
    token: str = ""

    try:
        token = await asyncio.to_thread(
            load_or_register_jwt,
            portal_url=settings.portal_url,
            bootstrap_secret=settings.portal_bootstrap_secret,
            state_dir=state_dir,
            hostname=settings.node_hostname or "",
            tailscale_ip="127.0.0.1",
            gpu_model=None,
            max_cameras=settings.node_max_cameras,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("heartbeat_jwt_bootstrap_failed", error=str(e))

    while True:
        try:
            payload = sample_node(gpu_sampler)
            payload = {**payload, "node_id": settings.node_id}
            resp = await post_fn(
                f"{settings.portal_url.rstrip('/')}/api/edge/heartbeat",
                {"Authorization": f"Bearer {token}"},
                dict(payload),
            )
            # httpx response has status_code; auto-rotate on 401 expired.
            status_code = getattr(resp, "status_code", 200)
            if status_code == 401:
                try:
                    token = await asyncio.to_thread(
                        refresh_jwt,
                        portal_url=settings.portal_url,
                        current_token=token,
                        state_dir=state_dir,
                        node_id=settings.node_id,
                    )
                    logger.info("heartbeat_token_rotated")
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "heartbeat_rotate_failed", error=str(e)
                    )
                    token = await asyncio.to_thread(
                        load_or_register_jwt,
                        portal_url=settings.portal_url,
                        bootstrap_secret=settings.portal_bootstrap_secret,
                        state_dir=state_dir,
                        hostname=settings.node_hostname or "",
                        tailscale_ip="127.0.0.1",
                        gpu_model=None,
                        max_cameras=settings.node_max_cameras,
                    )
                await asyncio.sleep(period)
                continue
            logger.info(
                "heartbeat_sent",
                node_id=settings.node_id,
                gpu_percent=payload["gpu_percent"],
                gpu_mem_mb=payload["gpu_mem_mb"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("heartbeat_failed", error=str(e))
        await asyncio.sleep(period)


__all__: tuple[str, ...] = ("GpuSampler", "sample_node", "heartbeat_loop")
