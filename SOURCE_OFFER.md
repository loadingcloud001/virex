# Source Offer

Virex is licensed under the **GNU Affero General Public License v3.0
(AGPL-3.0)**. Under §13 of the AGPL ("Remote Network Interaction"),
if you interact with a deployed Virex instance over a network, the
operator of that instance must offer you access to the **Corresponding
Source** for the version they are running.

This document fulfils that obligation for the Virex project itself.

## How to obtain the source

The canonical source is the public Git repository:

```
https://github.com/virex/virex
```

Every release is tagged. To obtain the exact source for a deployed
version, locate the version string at:

- SaaS portal footer: **"Source code — AGPL-3.0"** link
- API: `GET /api/v1/source_offer` returns `{ "version": "...", "tag": "...", "url": "..." }`

Then check out the matching tag:

```bash
git clone https://github.com/virex/virex.git
cd virex
git checkout v<TAG>     # e.g. git checkout v0.3.0
```

## What counts as Corresponding Source

Per AGPL §1 ("source code" definition), we provide for each release
tag:

- All Python modules (`ai-backend/`, `edge-agent/`, `event-router/`,
  `portal/`, `worker/`, `detector/`, `clip_builder/`)
- All Jinja2 templates (`**/*.j2`) used for config generation
- All Dockerfiles (`**/Dockerfile*`, `**/docker-compose*.yml`)
- All build scripts (`deploy/edge/build.sh`, etc.)
- `requirements.txt`, `pyproject.toml`, lockfiles where present
- `state/` rendering logic (worker-compose, transcoder-compose,
  mediamtx.yml templates)
- `docs/` for the deployed version
- A pre-exported ONNX model repository at
  `deploy/edge/state/triton/model_repository/` (large; see Releases page
  for download link, since git LFS may apply)

We do **not** include:

- Trained custom model weights for individual tenants (operator's
  responsibility — these may be subject to their own licences)
- Production database contents, customer-uploaded media, or `.env`
- Live recordings (these are stored in COS, not in this repo)

## Building from source

```bash
# Virex edge node (GPU host with CUDA 12.1):
docker build -t virex-edge-agent:latest  -f edge-agent/Dockerfile    edge-agent
docker build -t virex-ai-backend:latest  -f ai-backend/deploy/Dockerfile  ai-backend

# Detector runtime (Ultralytics YOLOv8m + Triton ensemble):
docker build -t virex-detector:latest    -f ai-backend/deploy/Dockerfile.cuda  ai-backend

# VPS control plane (Phase 2):
docker build -t virex-portal:latest      -f portal/Dockerfile        portal
docker build -t virex-event-router:latest -f event-router/Dockerfile  event-router
```

Full build instructions in `docs/ai-pipeline.md` and `docs/config-hot-reload.md`.

## Modifications

If you fork Virex and modify it for your own deployment:

- Your fork must remain under AGPL-3.0 (§5).
- Your SaaS users must be able to obtain the source for **your
  modified version** (§13). Either:
  - publish your fork publicly, OR
  - provide a written offer (per §6b) sent on request to any
    interacting user.

Virex authors do not provide commercial dual-licensing. If you need
an alternative licence for closed-source redistribution, your only
option is to maintain a fork of Virex under AGPL-3.0 and offer source
to your users as above.

## Trademark

"Virex" is the project name. You may refer to your fork by another
name. Do not use the Virex name in product branding for forks that
substantially diverge from upstream.

## Contact

- GitHub issues: <https://github.com/virex/virex/issues>
- GitHub discussions: <https://github.com/virex/virex/discussions>

(This file is part of the Corresponding Source for the version it
ships with. Update it whenever a new version is tagged.)