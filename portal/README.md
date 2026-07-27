# Virex Portal (Control Plane)

FastAPI-based multi-tenant web portal for Virex. Currently implements:

* `/api/edge/config` — JSON bundle consumed by edge-agent every 60 s.
* `/api/edge/heartbeat` — pynvml + psutil stats updater.
* `/internal/events/{event_id}/clip` — clip-builder PATCHes the event row.
* `/healthz` — liveness probe.

The full REST surface (`/api/auth/*`, `/api/cameras`, `/api/events`, …)
lives in the broader Phase-1 plan; the endpoints above are the minimum
required to wire the BentoML → worker → event-router → clip-builder loop.

## Why `mtx_path` not `frigate_name`

`cameras.mtx_path` is the single string `t{tenant_id}c{camera_id}` that:
- Becomes a MediaMTX path name (`<mtx_path>h264`, `<mtx_path>raw`).
- Is the MQTT routing key — workers emit it directly.
- Has no underscores — MediaMTX treats underscores in path names as
  nesting, which is a footgun the old `t1_c5` form hit.

Phase 1 implementation is the Apache-2.0 + BentoML plan at
`~/.local/share/kilo/plans/1784975160518-bentoml-ai-edge-pipeline.md`.
See that file for architectural context.

## Settings (env, prefix `VIREX_`)

| Var | Default |
|---|---|
| `VIREX_DATABASE_URL` | `postgresql+asyncpg://virex:virex@127.0.0.1:5432/virex` |
| `VIREX_REDIS_URL` | `redis://default@127.0.0.1:6379/0` |
| `VIREX_MINIO_ENDPOINT` | `minio:9000` |
| `VIREX_MINIO_ACCESS_KEY` | `minioadmin` |
| `VIREX_MINIO_SECRET_KEY` | `minioadmin` |
| `VIREX_MINIO_BUCKET` | `virex` |
| `VIREX_MINIO_SECURE` | `false` |
| `VIREX_EDGE_BEARER` | `virex-edge-shared-secret` |

## Run

```bash
pip install -e .[dev]
uvicorn app:app --reload --port 8000

# Tests
pytest tests/ -v
ruff check core/ models/ schemas/ api/ app.py
```

## See also

- `../ai-backend/` — worker + clip-builder (this portal receives events and PATCHes).
- `../edge-agent/` — pulls `/api/edge/config` and posts `/api/edge/heartbeat`.
- `../event-router/` — POSTs to `/internal/events/{id}/clip` from the VPS side.