# SPDX-License-Identifier: Apache-2.0
# Lightweight worker + clip-builder image (CPU runtime; only host network
# access to MediaMTX on the same host + outbound Tailscale to VPS).
#
# The BentoML detector service has its own CUDA image — see `bentofile.yaml`.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        ca-certificates \
        curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching.
COPY pyproject.toml ./pyproject.toml
RUN pip install --quiet \
        "httpx>=0.27" \
        "paho-mqtt>=2" \
        "minio>=7.2" \
        "pydantic>=2.5" \
        "structlog>=24" \
        "tenacity>=8" \
        "pillow>=10" \
        "opencv-python-headless>=4.9" \
        "av>=12" \
        "pyyaml>=6" \
        # Hot-reload: admin FastAPI server.
        "fastapi>=0.115" \
        "uvicorn>=0.32" \
        "numpy<2"

# Source is mounted read-only; bundle is small for fast iteration.
COPY detector/        ./detector/
COPY worker/          ./worker/
COPY clip_builder/    ./clip_builder/

# No ENTRYPOINT — each compose service sets its own `command:` to pick
# the right entry point (worker, clip_builder, detector, …).
CMD ["python", "-m", "worker.main", "--config", "/etc/virex/workers.yaml"]