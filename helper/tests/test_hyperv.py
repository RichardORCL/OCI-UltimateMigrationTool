"""Hyper-V inventory mapping, preflight and VHD/VHDX reads, without the web API."""

from helper_app.disk.vhd_image import open_image
from helper_app.hyperv.inventory import (
    HypervVmDetails,
    firmware_of,
    guest_os_from_kvp,
    preflight,
    vm_spec_from_hyperv,
)
from helper_app.hyperv.power import shut_down
from helper_app.hyperv.smb import to_unc
from helper_app.models import Firmware
from helper_app.oci.mapping import map_guest_os

from .fake_hyperv import HOST, ORPHAN, PASS, SAVED, SHIELD, WEB, FakeHyperv, make_fleet
from .vhd_builders import build_differencing_vhdx, build_dynamic_vhd, build_dynamic_vhdx, build_fixed_vhd


class _Mem:
    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def read_at(self, offset: int, length: int) -> bytes:
        return self.data[offset:offset + length]


def _details(fleet: FakeHyperv, vm_id: str) -> HypervVmDetails:
    vm = fleet.vms[vm_id].doc()
    spec, chains = vm_spec_from_hyperv(vm, HOST)
    return HypervVmDetails(spec=spec, host=HOST, chains=chains, vm=vm)


def test_guest_os_and_firmware_mapping():
    guest_id, full = guest_os_from_kvp("Ubuntu 24.04.1 LTS", "24.04")
    meta = map_guest_os(guest_id, full)
    assert (guest_id, full) == ("ubuntu64Guest", "Ubuntu 24.04")
    assert (meta.operating_system, meta.operating_system_version, meta.version_detected) == ("Ubuntu", "24.04", True)
    guest_id, full = guest_os_from_kvp("Windows Server 2022 Datacenter", "10.0.20348")
    meta = map_guest_os(guest_id, full)
    assert guest_id == "windows2022srv_64Guest" and meta.operating_system == "Windows" and "2022" in meta.operating_system_version
    guest_id, full = guest_os_from_kvp("Oracle Linux Server", "9.4")
    assert guest_id == "oracleLinux9_64Guest" and full.startswith("Oracle Linux 9")
    guest_id, full = guest_os_from_kvp("", "")
    meta = map_guest_os(guest_id, full)
    assert meta.operating_system == "Custom Linux" and meta.version_detected is False

    bios, secure = firmware_of({"generation": 2, "secure_boot": True})
    assert bios is Firmware.EFI and secure is True
    bios, secure = firmware_of({"generation": 1, "secure_boot": False})
    assert bios is Firmware.BIOS and secure is False


def test_preflight_refusals():
    fleet = make_fleet()
    assert preflight(_details(fleet, WEB)) == []
    assert any("saved" in problem for problem in preflight(_details(fleet, SAVED)))
    assert any("pass-through" in problem for problem in preflight(_details(fleet, PASS)))
    assert any("shielded" in problem for problem in preflight(_details(fleet, SHIELD)))
    assert any("parent" in problem for problem in preflight(_details(fleet, ORPHAN)))


def test_unc_maps_a_drive_path_and_keeps_a_share_path():
    assert to_unc("hv.test", r"C:\Hyper-V\disk.vhdx") == r"\\hv.test\C$\Hyper-V\disk.vhdx"
    assert to_unc("hv.test", r"\\files\share\disk.vhdx") == r"\\files\share\disk.vhdx"


def test_dynamic_disks_skip_unallocated_blocks_and_a_differencing_chain_reads_the_parent():
    block = 1024 * 1024
    payload = b"\x11" * 600
    dynamic = open_image(_Mem(build_dynamic_vhdx(block * 2, block, {0: payload})))
    assert dynamic.allocated() == [(0, block)]
    assert dynamic.read(0, 4) == b"\x11\x11\x11\x11"
    assert dynamic.read(block, 4) == b"\x00\x00\x00\x00"

    vhd = open_image(_Mem(build_dynamic_vhd(block * 2, block, {1: b"\x33" * 16})))
    assert vhd.allocated() == [(block, block)]
    assert vhd.read(block, 2) == b"\x33\x33"

    fixed = open_image(_Mem(build_fixed_vhd(b"FIXED-DISK")))
    assert fixed.read(0, 5) == b"FIXED"
    assert fixed.allocated() == [(0, len(b"FIXED-DISK"))]

    child = open_image(_Mem(build_differencing_vhdx(block * 2, block, 512, {0: b"\x22" * 512})), parent=dynamic)
    assert child.read(0, 4) == b"\x22\x22\x22\x22"
    assert child.read(512, 4) == b"\x11\x11\x11\x11"
    assert child.allocated() == [(0, block)]


def test_shutdown_then_hard_turn_off():
    fleet = make_fleet()
    fleet.vms[WEB].stubborn = True
    client = fleet.connector(None)._factory("hv.test", "HOST\\Administrator", "secret", True, 5986, False)
    slept: list[float] = []
    result = shut_down(client, WEB, timeout_s=0, sleep=slept.append)
    assert result == "powered_off"
    assert fleet.vms[WEB].ops == ["shutdown", "turnoff"]
    assert fleet.vms[WEB].state == "Off"
    assert shut_down(client, WEB, timeout_s=0, sleep=slept.append) == "already_off"


def test_winrm_skips_channel_binding_unless_the_certificate_is_checked(monkeypatch):
    """A self-signed WinRM certificate makes the default channel-binding token fail as a bad password."""
    import sys
    import types

    from helper_app.hyperv.client import HypervClient

    calls = []

    class Result:
        status_code = 0
        std_out = b"HVHOST\n"
        std_err = b""

    class FakeSession:
        def __init__(self, endpoint, auth, **kwargs):
            calls.append((endpoint, auth, kwargs))

        def run_ps(self, script):
            return Result()

    winrm = types.ModuleType("winrm")
    winrm.Session = FakeSession
    monkeypatch.setitem(sys.modules, "winrm", winrm)

    client = HypervClient("hv.test", r"HOST\Administrator", "secret", use_https=True, port=5986, verify_ssl=False)
    assert client.probe() == "HVHOST"
    endpoint, auth, kwargs = calls[0]
    assert endpoint == "https://hv.test:5986/wsman"
    assert auth == (r"HOST\Administrator", "secret")
    assert kwargs["transport"] == "ntlm"
    assert kwargs["server_cert_validation"] == "ignore"
    assert kwargs["send_cbt"] is False

    client.verify_ssl = True
    client.probe()
    assert calls[1][2]["server_cert_validation"] == "validate"
    assert calls[1][2]["send_cbt"] is True
