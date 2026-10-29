"""Copy a raw Prism image download onto a ``PositionalWriter``.

A cloned AHV disk image is raw and downloads at its full size, zeros included. With a region map
from Prism (``nutanix/regions.py``) only the data extents are fetched. All-zero chunks are read but
not written, because a fresh OCI volume already reads as zeros. When the server answers range
requests, chunks are fetched by parallel workers and a failed chunk is retried on its own;
otherwise the image is streamed once from the start.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterator, Optional

import httpx

from helper_app.disk.copy_common import CopyStats, run_workers
from helper_app.disk.writer import PositionalWriter
from helper_app.nutanix.client import NutanixError

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class NutanixCopyError(RuntimeError):
    pass


def is_zero(data: bytes) -> bool:
    return not data or data == bytes(len(data))


def split_chunks(size: int, chunk_bytes: int) -> list[tuple[int, int]]:
    return [(off, min(chunk_bytes, size - off)) for off in range(0, size, chunk_bytes)]


def split_extents(extents: list[tuple[int, int]], chunk_bytes: int) -> list[tuple[int, int]]:
    return [(off + rel, min(chunk_bytes, length - rel))
            for off, length in extents for rel in range(0, length, chunk_bytes)]


def copy_ranges(
    fetch: Callable[[int, int], bytes],
    size: int,
    writer: PositionalWriter,
    *,
    extents: Optional[list[tuple[int, int]]] = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    workers: int = 4,
    retries: int = DEFAULT_RETRIES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CopyStats:
    """Fetch ``size`` bytes (or only ``extents``) with ``fetch(offset, length)`` range reads and write
    the non-zero chunks."""
    stats = CopyStats()
    chunks = split_chunks(size, chunk_bytes) if extents is None else split_extents(extents, chunk_bytes)
    if not chunks:
        return stats

    def fetch_chunk(off: int, length: int) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                data = fetch(off, length)
                if len(data) != length:
                    raise NutanixCopyError(f"range {off}-{off + length - 1} returned {len(data)} bytes, "
                                           f"expected {length}")
                return data
            except (NutanixCopyError, NutanixError, httpx.HTTPError, OSError) as exc:
                last = exc
            if attempt < retries:
                with stats._lock:
                    stats.retries += 1
                sleep(min(10.0, 1.0 * attempt))
        raise NutanixCopyError(f"{last}") from last

    def process(bounds: tuple[int, int]) -> None:
        off, length = bounds
        data = fetch_chunk(off, length)
        if is_zero(data):
            stats.add(len(data), 0)
        else:
            writer.write_at(off, data)
            stats.add(len(data))
        if on_progress:
            on_progress(len(data))

    run_workers(chunks, process, workers=min(max(1, workers), len(chunks)), check_cancel=check_cancel)
    return stats


def copy_stream(
    open_stream: Callable[[int], Iterator[bytes]],
    size: int,
    writer: PositionalWriter,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    resumable: bool = False,
    retries: int = DEFAULT_RETRIES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CopyStats:
    """Read ``open_stream(offset)`` sequentially and write the non-zero ``chunk_bytes`` blocks.

    With ``resumable`` a broken stream is reopened at the last block boundary; otherwise the error
    is raised and the caller retries the whole disk.
    """
    stats = CopyStats()
    offset = 0
    attempt = 0
    while True:
        buf = bytearray()
        try:
            for piece in open_stream(offset):
                if check_cancel:
                    check_cancel()
                buf.extend(piece)
                while len(buf) >= chunk_bytes:
                    offset = _flush(writer, offset, bytes(buf[:chunk_bytes]), stats, on_progress)
                    del buf[:chunk_bytes]
            if buf:
                offset = _flush(writer, offset, bytes(buf), stats, on_progress)
        except (NutanixError, httpx.HTTPError, OSError) as exc:
            attempt += 1
            if not resumable or attempt >= retries:
                raise NutanixCopyError(f"image download failed at byte {offset:,}: {exc}") from exc
            stats.retries += 1
            log.info("image download broke at byte %s (%s); resuming", offset, exc)
            sleep(min(10.0, 1.0 * attempt))
            continue
        if size and offset < size:
            raise NutanixCopyError(f"image download ended at byte {offset:,} of {size:,}")
        return stats


def _flush(writer: PositionalWriter, offset: int, data: bytes, stats: CopyStats,
           on_progress: Optional[Callable[[int], None]]) -> int:
    if is_zero(data):
        stats.add(len(data), 0)
    else:
        writer.write_at(offset, data)
        stats.add(len(data))
    if on_progress:
        on_progress(len(data))
    return offset + len(data)
