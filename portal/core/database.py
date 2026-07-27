# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy 2.0 async database layer for the portal."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by portal models."""


engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency yielding an async session per request."""
    async with SessionLocal() as session:
        yield session


# Module-level lazy create hook for tests / dev bootstrap. Production runs
# Alembic migrations instead.
async def create_all_for_dev() -> None:
    import models  # noqa: F401, PLC0415

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


__all__: tuple[str, ...] = ("Base", "engine", "SessionLocal", "get_db", "create_all_for_dev")
