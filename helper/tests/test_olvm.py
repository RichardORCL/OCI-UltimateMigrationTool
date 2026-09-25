"""OLVM inventory mapping, preflight and image-extent copy, without the web API."""

from helper_app.disk.imageio_range_copy import allocated_ranges, copy_extents
from helper_app.models import Firmware
from helper_app.oci.mapping import map_guest_os
from helper_app.olvm.export import OlvmDiskExport
from helper_app.olvm.inventory import (
    OlvmVmDetails,
    firmware_of,
    guess_guest_os,
    preflight,
    vm_spec_from_olvm,
)

from .fake_olvm import HOSTED, LOCKED, LUN, MOVING, WEB, FakeOlvm, extents_of, make_fleet
from .test_vmdk_stream import make_raw


class _Mem:
    def __init__(self, size: int):
        self.buf = bytearray(size)

    def write_at(self, offset: int, data: bytes) -> None:
        self.buf[offset:offset + len(data)] = data

    def close(self) -> None:
        return None

    @property
    def size(self) -> int:
        return len(self.buf)


def _fleet() -> FakeOlvm:
    raws = {0: make_raw(64 * 1024, seed=3), 1: make_raw(32 * 1024, seed=4)}
    return make_fleet(raws)


def test_guest_os_and_firmware_mapping():
    guest_id, full = guess_guest_os("rhel_9x64")
    meta = map_guest_os(guest_id, full)
    assert (meta.operating_system, meta.operating_system_version, meta.version_detected) == (
        "Red Hat Enterprise Linux", "9", True)
    guest_id, full = guess_guest_os("ol_8x64")
    meta = map_guest_os(guest_id, full)
    assert (meta.operating_system, meta.operating_system_version) == ("Oracle Linux", "8")
    guest_id, full = guess_guest_os("windows_2022")
    meta = map_guest_os(guest_id, full)
    assert meta.operating_system == "Windows" and "2022" in meta.operating_system_version
    guest_id, full = guess_guest_os("ubuntu_24_04")
    meta = map_guest_os(guest_id, full)
    assert meta.operating_system == "Ubuntu" and meta.operating_system_version == "24.04" and meta.version_detected
    guest_id, full = guess_guest_os("other_linux")
    meta = map_guest_os(guest_id, full)
    assert meta.operating_system == "Custom Linux" and meta.version_detected is False

    bios, secure = firmware_of({"bios": {"type": "q35_secure_boot"}})
    assert bios is Firmware.EFI and secure is True
    bios, secure = firmware_of({"bios": {"type": "i440fx_sea_bios"}})
    assert bios is Firmware.BIOS and secure is False


def test_preflight_refusals():
    fleet = _fleet()
    def details(vm_id):
        vm = fleet.vms[vm_id].doc()
        spec, disk_ids = vm_spec_from_olvm(vm)
        return OlvmVmDetails(spec=spec, cluster=spec.host_name, disk_ids=disk_ids, vm=vm)

    web = details(WEB)
    assert preflight(web) == []
    assert web.spec.firmware is Firmware.EFI
    assert web.spec.disks[0].label == "web_Disk1"
    assert web.disk_ids == ["disk-web-os", "disk-web-data"]
    assert any("hosted engine" in p for p in preflight(details(HOSTED)))
    assert any("lun disk" in p for p in preflight(details(LUN)))
    assert any("locked" in p for p in preflight(details(LOCKED)))
    assert any("migrating" in p for p in preflight(details(MOVING)))


def test_extent_copy_skips_zeros():
    raw = b"\x01" * 100 + b"\x00" * 200 + b"\x02" * 50
    extents = extents_of(raw)
    ranges = allocated_ranges(extents, len(raw))
    assert ranges and sum(length for _, length in ranges) < len(raw)

    def fetch(off, length):
        return raw[off:off + length]

    writer = _Mem(len(raw))
    stats = copy_extents(fetch, ranges, writer, chunk_bytes=4096, workers=2)
    assert bytes(writer.buf) == raw
    assert stats.bytes_written == sum(length for _, length in ranges)


def test_export_exit_cancels_open_transfer():
    class Stub:
        def __init__(self):
            self.phases = []

        def set_transfer_phase(self, transfer_id, phase):
            self.phases.append((transfer_id, phase))

    stub = Stub()
    export = OlvmDiskExport(stub, inactivity_timeout_s=10, ready_timeout_s=1)
    export.open_ids.append("transfer-1")
    export.__exit__(RuntimeError, RuntimeError("boom"), None)
    assert stub.phases == [("transfer-1", "cancelled")]
    assert export.open_ids == []
