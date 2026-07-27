# SPDX-License-Identifier: Apache-2.0
"""edge-agent settings."""

from __future__ import annotations

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Edge-agent settings.

    Uses `pydantic_settings.BaseSettings` (NOT `pydantic.BaseModel`)
    because BaseModel silently ignores env_prefix and never reads env
    vars. With BaseSettings, VIREX_EDGE_PORTAL_URL / NODE_ID /
    STATE_DIR etc. set in the docker-compose environment actually
    reach this object — without this fix, every env var defaults to
    the compiled-in value (e.g. portal_url=http://127.0.0.1:8000
    regardless of VIREX_EDGE_PORTAL_URL) and external deploy crashes.
    """
    model_config = ConfigDict(env_prefix="VIREX_EDGE_", extra="ignore")

    node_id: int = 1
    portal_url: str = "http://127.0.0.1:8000"
    portal_bearer: str = "virex-edge-shared-secret"
    heartbeat_period_sec: int = 30
    config_pull_period_sec: int = 60
    edge_compose_path: str = "/etc/virex/docker-compose.worker.yml"
    mediamtx_main_path: str = "/etc/virex/mediamtx.yml"
    mediamtx_rtsp_port: int = 19554
    mediamtx_h264_suffix: str = "h264"
    workers_yaml_path: str = "/etc/virex/workers.yaml"
    state_dir: str = "/home/loadingcloud001/virex/deploy/edge/state"
    docker_compose_bin: str = "docker"
    worker_image: str = "virex-ai-backend:latest"


settings = Settings()
