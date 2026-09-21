"""Azure source: client, inventory mapping, preflight, export access and the VHD page range copy, all against
``fake_azure`` (no network)."""

from __future__ import annotations

import os
import random
import threading

import pytest

import httpx

from helper_app.azure.client import AzureAuthError, AzureClient, AzureError, _append_sas_query
from helper_app.azure.export import AzureDiskExport, release_azure_resources, snapshot_name
from helper_app.azure.inventory import (
    guess_guest_os,
    inspect_vm,
    list_vm_summaries,
    parse_resource_id,
    power_state,
    vm_spec_from_azure,
)
from helper_app.azure.preflight import preflight, warnings
from helper_app.azure.session import AzureConnector
from helper_app.config import Settings
from helper_app.disk.vhd_range_copy import (
    VhdCopyError,
    blob_length,
    copy_ranges,
    list_page_ranges,
    merge_ranges,
    split_chunks,
)
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.models import AzureSourceInfo, Firmware
from helper_app.oci.mapping import map_guest_os

from .fake_azure import CLIENT_ID, SECRET, SUB, TENANT, FakeAzure, FakeDisk, FakeVm, make_fleet

MIB = 1024**2


def sparse_raw(size: int, seed: int = 1) -> bytes:
    rnd = random.Random(seed)
    raw = bytearray(size)
    for off in range(0, size, 64 * 1024):
        mode = (off // (64 * 1024)) % 3
        if mode == 0:
            raw[off:off + 64 * 1024] = rnd.randbytes(min(64 * 1024, size - off))
        elif mode == 1:
            raw[off + 700:off + 3000] = rnd.randbytes(2300)
    return bytes(raw)


@pytest.fixture
def fleet():
    raws = {0: sparse_raw(2 * MIB, 3), 1: sparse_raw(MIB, 4)}
    fake = make_fleet(raws)
    fake.raws = raws
    return fake


def login(fake: FakeAzure, settings=None):
    return fake.connector(settings or Settings()).login(TENANT, CLIENT_ID, SECRET)


# --------------------------------------------------------------------------- client + login
def test_login_and_token_errors(fleet):
    session = login(fleet)
    assert session.tenant_id == TENANT and session.client_id == CLIENT_ID
    assert [s.id for s in session.subscriptions] == [SUB] and session.subscriptions[0].name == "Prod Subscription"
    assert session.username == f"azure:{CLIENT_ID}"
    assert fleet.tokens_issued == 1
    # the token is cached; the list of subscriptions did not ask for a new one
    session.client.list_subscriptions()
    assert fleet.tokens_issued == 1
    session.close()

    conn = fleet.connector(Settings())
    with pytest.raises(AzureAuthError, match="invalid client secret"):
        conn.login(TENANT, CLIENT_ID, "wrong")
    with pytest.raises(AzureAuthError, match="tenant not found"):
        conn.login("22222222-2222-3333-4444-555555555555", CLIENT_ID, SECRET)
    with pytest.raises(AzureAuthError, match="not found in this tenant"):
        conn.login(TENANT, "aaaaaaaa-bbbb-cccc-dddd-000000000000", SECRET)
    with pytest.raises(AzureAuthError, match="required"):
        conn.login(TENANT, CLIENT_ID, "")
    with pytest.raises(AzureAuthError, match="invalid client ID"):
        conn.login(TENANT, "not-a-guid", SECRET)
    with pytest.raises(AzureAuthError, match="invalid tenant"):
        conn.login("bad tenant!", CLIENT_ID, SECRET)
    # no subscription visible: a clear RBAC hint
    empty = FakeAzure([], subscriptions=[])
    with pytest.raises(AzureAuthError, match="Reader role"):
        empty.connector(Settings()).login(TENANT, CLIENT_ID, SECRET)


def test_client_errors_and_lro(fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    with pytest.raises(AzureError) as ei:
        client.get_disk("/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/disks/nope")
    assert ei.value.status == 404 and ei.value.code == "ResourceNotFound"
    vm = next(v for v in fleet.vms.values() if v.name == "lin-01")
    assert vm.power == "running"
    client.deallocate_vm(vm.id, timeout_s=60)  # Azure-AsyncOperation polling until Succeeded
    assert vm.power == "deallocated" and vm.ops == ["deallocate"]
    # a Location-style operation with a result body
    sas = client.begin_get_access(vm.os_disk.id, 3600)
    assert sas.startswith("https://md-fake") and "sig=" in sas
    assert fleet.sas_granted == [vm.os_disk.id]
    client.end_get_access(vm.os_disk.id)
    assert fleet.sas_revoked == [vm.os_disk.id]
    # timeouts are reported against the operation
    fleet.deallocate_polls = 50
    clock = [0.0]
    slow = AzureClient(TENANT, CLIENT_ID, SECRET, http=fleet.http(), sleep=lambda s: clock.__setitem__(0, clock[0] + 10),
                       clock=lambda: clock[0])
    vm.power = "running"
    with pytest.raises(AzureError, match="did not finish within 30 s"):
        slow.deallocate_vm(vm.id, timeout_s=30)


# --------------------------------------------------------------------------- inventory
def test_vm_list_and_spec_mapping(fleet):
    session = login(fleet)
    rows = list_vm_summaries(session)
    assert [r.name for r in rows] == ["ol-01", "conf-01", "enc-01", "lin-01", "win-01"]  # by subscription/RG, name
    lin = next(r for r in rows if r.name == "lin-01")
    assert lin.folder == "Prod Subscription/rg-prod" and lin.power_state == "poweredOn"
    assert lin.num_disks == 2 and lin.disk_capacity_bytes == 2 * 1024**3  # two 1 GB disks (rounded up)
    assert lin.vm_size == "Standard_D2s_v3" and lin.location == "westeurope"
    assert lin.guest_full_name.startswith("Ubuntu 22.04")
    assert next(r for r in rows if r.name == "enc-01").encrypted is True
    assert next(r for r in rows if r.name == "ol-01").power_state == "stopped"
    assert next(r for r in rows if r.name == "win-01").power_state == "poweredOff"

    details = inspect_vm(session, lin.moid)
    spec = details.spec
    assert spec.moid == lin.moid and spec.name == "lin-01"
    assert spec.num_cpu == 2 and spec.memory_mb == 8192  # from the VM size catalog
    assert spec.firmware == Firmware.EFI and spec.secure_boot is False and spec.has_vtpm is False
    assert spec.power_state == "poweredOn"
    assert [d.label for d in spec.disks] == ["lin-01_OsDisk", "lin-01-data0"]
    assert spec.disks[0].capacity_bytes == 2 * MIB and spec.disks[1].capacity_bytes == MIB
    assert spec.disks[0].backing_file.endswith("/disks/lin-01_OsDisk") and spec.disks[0].controller_type == "scsi"
    assert spec.guest_id == "ubuntu64Guest" and spec.guest_full_name == "Ubuntu 22.04 LTS"
    assert spec.instance_uuid == "vmid-lin-01" and len(spec.nics) == 1
    os_meta = map_guest_os(spec.guest_id, spec.guest_full_name)
    assert (os_meta.operating_system, os_meta.operating_system_version, os_meta.version_detected) == ("Ubuntu", "22.04", True)
    assert details.subscription_id == SUB and details.resource_group == "rg-prod" and details.location == "westeurope"
    assert details.disk_ids == [spec.disks[0].backing_file, spec.disks[1].backing_file]

    win = inspect_vm(session, next(r for r in rows if r.name == "win-01").moid).spec
    assert win.firmware == Firmware.BIOS and win.is_windows and win.num_cpu == 4
    wm = map_guest_os(win.guest_id, win.guest_full_name)
    assert (wm.operating_system, wm.operating_system_version) == ("Windows", "Server 2022 Standard")
    ol = inspect_vm(session, next(r for r in rows if r.name == "ol-01").moid).spec
    om = map_guest_os(ol.guest_id, ol.guest_full_name)
    assert (om.operating_system, om.operating_system_version) == ("Oracle Linux", "9")
    assert ol.power_state == "stopped"

    # size cache: one vmSizes call per (subscription, location)
    cache = {}
    n = len([r for r in fleet.requests if r.endswith("/vmSizes")])
    inspect_vm(session, lin.moid, cache)
    inspect_vm(session, win.moid, cache)
    assert len([r for r in fleet.requests if r.endswith("/vmSizes")]) == n + 1

    with pytest.raises(AzureError, match="not an Azure resource ID"):
        parse_resource_id("vm-101")


def test_guest_os_guess_from_image_reference():
    def vm(publisher="", offer="", sku="", os_type="Linux", os_name="", os_version=""):
        return {"properties": {"storageProfile": {"imageReference": {"publisher": publisher, "offer": offer, "sku": sku},
                                                  "osDisk": {"osType": os_type}},
                               "instanceView": {"osName": os_name, "osVersion": os_version}}}

    cases = [
        (vm("Canonical", "0001-com-ubuntu-server-noble", "24_04-lts-gen2"), ("Ubuntu", "24.04", True)),
        (vm("Canonical", "UbuntuServer", "18.04-LTS"), ("Ubuntu", "18.04", True)),
        (vm("RedHat", "RHEL", "9_4"), ("Red Hat Enterprise Linux", "9", True)),
        (vm("RedHat", "RHEL", "8-lvm-gen2"), ("Red Hat Enterprise Linux", "8", True)),
        (vm("Oracle", "Oracle-Linux", "ol89-lvm-gen2"), ("Oracle Linux", "8", True)),
        (vm("SUSE", "sles-15-sp5", "gen2"), ("SUSE Linux Enterprise Server", "15", True)),
        (vm("Debian", "debian-12", "12-gen2"), ("Debian", "12", True)),
        (vm("resf", "rockylinux-x86_64", "9-base"), ("Rocky Linux", "9", True)),
        (vm("almalinux", "almalinux-x86_64", "9-gen2"), ("AlmaLinux", "9", True)),
        (vm("MicrosoftWindowsServer", "WindowsServer", "2019-Datacenter", "Windows"), ("Windows", "Server 2019 Standard", True)),
        (vm("MicrosoftWindowsServer", "WindowsServer", "2012-R2-Datacenter", "Windows"), ("Windows", "Server 2012 R2 Standard", True)),
        (vm("MicrosoftWindowsDesktop", "Windows-11", "win11-23h2-pro", "Windows"), ("Windows", "Windows11", True)),
        (vm("MicrosoftWindowsDesktop", "Windows-10", "win10-22h2-pro", "Windows"), ("Windows", "Windows10", True)),
        (vm(os_type="Windows"), ("Windows", "Server 2019 Standard", False)),  # custom image, nothing known
        (vm(os_name="ubuntu", os_version="20.04"), ("Ubuntu", "20.04", True)),  # custom image, agent reports the OS
        (vm(), ("Custom Linux", "unknown", False)),
    ]
    for doc, expected in cases:
        gid, full = guess_guest_os(doc)
        m = map_guest_os(gid, full)
        assert (m.operating_system, m.operating_system_version, m.version_detected) == expected, (doc, gid, full)


def test_vm_spec_disk_order_and_flags():
    disks = {}
    vm = {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/VM1", "name": "VM1",
          "location": "westeurope",
          "properties": {"hardwareProfile": {"vmSize": "Standard_B1ms"},
                         "securityProfile": {"securityType": "TrustedLaunch",
                                             "uefiSettings": {"secureBootEnabled": True, "vTpmEnabled": True}},
                         "storageProfile": {"osDisk": {"name": "os", "osType": "Linux", "diskSizeGB": 30,
                                                       "managedDisk": {"id": "/x/disks/OS"}},
                                            "dataDisks": [{"name": "d-lun2", "lun": 2, "diskSizeGB": 10, "managedDisk": {"id": "/x/disks/D2"}},
                                                          {"name": "d-lun0", "lun": 0, "diskSizeGB": 20, "managedDisk": {"id": "/x/disks/D0"}}],
                                            "diskControllerType": "NVMe"},
                         "instanceView": {"hyperVGeneration": "V2", "statuses": [{"code": "PowerState/deallocated"}]}}}
    spec = vm_spec_from_azure(vm, disks, {"Standard_B1ms": {"numberOfCores": 1, "memoryInMB": 2048}})
    assert [d.label for d in spec.disks] == ["os", "d-lun0", "d-lun2"]  # OS first, then by LUN
    assert [d.index for d in spec.disks] == [0, 1, 2] and [d.unit_number for d in spec.disks] == [-1, 0, 2]
    assert [d.capacity_bytes // 1024**3 for d in spec.disks] == [30, 20, 10]  # no disk documents: sizes from the VM
    assert spec.disks[0].controller_type == "nvme"
    assert spec.moid.endswith("/virtualmachines/vm1")  # lower-cased
    assert spec.firmware == Firmware.EFI and spec.secure_boot and spec.has_vtpm
    assert spec.power_state == "poweredOff" and spec.num_cpu == 1 and spec.memory_mb == 2048
    assert power_state({"properties": {}}) == "unknown"


# --------------------------------------------------------------------------- preflight
def test_preflight_refusals_and_warnings(fleet):
    session = login(fleet)
    by_name = {vm.name: vm for vm in fleet.vms.values()}
    lin = inspect_vm(session, by_name["lin-01"].id)
    assert preflight(lin) == []
    w = warnings(lin)
    assert any("waagent" in x for x in w) and any("2 vCPUs" not in x for x in w)
    assert any("Snapshot mode" in x for x in warnings(lin, "snapshot"))

    enc = inspect_vm(session, by_name["enc-01"].id)
    assert any("Azure Disk Encryption" in p for p in preflight(enc))
    conf = inspect_vm(session, by_name["conf-01"].id)
    assert any("Confidential VMs" in p for p in preflight(conf))
    assert any("Secure Boot" in x for x in warnings(conf))
    ol = inspect_vm(session, by_name["ol-01"].id)
    assert preflight(ol) == [] and any("deallocates it before the copy" in x for x in warnings(ol))

    # DenyAll disks, ephemeral OS disks, unmanaged disks and transitional power states are refused
    deny = fleet.add_vm(FakeVm("deny-01", FakeDisk("deny-os", b"\1" * 4096, os_type="Linux", network_policy="DenyAll"),
                               power="deallocated"))
    assert any("DenyAll" in p for p in preflight(inspect_vm(session, deny.id)))
    eph = fleet.add_vm(FakeVm("eph-01", FakeDisk("eph-os", b"\1" * 4096, os_type="Linux"), power="deallocated",
                              ephemeral=True))
    assert any("ephemeral" in p for p in preflight(inspect_vm(session, eph.id)))
    unm = fleet.add_vm(FakeVm("unm-01", FakeDisk("unm-os", b"\1" * 4096, os_type="Linux"), power="deallocated",
                              unmanaged=True))
    assert any("unmanaged" in p for p in preflight(inspect_vm(session, unm.id)))
    busy = fleet.add_vm(FakeVm("busy-01", FakeDisk("busy-os", b"\1" * 4096, os_type="Linux"), power="stopping"))
    assert any("stopping" in p for p in preflight(inspect_vm(session, busy.id)))
    priv = fleet.add_vm(FakeVm("priv-01", FakeDisk("priv-os", b"\1" * 4096, os_type="Linux",
                                                   network_policy="AllowPrivate"), power="deallocated"))
    d = inspect_vm(session, priv.id)
    assert preflight(d) == [] and any("private endpoint" in x for x in warnings(d))
    # a disk with an export SAS still granted (another job / a manual export) blocks deallocate mode
    by_name["win-01"].os_disk.sas_token = "stale"
    assert any("ActiveSAS" in p for p in preflight(inspect_vm(session, by_name["win-01"].id)))
    assert preflight(inspect_vm(session, by_name["win-01"].id), "snapshot") == []


# --------------------------------------------------------------------------- page ranges
def test_merge_and_split_ranges():
    assert merge_ranges([(4096, 512), (0, 4096), (8192, 512)], limit=100000) == [(0, 4608), (8192, 512)]
    # clipped to the disk size: the VHD footer page (and anything past the end) is dropped
    assert merge_ranges([(0, 512), (2048, 512)], limit=2048) == [(0, 512)]
    assert merge_ranges([(1536, 1024)], limit=2048) == [(1536, 512)]
    assert split_chunks([(0, 2500)], 1000) == [(0, 1000), (1000, 1000), (2000, 500)]


def test_list_page_ranges_paginated_and_footer_excluded(fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    disk = next(d for d in fleet.disks.values() if d.name == "lin-01_OsDisk")
    owner = fleet.vms[disk.managed_by.lower()]
    owner.power = "deallocated"
    sas = client.begin_get_access(disk.id, 3600)
    length = blob_length(client, sas)
    assert length == len(disk.data) + 512
    ranges = list_page_ranges(client, sas, length - 512)
    assert ranges and all(off + ln <= len(disk.data) for off, ln in ranges)
    assert sum(ln for _, ln in ranges) < len(disk.data)  # sparse
    # two pages of results were fetched (marker)
    assert len([r for r in fleet.requests if r.startswith("GET /sas")]) >= 2
    # every non-zero byte of the disk lies inside an allocated range
    covered = bytearray(len(disk.data))
    for off, ln in ranges:
        covered[off:off + ln] = b"\1" * ln
    for i, b in enumerate(disk.data):
        if b and not covered[i]:
            raise AssertionError(f"byte {i} not covered")


def test_copy_ranges_sparse_retry_and_cancel(tmp_path, fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    disk = next(d for d in fleet.disks.values() if d.name == "lin-01_OsDisk")
    fleet.vms[disk.managed_by.lower()].power = "deallocated"
    sas = client.begin_get_access(disk.id, 3600)
    ranges = list_page_ranges(client, sas, len(disk.data))
    chunks = split_chunks(ranges, 64 * 1024)
    # one chunk fails with 503 once, another with a connection error once: both are retried
    fleet.fail_ranges = {chunks[1][0]: 503, chunks[3][0]: 0}
    dev = tmp_path / "vol"
    dev.write_bytes(bytes(len(disk.data)))
    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    progress = []
    try:
        stats = copy_ranges(client, lambda: sas, ranges, w, chunk_bytes=64 * 1024, workers=3,
                            on_progress=progress.append, sleep=lambda s: None)
    finally:
        w.close()
    assert dev.read_bytes() == disk.data
    assert stats.bytes_received == stats.bytes_written == sum(ln for _, ln in ranges) == sum(progress)
    assert stats.chunks_written == len(chunks) and stats.retries == 2
    assert not [t for t in threading.enumerate() if t.name.startswith("vhd-copy")]

    # permanent failure: the chunk gives up after 3 tries and the copy fails
    fleet.fail_ranges = {chunks[0][0]: 503}
    for _ in range(3):
        fleet.fail_ranges[chunks[0][0]] = 503
    calls = {"n": 0}
    orig = client.blob_get

    def flaky(url, params=None, headers=None):
        if headers and headers.get("x-ms-range", "").startswith(f"bytes={chunks[0][0]}-"):
            calls["n"] += 1
            import httpx

            return httpx.Response(500, text="boom")
        return orig(url, params=params, headers=headers)

    client.blob_get = flaky
    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    with pytest.raises(VhdCopyError, match="HTTP 500"):
        copy_ranges(client, lambda: sas, ranges, w, chunk_bytes=64 * 1024, workers=2, retries=3, sleep=lambda s: None)
    w.close()
    assert calls["n"] == 3
    client.blob_get = orig

    # SAS expired mid-copy: 403 triggers refresh() and the retry uses the new URL
    current = {"sas": sas + "&expired=1"}
    refreshed = []

    def refresh():
        current["sas"] = sas
        refreshed.append(1)
        return sas

    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    stats = copy_ranges(client, lambda: current["sas"], ranges[:1], w, chunk_bytes=64 * 1024, workers=1,
                        refresh=refresh, sleep=lambda s: None)
    w.close()
    assert refreshed == [1] and stats.bytes_written == ranges[0][1]

    # cancellation raised from check_cancel propagates unchanged
    class Stop(RuntimeError):
        pass

    def cancel():
        raise Stop()

    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    with pytest.raises(Stop):
        copy_ranges(client, lambda: sas, ranges, w, chunk_bytes=64 * 1024, workers=2, check_cancel=cancel)
    w.close()
    # an empty range list is a no-op
    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    assert copy_ranges(client, lambda: sas, [], w).bytes_written == 0
    w.close()


@pytest.mark.skipif(not hasattr(os, "pwrite"), reason="pwrite path")
def test_copy_ranges_parallel_writes_are_positional(tmp_path, fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    disk = next(d for d in fleet.disks.values() if d.name == "lin-01-data0")
    fleet.vms[disk.managed_by.lower()].power = "deallocated"
    sas = client.begin_get_access(disk.id, 3600)
    ranges = list_page_ranges(client, sas, len(disk.data))
    dev = tmp_path / "vol"
    dev.write_bytes(bytes(len(disk.data)))
    w = BlockDeviceWriter(str(dev), expected_min_size=len(disk.data))
    copy_ranges(client, lambda: sas, ranges, w, chunk_bytes=4096, workers=8)
    w.close()
    assert dev.read_bytes() == disk.data


# --------------------------------------------------------------------------- export access
def _info(vm: FakeVm, mode="deallocate") -> AzureSourceInfo:
    return AzureSourceInfo(tenant_id=TENANT, subscription_id=SUB, resource_group=vm.rg, location="westeurope",
                           capture_mode=mode, disk_ids=[d.id for d in vm.disks])


def test_disk_export_deallocate_mode_grants_and_revokes(fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    vm = next(v for v in fleet.vms.values() if v.name == "lin-01")
    vm.power = "deallocated"
    info = _info(vm)
    saves = []
    with AzureDiskExport(client, "job1", info, sas_duration_s=3600, snapshot_timeout_s=10,
                         save=lambda: saves.append(1)) as ex:
        assert ex.sas_url(0) != ex.sas_url(1)
        assert set(info.sas_granted) == {d.id for d in vm.disks} and info.sas_expires_at is not None
        assert fleet.sas_granted == [d.id for d in vm.disks]
        assert not ex.expires_soon
        # refresh: a new SAS for one disk, the old one revoked
        old = ex.sas_url(0)
        assert ex.refresh(0) != old and fleet.sas_revoked == [vm.disks[0].id]
    assert info.sas_granted == [] and info.sas_expires_at is None and info.snapshot_ids == []
    assert sorted(fleet.sas_revoked) == sorted([vm.disks[0].id, vm.disks[0].id, vm.disks[1].id])
    assert all(d.sas_token is None for d in vm.disks)
    assert saves  # progress persisted along the way


def test_disk_export_snapshot_mode_and_cleanup_on_failure(fleet):
    client = fleet.client_factory(TENANT, CLIENT_ID, SECRET)
    vm = next(v for v in fleet.vms.values() if v.name == "lin-01")
    assert vm.power == "running"
    info = _info(vm, "snapshot")
    with AzureDiskExport(client, "job2abcdef", info, sas_duration_s=3600, snapshot_timeout_s=10, save=lambda: None) as ex:
        assert len(info.snapshot_ids) == 2 and all(s.rsplit("/", 1)[-1].startswith("oci-umt-job2abcd-") for s in info.snapshot_ids)
        assert set(info.sas_granted) == set(info.snapshot_ids)  # SAS on the snapshots, not the disks
        assert fleet.sas_granted == info.snapshot_ids
        snap = fleet.snapshots[info.snapshot_ids[0].lower()]
        assert snap.body["properties"]["incremental"] is True and snap.body["tags"] == {"oci-umt-job": "job2abcdef"}
        assert client.blob_head(ex.sas_url(0)).headers["Content-Length"] == str(len(vm.os_disk.data) + 512)
    assert vm.power == "running" and vm.ops == []  # the VM was never touched
    assert info.snapshot_ids == [] and info.sas_granted == []
    assert len(fleet.snapshots_deleted) == 2 and fleet.snapshots == {}

    # beginGetAccess on the second disk fails -> nothing stays granted or created
    vm2 = next(v for v in fleet.vms.values() if v.name == "win-01")
    fleet.add_vm(FakeVm("multi", FakeDisk("m-os", b"\1" * 4096, os_type="Linux"),
                        [FakeDisk("m-data", b"\1" * 4096, network_policy="DenyAll")], power="deallocated"))
    multi = next(v for v in fleet.vms.values() if v.name == "multi")
    orig = client.begin_get_access
    client.begin_get_access = lambda rid, dur, **kw: (orig(rid, dur, **kw) if "m-os" in rid
                                                      else (_ for _ in ()).throw(AzureError("beginGetAccess m-data: HTTP 403 DenyAll", status=403)))
    info = _info(multi, "snapshot")
    with pytest.raises(AzureError, match="DenyAll"):
        with AzureDiskExport(client, "job3", info, sas_duration_s=60, snapshot_timeout_s=10, save=lambda: None):
            raise AssertionError("must not enter")
    client.begin_get_access = orig
    assert info.snapshot_ids == [] and info.sas_granted == [] and len(fleet.snapshots) == 0
    assert vm2.os_disk.sas_token is None

    # release with a resource that is already gone: dropped silently
    info = AzureSourceInfo(tenant_id=TENANT, subscription_id=SUB, resource_group="rg", disk_ids=[],
                           sas_granted=["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/disks/gone"],
                           snapshot_ids=["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/snapshots/gone"])
    actions = release_azure_resources(client, info)
    assert info.sas_granted == [] and info.snapshot_ids == []
    assert all(a.startswith("ok") for a in actions)


def test_snapshot_name_is_valid():
    name = snapshot_name("0123456789abcdef", "/subs/x/disks/my disk (os)!", 0)
    assert name.startswith("oci-umt-01234567-0-my-disk-") and len(name) <= 80
    assert all(c.isalnum() or c in "-_." for c in name)
    long = snapshot_name("abcdefgh" * 4, "/x/disks/" + "d" * 100, 12)
    assert len(long) <= 80


def test_connector_default_factory_builds_real_client():
    c = AzureConnector(Settings())._factory(TENANT, CLIENT_ID, SECRET)
    assert isinstance(c, AzureClient) and c.arm_base == "https://management.azure.com"
    c.close()


def test_append_sas_query_preserves_existing_signature():
    sas = "https://md.blob.storage.azure.net/c/b?sv=2019-07-07&sr=b&sig=abc%2Bdef%3D"
    assert _append_sas_query(sas, {"comp": "pagelist", "maxresults": "10000"}) == (
        sas + "&comp=pagelist&maxresults=10000"
    )


def test_blob_get_appends_query_without_httpx_params():
    """httpx ``params=`` re-encodes the SAS query string and Azure returns HTTP 401."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<PageList></PageList>")

    client = AzureClient("tenant", "id", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    sas = "https://md.blob.storage.azure.net/c/b?sv=2019-07-07&sr=b&sig=abc%2Bdef%3D"
    client.blob_get(sas, params={"comp": "pagelist", "maxresults": "10000"})
    assert len(requests) == 1
    url = str(requests[0].url)
    assert "comp=pagelist" in url and "maxresults=10000" in url
    assert "sig=abc%2Bdef%3D" in url
    client.close()
