# SPDX-License-Identifier: Apache-2.0
"""Pydantic-settings-managed portal configuration."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="VIREX_", extra="ignore")

    database_url: str = "postgresql+asyncpg://virex:virex@127.0.0.1:5432/virex"
    redis_url: str = "redis://default@127.0.0.1:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "virex"
    minio_secure: bool = False

    # Secrets for edge ↔ portal internal endpoints (Phase 1 simple bearer).
    edge_bearer: str = "virex-edge-shared-secret"

    # Default MediaMTX edge ingest port (workers/readers use this).
    mediamtx_rtsp_port: int = 19554


settings = Settings()
