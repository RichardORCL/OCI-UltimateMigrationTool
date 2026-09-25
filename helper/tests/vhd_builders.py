"""Small VHD and VHDX files for the Hyper-V reader tests.

The layout matches ``helper_app.disk.vhd_image``: dynamic VHD blocks carry a sector bitmap in
front of the payload, and a VHDX block allocation table keeps a sector-bitmap slot after every
chunk, including a dynamic disk that has no parent.
"""

from __future__ import annotations

import struct
import uuid

MIB = 1024 * 1024

_BAT_GUID = uuid.UUID("2dc27766-f623-4200-9d64-115e9bfd4a08")
_META_GUID = uuid.UUID("8b7ca206-4790-4b9a-b8fe-575f050f886e")
_FILE_PARAMS = uuid.UUID("caa16737-fa36-4d43-b3b6-33f0aa44e76b")
_VDISK_SIZE = uuid.UUID("2fa54224-cd1b-4876-b211-5dbed83bf4b8")
_LOGICAL_SECTOR = uuid.UUID("8141bf1d-a96f-4709-ba47-f233a8faab5f")
_PHYSICAL_SECTOR = uuid.UUID("cda348c7-445d-4471-9cc9-e9885251c556")

_PARTIAL = 7
_FULL = 6


def build_fixed_vhd(payload: bytes) -> bytes:
    return payload + _vhd_footer(0xFFFFFFFFFFFFFFFF, len(payload), 2)


