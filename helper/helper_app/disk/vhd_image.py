"""Read a VHD or VHDX (fixed, dynamic, or differencing) as a virtual disk.

Unallocated blocks are skipped. A differencing file is read together with its parent: a sector comes
from the child when the sector bitmap says it is present, and from the parent otherwise. Nothing here
writes to the source files.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

MIB = 1024 * 1024

# VHDX region and metadata item identifiers (mixed-endian GUID layout).
_BAT_GUID = uuid.UUID("2dc27766-f623-4200-9d64-115e9bfd4a08")
_META_GUID = uuid.UUID("8b7ca206-4790-4b9a-b8fe-575f050f886e")
_FILE_PARAMS = uuid.UUID("caa16737-fa36-4d43-b3b6-33f0aa44e76b")
_VDISK_SIZE = uuid.UUID("2fa54224-cd1b-4876-b211-5dbed83bf4b8")
_LOGICAL_SECTOR = uuid.UUID("8141bf1d-a96f-4709-ba47-f233a8faab5f")

_NOT_PRESENT = 0
_ZERO = 2
_UNMAPPED = 3
_FULL = 6
_PARTIAL = 7

_VHD_FIXED = 2
_VHD_DYNAMIC = 3
_VHD_DIFF = 4


class ImageError(Exception):
    """The file is not a readable VHD/VHDX, or a differencing parent is missing."""


class DiskReader(Protocol):
    size: int

    def read_at(self, offset: int, length: int) -> bytes: ...


@dataclass
class _Span:
    start: int
    length: int
    mode: str  # data | zero | parent | partial
    data_offset: int = 0
    bitmap_offset: int = 0
    bitmap_bit0: int = 0
    sector: int = 512


def _coalesce(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [list(ordered[0])]
    for start, length in ordered[1:]:
        end = out[-1][0] + out[-1][1]
        if start <= end:
            out[-1][1] = max(end, start + length) - out[-1][0]
        else:
            out.append([start, length])
    return [(start, length) for start, length in out if length > 0]


def _clip(ranges: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    clipped = []
    for rs, rl in ranges:
        a = max(rs, start)
        b = min(rs + rl, end)
        if b > a:
            clipped.append((a, b - a))
    return clipped


class VirtualDisk:
    """One VHD/VHDX file, optionally chained to the parent it differences against."""

    def __init__(self, reader: DiskReader, virtual_size: int, spans: list[_Span], block_size: int, *,
                 parent: Optional["VirtualDisk"] = None, sector: int = 512):
        self.reader = reader
        self.virtual_size = virtual_size
        self.spans = spans
        self.block_size = block_size
        self.parent = parent
        self.sector = sector
        self._allocated: Optional[list[tuple[int, int]]] = None

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.virtual_size:
            raise ImageError(f"read {offset}+{length} is outside the {self.virtual_size}-byte disk")
        if length == 0:
            return b""
        out = bytearray()
        pos = offset
        end = offset + length
        while pos < end:
            span = self._span_for(pos)
            take = min(end, span.start + span.length) - pos
            out += self._read_span(span, pos, take)
            pos += take
        return bytes(out)

    def allocated(self) -> list[tuple[int, int]]:
        """Virtual ranges that hold disk data. Unallocated and explicit-zero blocks are omitted."""
        if self._allocated is not None:
            return self._allocated
        parent_ranges = self.parent.allocated() if self.parent is not None else []
        ranges: list[tuple[int, int]] = []
        for span in self.spans:
            if span.mode == "data":
                ranges.append((span.start, span.length))
            elif span.mode == "parent":
                ranges.extend(_clip(parent_ranges, span.start, span.start + span.length))
            elif span.mode == "partial":
                ranges.extend(self._partial_ranges(span, parent_ranges))
        self._allocated = _coalesce(ranges)
        return self._allocated

    def _span_for(self, pos: int) -> _Span:
        if self.block_size <= 0:
            return self.spans[0]
        index = min(pos // self.block_size, len(self.spans) - 1)
        return self.spans[index]

    def _read_span(self, span: _Span, pos: int, length: int) -> bytes:
        if span.mode == "data":
            return self.reader.read_at(span.data_offset + (pos - span.start), length)
        if span.mode == "zero":
            return b"\x00" * length
        if span.mode == "parent":
            if self.parent is not None:
                return self.parent.read(pos, length)
            return b"\x00" * length
        return self._read_partial(span, pos, length)

    def _read_partial(self, span: _Span, pos: int, length: int) -> bytes:
        return self._take_run(span, self._load_bitmap(span), pos, pos + length)

    def _sector_present(self, span: _Span, bitmap: bytes, sector_index: int) -> bool:
        bit = span.bitmap_bit0 + sector_index
        byte_index = bit // 8
        if byte_index >= len(bitmap):
            return False
        return (bitmap[byte_index] >> (bit % 8)) & 1 == 1

    def _load_bitmap(self, span: _Span) -> bytes:
        sectors = (span.length + span.sector - 1) // span.sector
        last_bit = span.bitmap_bit0 + sectors
        first_byte = span.bitmap_bit0 // 8
        nbytes = (last_bit + 7) // 8 - first_byte
        raw = self.reader.read_at(span.bitmap_offset + first_byte, nbytes)
        return b"\x00" * first_byte + raw

    def _take_run(self, span: _Span, bitmap: bytes, pos: int, end: int) -> bytes:
        out = bytearray()
        cursor = pos
        while cursor < end:
            sector_index = (cursor - span.start) // self.sector
            present = self._sector_present(span, bitmap, sector_index)
            run_end = cursor
            while run_end < end:
                idx = (run_end - span.start) // self.sector
                if self._sector_present(span, bitmap, idx) != present:
                    break
                run_end = min(end, span.start + (idx + 1) * self.sector)
            length = run_end - cursor
            if present:
                rel = cursor - span.start
                out += self.reader.read_at(span.data_offset + rel, length)
            elif self.parent is not None:
                out += self.parent.read(cursor, length)
            else:
                out += b"\x00" * length
            cursor = run_end
        return bytes(out)

    def _partial_ranges(self, span: _Span, parent_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        bitmap = self._load_bitmap(span)
        ranges: list[tuple[int, int]] = []
        sectors = (span.length + span.sector - 1) // span.sector
        index = 0
        while index < sectors:
            present = self._sector_present(span, bitmap, index)
            nxt = index + 1
            while nxt < sectors and self._sector_present(span, bitmap, nxt) == present:
                nxt += 1
            start = span.start + index * span.sector
            end = min(span.start + span.length, span.start + nxt * span.sector)
            if present:
                ranges.append((start, end - start))
            else:
                ranges.extend(_clip(parent_ranges, start, end))
            index = nxt
        return ranges


def open_image(reader: DiskReader, parent: Optional[VirtualDisk] = None) -> VirtualDisk:
    """Parse ``reader`` as VHDX or VHD. ``parent`` is the differencing parent, leaf toward the root."""
    if reader.size >= 8 and reader.read_at(0, 8) == b"vhdxfile":
        return _open_vhdx(reader, parent)
    if reader.size >= 512:
        footer = reader.read_at(reader.size - 512, 512)
        if footer[:8] == b"conectix":
            return _open_vhd(reader, footer, parent)
    raise ImageError("not a VHD or VHDX file")


def open_chain(readers: list[DiskReader]) -> VirtualDisk:
    """``readers`` is leaf first, then each parent. The returned disk reads the leaf's view."""
    if not readers:
        raise ImageError("disk chain is empty")
    disk: Optional[VirtualDisk] = None
    for reader in reversed(readers):
        disk = open_image(reader, parent=disk)
    assert disk is not None
    return disk


