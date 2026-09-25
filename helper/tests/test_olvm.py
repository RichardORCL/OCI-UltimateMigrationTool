"""OLVM inventory mapping, preflight and image-extent copy, without the web API."""

from urllib.parse import parse_qs

import httpx
import pytest

from helper_app.disk.imageio_range_copy import allocated_ranges, copy_extents
from helper_app.models import Firmware
from helper_app.oci.mapping import map_guest_os, resolve_target_os
from helper_app.olvm.client import OlvmAuthError, OlvmClient
from helper_app.olvm.export import OlvmDiskExport, transfer_download_url
from helper_app.olvm.inventory import (
    OlvmVmDetails,
    firmware_of,
    guess_guest_os,
    guest_os_from_vm,
    preflight,
    vm_spec_from_olvm,
)

from .fake_olvm import (
    ENGINE,
    HOSTED,
    LOCKED,
    LUN,
    MOVING,
    PASSWORD,
    USER,
    WEB,
    FakeOlvm,
    extents_of,
    make_fleet,
)
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


def test_guest_agent_reports_the_installed_os():
    vm = {
        "os": {"type": "other_linux"},
        "guest_operating_system": {
            "distribution": "Ubuntu",
            "family": "Linux",
            "architecture": "x86_64",
            "version": {"full_version": "26.04", "major": "26", "minor": "4"},
        },
    }
    guest_id, full_name = guest_os_from_vm(vm)
    assert (guest_id, full_name) == ("ubuntu64Guest", "Ubuntu 26.04")
    meta = map_guest_os(guest_id, full_name)
    assert (meta.operating_system, meta.operating_system_version, meta.version_detected) == ("Ubuntu", "26.04", True)
    # the configured type is only the fallback
    assert guest_os_from_vm({"os": {"type": "rhel_9x64"}})[0] == "rhel9_64Guest"
    windows = guest_os_from_vm({"guest_operating_system": {
        "distribution": "Windows Server 2022", "family": "Windows",
        "version": {"full_version": "10.0"},
    }})
    assert windows == ("windows2022srv_64Guest", "Microsoft Windows Server 2022")


def test_target_os_override_replaces_the_detected_family():
    meta = resolve_target_os("otherLinux64Guest", "Linux (64-bit)", "Ubuntu", "24.04")
    assert (meta.operating_system, meta.operating_system_version, meta.family) == ("Ubuntu", "24.04", "linux")
    kept = resolve_target_os("rhel9_64Guest", "Red Hat Enterprise Linux 9", None, None)
    assert (kept.operating_system, kept.operating_system_version) == ("Red Hat Enterprise Linux", "9")


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


def test_direct_kvm_download_uses_the_host_url():
    transfer = {
        "proxy_url": "https://olvm.test:54323/images/abc",
        "transfer_url": "https://kvm01.test:54322/images/abc",
    }
    assert transfer_download_url(transfer) == "https://olvm.test:54323/images/abc"
    assert transfer_download_url(transfer, direct_from_host=True) == "https://kvm01.test:54322/images/abc"
    assert transfer_download_url({"proxy_url": "https://olvm.test:54323/images/abc"}, direct_from_host=True) == (
        "https://olvm.test:54323/images/abc"
    )

    fleet = _fleet()
    client = fleet.client_factory(ENGINE, USER, PASSWORD, False)
    export = OlvmDiskExport(client, inactivity_timeout_s=30, ready_timeout_s=5, direct_from_host=True,
                            sleep=lambda _s: None)
    with export:
        opened = export.open("disk-web-os")
        assert opened.url.startswith("https://host.internal:54322/images/")
        export.finish(opened)


def test_export_exit_cancels_open_transfer():
    class Stub:
        def __init__(self):
            self.cancelled = []

        def cancel_transfer(self, transfer_id):
            self.cancelled.append(transfer_id)

    stub = Stub()
    export = OlvmDiskExport(stub, inactivity_timeout_s=10, ready_timeout_s=1)
    export.open_ids.append("transfer-1")
    export.__exit__(RuntimeError, RuntimeError("boom"), None)
    assert stub.cancelled == ["transfer-1"]
    assert export.open_ids == []


def test_download_uses_proxy_url_and_clears_a_disk_lock():
    """OLVM 4.5 omits signed_ticket and rejects PUT phase changes. The proxy URL is the credential,
    and a disk locked by an earlier transfer has to be cancelled before a new download."""
    fleet = _fleet()
    fleet.omit_ticket = True
    fleet.require_ticket = False
    client = fleet.client_factory(ENGINE, USER, PASSWORD, False)
    disk_id = "disk-web-os"
    stale = client.create_transfer(disk_id, 60)
    export = OlvmDiskExport(client, inactivity_timeout_s=30, ready_timeout_s=5, sleep=lambda _s: None)
    with export:
        transfer = export.open(disk_id)
        assert transfer.id != stale["id"]
        assert transfer.ticket == ""
        assert transfer.url.startswith("https://olvm.test:54323/images/")
        assert client.image_extents(transfer.url, transfer.ticket)
        assert len(client.read_image(transfer.url, transfer.ticket, 0, 16)) == 16
        export.finish(transfer)
    assert f"POST /ovirt-engine/api/imagetransfers/{stale['id']}/cancel" in fleet.requests
    assert not any(line.startswith("PUT ") and "/imagetransfers/" in line for line in fleet.requests)
    assert fleet.transfers_cancelled == [disk_id]
    assert fleet.transfers_finished == [disk_id]


def _token_client(handler) -> OlvmClient:
    return OlvmClient("https://olvm.test", "admin@ovirt", "secret", verify_ssl=False,
                      http=httpx.Client(transport=httpx.MockTransport(handler)))


def _posted_user(request: httpx.Request) -> str:
    return (parse_qs(request.content.decode()).get("username") or [""])[0]


def test_keycloak_user_is_retried_with_internal_profile():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        user = _posted_user(request)
        seen.append(user)
        if user == "admin@ovirt":
            return httpx.Response(400, json={
                "error": "Cannot authenticate user No valid profile found in credentials..",
            })
        if user == "admin@ovirt@internal":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(401, json={"error": "unexpected"})

    client = _token_client(handler)
    assert client.token() == "t"
    client._token = ""
    client._deadline = 0
    assert client.token() == "t"
    assert seen == ["admin@ovirt", "admin@ovirt@internal", "admin@ovirt@internal"]


def test_rejected_password_is_not_retried():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_posted_user(request))
        return httpx.Response(400, json={"error": "Cannot authenticate user Invalid user credentials."})

    client = _token_client(handler)
    with pytest.raises(OlvmAuthError, match="Invalid user credentials"):
        client.token()
    assert seen == ["admin@ovirt"]
