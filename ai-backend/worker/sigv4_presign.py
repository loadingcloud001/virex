# SPDX-License-Identifier: Apache-2.0
"""AWS SigV4 presigner for arbitrary endpoints.

The `minio` Python SDK only signs presigned URLs against the bucket's
endpoint host (`virex-snapshots-1308927282.cos.ap-singapore.myqcloud.com`).
Substituting the host to a custom domain (`snapshots.loadingtechnology.app`)
breaks the signature because `X-Amz-SignedHeaders=host` is included.

This module signs presigned URLs directly with SigV4, so we can choose
the host at signing time. Used by the portal in Phase 2; for v1
the worker still writes raw-key object paths and uses the standard
SDK presign against the COS endpoint.

Pattern follows the AWS SigV4 spec:
  https://docs.aws.amazon.com/general/latest/gr/sigv4-signed-request-examples.html
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus


def sign_presigned_get(
    *,
    access_key: str,
    secret_key: str,
    region: str,
    host: str,
    bucket: str,
    object_key: str,
    expires_sec: int = 300,
    service: str = "s3",
    now: datetime | None = None,
) -> str:
    """Build a presigned GET URL signed for `host` (any hostname).

    Args:
        access_key: COS SecretId / AWS Access Key Id.
        secret_key: COS SecretKey / AWS Secret Access Key.
        region: e.g. `ap-singapore` for COS Singapore.
        host: hostname to sign against. Use the public hostname the
            client will fetch from (e.g. `snapshots.loadingtechnology.app`).
        bucket: COS bucket name.
        object_key: Object key within the bucket.
        expires_sec: Validity of the URL in seconds.
        service: AWS service name (default `s3` for S3/COS).
        now: Override for tests.

    Returns:
        Full URL string ready to share.
    """
    if now is None:
        now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    canonical_uri = f"/{quote(object_key, safe='/')}"
    credential = (
        f"{access_key}/{date_stamp}/{region}/s3/aws4_request"
    )
    canonical_querystring = (
        f"X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={quote_plus(credential)}"
        f"&X-Amz-Date={amz_date}"
        f"&X-Amz-Expires={expires_sec}"
        f"&X-Amz-SignedHeaders=host"
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"

    canonical_request = (
        f"GET\n"
        f"{canonical_uri}\n"
        f"{canonical_querystring}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashed_canonical_request}"
    )

    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    scheme = "https"
    return (
        f"{scheme}://{host}{canonical_uri}"
        f"?{canonical_querystring}"
        f"&X-Amz-Signature={signature}"
    )


__all__: tuple[str, ...] = ("sign_presigned_get",)
