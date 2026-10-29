"""Nutanix AHV: Prism URL parsing, PC/PE detection, VM mapping, power-off and the raw image copy."""

import httpx
import pytest

from helper_app.config import Settings
from helper_app.disk.nutanix_image_copy import NutanixCopyError, copy_ranges, copy_stream, is_zero, split_extents
from helper_app.models import Firmware, NutanixSourceInfo
from helper_app.nutanix.client import NutanixAuthError, NutanixError, normalize_v2_vm, normalize_v3_vm, parse_prism_url
from helper_app.nutanix.export import NutanixDiskExport, image_name, release_nutanix_resources
from helper_app.nutanix.inventory import guest_os_from_vm, inspect_vm, preflight, vm_spec_from_ahv, warnings
from helper_app.nutanix.power import shut_down
from helper_app.nutanix.regions import RegionsUnavailable, merge_extents, snapshot_paths

from .fake_nutanix import CDONLY, CLUSTER, PASSWORD, PRISM_URL, STUCK, USER, VG, WEB, WIN, data_regions, make_fleet

RAWS = {0: b"\x01" * 4096 + b"\x00" * 8192 + b"\x02" * 4096, 1: b"\x00" * 4096 + b"\x03" * 4096}


class MemWriter:
    def __init__(self, size: int):
        self.buf = bytearray(size)
        self.writes: list[tuple[int, int]] = []

    def write_at(self, offset: int, data: bytes) -> None:
        self.buf[offset:offset + len(data)] = data
        self.writes.append((offset, len(data)))

    def close(self) -> None:
        pass

    @property
    def size(self):
        return len(self.buf)


def client_for(fleet):
    return fleet.client_factory(PRISM_URL, USER, PASSWORD, False)


@pytest.mark.parametrize("raw, base, display", [
    ("prism.test", "https://prism.test:9440", "prism.test"),
    ("https://prism.test:9440/console/#page", "https://prism.test:9440", "prism.test"),
    ("https://10.0.0.5:8443", "https://10.0.0.5:8443", "10.0.0.5:8443"),
    ("prism.test/PrismGateway", "https://prism.test:9440", "prism.test"),
])
def test_parse_prism_url(raw, base, display):
    assert parse_prism_url(raw) == (base, display)


@pytest.mark.parametrize("raw", ["", "http://prism.test", "https://user:pw@prism.test", "https://prism.test/some/page"])
def test_parse_prism_url_rejects(raw):
    with pytest.raises(NutanixAuthError):
        parse_prism_url(raw)


@pytest.mark.parametrize("flavor", ["pc", "pe"])
def test_detect_flavor_and_bad_password(flavor):
    fleet = make_fleet(RAWS, flavor=flavor)
    assert client_for(fleet).detect() == flavor
    bad = fleet.client_factory(PRISM_URL, USER, "nope", False)
    with pytest.raises(NutanixAuthError):
        bad.detect()


@pytest.mark.parametrize("flavor", ["pc", "pe"])
def test_inventory_mapping_is_the_same_on_both_apis(flavor):
    client = client_for(make_fleet(RAWS, flavor=flavor))
    client.detect()
    vms = {vm.uuid: vm for vm in client.list_vms()}
    assert set(vms) == {WEB, WIN, VG, CDONLY, STUCK}
    spec, disk_ids = vm_spec_from_ahv(vms[WEB])
    assert disk_ids == ["vd-web-os", "vd-web-data"]
    assert [d.label for d in spec.disks] == ["scsi.0", "scsi.1"]
    assert [d.capacity_bytes for d in spec.disks] == [len(RAWS[0]), len(RAWS[1])]
    assert spec.firmware == Firmware.EFI and not spec.secure_boot and spec.power_state == "poweredOn"
    assert spec.num_cpu == 2 and spec.memory_mb == 2048 and spec.host_name == CLUSTER
    assert spec.guest_id == "rhel9_64Guest"
    assert spec.nics[0].network == "vlan0" and spec.nics[0].ip_addresses == ["10.0.0.15"]
    win, _ = vm_spec_from_ahv(vms[WIN])
    assert win.firmware == Firmware.BIOS and win.disks[0].controller_type == "sata"
    assert win.guest_id == "windows2022srv_64Guest" and win.is_windows

    details = inspect_vm(client, WEB)
    assert preflight(details) == []
    assert details.container_uuids == ["ctr-1", "ctr-1"]
    notes = warnings(details)
    assert any("powered on" in n for n in notes) and any("CD-ROM" in n for n in notes)
    assert any("volume group" in p for p in preflight(inspect_vm(client, VG)))
    assert any("CD-ROM" in p for p in preflight(inspect_vm(client, CDONLY)))
    stuck = inspect_vm(client, STUCK)
    assert preflight(stuck) == []
    assert any("Guest Tools are not installed" in n for n in warnings(stuck))


