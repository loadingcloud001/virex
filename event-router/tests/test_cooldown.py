# SPDX-License-Identifier: Apache-2.0
"""Cooldown tests with fakeredis."""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fake_redis

from src.cooldown import (
    COOLDOWN_WINDOW_SEC,
    acquire_cooldown,
    cooldown_key,
    release_cooldown,
)


@pytest.fixture
async def redis() -> fake_redis.FakeRedis:
    client = fake_redis.FakeRedis()
    yield client
    await client.aclose()


async def test_first_call_acquires() -> None:
    client = fake_redis.FakeRedis()
    ok = await acquire_cooldown(
        client, tenant_id=1, camera_id=2, label="person"
    )
    assert ok is True
    await client.aclose()


async def test_second_call_blocked() -> None:
    client = fake_redis.FakeRedis()
    assert await acquire_cooldown(client, tenant_id=1, camera_id=2, label="person")
    assert not await acquire_cooldown(client, tenant_id=1, camera_id=2, label="person")
    await client.aclose()


async def test_release_allows_reacquire() -> None:
    client = fake_redis.FakeRedis()
    assert await acquire_cooldown(client, tenant_id=1, camera_id=2, label="person")
    await release_cooldown(client, tenant_id=1, camera_id=2, label="person")
    assert await acquire_cooldown(client, tenant_id=1, camera_id=2, label="person")
    await client.aclose()


async def test_different_key_independent() -> None:
    client = fake_redis.FakeRedis()
    assert await acquire_cooldown(client, tenant_id=1, camera_id=2, label="person")
    # Same tenant, different camera → independent cooldown.
    assert await acquire_cooldown(client, tenant_id=1, camera_id=3, label="person")
    await client.aclose()


def test_default_window_is_30s() -> None:
    assert COOLDOWN_WINDOW_SEC == 30


def test_key_format() -> None:
    # Style check — keep wire format stable.
    assert cooldown_key(1, 5, "person") == "cd:1:5:person"
