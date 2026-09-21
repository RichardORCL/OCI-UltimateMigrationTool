"""AWS Signature Version 4 for REST and Query APIs over httpx (no boto3)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k = hmac.new(k, region.encode("utf-8"), hashlib.sha256).digest()
    k = hmac.new(k, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k, b"aws4_request", hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    parts = []
    for item in query.split("&"):
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
        else:
            k, v = item, ""
        parts.append((k, v))
    parts.sort()
    return "&".join(f"{k}={v}" for k, v in parts)


def sign_headers(
    method: str,
    url: str,
    body: bytes,
    *,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str,
    now: datetime | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the headers that must be sent (including ``Authorization`` and ``X-Amz-Date``)."""
    now = now or datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    parsed = urlsplit(url)
    host = parsed.netloc
    path = parsed.path or "/"
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    headers = {"host": host, "x-amz-date": amzdate, "x-amz-content-sha256": payload_hash}
    if extra_headers:
        headers.update({k.lower(): v for k, v in extra_headers.items()})
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([
        method.upper(),
        quote(path, safe="/~"),
        _canonical_query(parsed.query),
        canonical_headers,
        signed,
        payload_hash,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        scope,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    sig = hmac.new(signing_key(secret_access_key, datestamp, region, service),
                   string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}")
    out = {"X-Amz-Date": amzdate, "X-Amz-Content-Sha256": payload_hash, "Authorization": auth}
    if extra_headers:
        out.update(extra_headers)
    return out
