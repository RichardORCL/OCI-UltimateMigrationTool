"""Stream-optimized (compressed, marker based) VMDK decoder and encoder.

The stream-optimized sparse extent format is what ESXi produces for OVF/OVA
export via an ``HttpNfcLease``.  It is designed to be consumed sequentially:

    sector 0            SparseExtentHeader (512 bytes)
    sectors 1..n        embedded text descriptor
    ...                 padding up to ``overHead`` sectors
    overHead..          sequence of *markers*:
                          grain marker     : val=LBA, size=compressed length, data follows in-line at byte 12
                          metadata marker  : size=0, type in {GT, GD, FOOTER, EOS}, val=#sectors that follow

The marker is ``struct { uint64 val; uint32 size; union { uint32 type; uint8 data[0]; } }``:
the ``type`` field and the first data byte share offset 12.  A grain marker therefore has no
type field; its deflate stream starts right after ``size``.
    end                 FOOTER (copy of header with the real gdOffset) then EOS

Because every grain carries its own LBA, the decoder can write blocks straight
onto the target block device without any random access to the source stream and
without staging the VMDK on disk.  Grains that are not present in the stream are
unallocated (zero) which matches a freshly created OCI volume.
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable, Iterator, Optional

SECTOR = 512
VMDK_MAGIC = 0x564D444B  # 'KDMV' little-endian
HEADER_STRUCT = struct.Struct("<IIIQQQQIQQQB4sH433s")
MARKER_STRUCT = struct.Struct("<QII")  # metadata marker: val, size (=0), type
GRAIN_MARKER_STRUCT = struct.Struct("<QI")  # grain marker: lba, size; compressed data follows immediately
assert HEADER_STRUCT.size == SECTOR
assert GRAIN_MARKER_STRUCT.size == 12

FLAG_NEWLINE_VALID = 0x1
FLAG_COMPRESSED = 0x10000
FLAG_MARKERS = 0x20000

COMPRESSION_NONE = 0
COMPRESSION_DEFLATE = 1

MARKER_EOS = 0
MARKER_GT = 1
MARKER_GD = 2
MARKER_FOOTER = 3

GD_AT_END = 0xFFFFFFFFFFFFFFFF

DEFAULT_GRAIN_SECTORS = 128  # 64 KiB grains
DEFAULT_GTES_PER_GT = 512
DEFAULT_OVERHEAD_SECTORS = 128


class VmdkFormatError(ValueError):
    """Raised when the byte stream is not a valid stream-optimized VMDK."""


@dataclass
class SparseExtentHeader:
    version: int
    flags: int
    capacity_sectors: int
    grain_sectors: int
    descriptor_offset: int
    descriptor_sectors: int
    gtes_per_gt: int
    rgd_offset: int
    gd_offset: int
    overhead_sectors: int
    unclean_shutdown: bool
    compress_algorithm: int

    @property
    def capacity_bytes(self) -> int:
        return self.capacity_sectors * SECTOR

    @property
    def grain_bytes(self) -> int:
        return self.grain_sectors * SECTOR

    @property
    def is_stream_optimized(self) -> bool:
        return bool(self.flags & FLAG_COMPRESSED) and bool(self.flags & FLAG_MARKERS)

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(
            VMDK_MAGIC,
            self.version,
            self.flags,
            self.capacity_sectors,
            self.grain_sectors,
            self.descriptor_offset,
            self.descriptor_sectors,
            self.gtes_per_gt,
            self.rgd_offset,
            self.gd_offset,
            self.overhead_sectors,
            1 if self.unclean_shutdown else 0,
            b"\n \r\n",
            self.compress_algorithm,
            b"\0" * 433,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "SparseExtentHeader":
        if len(data) < SECTOR:
            raise VmdkFormatError("header shorter than one sector")
        (
            magic,
            version,
            flags,
            capacity,
            grain,
            desc_off,
            desc_size,
            gtes,
            rgd,
            gd,
            overhead,
            unclean,
            _eol,
            compress,
            _pad,
        ) = HEADER_STRUCT.unpack(data[:SECTOR])
        if magic != VMDK_MAGIC:
            raise VmdkFormatError(f"bad VMDK magic 0x{magic:08x} (expected KDMV)")
        if version not in (1, 2, 3):
            raise VmdkFormatError(f"unsupported sparse extent version {version}")
        if grain <= 0 or (grain & (grain - 1)) != 0:
            raise VmdkFormatError(f"grain size {grain} is not a power of two")
        return cls(
            version=version,
            flags=flags,
            capacity_sectors=capacity,
            grain_sectors=grain,
            descriptor_offset=desc_off,
            descriptor_sectors=desc_size,
            gtes_per_gt=gtes,
            rgd_offset=rgd,
            gd_offset=gd,
            overhead_sectors=overhead,
            unclean_shutdown=bool(unclean),
            compress_algorithm=compress,
        )


@dataclass
class DecodeStats:
    bytes_consumed: int = 0
    grains_written: int = 0
    bytes_written: int = 0
    metadata_markers: int = 0
    finished: bool = False


class StreamOptimizedDecoder:
    """Incremental decoder.  Feed it arbitrary chunks; it calls ``write(offset, data)``
    for every grain.  ``write`` is expected to be a positional write (pwrite semantics).
    """

    _ST_HEADER = 0
    _ST_PREAMBLE = 1
    _ST_MARKERS = 2
    _ST_DONE = 3

    def __init__(
        self,
        write: Callable[[int, bytes], None],
        expected_capacity_bytes: Optional[int] = None,
        skip_zero_grains: bool = True,
    ) -> None:
        self._write = write
        self._expected_capacity = expected_capacity_bytes
        self._skip_zero = skip_zero_grains
        self._buf = bytearray()
        self._state = self._ST_HEADER
        self.header: Optional[SparseExtentHeader] = None
        self.descriptor: str = ""
        self.stats = DecodeStats()
        self._zero_grain: bytes = b""

    # ------------------------------------------------------------------ public
    @property
    def finished(self) -> bool:
        return self._state == self._ST_DONE

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._state == self._ST_DONE:
            # Trailing bytes after EOS are tolerated (some exporters pad).
            self.stats.bytes_consumed += len(chunk)
            return
        self._buf.extend(chunk)
        self.stats.bytes_consumed += len(chunk)
        self._drain()

    def finish(self) -> DecodeStats:
        """Signal end of input.  Raises if the stream ended mid-marker."""
        if self._state != self._ST_DONE:
            if self._state == self._ST_MARKERS and not self._buf:
                # Some tools omit the trailing EOS; accept a clean marker boundary.
                self._state = self._ST_DONE
            else:
                raise VmdkFormatError(
                    f"stream ended prematurely (state={self._state}, buffered={len(self._buf)} bytes)"
                )
        self.stats.finished = True
        return self.stats

    # ----------------------------------------------------------------- private
    def _drain(self) -> None:
        while True:
            if self._state == self._ST_HEADER:
                if len(self._buf) < SECTOR:
                    return
                self.header = SparseExtentHeader.unpack(bytes(self._buf[:SECTOR]))
                del self._buf[:SECTOR]
                self._validate_header(self.header)
                self._zero_grain = bytes(self.header.grain_bytes)
                self._state = self._ST_PREAMBLE
            elif self._state == self._ST_PREAMBLE:
                assert self.header is not None
                need = (self.header.overhead_sectors - 1) * SECTOR
                if len(self._buf) < need:
                    return
                preamble = bytes(self._buf[:need])
                del self._buf[:need]
                self._extract_descriptor(preamble)
                self._state = self._ST_MARKERS
            elif self._state == self._ST_MARKERS:
                if not self._process_marker():
                    return
            else:
                return

    def _validate_header(self, h: SparseExtentHeader) -> None:
        if not h.is_stream_optimized:
            raise VmdkFormatError(
                f"extent flags 0x{h.flags:x} do not indicate a stream-optimized (compressed+markers) VMDK"
            )
        if h.compress_algorithm != COMPRESSION_DEFLATE:
            raise VmdkFormatError(f"unsupported compression algorithm {h.compress_algorithm}")
        if h.overhead_sectors < 1:
            raise VmdkFormatError("overHead must be at least one sector")
        if self._expected_capacity is not None and h.capacity_bytes != self._expected_capacity:
            raise VmdkFormatError(
                f"VMDK capacity {h.capacity_bytes} bytes does not match expected {self._expected_capacity}"
            )

    def _extract_descriptor(self, preamble: bytes) -> None:
        assert self.header is not None
        start = (self.header.descriptor_offset - 1) * SECTOR
        end = start + self.header.descriptor_sectors * SECTOR
        if self.header.descriptor_offset >= 1 and 0 <= start < end <= len(preamble):
            raw = preamble[start:end].split(b"\0", 1)[0]
            self.descriptor = raw.decode("utf-8", errors="replace")

    def _process_marker(self) -> bool:
        """Returns True when a marker was fully consumed, False when more data is needed."""
        assert self.header is not None
        if len(self._buf) < SECTOR:
            return False
        val, size, mtype = MARKER_STRUCT.unpack_from(self._buf, 0)
        if size > 0:
            # grain marker: the deflate stream starts at byte 12 (where a metadata marker keeps its type)
            total = _round_up(GRAIN_MARKER_STRUCT.size + size, SECTOR)
            if len(self._buf) < total:
                return False
            compressed = bytes(self._buf[GRAIN_MARKER_STRUCT.size : GRAIN_MARKER_STRUCT.size + size])
            del self._buf[:total]
            self._emit_grain(val, compressed)
            return True

        # metadata marker
        if mtype == MARKER_EOS:
            del self._buf[:SECTOR]
            self._state = self._ST_DONE
            self.stats.metadata_markers += 1
            return True
        if mtype in (MARKER_GT, MARKER_GD, MARKER_FOOTER):
            total = SECTOR + val * SECTOR
            if len(self._buf) < total:
                return False
            del self._buf[:total]
            self.stats.metadata_markers += 1
            return True
        raise VmdkFormatError(f"unknown marker type {mtype} (val={val})")

    def _emit_grain(self, lba: int, compressed: bytes) -> None:
        assert self.header is not None
        try:
            data = zlib.decompress(compressed)
        except zlib.error as exc:
            raise VmdkFormatError(f"grain at LBA {lba} failed to decompress: {exc}") from exc
        offset = lba * SECTOR
        cap = self.header.capacity_bytes
        if offset >= cap:
            raise VmdkFormatError(f"grain LBA {lba} is beyond the extent capacity")
        if offset + len(data) > cap:
            data = data[: cap - offset]
        if self._skip_zero and (data == self._zero_grain or not any(data)):
            return
        self._write(offset, data)
        self.stats.grains_written += 1
        self.stats.bytes_written += len(data)


# --------------------------------------------------------------------------- #
# Encoder (used for the seed image placeholder and for test fixtures)
# --------------------------------------------------------------------------- #
def _round_up(n: int, to: int) -> int:
    return (n + to - 1) // to * to


def build_descriptor(capacity_sectors: int, extent_name: str = "disk.vmdk", adapter: str = "lsilogic") -> str:
    cylinders = max(1, capacity_sectors // (16 * 63))
    return (
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=fffffffe\n"
        "parentCID=ffffffff\n"
        'createType="streamOptimized"\n'
        "\n"
        "# Extent description\n"
        f'RW {capacity_sectors} SPARSE "{extent_name}"\n'
        "\n"
        "# The Disk Data Base\n"
        "#DDB\n"
        "\n"
        f'ddb.adapterType = "{adapter}"\n'
        f'ddb.geometry.cylinders = "{cylinders}"\n'
        'ddb.geometry.heads = "16"\n'
        'ddb.geometry.sectors = "63"\n'
        'ddb.virtualHWVersion = "4"\n'
    )


def _marker(val: int, size: int, mtype: int) -> bytes:
    return MARKER_STRUCT.pack(val, size, mtype).ljust(SECTOR, b"\0")


def iter_grains_from_file(src: BinaryIO, grain_bytes: int) -> Iterator[tuple[int, bytes]]:
    """Yield (lba, data) for each non-zero grain of a raw image."""
    lba = 0
    while True:
        data = src.read(grain_bytes)
        if not data:
            return
        if any(data):
            yield lba, data
        lba += grain_bytes // SECTOR


def encode_stream_optimized(
    out: BinaryIO,
    capacity_bytes: int,
    grains: Iterable[tuple[int, bytes]],
    grain_sectors: int = DEFAULT_GRAIN_SECTORS,
    overhead_sectors: int = DEFAULT_OVERHEAD_SECTORS,
    compress_level: int = 6,
    extent_name: str = "disk.vmdk",
) -> int:
    """Write a stream-optimized VMDK.  ``grains`` yields (lba_sector, data) with data
    being at most one grain; all-zero grains may be omitted.  Returns bytes written.
    """
    if capacity_bytes % SECTOR:
        raise ValueError("capacity must be a multiple of 512 bytes")
    capacity_sectors = capacity_bytes // SECTOR
    grain_bytes = grain_sectors * SECTOR
    gtes = DEFAULT_GTES_PER_GT
    num_grains = _round_up(capacity_sectors, grain_sectors) // grain_sectors
    num_gts = max(1, _round_up(num_grains, gtes) // gtes)

    descriptor = build_descriptor(capacity_sectors, extent_name).encode()
    desc_sectors = max(1, _round_up(len(descriptor), SECTOR) // SECTOR)
    if 1 + desc_sectors > overhead_sectors:
        overhead_sectors = _round_up(1 + desc_sectors, DEFAULT_OVERHEAD_SECTORS)

    header = SparseExtentHeader(
        version=3,
        flags=FLAG_NEWLINE_VALID | FLAG_COMPRESSED | FLAG_MARKERS,
        capacity_sectors=capacity_sectors,
        grain_sectors=grain_sectors,
        descriptor_offset=1,
        descriptor_sectors=desc_sectors,
        gtes_per_gt=gtes,
        rgd_offset=0,
        gd_offset=GD_AT_END,
        overhead_sectors=overhead_sectors,
        unclean_shutdown=False,
        compress_algorithm=COMPRESSION_DEFLATE,
    )

    written = 0

    def emit(b: bytes) -> None:
        nonlocal written
        out.write(b)
        written += len(b)

    emit(header.pack())
    emit(descriptor.ljust(desc_sectors * SECTOR, b"\0"))
    emit(b"\0" * ((overhead_sectors - 1 - desc_sectors) * SECTOR))

    gt_entries = [0] * (num_gts * gtes)
    for lba, data in grains:
        if lba % grain_sectors:
            raise ValueError(f"grain LBA {lba} is not aligned to the grain size")
        if len(data) > grain_bytes:
            raise ValueError("grain larger than grain size")
        if not any(data):
            continue
        if len(data) < grain_bytes:
            data = data.ljust(grain_bytes, b"\0")
        comp = zlib.compress(data, compress_level)
        sector_pos = written // SECTOR
        gt_entries[lba // grain_sectors] = sector_pos
        body = GRAIN_MARKER_STRUCT.pack(lba, len(comp)) + comp
        emit(body.ljust(_round_up(len(body), SECTOR), b"\0"))

    gt_sectors = gtes * 4 // SECTOR
    gd_entries: list[int] = []
    for i in range(num_gts):
        emit(_marker(gt_sectors, 0, MARKER_GT))
        gd_entries.append(written // SECTOR)
        emit(struct.pack(f"<{gtes}I", *gt_entries[i * gtes : (i + 1) * gtes]))

    gd_bytes = struct.pack(f"<{num_gts}I", *gd_entries)
    gd_sectors = _round_up(len(gd_bytes), SECTOR) // SECTOR
    emit(_marker(gd_sectors, 0, MARKER_GD))
    gd_offset = written // SECTOR
    emit(gd_bytes.ljust(gd_sectors * SECTOR, b"\0"))

    footer = SparseExtentHeader(**{**header.__dict__, "gd_offset": gd_offset})
    emit(_marker(1, 0, MARKER_FOOTER))
    emit(footer.pack())
    emit(_marker(0, 0, MARKER_EOS))
    return written


def encode_raw_bytes(raw: bytes, **kwargs) -> bytes:
    """Convenience: encode an in-memory raw image (capacity = len(raw), padded to a sector)."""
    capacity = _round_up(len(raw), SECTOR)
    raw = raw.ljust(capacity, b"\0")
    out = io.BytesIO()
    grain_bytes = kwargs.get("grain_sectors", DEFAULT_GRAIN_SECTORS) * SECTOR
    encode_stream_optimized(out, capacity, iter_grains_from_file(io.BytesIO(raw), grain_bytes), **kwargs)
    return out.getvalue()


def encode_empty_disk(capacity_bytes: int, **kwargs) -> bytes:
    """A fully unallocated disk; used as the placeholder for OCI seed images."""
    out = io.BytesIO()
    encode_stream_optimized(out, capacity_bytes, [], **kwargs)
    return out.getvalue()
