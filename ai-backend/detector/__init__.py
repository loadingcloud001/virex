# SPDX-License-Identifier: AGPL-3.0
"""Virex AI inference package.

Two interchangeable runtimes for v1:

1. **FastAPI shim** (`detector.app.VirexDetectorApp`): wraps a single
   Ultralytics YOLOv8m model loaded in-process; useful for local dev,
   `curl` smoke tests, and the worker when `detector_kind=fastapi` is
   set in `workers.yaml`.

   Run locally:
       uvicorn detector.app:VirexDetectorApp --host 0.0.0.0 --port 31001

2. **NVIDIA Triton Inference Server** (production): hosts YOLOv8m +
   SAM2 + DepthAnything in a single GPU process with dynamic batching
   and ensemble pipelines. Worker talks to it via KServe v2 HTTP at
   `http://127.0.0.1:38000`; this package is not loaded in production.
   See `docs/ai-pipeline.md` for the ensemble config layout.

`detector.service.Detector` deliberately does NOT eagerly import
`ultralytics` so that lightweight imports (`detector.postprocess`,
`worker.config_hot`) used by unit tests don't require `ultralytics`.

The worker (`ai-backend/worker/`) POSTs JPEG bytes to either runtime's
`/detect` (FastAPI) or `/v2/models/<ensemble>/infer` (Triton).
"""
