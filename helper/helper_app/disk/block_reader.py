"""Read a block device (or regular file) as stream-optimized VMDK grains."""

from __future__ import annotations

import os
from typing import Callable, Iterator, Optional

from helper_app.disk.vmdk_stream import DEFAULT_GRAIN_SECTORS, SECTOR

ProgressFn = Callable[[int, int], None]


def _pread(fd: int, n: int, offset: int) -> bytes:
    """``os.pread`` on POSIX; seek+read on Windows (no pread in the C runtime)."""
    if hasattr(os, "pread"):
        return os.pread(fd, n, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, n)


def iter_device_grains(
    path: str,
    capacity_bytes: int,
    grain_bytes: int | None = None,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[ProgressFn] = None,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(lba, data)`` for each non-zero grain of ``path``.

    Reads with ``os.pread`` so the helper's other jobs are not affected by the file
    offset.  Short reads past EOF are padded with zeros (test fixtures may be smaller
    than the advertised volume capacity).
    """
    grain_bytes = grain_bytes or DEFAULT_GRAIN_SECTORS * SECTOR
    if grain_bytes % SECTOR:
        raise ValueError("grain size must be a multiple of 512")
    if capacity_bytes < 0:
        raise ValueError("capacity must be non-negative")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        offset = 0
        emitted = 0
        while offset < capacity_bytes:
            if check_cancel is not None:
                check_cancel()
            want = min(grain_bytes, capacity_bytes - offset)
            data = _pread(fd, want, offset)
            if len(data) < want:
                data = data + b"\0" * (want - len(data))
            if any(data):
                yield offset // SECTOR, data
                emitted += 1
            offset += want
            if on_progress is not None:
                on_progress(offset, emitted)
    finally:
        os.close(fd)
