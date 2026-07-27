# Third-Party Notices

Virex depends on the following open-source projects. All are compatible
with Virex's AGPL-3.0 distribution.

| Component | Upstream | License | Source |
|---|---|---|---|
| **Ultralytics YOLOv8 / YOLOv11 / YOLOv26** | <https://github.com/ultralytics/ultralytics> | **AGPL-3.0** (model weights) | github.com/ultralytics/assets |
| Ultralytics Python SDK (`ultralytics`) | same | **AGPL-3.0** (SDK) | pip / conda `ultralytics` |
| Ultralytics SAM (Segment Anything) wrapper | <https://github.com/ultralytics/ultralytics> | Apache-2.0 (wrapper) / SAM2 weights from Meta under SAM license | ultralytics |
| facebookresearch SAM2 | <https://github.com/facebookresearch/sam2> | Apache-2.0 | github.com/facebookresearch/sam2 |
| DepthAnything V2 | <https://github.com/depth-anything/Depth-Anything-V2> | **MIT** | github.com/depth-anything |
| NVIDIA Triton Inference Server | <https://github.com/triton-inference-server/server> | **BSD-3-Clause** | github.com/triton-inference-server |
| MediaMTX | <https://github.com/bluenviron/mediamtx> | **MIT** | github.com/bluenviron/mediamtx |
| jrottenberg/ffmpeg | <https://github.com/jrottenberg/ffmpeg> | LGPL-2.1+ / GPL-2 | Docker Hub |
| OpenCV (`opencv-python-headless`) | <https://github.com/opencv/opencv> | **Apache-2.0** | github.com/opencv |
| PyAV (`av`) | <https://github.com/PyAV-Org/PyAV> | **BSD-3-Clause** | github.com/PyAV-Org |
| FastAPI | <https://github.com/tiangolo/fastapi> | **MIT** | github.com/tiangolo |
| Uvicorn | <https://github.com/encode/uvicorn> | **BSD-3-Clause** | github.com/encode |
| Pydantic v2 | <https://github.com/pydantic/pydantic> | **MIT** | github.com/pydantic |
| structlog | <https://github.com/hynek/structlog> | Apache-2.0 / MIT | github.com/hynek |
| paho-mqtt | <https://github.com/eclipse/paho.mqtt.python> | **EPL-2.0** / BSD-3 | github.com/eclipse/paho.mqtt.python |
| minio-py | <https://github.com/minio/minio-py> | Apache-2.0 | github.com/minio |
| watchdog | <https://github.com/gorakhargosh/watchdog> | Apache-2.0 | github.com/gorakhargosh |
| httpx | <https://github.com/encode/httpx> | **BSD-3-Clause** | github.com/encode |
| Jinja2 | <https://github.com/pallets/jinja> | **BSD-3-Clause** | github.com/pallets |
| pyyaml | <https://github.com/yaml/pyyaml> | **MIT** | github.com/yaml |
| Docker SDK | <https://github.com/docker/docker-py> | Apache-2.0 | github.com/docker |
| tenacity | <https://github.com/jd/tenacity> | Apache-2.0 | github.com/jd |

## Notable interaction with Virex licence

Because Virex links against (or imports at runtime) the AGPL-3.0
Ultralytics SDK + YOLO weights, Virex is itself an "AGPL-3.0 affected
work" and is licensed as AGPL-3.0 in its entirety. See `LICENSE`.

The AGPL-3.0 licence does **not** restrict commercial use or paid
distribution; it requires only that:

1. The complete Corresponding Source be made available to network
   users (§13), and
2. Modifications be released under the same licence (§5).

`SOURCE_OFFER.md` documents the mechanism we use to satisfy §13.

## Embedded model weights

The following model weight files are downloaded at build time and
bundled into the Docker images:

- `yolov8m.pt`, `yolov8l.pt`, `yolov8x.pt` — Ultralytics AGPL-3.0
- `yolo11m.pt`, `yolo11l.pt` — Ultralytics AGPL-3.0
- `sam2_hiera_base_plus.pt` — Meta SAM licence (similar to Apache-2.0)
- `depth_anything_v2_small.pth` — MIT
- `rtdetr_r18vd_*` (legacy) — Apache-2.0 (PekingU / HuggingFace)

Operator-supplied custom fine-tuned weights are subject to the licence
under which they were trained. Virex does not bundle third-party
training data.

## Tools used to generate this file

Run `pip-licenses --format=markdown --with-system` inside each
sub-project (`ai-backend/`, `edge-agent/`, etc.) to regenerate.