"""Hyper-V inventory: the VM list for the web UI and ``VmSpec`` extraction for a selected VM.

The rest of the pipeline only knows ``VmSpec``. The guest OS is expressed as a vSphere-style
``guest_id`` plus a display name so ``oci.mapping.map_guest_os`` works unchanged. Integration
services KVP (``OSName`` / ``OSVersion``) is the source; a VM with no guest data stays generic
and the migration form's OS dropdown overrides it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from helper_app.hyperv.client import HypervClient
from helper_app.hyperv.session import HypervSession
from helper_app.hyperv.smb import to_unc
from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary

MAX_OCI_VOLUME_BYTES = 32 * 1024**4

_OFF = {"off"}
_ON = {"running"}
_SAVED = {"saved", "paused", "suspended", "hibernated", "fastsaved"}
_TRANSIENT_WORDS = ("starting", "stopping", "saving", "pausing", "resuming", "reset", "critical", "fastsaving")


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return [value]


def power_state(vm: dict) -> str:
    state = str(vm.get("state") or "").strip().lower()
    if state in _ON:
        return "poweredOn"
    if state in _OFF:
        return "poweredOff"
    if state in _SAVED:
        return "suspended"
    return state or "unknown"


def firmware_of(vm: dict) -> tuple[Firmware, bool]:
    generation = int(vm.get("generation") or 1)
    secure = bool(vm.get("secure_boot"))
    if generation >= 2:
        return Firmware.EFI, secure
    return Firmware.BIOS, False


def guest_os_from_kvp(os_name: str, os_version: str = "") -> tuple[str, str]:
    """``(guest_id, guest_full_name)`` from Hyper-V integration-services KVP."""
    blob = f"{os_name or ''} {os_version or ''}".strip()
    if not blob:
        return "otherLinux64Guest", "Linux (64-bit)"
    low = blob.lower()
    if "windows" in low:
        return _windows(blob)
    ubuntu = re.search(r"(\d{2}\.\d{2})", blob)
    if "ubuntu" in low:
        if ubuntu:
            return "ubuntu64Guest", f"Ubuntu {ubuntu.group(1)}"
        return "ubuntu64Guest", "Ubuntu Linux (64-bit)"
    major = ""
    match = re.search(r"(\d+)", os_version or os_name or "")
    if match:
        major = match.group(1)
    named = (
        ("oracle linux", "oracleLinux", "Oracle Linux"),
        ("red hat", "rhel", "Red Hat Enterprise Linux"),
        ("rhel", "rhel", "Red Hat Enterprise Linux"),
        ("centos", "centos", "CentOS"),
        ("rocky", "rockylinux", "Rocky Linux"),
        ("alma", "almalinux", "AlmaLinux"),
        ("debian", "debian", "Debian"),
        ("suse", "sles", "SUSE Linux Enterprise Server"),
    )
    for needle, prefix, title in named:
        if needle not in low:
            continue
        if prefix in ("rockylinux", "almalinux"):
            return f"{prefix}_64Guest", f"{title} {major}".strip()
        if major:
            return f"{prefix}{major}_64Guest", f"{title} {major}"
        return "otherLinux64Guest", title
    if os_name:
        return "otherLinux64Guest", os_name.strip()
    return "otherLinux64Guest", "Linux (64-bit)"


def _windows(text: str) -> tuple[str, str]:
    low = text.lower()
    if "server" in low:
        match = re.search(r"(20\d\d)\s*r2", low) or re.search(r"(20\d\d)", low)
        if match:
            year = match.group(1)
            r2 = " R2" if re.search(rf"{year}\s*r2", low) else ""
            return f"windows{year}srv_64Guest", f"Microsoft Windows Server {year}{r2}"
    match = re.search(r"windows\s*(1[01])\b", low)
    if match:
        release = match.group(1)
        return f"windows{release}_64Guest", f"Microsoft Windows {release} (64-bit)"
    return "windows2019srv_64Guest", "Microsoft Windows"


def _mac(value: str) -> str:
    hexes = re.sub(r"[^0-9a-fA-F]", "", value or "")
    if len(hexes) == 12:
        return ":".join(hexes[i:i + 2] for i in range(0, 12, 2)).lower()
    return value or ""


def _disk_sort_key(disk: dict) -> tuple:
    controller = str(disk.get("controller") or "").lower()
    order = {"ide": 0, "scsi": 1}.get(controller, 2)
    return (0 if disk.get("boot") else 1, order, int(disk.get("controller_number") or 0),
            int(disk.get("controller_location") or 0))


def _chain(disk: dict) -> list[str]:
    paths = [str(path) for path in _as_list(disk.get("chain")) if str(path or "").strip()]
    leaf = str(disk.get("path") or "").strip()
    if leaf and (not paths or paths[0] != leaf):
        paths.insert(0, leaf)
    return paths


def _label(path: str, index: int) -> str:
    name = path.replace("/", "\\").rstrip("\\").split("\\")[-1]
    return name or f"disk {index}"


def vm_spec_from_hyperv(vm: dict, host: str) -> tuple[VmSpec, list[list[str]]]:
    """``(VmSpec, disk chains)``. Each chain is leaf path first, in ``VmSpec.disks`` order."""
    guest_id, full_name = guest_os_from_kvp(str(vm.get("os_name") or ""), str(vm.get("os_version") or ""))
    firmware, secure = firmware_of(vm)
    disks: list[DiskSpec] = []
    chains: list[list[str]] = []
    ordered = sorted(_as_list(vm.get("disks")), key=_disk_sort_key)
    for raw in ordered:
        if not isinstance(raw, dict):
            continue
        chain = _chain(raw)
        leaf = chain[0] if chain else str(raw.get("path") or "")
        controller = str(raw.get("controller") or "scsi").lower()
        disks.append(DiskSpec(
            index=len(disks),
            label=_label(leaf, len(disks)),
            device_key=len(disks),
            capacity_bytes=int(raw.get("size_bytes") or 0),
            controller_type="ide" if controller == "ide" else "scsi",
            controller_class="HypervDisk",
            controller_bus=int(raw.get("controller_number") or 0),
            unit_number=int(raw.get("controller_location") or 0),
            thin_provisioned=str(raw.get("vhd_type") or "").lower() == "dynamic",
            backing_file=leaf,
        ))
        chains.append(chain)
    nics = []
    for nic in _as_list(vm.get("nics")):
        if not isinstance(nic, dict):
            continue
        nics.append(NicSpec(
            label=str(nic.get("name") or "nic"),
            adapter_type="synthetic",
            mac_address=_mac(str(nic.get("mac") or "")),
            network=str(nic.get("switch") or ""),
        ))
    spec = VmSpec(
        moid=str(vm.get("id") or ""),
        name=str(vm.get("name") or ""),
        num_cpu=max(1, int(vm.get("cpu") or 1)),
        memory_mb=max(1, int(vm.get("memory_mb") or 1024)),
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=firmware,
        secure_boot=secure,
        power_state=power_state(vm),
        host_name=host,
        has_snapshots=any(len(chain) > 1 for chain in chains),
        disks=disks,
        nics=nics,
    )
    return spec, chains


@dataclass
class HypervVmDetails:
    spec: VmSpec
    host: str
    chains: list[list[str]]
    vm: dict


def list_vm_summaries(session: HypervSession) -> list[VmSummary]:
    rows = []
    for vm in session.client.list_vms():
        spec, _chains = vm_spec_from_hyperv(vm, session.host)
        rows.append(VmSummary(
            moid=spec.moid,
            name=spec.name,
            folder=spec.host_name,
            power_state=spec.power_state,
            guest_full_name=spec.guest_full_name,
            guest_id=spec.guest_id,
            num_cpu=spec.num_cpu,
            memory_mb=spec.memory_mb,
            num_disks=len(spec.disks),
            disk_capacity_bytes=sum(disk.capacity_bytes for disk in spec.disks),
            encrypted=bool(vm.get("shielded")),
        ))
    rows.sort(key=lambda row: row.name.lower())
    return rows


def inspect_vm(client: HypervClient, vm_id: str, host: str) -> HypervVmDetails:
    vm = client.get_vm(vm_id)
    spec, chains = vm_spec_from_hyperv(vm, host)
    return HypervVmDetails(spec=spec, host=host, chains=chains, vm=vm)


def _state_problem(state: str) -> str:
    if state in _SAVED:
        return (f"VM is {state}; resume it and shut it down in Hyper-V Manager before migrating "
                "(a saved state is not discarded)")
    if state in _OFF or state in _ON or state == "":
        return ""
    if any(word in state for word in _TRANSIENT_WORDS) or state not in (_OFF | _ON):
        return f"VM is {state or 'in an unknown state'}; wait until it is running or off"
    return ""


def preflight(details: HypervVmDetails) -> list[str]:
    problems: list[str] = []
    vm = details.vm
    if vm.get("shielded"):
        problems.append("VM is shielded; Hyper-V will not expose the disk contents")
    state = str(vm.get("state") or "").strip().lower()
    problem = _state_problem(state)
    if problem:
        problems.append(problem)
    raw_disks = [disk for disk in _as_list(vm.get("disks")) if isinstance(disk, dict)]
    if not raw_disks:
        problems.append("VM has no disks")
    for raw in sorted(raw_disks, key=_disk_sort_key):
        chain = _chain(raw)
        label = _label(chain[0] if chain else str(raw.get("path") or ""), 0)
        if raw.get("passthrough") or not str(raw.get("path") or "").strip():
            problems.append(f"disk {label} is a pass-through disk; only VHD and VHDX files can be copied")
            continue
        if raw.get("shared"):
            problems.append(f"disk {label} is a shared VHDX")
        size = int(raw.get("size_bytes") or 0)
        if size <= 0:
            problems.append(f"disk {label} has no size")
        elif size > MAX_OCI_VOLUME_BYTES:
            problems.append(f"disk {label} is larger than the 32 TB OCI volume maximum")
        vhd_type = str(raw.get("vhd_type") or "").lower()
        if vhd_type == "differencing" and len(chain) < 2:
            problems.append(f"disk {label} is a differencing disk whose parent file is missing")
        try:
            to_unc(details.host, chain[0])
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc))
    return problems


def warnings(details: HypervVmDetails) -> list[str]:
    notes = []
    if details.spec.guest_id.startswith("other"):
        notes.append("Hyper-V did not report a specific guest OS; confirm the OS version on the migration form")
    if details.spec.power_state == "poweredOn":
        notes.append("The VM is powered on: it is shut down right before the disk export and stays powered off")
    if details.spec.has_snapshots:
        notes.append("The VM has checkpoints: the copy reads the active disk and its parents, and does not merge them")
    return notes


def guest_services_running(vm: dict) -> bool:
    """Integration services last reported a guest OS, which is what a guest shutdown needs."""
    return power_state(vm) == "poweredOn" and bool(str(vm.get("os_name") or "").strip())
