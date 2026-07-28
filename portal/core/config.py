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

    # Secrets for edge ↔ portal internal endpoints. Phase 1 ships with
    # BOTH a shared-secret bootstrap (for first-time edge registration)
    # AND JWT bearer tokens (for all subsequent calls). The shared
    # secret is rotated only at deploy time; JWTs are rotated every
    # 30 days via /api/edge/nodes/{id}/rotate.
    edge_bootstrap_secret: str = "virex-edge-bootstrap-secret-change-me"
    jwt_secret: str = "virex-jwt-secret-change-me"
    jwt_ttl_sec: int = 30 * 24 * 3600  # 30 days

    # UI session cookie signing key. Sessions are stateless-signed
    # cookies (no Redis dependency) for Phase 1.
    session_secret: str = "virex-session-secret-change-me"
    cookie_secure: bool = False  # set to True in prod (HTTPS via CF Tunnel)

    # Default tenant slug used in dev mode (Host=127.0.0.1 or localhost).
    default_tenant_slug: str = "acme"

    # Bootstrap admin credentials (used by `python -m portal.seed`).
    bootstrap_admin_email: str = "admin@acme.example.com"
    bootstrap_admin_password: str = "change-me-now"

    # Default MediaMTX edge ingest port (workers/readers use this).
    mediamtx_rtsp_port: int = 19554

    # Public MediaMTX base URL for portal playback endpoints
    # (`/api/cameras/{id}/hls_url` builds HLS URLs from this). In dev the
    # portal container reaches the edge's MediaMTX via host network on
    # port 8888; in prod this is the public CDN/Cloudflare-fronted URL.
    mediamtx_public_url: str = "http://mediamtx:8888"

    # Show the dev-only "Demo credentials" helper card on the login page.
    # Set to `False` (or set VIREX_DEBUG_LOGIN_HELPER=false) before public
    # launch — the helper exposes the default bootstrap admin password.
    debug_login_helper: bool = True


settings = Settings()
