# AI Pipeline (AGPL-3.0, Ultralytics + Triton)

Virex uses a **multi-model stage-based AI pipeline** that runs entirely
on edge nodes. Each camera declares its own pipeline configuration in
`workers.yaml`; the worker hot-reloads changes with zero downtime (see
`docs/config-hot-reload.md`).

## Architecture

```
   ┌──────────────────────────────────────────┐
   │ per-camera worker (virex-worker-X)       │
   │                                          │
   │  PyAV → BGR frame                        │
   │     ↓                                    │
   │  apply_masks (cv2.fillPoly BLACK)        │
   │     ↓                                    │
   │  motion detector (cv2 absdiff + contour) │ ← CPU pre-filter
   │     ↓                                    │
   │  Triton HTTP POST (KServe v2)            │
   │     ↓                                    │
   │  zone filter (bottom-center polygon test) │
   │     ↓                                    │
   │  DetectionEvent (zone_ids tagged)         │ → MQTT virex/detections
   └──────────────────────────────────────────┘
        │
        ↓
   Triton Inference Server (host network :38000)
        │
        ├─► yolov8m              (Ultralytics, AGPL-3.0)
        ├─► sam2_hiera_base_plus  (Apache-2.0 wrapper + SAM2)
        └─► depth_anything_v2_small  (MIT)
```

## Stage registry

The Triton model repository (under
`deploy/edge/state/triton/model_repository/`) ships three models and four
ensembles. The worker picks the ensemble based on the camera's
`pipeline:` config:

| Pipeline | Triton ensemble |
|---|---|
| `[detect]` | `detect_only` |
| `[detect, segment]` | `detect_segment` |
| `[detect, depth]` | `detect_depth` |
| `[detect, segment, depth]` | `detect_segment_depth` |

### Per-stage trigger semantics

Each pipeline stage has a `trigger:` field:

- `always` — stage runs every motion-gated frame
- `on_motion` — only runs if `MotionDetector.update()` reports motion this
  frame (we already motion-gate at the worker level; this is a finer
  secondary gate)
- `on_high_conf` — runs on every frame; the worker filters inside
  before publishing the event (the unused stages are still computed by
  Triton but the event only carries `detections`)
- `on_zone_enter(<zone_id>)` — runs only when at least one prior
  detection is in that zone

For v1 we collapse trigger semantics to "motion-gated always", which
covers 90% of deployments. Per-stage trigger evaluation lives in the
worker; Triton's ensembles don't carry trigger semantics.

### Adding a new model

To add a new model (e.g. PPE classification) to all cameras:

1. Add a `PpeStage` adapter class in `detector/stages/` (mirrors the
   existing stage interface).
2. Export ONNX: `python -m detector.onnx_export --ppe ppe_yolov8m.pt`.
3. Drop the resulting `model.onnx` under
   `deploy/edge/state/triton/model_repository/ppe_yolov8m/1/`.
4. Add `config.pbtxt` with input/output tensor names.
5. Update ensembles: add the new stage to `config.pbtxt`'s ensemble
   step list. The worker picks the new ensemble via `select_ensemble()`.
6. Bump pipeline default in `workers.yaml` for cameras that want PPE.

Workers do **not** need to change code; the registry maps names to
classes.

## Motion detection (CPU pre-filter)

Frigate-style motion detector (`worker/motion.py`):

1. Resize BGR frame to 320×180 grayscale.
2. `cv2.absdiff(prev_gray, curr_gray) → mask` (threshold = `motion.threshold`).
3. `cv2.findContours(EXTERNAL)` → list of regions.
4. Sum contour areas; if any ≥ `motion.contour_area`, motion is
   significant.
5. Skip detection entirely if no motion (or if `motion.enabled=False`).
6. Whole-frame brightness change (`lightning_ratio > lightning_threshold`)
   is suppressed — prevents false positives on sun glare / IR cut.

Default values (overridable per camera in `workers.yaml`):

```yaml
motion:
  enabled: true
  threshold: 30            # cv2 threshold 1–255
  contour_area: 10         # min contour area in resized frame px²
  lightning_threshold: 0.8  # fraction of bright pixels above which we ignore
```

### Motion saves GPU cost

For a static camera (parking lot with rare events), motion gating skips
80–95% of inference calls. On RTX 4070 the YOLOv8m inference is
~30 ms; motion detection itself is ~2 ms. Net win: 25–28 ms per
skipped frame × hundreds of skipped frames per hour.

