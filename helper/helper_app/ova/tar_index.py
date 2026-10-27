"""List the members of an ``.ova`` tarball in Object Storage without downloading it.

An OVA is a plain ustar/GNU tar. Member headers are 512-byte blocks, so with HTTP range reads the
whole table of contents costs a handful of small ``GetObject`` calls regardless of archive size, and
each disk can later be streamed straight from its data offset. Compressed archives (not valid OVAs,
but ``tar|*`` used to accept them) fall back to one sequential pass with no usable offsets.
"""

from __future__ import annotations

import io
import logging
import tarfile
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from helper_app.oci.object_bytes import open_object_stream, read_object_range

if TYPE_CHECKING:
    from helper_app.oci.clients import OciClients

log = logging.getLogger(__name__)

READAHEAD = 256 * 1024
_DRAIN_CHUNK = 1024 * 1024


@dataclass
class OvaMember:
    name: str
    size: int
    is_file: bool
    offset_data: int | None = None  # None when the archive had to be read sequentially (compressed)

    @property
    def basename(self) -> str:
        return self.name.replace("\\", "/").rstrip("/").split("/")[-1]


@dataclass
class OvaIndex:
    members: list[OvaMember]
    seekable: bool  # offsets are valid for range reads
    truncated_at: int | None = None  # offset of an unparsable entry, if the listing stopped early

    def find(self, basename: str) -> OvaMember | None:
        want = basename.replace("\\", "/").split("/")[-1].lower()
        for m in self.members:
            if m.is_file and m.basename.lower() == want:
                return m
        return None

    def files_with_suffix(self, suffix: str) -> list[OvaMember]:
        return [m for m in self.members if m.is_file and m.basename.lower().endswith(suffix)]

    def describe(self) -> str:
        if not self.members:
            return "(no entries)"
        return ", ".join(f"{m.basename} ({_human(m.size)})" for m in self.members if m.is_file)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class _RangeFile(io.RawIOBase):
    """A seekable read-only view of an object backed by ``Range`` requests with a small read-ahead."""

    def __init__(self, c: OciClients, namespace: str, bucket: str, object_name: str, readahead: int = READAHEAD):
        super().__init__()
        self._c, self._ns, self._bucket, self._name = c, namespace, bucket, object_name
        self._readahead = readahead
        self._pos = 0
        self._buf = b""
        self._buf_start = 0
        self._eof_at: int | None = None
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            raise io.UnsupportedOperation("seek from end is not supported on an object range view")
        if self._pos < 0:
            raise ValueError("negative seek position")
        return self._pos

    def _fill(self, want: int) -> bool:
        if self._eof_at is not None and self._pos >= self._eof_at:
            return False
        length = max(want, self._readahead)
        try:
            data = read_object_range(self._c, self._ns, self._bucket, self._name, self._pos, length)
        except Exception as exc:  # noqa: BLE001 - only an out-of-range read means EOF
            if getattr(exc, "status", None) == 416:
                data = b""
            else:
                raise
        self.requests += 1
        self._buf_start, self._buf = self._pos, data
        if len(data) < length:
            self._eof_at = self._pos + len(data)
        return bool(data)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise io.UnsupportedOperation("unbounded read on an object range view")
        out = bytearray()
        while len(out) < size:
            if not (self._buf_start <= self._pos < self._buf_start + len(self._buf)):
                if not self._fill(size - len(out)):
                    break
            off = self._pos - self._buf_start
            take = self._buf[off : off + size - len(out)]
            out += take
            self._pos += len(take)
        return bytes(out)

    def readinto(self, b) -> int:  # pragma: no cover - RawIOBase protocol
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


class OvaIndexError(Exception):
    """The archive starts like a tar but cannot be read to the end."""


def _index_by_ranges(c: OciClients, namespace: str, bucket: str, object_name: str) -> OvaIndex:
    rf = _RangeFile(c, namespace, bucket, object_name)
    members: list[OvaMember] = []
    # tarfile.open() parses the first header itself: a ReadError there means "not a plain tar" and the
    # caller falls back to a sequential read. A ReadError later means the archive is damaged or short.
    with tarfile.open(fileobj=rf, mode="r:") as tar:
        try:
            for member in tar:
                members.append(
                    OvaMember(name=member.name, size=member.size, is_file=member.isfile(), offset_data=member.offset_data)
                )
        except tarfile.ReadError as exc:
            seen = ", ".join(m.basename for m in members) or "nothing"
            raise OvaIndexError(
                f"{object_name} is damaged or incomplete after {seen}: {exc} "
                f"(the archive may have been truncated during upload; compare the object size with the source file)"
            ) from exc
        end = tar.offset
    truncated_at = None
    rf.seek(end)
    tail = rf.read(tarfile.BLOCKSIZE)
    if tail.strip(b"\0"):
        truncated_at = end  # tarfile stopped at something that is not a header and not the end-of-archive marker
    log.debug("%s/%s: %d tar members via %d range reads", bucket, object_name, len(members), rf.requests)
    return OvaIndex(members=members, seekable=True, truncated_at=truncated_at)


def _index_by_streaming(c: OciClients, namespace: str, bucket: str, object_name: str) -> OvaIndex:
    resp = c.object_storage.get_object(namespace, bucket, object_name)
    stream = open_object_stream(resp.data)
    members: list[OvaMember] = []
    with closing(stream), tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            members.append(OvaMember(name=member.name, size=member.size, is_file=member.isfile()))
            if member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    while f.read(_DRAIN_CHUNK):
                        pass
    return OvaIndex(members=members, seekable=False)


def index_ova(c: OciClients, namespace: str, bucket: str, object_name: str) -> OvaIndex:
    """Table of contents of an OVA in Object Storage. Raises ``tarfile.ReadError`` if it is not a tar."""
    try:
        return _index_by_ranges(c, namespace, bucket, object_name)
    except tarfile.ReadError as exc:
        log.info("%s/%s: not a plain tar via range reads (%s); reading sequentially", bucket, object_name, exc)
        return _index_by_streaming(c, namespace, bucket, object_name)


def read_member(c: OciClients, namespace: str, bucket: str, object_name: str, member: OvaMember) -> bytes:
    """Whole body of a (small) member, by range when the offset is known, else by streaming."""
    if member.offset_data is not None:
        data = read_object_range(c, namespace, bucket, object_name, member.offset_data, member.size)
        if len(data) != member.size:
            raise tarfile.ReadError(
                f"{member.basename}: got {len(data)} of {member.size} bytes from {object_name}"
            )
        return data
    resp = c.object_storage.get_object(namespace, bucket, object_name)
    stream = open_object_stream(resp.data)
    with closing(stream), tarfile.open(fileobj=stream, mode="r|*") as tar:
        for m in tar:
            if m.name != member.name or not m.isfile():
                continue
            f = tar.extractfile(m)
            if f is None:
                break
            return f.read()
    raise tarfile.ReadError(f"{member.basename} not found inside {object_name}")
