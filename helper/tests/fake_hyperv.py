"""Fake Hyper-V host: WinRM script replies and in-memory VHDX files, no network."""

from __future__ import annotations

import json
import re

from helper_app.hyperv.client import HypervAuthError, HypervClient, HypervError
from helper_app.hyperv.session import HypervConnector
from helper_app.hyperv.smb import to_unc

from .vhd_builders import build_dynamic_vhdx

HOST = "hv.test"
USER = r"HOST\Administrator"
PASSWORD = "secret"

WEB = "11111111-1111-1111-1111-111111111111"
WIN = "22222222-2222-2222-2222-222222222222"
SAVED = "33333333-3333-3333-3333-333333333333"
PASS = "44444444-4444-4444-4444-444444444444"
SHIELD = "55555555-5555-5555-5555-555555555555"
ORPHAN = "66666666-6666-6666-6666-666666666666"

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
BLOCK = 1024 * 1024


class MemFile:
    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def read_at(self, offset: int, length: int) -> bytes:
        chunk = self.data[offset:offset + length]
        if len(chunk) != length:
            raise HypervError(f"short read at {offset}")
        return chunk

    def close(self) -> None:
        return None


class FakeVm:
    def __init__(self, vm_id: str, name: str, *, state: str = "Off", generation: int = 2,
                 cpu: int = 2, memory_mb: int = 2048, os_name: str = "", os_version: str = "",
                 secure_boot: bool = False, shielded: bool = False, disks: list | None = None,
                 stubborn: bool = False):
        self.id = vm_id
        self.name = name
        self.state = state
        self.generation = generation
        self.cpu = cpu
        self.memory_mb = memory_mb
        self.os_name = os_name
        self.os_version = os_version
        self.secure_boot = secure_boot
        self.shielded = shielded
        self.disks = disks or []
        self.stubborn = stubborn
        self.ops: list[str] = []

    def doc(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "generation": self.generation,
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "shielded": self.shielded,
            "secure_boot": self.secure_boot,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "nics": [{"name": "Network Adapter", "mac": "00155D010203", "switch": "External"}],
            "disks": self.disks,
        }


def _disk(path: str, size: int, *, controller: str = "SCSI", vhd_type: str = "Dynamic",
          chain: list[str] | None = None, shared: bool = False, passthrough: bool = False,
          boot: bool = True) -> dict:
    return {
        "path": path,
        "controller": controller,
        "controller_number": 0,
        "controller_location": 0,
        "size_bytes": size,
        "vhd_type": vhd_type,
        "shared": shared,
        "passthrough": passthrough,
        "boot": boot,
        "chain": chain if chain is not None else ([path] if path else []),
    }


class FakeHyperv:
    def __init__(self, vms: dict[str, FakeVm], files: dict[str, bytes]):
        self.vms = vms
        self.files = files

    def runner(self, username: str, password: str):
        def run(script: str) -> str:
            if username != USER or password != PASSWORD:
                raise HypervAuthError("invalid user name or password")
            if "hyperv-action: probe" in script:
                return "hv-host\n"
            if "hyperv-action: inventory" in script:
                only = ""
                match = re.search(r"\$OnlyId = '([^']*)'", script)
                if match:
                    only = match.group(1)
                chosen = [vm for vm in self.vms.values() if not only or vm.id == only]
                return "".join(json.dumps(vm.doc()) + "\n" for vm in chosen)
            match = _GUID.search(script)
            if match is None or match.group(0) not in self.vms:
                raise HypervError("virtual machine was not found", status=404)
            vm = self.vms[match.group(0)]
            if "hyperv-action: state" in script:
                return vm.state + "\n"
            if "hyperv-action: shutdown" in script:
                vm.ops.append("shutdown")
                if not vm.stubborn:
                    vm.state = "Off"
                return ""
            if "hyperv-action: turnoff" in script:
                vm.ops.append("turnoff")
                vm.state = "Off"
                return ""
            raise HypervError(f"unexpected Hyper-V script: {script.splitlines()[0]}")

        return run

    def connector(self, settings) -> HypervConnector:
        fake = self

        def factory(host, user, password, use_https, port, verify):
            return HypervClient(host, user, password, use_https=use_https, port=port, verify_ssl=verify,
                                runner=fake.runner(user, password))

        def opener_factory(host, user, password):
            def open_unc(unc: str) -> MemFile:
                if unc not in fake.files:
                    raise HypervError(f"cannot open {unc}")
                return MemFile(fake.files[unc])

            return open_unc

        return HypervConnector(settings, client_factory=factory, opener_factory=opener_factory)


def make_fleet() -> FakeHyperv:
    """A small host: one running Linux VM whose dynamic VHDX leaves the second block unallocated."""
    virtual = 2 * BLOCK
    payload = b"\x11" * 600
    web_path = r"C:\Hyper-V\web.vhdx"
    win_path = r"C:\Hyper-V\win.vhdx"
    files = {
        to_unc(HOST, web_path): build_dynamic_vhdx(virtual, BLOCK, {0: payload}),
        to_unc(HOST, win_path): build_dynamic_vhdx(BLOCK, BLOCK, {0: b"\x22" * 32}),
    }
    vms = {
        WEB: FakeVm(WEB, "web-01", state="Running", generation=2, secure_boot=True,
                    os_name="Ubuntu 24.04.1 LTS", os_version="24.04",
                    disks=[_disk(web_path, virtual)]),
        WIN: FakeVm(WIN, "win-01", state="Off", generation=1, os_name="Windows Server 2022 Datacenter",
                    os_version="10.0.20348", disks=[_disk(win_path, BLOCK, controller="IDE")]),
        SAVED: FakeVm(SAVED, "saved-01", state="Saved", os_name="Ubuntu 22.04 LTS",
                      disks=[_disk(web_path, virtual)]),
        PASS: FakeVm(PASS, "pass-01", state="Off",
                     disks=[_disk("", 0, passthrough=True, chain=[])]),
        SHIELD: FakeVm(SHIELD, "shield-01", state="Off", shielded=True, os_name="Windows Server 2022",
                       disks=[_disk(win_path, BLOCK)]),
        ORPHAN: FakeVm(ORPHAN, "orphan-01", state="Off",
                       disks=[_disk(r"C:\Hyper-V\leaf.avhdx", BLOCK, vhd_type="Differencing", chain=[])]),
    }
    return FakeHyperv(vms, files)
