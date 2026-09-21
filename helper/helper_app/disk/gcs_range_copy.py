"""Copy a GCS object (raw disk export) onto a PositionalWriter with parallel range GETs."""

from __future__ import annotations

import gzip
import logging
import tarfile
from typing import Callable, Iterator, Optional

from helper_app.disk.copy_common import CopyStats as GcsCopyStats
from helper_app.disk.copy_common import run_workers
from helper_app.disk.writer import PositionalWriter
from helper_app.gcp.client import GcpClient, GcpError

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class GcsCopyError(RuntimeError):
    pass


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
                    if not member.isfile() or member.size != expected_bytes:
                        raise GcsCopyError(
                            f"disk.raw logical size {member.size} does not match expected {expected_bytes} bytes"
                        )
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
        raise GcsCopyError(f"disk.raw streamed {stats.bytes_received} bytes, expected {expected_bytes}")
    return stats


def object_size(client: GcpClient, bucket: str, object_name: str) -> int:
    meta = client.object_head(bucket, object_name)
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise GcsCopyError(f"gs://{bucket}/{object_name} has no size")
    return size


def copy_parallel_ranges(
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
    if chunk_bytes <= 0 or total_bytes < 0:
        raise ValueError("chunk_bytes must be positive and total_bytes nonnegative")
    ranges = ((pos, min(total_bytes - 1, pos + chunk_bytes - 1))
              for pos in range(0, total_bytes, chunk_bytes))

    def one_range(start: int, end: int) -> None:
        if check_cancel:
            check_cancel()
        last: Optional[Exception] = None
        for _attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                data = client.object_get_range(bucket, object_name, start, end)
                if len(data) != end - start + 1:
                    raise GcsCopyError(f"range {start}-{end} returned {len(data)} bytes")
                writer.write_at(start, data)
                stats.add(len(data))
                if on_progress:
                    on_progress(len(data))
                return
            except (GcpError, GcsCopyError, OSError) as exc:
                last = exc
                with stats._lock:
                    stats.retries += 1
        raise GcsCopyError(f"range {start}-{end}: {last}") from last

    run_workers(ranges, lambda bounds: one_range(*bounds), workers=workers, check_cancel=check_cancel)
    return stats
