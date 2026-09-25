"""Copy allocated extents of an OLVM image-transfer download onto a ``PositionalWriter``.

``/extents`` lists the ranges that hold data. Zero extents are skipped (a fresh OCI volume
already reads as zeros). Each remaining range is fetched with an HTTP range request and
written at the same offset.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import httpx

from helper_app.disk.copy_common import CopyStats, run_workers
from helper_app.disk.writer import PositionalWriter
from helper_app.olvm.client import OlvmAuthError, OlvmError

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class ImageioCopyError(RuntimeError):
    pass


class ImageioAuthExpired(ImageioCopyError):
    """The image-transfer ticket was rejected; the caller may refresh it and retry."""


def allocated_ranges(extents: list[dict], limit: int) -> list[tuple[int, int]]:
    """Non-zero ``(offset, length)`` extents, clipped to ``limit`` bytes and merged when adjacent."""
    raw: list[tuple[int, int]] = []
    for extent in extents:
        if extent.get("zero") is True or extent.get("hole") is True:
            continue
        start = int(extent.get("start") or 0)
        length = int(extent.get("length") or 0)
        if length <= 0 or start >= limit:
            continue
        length = min(length, limit - start)
        raw.append((start, length))
    return _merge(raw)


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for off, length in sorted(ranges):
        if out and out[-1][0] + out[-1][1] >= off:
            last_off, last_len = out[-1]
            end = max(last_off + last_len, off + length)
            out[-1] = (last_off, end - last_off)
        else:
            out.append((off, length))
    return out


def split_chunks(ranges: list[tuple[int, int]], chunk_bytes: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    for off, length in ranges:
        while length > 0:
            n = min(length, chunk_bytes)
            chunks.append((off, n))
            off += n
            length -= n
    return chunks


def copy_extents(
    fetch: Callable[[int, int], bytes],
    ranges: list[tuple[int, int]],
    writer: PositionalWriter,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    workers: int = 4,
    retries: int = DEFAULT_RETRIES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    refresh: Optional[Callable[[], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CopyStats:
    """Download every range in ``chunk_bytes`` pieces and write it in place.

    ``fetch(offset, length)`` returns the bytes. ``refresh`` is called once when a read reports
    an expired ticket, then that chunk is retried.
    """
    stats = CopyStats()
    chunks = split_chunks(ranges, chunk_bytes)
    if not chunks:
        return stats
    stop = threading.Event()
    refresh_lock = threading.Lock()
    refreshed_at = [0.0]

    def refresh_ticket() -> None:
        if refresh is None:
            return
        with refresh_lock:
            if time.monotonic() - refreshed_at[0] < 30:
                return
            refresh()
            refreshed_at[0] = time.monotonic()

    def fetch_chunk(off: int, length: int) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            if stop.is_set():
                raise ImageioCopyError("aborted")
            try:
                data = fetch(off, length)
                if len(data) != length:
                    raise ImageioCopyError(f"range {off}-{off + length - 1} returned {len(data)} bytes, "
                                           f"expected {length}")
                return data
            except (ImageioAuthExpired, OlvmAuthError) as exc:
                last = exc
                refresh_ticket()
            except (ImageioCopyError, OlvmError, httpx.HTTPError, OSError) as exc:
                last = exc
            if attempt < retries:
                with stats._lock:
                    stats.retries += 1
                sleep(min(10.0, 1.0 * attempt))
        raise ImageioCopyError(f"{last}") from last

    def process(bounds: tuple[int, int]) -> None:
        off, length = bounds
        data = fetch_chunk(off, length)
        writer.write_at(off, data)
        stats.add(len(data))
        if on_progress:
            on_progress(len(data))

    run_workers(chunks, process, workers=min(workers, len(chunks)), check_cancel=check_cancel, stop=stop)
    return stats