## Zones

Polygon zones (`worker/zones.py`):

- Define regions of interest in normalised `[0, 1]` xy coords.
- `inertia`: how many consecutive frames the bottom-center of the bbox
  must stay inside before the zone fires (avoids one-frame false
  positives).
- `objects`: which class labels the zone applies to (default:
  `["person"]`).

A detection can be inside multiple zones simultaneously; the
`DetectionEvent.detections[i].zone_ids` field carries the list.

### Hot-reload for zones

Zones are Tier A: edit `workers.yaml`, the worker swaps `HotConfig` on
next poll, the new `ZoneFilter` is constructed on first frame, and
the new zones apply from that point. **No worker restart needed.**

## Masks

Static polygons where motion + detect are disabled. Use for:

- Wind-blown trees / reflections that always trigger motion
- Timestamps / overlays burned into the camera feed
- Areas where the customer doesn't want any recording or alerting

`cv2.fillPoly(BLACK)` erases the polygon region before motion + detect
runs.

## Mask

[mask.mjs example moved to `docs/config-hot-reload.md` — flat schema section.]

## Tier reload semantics for new fields

| Field | Tier | Behaviour |
|---|---|---|
| `detect.fps / min_score / classes / roi` | A | Atomic swap; next frame uses new value |
| `motion.enabled / threshold / contour_area / lightning_threshold` | A | Same as detect |
| `masks` | A | Same as detect |
| `zones` | A | New `ZoneFilter` constructed lazily on first frame |
| `pipeline` | A | Triton ensemble chosen per frame from current pipeline |
| `source_rtsp` | B | PyAV reconnect |
| `record` | C | MediaMTX restart (~5 s blip) |
| Add/remove camera | D | Full reconcile (~10–30 s) |

## Building the model repository

Once at image build time:

```bash
# Inside the virex-detector image (already has ultralytics + optimum)
python -m detector.onnx_export \
  --out-root /opt/virex/triton/model_repository \
  --yolo yolov8m.pt \
  --sam sam2_hiera_base_plus.pt \
  --depth depth-anything/Depth-Anything-V2-Small

# Then mount the model repository into Triton:
#   -v ./deploy/edge/state/triton/model_repository:/models:ro
```

For local dev with the FastAPI detector instead of Triton, no model
export is needed — the FastAPI shim loads `.pt` weights directly via
Ultralytics (`YOLO("yolov8m.pt")`).

## GPU memory budget

All three models on a single RTX 4070 (8 GB):

| Model | fp16 size |
|---|---|
| YOLOv8m | ~250 MB |
| SAM2 base-plus | ~1.4 GB |
| DepthAnything V2 Small | ~150 MB |
| **Total** | **~1.8 GB** |

Plenty of headroom for additional models (PPE, vehicle brand,
license-plate OCR, etc.) without GPU memory contention.

## Edge cases & failure modes

| Failure | Behaviour |
|---|---|
| Motion detector raises on bad frame | Treats as `no motion`; skips `/detect` |
| Triton 5xx | Worker logs `detector_call_failed`, continues to next frame |
| Triton connect timeout (10 s) | Same as 5xx; detector circuit-breaker not yet implemented (deferred) |
| Segment OOM on Triton | Triton returns `detect_segment` results with `masks: null`; worker logs warning |
| Zone polygon invalid (self-intersecting) | Worker logs `invalid_zone`, applies other zones |
| Mask polygon invalid | Worker logs `invalid_mask`, applies other masks |

## Adding per-tenant fine-tuned models (Phase 3)

Each tenant can fine-tune the detector on site-specific data (PPE,
vehicle models). Ultralytics CLI + Roboflow Hub handle training. The
fine-tuned weights live in `state/triton/model_repository/<tenant_id>/`
and a per-tenant ensemble routes by `tenant_id` header. This is
a Phase 3 concern; v1 ships the COCO-pretrained YOLOv8m as the
default for all tenants.

## Triton python_backend/image_decoder

The worker sends JPEG bytes as a BYTES tensor; Triton's python
backend `image_decoder/1/model.py` decodes the bytes into NCHW
float32 tensors at three resolutions (640×640, 1024×1024,
518×518). This avoids sending the JPEG through Python and re-decoding
in each model — Triton handles it once on GPU.

To debug the decoder: `curl http://127.0.0.1:38000/v2/models/image_decoder/ready`.