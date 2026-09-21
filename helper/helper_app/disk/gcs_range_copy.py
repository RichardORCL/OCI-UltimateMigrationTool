"""Copy a GCS object (raw disk export) onto a PositionalWriter with parallel range GETs."""

from __future__ import annotations

import gzip
import logging
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from helper_app.disk.writer import PositionalWriter
from helper_app.gcp.client import GcpClient, GcpError

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class GcsCopyError(RuntimeError):
    pass


@dataclass
class GcsCopyStats:
    bytes_received: int = 0
    bytes_written: int = 0
    chunks_written: int = 0
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, received: int, written: Optional[int] = None) -> None:
        if written is None:
            written = received
        with self._lock:
            self.bytes_received += received
            self.bytes_written += written
            if written:
                self.chunks_written += 1


class _StreamByteReader:
    """File-like reader over an httpx streaming response (for gzip/tar export objects)."""

    def __init__(self, chunks: Iterator[bytes]):
        self._iter = iter(chunks)
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            parts = [self._buf] if self._buf else []
            self._buf = b""
            parts.extend(self._iter)
            return b"".join(parts)
        while len(self._buf) < size:
            try:
                self._buf += next(self._iter)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


def copy_from_gcs_export_tarball(
    client: GcpClient,
    bucket: str,
    object_name: str,
    writer: PositionalWriter,
    *,
    expected_bytes: int,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> GcsCopyStats:
    """Stream a Compute Engine export (``.tar.gz`` containing ``disk.raw``) onto a block device."""
    stats = GcsCopyStats()
    url = client.object_media_url(bucket, object_name)
    headers = client._headers()
    with client.http.stream("GET", url, headers=headers) as resp:
        if resp.status_code >= 400:
            raise GcsCopyError(f"download gs://{bucket}/{object_name}: HTTP {resp.status_code}")
        reader = _StreamByteReader(resp.iter_bytes())
        with gzip.GzipFile(fileobj=reader, mode="rb") as gz:
            with tarfile.open(fileobj=gz, mode="r|") as tar:
                found = False
                for member in tar:
                    base = member.name.rsplit("/", 1)[-1]
                    if base != "disk.raw":
                        continue
                    found = True
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise GcsCopyError(f"disk.raw in gs://{bucket}/{object_name} is not readable")
                    pos = 0
                    while pos < expected_bytes:
                        if check_cancel:
                            check_cancel()
                        to_read = min(chunk_bytes, expected_bytes - pos)
                        chunk = extracted.read(to_read)
                        if not chunk:
                            break
                        # OCI volumes start zeroed; skip unallocated / unused space (same as VMDK skip_zero_grains).
                        wrote = 0
                        if any(chunk):
                            writer.write_at(pos, chunk)
                            wrote = len(chunk)
                        pos += len(chunk)
                        stats.add(len(chunk), written=wrote)
                        if on_progress:
                            on_progress(len(chunk))
                    break
                if not found:
                    raise GcsCopyError(f"export tarball gs://{bucket}/{object_name} has no disk.raw member")
    if stats.bytes_received < expected_bytes:
        log.warning(
            "gs://%s/%s disk.raw streamed %s bytes, expected %s (trailing zeros may be implicit)",
            bucket, object_name, stats.bytes_received, expected_bytes,
        )
    return stats


def object_size(client: GcpClient, bucket: str, object_name: str) -> int:
    meta = client.object_head(bucket, object_name)
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise GcsCopyError(f"gs://{bucket}/{object_name} has no size")
    return size


def copy_sequential(
    client: GcpClient,
    bucket: str,
    object_name: str,
    writer: PositionalWriter,
    *,
    total_bytes: int,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    workers: int = 4,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> GcsCopyStats:
    stats = GcsCopyStats()
    ranges: list[tuple[int, int]] = []
    pos = 0
    while pos < total_bytes:
        end = min(total_bytes - 1, pos + chunk_bytes - 1)
        ranges.append((pos, end))
        pos = end + 1

    def one_range(start: int, end: int) -> None:
        if check_cancel:
            check_cancel()
        last: Optional[Exception] = None
        for attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                data = client.object_get_range(bucket, object_name, start, end)
                writer.write_at(start, data)
                stats.add(len(data))
                if on_progress:
                    on_progress(len(data))
                return
            except (GcpError, OSError) as exc:
                last = exc
                with stats._lock:
                    stats.retries += 1
        raise GcsCopyError(f"range {start}-{end}: {last}") from last

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(one_range, s, e) for s, e in ranges]
        for f in as_completed(futs):
            f.result()
    return stats
