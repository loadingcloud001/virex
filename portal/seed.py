# SPDX-License-Identifier: Apache-2.0
"""Portal database seeder — runs after `alembic upgrade head`.

Idempotent: re-running is safe and produces the same end state. Use
`python -m portal.seed` (or `bootstrap.sh`) after every fresh migration
to bring a brand-new Postgres up to a usable pilot state.

What it does:
1. Creates the default tenant (`slug=<VIREX_DEFAULT_TENANT_SLUG>`).
2. Creates the first admin user from env (`VIREX_BOOTSTRAP_ADMIN_EMAIL`
   + `VIREX_BOOTSTRAP_ADMIN_PASSWORD`).
3. Creates a single Node row for the local edge.
4. Pre-populates cameras matching the current `state/workers.yaml`
   (so existing workers keep working through the migration).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import structlog
import yaml
from sqlalchemy import select

from core.config import settings
from core.database import SessionLocal, engine
from core.security import hash_password
from models import Camera, Node, Tenant, User

logger = structlog.get_logger(__name__)


# Mirror the regex in models/__init__.py — kept loose for the pilot.
_MTX_RE = re.compile(r"^[a-z0-9]+$")


async def _ensure_tenant(session) -> Tenant:
    slug = settings.default_tenant_slug
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(
            slug=slug,
            name="Acme Construction (default)",
            subdomain=slug,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        logger.info("seed_tenant_created", tenant_id=tenant.id, slug=slug)
    return tenant


async def _ensure_admin(session, tenant: Tenant) -> User:
    user = (
        await session.execute(
            select(User).where(
                User.tenant_id == tenant.id,
                User.email == settings.bootstrap_admin_email,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=settings.bootstrap_admin_email,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info("seed_admin_created", user_id=user.id, email=user.email)
    return user


async def _ensure_node(session, tenant: Tenant) -> Node:
    hostname = os.environ.get("VIREX_BOOTSTRAP_NODE_HOSTNAME", "edge-rtx4070-1")
    node = (
        await session.execute(
            select(Node).where(
                Node.tenant_id == tenant.id, Node.hostname == hostname
            )
        )
    ).scalar_one_or_none()
    if node is None:
        node = Node(
            tenant_id=tenant.id,
            hostname=hostname,
            tailscale_ip=os.environ.get(
                "VIREX_BOOTSTRAP_NODE_TS_IP", "127.0.0.1"
            ),
            max_cameras=int(os.environ.get("VIREX_BOOTSTRAP_NODE_MAX_CAMERAS", "8")),
            status="pending",
        )
        session.add(node)
        await session.commit()
        await session.refresh(node)
        logger.info("seed_node_created", node_id=node.id, hostname=hostname)
    return node


async def _ensure_cameras_from_workers_yaml(session, tenant: Tenant, node: Node) -> int:
    """Read state/workers.yaml and create Camera rows for each entry."""
    yaml_path = Path(
        os.environ.get(
            "VIREX_BOOTSTRAP_WORKERS_YAML",
            "/home/loadingcloud001/virex/deploy/edge/state/workers.yaml",
        )
    )
    if not yaml_path.is_file():
        logger.info("seed_workers_yaml_missing", path=str(yaml_path))
        return 0

    raw = yaml_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    cameras_data = data.get("cameras", [])
    if not cameras_data:
        logger.info("seed_workers_yaml_empty", path=str(yaml_path))
        return 0

    created = 0
    for idx, c in enumerate(cameras_data, start=1):
        mtx = c.get("mtx_path")
        if not mtx or not _MTX_RE.match(mtx):
            logger.warning("seed_camera_skipped_invalid_mtx_path", mtx=mtx)
            continue

        existing = (
            await session.execute(
                select(Camera).where(Camera.mtx_path == mtx)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        cam = Camera(
            tenant_id=tenant.id,
            node_id=node.id,
            name=c.get("name") or mtx,
            location=c.get("location"),
            rtsp_url=c.get("source_rtsp", ""),
            mtx_path=mtx,
            status="active",
            recording_enabled=c.get("record", True),
            retention_days=int(c.get("retention_days", 7)),
        )
        session.add(cam)
        created += 1
        logger.info("seed_camera_created", camera_id=idx, mtx_path=mtx)

    if created:
        await session.commit()
        # Bump config_version so edge-agent picks up the change on its
        # next pull (in case edge-agent is running and watching).
        from sqlalchemy import update as _update
        await session.execute(
            _update(Node)
            .where(Node.id == node.id)
            .values(current_config_version=Node.current_config_version + 1)
        )
        await session.commit()

    return created


async def main() -> int:
    """Run the full seed flow."""
    async with SessionLocal() as session:
        tenant = await _ensure_tenant(session)
        await _ensure_admin(session, tenant)
        node = await _ensure_node(session, tenant)
        cameras_created = await _ensure_cameras_from_workers_yaml(session, tenant, node)

    print(
        f"\n✅ Seed complete:\n"
        f"   tenant_slug = {tenant.slug} (id={tenant.id})\n"
        f"   admin email = {settings.bootstrap_admin_email}\n"
        f"   admin password = {settings.bootstrap_admin_password!r}\n"
        f"   node id = {node.id} (hostname={node.hostname})\n"
        f"   cameras created = {cameras_created}\n"
    )
    return 0


if __name__ == "__main__":
    from core.database import Base
    import models  # noqa: F401

    # Run the seeder.
    sys.exit(asyncio.run(main()))