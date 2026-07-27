# MediaMTX Standalone Deployment

Two-layer MediaMTX + FFmpeg transcoder sidecars for Hikvision-style RTSP
cameras. All camera streams (H.264 or H.265) are normalized to H.264
on a second layer so downstream consumers (browsers, AI, recorders)
always see one format.

## Architecture

```
Camera (H.264 or H.265 RTSP)
        │
        ▼
   MediaMTX path Xraw   ← passthrough, original codec
        │
        │ (consumed by)
        ▼
   FFmpeg sidecar container   ← transcodes to H.264 High Profile
        │
        ▼
   MediaMTX path Xh264   ← unified H.264, recorded
        │
        ▼
   Consumers (browser, AI, recorder)
```

| Path | Codec | Source | Recording |
|---|---|---|---|
| `he202504cam04raw` | H.264 (camera native) | camera RTSP | no |
| `he202504cam04h264` | H.264 (transcoded) | FFmpeg sidecar | yes |
| `hc202502cam04raw` | H.265 (camera native) | camera RTSP | no |
| `hc202502cam04h264` | H.264 (transcoded) | FFmpeg sidecar | yes |

Consumers should use the `*h264` paths. Raw paths are intermediate.

## First-time setup

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env and set RTSP_PASSWORD_1 and RTSP_PASSWORD_2

# 3. Pull images and start
docker compose pull
docker compose up -d
```

## Verification

**Note**: Host ports are shifted by +10000 to avoid conflict with the
existing `frigate` container which already binds 8554/8888/8889 on the host.
Internally MediaMTX still uses the default ports; only the host mapping
is offset.

```bash
# Container status — expect 3 containers: mediamtx + 2 transcoders
docker ps | grep -E "mediamtx|transcoder"

# Control API — should list all 4 paths with ready: true
curl -s http://localhost:19997/v3/paths/list | python3 -m json.tool

# RTSP playback via ffprobe — _h264 paths are always H.264
ffprobe -v error -show_streams rtsp://localhost:18554/he202504cam04h264
ffprobe -v error -show_streams rtsp://localhost:18554/hc202502cam04h264

# Raw paths show original camera codec (may be H.265)
ffprobe -v error -show_streams rtsp://localhost:18554/hc202502cam04raw

# WebRTC (open in Chrome/Firefox — browser will display stream directly)
open http://localhost:18889/he202504cam04h264

# Prometheus metrics
curl -s http://localhost:19998/metrics | grep "^paths"

# Recordings (H.264 only, in *_h264/ subdirs)
ls -lh recordings/*h264/
```

## Operations

```bash
# Tail logs
docker compose logs -f mediamtx
docker compose logs -f transcoder-he202504cam04
docker compose logs -f transcoder-hc202502cam04

# Restart
docker compose restart

# Stop (keeps recordings)
docker compose down

# Full removal (deletes containers AND recordings)
docker compose down -v
rm -rf recordings/
```

## Operations

```bash
# Tail logs
docker compose logs -f mediamtx

# Restart
docker compose restart mediamtx

# Stop (keeps recordings)
docker compose down

# Full removal (deletes container AND recordings)
docker compose down -v
rm -rf recordings/
```

## Configuration

- `mediamtx.yml` — MediaMTX config (committed; no credentials)
- `docker-compose.yml` — Compose service definition (committed)
- `.env` — Camera passwords (NOT committed; gitignored)
- `recordings/` — Disk recordings (NOT committed; gitignored)
- `.env.example` — Template for `.env` (committed)
- `.gitignore` — Lists files to exclude from version control (committed)

Source URLs are composed at container start: passwords come from `.env`,
URL structure comes from `docker-compose.yml`. Neither committed file
contains actual passwords.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits immediately | `mediamtx.yml` syntax error — check `docker compose logs mediamtx` |
| Path `"ready": false` | Camera unreachable from container — `docker exec mediamtx ping 218.189.218.174` |
| 401 Unauthorized from camera | Wrong password in `.env` |
| Container `unhealthy` | API not reachable — verify `api: yes` and port mapping |
| Recordings empty after 1 h | `recordSegmentDuration: 1h` — wait or lower to 1m for testing |

## Related docs

- `/home/loadingcloud001/virex/docs/mediamtx-architecture.md` — full architecture reference
- MediaMTX official docs: https://mediamtx.org/docs/features/configuration