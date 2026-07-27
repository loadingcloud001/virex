# SPDX-License-Identifier: AGPL-3.0
"""Tests for the Triton Inference Server client (KServe v2 HTTP)."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from worker.config_hot import HotPipelineStage
from worker.triton_client import (
    InferenceError,
    TritonClient,
    _parse_ensemble,
    select_ensemble,
)


# ---------------------------------------------------------------------------
# Helper: httpx MockTransport returning canned responses.
# ---------------------------------------------------------------------------
def _make_transport(handler):
    """Return an httpx.MockTransport with given async handler."""

    async def _h(request: httpx.Request) -> httpx.Response:
        return await handler(request)

    return httpx.MockTransport(_h)


def _make_client(handler) -> TritonClient:
    transport = _make_transport(handler)
    # httpx.AsyncClient with mock transport: base_url used for absolute
    # URL building inside the client.
    http = httpx.AsyncClient(
        base_url="http://triton:38000",
        transport=transport,
    )
    return TritonClient(
        base_url="http://triton:38000",
        client=http,
    )


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ready_returns_true_on_200() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v2/health/ready"
        return httpx.Response(200, json={"ready": True})

    client = _make_client(handler)
    try:
        assert await client.ready() is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ready_returns_false_on_connection_error() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _make_client(handler)
    try:
        assert await client.ready() is False
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Inference call
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_infer_ensemble_sends_correct_kserve_body() -> None:
    captured: dict[str, Any] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/health/ready":
            return httpx.Response(200)
        assert req.method == "POST"
        assert req.url.path == "/v2/models/detect_only/infer"
        captured["body"] = req.content.decode()
        return httpx.Response(
            200,
            json={
                "model_version": "1",
                "outputs": [
                    {
                        "name": "detections",
                        "shape": [1, 6],
                        "datatype": "FP32",
                        "data": [0, 0.92, 100, 50, 200, 250],
                    },
                ],
            },
        )

    client = _make_client(handler)
    try:
        result = await client.infer_ensemble("detect_only", b"\xff\xd8\xff\xe0fakejpeg")
        assert result.model_version == "1"
        assert len(result.detections) == 1
        d = result.detections[0]
        # Detection is a dict in the same shape as FastAPI `/detect`.
        assert d["label"] == 0
        assert d["score"] == 0.92
        assert d["box"] == [100, 50, 200, 250]
        # Validate the request body is base64 JPEG.
        body = captured["body"]
        import json as _json

        parsed = _json.loads(body)
        encoded = parsed["inputs"][0]
        assert encoded["name"] == "IMAGE"
        assert encoded["datatype"] == "BYTES"
        assert encoded["shape"] == [1]
        assert base64.b64decode(encoded["data"][0]) == b"\xff\xd8\xff\xe0fakejpeg"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_infer_5xx_raises_inference_error() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="triton offline")

    client = _make_client(handler)
    try:
        with pytest.raises(InferenceError, match="503"):
            await client.infer_ensemble("detect_only", b"jpeg")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_infer_4xx_raises_inference_error() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad input")

    client = _make_client(handler)
    try:
        with pytest.raises(InferenceError, match="400"):
            await client.infer_ensemble("detect_only", b"jpeg")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_infer_non_json_response_raises() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = _make_client(handler)
    try:
        with pytest.raises(InferenceError, match="non-JSON"):
            await client.infer_ensemble("detect_only", b"jpeg")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_infer_connect_error_raises() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _make_client(handler)
    try:
        with pytest.raises(InferenceError, match="http error"):
            await client.infer_ensemble("detect_only", b"jpeg")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_ensemble_empty_outputs() -> None:
    result = _parse_ensemble({"outputs": []})
    assert result.detections == ()
    assert result.masks is None
    assert result.depth_map is None


def test_parse_ensemble_multiple_detections() -> None:
    payload = {
        "model_version": "1",
        "outputs": [
            {
                "name": "detections",
                "shape": [2, 6],
                "datatype": "FP32",
                "data": [
                    0, 0.92, 10, 20, 100, 200,
                    2, 0.85, 50, 60, 150, 220,
                ],
            },
        ],
    }
    result = _parse_ensemble(payload)
    assert len(result.detections) == 2
    assert result.detections[0]["label"] == 0
    assert result.detections[1]["label"] == 2
    assert result.model_version == "1"


def test_parse_ensemble_skips_malformed_rows() -> None:
    """Rows with wrong length or non-numeric data are silently skipped."""
    payload = {
        "outputs": [
            {
                "name": "detections",
                "shape": [2, 6],
                "datatype": "FP32",
                # First row OK, second row truncated.
                "data": [0, 0.9, 1, 2, 3, 4, 0.5],
            },
        ],
    }
    result = _parse_ensemble(payload)
    assert len(result.detections) == 1


def test_parse_ensemble_extracts_masks_and_depth() -> None:
    payload = {
        "outputs": [
            {"name": "detections", "shape": [0, 6], "datatype": "FP32", "data": []},
        ],
    }
    result = _parse_ensemble(payload)
    assert result.masks is None
    assert result.depth_map is None


# ---------------------------------------------------------------------------
# select_ensemble
# ---------------------------------------------------------------------------
def _stage(stage: str, trigger: str = "always") -> HotPipelineStage:
    return HotPipelineStage(
        stage=stage, trigger=trigger, target_objects=(), zone_id=None
    )


def test_select_ensemble_only_detect() -> None:
    stages = (_stage("detect"),)
    assert select_ensemble(stages, on_motion=True) == "detect_only"
    assert select_ensemble(stages, on_motion=False) == "detect_only"


def test_select_ensemble_detect_segment() -> None:
    stages = (_stage("detect"), _stage("segment"))
    assert select_ensemble(stages, on_motion=True) == "detect_segment"


def test_select_ensemble_detect_depth() -> None:
    stages = (_stage("detect"), _stage("depth"))
    assert select_ensemble(stages, on_motion=True) == "detect_depth"


def test_select_ensemble_detect_segment_depth() -> None:
    stages = (_stage("detect"), _stage("segment"), _stage("depth"))
    assert select_ensemble(stages, on_motion=True) == "detect_segment_depth"


def test_select_ensemble_segment_only_falls_back_to_detect_segment() -> None:
    """Pipeline without `detect` is unusual; fall back to detect_segment."""
    stages = (_stage("segment"),)
    assert select_ensemble(stages, on_motion=True) == "detect_segment"


def test_select_ensemble_empty_pipeline_returns_detect_only() -> None:
    assert select_ensemble((), on_motion=True) == "detect_only"


def test_select_ensemble_depth_only_returns_detect_depth() -> None:
    """Even with no explicit detect stage, depth-only maps to detect_depth."""
    stages = (_stage("depth"),)
    assert select_ensemble(stages, on_motion=True) == "detect_depth"
