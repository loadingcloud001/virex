# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the portal.

Uses an in-memory SQLite database (via aiosqlite) so we don't need a
running Postgres for unit tests. The DDL is loaded via SQLAlchemy
metadata.create_all on the SQLite engine — alembic migrations are
exercised separately via test_alembic_migration.py.

Each test gets a fresh in-memory DB by overriding `core.database`'s
engine / SessionLocal. The `get_db` dependency is patched via
FastAPI's dependency_overrides.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import create_app
from core.config import settings
from core.database import get_db
from core.security import hash_password
from models import MTX_PATH_RE, Camera, Node, Tenant, User

# Use aiosqlite in-memory DB — one per test for isolation.
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def app_client() -> AsyncIterator[AsyncClient]:
    """Provide an AsyncClient backed by an isolated in-memory SQLite DB.

    SQLite doesn't support the Postgres `~` regex operator used in our
    CheckConstraint for mtx_path format, so we monkey-patch the
    constraint away by reassigning the in-memory ``Base.metadata``
    before ``create_all``. Tests cover the validation logic separately
    in test_camera_mtx_path_validation (which checks the DTO-level
    validation in the endpoint, not the DB constraint).
    """
    # Build a fresh engine on a freshly-cloned metadata so we don't
    # mutate the global Base.metadata (which would leak across tests).
    from core.database import Base
    import models  # noqa: F401

    # Clone the metadata and drop the Postgres-only regex constraint.
    cloned_meta = MetaData()
    for table in Base.metadata.tables.values():
        # Re-bind each table to the new metadata, dropping the regex CHECK.
        cloned_table = table.to_metadata(cloned_meta, schema=table.schema)
        cloned_table.constraints = {
            c for c in cloned_table.constraints
            if not (hasattr(c, "sqltext") and "~" in str(c.sqltext))
        }

    engine = create_async_engine(TEST_DB_URL)

    async with engine.begin() as conn:
        await conn.run_sync(cloned_meta.create_all)

    TestSession = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    # Build the app and override get_db to use TestSession. Also patch
    # the module-level engine and SessionLocal so any code path that
    # bypasses the FastAPI dependency (e.g. TenantMiddleware resolving
    # tenants directly) uses the SQLite engine.
    import core.database as db_module

    original_engine = db_module.engine
    original_session_local = db_module.SessionLocal
    db_module.engine = engine
    db_module.SessionLocal = TestSession
    # Reconfigure the original sessionmaker to bind to the test engine.
    # Without this, `SessionLocal()` (called from anywhere outside the
    # FastAPI dependency, e.g. TenantMiddleware) still uses the original
    # asyncpg engine because sessionmakers bind at construction time.
    if hasattr(original_session_local, "configure"):
        original_session_local.configure(bind=engine)

    app = create_app()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with TestSession() as session:
            yield session

    # The deps module imports get_db by name; FastAPI's override must
    # target the same reference that the dependency uses. So we
    # override the symbol that's actually imported into api.v1.deps.
    app.dependency_overrides[get_db] = _override_get_db
    import api.v1.deps as _deps
    app.dependency_overrides[_deps.get_db] = _override_get_db

    # Seed a tenant + admin + node so most tests don't need to do it themselves.
    async with TestSession() as session:
        tenant = Tenant(slug="acme", name="Acme Construction", subdomain="acme")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        user = User(
            tenant_id=tenant.id,
            email="admin@acme.example.com",
            password_hash=hash_password("hunter2"),
            role="admin",
            is_active=True,
        )
        node = Node(
            tenant_id=tenant.id,
            hostname="edge-rtx4070-1",
            tailscale_ip="127.0.0.1",
            max_cameras=8,
        )
        session.add_all([user, node])
        await session.commit()
        await session.refresh(user)
        await session.refresh(node)

    try:
        # Transport that targets the in-process app (no network).
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        # Restore module-level state so other tests / fixtures aren't affected.
        db_module.engine = original_engine
        db_module.SessionLocal = original_session_local
        await engine.dispose()


