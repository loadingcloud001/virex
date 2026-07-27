# AI Backend

Virex AI edge stack — three components, all in one image:

| Component | Role | Entrypoint |
|---|---|---|
| `detector/` | FastAPI shim wrapping **Ultralytics YOLOv8m** (AGPL-3.0 weights) for local dev and `curl` smoke tests. Production uses Triton instead. | `uvicorn detector.app:VirexDetectorApp --port 31001` |
| `worker/` | One asyncio task per camera: PyAV → motion gate → masks → Triton KServe `/v2/models/<ens>/infer` (or FastAPI fallback) → zone filter → MinIO snapshot → MQTT `virex/detections`. | `python -m worker.main --config workers.yaml` |
| `clip_builder/` | MQTT subscriber on `virex/events_created`; cuts 10 s clips via `ffmpeg -c copy` and uploads to MinIO + PATCHes the portal event row. | `python -m clip_builder.main` |

## License

Virex is **AGPL-3.0** (see repo root `LICENSE` + `SOURCE_OFFER.md`). All
model weights and SDKs used are compatible:

| Dependency | License | Used for |
|---|---|---|
| Ultralytics SDK (`ultralytics`) | AGPL-3.0 (SDK code) | YOLOv8m / YOLOv11 / YOLOv26 loading |
| Ultralytics YOLOv8m weights | AGPL-3.0 | Object detection |
| Ultralytics SAM2 wrapper | Apache-2.0 | Segmentation prompt encoder |
| facebookresearch SAM2 weights | Apache-2.0 | `sam2_hiera_base_plus` |
| DepthAnything V2 | MIT | Per-frame depth map |
| NVIDIA Triton Inference Server | BSD-3 | Multi-model ensemble server |
| MediaMTX | MIT | RTSP/HLS/WebRTC gateway |
| FFmpeg (`jrottenberg/ffmpeg`) | LGPL-2.1+ / GPL-2 | Transcoding sidecars |

The Ultralytics YOLO weights being AGPL-3.0 was the design decision that
made Virex adopt AGPL-3.0 overall. Public on GitHub + AGPL-3.0 is the
right combination — see `THIRD_PARTY_NOTICES.md` for the full list.

## Run

```bash
# Detector (dev only; production uses Triton):
uvicorn detector.app:VirexDetectorApp --host 0.0.0.0 --port 31001

# Worker (one per camera):
python -m worker.main --config deploy/edge/workers.yaml

# Clip-builder (one per edge node):
python -m clip_builder.main  # uses env vars — see deploy/edge/.env.example

# ONNX export (run once at image build time, then Triton loads ONNX):
python -m detector.onnx_export --out-root /opt/virex/triton/model_repository
```

## Tests

```bash
uv pip install -e .[dev]
pytest tests/ -v
ruff check detector/ worker/
mypy detector/ worker/
```

## See also

- `../docs/mediamtx-architecture.md` — streaming substrate.
- `../docs/ai-pipeline.md` — Ultralytics + Triton ensemble pipeline.
- `../docs/config-hot-reload.md` — Tier A/B/C/D hot-reload semantics.
- `../event-router/` — MQTT → DB → n8n fan-out.
- `../edge-agent/` — periodic reconcile against portal DB.
- The implementation plan: `~/.local/share/kilo/plans/1784975160518-pipeline-and-motion-plan.md`.

## Storage backend (MinIO / COS / S3 / R2)

The `minio` Python SDK is S3-compatible, so the worker + clip-builder
work against any of these backends without code changes — just set
`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`,
`MINIO_BUCKET` in `deploy/edge/.env`:

| Backend | Endpoint format |
|---|---|
| Self-hosted MinIO | `host:port` (no scheme) |
| Tencent Cloud COS | `cos.<region>.myqcloud.com` |
| AWS S3 | `s3.<region>.amazonaws.com` |
| Cloudflare R2 | `<account>.r2.cloudflarestorage.com` |
| Backblaze B2 | `s3.<region>.backblazeb2.com` |
| Aliyun OSS | `oss-<region>.aliyuncs.com` |

`MINIO_SECURE` must be `true` for cloud endpoints (HTTPS), `false` only
for internal plain-HTTP MinIO on Tailscale. `MINIO_REGION` is required
by COS / R2 / B2 to sign the request to the correct regional
endpoint.