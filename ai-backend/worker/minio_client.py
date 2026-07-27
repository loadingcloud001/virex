# SPDX-License-Identifier: Apache-2.0
"""Thin MinIO client for snapshot uploads.

Used by the per-camera worker to push JPEG snapshots over Tailscale to the
VPS MinIO bucket. Failures are retried with exponential backoff — if all
retries fail, the worker logs the drop and continues (Phase 2 will add a
local spool to recover these).
"""

from __future__ import annotations

import io

import structlog
from minio import Minio
from minio.error import S3Error
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_RETRY_STOP: int = 3
DEFAULT_RETRY_WAIT_MS: int = 500
DEFAULT_RETRY_MAX_WAIT_MS: int = 4000
JPEG_CONTENT_TYPE: str = "image/jpeg"


class SnapshotUploadError(RuntimeError):
    """Raised when all retries to upload a snapshot have failed."""


class SnapshotUploader:
    """Idempotent wrapper over `minio.Minio.put_object`.

    Constructed once per worker; `upload()` is async-safe (no shared
    mutable state) so a single client can serve multiple camera loops.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = True,
        retry_attempts: int = DEFAULT_RETRY_STOP,
        region: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._retry_attempts = retry_attempts
        # Strip any scheme prefix from the endpoint — Minio client expects
        # bare host[:port]. Caller (worker.yaml / .env) supplies the scheme
        # via `minio_secure` separately.
        bare_endpoint = endpoint.split("://", 1)[-1]
        self._client = Minio(
            endpoint=bare_endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
        )
        # Force virtual-host style addressing for cloud providers whose
        # hostnames are not in the SDK's auto-detect list (Tencent COS
        # `*.myqcloud.com`, Aliyun OSS `*.aliyuncs.com` is auto, AWS S3 is
        # auto; Cloudflare R2 needs it too). Minio's default is path
        # style which COS rejects with `PathStyleDomainForbidden`.
        self._client._base_url.virtual_style_flag = True
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Best-effort check + create. Never raises: the bucket may be
        pre-provisioned by ops (Tencent COS, AWS S3, etc.), and we don't
        want the worker to crash-loop on startup just because the bucket
        policy forbids create. Failures are logged and re-attempted on
        every `upload()`.
        """
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("minio_bucket_created", bucket=self._bucket)
        except S3Error as e:
            logger.warning(
                "minio_bucket_check_failed",
                error=str(e),
                bucket=self._bucket,
                note="continuing; uploads will surface the error per call",
            )

    def upload(self, *, object_key: str, jpeg_bytes: bytes) -> int:
        """Upload JPEG bytes to `object_key`; returns byte count.

        Raises:
            SnapshotUploadError: if all retries fail.
        """
        stream = io.BytesIO(jpeg_bytes)
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self._retry_attempts),
                wait=wait_exponential(
                    multiplier=DEFAULT_RETRY_WAIT_MS / 1000,
                    max=DEFAULT_RETRY_MAX_WAIT_MS / 1000,
                ),
                retry=retry_if_exception_type(S3Error),
                reraise=True,
            ):
                with attempt:
                    stream.seek(0)
                    self._client.put_object(
                        bucket_name=self._bucket,
                        object_name=object_key,
                        data=stream,
                        length=len(jpeg_bytes),
                        content_type=JPEG_CONTENT_TYPE,
                    )
        except S3Error as e:
            logger.error(
                "snapshot_upload_failed",
                object_key=object_key,
                error=str(e),
                attempts=self._retry_attempts,
            )
            raise SnapshotUploadError(str(e)) from e

        logger.info(
            "snapshot_uploaded",
            object_key=object_key,
            size=len(jpeg_bytes),
        )
        return len(jpeg_bytes)


def snapshot_key(tenant_id: int, event_uuid: str) -> str:
    """Build the canonical MinIO object key for a snapshot."""
    return f"tenants/{tenant_id}/snapshots/{event_uuid}.jpg"


def clip_key(tenant_id: int, event_id: int) -> str:
    """Build the canonical MinIO object key for an event clip."""
    return f"tenants/{tenant_id}/clips/{event_id}.mp4"


# Used by clip_builder — kept here so the key shape is in one place.
def make_retry_decorator(*, attempts: int = DEFAULT_RETRY_STOP) -> Retrying:
    """Factory for a tenacity retry decorator with the worker's defaults.

    Returning a configured `Retrying` instance (vs. `retry()` decorator)
    keeps the call sites readable when used inside `async` code where we
    need `for attempt in Retrying(...)`.
    """
    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(
            multiplier=DEFAULT_RETRY_WAIT_MS / 1000,
            max=DEFAULT_RETRY_MAX_WAIT_MS / 1000,
        ),
        retry=retry_if_exception_type(S3Error),
        reraise=True,
    )
