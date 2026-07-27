# SPDX-License-Identifier: AGPL-3.0
"""Regression test: Settings must use pydantic_settings.BaseSettings, NOT
pydantic.BaseModel, so VIREX_EDGE_* env vars actually reach the config.

Earlier versions had `class Settings(BaseModel)` with `env_prefix=` —
pydantic.BaseModel silently ignores env_prefix, so env vars like
VIREX_EDGE_PORTAL_URL, VIREX_EDGE_NODE_ID, VIREX_EDGE_STATE_DIR were all
ignored and Settings always returned compile-time defaults. This
silently broke every non-default deployment.
"""

from __future__ import annotations


def test_settings_reads_portal_url_from_env(monkeypatch):
    monkeypatch.setenv("VIREX_EDGE_PORTAL_URL", "http://portal.example.com:9999")
    # Important: force re-import so Settings class is fresh (BaseSettings
    # reads env at __init__ time, so a fresh instance is enough)
    from src.config import Settings
    s = Settings()
    assert s.portal_url == "http://portal.example.com:9999", (
        "Settings does not read env vars — must use BaseSettings, "
        f"got portal_url={s.portal_url!r}"
    )


def test_settings_reads_node_id_from_env(monkeypatch):
    monkeypatch.setenv("VIREX_EDGE_NODE_ID", "42")
    from src.config import Settings
    s = Settings()
    assert s.node_id == 42


def test_settings_reads_state_dir_from_env(monkeypatch):
    monkeypatch.setenv("VIREX_EDGE_STATE_DIR", "/opt/virex/state")
    from src.config import Settings
    s = Settings()
    assert s.state_dir == "/opt/virex/state", (
        "Field name mismatch: env var VIREX_EDGE_STATE_DIR expects field "
        f"named `state_dir`, got attribute name (or default value): {s.state_dir!r}"
    )
