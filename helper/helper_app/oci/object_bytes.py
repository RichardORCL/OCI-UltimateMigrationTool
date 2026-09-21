"""Read full Object Storage object bodies (OCI SDK response shapes differ)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helper_app.oci.clients import OciClients

CHUNK = 1024 * 1024


def open_object_stream(data) -> io.IOBase:
    """Return a readable file-like for an OCI ``GetObject`` response ``data`` body."""
    if hasattr(data, "raw") and data.raw is not None:
        stream = getattr(data.raw, "stream", None)
        if stream is not None:
            if callable(stream) and not hasattr(stream, "read"):
                stream = stream()
            if hasattr(stream, "read"):
                return stream  # type: ignore[return-value]
        if hasattr(data.raw, "read"):
            return data.raw  # type: ignore[return-value]

    content = getattr(data, "content", None)
    if content is not None:
        if isinstance(content, (bytes, bytearray)):
            return io.BytesIO(content)
        if isinstance(content, io.IOBase):
            return content
        if isinstance(content, str):
            return io.BytesIO(content.encode())

    return io.BytesIO(b"")


def read_object_bytes(c: OciClients, namespace: str, bucket: str, object_name: str) -> bytes:
    """Download an object from a bucket into memory."""
    resp = c.object_storage.get_object(namespace, bucket, object_name)
    data = resp.data
    stream = open_object_stream(data)
    parts: list[bytes] = []
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        parts.append(block if isinstance(block, (bytes, bytearray)) else bytes(block))
    if parts:
        return b"".join(parts)

    text = getattr(data, "text", None)
    if text:
        return text.encode() if isinstance(text, str) else bytes(text)

    return b""
