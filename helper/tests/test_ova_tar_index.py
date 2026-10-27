"""OVA table-of-contents via range reads, member resolution, and the ranged VMDK copy."""

import io
import logging
import tarfile

import pytest

from helper_app.config import get_settings
from helper_app.disk import object_vmdk_copy, vmdk_stream as vs
from helper_app.ova import tar_index
from helper_app.ova.package import OvaPackageError, parse_ova_layout, read_ovf_from_object
from helper_app.redaction import configure_logging_redaction

from .fake_oci import FakeOci
from .test_ova_ovf import _minimal_ovf, _rich_ovf, make_raw_small


def _ova(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in entries:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _bucket_with(fake: FakeOci, name: str, body: bytes) -> None:
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects.setdefault("OVA", {})[name] = body
    fake.object_storage.add_object("OVA", name, size=len(body))


def _counting(fake: FakeOci):
    """Wrap the fake object storage to count GetObject calls and whether they were ranged."""
    calls: list[str | None] = []
    inner = fake.object_storage.get_object

    def get_object(namespace, bucket, name, **kw):
        calls.append(kw.get("range"))
        return inner(namespace, bucket, name, **kw)

    fake.object_storage.get_object = get_object
    return calls


def test_index_reads_only_headers_by_range():
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    ovf = _minimal_ovf(len(vmdk))
    body = _ova([("vm.ovf", ovf), ("disk1.vmdk", vmdk), ("vm.mf", b"SHA256(vm.ovf)= 00\n")])
    _bucket_with(fake, "vm.ova", body)
    calls = _counting(fake)

    index = tar_index.index_ova(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "vm.ova")
    assert [m.basename for m in index.members] == ["vm.ovf", "disk1.vmdk", "vm.mf"]
    assert index.seekable and index.truncated_at is None
    disk = index.find("DISK1.VMDK")
    assert disk is not None and disk.size == len(vmdk)
    assert body[disk.offset_data : disk.offset_data + disk.size] == vmdk
    assert all(r is not None for r in calls), "index must not download the whole object"
    assert len(calls) <= 4


def test_ovf_after_a_large_disk_is_found_without_draining_it():
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    ovf = _minimal_ovf(len(vmdk))
    _bucket_with(fake, "odd.ova", _ova([("disk1.vmdk", vmdk), ("vm.ovf", ovf)]))
    calls = _counting(fake)
    assert read_ovf_from_object(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "odd.ova") == ovf
    assert all(r is not None for r in calls)


def test_layout_and_ranged_copy_decode_the_member(tmp_path):
    fake = FakeOci(get_settings().device_prefix)
    raw = make_raw_small()
    vmdk = vs.encode_raw_bytes(raw)
    ova = _ova([("vm.ovf", _minimal_ovf(len(raw))), ("disk1.vmdk", vmdk)])
    _bucket_with(fake, "vm.ova", ova)
    ns = fake.object_storage.NAMESPACE

    layout = parse_ova_layout(fake.clients(), ns, "OVA", "vm.ova")
    (src,) = layout.disks
    assert src.ova_object == "vm.ova" and src.ova_member == "disk1.vmdk"
    assert src.ova_member_size == len(vmdk) and src.ova_member_offset is not None

    target = tmp_path / "disk"
    target.touch()
    calls = _counting(fake)
    received, written = object_vmdk_copy.copy_vmdk_from_ova(
        fake.clients(), ns, "OVA", "vm.ova", src.ova_member, str(target), len(raw),
        member_offset=src.ova_member_offset, member_size=src.ova_member_size,
    )
    assert received == len(vmdk)
    assert target.read_bytes() == raw
    assert calls == [f"bytes={src.ova_member_offset}-{src.ova_member_offset + len(vmdk) - 1}"]


def test_lone_disk_with_a_different_name_is_used(caplog):
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    # OVF says disk1.vmdk; the archive's only disk was renamed by whoever repacked it.
    ova = _ova([("OL9-YOANN.ovf", _minimal_ovf(len(vmdk))), ("OL9-YOANN-disk1.vmdk", vmdk)])
    _bucket_with(fake, "OL9Yo.ova", ova)
    with caplog.at_level(logging.WARNING):
        layout = parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "OL9Yo.ova")
    assert layout.disks[0].ova_member == "OL9-YOANN-disk1.vmdk"
    assert "only disk is OL9-YOANN-disk1.vmdk" in caplog.text


