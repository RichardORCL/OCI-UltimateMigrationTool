"""qcow2 header parsing and cluster copy onto a block device (a regular file in tests)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from helper_app.config import get_settings
from helper_app.disk.qcow import QcowError, copy_qcow_object, parse_qcow_header
from helper_app.ova.package import OvaPackageError, parse_and_stage, parse_ova_layout

from .fake_oci import FakeOci

_CLUSTER = 512
_VIRTUAL = 2048


def build_qcow2() -> tuple[bytes, bytes]:
    """A 4-cluster disk: data, a hole, one zlib cluster, and an allocated zero cluster."""
    img = bytearray(_CLUSTER * 6)
    struct.pack_into(">I", img, 0, 0x514649FB)
    struct.pack_into(">I", img, 4, 3)
    struct.pack_into(">I", img, 20, 9)  # cluster_bits
    struct.pack_into(">Q", img, 24, _VIRTUAL)
    struct.pack_into(">I", img, 36, 1)  # l1_size
    struct.pack_into(">Q", img, 40, _CLUSTER)  # l1 at cluster 1
    struct.pack_into(">I", img, 96, 4)  # refcount_order
    struct.pack_into(">I", img, 100, 104)  # header_length
    struct.pack_into(">Q", img, _CLUSTER, _CLUSTER * 2)  # L2 at cluster 2
    img[_CLUSTER * 3:_CLUSTER * 3 + 5] = b"HELLO"
    raw = b"Z" * _CLUSTER
    comp = zlib.compressobj(level=9, wbits=-15)
    cdata = comp.compress(raw) + comp.flush()
    img[_CLUSTER * 4:_CLUSTER * 4 + len(cdata)] = cdata
    compressed = (_CLUSTER * 4) | (1 << 62)  # one sector, additional count 0
    assert cdata and len(cdata) <= 512
    struct.pack_into(">Q", img, _CLUSTER * 2, _CLUSTER * 3)  # guest cluster 0
    struct.pack_into(">Q", img, _CLUSTER * 2 + 16, compressed)  # guest cluster 2
    struct.pack_into(">Q", img, _CLUSTER * 2 + 24, _CLUSTER * 5)  # guest cluster 3, zeros
    guest = bytearray(_VIRTUAL)
    guest[:5] = b"HELLO"
    guest[_CLUSTER * 2:_CLUSTER * 3] = raw
    return bytes(img), bytes(guest)


def _store(name: str = "disk.qcow2") -> tuple[FakeOci, bytes, bytes]:
    blob, guest = build_qcow2()
    fake = FakeOci(get_settings().device_prefix)
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects.setdefault("OVA", {})[name] = blob
    fake.object_storage.add_object("OVA", name, size=len(blob), etag="qcow-etag")
    return fake, blob, guest


def test_parse_reads_the_virtual_size_not_the_file_size():
    fake, blob, _guest = _store()
    layout = parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "disk.qcow2")
    assert layout.disks[0].capacity_bytes == _VIRTUAL
    assert layout.disks[0].capacity_bytes != len(blob)
    assert layout.disks[0].image_format == "qcow2"
    assert layout.disks[0].is_boot is True


def test_copy_writes_allocated_clusters_and_leaves_holes_zero(tmp_path: Path):
    fake, _blob, guest = _store()
    dest = tmp_path / "disk"
    dest.touch()
    received, written = copy_qcow_object(
        fake.clients(), fake.object_storage.NAMESPACE, "OVA", "disk.qcow2", str(dest), _VIRTUAL,
    )
    assert received == _VIRTUAL
    assert written == _CLUSTER * 2  # HELLO cluster and the compressed cluster; the zero cluster is skipped
    assert dest.read_bytes() == guest


def test_qcow_extension_is_accepted():
    fake, _blob, _guest = _store("vm.qcow")
    layout = parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "vm.qcow")
    assert layout.disks[0].image_format == "qcow2"


def test_encrypted_backing_and_zstd_images_are_refused():
    blob, _guest = build_qcow2()
    encrypted = bytearray(blob)
    struct.pack_into(">I", encrypted, 32, 1)
    with pytest.raises(QcowError, match="encrypted"):
        parse_qcow_header(bytes(encrypted))

    backing = bytearray(blob)
    struct.pack_into(">I", backing, 16, 12)
    with pytest.raises(QcowError, match="backing file"):
        parse_qcow_header(bytes(backing))

    zstd = bytearray(blob)
    struct.pack_into(">Q", zstd, 72, 1 << 3)
    struct.pack_into(">I", zstd, 100, 105)
    zstd[104] = 1
    with pytest.raises(QcowError, match="zstd"):
        parse_qcow_header(bytes(zstd))

    with pytest.raises(QcowError, match="version 1"):
        parse_qcow_header(b"QFI\xfb" + struct.pack(">I", 1) + b"\x00" * 64)


def test_parse_and_stage_does_not_download_a_qcow2():
    fake, _blob, _guest = _store()
    with pytest.raises(OvaPackageError, match="qcow2"):
        parse_and_stage(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "disk.qcow2", "job1")
