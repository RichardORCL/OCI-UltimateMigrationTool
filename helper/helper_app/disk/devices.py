"""Discovering block devices on the helper.

Block volumes are attached with a consistent device path (``/dev/oracleoci/oraclevdX``), but OCI does
not allow a device path when a *boot* volume is attached as a data volume ("Device paths are not
available when you attach a boot volume as a data volume to a second instance").  For those the helper
takes a snapshot of the whole disks it can see, attaches, and waits for exactly one new disk of the
expected size to appear.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SYS_BLOCK = Path("/sys/block")
SCSI_HOST = Path("/sys/class/scsi_host")
_DISK_PREFIXES = ("sd", "vd", "xvd", "nvme")
# OCI size_in_gbs vs the kernel (GiB vs GB) and marketplace images (~47 vs 50 GiB) need slack.
_SIZE_SLACK_BYTES = 4 * 1024**3
_SIZE_SLACK_RATIO = 10  # 10%

DeviceScanner = Callable[[], dict[str, int]]  # device path -> size in bytes
ProgressFn = Callable[[dict[str, int]], None]


def scan_block_devices(sys_block: Path = SYS_BLOCK) -> dict[str, int]:
    """Whole disks (no partitions, no loop/ram/dm devices) with their size in bytes."""
    devices: dict[str, int] = {}
    if not sys_block.is_dir():
        return devices
    for entry in sys_block.iterdir():
        name = entry.name
        if not name.startswith(_DISK_PREFIXES) or (entry / "partition").exists():
            continue
        try:
            sectors = int((entry / "size").read_text().strip())
        except (OSError, ValueError):
            continue
        if sectors > 0:
            devices[f"/dev/{name}"] = sectors * 512
    return devices


def rescan_scsi_hosts(scsi_host: Path = SCSI_HOST) -> None:
    """Ask SCSI hosts to look for a newly attached disk.  No-op when ``/sys`` is missing."""
    if not scsi_host.is_dir():
        return
    for host in scsi_host.iterdir():
        scan = host / "scan"
        try:
            scan.write_text("- - -\n")
        except OSError:
            continue


def device_size_matches(actual: int, expected: int) -> bool:
    """True when ``actual`` is the kernel size of a volume whose OCI size is ``expected``.

    Exact match is preferred.  A few GiB of slack covers size_in_gbs rounding and GiB-vs-GB, but a
    second attached volume of a different size (e.g. 50 vs 100 GiB) still does not match.
    """
    if actual <= 0 or expected <= 0:
        return False
    if actual == expected:
        return True
    slack = max(_SIZE_SLACK_BYTES, expected // _SIZE_SLACK_RATIO)
    if abs(actual - expected) <= slack:
        return True
    gbs = max(1, round(expected / 1024**3))
    return actual == gbs * 1000**3


def wait_for_new_device(
    before: dict[str, int],
    expected_bytes: int,
    timeout_s: float,
    scan: DeviceScanner = scan_block_devices,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: ProgressFn | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> str:
    """Return the path of the disk that appeared since ``before`` and matches ``expected_bytes``.

    Size matching allows a few GiB of slack so OCI ``size_in_gbs`` and the kernel capacity can
    disagree (marketplace images, GB vs GiB).  Raises ``RuntimeError`` when nothing suitable shows
    up in time.
    """
    deadline = time.monotonic() + timeout_s
    last: dict[str, int] = {}
    last_log = 0.0
    while True:
        now = scan()
        new = {path: size for path, size in now.items() if path not in before}
        matching = [path for path, size in new.items() if device_size_matches(size, expected_bytes)]
        if len(matching) == 1:
            path = matching[0]
            actual = new[path]
            if actual != expected_bytes:
                log.info(
                    "using new disk %s (%s bytes); expected %s (within size slack)",
                    path, actual, expected_bytes,
                )
            return path
        if len(matching) > 1:
            raise RuntimeError(
                f"{len(matching)} new disks of about {expected_bytes} bytes appeared at once "
                f"({sorted(matching)}); cannot tell which one is the attached boot volume"
            )
        last = new
        if check_cancel is not None:
            check_cancel()
        now_t = time.monotonic()
        if now_t - last_log >= 15:
            seen = ", ".join(f"{p} ({s} bytes)" for p, s in sorted(new.items())) or "none"
            log.info(
                "waiting for a new disk of %s bytes on the migration tool VM (new disks seen: %s)",
                expected_bytes, seen,
            )
            if on_progress is not None:
                on_progress(new)
            last_log = now_t
        if now_t >= deadline:
            break
        sleep(poll_s)
    seen = ", ".join(f"{p} ({s} bytes)" for p, s in sorted(last.items())) or "none"
    raise RuntimeError(
        f"no new disk of {expected_bytes} bytes appeared on the migration tool VM within {timeout_s:.0f}s "
        f"(new disks seen: {seen})"
    )
