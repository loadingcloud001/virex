# Config Hot-Reload

This document describes how Virex applies config changes without restarting
the AI detection pipeline.

## Tier Model

| Tier | Blip | Mechanism |
|---|---|---|
| **A** — AI parameters | **0 s** | Atomic `HotConfig` swap inside worker; reads happen per frame |
| **B** — RTSP URL/MQTT/COS | **~3 s** | Worker reconnects affected channel |
| **C** — MediaMTX `record` / FFmpeg params | **~5 s** | `docker compose -f transcoder up -d --force-recreate` or `docker restart mediamtx` |
| **D** — Add/remove camera | **~10–30 s** | Full reconcile (compose up + mediamtx restart) |

Tier-A fields (apply instantly without restarting any container):
- `detect.fps`
- `detect.min_score`
- `detect.classes`
- `detect.roi`
- `snapshot_quality`
- `motion.enabled`, `motion.threshold`, `motion.contour_area`, `motion.lightning_threshold`
- `masks` (static polygons where motion + detect are disabled)
- `zones` (per-camera polygon regions with inertia)
- `pipeline` (per-camera list of stages: `[detect, segment, depth]` etc.)

Tier-B fields (~3 s reconnect):
- `source_rtsp` (per camera)
- `mqtt_broker`, `mqtt_topic`
- `minio_endpoint`, `minio_access_key`, `minio_secret_key`, `minio_bucket`, `minio_region`
- `detector_url`, `detector_kind` (Triton vs FastAPI fallback)

Tier-C fields (~5 s):
- `record: true/false` (per camera → MediaMTX restart)
- FFmpeg transcoder bitrate / GOP / preset (only if those become editable via portal; currently rendered at reconcile time)

Tier-D operations:
- Add new camera → new worker + new transcoder + new MediaMTX paths
- Remove camera → `--remove-orphans` tears down worker + transcoder + paths

See `docs/ai-pipeline.md` for the full stage-based pipeline architecture
(AGPL-3.0 Ultralytics YOLO + Triton ensembles).

## Architecture

```
  Web portal                      Edge-agent                     Worker
  ┌──────────┐  POST /api/edge/    ┌──────────────┐               ┌──────────┐
  │ form UI  │ ──────config───────►│ file watcher │               │ per-cam  │
  │          │                     │ (inotify /   │   POST        │ process  │
  │ save     │                     │  polling)    │  /admin/reload│          │
  └──────────┘                     └──────┬───────┘ ─────────────►└────┬─────┘
                                         │                            │
                                         │ diff against baseline       │
                                         │ classify into tier A/B/C/D  │
                                         │ apply per tier              │
                                         │   A/B → worker admin        │
                                         │   C   → docker restart      │
                                         │   D   → docker compose up   │
                                         │                            │
                                         └──── polling fallback ──────┘
```

## Components

### 1. Edge-agent — file watcher

**File**: `edge-agent/src/config_watcher.py`

Two implementations:
- `InotifyConfigWatcher` — uses `watchdog.observers.Observer` with Linux inotify
- `PollingConfigWatcher` — fallback for Docker bind mounts where inotify
  events don't propagate from host → container; checks mtime + content
  hash every 1 second

The watcher reads the file, parses to an `EdgeConfigBundle`, diffs against
the cached baseline (kept in module-level `_last_applied` so multiple
watcher instances share state), and calls the tier-aware handler on each
real change.

`make_config_watcher()` returns the inotify backend if watchdog imports,
else the polling backend. For v1 pilot we **force polling** because the
worker /etc/virex bind mount doesn't propagate inotify events; this is
called out in `main.py`.

### 2. Tier classifier

**File**: `edge-agent/src/tier_classifier.py`

Pure function: `classify(old: EdgeConfigBundle | None, new: EdgeConfigBundle) -> TierReport`.

Returns a `TierReport` whose fields cover the four tiers. Tested with
13 unit tests in `tests/test_tier_classifier.py`.

### 3. Tier-aware apply

**File**: `edge-agent/src/reconcile.py:175`

`apply_tier_report(bundle, report, worker_admin_port=32000)` decides what
to do based on which tier fields are populated:

| Tier field | Action |
|---|---|
| `tier_a_per_camera` or `tier_b_per_camera` | `POST http://127.0.0.1:<port>/admin/reload` on the affected worker |
| `tier_c_paths` | Re-render `mediamtx.yml`, `docker restart mediamtx` |
| `added`/`removed` | `run_reconcile(bundle)` (full compose up + mediamtx restart) |

### 4. Worker — atomic config store

**File**: `ai-backend/worker/config_hot.py`

`HotConfig` is an immutable dataclass. `HotConfigStore` exposes `get()`
(lock-free pointer copy) and `set()` (atomic swap under a brief Lock).

`CameraLoop` no longer takes an immutable `CameraConfig`. Instead it holds
a `HotConfigStore` reference and reads `self._hot.get()` per frame:

```python
async def _loop_once(self) -> None:
    cfg = self._store.get()
    cam = self._path_cfg(cfg)
    period = 1.0 / max(1, cam.fps)  # Tier A — fps read fresh
    ...
    await self._http.post("/detect",
        data={"min_score": str(cam.min_score)})  # Tier A
    kept = [p for p in persons if p.get("label") in cam.classes]  # Tier A
    ...
```

A new sentinel from the reloader sets `_reconnect_event`, which forces the
PyAV container to close and reopen on the next iteration — used for Tier B
(`source_rtsp` change).

### 5. Worker — admin HTTP server

**File**: `ai-backend/worker/admin.py` + `main.py`

