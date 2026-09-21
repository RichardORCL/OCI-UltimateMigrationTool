"""Streaming uploads to Object Storage (multipart for large VMDKs, put_object for small XML)."""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

log = logging.getLogger(__name__)

PART_SIZE = 128 * 1024 * 1024


def put_small_object(clients, namespace: str, bucket: str, object_name: str, body: bytes) -> None:
    clients.object_storage.put_object(namespace, bucket, object_name, body)


class MultipartObjectWriter:
    """File-like writer that buffers parts and uploads them with the Object Storage API.

    Tracks ``bytes_written`` and a running SHA-256 of every byte written (used for the
    OVF manifest).  A payload smaller than one part is stored with ``put_object``.
    """

    def __init__(
        self,
        clients,
        namespace: str,
        bucket: str,
        object_name: str,
        part_size: int = PART_SIZE,
    ):
        if part_size < 1:
            raise ValueError("part_size must be at least 1")
        self.c = clients
        self.namespace = namespace
        self.bucket = bucket
        self.object_name = object_name
        self.part_size = part_size
        self.bytes_written = 0
        self.upload_id: Optional[str] = None
        self._buf = bytearray()
        self._sha = hashlib.sha256()
        self._parts: list[tuple[int, str]] = []
        self._part_num = 1
        self._closed = False
        self._committed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write to a closed MultipartObjectWriter")
        if not data:
            return 0
        if isinstance(data, memoryview):
            data = data.tobytes()
        self._sha.update(data)
        self._buf.extend(data)
        self.bytes_written += len(data)
        while len(self._buf) >= self.part_size:
            self._ensure_upload()
            self._flush_part(bytes(self._buf[: self.part_size]))
            del self._buf[: self.part_size]
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.upload_id is None:
                put_small_object(self.c, self.namespace, self.bucket, self.object_name, bytes(self._buf))
                self._buf.clear()
                self._committed = True
                return
            if self._buf:
                self._flush_part(bytes(self._buf))
                self._buf.clear()
            import oci.object_storage.models as M

            details = M.CommitMultipartUploadDetails(
                parts_to_commit=[
                    M.CommitMultipartUploadPartDetails(part_num=num, etag=etag) for num, etag in self._parts
                ]
            )
            self.c.object_storage.commit_multipart_upload(
                self.namespace, self.bucket, self.object_name, self.upload_id, details
            )
            self._committed = True
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        if not self.upload_id or self._committed:
            return
        try:
            self.c.object_storage.abort_multipart_upload(
                self.namespace, self.bucket, self.object_name, self.upload_id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("abort multipart %s/%s failed: %s", self.bucket, self.object_name, exc)
        self.upload_id = None

    @property
    def sha256_hex(self) -> str:
        return self._sha.hexdigest()

    def _ensure_upload(self) -> None:
        if self.upload_id:
            return
        import oci.object_storage.models as M

        details = M.CreateMultipartUploadDetails(object=self.object_name)
        resp = self.c.object_storage.create_multipart_upload(self.namespace, self.bucket, details)
        self.upload_id = resp.data.upload_id

    def _flush_part(self, body: bytes) -> None:
        resp = self.c.object_storage.upload_part(
            self.namespace, self.bucket, self.object_name, self.upload_id, self._part_num, body
        )
        etag = None
        data = getattr(resp, "data", None)
        if data is not None:
            etag = getattr(data, "etag", None)
        headers = getattr(resp, "headers", None) or {}
        if not etag:
            etag = headers.get("etag") or headers.get("ETag") or headers.get("opc-content-md5")
        if not etag:
            etag = f"part-{self._part_num}"
        self._parts.append((self._part_num, str(etag).strip('"')))
        self._part_num += 1
