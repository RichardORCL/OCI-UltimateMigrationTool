"""Export VM disks through an ``HttpNfcLease`` (the mechanism behind "Export OVF/OVA").

ESXi serves each disk as a stream-optimized VMDK over HTTPS.  No VDDK is involved: the helper
GETs the lease URLs (proxied by vCenter) and feeds the bytes straight into the decoder.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from helper_app.models import DiskSpec

log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    pass


@dataclass
class DiskUrl:
    key: str
    target_id: str
    url: str
    file_size: Optional[int] = None


_TARGET_INDEX = re.compile(r"disk-(\d+)\.vmdk$", re.IGNORECASE)


def rewrite_lease_url(url: str, host: str) -> str:
    """Lease URLs contain ``*`` as host placeholder; substitute the reachable host."""
    parts = urlsplit(url)
    if parts.hostname == "*" or parts.netloc.startswith("*"):
        netloc = parts.netloc.replace("*", host, 1)
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def match_disk_urls(disks: list[DiskSpec], device_urls: list[DiskUrl]) -> dict[int, DiskUrl]:
    """Map DiskSpec.index -> DiskUrl.

    Preferred: ``DeviceUrl.key`` ends with ``<ControllerClass><bus>:<unit>`` (the hint we
    recorded from the VM hardware).  Fallback: ``targetId`` ``disk-N.vmdk`` order, then
    plain positional order.
    """
    result: dict[int, DiskUrl] = {}
    remaining = list(device_urls)
    for disk in disks:
        hint = disk.nfc_key_hint
        for du in remaining:
            if du.key and du.key.endswith(hint):
                result[disk.index] = du
                remaining.remove(du)
                break
    if len(result) == len(disks):
        return result

    unmatched = [d for d in disks if d.index not in result]
    by_target: dict[int, DiskUrl] = {}
    for du in remaining:
        m = _TARGET_INDEX.search(du.target_id or "")
        if m:
            by_target[int(m.group(1))] = du
    if len(by_target) == len(remaining) and len(remaining) == len(unmatched):
        ordered = [by_target[k] for k in sorted(by_target)]
        for disk, du in zip(sorted(unmatched, key=lambda d: d.index), ordered, strict=True):
            result[disk.index] = du
        return result

    if len(remaining) == len(unmatched):
        for disk, du in zip(sorted(unmatched, key=lambda d: d.index), remaining, strict=True):
            result[disk.index] = du
        return result
    raise ExportError(
        f"cannot match {len(disks)} VM disks to {len(device_urls)} lease disk URLs "
        f"(keys={[d.key for d in device_urls]})"
    )


class NfcExport:
    """Context manager around ``vm.ExportVm()``.

    Keeps the lease alive with periodic ``HttpNfcLeaseProgress`` calls while disks are
    streamed and completes/aborts the lease on exit.
    """

    def __init__(self, vm, nfc_host: str, verify_ssl: bool = False, progress_interval_s: float = 60.0,
                 ready_timeout_s: float = 300.0, chunk_bytes: int = 1024 * 1024):
        self.vm = vm
        self.nfc_host = nfc_host
        self.verify_ssl = verify_ssl
        self.progress_interval_s = progress_interval_s
        self.ready_timeout_s = ready_timeout_s
        self.chunk_bytes = chunk_bytes
        self.lease = None
        self.total_bytes_hint = 0
        self._sent = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keepalive: Optional[threading.Thread] = None
        self._failed: Optional[str] = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "NfcExport":
        self.lease = self.vm.ExportVm()
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            state = str(self.lease.state)
            if state == "ready":
                break
            if state == "error":
                raise ExportError(f"NFC lease failed: {self.lease.error}")
            if time.monotonic() > deadline:
                raise ExportError("NFC lease did not become ready in time")
            time.sleep(1.0)
        info = self.lease.info
        self.total_bytes_hint = int(getattr(info, "totalDiskCapacityInKB", 0) or 0) * 1024
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="nfc-keepalive", daemon=True)
        self._keepalive.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._keepalive:
            self._keepalive.join(timeout=5)
        if self.lease is None:
            return
        try:
            if exc_type is None and self._failed is None:
                self.lease.HttpNfcLeaseComplete()
            else:
                self.lease.HttpNfcLeaseAbort()
        except Exception as e:  # noqa: BLE001
            log.warning("closing NFC lease failed: %s", e)

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(self.progress_interval_s):
            try:
                self.lease.HttpNfcLeaseProgress(self.percent)
            except Exception as e:  # noqa: BLE001
                log.warning("HttpNfcLeaseProgress failed: %s", e)

    @property
    def total_stream_bytes(self) -> int:
        """Best known size of the whole export: the lease's per-disk file sizes when reported, else the
        raw disk capacity (the compressed stream is at most that large)."""
        try:
            sizes = [du.file_size for du in self.disk_urls()]
        except Exception:  # noqa: BLE001 - lease may be gone
            sizes = []
        if sizes and all(s for s in sizes):
            return int(sum(sizes))
        return self.total_bytes_hint

    @property
    def percent(self) -> int:
        """Progress as reported to the lease, i.e. what vCenter shows on the *Export OVF template* task."""
        total = self.total_stream_bytes
        if not total:
            return 0
        with self._lock:
            return max(0, min(99, int(self._sent * 100 / total)))

    # ---------------------------------------------------------------- disks
    def disk_urls(self) -> list[DiskUrl]:
        urls = []
        for du in self.lease.info.deviceUrl or []:
            if not getattr(du, "disk", False):
                continue
            urls.append(DiskUrl(key=du.key or "", target_id=du.targetId or "",
                                url=rewrite_lease_url(du.url, self.nfc_host),
                                file_size=getattr(du, "fileSize", None)))
        return urls

    def iter_disk(self, url: str, on_progress: Optional[Callable[[int], None]] = None) -> Iterator[bytes]:
        """Yield the stream-optimized VMDK bytes of one disk from ESXi/vCenter."""
        with httpx.Client(verify=self.verify_ssl, timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ExportError(f"GET {url} returned HTTP {resp.status_code}")
                for chunk in resp.iter_bytes(self.chunk_bytes):
                    if not chunk:
                        continue
                    with self._lock:
                        self._sent += len(chunk)
                    if on_progress:
                        on_progress(len(chunk))
                    yield chunk

    def mark_failed(self, reason: str) -> None:
        self._failed = reason
