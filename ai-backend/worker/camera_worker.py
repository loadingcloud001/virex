# SPDX-License-Identifier: AGPL-3.0
"""Per-camera ingestion + inference loop.

Each worker process runs ONE instance of `CameraLoop` per camera entry.
Loops are independent: a slow RTSP read on camera A does not block
detection-posting for camera B, so we don't need a producer/consumer
queue for v1.

Hot-reload wiring:
  * `CameraLoop` no longer takes an immutable `CameraConfig`; it holds
    a `HotConfigStore` reference and reads `self._hot.get()` once per
    frame. The reloader swaps the snapshot atomically (Tier-A fields
    like `fps`, `min_score`, `classes`, `roi`, `snapshot_quality`,
    `motion.*`, `masks`, `zones`, `pipeline` apply on the next frame,
    with zero downtime).
  * `MotionDetector` and `ZoneFilter` are rebuilt on config version
    change — their per-camera parameters (`motion.threshold`,
    `zones[i].inertia`, …) would otherwise drift stale across a
    Tier-A hot-reload. We track `self._motion_cfg_signature` so a
    rebuild only happens when relevant params actually changed.
  * The `request_reconnect()` method pushes a sentinel into the queue
    to break out of `_loop_once`; on the next iteration `run()` re-reads
    the RTSP URL from `self._hot.get()` (Tier B — `source_rtsp`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

import av
import cv2
import httpx
import numpy as np
import structlog
from numpy.typing import NDArray

from worker._coco import COCO_NAMES as _COCO_NAMES
from worker.config_hot import HotConfig, HotConfigStore
from worker.minio_client import SnapshotUploader, SnapshotUploadError, snapshot_key
from worker.motion import MotionDetector
from worker.mqtt_client import MqttPublisher
from worker.schema import DetectionEvent, DetectionPayload
from worker.triton_client import InferenceError, TritonClient
from worker.zones import build_zone_filter

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RTSP_OPEN_TIMEOUT_SEC: float = 15.0
CONSUMER_RECONNECT_BACKOFF_SEC: float = 5.0
JPEG_DEFAULT_QUALITY: int = 80
HTTP_TIMEOUT_SEC: float = 30.0
FRAME_DECODE_ERROR_SLEEP_SEC: float = 0.2
H264_INDEX_ZERO: int = 0
MIN_RECONNECT_BACKOFF_SEC: float = 1.0


def _safe_put(queue: asyncio.Queue, frame: av.VideoFrame) -> None:
    """Async-safe put_nowait: drop the new frame when the queue is full.

    Wrapping `put_nowait` in a function keeps the feeder simple while
    silencing `QueueFull` warnings when the consumer is briefly slow.
    """
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(frame)


class CameraLoop:
    """One asyncio task bound to a single camera.

    Reads inference parameters from the shared `HotConfigStore` per frame;
    a `request_reconnect()` call from the reloader forces the feeder to
    close its RTSP container and reopen (used for Tier-B `source_rtsp`
    changes).
    """

    def __init__(  # noqa: PLR0913
        self,
        mtx_path: str,
        store: HotConfigStore,
        *,
        http: httpx.AsyncClient,
        uploader: SnapshotUploader,
        publisher: MqttPublisher,
        triton: TritonClient,
        frame_id_start: int = 0,
    ) -> None:
        self._path = mtx_path
        self._store = store
        self._http = http
        self._uploader = uploader
        self._publisher = publisher
        self._triton = triton
        self._frame_id: int = frame_id_start
        self._reconnect_event = asyncio.Event()
        self._reconnect_event.clear()
        # Per-camera motion detector (Frigate-style cv2 pre-filter).
        # Rebuilt whenever the relevant HotConfig params change so a
        # Tier-A hot-reload of motion.threshold / contour_area actually
        # applies on the next frame, not just on first init.
        self._motion: MotionDetector | None = None
        self._motion_signature: tuple | None = None
        self._zone_filter = None  # rebuilt on zones change
        self._zone_signature: tuple | None = None

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Run forever; reconnects on stream failures or reload requests."""
        cfg0 = self._store.get()
        cam0 = self._path_cfg(cfg0)
        if cam0 is None:
            logger.warning("camera_loop_unknown_path", path=self._path)
            return
        full_path = f"{self._path}{cfg0.layer_suffix}"
        self._full_mtx_path: str = full_path

        logger.info(
            "camera_loop_start",
            mtx_path=full_path,
            rtsp_url=self._redact(self._rtsp_url(cfg0)),
            fps=cam0.fps,
        )
        while True:
            try:
                await self._loop_once()
            except av.FFmpegError as e:
                logger.warning(
                    "rtsp_stream_error", error=str(e), path=self._full_mtx_path
                )
                await asyncio.sleep(CONSUMER_RECONNECT_BACKOFF_SEC)
            except Exception:  # noqa: BLE001
                logger.exception("camera_loop_crashed", path=self._full_mtx_path)
                await asyncio.sleep(CONSUMER_RECONNECT_BACKOFF_SEC)

    def request_reconnect(self) -> None:
        """Signal the loop to break out of `_loop_once` and reopen RTSP.

        Called by `ConfigReloader` when the per-camera `source_rtsp`
        field changed (Tier B). The loop's main `run()` already
        reconnects on stream failure; we just need to break the frame
        loop with a sentinel. `set()` wakes any awaiting reconnect.
        """
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _path_cfg(self, cfg: HotConfig):
        return cfg.get_camera(self._path)

    def _rtsp_url(self, cfg: HotConfig) -> str:
        cam = self._path_cfg(cfg)
        if cam is None or not cam.source_rtsp:
            return f"rtsp://127.0.0.1:19554/{self._path}{cfg.layer_suffix}"
        return cam.source_rtsp

    async def _loop_once(self) -> None:
        """Open RTSP and consume frames at the configured fps.

        Tier-A reads happen inline below (`period = 1.0 / fps`,
        `min_score`, `classes`) and are picked up from the latest
        `HotConfig` on every iteration — no process restart.

        Tier-B (RTSP URL change) is handled by `_reconnect_event` set
        from the reloader; on the next `_safe_wait_reconnect()` we'll
        break out and `run()` re-enters `_loop_once` with the new URL.
        """
        cfg = self._store.get()
        cam = self._path_cfg(cfg)
        if cam is None:
            await asyncio.sleep(CONSUMER_RECONNECT_BACKOFF_SEC)
            return

        url = self._rtsp_url(cfg)
        self._reconnect_event.clear()

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[av.VideoFrame | None] = asyncio.Queue(maxsize=2)

        def _feeder() -> None:
            container: av.container.InputContainer | None = None
            try:
                # RTSP transport: prefer TCP (works through every hop
                # including MediaMTX and Hikvision NVRs). Hikvision URLs
                # with `transportmode=multicast` actually negotiate TCP
                # fine — the URL flag tells the server, the client
                # can still pull over TCP.
                container = av.open(
                    url,
                    options={
                        "rtsp_transport": "tcp",
                        "fflags": "nobuffer",
                        "flags": "low_delay",
                    },
                    timeout=RTSP_OPEN_TIMEOUT_SEC,
                )
                for frame in container.decode(video=H264_INDEX_ZERO):
                    if self._reconnect_event.is_set():
                        break
                    try:
                        loop.call_soon_threadsafe(_safe_put, queue, frame)
                    except RuntimeError:
                        return
            except av.FFmpegError as e:
                logger.warning(
                    "rtsp_feeder_error",
                    error=str(e),
                    path=self._full_mtx_path,
                )
            finally:
                if container is not None:
                    container.close()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, None)

        feeder_task = asyncio.create_task(
            asyncio.to_thread(_feeder),
            name=f"feeder-{self._full_mtx_path}",
        )
        reconnect_task = asyncio.create_task(
            self._reconnect_event.wait(),
            name=f"reconnect-{self._full_mtx_path}",
        )

        try:
            while True:
                cfg_now = self._store.get()
                cam_now = self._path_cfg(cfg_now)
                if cam_now is None:
                    # Camera removed from config — graceful exit.
                    self._reconnect_event.set()

                period = 1.0 / max(1, cam_now.fps)
                url_now = self._rtsp_url(cfg_now)
                if url_now != url:
                    url = url_now
                    self._reconnect_event.set()

                # Wait for either a frame OR a reconnect signal.
                frame_wait = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {frame_wait, reconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reconnect_task in done:
                    frame_wait.cancel()
                    break

                frame = frame_wait.result()
                if frame is None:
                    # Feeder exited (RTSP error or reconnect signal).
                    # Treat as a stream error so the outer run() loop
                    # applies CONSUMER_RECONNECT_BACKOFF_SEC before
                    # re-entering _loop_once. Without this, a 404 /
                    # network failure spins a tight reconnect cycle
                    # (~RTSP_OPEN_TIMEOUT per attempt, no jitter) and
                    # floods logs with `rtsp_feeder_error` lines.
                    raise av.FFmpegError(
                        "feeder closed without frame",
                        errno=0,
                    ) from None

                now = time.monotonic()
                if not hasattr(self, "_next_emit_at"):
                    self._next_emit_at = 0.0
                if now < self._next_emit_at:
                    continue
                self._next_emit_at = now + period

                img = await asyncio.to_thread(self._frame_to_bgr, frame)
                if img is None:
                    await asyncio.sleep(FRAME_DECODE_ERROR_SLEEP_SEC)
                    continue

                self._frame_id += 1
                await self._process_frame(img, cfg_now, cam_now)
        finally:
            for t in (feeder_task, reconnect_task):
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await feeder_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reconnect_task

    @staticmethod
    def _frame_to_bgr(frame: av.VideoFrame) -> NDArray[np.uint8] | None:
        """PyAV frame → BGR uint8 ndarray via OpenCV; None on decode error."""
        try:
            rgb = frame.to_ndarray(format="rgb24")
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            return bgr
        except (ValueError, av.BlockingIOError):  # pragma: no cover
            return None

    async def _process_frame(
        self,
        img: NDArray[np.uint8],
        cfg: HotConfig,
        cam,
    ) -> None:
        # --- Motion pre-filter (Frigate-style CPU gate) ---
        # Rebuild motion detector when its config params change so a
        # Tier-A hot-reload of motion.threshold / contour_area / etc
        # actually applies on the next frame. We compare a signature
        # tuple to avoid rebuilding on every frame (cheap, but the
        # previous frame has to be discarded on a real change).
        new_motion_sig = (
            cam.motion_enabled,
            cam.motion_threshold,
            cam.motion_contour_area,
            cam.motion_lightning_threshold,
        )
        if self._motion is None or self._motion_signature != new_motion_sig:
            self._motion = MotionDetector(
                enabled=cam.motion_enabled,
                threshold=cam.motion_threshold,
                contour_area=cam.motion_contour_area,
                lightning_threshold=cam.motion_lightning_threshold,
            )
            self._motion_signature = new_motion_sig

        # Same pattern for zones — rebuild when the polygon set changes.
        # We compare the full zone list (id + inertia + coordinates).
        new_zone_sig = tuple(
            (z.id, z.inertia, tuple(z.coordinates)) for z in cam.zones
        )
        if self._zone_filter is None or self._zone_signature != new_zone_sig:
            self._zone_filter = build_zone_filter(self._path, cam.zones)
            self._zone_signature = new_zone_sig

        # Apply static masks (cv2.fillPoly BLACK).
        masked_img = await asyncio.to_thread(
            self._motion.apply_mask, img, cam.masks
        )

        # Run motion detection on the masked frame. If unchanged, skip
        # the expensive Triton call entirely.
        motion_result = await asyncio.to_thread(
            self._motion.update, masked_img
        )
        if not motion_result.changed:
            # No motion → skip detect. We still publish a heartbeat
            # is omitted to keep MQTT quiet on static scenes.
            return

        # --- JPEG encode + infer ---
        jpeg_bytes = await asyncio.to_thread(
            cv2.imencode,
            ".jpg",
            masked_img,
            [int(cv2.IMWRITE_JPEG_QUALITY), cfg.snapshot_quality or JPEG_DEFAULT_QUALITY],
        )
        if not jpeg_bytes[0]:
            return
        jpeg = jpeg_bytes[1].tobytes()

        # Pick the Triton ensemble based on the camera's pipeline +
        # motion state. Pure stages (trigger=always) always run;
        # on_motion / on_high_conf / on_zone_enter stages are
        # pre-filtered client-side so we don't pay the GPU cost.
        ensemble_name = self._select_ensemble(cfg, cam, motion_result.changed)
        try:
            if cfg.detector_kind == "triton":
                result = await self._triton.infer_ensemble(ensemble_name, jpeg)
                raw_dets = result.detections
                image_hw = img.shape[:2]  # (h, w) from BGR ndarray
            else:
                resp = await self._http.post(
                    "/detect",
                    files={"image": ("frame.jpg", jpeg, "image/jpeg")},
                    data={"min_score": str(cam.min_score)},
                    timeout=HTTP_TIMEOUT_SEC,
                )
                resp.raise_for_status()
                body = resp.json()
                raw_dets = body.get("detections", [])
                image_hw = (img.shape[0], img.shape[1])
        except (httpx.HTTPError, InferenceError) as e:
            logger.warning(
                "detector_call_failed",
                detector_kind=cfg.detector_kind,
                ensemble=ensemble_name,
                error=str(e),
            )
            return

        # --- Filter by `cam.classes` + apply zone inertia ---
        # `raw_dets` is a list[dict] regardless of detector_kind: the
        # FastAPI shim returns JSON dicts, `TritonClient.infer_ensemble()`
        # returns dicts in the same shape (`{label, score, box}`). We
        # only differ on label type — Triton returns int class ids,
        # FastAPI returns strings (already mapped by the detector).
        h, w = image_hw
        kept: list[DetectionPayload] = []
        zone_hits: list[tuple[int, str]] = []  # (idx_in_kept, zone_id)
        for d in raw_dets:
            # Tolerate malformed detection dicts (e.g. FastAPI shim
            # omitted `box`, or a Triton postprocess bug emitted a
            # partial entry). Skip the bad entry instead of crashing
            # the whole frame — a crash here triggers a 5s stall +
            # RTSP reconnect via camera_loop_crashed.
            if not isinstance(d, dict):
                continue
            label = d.get("label")
            if label is None:
                continue
            if isinstance(label, int):
                # From Triton — map int back to COCO name.
                label = (
                    _COCO_NAMES[label]
                    if 0 <= label < len(_COCO_NAMES)
                    else f"object:{label}"
                )
            if label not in cam.classes:
                continue

            score = d.get("score")
            box = d.get("box")
            # Validate box shape: must be a 4-element iterable of numbers.
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            try:
                x1, y1, x2, y2 = box
            except (TypeError, ValueError):
                continue
            if score is None:
                continue
            if not all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
                continue
            if w <= 0 or h <= 0:
                continue
            # Triton emits pixel coords; normalise to [0, 1] here.
            nx1 = max(0.0, min(1.0, x1 / w))
            ny1 = max(0.0, min(1.0, y1 / h))
            nx2 = max(0.0, min(1.0, x2 / w))
            ny2 = max(0.0, min(1.0, y2 / h))
            # Zone filter (bottom-center test).
            zone_ids = self._zone_filter.apply((nx1, ny1, nx2, ny2))
            kept.append(
                DetectionPayload(
                    label=label,
                    score=score,
                    box=(nx1, ny1, nx2, ny2),
                    zone_ids=zone_ids,  # type: ignore[arg-type]
                )
            )
            for zid in zone_ids:
                zone_hits.append((len(kept) - 1, zid))

        if not kept:
            return

        # --- Upload snapshot + publish event ---
        event_uuid = uuid.uuid4().hex
        obj_key = snapshot_key(cam.tenant_id, event_uuid)
        try:
            size = await asyncio.to_thread(
                self._uploader.upload, object_key=obj_key, jpeg_bytes=jpeg
            )
        except SnapshotUploadError:
            logger.exception("snapshot_upload_failed", object_key=obj_key)
            return

        event = DetectionEvent(
            event_uuid=event_uuid,
            ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            node_id=cfg.node_id,
            tenant_id=cam.tenant_id,
            camera_id=cam.camera_id,
            mtx_path=self._full_mtx_path,
            frame_id=self._frame_id,
            detections=kept,
            snapshot_url=obj_key,
            snapshot_size=size,
        )
        self._publisher.publish(event.model_dump())
        logger.info(
            "detection_emitted",
            event_uuid=event_uuid,
            count=len(kept),
            zones=[z for _, z in zone_hits],
            path=self._full_mtx_path,
            frame_id=self._frame_id,
        )

    @staticmethod
    def _redact(url: str) -> str:
        if "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host_path = rest.rsplit("@", 1)
            return f"{scheme}://***@{host_path}"
        return url

    def _select_ensemble(self, cfg: HotConfig, cam, on_motion: bool) -> str:
        """Pick the Triton ensemble for this camera + trigger state.

        Maps `cam.pipeline` + the current trigger state to one of:
            detect_only, detect_segment, detect_depth, detect_segment_depth

        Trigger semantics:
        - `always`          → stage runs every frame (motion-gated)
        - `on_motion`       → only when motion detected this frame
        - `on_high_conf`    → always (worker filters inside)
        - `on_zone_enter`   → only when at least one detection entered a zone

        For v1 we keep this simple: any pipeline with multiple stages
        uses the full ensemble; the worker pre-filters by motion gate.
        Future: per-stage ensemble routing.
        """
        stages = {p.stage for p in cam.pipeline}
        has_segment = "segment" in stages
        has_depth = "depth" in stages
        if has_segment and has_depth:
            return "detect_segment_depth"
        if has_segment:
            return "detect_segment"
        if has_depth:
            return "detect_depth"
        return "detect_only"


async def run_all(store: HotConfigStore, *, reloader=None) -> None:
    """Spawn one `CameraLoop` per camera in the current HotConfig.

    If `reloader` is supplied, each spawned CameraLoop is attached via
    `reloader.attach(mtx_path, loop)` so the Tier-B `source_rtsp`
    reload path can call `loop.request_reconnect()`. Without this
    wiring, the reloader's `camera_loops` dict stays empty and Tier B
    RTSP URL changes silently never propagate (they only take effect
    on container restart).
    """
    cfg = store.get()
    http = httpx.AsyncClient(base_url=cfg.detector_url, timeout=HTTP_TIMEOUT_SEC)
    triton = TritonClient(base_url=cfg.detector_url)
    uploader = SnapshotUploader(
        endpoint=cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        bucket=cfg.minio_bucket,
        secure=cfg.minio_secure,
        region=cfg.minio_region or None,
    )
    publisher = MqttPublisher(
        broker=cfg.mqtt_broker,
        client_id=f"{cfg.mqtt_client_id}-{os.getpid()}-{secrets.token_hex(4)}",
        topic=cfg.mqtt_topic,
    )

    tasks: list[asyncio.Task] = []
    try:
        for cam_cfg in cfg.cameras:
            loop = CameraLoop(
                cam_cfg.mtx_path,
                store,
                http=http,
                uploader=uploader,
                publisher=publisher,
                triton=triton,
            )
            # Wire the loop into the reloader so Tier B (source_rtsp)
            # changes can signal reconnect via reloader._apply.
            if reloader is not None:
                reloader.attach(cam_cfg.mtx_path, loop)
            tasks.append(
                asyncio.create_task(loop.run(), name=f"cam-{cam_cfg.mtx_path}")
            )
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await http.aclose()
        publisher.close()


__all__: Iterable[str] = ("CameraLoop", "run_all")
