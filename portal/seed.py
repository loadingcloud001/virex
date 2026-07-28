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
5. Optional: inserts demo events with `event_uuid` prefix `demo-` so the
   `/events` UI can be exercised without a running detector. Controlled
   via `--demo-events` CLI flag or `VIREX_BOOTSTRAP_DEMO_EVENTS` env
   (default: True in dev).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
import yaml
from sqlalchemy import delete, select

from core.config import settings
from core.database import SessionLocal, engine
from core.security import hash_password
from models import Camera, Event, Node, Tenant, User

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


async def _ensure_demo_events(
    session, tenant: Tenant, count: int = 20
) -> tuple[int, int]:
    """Insert demo events across the tenant's first active camera.

    Idempotent: purges prior `demo-` rows on each run, so re-running
    produces a stable demo set. Bumps `current_config_version` is
    not needed (events don't change edge state).

    Returns `(purged, inserted)`.
    """
    # First, purge any prior demo events so re-runs are deterministic.
    purged = (
        await session.execute(
            delete(Event).where(
                Event.tenant_id == tenant.id,
                Event.event_uuid.like("demo-%"),
            )
        )
    ).rowcount or 0

    # Find the first active camera in this tenant.
    cam = (
        await session.execute(
            select(Camera)
            .where(Camera.tenant_id == tenant.id, Camera.status == "active")
            .order_by(Camera.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if cam is None:
        logger.info("seed_demo_events_skipped_no_camera")
        return purged, 0

    rng = random.Random(42)  # deterministic across runs
    class_labels = ["person", "vehicle", "dog", "package", "bicycle"]
    now = datetime.now(timezone.utc)

    inserted = 0
    for i in range(count):
        event_time = now - timedelta(minutes=rng.randint(1, 360))
        class_label = rng.choice(class_labels)
        score = round(rng.uniform(0.45, 0.97), 4)
        # bbox is "[x,y,w,h]" normalized to image dims (640x480)
        x = round(rng.uniform(0.1, 0.7), 4)
        y = round(rng.uniform(0.1, 0.7), 4)
        w = round(rng.uniform(0.05, 0.4), 4)
        h = round(rng.uniform(0.1, 0.5), 4)
        bbox = f"[{x},{y},{w},{h}]"
        ev = Event(
            tenant_id=tenant.id,
            camera_id=cam.id,
            event_uuid=f"demo-{cam.mtx_path}-{i:03d}",
            class_label=class_label,
            score=score,
            bbox=bbox,
            snapshot_url=None,
            clip_url=None,
            clip_built=False,
            event_time=event_time,
        )
        session.add(ev)
        inserted += 1

    if inserted:
        await session.commit()

    logger.info(
        "seed_demo_events_done",
        camera_id=cam.id,
        mtx_path=cam.mtx_path,
        purged=purged,
        inserted=inserted,
    )
    return purged, inserted


async def main() -> int:
    """Run the full seed flow."""
    parser = argparse.ArgumentParser(description="Portal seeder")
    parser.add_argument(
        "--demo-events",
        dest="demo_events",
        action="store_true",
        default=None,
        help="Insert demo events (default: env VIREX_BOOTSTRAP_DEMO_EVENTS, else True in dev)",
    )
    parser.add_argument(
        "--no-demo-events",
        dest="demo_events",
        action="store_false",
        help="Skip demo events",
    )
    args = parser.parse_args()

    if args.demo_events is None:
        demo_events_env = os.environ.get("VIREX_BOOTSTRAP_DEMO_EVENTS", "1")
        demo_events = demo_events_env.lower() not in {"0", "false", "no"}
    else:
        demo_events = args.demo_events

    async with SessionLocal() as session:
        tenant = await _ensure_tenant(session)
        await _ensure_admin(session, tenant)
        node = await _ensure_node(session, tenant)
        cameras_created = await _ensure_cameras_from_workers_yaml(session, tenant, node)
        demo_purged, demo_inserted = (0, 0)
        if demo_events:
            demo_purged, demo_inserted = await _ensure_demo_events(session, tenant)

    print(
        f"\n✅ Seed complete:\n"
        f"   tenant_slug = {tenant.slug} (id={tenant.id})\n"
        f"   admin email = {settings.bootstrap_admin_email}\n"
        f"   admin password = {settings.bootstrap_admin_password!r}\n"
        f"   node id = {node.id} (hostname={node.hostname})\n"
        f"   cameras created = {cameras_created}\n"
        f"   demo events = {demo_inserted} (purged {demo_purged})\n"
    )
    return 0


if __name__ == "__main__":
    from core.database import Base
    import models  # noqa: F401

    # Run the seeder.
    sys.exit(asyncio.run(main()))