Each worker container starts a tiny FastAPI uvicorn server on
`WORKER_ADMIN_PORT` (default 32000 + camera index) bound to `127.0.0.1`
on the host network. Endpoints:

```
GET  /admin/healthz    liveness, returns HotConfigStore.version
GET  /admin/config     sanitised dump (redacts secrets)
POST /admin/reload     force re-read of workers.yaml, apply Tier A/B
POST /admin/rollback   revert to previous HotConfig (one-deep snapshot)
```

`POST /admin/reload` calls `Reloader.apply_now()`, which reads the YAML
fresh, runs `diff_configs()`, atomically swaps the store, and returns
the diff report. If only Tier-A fields changed the report is empty and
no reconnect happens.

### 6. Worker — config reloader

**File**: `ai-backend/worker/config_reloader.py`

Spawned as an asyncio task in `worker/main.py`. Polls `workers.yaml`
every 5 seconds (configurable). Uses **mtime + content hash** to detect
changes — defends against Docker bind-mount kernel page-cache staleness
where `stat()` returns old mtime but `open()` reads fresh content.

On a Tier B change (`source_rtsp`), the reloader calls
`CameraLoop.request_reconnect()` which sets an asyncio Event; the loop
breaks out of `_loop_once` and re-reads the URL from `HotConfig`.

## Test Coverage

`ai-backend/tests/test_config_reloader.py` — 16 tests covering:
- `HotConfig` immutability & atomic swap
- `diff_configs` for all 4 tiers
- `Reloader.poll_once` mtime / content-hash / error-handling paths
- `rollback()` returns previous snapshot

`ai-backend/tests/test_admin.py` — 5 tests covering:
- `/admin/healthz` liveness
- `/admin/config` redaction
- `/admin/reload` Tier-A and Tier-B paths
- Operator typo → 200 with `error` field, no swap

`edge-agent/tests/test_tier_classifier.py` — 13 tests covering:
- First reconcile marks everything as Tier D
- Each tier field detection (fps, min_score, classes, roi, source_rtsp, record)
- Add/remove camera detection
- No-change diff returns empty report

`edge-agent/tests/test_config_watcher.py` — 7 tests covering:
- Polling baseline establishment
- Polling fires on file change
- Polling survives invalid YAML
- Polling doesn't fire on touch-only-with-identical-content
- `_last_applied` cache round-trip
- `make_config_watcher` factory

Total: **159 unit tests across 4 modules** (118 ai-backend + 29 edge-agent + 12 event-router), all passing.

## Known Limitations

### Docker bind-mount page cache staleness

On Linux, when you `cat state/workers.yaml` from inside a container, the
kernel page cache can show a slightly stale version of the file even
after the host kernel has updated the inode. Symptoms:
- Host `stat state/workers.yaml` shows new mtime
- Container `stat /etc/virex/workers.yaml` shows OLD mtime (cache-stale)
- Container `cat /etc/virex/workers.yaml` DOES show new content (Python `open()` reads through page cache but converts/invalidates it)

The polling watcher uses `mtime + sha256(content)` to defeat this.
Even so, the WORKER-side `Reloader.poll_once()` may miss changes if the
worker's bind mount hasn't invalidated the cache after a host edit.

**Workaround for v1 pilot**: when you want a guaranteed hot-reload
across all workers, recreate the affected worker container:

```bash
docker compose -f state/docker-compose.worker.yml up -d \
  --force-recreate worker-hc202502cam04
```

This is a Docker/platform issue, not a Virex code issue. Phase 2 (with
portal) will address it via direct file write into the container using
`docker cp` or by using `:cached` mount propagation.

### Per-worker admin port

Each worker gets a unique admin port (32000, 32001, 32002, ...).
Edge-agent's `worker_admin_port_for_path(mtx_path)` looks up the
rendered compose file to find the right port. If `docker-compose up` is
run outside of edge-agent (manual operation), the port mapping may
fall back to the default 32000, causing a port collision. The remedy
is to always use edge-agent to render the worker-compose.

## Operational Recipe

**Add a camera**:
1. Edit `state/workers.yaml` — append to `cameras:` list
2. Within ~1 s (poll cycle) edge-agent detects the addition
3. Tier D applies: full reconcile — new worker + new transcoder come up
4. MediaMTX restart (~5 s blip on all paths)

**Change fps / min_score**:
1. Edit `state/workers.yaml` — change `fps:` or `min_score:` value
2. Edge-agent detects, classifies as Tier A
3. Edge-agent calls `POST /admin/reload` on the affected worker(s)
4. Workers atomically swap HotConfig; next frame uses new value
5. **Total time**: ~1–2 s; zero downtime on detection

**Change RTSP URL**:
1. Edit `state/workers.yaml` — change `source_rtsp:` value
2. Edge-agent classifies as Tier B
3. Edge-agent calls worker's `/admin/reload`; reloader sets reconnect
4. Worker closes PyAV container, reopens with new URL (~3 s interruption)

**Toggle recording**:
1. Edit `state/workers.yaml` — change `record: true|false`
2. Edge-agent classifies as Tier C
3. Edge-agent re-renders `mediamtx.yml`, calls `docker restart mediamtx`
4. All paths blip ~5 s; recording state updates

## Verification

```bash
# Force-reload a specific worker (instant Tier A)
curl -X POST http://127.0.0.1:32000/admin/reload

# View current HotConfig (redacts secrets)
curl http://127.0.0.1:32000/admin/config | python3 -m json.tool

# Roll back to last-known-good (one-deep snapshot)
curl -X POST http://127.0.0.1:32000/admin/rollback

# Tail edge-agent apply logs (real-time)
docker logs -f virex-edge-agent 2>&1 | grep -E 'apply_tier_report|worker_reload'
```
