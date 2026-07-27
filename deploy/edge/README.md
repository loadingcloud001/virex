# Virex deploy/edge — AI edge node stack

Single-host docker-compose orchestrating MediaMTX (recorder),
FFmpeg transcoders, BentoML detector, per-camera workers, clip-builder,
and edge-agent. Host networking is used end-to-end so WebRTC ICE UDP
works directly (no Docker-NAT UDP hole-punching).

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Edge stack orchestration. |
| `.env.example` | Camera credentials + VPS endpoints (Tailscale). |
| `workers.yaml.example` | Worker config — copied to `etc/virex/workers.yaml` on the host. |
| `../mediamtx/mediamtx.yml` | Streaming gateway config (shared with the legacy pilot deploy). |
| `../mediamtx/recordings/` | 30s-sealed fMP4 segments clip-builder reads from. |

## Bring-up

```bash
cd deploy/edge
cp .env.example .env                 # then fill the secrets
cp workers.yaml.example workers.yaml
docker compose --env-file .env up -d
```

## Smoke test (end-to-end)

1. Confirm all containers are `Up`:
   `docker compose ps`
2. Confirm MediaMTX has the `_h264` paths ready:
   `curl -s http://localhost:19997/v3/paths/list | jq '.items[].confName'`
3. Walk in front of a camera — within ~5 s the VPS Mosquitto should
   see `virex/detections`; the portal should have a new `events` row.
4. Within ~40 s more, the clip-builder should have uploaded a 10 s MP4
   to MinIO and PATCHed the event row with `clip_built=TRUE`.

## Reconciliation

When a tenant admin adds / removes a camera via the portal, the
edge-agent's 60s `config-pull` task fetches the fresh bundle and
reconciles in under ~90 s:

* Renders `worker-<mtx_path>` services into `docker-compose.worker.yml`.
* Re-renders MediaMTX `paths:` fragment (mounts the new camera path).
* Runs `docker compose up -d --remove-orphans` + `docker restart mediamtx`.

See `edge-agent/src/reconcile.py` for the implementation.

## License reminder

The full edge image stack stays Apache-2.0 — no AGPL components. See
`ai-backend/README.md` for the component-by-component breakdown.