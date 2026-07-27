# Edge Agent

Python sidecar that runs on each GPU edge node. Two async tasks:

1. **Heartbeat** (every 30 s) — POST `nvidia-smi` + `psutil` stats to
   `portal /api/edge/heartbeat`. Reports `healthy=false` when NVML is
   unreachable (e.g. dev box without GPU).
2. **Config-pull + reconcile** (every 60 s) — GET
   `portal /api/edge/config?node_id=<id>&since=<vsn>`. When the
   `config_version` advances, render:
   * `docker-compose.worker.yml.j2` → one service per camera —
     rendered by Jinja2 in `reconcile.render_worker_compose`.
   * `mediamtx.paths.j2` → camera paths fragment MediaMTX mounts.

   Then run `docker compose up -d --remove-orphans` and
   `docker restart mediamtx` if the paths set changed.

## Why this exists

The portal holds the source of truth for `(camera, RTSP URL, tenant,
node)` rows. Edge workers and MediaMTX path configuration are derived
artifacts kept in sync by this agent. No direct DB access from the edge.

## Settings (env, prefix `VIREX_EDGE_`)

| Var | Default |
|---|---|
| `VIREX_EDGE_NODE_ID` | 1 |
| `VIREX_EDGE_PORTAL_URL` | `http://127.0.0.1:8000` |
| `VIREX_EDGE_PORTAL_BEARER` | `virex-edge-shared-secret` |
| `VIREX_EDGE_HEARTBEAT_PERIOD_SEC` | 30 |
| `VIREX_EDGE_CONFIG_PULL_PERIOD_SEC` | 60 |
| `VIREX_EDGE_EDGE_COMPOSE_PATH` | `/etc/virex/docker-compose.worker.yml` |
| `VIREX_EDGE_MEDIAMTX_PATHS_FRAGMENT` | `/etc/virex/mediamtx.paths.d/paths.yaml` |

## Run

```bash
python -m src.main
# OR install via deploy/edge-agent.service (systemd) so it survives reboot.
```

## See also

- `templates/docker-compose.worker.yml.j2`
- `templates/mediamtx.paths.j2`
- Implementation plan §3 File Layout (edge-agent) and §7 Phase F.