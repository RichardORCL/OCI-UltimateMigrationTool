"""Read a qcow2 disk from Object Storage onto a block device.

Allocated clusters are fetched with range reads and written at their guest offsets.
Unallocated clusters stay zero, which is what a new OCI volume already is. Version 2 and 3
images are accepted, including zlib-compressed clusters. Encryption, a backing file, an
external data file, extended L2 entries, and zstd compression are refused.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from typing import Callable

from helper_app.disk.writer import BlockDeviceWriter
from helper_app.oci.clients import OciClients
from helper_app.oci.object_bytes import read_object_range

log = logging.getLogger(__name__)

_MAGIC = 0x514649FB
_OFFSET_MASK = 0x00FFFFFFFFFFFE00
_COMPRESSED = 1 << 62
_INCOMPAT_DIRTY = 1 << 0
_INCOMPAT_CORRUPT = 1 << 1
_INCOMPAT_EXTERNAL = 1 << 2
_INCOMPAT_COMPRESSION = 1 << 3
_INCOMPAT_EXTL2 = 1 << 4
_HEADER_PROBE = 4096
_MAX_RANGE = 8 * 1024 * 1024
_MAX_L1_ENTRIES = 4_000_000


class QcowError(Exception):
    """The object is not a qcow2 disk this importer can copy."""


@dataclass(frozen=True)
class QcowHeader:
    version: int
    virtual_size: int
    cluster_bits: int
    cluster_size: int
    l1_offset: int
    l1_size: int


def parse_qcow_header(buf: bytes) -> QcowHeader:
    """Read a qcow2 header. ``buf`` must start at byte 0 of the image."""
    if len(buf) < 72 or struct.unpack_from(">I", buf, 0)[0] != _MAGIC:
        raise QcowError("not a qcow2 image")
    version = struct.unpack_from(">I", buf, 4)[0]
    if version == 1:
        raise QcowError("qcow version 1 is not supported; convert the disk to qcow2")
    if version not in (2, 3):
        raise QcowError(f"unsupported qcow version {version}")
    backing_size = struct.unpack_from(">I", buf, 16)[0]
    cluster_bits = struct.unpack_from(">I", buf, 20)[0]
    virtual_size = struct.unpack_from(">Q", buf, 24)[0]
    crypt = struct.unpack_from(">I", buf, 32)[0]
    l1_size = struct.unpack_from(">I", buf, 36)[0]
    l1_offset = struct.unpack_from(">Q", buf, 40)[0]
    if backing_size:
        raise QcowError("the qcow2 image depends on a backing file, which is not in this object")
    if crypt:
        raise QcowError("encrypted qcow2 images cannot be imported")
    if not 9 <= cluster_bits <= 21:
        raise QcowError(f"invalid qcow2 cluster size (2^{cluster_bits})")
    if virtual_size <= 0:
        raise QcowError("the qcow2 image has no virtual size")
    if l1_size <= 0 or l1_offset <= 0:
        raise QcowError("the qcow2 image has no L1 table")
    if l1_size > _MAX_L1_ENTRIES:
        raise QcowError("the qcow2 L1 table is unreasonably large")
    if version >= 3:
        if len(buf) < 104:
            raise QcowError("qcow2 v3 header is truncated")
        incompat = struct.unpack_from(">Q", buf, 72)[0]
        header_length = struct.unpack_from(">I", buf, 100)[0]
        if incompat & _INCOMPAT_CORRUPT:
            raise QcowError("the qcow2 image is marked corrupt")
        if incompat & _INCOMPAT_EXTERNAL:
            raise QcowError("the qcow2 image stores its data in an external file")
        if incompat & _INCOMPAT_EXTL2:
            raise QcowError("qcow2 images with extended L2 entries are not supported")
        if incompat & _INCOMPAT_COMPRESSION:
            if header_length < 105 or len(buf) < 105:
                raise QcowError("qcow2 compression type is missing")
            ctype = buf[104]
            if ctype == 1:
                raise QcowError("zstd-compressed qcow2 images are not supported")
            if ctype != 0:
                raise QcowError(f"unsupported qcow2 compression type {ctype}")
        unknown = incompat & ~(_INCOMPAT_DIRTY | _INCOMPAT_COMPRESSION)
        if unknown:
            raise QcowError("the qcow2 image uses features this importer does not support")
    cluster_size = 1 << cluster_bits
    guest_per_l2 = (cluster_size // 8) * cluster_size
    needed = (virtual_size + guest_per_l2 - 1) // guest_per_l2
    if l1_size < needed:
        raise QcowError("the qcow2 L1 table is shorter than the virtual disk")
    return QcowHeader(version, virtual_size, cluster_bits, cluster_size, l1_offset, l1_size)


def _all_zero(data: bytes) -> bool:
    return data.count(0) == len(data)


def _inflate(data: bytes, cluster_size: int) -> bytes:
    dec = zlib.decompressobj(wbits=-15)
    try:
        out = dec.decompress(data, cluster_size + 1)
    except zlib.error as exc:
        raise QcowError(f"a compressed cluster could not be read: {exc}") from exc
    if len(out) > cluster_size:
        raise QcowError("a compressed cluster is larger than the cluster size")
    if len(out) < cluster_size:
        out += b"\x00" * (cluster_size - len(out))
    return out


def _compressed_size(entry: int, cluster_bits: int) -> tuple[int, int]:
    shift = 62 - (cluster_bits - 8)
    coffset = entry & ((1 << shift) - 1)
    sectors = ((entry >> shift) & ((1 << (cluster_bits - 8)) - 1)) + 1
    nbytes = sectors * 512 - (coffset & 511)
    if coffset <= 0 or nbytes <= 0:
        raise QcowError("a compressed cluster has an invalid location")
    return coffset, nbytes


@dataclass
class _Piece:
    file_off: int
    guest_off: int
    length: int
    kind: str  # raw | zlib


def copy_qcow(
    read_at: Callable[[int, int], bytes],
    header: QcowHeader,
    write_at: Callable[[int, bytes], None],
    *,
    skip_zero_clusters: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Copy allocated clusters. Returns ``(virtual_size, bytes_written)``."""

    def exact(offset: int, length: int) -> bytes:
        if check_cancel:
            check_cancel()
        data = read_at(offset, length)
        if len(data) != length:
            raise QcowError(f"qcow2 image ended at byte {offset + len(data)}; needed {length} bytes at {offset}")
        return data

    written = 0
    l2_entries = header.cluster_size // 8
    l2_span = l2_entries * header.cluster_size
    l1 = exact(header.l1_offset, header.l1_size * 8)

    def emit(guest_off: int, data: bytes) -> None:
        nonlocal written
        room = header.virtual_size - guest_off
        if room <= 0:
            return
        chunk = data[:room]
        if skip_zero_clusters and _all_zero(chunk):
            return
        write_at(guest_off, chunk)
        written += len(chunk)

    def flush(run: list[_Piece]) -> None:
        if not run:
            return
        span = run[-1].file_off + run[-1].length - run[0].file_off
        blob = exact(run[0].file_off, span)
        base = run[0].file_off
        for piece in run:
            rel = piece.file_off - base
            emit(piece.guest_off, blob[rel:rel + piece.length])

    for i in range(header.l1_size):
        guest_base = i * l2_span
        if guest_base >= header.virtual_size:
            break
        if check_cancel:
            check_cancel()
        entry = struct.unpack_from(">Q", l1, i * 8)[0]
        l2_off = entry & _OFFSET_MASK
        if l2_off == 0:
            if on_progress:
                on_progress(min(header.virtual_size, guest_base + l2_span), written)
            continue
        l2 = exact(l2_off, header.cluster_size)
        pieces: list[_Piece] = []
        for j in range(l2_entries):
            guest = guest_base + j * header.cluster_size
            if guest >= header.virtual_size:
                break
            slot = struct.unpack_from(">Q", l2, j * 8)[0]
            if slot & _COMPRESSED:
                file_off, nbytes = _compressed_size(slot, header.cluster_bits)
                pieces.append(_Piece(file_off, guest, nbytes, "zlib"))
                continue
            file_off = slot & _OFFSET_MASK
            if file_off:
                pieces.append(_Piece(file_off, guest, header.cluster_size, "raw"))
        raw: list[_Piece] = []
        for piece in sorted(pieces, key=lambda p: (p.file_off, p.guest_off)):
            if piece.kind == "zlib":
                flush(raw)
                raw = []
                emit(piece.guest_off, _inflate(exact(piece.file_off, piece.length), header.cluster_size))
                continue
            if raw:
                end = raw[-1].file_off + raw[-1].length
                if piece.file_off != end or end - raw[0].file_off + piece.length > _MAX_RANGE:
                    flush(raw)
                    raw = []
            raw.append(piece)
        flush(raw)
        if on_progress:
            on_progress(min(header.virtual_size, guest_base + l2_span), written)
    return header.virtual_size, written