def test_missing_disk_in_multi_disk_ova_lists_the_entries():
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    ova = _ova([("vm.ovf", _rich_ovf()), ("disk1.vmdk", vmdk), ("other.vmdk", vmdk)])
    _bucket_with(fake, "two.ova", ova)
    with pytest.raises(OvaPackageError, match=r"does not contain disk2\.vmdk.*Entries in the archive: vm\.ovf.*other\.vmdk"):
        parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "two.ova")


def test_truncated_upload_is_reported_before_provisioning():
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    ova = _ova([("vm.ovf", _minimal_ovf(len(vmdk))), ("disk1.vmdk", vmdk), ("vm.mf", b"x\n")])
    cut = ova[: len(ova) - len(vmdk) // 2]  # upload stopped half-way through the disk
    _bucket_with(fake, "short.ova", cut)
    with pytest.raises(OvaPackageError, match="damaged or incomplete after vm.ovf, disk1.vmdk"):
        parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "short.ova")


def test_garbage_after_the_ovf_is_reported():
    fake = FakeOci(get_settings().device_prefix)
    ovf = _minimal_ovf(1024)
    ova = _ova([("vm.ovf", ovf)])
    end = ova.rstrip(b"\0")  # drop the end-of-archive marker, append a non-tar blob
    ova = end + b"\0" * (-len(end) % 512) + b"not a tar header" * 64
    _bucket_with(fake, "bad.ova", ova)
    with pytest.raises(OvaPackageError, match="not a valid tar header"):
        parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "bad.ova")


def test_not_a_tar_is_rejected():
    fake = FakeOci(get_settings().device_prefix)
    _bucket_with(fake, "zip.ova", b"PK\x03\x04" + b"\0" * 2048)
    with pytest.raises(OvaPackageError, match="not a tar archive"):
        parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "zip.ova")


def test_gzipped_ova_falls_back_to_sequential_read():
    import gzip

    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    ova = gzip.compress(_ova([("vm.ovf", _minimal_ovf(len(vmdk))), ("disk1.vmdk", vmdk)]))
    _bucket_with(fake, "gz.ova", ova)
    layout = parse_ova_layout(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "gz.ova")
    assert layout.disks[0].ova_member == "disk1.vmdk"
    assert layout.disks[0].ova_member_offset is None


def test_sequential_fallback_reports_entries_when_member_missing():
    fake = FakeOci(get_settings().device_prefix)
    vmdk = vs.encode_raw_bytes(make_raw_small())
    _bucket_with(fake, "vm.ova", _ova([("vm.ovf", b"<x/>"), ("disk1.vmdk", vmdk)]))
    gen = object_vmdk_copy._iter_ova_vmdk(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "vm.ova", "nope.vmdk")
    with pytest.raises(vs.VmdkFormatError, match="archive ended after vm.ovf, disk1.vmdk"):
        list(gen)


def test_uvicorn_access_records_keep_their_args():
    configure_logging_redaction()
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:1234", "GET", "/api/jobs", "1.1", 200), None,
    )
    assert len(record.args) == 5
    assert record.getMessage() == '10.0.0.1:1234 - "GET /api/jobs HTTP/1.1" 200'
    other = logging.getLogger("helper_app.x").makeRecord(
        "helper_app.x", logging.INFO, __file__, 1, "password=%s", ("FAKE_SECRET",), None,
    )
    assert other.args == () and "FAKE_SECRET" not in other.getMessage()
