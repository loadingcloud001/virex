# SPDX-License-Identifier: Apache-2.0
"""Alembic environment for the Virex portal.

Loads `VIREX_DATABASE_URL` (and any other settings) from
`core.config.settings` and configures the Alembic context against the
same SQLAlchemy `Base.metadata` used by the FastAPI app — this means
`alembic revision --autogenerate` sees the same model classes the app
sees at runtime.

The portal container's working directory is `/app`, and `alembic.ini`
sits one level up at `/alembic.ini`. The `script_location` in
alembic.ini is `portal/migrations`, which is relative to the project
root, so we use the same root in env.py.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make `core` importable when alembic is invoked from the repo root.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # .../virex
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import settings  # noqa: E402
from core.database import Base  # noqa: E402
import models  # noqa: E402,F401  — registers all tables with Base.metadata

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Override sqlalchemy.url with the runtime setting. We swap the
# asyncpg scheme to the sync psycopg2/psycopg driver because alembic
# runs migrations synchronously; async migrations are Phase 2.
#
# Accepted sync schemes (in order of preference):
#   postgresql+psycopg://...
#   postgresql+psycopg2://...
#   postgresql://...          (libpq, falls back to psycopg2 if installed)
_async_url = settings.database_url
_sync_url = (
    _async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    .replace("postgresql+asyncpg+psycopg2://", "postgresql+psycopg2://", 1)
)
config.set_main_option("sqlalchemy.url", _sync_url)

# Configure logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection).

    Used by `alembic upgrade head --sql` to generate SQL scripts.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()