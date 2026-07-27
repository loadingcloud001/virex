# SPDX-License-Identifier: AGPL-3.0
"""Build-time ONNX export helper.

Virex ships models in the Triton Inference Server ONNX format. This
module is run once at image-build time (see `deploy/edge/build.sh`) to
produce the .onnx files that ship in the Docker image.

Three models are exported today:

1. **YOLOv8** detection — via Ultralytics native `.export(format="onnx")`.
   - Output: `[1, 84, 8400]` (84 = 4 box coords + 80 classes; YOLOv8 uses
     sigmoid-then-multiplication decoding, so the box + class scores
     are bundled in one tensor).
   - Inputs: `images: float32[1, 3, 640, 640]`.

2. **SAM2 (segmentation)** — `facebookresearch/sam2` does not ship an
   ONNX export by default. We use the Ultralytics SAM wrapper
   (`ultralytics.YOLO("sam2_hiera_base_plus.pt")`) which provides
   `model.export(format="onnx")`. Inputs: `image: float32[1, 3, 1024, 1024]`,
   `boxes: float32[N, 4]`. Output: `masks: float32[N, 256, 256]`.

3. **DepthAnything V2 Small** — exported via `optimum-cli`:

   ```text
   optimum-cli export onnx --task depth-estimation \
     --model depth-anything/Depth-Anything-V2-Small \
     depth_anything_v2_small.onnx
   ```

   Inputs: `pixel_values: float32[1, 3, 518, 518]`. Output:
   `predicted_depth: float32[1, 518, 518]`.

The exported files are placed at:
    deploy/edge/state/triton/model_repository/<model>/1/model.onnx

The export script is invoked from `deploy/edge/build.sh` (this is a
build-time-only concern; it does not run on every deployment).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRITON_REPO = REPO_ROOT / "deploy" / "edge" / "state" / "triton" / "model_repository"


def _ensure_ultralytics() -> None:
    try:
        import ultralytics  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "ultralytics not installed. Install via `uv pip install ultralytics`."
        ) from e


def export_yolo(
    out_dir: Path,
    model_id: str = "yolov8m.pt",
    imgsz: int = 640,
    half: bool = True,
) -> Path:
    """Export Ultralytics YOLOv8 to ONNX and place under `out_dir/yolov8m/1/`."""
    _ensure_ultralytics()
    from ultralytics import YOLO

    yolo = YOLO(model_id)
    logger.info("yolo_export_start", model=model_id, imgsz=imgsz, half=half)
    tmp_path = yolo.export(
        format="onnx",
        imgsz=imgsz,
        half=half,
        simplify=True,
        opset=17,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "model.onnx"
    shutil.move(str(tmp_path), str(final))
    (out_dir.parent / "1").mkdir(parents=True, exist_ok=True)
    # Triton expects `model.onnx` under `<model_name>/<version>/`.
    versioned = out_dir.parent / "1" / "model.onnx"
    shutil.copy(final, versioned)
    logger.info("yolo_export_done", path=str(versioned))
    return versioned


def export_sam2(
    out_dir: Path,
    model_id: str = "sam2_hiera_base_plus.pt",
    imgsz: int = 1024,
) -> Path:
    """Export Ultralytics SAM2 wrapper to ONNX. Boxes are baked as input."""
    _ensure_ultralytics()
    from ultralytics import YOLO

    model = YOLO(model_id)
    logger.info("sam2_export_start", model=model_id, imgsz=imgsz)
    tmp_path = model.export(format="onnx", imgsz=imgsz, simplify=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "model.onnx"
    shutil.move(str(tmp_path), str(final))
    versioned = out_dir.parent / "1" / "model.onnx"
    shutil.copy(final, versioned)
    logger.info("sam2_export_done", path=str(versioned))
    return versioned


def export_depth_anything(
    out_dir: Path,
    model_id: str = "depth-anything/Depth-Anything-V2-Small",
    imgsz: int = 518,
) -> Path:
    """Export DepthAnything V2 via `optimum-cli` ONNX exporter."""
    try:
        import optimum.onnxruntime  # noqa: F401
    except ImportError:
        logger.info("installing_optimum_onnxruntime")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "optimum[onnxruntime]"],
            check=True,
        )

    cmd = [
        "optimum-cli",
        "export",
        "onnx",
        "--task",
        "depth-estimation",
        "--model",
        model_id,
        "--opset",
        "17",
        str(out_dir / "model.onnx"),
    ]
    logger.info("depth_export_start", model=model_id)
    subprocess.run(cmd, check=True)
    versioned = out_dir.parent / "1" / "model.onnx"
    shutil.copy(out_dir / "model.onnx", versioned)
    logger.info("depth_export_done", path=str(versioned))
    return versioned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Virex AI models to ONNX")
    parser.add_argument(
        "--out-root",
        type=Path,
        default=TRITON_REPO,
        help="Triton model_repository root (default: deploy/edge/state/triton/model_repository)",
    )
    parser.add_argument("--yolo", default="yolov8m.pt")
    parser.add_argument("--sam", default="sam2_hiera_base_plus.pt")
    parser.add_argument("--depth", default="depth-anything/Depth-Anything-V2-Small")
    parser.add_argument("--imgsz-yolo", type=int, default=640)
    parser.add_argument("--imgsz-sam", type=int, default=1024)
    parser.add_argument("--imgsz-depth", type=int, default=518)
    parser.add_argument("--no-half", action="store_true")
    args = parser.parse_args(argv)

    out_root: Path = args.out_root
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "yolov8m" / "1").mkdir(parents=True, exist_ok=True)
    (out_root / "sam2_hiera_base_plus" / "1").mkdir(parents=True, exist_ok=True)
    (out_root / "depth_anything_v2_small" / "1").mkdir(parents=True, exist_ok=True)

    export_yolo(
        out_root / "yolov8m" / "1",
        model_id=args.yolo,
        imgsz=args.imgsz_yolo,
        half=not args.no_half,
    )
    export_sam2(
        out_root / "sam2_hiera_base_plus" / "1",
        model_id=args.sam,
        imgsz=args.imgsz_sam,
    )
    export_depth_anything(
        out_root / "depth_anything_v2_small" / "1",
        model_id=args.depth,
        imgsz=args.imgsz_depth,
    )
    logger.info("all_exports_done", out_root=str(out_root))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: tuple[str, ...] = (
    "export_yolo",
    "export_sam2",
    "export_depth_anything",
    "main",
)
