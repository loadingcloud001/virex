#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# One-shot deploy of the Virex edge stack.
#
# Usage:
#   cp .env.example .env            # fill in MQTT_BROKER, MINIO_*, PORTAL_*
#   cp workers.yaml.example workers.yaml  # only if a custom config is needed
#   ./up.sh                         # build images + bring up containers
#
# Single source of truth for secrets:
#   `deploy/edge/.env` — read by both the parent docker compose
#   (`--env-file .env`) AND by per-worker containers (symlinked from
#   `state/.env`, see `reconcile.ensure_state_symlinks`).
#   NEVER duplicate secrets into `workers.yaml` — that file uses
#   `${VAR}` placeholders that `worker._yaml.expand_env()` resolves at
#   load time. Rotating a key means editing ONE file (`deploy/edge/.env`)
#   and restarting the affected workers.
#
# The script is idempotent: re-running it re-creates worker containers
# via `docker compose up -d --remove-orphans`. To restart from scratch,
# pass `--rebuild` to also re-build images, or `--fresh` to also wipe
# /etc/virex.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REBUILD=0
FRESH=0
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=1 ;;
        --fresh) FRESH=1; REBUILD=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# 1. Bootstrap .env from example if missing. .env is the ONLY place
#    secrets live; do not duplicate them into workers.yaml.
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "==> wrote .env from .env.example (fill in MQTT/MinIO/portal secrets)"
    echo "==> this is the ONLY file you need to edit for credentials"
fi
if [[ ! -f workers.yaml ]]; then
    cp workers.yaml.example workers.yaml
    echo "==> wrote workers.yaml from workers.yaml.example"
    echo "==> edit cameras (mtx_path, source_rtsp, detect.*, motion.*, zones)"
fi

# 2. Ensure the host-mounted directories exist.
mkdir -p /home/loadingcloud001/virex/deploy/edge/state
mkdir -p /home/loadingcloud001/virex/deploy/mediamtx/recordings

# 2a. Bootstrap mediamtx.yml from the static file if missing — edge-agent
#     will take over and re-render it on first reconcile, but mediamtx
#     needs a starting config to come up.
if [[ ! -f /home/loadingcloud001/virex/deploy/edge/state/mediamtx.yml ]]; then
    cp /home/loadingcloud001/virex/deploy/mediamtx/mediamtx.yml \
       /home/loadingcloud001/virex/deploy/edge/state/mediamtx.yml
    echo "==> bootstrapped state/mediamtx.yml from deploy/mediamtx/mediamtx.yml"
fi

# 3. Build images unless --rebuild skipped.
if (( REBUILD )) || ! docker image inspect virex-detector:latest >/dev/null 2>&1; then
    ./build.sh
fi

# 4. Fresh: clear edge-agent state.
if (( FRESH )); then
    rm -f /home/loadingcloud001/virex/deploy/edge/state/mediamtx.yml
    rm -f /home/loadingcloud001/virex/deploy/edge/state/docker-compose.worker.yml
    cp /home/loadingcloud001/virex/deploy/mediamtx/mediamtx.yml \
       /home/loadingcloud001/virex/deploy/edge/state/mediamtx.yml
fi

# 5. Bring everything up. The single .env is the SSOT for all secrets.
docker compose --env-file .env up -d --remove-orphans

echo ""
echo "==> status:"
docker compose ps
echo ""
echo "==> mediamtx paths:"
sleep 5
curl -s http://localhost:19997/v3/paths/list \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in sorted(d['items'], key=lambda x: x['confName']):
    print(f\"  {p['confName']:30s} ready={str(p['ready']):5s}\")
"