def test_secure_boot_and_v2_shapes():
    vm = normalize_v2_vm({"uuid": "u", "name": "n", "power_state": "on", "num_vcpus": 2, "num_cores_per_vcpu": 2,
                          "memory_mb": 4096, "boot": {"uefi_boot": True, "secure_boot": True}})
    spec, _ = vm_spec_from_ahv(vm)
    assert spec.firmware == Firmware.EFI and spec.secure_boot and spec.num_cpu == 4
    v3 = normalize_v3_vm({"metadata": {"uuid": "u"}, "status": {"name": "n", "resources": {
        "power_state": "ON", "boot_config": {"boot_type": "SECURE_BOOT"}, "vtpm_config": {"vtpm_enabled": True}}}})
    spec, _ = vm_spec_from_ahv(v3)
    assert spec.secure_boot and spec.has_vtpm


@pytest.mark.parametrize("text, guest_id", [
    ("linux:64:CentOS Linux-7.9.2009", "centos7_64Guest"),
    ("linux:64:Ubuntu-22.04", "ubuntu64Guest"),
    ("linux:64:Oracle Linux Server-8.8", "oracleLinux8_64Guest"),
    ("windows:64:Windows Server 2019", "windows2019srv_64Guest"),
    ("", "otherLinux64Guest"),
])
def test_guest_os_from_ngt(text, guest_id):
    vm = normalize_v3_vm({"metadata": {"uuid": "u"}, "status": {"name": "n", "resources": {
        "guest_tools": {"nutanix_guest_tools": {"guest_os_version": text}}}}})
    assert guest_os_from_vm(vm)[0] == guest_id


@pytest.mark.parametrize("flavor", ["pc", "pe"])
def test_shut_down_acpi_then_hard(flavor):
    fleet = make_fleet(RAWS, flavor=flavor)
    client = client_for(fleet)
    client.detect()
    assert shut_down(client, WIN, 1, sleep=lambda _s: None) == "already_off"
    assert shut_down(client, WEB, 1, sleep=lambda _s: None) == "guest_shutdown"
    assert fleet.vms[WEB].power == "OFF"
    assert shut_down(client, STUCK, 0, sleep=lambda _s: None) == "powered_off"
    expected = ["OFF:ACPI", "OFF:HARD"] if flavor == "pc" else ["ACPI_SHUTDOWN", "OFF"]
    assert fleet.vms[STUCK].ops == expected


@pytest.mark.parametrize("flavor", ["pc", "pe"])
def test_disk_export_clones_and_deletes_images(flavor):
    fleet = make_fleet(RAWS, flavor=flavor, ranges=flavor == "pc")
    client = client_for(fleet)
    client.detect()
    info = NutanixSourceInfo(prism_host="prism.test", api=flavor, disk_ids=["vd-web-os", "vd-web-data"],
                             container_uuids=["ctr-1", "ctr-1"])
    saves = []
    with NutanixDiskExport(client, "job12345678", info, image_timeout_s=5, save=lambda: saves.append(1)) as export:
        image = export.open(0)
        assert image.size == len(RAWS[0]) and image.ranges is (flavor == "pc")
        assert image.url.endswith(f"/images/{image.uuid}/file")
        assert info.image_uuids == [image.uuid]
        assert fleet.images[image.uuid]["name"] == image_name("job12345678", 0)
        export.finish(image)
        assert info.image_uuids == [] and image.uuid in fleet.images_deleted
        left = export.open(1)
        assert info.image_uuids == [left.uuid]
    assert info.image_uuids == [] and fleet.images == {}
    assert saves