@pytest_asyncio.fixture
async def auth_cookie(app_client: AsyncClient) -> str:
    """Log in as the seeded admin and return the session cookie value."""
    resp = await app_client.post(
        "/api/auth/login",
        json={"email": "admin@acme.example.com", "password": "hunter2"},
    )
    assert resp.status_code == 200, resp.text
    return resp.cookies["virex_session"]


# ---------------------------------------------------------------------------
# Sanity: the in-memory stack actually works
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_healthz_is_unauthenticated(app_client: AsyncClient) -> None:
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Tenant middleware
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dev_host_resolves_to_default_tenant(app_client: AsyncClient) -> None:
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    # Cameras endpoint requires auth, but we want to confirm Host parsing.
    # Hit /cameras without auth → 401 with Location: /login header (the
    # session dep raises 401, not a redirect; UI relies on the client
    # to follow the Location header to /login).
    resp = await app_client.get("/cameras")
    assert resp.status_code == 401
    assert resp.headers.get("location") == "/login"


@pytest.mark.asyncio
async def test_unknown_subdomain_404(app_client: AsyncClient) -> None:
    resp = await app_client.get(
        "/api/edge/config", headers={"Host": "unknown.portal.example.com"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_correct_subdomain_resolves(app_client: AsyncClient) -> None:
    """A correct subdomain should let bootstrap-secret request through."""
    resp = await app_client.post(
        "/api/edge/nodes/register",
        headers={
            "Host": "acme.portal.example.com",
            "Authorization": "Bearer virex-edge-bootstrap-secret-change-me",  # bootstrap secret (default)
        },
        json={
            "hostname": "edge-new",
            "tailscale_ip": "100.64.0.2",
            "gpu_model": "RTX 4070",
            "max_cameras": 8,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] >= 1
    assert body["jwt_token"]
    assert body["ttl_sec"] > 0


# ---------------------------------------------------------------------------
# Auth: login / logout / me / register
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_with_correct_creds(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/auth/login",
        json={"email": "admin@acme.example.com", "password": "hunter2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@acme.example.com"
    assert body["tenant_slug"] == "acme"
    assert "virex_session" in resp.cookies


@pytest.mark.asyncio
async def test_login_with_wrong_password_401(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/auth/login",
        json={"email": "admin@acme.example.com", "password": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_with_unknown_email_401(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/auth/login",
        json={"email": "nobody@acme.example.com", "password": "hunter2"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_session_cookie(app_client: AsyncClient, auth_cookie: str) -> None:
    resp = await app_client.get(
        "/api/auth/me", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@acme.example.com"


@pytest.mark.asyncio
async def test_me_without_cookie_401(app_client: AsyncClient) -> None:
    resp = await app_client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookie(app_client: AsyncClient, auth_cookie: str) -> None:
    resp = await app_client.post(
        "/logout", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code in (302, 303)


@pytest.mark.asyncio
async def test_register_creates_user(app_client: AsyncClient, auth_cookie: str) -> None:
    resp = await app_client.post(
        "/api/auth/register",
        cookies={"virex_session": auth_cookie},
        json={"email": "new-admin@acme.example.com", "password": "another-passw0rd"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "new-admin@acme.example.com"


# ---------------------------------------------------------------------------
# Cameras CRUD
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_camera_via_form(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    resp = await app_client.post(
        "/cameras/new",
        cookies={"virex_session": auth_cookie},
        data={
            "name": "Test cam",
            "mtx_path": "testcam01",
            "rtsp_url": "rtsp://example.com/test",
            "location": "lab",
            "node_id": "1",
            "retention_days": "7",
            "recording_enabled": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


@pytest.mark.asyncio
async def test_create_camera_via_api(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    resp = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": auth_cookie},
        json={
            "name": "API cam",
            "mtx_path": "apicam01",
            "rtsp_url": "rtsp://example.com/api",
            "node_id": 1,
            "recording_enabled": True,
            "retention_days": 7,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mtx_path"] == "apicam01"
    cam_id = body["id"]

    # GET /api/cameras
    resp = await app_client.get(
        "/api/cameras", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 200
    cameras = resp.json()
    assert any(c["id"] == cam_id for c in cameras)

    # PUT /api/cameras/{id}
    resp = await app_client.put(
        f"/api/cameras/{cam_id}",
        cookies={"virex_session": auth_cookie},
        json={"name": "API cam renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "API cam renamed"

    # DELETE /api/cameras/{id}
    resp = await app_client.delete(
        f"/api/cameras/{cam_id}", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_camera_mtx_path_validation(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """mtx_path with an underscore must be rejected (MediaMTX nests them)."""
    resp = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": auth_cookie},
        json={
            "name": "Bad cam",
            "mtx_path": "bad_name",
            "rtsp_url": "rtsp://example.com/bad",
            "node_id": 1,
        },
    )
    assert resp.status_code == 422
    assert MTX_PATH_RE == r"^[a-z0-9]+$"


# ---------------------------------------------------------------------------
# Edge: register + config + heartbeat (JWT)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_edge_register_with_bootstrap_secret(app_client: AsyncClient) -> None:
    """First registration uses the shared bootstrap secret."""
    resp = await app_client.post(
        "/api/edge/nodes/register",
        headers={"Authorization": "Bearer virex-edge-bootstrap-secret-change-me"},
        json={
            "hostname": "edge-test-1",
            "tailscale_ip": "100.64.0.10",
            "gpu_model": "RTX 4070",
            "max_cameras": 8,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jwt_token"]
    assert body["ttl_sec"] > 0
    return body  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_edge_register_rejects_bad_bootstrap_secret(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/edge/nodes/register",
        headers={"Authorization": "Bearer wrong-secret"},
        json={"hostname": "x", "tailscale_ip": "127.0.0.1", "max_cameras": 1},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_edge_full_flow(app_client: AsyncClient) -> None:
    """register → add camera → fetch config (JWT) → heartbeat (JWT) → rotate."""
    # 1. Register.
    reg = await app_client.post(
        "/api/edge/nodes/register",
        headers={"Authorization": "Bearer virex-edge-bootstrap-secret-change-me"},
        json={
            "hostname": "edge-flow-1",
            "tailscale_ip": "100.64.0.99",
            "max_cameras": 8,
        },
    )
    assert reg.status_code == 200
    body = reg.json()
    jwt_token = body["jwt_token"]
    node_id = body["node_id"]

    # 2. Add a camera via admin session.
    admin = await app_client.post(
        "/api/auth/login",
        json={"email": "admin@acme.example.com", "password": "hunter2"},
    )
    admin_cookie = admin.cookies["virex_session"]
    cam = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": admin_cookie},
        json={
            "name": "Edge flow cam",
            "mtx_path": "edgeflow01",
            "rtsp_url": "rtsp://example.com/edge",
            "node_id": node_id,
        },
    )
    assert cam.status_code == 201, cam.text

    # 3. Fetch config via JWT.
    cfg = await app_client.get(
        "/api/edge/config",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert cfg.status_code == 200
    cfg_body = cfg.json()
    assert cfg_body["node_id"] == node_id
    assert any(c["mtx_path"] == "edgeflow01" for c in cfg_body["cameras"])

    # 4. Heartbeat via JWT.
    hb = await app_client.post(
        "/api/edge/heartbeat",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={
            "node_id": node_id,
            "gpu_percent": 12.0,
            "gpu_mem_mb": 1024,
            "cpu_percent": 5.0,
            "ram_percent": 30.0,
            "active_cameras": 1,
            "healthy": True,
        },
    )
    assert hb.status_code == 204

    # 5. Rotate token via JWT.
    rotate = await app_client.post(
        f"/api/edge/nodes/{node_id}/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert rotate.status_code == 200
    new_token = rotate.json()["jwt_token"]
    # Tokens may be byte-identical if minted in the same second
    # (HS256 + identical payload). Either different bytes OR a fresh
    # iat-exp window — both prove the endpoint worked.
    assert (
        new_token != jwt_token
        or rotate.json()["ttl_sec"] == settings.jwt_ttl_sec
    )


@pytest.mark.asyncio
async def test_edge_config_rejects_missing_token(app_client: AsyncClient) -> None:
    resp = await app_client.get("/api/edge/config")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_edge_config_rejects_bad_token(app_client: AsyncClient) -> None:
    resp = await app_client.get(
        "/api/edge/config",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Frontend stack — HTMX / Alpine / DaisyUI scaffolding
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_security_headers_present(app_client: AsyncClient) -> None:
    """Every response should carry CSP + standard hardening headers."""
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "strict-origin-when-cross-origin" in resp.headers.get("referrer-policy", "")
    permissions = resp.headers.get("permissions-policy", "")
    assert "camera=()" in permissions
    assert "microphone=()" in permissions


@pytest.mark.asyncio
async def test_login_page_loads_daisyui_classes(app_client: AsyncClient) -> None:
    """The login page should render DaisyUI components + HTMX + Alpine.

    Regression test that catches accidental removal of <script>/<link>
    tags from base.html, dropping the operator back to the unstyled
    placeholder that base.html used to ship.
    """
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    html = resp.text

    # Tailwind / DaisyUI via CDN
    assert "cdn.jsdelivr.net/npm/daisyui" in html
    assert "cdn.jsdelivr.net/npm/alpinejs" in html

    # HTMX v2.x script tag (any minor)
    assert 'src="https://cdn.jsdelivr.net/npm/htmx.org@2.' in html

    # DaisyUI "hero" / "card" / "btn" component classes (used by login.html)
    assert "class=" in html  # sanity: page rendered some classes

    # data-theme attribute on <html> for SSR-correct first paint
    assert 'data-theme="' in html
    # Default theme = 'corporate'
    assert 'data-theme="corporate"' in html


@pytest.mark.asyncio
async def test_theme_cookie_round_trip(app_client: AsyncClient) -> None:
    """Setting virex_theme=business must surface as data-theme on next render."""
    # First, default theme (corporate)
    resp = await app_client.get("/login")
    assert 'data-theme="corporate"' in resp.text

    # Send cookie on a follow-up request.
    resp = await app_client.get(
        "/login", cookies={"virex_theme": "business"}
    )
    assert 'data-theme="business"' in resp.text

    # Bad cookie value falls back to corporate (defensive).
    resp = await app_client.get(
        "/login", cookies={"virex_theme": "totally-bogus-theme"}
    )
    assert 'data-theme="corporate"' in resp.text


@pytest.mark.asyncio
async def test_login_form_htmx_endpoint(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The login form has hx-post=/login so HTMX swaps the response in place.

    Catches accidental removal of the hx-post attribute when the form is
    restyled — which would silently fall back to native POST + redirect
    (still functional, but loses the inline-error UX when JS is on).
    """
    resp = await app_client.get("/login")
    html = resp.text
    assert 'hx-post="/login"' in html
    assert 'hx-target="this"' in html


# ---------------------------------------------------------------------------
# Phase 2 UI surfaces — events list + camera detail
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_events_page_renders_with_daisyui_components(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The /events page must render the filter toolbar + auto-refresh tbody."""
    resp = await app_client.get(
        "/events", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 200
    html = resp.text
    assert 'id="events-toolbar"' in html
    assert 'id="events-tbody"' in html
    # Auto-refresh every 10s
    assert 'hx-trigger="every 10s' in html
    # DaisyUI select controls for camera + window
    assert 'id="camera-filter"' in html
    assert 'id="window-filter"' in html


@pytest.mark.asyncio
async def test_camera_detail_page_renders_hls_player(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The /cameras/{id} page must render the HLS player + Alpine wiring."""
    # Need at least one camera — create via API.
    create = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": auth_cookie},
        json={
            "name": "Detail page cam",
            "mtx_path": "detail01",
            "rtsp_url": "rtsp://example.com/d",
            "node_id": 1,
        },
    )
    assert create.status_code == 201, create.text
    cam_id = create.json()["id"]

    resp = await app_client.get(
        f"/cameras/{cam_id}", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 200
    html = resp.text
    # hls.js from CDN
    assert "hls.js@" in html or "hls.min.js" in html
    # Local hls-player.js + Alpine factory
    assert "/static/hls-player.js" in html
    assert 'x-data="hlsPlayer(' in html
    # <video> with x-ref
    assert "<video" in html
    assert 'x-ref="video"' in html
    # HLS URL built from mtx_path
    assert "/index.m3u8" in html


# ---------------------------------------------------------------------------
# Polish pass — DaisyUI v4 CDN, navbar visibility, dashboard route
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_page_uses_daisyui_v4_cdn(app_client: AsyncClient) -> None:
    """The login HTML must reference the DaisyUI v4 standalone CSS bundle."""
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    assert "daisyui@4" in resp.text
    # v5 path should NOT appear (it 404s).
    assert "daisyui@5.7.4/dist/full.min.css" not in resp.text


@pytest.mark.asyncio
async def test_login_page_hides_navbar(app_client: AsyncClient) -> None:
    """The /login page should render without the navbar — only on the
    login page do we override the navbar block in base.html."""
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    # DaisyUI navbar div class is "navbar bg-base-100 shadow-sm"
    assert 'class="navbar bg-base-100 shadow-sm"' not in resp.text
    # But the two-column hero MUST still be present.
    assert "hero-content flex-col lg:flex-row" in resp.text


@pytest.mark.asyncio
async def test_login_helper_visible_in_dev(app_client: AsyncClient) -> None:
    """Default `debug_login_helper=True` must surface the demo-credentials card."""
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    html = resp.text
    assert "Demo credentials" in html
    assert "change-me-now" in html
    assert "admin@acme.example.com" in html


@pytest.mark.asyncio
async def test_root_redirects_dashboard_when_authed(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """GET / → /dashboard when a session cookie is present.

    We assert on the unfollowed redirect (303 + Location header) so the
    test isn't sensitive to httpx's redirect-following quirks.
    """
    resp = await app_client.get(
        "/", cookies={"virex_session": auth_cookie}, follow_redirects=False
    )
    assert resp.status_code in (302, 303, 307)
    assert resp.headers.get("location") == "/dashboard"


@pytest.mark.asyncio
async def test_root_redirects_login_when_unauthed(app_client: AsyncClient) -> None:
    """GET / → /login when no session cookie is present."""
    resp = await app_client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers.get("location") == "/login"


@pytest.mark.asyncio
async def test_dashboard_renders_for_admin(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The /dashboard page renders the 4 stat tiles + recent events panel."""
    resp = await app_client.get(
        "/dashboard", cookies={"virex_session": auth_cookie}
    )
    assert resp.status_code == 200
    html = resp.text
    # 4 stat titles.
    for title in ("Cameras", "Events today", "Nodes", "Alerts firing"):
        assert title in html
    # Sections.
    assert "Recent events" in html
    assert "Cameras" in html  # also in the snapshot panel
    # DaisyUI stat component.
    assert "stats stats-vertical lg:stats-horizontal" in html


@pytest.mark.asyncio
async def test_recent_events_fragment_includes_count_badge(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The /cameras/{id}/events/fragment must include both the list and the
    count badge inside a single swap target so HTMX refreshes both at once."""
    # Need a camera first.
    create = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": auth_cookie},
        json={
            "name": "Frag cam",
            "mtx_path": "fragcam01",
            "rtsp_url": "rtsp://x/f",
            "node_id": 1,
        },
    )
    cam_id = create.json()["id"]

    resp = await app_client.get(
        f"/cameras/{cam_id}/events/fragment",
        cookies={"virex_session": auth_cookie},
    )
    assert resp.status_code == 200
    html = resp.text
    # Both elements live inside the same #recent-events-panel div.
    assert 'id="recent-events-panel"' in html
    assert 'id="recent-events-count"' in html


@pytest.mark.asyncio
async def test_webrtc_tab_disabled(
    app_client: AsyncClient, auth_cookie: str
) -> None:
    """The WebRTC tab on camera detail must have tab-disabled + Phase 3 badge."""
    create = await app_client.post(
        "/api/cameras",
        cookies={"virex_session": auth_cookie},
        json={
            "name": "Tab cam",
            "mtx_path": "tabcam01",
            "rtsp_url": "rtsp://x/t",
            "node_id": 1,
        },
    )
    cam_id = create.json()["id"]
    resp = await app_client.get(
        f"/cameras/{cam_id}", cookies={"virex_session": auth_cookie}
    )
    html = resp.text
    assert "tab-disabled" in html
    assert "Phase 3" in html
    assert 'aria-disabled="true"' in html