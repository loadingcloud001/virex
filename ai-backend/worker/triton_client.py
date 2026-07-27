# SPDX-License-Identifier: AGPL-3.0
"""Triton Inference Server client (KServe HTTP protocol).

Virex workers talk to Triton Inference Server via the standard KServe
v2 HTTP API. We don't depend on `tritonclient[http]` to keep the worker
image slim — `httpx` is already required and KServe is just JSON.

Three endpoints are used:

- `GET  /v2/health/ready`              — readiness probe.
- `GET  /v2/models/<name>`             — model metadata (label, dims).
- `POST /v2/models/<name>/infer`       — actual inference call.

The `infer` request body wraps the JPEG bytes as a BYTES tensor:

    {
      "inputs": [{
        "name": "IMAGE",
        "shape": [1],
        "datatype": "BYTES",
        "data": ["<base64 JPEG>"]
      }]
    }

Response shape for our ensembles:

    {
      "outputs": [
        {"name": "detections", "shape": [N, 6], "datatype": "FP32",
         "data": [label, score, x1, y1, x2, y2, ...]},
        {"name": "masks",      "shape": [N, 256, 256], "datatype": "FP32"},
        {"name": "depth_map",  "shape": [1, 384, 384],  "datatype": "FP32"}
      ],
      "model_version": "1"
    }

Failures (Triton down, 5xx, network) are caught and surfaced as
`InferenceError` so the worker can keep running. Workers never crash
the per-camera loop on a single inference failure.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)


class InferenceError(Exception):
    """Raised on Triton transport / 5xx / unexpected response."""


@dataclass
class EnsembleResult:
    """Parsed output of a Triton ensemble inference call.

    `detections` is a list of dicts in the SAME shape as the FastAPI
    shim's `/detect` response: `{label:int, score:float, box:[x1,y1,x2,y2]}`
    in **pixel** coordinates. The worker normalises by dividing by image
    width/height — so the downstream filter + zone + publish path is
    identical regardless of `detector_kind`.
    """

    detections: list[dict]
    masks: list[bytes] | None = None
    depth_map: bytes | None = None
    inference_ms: float = 0.0
    model_version: str = ""


class TritonClient:
    """Synchronous-ish HTTP client to a Triton Inference Server.

    Triton's KServe v2 API is JSON over HTTP — `async with httpx.AsyncClient`
    is the right fit. We keep a single client per worker (long-lived
    connection pool) and refresh on a 5xx that suggests DNS / upstream
    failover.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Normalise trailing slash.
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._client = client or httpx.AsyncClient(
            timeout=timeout_sec,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def ready(self) -> bool:
        try:
            r = await self._client.get(f"{self._base_url}/v2/health/ready")
            return r.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("triton_ready_failed", error=str(e))
            return False

    async def infer_ensemble(
        self,
        ensemble_name: str,
        jpeg_bytes: bytes,
    ) -> EnsembleResult:
        """Call a Triton ensemble and parse the response.

        Raises:
            InferenceError: on transport failure, 4xx/5xx, or
                unexpected response shape.
        """
        encoded = base64.b64encode(jpeg_bytes).decode("ascii")
        body = {
            "inputs": [
                {
                    "name": "IMAGE",
                    "shape": [1],
                    "datatype": "BYTES",
                    "data": [encoded],
                }
            ]
        }
        url = f"{self._base_url}/v2/models/{ensemble_name}/infer"
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise InferenceError(f"triton http error: {e}") from e

        if resp.status_code >= 400:
            raise InferenceError(
                f"triton {resp.status_code}: {resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise InferenceError(f"triton returned non-JSON: {e}") from e

        return _parse_ensemble(payload)


def _parse_ensemble(payload: dict) -> EnsembleResult:
    """Parse a Triton ensemble JSON response into `EnsembleResult`."""
    outputs = payload.get("outputs") or []
    by_name = {o.get("name"): o for o in outputs}

    dets_out: list[dict] = []
    det_obj = by_name.get("detections")
    if det_obj is not None:
        data = det_obj.get("data") or []
        for i in range(0, len(data), 6):
            try:
                label = int(data[i])
                score = float(data[i + 1])
                x1 = float(data[i + 2])
                y1 = float(data[i + 3])
                x2 = float(data[i + 4])
                y2 = float(data[i + 5])
            except (ValueError, IndexError):
                continue
            dets_out.append(
                {"label": label, "score": score, "box": [x1, y1, x2, y2]}
            )

    masks_raw: tuple[bytes, ...] | None = None
    masks_obj = by_name.get("masks")
    if masks_obj is not None:
        masks_raw = tuple(bytes(m) for m in (masks_obj.get("data") or []))

    depth_raw: bytes | None = None
    depth_obj = by_name.get("depth_map")
    if depth_obj is not None:
        data = depth_obj.get("data") or []
        try:
            depth_raw = bytes(data)
        except TypeError:
            depth_raw = None

    return EnsembleResult(
        detections=tuple(dets_out),
        masks=masks_raw,
        depth_map=depth_raw,
        model_version=str(payload.get("model_version", "")),
    )


def select_ensemble(pipeline: tuple, *, on_motion: bool = False) -> str:  # noqa: ARG001
    """Map a camera's `pipeline` config to a Triton ensemble name.

    `on_motion` is accepted for v1-API parity — per-stage trigger
    evaluation (`on_motion` filters out `trigger=always` stages that
    don't need to run when motion is false) is a Phase-2 concern. For
    v1, ensemble selection is purely about which stages are enabled,
    independent of the per-frame motion state.

    Heuristic mapping (matches `deploy/edge/state/triton/ensembles/`):
    - only `detect` stage                  → `detect_only`
    - detect + segment                     → `detect_segment`
    - detect + depth                        → `detect_depth`
    - detect + segment + depth              → `detect_segment_depth`
    """
    stages = {p.stage for p in pipeline}
    if stages == {"detect"}:
        return "detect_only"
    if stages == {"detect", "segment"}:
        return "detect_segment"
    if stages == {"detect", "depth"}:
        return "detect_depth"
    if stages == {"detect", "segment", "depth"}:
        return "detect_segment_depth"
    # Fallback — single model, no ensemble.
    if "segment" in stages:
        return "detect_segment"
    if "depth" in stages:
        return "detect_depth"
    return "detect_only"


__all__: tuple[str, ...] = (
    "EnsembleResult",
    "InferenceError",
    "TritonClient",
    "select_ensemble",
)