def test_disk_export_replaces_a_stale_image():
    fleet = make_fleet(RAWS)
    client = client_for(fleet)
    client.detect()
    info = NutanixSourceInfo(prism_host="prism.test", disk_ids=["vd-web-os"])
    with NutanixDiskExport(client, "jobabcdef00", info, image_timeout_s=5, save=lambda: None) as export:
        first = export.open(0)
        info.image_uuids.clear()  # as if the helper restarted and forgot it
        second = export.open(0)
        assert first.uuid in fleet.images_deleted and second.uuid in fleet.images
    assert fleet.images == {}


@pytest.mark.parametrize("flavor", ["pc", "pe"])
def test_region_map_matches_the_disk_data(flavor):
    fleet = make_fleet(RAWS, flavor=flavor)
    client = client_for(fleet)
    client.detect()
    info = NutanixSourceInfo(prism_host="prism.test", api=flavor, disk_ids=["vd-web-os", "vd-web-data"],
                             container_uuids=["ctr-1", "ctr-1"])
    with NutanixDiskExport(client, "job12345678", info, image_timeout_s=5, save=lambda: None) as export:
        regions = export.map_regions(WEB)
    for index, disk_id in enumerate(info.disk_ids):
        expected = [(r["offset"], r["length"]) for r in data_regions(RAWS[index]) if r["type"] == "REGULAR"]
        assert merge_extents(regions[disk_id], len(RAWS[index]), 0) == expected
        rebuilt = bytearray(len(RAWS[index]))
        for off, length in regions[disk_id]:
            rebuilt[off:off + length] = RAWS[index][off:off + length]
        assert bytes(rebuilt) == RAWS[index]  # nothing outside the regions holds data
    assert info.recovery_point_ids == [] and info.protection_domains == []
    assert fleet.recovery_points == {} and fleet.protection_domains == {}


def test_region_map_unavailable_leaves_nothing_behind():
    fleet = make_fleet(RAWS, regions=False)
    client = client_for(fleet)
    client.detect()
    info = NutanixSourceInfo(prism_host="prism.test", disk_ids=["vd-web-os"])
    with pytest.raises(RegionsUnavailable, match="pc.2024.3"):
        NutanixDiskExport(client, "job12345678", info, image_timeout_s=5, save=lambda: None).map_regions(WEB)
    assert info.recovery_point_ids == []


def test_release_removes_left_over_recovery_points_and_protection_domains():
    pc = make_fleet(RAWS)
    client = client_for(pc)
    client.detect()
    rp_body = {"vmRecoveryPoints": [{"vmExtId": WEB}], "expirationTime": "2026-10-30T00:00:00Z"}
    client.request("POST", "/api/dataprotection/v4.0/config/recovery-points", json=rp_body,
                   headers={"NTNX-Request-Id": "r1"})
    rp_id = next(iter(pc.recovery_points))
    info = NutanixSourceInfo(prism_host="prism.test", recovery_point_ids=[rp_id, "gone"])
    actions = release_nutanix_resources(client, info)
    assert f"ok: deleted recovery point {rp_id}" in actions and pc.recovery_points == {}
    assert info.recovery_point_ids == []

    pe = make_fleet(RAWS, flavor="pe")
    client = client_for(pe)
    client.detect()
    client.request("POST", "/PrismGateway/services/rest/v2.0/protection_domains/", json={"value": "oci-umt-x"})
    client.request("POST", "/PrismGateway/services/rest/v2.0/protection_domains/oci-umt-x/protect_vms",
                   json={"uuids": [WIN]})
    client.request("POST", "/PrismGateway/services/rest/v2.0/protection_domains/oci-umt-x/oob_schedules", json={})
    info = NutanixSourceInfo(prism_host="prism.test", api="pe", protection_domains=["oci-umt-x"])
    actions = release_nutanix_resources(client, info)
    assert "ok: deleted protection domain oci-umt-x" in actions and not [a for a in actions if "failed" in a]
    assert pe.protection_domains == {} and info.protection_domains == []


