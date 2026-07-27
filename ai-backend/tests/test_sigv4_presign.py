# SPDX-License-Identifier: Apache-2.0
"""Tests for SigV4 custom-host presigner."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from worker.sigv4_presign import sign_presigned_get


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def test_signs_against_custom_host(fixed_now: datetime) -> None:
    url = sign_presigned_get(
        access_key="AKID_test",
        secret_key="secret_test",
        region="ap-singapore",
        host="snapshots.example.com",
        bucket="virex-snapshots-1308927282",
        object_key="tenants/1/snapshots/abc.jpg",
        expires_sec=300,
        now=fixed_now,
    )
    assert url.startswith("https://snapshots.example.com/")
    assert "X-Amz-SignedHeaders=host" in url
    assert "X-Amz-Credential=AKID_test" in url
    # Region + date encoded in credential scope.
    assert "%2F20260726%2Fap-singapore%2Fs3%2Faws4_request" in url or (
        "/20260726/ap-singapore/s3/aws4_request" in url
    )
    # Signature is 64 hex chars.
    assert "X-Amz-Signature=" in url
    sig = url.split("X-Amz-Signature=", 1)[1]
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_deterministic_for_fixed_time(fixed_now: datetime) -> None:
    """Same input → same output (SigV4 is deterministic)."""
    kwargs = dict(
        access_key="AKID_x",
        secret_key="secret_x",
        region="us-east-1",
        host="cdn.example.com",
        bucket="bkt",
        object_key="k/v/1.jpg",
        expires_sec=60,
        now=fixed_now,
    )
    assert sign_presigned_get(**kwargs) == sign_presigned_get(**kwargs)


def test_special_chars_in_object_key(fixed_now: datetime) -> None:
    """Spaces, slashes, and unicode are URL-encoded in canonical URI."""
    url = sign_presigned_get(
        access_key="AKID_x",
        secret_key="secret_x",
        region="us-east-1",
        host="cdn.example.com",
        bucket="bkt",
        object_key="dir with spaces/中文.jpg",
        expires_sec=60,
        now=fixed_now,
    )
    # URL path segment encoding — %2F would be wrong; / should remain /
    assert "/dir%20with%20spaces/" in url
    # Chinese encoded as UTF-8 percent-encoded bytes
    assert "%E4%B8%AD%E6%96%87" in url


def test_different_hosts_produce_different_signatures(fixed_now: datetime) -> None:
    """Host is signed — changing host must invalidate signature."""
    base = dict(
        access_key="AKID_x",
        secret_key="secret_x",
        region="us-east-1",
        bucket="bkt",
        object_key="a.jpg",
        expires_sec=60,
        now=fixed_now,
    )
    raw = sign_presigned_get(host="a.example.com", **base)
    custom = sign_presigned_get(host="b.example.com", **base)
    # Same path, same query except X-Amz-Signature differs.
    raw_sig = raw.split("X-Amz-Signature=", 1)[1]
    custom_sig = custom.split("X-Amz-Signature=", 1)[1]
    assert raw_sig != custom_sig
