# Documentation

Detailed guides for the Virex AI edge stack.

> **Phase-1 MVP placeholders removed.** The Frigate + go2rtc + n8n + Week-N
> planning references in earlier versions of this file referred to the
> original Phase 1 design that this project no longer follows. The
> current v1 pilot stack is AGPL-3.0 + MediaMTX + per-camera workers
> + edge-agent — see [`../README.md`](../README.md) for the canonical
> overview.

## Available Docs

- [`ai-pipeline.md`](ai-pipeline.md) — Per-camera AI pipeline stages
  (motion, zone, detect, segment, depth, pose) + Ultralytics YOLO +
  optional Triton Inference Server.
- [`config-hot-reload.md`](config-hot-reload.md) — Tier A / B / C / D
  hot-reload contract: how `workers.yaml` edits propagate to running
  worker containers with the cheapest possible action.
- [`mediamtx-architecture.md`](mediamtx-architecture.md) — MediaMTX
  path layout (`<camera>raw` for camera-native RTSP, `<camera>h264`
  for FFmpeg-repacked H.264), recording layout, REST control API usage.

## Quick Links

- [`../README.md`](../README.md) — top-level overview, modules, hot-reload
  summary, tech stack, license.
- [`../CLAUDE.md`](../CLAUDE.md) — coding standards (the conventions
  section is still authoritative; the architecture-summary section
  inside it is historical — see the banner).
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — historical Phase 1 design
  rationale (banner at top). For current architecture, see
  [`../README.md`](../README.md).

## Planned Docs (roadmap)

- `OPERATIONS.md` — runbook for the v1 pilot (deploy, observe, rotate
  keys, restart workers, diagnose hot-reload).
- `MULTI_TENANT.md` — when the portal becomes the source of truth (Phase
  2), how the edge-agent switches from local `workers.yaml` to portal-
  supplied config.