def test_merge_and_split_extents():
    assert merge_extents([(100, 50), (0, 10), (10, 5), (5000, 10)], 1000, 0) == [(0, 15), (100, 50)]
    assert merge_extents([(0, 10), (30, 10)], 0, 20) == [(0, 40)]
    assert split_extents([(0, 10), (100, 25)], 10) == [(0, 10), (100, 10), (110, 10), (120, 5)]


def test_snapshot_paths_prefer_listed_paths():
    listed = "/ctr/.snapshot/05/1-2-70839205/.acropolis/vmdisk/vd1"
    assert snapshot_paths({"files": [listed]}, "vd1", "ctr", "1", "2", "70839205")[0] == listed
    assert snapshot_paths({}, "vd1", "ctr", "1", "2", "70839205") == [
        "/ctr/.snapshot/05/1-2-70839205/.acropolis/vmdisk/vd1", "/ctr/.snapshot/5/1-2-70839205/.acropolis/vmdisk/vd1"]
    with pytest.raises(RegionsUnavailable):
        snapshot_paths({}, "vd1", "", "1", "2", "70839205")


def test_is_zero():
    assert is_zero(b"") and is_zero(b"\x00" * 10) and not is_zero(b"\x00\x01")


def test_copy_ranges_skips_zero_chunks_and_retries():
    data = RAWS[0]
    calls = {"n": 0}

    def fetch(off, length):
        calls["n"] += 1
        if calls["n"] == 2:
            raise NutanixError("transient")
        return data[off:off + length]

    writer = MemWriter(len(data))
    stats = copy_ranges(fetch, len(data), writer, chunk_bytes=4096, workers=1, retries=3, sleep=lambda _s: None)
    assert bytes(writer.buf) == data
    assert stats.bytes_received == len(data) and stats.bytes_written == 8192 and stats.retries == 1
    assert sorted(writer.writes) == [(0, 4096), (12288, 4096)]


def test_copy_ranges_gives_up():
    def fetch(off, length):
        raise httpx.ReadError("boom")

    with pytest.raises(NutanixCopyError):
        copy_ranges(fetch, 8192, MemWriter(8192), chunk_bytes=4096, workers=2, retries=2, sleep=lambda _s: None)


def test_copy_stream_rechunks_and_resumes():
    data = RAWS[0]
    opened = []

    def open_stream(offset):
        opened.append(offset)
        rest = data[offset:]
        for i in range(0, len(rest), 1000):
            if len(opened) == 1 and offset + i >= 9000:
                raise NutanixError("connection reset")
            yield rest[i:i + 1000]

    writer = MemWriter(len(data))
    stats = copy_stream(open_stream, len(data), writer, chunk_bytes=4096, resumable=True, sleep=lambda _s: None)
    assert bytes(writer.buf) == data and opened == [0, 8192] and stats.retries == 1
    assert stats.bytes_received == len(data) and stats.bytes_written == 8192


def test_copy_stream_without_resume_and_short_stream():
    def broken(offset):
        yield b"\x01" * 100
        raise NutanixError("reset")

    with pytest.raises(NutanixCopyError, match="failed at byte"):
        copy_stream(broken, 4096, MemWriter(4096), chunk_bytes=4096, resumable=False, sleep=lambda _s: None)

    def short(offset):
        yield b"\x01" * 100

    with pytest.raises(NutanixCopyError, match="ended at byte"):
        copy_stream(short, 4096, MemWriter(4096), chunk_bytes=4096, sleep=lambda _s: None)


def test_settings_defaults():
    s = Settings()
    assert s.nutanix_range_workers >= 1 and s.nutanix_chunk_bytes > 0 and s.nutanix_image_timeout_s > 0
