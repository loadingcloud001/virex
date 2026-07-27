# Virex

> **Sustainable AI live-stream platform** — NVR is one use case.
>
> Maintained by **Loading Cloud**. Released under **AGPL-3.0**.

Virex is a four-module open-source stack for ingesting RTSP / WebRTC camera streams,
running per-camera AI pipelines (motion + zone + detect + segment + depth) on the
edge, and forwarding structured events to any backend over MQTT. The first
production deployment is multi-tenant video surveillance, but the same modules
serve any AI-on-live-video use case (construction safety, retail queue depth,
parking occupancy, manufacturing line monitoring, etc.).

The earlier "Frigate + go2rtc + MIT-licensed multi-tenant SaaS" framing that
appears elsewhere in this repo is **historical / superseded**. The current
v1 pilot is AGPL-3.0, built on top of [MediaMTX](https://github.com/bluenviron/mediamtx)
and [Ultralytics YOLO](https://github.com/ultralytics/ultralytics), with custom
per-camera worker containers orchestrated by a hot-reload edge agent.

---

## What This Repo Is

A working end-to-end v1 pilot deployed on an RTX 4070 edge node + a Hetzner VPS,
with the following four composable modules under a shared AGPL-3.0 umbrella:

| Module | Role | Container image |
|---|---|---|
| `ai-backend/` | Per-camera AI pipeline (motion + zone + Triton/FastAPI detect + segment + depth) + clip-builder. One container per camera, hot-reloadable. | `virex-ai-backend:latest` |
| `edge-agent/` | Reads `workers.yaml` (or a portal-supplied config), classifies diffs into Tier A/B/C/D reloads, renders MediaMTX / worker / transcoder compose files, restarts the affected containers in the right order. | `virex-edge-agent:latest` |
| `event-router/` | MQTT subscriber that fans detection events into per-tenant notification sinks (Telegram / Email / Webhook). | `virex-event-router:latest` |
| `portal/` | FastAPI + Jinja2 SaaS control plane for tenant / camera / rule management. Phase 2 UI; the API surface is shipping, the operator UI is incrementally built. | `virex-portal:latest` |
| `detector/` (inside `ai-backend/`) | Standalone FastAPI wrapper around Ultralytics YOLOv8 — used as a dev-mode detector when Triton is not running. | `virex-detector:latest` |

The edge stack (`deploy/edge/`) ships a one-shot `up.sh` bootstrapper that
materialises `state/mediamtx.yml`, `state/workers.yaml`, and the per-worker
compose files from `*.example` templates plus a local `.env`.

---

## Current Status (2026-07-27)

**Phase**: v1 pilot — three cameras live on one RTX 4070 edge node.

| Track | Status |
|---|---|
| MediaMTX ingest (RTSP + transcoder → H.264 repack) | ✅ Live, 4 paths |
| Per-camera worker (motion → zone → detect) | ✅ Live, hot-reloadable |
| Clip-builder (FFmpeg segment cutter → MinIO/S3) | ✅ Live |
| MQTT event emission | ✅ Live, Mosquitto on edge node |
| Event-router → notifications | ✅ Live, webhook sink configured |
| Portal control plane (API) | ✅ API shipping, UI Phase 2 |
| AGPL-3.0 license + source-offer docs | ✅ Shipped |
| `virex-snapshots-1308927282.cos.ap-singapore.myqcloud.com` | ✅ Bound via Cloudflare Origin CA |
| Code review 2026-07-27 fixes (9 bugs, 6 Critical/High) | ✅ Live |

See `docs/` for module-level architecture details, hot-reload contract,
and the MediaMTX path layout.

---

## Quick Start (Developer)

```bash
# 1. Clone
git clone https://github.com/loadingcloud001/virex.git
cd virex

# 2. Local dev — each module is a standalone uv project
cd ai-backend && uv sync && uv run pytest       # 118 tests
cd ../edge-agent && uv sync && uv run pytest   #  29 tests
cd ../event-router && uv run pytest            #  12 tests
cd ../portal      && uv run pytest             # phase-2 API tests
```

## Quick Start (Edge Pilot)

```bash
cd deploy/edge

# 1. Bootstrap config
cp .env.example .env
# edit .env: MQTT_BROKER, MINIO_ENDPOINT, MINIO_ACCESS_KEY,
#           MINIO_SECRET_KEY, MINIO_BUCKET, MINIO_REGION
cp workers.yaml.example workers.yaml
# edit cameras: mtx_path, source_rtsp, detect.classes, motion.*, zones, masks

# 2. Build + bring up
./up.sh

# 3. Verify
curl -s http://127.0.0.1:19997/v3/paths/list | jq '.items[] | {name: .confName, ready}'
curl -s http://127.0.0.1:32000/admin/healthz    # any per-worker admin port
```

The first `up.sh` run builds all five images (detector, ai-backend, event-router,
portal, edge-agent) and brings up MediaMTX + Mosquitto + edge-agent + per-camera
worker containers + transcoder sidecars. The edge-agent then watches
`workers.yaml` and applies changes without restarting MediaMTX (Tier A) or with
a per-worker restart (Tier B/C/D).

---

## Repository Layout

```
virex/
├── README.md                 # you are here
├── LICENSE                   # AGPL-3.0 full text
├── SOURCE_OFFER.md           # AGPL §13 written-offer contact path
├── THIRD_PARTY_NOTICES.md    # YOLOv8, MediaMTX, etc. license attribution
│
├── ai-backend/               # worker + clip_builder + detector + ONNX export
├── edge-agent/               # hot-reload orchestrator + Jinja2 templates
├── event-router/             # MQTT → sink fan-out
├── portal/                   # FastAPI + Jinja2 SaaS control plane
│
├── deploy/
│   ├── edge/                 # docker-compose.yml + up.sh + workers.yaml.example
│   ├── mediamtx/             # static MediaMTX config + recording layout
│   └── vps/                  # control-plane compose (TBD — Phase 2)
│
├── docs/                     # module architecture, hot-reload contract,
│                             # MediaMTX layout, ai-pipeline details
└── scripts/                  # ops utilities (env validation, key rotation, …)
```

---

## Hot-Reload Contract (Tier A / B / C / D)

The edge-agent classifies any `workers.yaml` diff into one of four tiers and
applies the cheapest fix that keeps the system consistent:

| Tier | What changed | Action | Downtime |
|---|---|---|---|
| **A** | `detect.*`, `motion.*`, `masks`, `zones`, `pipeline`, `minio_*` (metadata), `snapshot_quality` | HotConfig atomic swap inside the affected worker | 0s (signature-aware rebuild of motion + zone + detector singletons) |
| **B** | `source_rtsp`, `mqtt_*`, `minio_endpoint` | Reconnect RTSP feeder / MQTT client / MinIO uploader | ~3s |
| **C** | `layer_suffix`, MediaMTX path layout | Restart MediaMTX container | ~5s |
| **D** | Anything else / schema change | Full reconcile: re-render compose + restart everything | ~10–30s |

See `docs/config-hot-reload.md` for the full diff-to-tier mapping and the
tier-classifier implementation.

---

## Tech Stack (Edge)

**Edge runtime**
- MediaMTX (RTSP / WebRTC / HLS relay + per-segment MP4 recording)
- Ultralytics YOLOv8m (Apache-2.0-compatible code path, AGPL-3.0 model weights)
- Triton Inference Server (optional — KServe HTTP/REST client)
- FFmpeg transcoder sidecars (repack camera-native streams into H.264)
- Mosquitto MQTT broker (per-edge, isolated)

**Application**
- Python 3.11+ with `uv` workspace layout
- FastAPI (detector, clip-builder, worker admin, portal API)
- pydantic v2 + pydantic-settings (env loading)
- structlog (structured JSON logging)
- watchdog (inotify on `workers.yaml`)
- httpx (Triton / portal HTTP)
- av (PyAV — RTSP frame extraction)
- minio (S3-compatible client)

**Control plane (VPS)**
- FastAPI + Jinja2 + Tailwind
- PostgreSQL 16
- Mosquitto MQTT
- MinIO (S3-compatible event / clip storage)
- Cloudflare Origin CA in front of the snapshot CDN

**AI model stack** (all commercially compatible, no AGPL-3.0 contamination beyond
the YOLOv8 weights themselves which are documented under `THIRD_PARTY_NOTICES.md`):
- Detection: RT-DETR or Ultralytics YOLO
- PPE / attributes: CLIP zero-shot or Florence-2
- Segmentation: SAM2
- Depth: Depth Anything V2 Small
- Pose: ViTPose
- OCR: PaddleOCR

---

## License

**AGPL-3.0** — see [`LICENSE`](LICENSE) for full text.

If you deploy a modified version over a network, AGPL §13 requires that you
either provide the source to your users or honor the written-offer mechanism
documented in [`SOURCE_OFFER.md`](SOURCE_OFFER.md).

Third-party model / library attributions live in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

---

## Security

If you find a vulnerability, please **do not** file a public issue. Contact the
maintainers directly — see git history for the current security contact.

The v1 pilot currently does not include a hardened production posture — TLS
termination is via Cloudflare Origin CA, the edge node is not behind a VPN,
and secrets live in `.env` files (gitignored). Production hardening is on the
roadmap; do not deploy this as-is to a multi-tenant public network.

---

## About Loading Cloud

**Loading Cloud** is a Hong Kong-based technology company building sustainable
open-source AI infrastructure for B2B customers. Virex is its flagship edge-AI
platform; the per-camera worker pattern and the tiered hot-reload design are
intended to be reusable across any live-stream AI use case.

---

**Last Updated**: 2026-07-27
**Phase**: v1 pilot (AGPL-3.0)
**Status**: 3 cameras live on RTX 4070 edge node, 159 tests passing