def _open_vhd(reader: DiskReader, footer: bytes, parent: Optional[VirtualDisk]) -> VirtualDisk:
    data_offset, _original, virtual_size, disk_type = struct.unpack(">8x4x4xQ4x4x4x4xQQ4xI", footer[:64])
    if disk_type == _VHD_FIXED:
        span = _Span(0, virtual_size, "data", data_offset=0)
        return VirtualDisk(reader, virtual_size, [span], virtual_size or 1, parent=parent)
    if disk_type not in (_VHD_DYNAMIC, _VHD_DIFF):
        raise ImageError(f"unsupported VHD type {disk_type}")
    if disk_type == _VHD_DIFF and parent is None:
        raise ImageError("differencing disk is missing its parent file")
    header = reader.read_at(data_offset, 1024)
    if header[:8] != b"cxsparse":
        raise ImageError("VHD dynamic header is missing")
    table_offset, _version, entries, block_size = struct.unpack(">8x8xQIII", header[:36])
    if block_size <= 0 or entries < 0:
        raise ImageError("VHD block allocation table is empty")
    bat = reader.read_at(table_offset, entries * 4)
    sectors = block_size // 512
    bitmap_size = ((sectors // 8 + 511) // 512) * 512
    spans: list[_Span] = []
    for index in range(entries):
        start = index * block_size
        if start >= virtual_size:
            break
        length = min(block_size, virtual_size - start)
        sector_off = struct.unpack(">I", bat[index * 4:(index + 1) * 4])[0]
        if sector_off == 0xFFFFFFFF:
            spans.append(_Span(start, length, "parent" if disk_type == _VHD_DIFF else "zero"))
            continue
        file_off = sector_off * 512
        if disk_type == _VHD_DIFF:
            spans.append(_Span(start, length, "partial", data_offset=file_off + bitmap_size,
                               bitmap_offset=file_off, sector=512))
        else:
            spans.append(_Span(start, length, "data", data_offset=file_off + bitmap_size))
    return VirtualDisk(reader, virtual_size, spans, block_size, parent=parent)


def _open_vhdx(reader: DiskReader, parent: Optional[VirtualDisk]) -> VirtualDisk:
    header = _best_header(reader)
    if header is None:
        raise ImageError("VHDX header is missing")
    regions = _region_table(reader)
    if _BAT_GUID not in regions or _META_GUID not in regions:
        raise ImageError("VHDX region table has no BAT or metadata")
    meta_off, meta_len = regions[_META_GUID]
    bat_off, bat_len = regions[_BAT_GUID]
    items = _metadata_items(reader, meta_off, meta_len)
    if _FILE_PARAMS not in items or _VDISK_SIZE not in items:
        raise ImageError("VHDX metadata is missing the file parameters or the virtual size")
    block_size, flags = struct.unpack("<II", items[_FILE_PARAMS][:8])
    virtual_size = struct.unpack("<Q", items[_VDISK_SIZE][:8])[0]
    sector = struct.unpack("<I", items[_LOGICAL_SECTOR][:4])[0] if _LOGICAL_SECTOR in items else 512
    has_parent = bool(flags & 2)
    if has_parent and parent is None:
        raise ImageError("differencing disk is missing its parent file")
    if block_size <= 0 or sector <= 0 or virtual_size < 0:
        raise ImageError("VHDX geometry is empty")
    chunk_ratio = (1 << 23) * sector // block_size
    if chunk_ratio <= 0:
        raise ImageError("VHDX block size is larger than a chunk")
    nblocks = (virtual_size + block_size - 1) // block_size
    bat = reader.read_at(bat_off, bat_len)
    spans: list[_Span] = []
    for index in range(nblocks):
        start = index * block_size
        length = min(block_size, virtual_size - start)
        # Hyper-V reserves a sector-bitmap slot after every chunk even when the disk has no parent.
        # Treating that slot as payload shifts every block past the first chunk (4 GB at 32 MB blocks).
        bat_index = index + index // chunk_ratio
        state, file_off = _bat_entry(bat, bat_index)
        if state == _FULL:
            spans.append(_Span(start, length, "data", data_offset=file_off, sector=sector))
        elif state in (_ZERO, _UNMAPPED):
            spans.append(_Span(start, length, "zero", sector=sector))
        elif state == _PARTIAL and has_parent:
            sb_index = (index // chunk_ratio) * (chunk_ratio + 1) + chunk_ratio
            sb_state, sb_off = _bat_entry(bat, sb_index)
            bit0 = (index % chunk_ratio) * (block_size // sector)
            if sb_state != _FULL:
                spans.append(_Span(start, length, "parent", sector=sector))
            else:
                spans.append(_Span(start, length, "partial", data_offset=file_off, bitmap_offset=sb_off,
                                   bitmap_bit0=bit0, sector=sector))
        elif has_parent:
            spans.append(_Span(start, length, "parent", sector=sector))
        else:
            spans.append(_Span(start, length, "zero", sector=sector))
    return VirtualDisk(reader, virtual_size, spans, block_size, parent=parent, sector=sector)


def _best_header(reader: DiskReader) -> Optional[bytes]:
    best: Optional[tuple[int, bytes]] = None
    for offset in (64 * 1024, 128 * 1024):
        if reader.size < offset + 80:
            continue
        blob = reader.read_at(offset, 80)
        if blob[:4] != b"head":
            continue
        sequence = struct.unpack_from("<Q", blob, 8)[0]
        if best is None or sequence > best[0]:
            best = (sequence, blob)
    return None if best is None else best[1]


def _region_table(reader: DiskReader) -> dict[uuid.UUID, tuple[int, int]]:
    for offset in (192 * 1024, 256 * 1024):
        if reader.size < offset + 16:
            continue
        header = reader.read_at(offset, 16)
        if header[:4] != b"regi":
            continue
        count = struct.unpack_from("<I", header, 8)[0]
        table = reader.read_at(offset, 16 + count * 32)
        regions = {}
        for index in range(count):
            entry = table[16 + index * 32:16 + (index + 1) * 32]
            guid = uuid.UUID(bytes_le=entry[:16])
            file_off, length = struct.unpack_from("<QI", entry, 16)
            regions[guid] = (file_off, length)
        return regions
    raise ImageError("VHDX region table is missing")


def _metadata_items(reader: DiskReader, offset: int, length: int) -> dict[uuid.UUID, bytes]:
    blob = reader.read_at(offset, length)
    if blob[:8] != b"metadata":
        raise ImageError("VHDX metadata table is missing")
    count = struct.unpack_from("<H", blob, 10)[0]
    items = {}
    for index in range(count):
        entry = blob[32 + index * 32:32 + (index + 1) * 32]
        guid = uuid.UUID(bytes_le=entry[:16])
        item_off, item_len = struct.unpack_from("<II", entry, 16)
        items[guid] = blob[item_off:item_off + item_len]
    return items


def _bat_entry(bat: bytes, index: int) -> tuple[int, int]:
    start = index * 8
    if start + 8 > len(bat):
        return _NOT_PRESENT, 0
    entry = struct.unpack_from("<Q", bat, start)[0]
    return entry & 7, (entry >> 20) * MIB

