import hashlib
import io
import os
import random
import struct

import pytest

from helper_app.disk import vmdk_stream as vs
from helper_app.disk.writer import BlockDeviceWriter


class MemSink:
    def __init__(self, capacity: int):
        self.buf = bytearray(capacity)
        self.writes = 0

    def write_at(self, offset: int, data: bytes) -> None:
        self.buf[offset : offset + len(data)] = data
        self.writes += 1


def make_raw(size: int, seed: int = 1) -> bytes:
    """Sparse-ish raw image: some random grains, some zero grains, partial data at the end."""
    rnd = random.Random(seed)
    raw = bytearray(size)
    grain = vs.DEFAULT_GRAIN_SECTORS * vs.SECTOR
    for g in range(size // grain):
        if g % 3 == 0:
            raw[g * grain : (g + 1) * grain] = rnd.randbytes(grain)
        elif g % 3 == 1:
            # partially filled grain
            raw[g * grain + 100 : g * grain + 5000] = rnd.randbytes(4900)
    return bytes(raw)


@pytest.mark.parametrize("size_mb", [1, 3])
def test_roundtrip_full_feed(size_mb):
    raw = make_raw(size_mb * 1024 * 1024)
    encoded = vs.encode_raw_bytes(raw)
    sink = MemSink(len(raw))
    dec = vs.StreamOptimizedDecoder(sink.write_at, expected_capacity_bytes=len(raw))
    dec.feed(encoded)
    stats = dec.finish()
    assert dec.finished
    assert bytes(sink.buf) == raw
    assert stats.bytes_consumed == len(encoded)
    assert stats.grains_written == sink.writes
    assert 'createType="streamOptimized"' in dec.descriptor


@pytest.mark.parametrize("chunk", [1, 7, 511, 512, 513, 4096, 65536 + 13])
def test_roundtrip_chunked(chunk):
    raw = make_raw(2 * 1024 * 1024, seed=chunk)
    encoded = vs.encode_raw_bytes(raw)
    sink = MemSink(len(raw))
    dec = vs.StreamOptimizedDecoder(sink.write_at)
    for i in range(0, len(encoded), chunk):
        dec.feed(encoded[i : i + chunk])
    dec.finish()
    assert bytes(sink.buf) == raw


def test_header_roundtrip_and_layout():
    h = vs.SparseExtentHeader(
        version=3,
        flags=vs.FLAG_NEWLINE_VALID | vs.FLAG_COMPRESSED | vs.FLAG_MARKERS,
        capacity_sectors=2048,
        grain_sectors=128,
        descriptor_offset=1,
        descriptor_sectors=20,
        gtes_per_gt=512,
        rgd_offset=0,
        gd_offset=vs.GD_AT_END,
        overhead_sectors=128,
        unclean_shutdown=False,
        compress_algorithm=1,
    )
    packed = h.pack()
    assert len(packed) == 512
    assert packed[:4] == b"KDMV"
    assert packed[77:79] == struct.pack("<H", 1)  # compressAlgorithm position
    assert vs.SparseExtentHeader.unpack(packed) == h


def test_empty_disk_has_no_grains():
    cap = 64 * 1024 * 1024
    encoded = vs.encode_empty_disk(cap)
    # overhead (128 sectors) + 2 GTs (4 sectors each + marker) + GD + footer + EOS, but no grain data
    assert len(encoded) < 80 * 1024
    sink = MemSink(cap)
    dec = vs.StreamOptimizedDecoder(sink.write_at, expected_capacity_bytes=cap)
    dec.feed(encoded)
    stats = dec.finish()
    assert stats.grains_written == 0
    assert dec.header.capacity_bytes == cap


def test_bad_magic_rejected():
    dec = vs.StreamOptimizedDecoder(lambda o, d: None)
    with pytest.raises(vs.VmdkFormatError):
        dec.feed(b"XXXX" + b"\0" * 508)


def test_capacity_mismatch_rejected():
    raw = make_raw(1024 * 1024)
    encoded = vs.encode_raw_bytes(raw)
    dec = vs.StreamOptimizedDecoder(lambda o, d: None, expected_capacity_bytes=len(raw) * 2)
    with pytest.raises(vs.VmdkFormatError):
        dec.feed(encoded)


def test_truncated_stream_detected():
    raw = make_raw(1024 * 1024)
    encoded = vs.encode_raw_bytes(raw)
    dec = vs.StreamOptimizedDecoder(lambda o, d: None)
    dec.feed(encoded[: len(encoded) // 2 + 3])
    with pytest.raises(vs.VmdkFormatError):
        dec.finish()


def test_trailing_bytes_after_eos_ignored():
    raw = make_raw(1024 * 1024)
    encoded = vs.encode_raw_bytes(raw) + b"\0" * 4096
    sink = MemSink(len(raw))
    dec = vs.StreamOptimizedDecoder(sink.write_at)
    dec.feed(encoded)
    dec.finish()
    assert bytes(sink.buf) == raw


def test_corrupt_grain_rejected():
    raw = make_raw(1024 * 1024)
    encoded = bytearray(vs.encode_raw_bytes(raw))
    # first grain marker sits at sector `overhead`; corrupt its compressed payload
    pos = vs.DEFAULT_OVERHEAD_SECTORS * vs.SECTOR + 12 + 5
    encoded[pos] ^= 0xFF
    encoded[pos + 1] ^= 0xFF
    dec = vs.StreamOptimizedDecoder(lambda o, d: None)
    with pytest.raises(vs.VmdkFormatError):
        dec.feed(bytes(encoded))


def test_grain_marker_wire_layout_matches_spec():
    """Marker = {uint64 val; uint32 size; union {uint32 type; uint8 data[0]}}: a grain's deflate stream
    starts at byte 12, where a metadata marker keeps its type.  Encoder and decoder must both follow the
    spec (not merely agree with each other), or ESXi exports fail to decode and OCI rejects our VMDKs."""
    import zlib

    grain = vs.DEFAULT_GRAIN_SECTORS * vs.SECTOR
    payload = bytes(range(256)) * (grain // 256)

    # encoder: first grain marker sits at sector `overhead`; bytes 12.. are the zlib stream (0x78 header)
    encoded = vs.encode_raw_bytes(payload)
    marker = encoded[vs.DEFAULT_OVERHEAD_SECTORS * vs.SECTOR :]
    lba, size = struct.unpack_from("<QI", marker, 0)
    assert (lba, marker[12]) == (0, 0x78)
    assert zlib.decompress(marker[12 : 12 + size]) == payload

    # decoder: hand-built spec-conformant stream (grain at LBA 128 followed by GT/GD/footer/EOS)
    comp = zlib.compress(payload)
    header = vs.SparseExtentHeader(version=3, flags=vs.FLAG_NEWLINE_VALID | vs.FLAG_COMPRESSED | vs.FLAG_MARKERS,
                                   capacity_sectors=2 * vs.DEFAULT_GRAIN_SECTORS, grain_sectors=vs.DEFAULT_GRAIN_SECTORS,
                                   descriptor_offset=1, descriptor_sectors=1, gtes_per_gt=512, rgd_offset=0,
                                   gd_offset=vs.GD_AT_END, overhead_sectors=128, unclean_shutdown=False,
                                   compress_algorithm=1)
    stream = header.pack() + b"\0" * (127 * vs.SECTOR)
    body = struct.pack("<QI", vs.DEFAULT_GRAIN_SECTORS, len(comp)) + comp
    stream += body.ljust((len(body) + vs.SECTOR - 1) // vs.SECTOR * vs.SECTOR, b"\0")
    stream += struct.pack("<QII", 4, 0, vs.MARKER_GT).ljust(vs.SECTOR, b"\0") + b"\0" * 4 * vs.SECTOR
    stream += struct.pack("<QII", 1, 0, vs.MARKER_GD).ljust(vs.SECTOR, b"\0") + b"\0" * vs.SECTOR
    stream += struct.pack("<QII", 1, 0, vs.MARKER_FOOTER).ljust(vs.SECTOR, b"\0") + header.pack()
    stream += struct.pack("<QII", 0, 0, vs.MARKER_EOS).ljust(vs.SECTOR, b"\0")
    sink = MemSink(2 * grain)
    dec = vs.StreamOptimizedDecoder(sink.write_at)
    dec.feed(stream)
    dec.finish()
    assert bytes(sink.buf) == b"\0" * grain + payload


def test_block_device_writer_on_regular_file(tmp_path):
    raw = make_raw(1024 * 1024, seed=42)
    encoded = vs.encode_raw_bytes(raw)
    target = tmp_path / "vol.img"
    with BlockDeviceWriter(target, create=True) as w:
        w.ensure_size(len(raw))
        dec = vs.StreamOptimizedDecoder(w.write_at, expected_capacity_bytes=len(raw))
        dec.feed(encoded)
        dec.finish()
    assert target.read_bytes() == raw
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_block_device_writer_extends_regular_file(tmp_path):
    target = tmp_path / "small.img"
    target.write_bytes(b"\0" * 1024)
    with BlockDeviceWriter(target, expected_min_size=4096) as w:
        assert w.size == 4096
    assert target.stat().st_size == 4096


def test_block_device_writer_rejects_small_block_device(tmp_path, monkeypatch):
    """Simulate a block device that is smaller than the source disk."""
    import stat as st_mod

    target = tmp_path / "blk"
    target.write_bytes(b"\0" * 1024)
    real_fstat = os.fstat

    class FakeStat:
        def __init__(self, real):
            self.st_mode = st_mod.S_IFBLK | 0o600
            self.st_size = real.st_size

    monkeypatch.setattr(os, "fstat", lambda fd: FakeStat(real_fstat(fd)))
    with pytest.raises(ValueError, match="smaller than the required"):
        BlockDeviceWriter(target, expected_min_size=4096)


def test_iter_grains_skips_zero():
    grain = vs.DEFAULT_GRAIN_SECTORS * vs.SECTOR
    raw = b"\0" * grain + b"\1" * grain + b"\0" * grain
    grains = list(vs.iter_grains_from_file(io.BytesIO(raw), grain))
    assert grains == [(vs.DEFAULT_GRAIN_SECTORS, b"\1" * grain)]


@pytest.mark.skipif(not os.environ.get("QEMU_IMG"), reason="set QEMU_IMG=/path/to/qemu-img to cross-check")
def test_cross_check_with_qemu_img(tmp_path):
    """Optional: verify we decode a qemu-img produced stream-optimized VMDK."""
    import subprocess

    raw = make_raw(4 * 1024 * 1024, seed=7)
    raw_path = tmp_path / "src.raw"
    raw_path.write_bytes(raw)
    vmdk_path = tmp_path / "src.vmdk"
    subprocess.run(
        [os.environ["QEMU_IMG"], "convert", "-f", "raw", "-O", "vmdk", "-o", "subformat=streamOptimized",
         str(raw_path), str(vmdk_path)],
        check=True,
    )
    sink = MemSink(len(raw))
    dec = vs.StreamOptimizedDecoder(sink.write_at, expected_capacity_bytes=len(raw))
    dec.feed(vmdk_path.read_bytes())
    dec.finish()
    assert bytes(sink.buf) == raw