def build_dynamic_vhd(virtual_size: int, block_size: int, blocks: dict[int, bytes]) -> bytes:
    """Dynamic VHD. ``blocks`` maps a block index to that block's payload (shorter payloads are padded)."""
    count = virtual_size // block_size
    sectors = block_size // 512
    bitmap_size = ((sectors // 8 + 511) // 512) * 512
    header_at = 512
    bat_at = header_at + 1024
    bat_bytes = count * 4
    bat_padded = ((bat_bytes + 511) // 512) * 512
    cursor = bat_at + bat_padded
    bat = bytearray(bat_padded)
    body = bytearray()
    for index in range(count):
        if index not in blocks:
            struct.pack_into(">I", bat, index * 4, 0xFFFFFFFF)
            continue
        struct.pack_into(">I", bat, index * 4, cursor // 512)
        payload = blocks[index]
        if len(payload) < block_size:
            payload = payload + b"\x00" * (block_size - len(payload))
        body += b"\xff" * bitmap_size + payload[:block_size]
        cursor += bitmap_size + block_size
    footer = _vhd_footer(header_at, virtual_size, 3)
    header = bytearray(1024)
    header[0:8] = b"cxsparse"
    struct.pack_into(">Q", header, 16, bat_at)
    struct.pack_into(">I", header, 24, 0x00010000)
    struct.pack_into(">I", header, 28, count)
    struct.pack_into(">I", header, 32, block_size)
    return footer + bytes(header) + bytes(bat) + bytes(body) + footer


def build_dynamic_vhdx(virtual_size: int, block_size: int, blocks: dict[int, bytes]) -> bytes:
    """Dynamic VHDX with no parent. Missing blocks are left unallocated.

    Payload entry ``i`` is stored at BAT index ``i + i // chunk_ratio``. The slot at the end of
    each chunk is the sector bitmap, left not-present, which is how Hyper-V writes a dynamic disk.
    """
    count = (virtual_size + block_size - 1) // block_size
    chunk_ratio = (1 << 23) * 512 // block_size
    slots = (count + chunk_ratio - 1) // chunk_ratio if count else 0
    bat = bytearray((count + slots) * 8)
    payloads: list[tuple[int, bytes]] = []
    cursor = 3 * MIB
    for index in range(count):
        if index not in blocks:
            continue
        payload = blocks[index]
        if len(payload) < block_size:
            payload = payload + b"\x00" * (block_size - len(payload))
        _put_bat(bat, index + index // chunk_ratio, _FULL, cursor)
        payloads.append((cursor, payload[:block_size]))
        cursor += block_size
    return _assemble_vhdx(virtual_size, block_size, has_parent=False, bat=bat, payloads=payloads)


def build_differencing_vhdx(virtual_size: int, block_size: int, sector: int,
                            present: dict[int, bytes]) -> bytes:
    """Differencing VHDX. ``present`` maps a sector index in block 0 to that sector's bytes.

    Other blocks are not present, so a read falls through to the parent.
    """
    chunk_ratio = (1 << 23) * sector // block_size
    sb_index = chunk_ratio  # bitmap covering block 0
    entries = sb_index + 1
    bat = bytearray(entries * 8)
    data_at = 3 * MIB
    bitmap_at = 4 * MIB
    _put_bat(bat, 0, _PARTIAL, data_at)
    _put_bat(bat, sb_index, _FULL, bitmap_at)
    sectors_in_block = block_size // sector
    bitmap = bytearray((sectors_in_block + 7) // 8)
    payload = bytearray(block_size)
    for index, data in present.items():
        if len(data) < sector:
            data = data + b"\x00" * (sector - len(data))
        payload[index * sector:(index + 1) * sector] = data[:sector]
        bitmap[index // 8] |= 1 << (index % 8)
    return _assemble_vhdx(virtual_size, block_size, has_parent=True, bat=bat,
                          payloads=[(data_at, bytes(payload)), (bitmap_at, bytes(bitmap))])


def _put_bat(bat: bytearray, index: int, state: int, file_offset: int) -> None:
    mb = file_offset // MIB
    struct.pack_into("<Q", bat, index * 8, (state & 7) | (mb << 20))


def _assemble_vhdx(virtual_size: int, block_size: int, *, has_parent: bool, bat: bytes,
                   payloads: list[tuple[int, bytes]]) -> bytes:
    meta = _metadata(block_size, virtual_size, has_parent)
    end = 3 * MIB
    for offset, payload in payloads:
        end = max(end, offset + len(payload))
    blob = bytearray(end)
    blob[0:8] = b"vhdxfile"
    blob[64 * 1024:64 * 1024 + 80] = _header(2)
    blob[128 * 1024:128 * 1024 + 80] = _header(1)
    regions = _regions(MIB, len(meta), 2 * MIB, len(bat))
    blob[192 * 1024:192 * 1024 + len(regions)] = regions
    blob[256 * 1024:256 * 1024 + len(regions)] = regions
    blob[MIB:MIB + len(meta)] = meta
    blob[2 * MIB:2 * MIB + len(bat)] = bat
    for offset, payload in payloads:
        blob[offset:offset + len(payload)] = payload
    return bytes(blob)


def _header(sequence: int) -> bytes:
    buf = bytearray(80)
    struct.pack_into("<4sIQ", buf, 0, b"head", 0, sequence)
    struct.pack_into("<H", buf, 66, 1)
    return bytes(buf)


def _regions(meta_off: int, meta_len: int, bat_off: int, bat_len: int) -> bytes:
    buf = bytearray(16 + 64)
    struct.pack_into("<4sIII", buf, 0, b"regi", 0, 2, 0)
    struct.pack_into("<16sQII", buf, 16, _BAT_GUID.bytes_le, bat_off, bat_len, 1)
    struct.pack_into("<16sQII", buf, 48, _META_GUID.bytes_le, meta_off, meta_len, 1)
    return bytes(buf)


def _metadata(block_size: int, virtual_size: int, has_parent: bool) -> bytes:
    items = [
        (_FILE_PARAMS, struct.pack("<II", block_size, 2 if has_parent else 0)),
        (_VDISK_SIZE, struct.pack("<Q", virtual_size)),
        (_LOGICAL_SECTOR, struct.pack("<I", 512)),
        (_PHYSICAL_SECTOR, struct.pack("<I", 4096)),
    ]
    table = 32 + len(items) * 32
    payloads = bytearray()
    entries = bytearray(len(items) * 32)
    for index, (guid, payload) in enumerate(items):
        while (table + len(payloads)) % 8:
            payloads += b"\x00"
        item_off = table + len(payloads)
        struct.pack_into("<16sIIII", entries, index * 32, guid.bytes_le, item_off, len(payload), 6, 0)
        payloads += payload
    header = bytearray(32)
    struct.pack_into("<8sHH", header, 0, b"metadata", 0, len(items))
    return bytes(header + entries + payloads)


def _vhd_footer(data_offset: int, virtual_size: int, disk_type: int) -> bytes:
    buf = bytearray(512)
    buf[0:8] = b"conectix"
    struct.pack_into(">Q", buf, 16, data_offset)
    struct.pack_into(">Q", buf, 40, virtual_size)
    struct.pack_into(">Q", buf, 48, virtual_size)
    struct.pack_into(">I", buf, 60, disk_type)
    return bytes(buf)
