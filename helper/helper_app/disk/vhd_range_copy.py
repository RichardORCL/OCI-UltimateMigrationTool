"""Copy the allocated pages of an Azure export blob (fixed VHD) onto a ``PositionalWriter``.

The SAS from ``beginGetAccess`` points at a page blob whose content is the raw disk followed by the
512-byte VHD footer.  ``Get Page Ranges`` lists the pages that were ever written, so unallocated
space is never downloaded (the OCI volume reads as zeros there anyway), and every page range is
fetched with an HTTP range request and written at the same offset - no format decoding involved.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import httpx

from helper_app.azure.client import AzureClient, AzureError
from helper_app.disk.copy_common import CopyStats as RangeCopyStats
from helper_app.disk.copy_common import run_workers
from helper_app.disk.writer import PositionalWriter

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class VhdCopyError(RuntimeError):
    pass


# --------------------------------------------------------------------------- page ranges
def blob_length(client: AzureClient, sas_url: str) -> int:
    """Size of the export blob (disk bytes + VHD footer)."""
    resp = client.blob_head(sas_url)
    length = resp.headers.get("Content-Length") or resp.headers.get("x-ms-blob-content-length")
    if not length:
        raise VhdCopyError("export blob reports no Content-Length")
    return int(length)


def _parse_page_list(xml_text: str) -> tuple[list[tuple[int, int]], Optional[str]]:
    root = ET.fromstring(xml_text)
    ranges: list[tuple[int, int]] = []
    for pr in root.iter("PageRange"):
        start = int(pr.findtext("Start", "0"))
        end = int(pr.findtext("End", "0"))
        if end >= start:
            ranges.append((start, end - start + 1))
    marker = root.findtext("NextMarker")
    return ranges, (marker.strip() if marker and marker.strip() else None)


def merge_ranges(ranges: list[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    """Sort, clip to ``limit`` bytes (drops the VHD footer) and merge adjacent ranges."""
    out: list[tuple[int, int]] = []
    for off, ln in sorted(ranges):
        if off >= limit:
            continue
        ln = min(ln, limit - off)
        if ln <= 0:
            continue
        if out and out[-1][0] + out[-1][1] >= off:
            last_off, last_len = out[-1]
            new_end = max(last_off + last_len, off + ln)
            out[-1] = (last_off, new_end - last_off)
        else:
            out.append((off, ln))
    return out


def list_page_ranges(client: AzureClient, sas_url: str, disk_bytes: int,
                     max_results: int = 10000) -> list[tuple[int, int]]:
    """Allocated ``(offset, length)`` ranges of the export blob, limited to the disk (footer excluded)."""
    ranges: list[tuple[int, int]] = []
    marker: Optional[str] = None
    while True:
        params = {"comp": "pagelist", "maxresults": str(max_results)}
        if marker:
            params["marker"] = marker
        try:
            resp = client.blob_get(sas_url, params=params)
        except httpx.HTTPError as exc:
            raise VhdCopyError(f"Get Page Ranges failed: {exc}") from exc
        if resp.status_code != 200:
            raise VhdCopyError(f"Get Page Ranges returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            page, marker = _parse_page_list(resp.text)
        except ET.ParseError as exc:
            raise VhdCopyError(f"Get Page Ranges returned no page list: {exc}") from exc
        ranges.extend(page)
        if not marker:
            break
    return merge_ranges(ranges, disk_bytes)


def split_chunks(ranges: list[tuple[int, int]], chunk_bytes: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    for off, ln in ranges:
        while ln > 0:
            n = min(ln, chunk_bytes)
            chunks.append((off, n))
            off += n
            ln -= n
    return chunks


# --------------------------------------------------------------------------- copy
def copy_ranges(
    client: AzureClient,
    sas_url: Callable[[], str],
    ranges: list[tuple[int, int]],
    writer: PositionalWriter,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    workers: int = 4,
    retries: int = DEFAULT_RETRIES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    refresh: Optional[Callable[[], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RangeCopyStats:
    """Download every range in ``chunk_bytes`` pieces with ``workers`` threads and write it in place.

    ``sas_url`` returns the current SAS (``refresh`` mints a new one on HTTP 403, i.e. an expired SAS).
    A chunk is retried ``retries`` times with a short backoff; when that fails the whole copy fails.
    ``check_cancel`` may raise to abort (the exception is re-raised in the calling thread).
    """
    stats = RangeCopyStats()
    chunks = split_chunks(ranges, chunk_bytes)
    if not chunks:
        return stats
    stop = threading.Event()
    refresh_lock = threading.Lock()
    refreshed_at = [0.0]

    def refresh_sas() -> None:
        if refresh is None:
            return
        with refresh_lock:
            if time.monotonic() - refreshed_at[0] < 30:
                return  # another worker just did it
            refresh()
            refreshed_at[0] = time.monotonic()

    def fetch(off: int, ln: int) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            if stop.is_set():
                raise VhdCopyError("aborted")
            try:
                resp = client.blob_get(sas_url(), headers={"x-ms-range": f"bytes={off}-{off + ln - 1}"})
                if resp.status_code in (200, 206):
                    data = resp.content
                    if len(data) != ln:
                        raise VhdCopyError(f"range {off}-{off + ln - 1} returned {len(data)} bytes, expected {ln}")
                    return data
                if resp.status_code == 403:
                    refresh_sas()
                    raise VhdCopyError(f"range {off}-{off + ln - 1} returned HTTP 403 (SAS expired or revoked)")
                raise VhdCopyError(f"range {off}-{off + ln - 1} returned HTTP {resp.status_code}")
            except (httpx.HTTPError, VhdCopyError, AzureError) as exc:
                last = exc
                if attempt < retries:
                    with stats._lock:
                        stats.retries += 1
                    sleep(min(10.0, 1.0 * attempt))
        raise VhdCopyError(f"{last}") from last

    def process(bounds: tuple[int, int]) -> None:
        off, ln = bounds
        data = fetch(off, ln)
        writer.write_at(off, data)
        stats.add(len(data))
        if on_progress:
            on_progress(len(data))

    run_workers(chunks, process, workers=min(workers, len(chunks)), check_cancel=check_cancel, stop=stop)
    return stats
