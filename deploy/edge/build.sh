#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build all Virex AI edge images locally. Idempotent — safe to re-run.
#
# Usage:    ./build.sh
# Output:   virex-detector:latest, virex-ai-backend:latest,
#           virex-event-router:latest, virex-portal:latest,
#           virex-edge-agent:latest

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> building virex-detector:latest (RT-DETR-R18 + CUDA)"
docker build \
    -t virex-detector:latest \
    -f ai-backend/deploy/Dockerfile.cuda \
    ai-backend

echo "==> building virex-ai-backend:latest (workers + clip-builder)"
docker build -t virex-ai-backend:latest -f ai-backend/deploy/Dockerfile ai-backend

echo "==> building virex-event-router:latest"
docker build -t virex-event-router:latest -f event-router/Dockerfile event-router

echo "==> building virex-portal:latest"
docker build -t virex-portal:latest -f portal/Dockerfile portal

echo "==> building virex-edge-agent:latest"
docker build -t virex-edge-agent:latest -f edge-agent/Dockerfile edge-agent

echo ""
echo "==> built images:"
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | grep -E "virex-|^REPOSITORY"