def copy_qcow_object(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
    device: str,
    capacity_bytes: int,
    skip_zero_clusters: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Copy a qcow2 object onto ``device``. Returns ``(virtual_size, bytes_written)``."""
    try:
        probe = read_object_range(c, namespace, bucket, object_name, 0, _HEADER_PROBE)
        header = parse_qcow_header(probe)
    except QcowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise QcowError(f"cannot read {bucket}/{object_name}: {exc}") from exc
    if header.virtual_size > capacity_bytes:
        raise QcowError(
            f"{object_name} is a {header.virtual_size}-byte disk, larger than the {capacity_bytes}-byte volume"
        )
    log.info("qcow2 %s: virtual size %d, cluster %d", object_name, header.virtual_size, header.cluster_size)

    def read_at(offset: int, length: int) -> bytes:
        try:
            return read_object_range(c, namespace, bucket, object_name, offset, length)
        except Exception as exc:  # noqa: BLE001
            raise QcowError(f"cannot read {bucket}/{object_name} at byte {offset}: {exc}") from exc

    with BlockDeviceWriter(device, expected_min_size=capacity_bytes) as writer:
        writer.ensure_size(capacity_bytes)
        return copy_qcow(
            read_at,
            header,
            writer.write_at,
            skip_zero_clusters=skip_zero_clusters,
            on_progress=on_progress,
            check_cancel=check_cancel,
        )
