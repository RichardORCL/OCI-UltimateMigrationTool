"""Positional writers used as the decoder sink."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Protocol

_HAS_PWRITE = hasattr(os, "pwrite")


class PositionalWriter(Protocol):
    def write_at(self, offset: int, data: bytes) -> None: ...

    def close(self) -> None: ...

    @property
    def size(self) -> int | None: ...


class BlockDeviceWriter:
    """pwrite() onto a block device (or a regular file for tests) with fsync on close."""

    def __init__(self, path: str | os.PathLike, expected_min_size: int | None = None, create: bool = False):
        self.path = Path(path)
        flags = os.O_WRONLY
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY  # type: ignore[attr-defined]
        self._fd = os.open(self.path, flags, 0o600)
        self._closed = False
        self._is_regular = stat.S_ISREG(os.fstat(self._fd).st_mode)
        self._size = self._probe_size()
        if expected_min_size is not None:
            if self._is_regular:
                self.ensure_size(expected_min_size)
            elif self._size is not None and self._size < expected_min_size:
                os.close(self._fd)
                self._closed = True
                raise ValueError(
                    f"target {self.path} is {self._size} bytes, smaller than the required {expected_min_size} bytes"
                )

    def _probe_size(self) -> int | None:
        st = os.fstat(self._fd)
        if stat.S_ISREG(st.st_mode):
            return st.st_size
        if stat.S_ISBLK(st.st_mode):
            try:
                return os.lseek(self._fd, 0, os.SEEK_END)
            except OSError:
                return None
        return None

    @property
    def size(self) -> int | None:
        return self._size

    def write_at(self, offset: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if _HAS_PWRITE:
                n = os.pwrite(self._fd, view, offset)
            else:  # Windows dev fallback; the helper itself always runs on Linux
                os.lseek(self._fd, offset, os.SEEK_SET)
                n = os.write(self._fd, view)
            if n <= 0:
                raise OSError(f"short write at offset {offset} on {self.path}")
            offset += n
            view = view[n:]

    def ensure_size(self, size: int) -> None:
        """For regular files only: extend to ``size`` so unallocated grains read as zeros."""
        if self._is_regular and (self._size or 0) < size:
            os.ftruncate(self._fd, size)
            self._size = size

    def close(self) -> None:
        if self._closed:
            return
        try:
            os.fsync(self._fd)
        finally:
            os.close(self._fd)
            self._closed = True

    def __enter__(self) -> "BlockDeviceWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
