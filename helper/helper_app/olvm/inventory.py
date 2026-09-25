"""OLVM inventory: the VM list for the web UI and ``VmSpec`` extraction for a selected VM.

The rest of the pipeline only knows ``VmSpec``. The guest OS is expressed as a vSphere-style
``guest_id`` plus a display name so ``oci.mapping.map_guest_os`` works unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary
from helper_app.olvm.client import OlvmClient
from helper_app.olvm.session import OlvmSession

GIB = 1024**3

# Statuses that are neither up nor down: the disk lock or the power state will change under us.
TRANSIENT = {
    "migrating", "image_locked", "powering_up", "powering_down", "wait_for_launch",
    "reboot_in_progress", "saving_state", "restoring_state",
}

POWER = {
    "up": "poweredOn",
    "down": "poweredOff",
    "powering_up": "poweringOn",
    "powering_down": "poweringOff",
    "paused": "suspended",
    "suspended": "suspended",
}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def attachments(vm: dict) -> list[dict]:
    block = vm.get("disk_attachments") or {}
    raw = block.get("disk_attachment") if isinstance(block, dict) else block
    atts = [a for a in _as_list(raw) if isinstance(a, dict)]
    atts.sort(key=lambda a: (0 if _truthy(a.get("bootable")) else 1,
                             str((a.get("disk") or {}).get("name") or "")))
    return atts


def power_state(vm: dict) -> str:
    status = str(vm.get("status") or "").lower()
    return POWER.get(status, status or "unknown")


def cluster_name(vm: dict) -> str:
    cluster = vm.get("cluster") or {}
    return str(cluster.get("name") or "") if isinstance(cluster, dict) else ""


def guest_agent_running(vm: dict) -> bool:
    if str(vm.get("status") or "").lower() != "up":
        return False
    return bool(vm.get("fqdn") or vm.get("guest_operating_system") or vm.get("guest_time"))


def firmware_of(vm: dict) -> tuple[Firmware, bool]:
    bios = str((vm.get("bios") or {}).get("type") or "").lower()
    if "secure_boot" in bios:
        return Firmware.EFI, True
    if "ovmf" in bios or "uefi" in bios:
        return Firmware.EFI, False
    return Firmware.BIOS, False


def guess_guest_os(os_type: str) -> tuple[str, str]:
    """``(guest_id, guest_full_name)`` in vSphere terms, from the oVirt ``os.type``."""
    kind = (os_type or "").strip().lower()
    if not kind or kind in ("other", "other_linux", "otherlinux"):
        return "otherLinux64Guest", "Linux (64-bit)"
    if kind.startswith("windows"):
        if re.search(r"windows_?11\b", kind):
            return "windows11_64Guest", "Microsoft Windows 11 (64-bit)"
        if re.search(r"windows_?10\b", kind):
            return "windows10_64Guest", "Microsoft Windows 10 (64-bit)"
        compact = kind.replace("_", "")
        match = re.search(r"(20\d\d)(r2)?", compact)
        if match:
            year = match.group(1)
            r2 = " R2" if match.group(2) else ""
            return f"windows{year}srv_64Guest", f"Microsoft Windows Server {year}{r2}"
        return "windows2019srv_64Guest", "Microsoft Windows"
    match = re.search(r"^(?:ol|oraclelinux)_?(\d+)", kind)
    if match:
        return f"oracleLinux{match.group(1)}_64Guest", f"Oracle Linux {match.group(1)}"
    match = re.search(r"^rhel_?(\d+)", kind)
    if match:
        return f"rhel{match.group(1)}_64Guest", f"Red Hat Enterprise Linux {match.group(1)}"
    match = re.search(r"^centos_?(\d+)", kind)
    if match:
        return f"centos{match.group(1)}_64Guest", f"CentOS {match.group(1)}"
    match = re.search(r"^ubuntu_?(\d+)_(\d+)", kind)
    if match:
        return "ubuntu64Guest", f"Ubuntu {match.group(1)}.{match.group(2)}"
    match = re.search(r"^sles_?(\d+)", kind)
    if match:
        return f"sles{match.group(1)}_64Guest", f"SUSE Linux Enterprise Server {match.group(1)}"
    match = re.search(r"^debian_?(\d+)", kind)
    if match:
        return f"debian{match.group(1)}_64Guest", f"Debian {match.group(1)}"
    if "rocky" in kind:
        match = re.search(r"(\d+)", kind)
        version = match.group(1) if match else ""
        name = f"Rocky Linux {version}".strip()
        return "rockylinux_64Guest", name
    return "otherLinux64Guest", os_type or "Linux (64-bit)"


def _reported_version(guest: dict) -> str:
    version = guest.get("version") or {}
    if not isinstance(version, dict):
        return ""
    full = str(version.get("full_version") or "").strip()
    if full:
        return full
    major = str(version.get("major") or "").strip()
    minor = str(version.get("minor") or "").strip()
    if major and minor and minor not in ("0",):
        return f"{major}.{minor}"
    return major


def _windows_from_guest(text: str) -> tuple[str, str]:
    low = text.lower()
    match = re.search(r"server\s*(20\d\d)", low) or re.search(r"\b(20\d\d)\b", low)
    if match and "server" in low:
        year = match.group(1)
        return f"windows{year}srv_64Guest", f"Microsoft Windows Server {year}"
    match = re.search(r"windows\s*(1[01])\b|\b(1[01])\b", low)
    if match:
        release = match.group(1) or match.group(2)
        return f"windows{release}_64Guest", f"Microsoft Windows {release} (64-bit)"
    return "windows2019srv_64Guest", "Microsoft Windows"


def _from_guest_agent(guest: Any) -> tuple[str, str] | None:
    """``(guest_id, guest_full_name)`` from the oVirt guest agent, when it reported a distribution."""
    if not isinstance(guest, dict):
        return None
    distro = str(guest.get("distribution") or "").strip()
    family = str(guest.get("family") or "").strip().lower()
    version = _reported_version(guest)
    blob = f"{distro} {version}".strip()
    low = blob.lower()
    if not low:
        kernel = str(((guest.get("kernel") or {}).get("version") or {}).get("full_version") or "")
        if kernel.endswith("-generic"):
            return "ubuntu64Guest", "Ubuntu Linux (64-bit)"
        return None
    if "windows" in low or family == "windows":
        return _windows_from_guest(blob)
    ubuntu = re.search(r"(\d{2}\.\d{2})", version)
    if "ubuntu" in low:
        if ubuntu:
            return "ubuntu64Guest", f"Ubuntu {ubuntu.group(1)}"
        return "ubuntu64Guest", "Ubuntu Linux (64-bit)"
    major = ""
    match = re.search(r"(\d+)", version)
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
    if distro:
        return "otherLinux64Guest", distro
    return None


def guest_os_from_vm(vm: dict) -> tuple[str, str]:
    """Guest identity for ``map_guest_os``.

    The guest agent (``guest_operating_system``) names the installed OS and release. The VM's
    configured ``os.type`` is only a fallback: it is often ``other_linux`` even when the guest
    agent knows the distribution.
    """
    reported = _from_guest_agent(vm.get("guest_operating_system"))
    if reported:
        return reported
    return guess_guest_os(str((vm.get("os") or {}).get("type") or ""))


def _cpu_count(vm: dict) -> int:
    topo = ((vm.get("cpu") or {}).get("topology") or {})
    sockets = int(topo.get("sockets") or 1)
    cores = int(topo.get("cores") or 1)
    threads = int(topo.get("threads") or 1)
    return max(1, sockets * cores * threads)


def _memory_mb(vm: dict) -> int:
    raw = int(vm.get("memory") or 0)
    return max(1, raw // (1024 * 1024)) if raw else 1024


def _nics(vm: dict) -> list[NicSpec]:
    block = vm.get("nics") or {}
    raw = block.get("nic") if isinstance(block, dict) else block
    nics = []
    for nic in _as_list(raw):
        if not isinstance(nic, dict):
            continue
        profile = nic.get("vnic_profile") or {}
        mac = nic.get("mac") or {}
        nics.append(NicSpec(
            label=str(nic.get("name") or "nic"),
            adapter_type=str(nic.get("interface") or "virtio"),
            mac_address=str(mac.get("address") or "") if isinstance(mac, dict) else "",
            network=str(profile.get("name") or "") if isinstance(profile, dict) else "",
        ))
    return nics


def vm_spec_from_olvm(vm: dict) -> tuple[VmSpec, list[str]]:
    """``(VmSpec, disk ids)`` with disk ids in the same order as ``VmSpec.disks``."""
    guest_id, full_name = guest_os_from_vm(vm)
    firmware, secure = firmware_of(vm)
    disks: list[DiskSpec] = []
    disk_ids: list[str] = []
    for att in attachments(vm):
        disk = att.get("disk") or {}
        disk_id = str(disk.get("id") or "")
        label = str(disk.get("name") or disk_id or f"disk {len(disks)}")
        disks.append(DiskSpec(
            index=len(disks),
            label=label,
            device_key=len(disks),
            capacity_bytes=int(disk.get("provisioned_size") or 0),
            controller_type=str(att.get("interface") or "virtio_scsi").lower(),
            controller_class="OlvmDisk",
            controller_bus=0,
            unit_number=len(disks),
            thin_provisioned=str(disk.get("format") or "").lower() == "cow",
            backing_file=disk_id,
        ))
        disk_ids.append(disk_id)
    spec = VmSpec(
        moid=str(vm.get("id") or ""),
        name=str(vm.get("name") or ""),
        num_cpu=_cpu_count(vm),
        memory_mb=_memory_mb(vm),
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=firmware,
        secure_boot=secure,
        power_state=power_state(vm),
        host_name=cluster_name(vm),
        disks=disks,
        nics=_nics(vm),
    )
    return spec, disk_ids


def is_template(vm: dict) -> bool:
    return "/templates/" in str(vm.get("href") or "")


@dataclass
class OlvmVmDetails:
    spec: VmSpec
    cluster: str
    disk_ids: list[str]
    vm: dict


def list_vm_summaries(session: OlvmSession) -> list[VmSummary]:
    rows = []
    for vm in session.client.list_vms():
        if is_template(vm):
            continue
        spec, _ids = vm_spec_from_olvm(vm)
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
            disk_capacity_bytes=sum(d.capacity_bytes for d in spec.disks),
        ))
    rows.sort(key=lambda row: row.name.lower())
    return rows


def inspect_vm(client: OlvmClient, vm_id: str) -> OlvmVmDetails:
    vm = client.get_vm(vm_id)
    spec, disk_ids = vm_spec_from_olvm(vm)
    return OlvmVmDetails(spec=spec, cluster=cluster_name(vm), disk_ids=disk_ids, vm=vm)


def preflight(details: OlvmVmDetails) -> list[str]:
    problems: list[str] = []
    vm = details.vm
    origin = str(vm.get("origin") or "").lower()
    if origin == "hosted_engine" or str(vm.get("name") or "").lower() == "hostedengine":
        problems.append("the hosted engine VM cannot be migrated")
    status = str(vm.get("status") or "").lower()
    if status in TRANSIENT:
        problems.append(f"VM is {status.replace('_', ' ')}; wait until it is up or down")
    elif status == "suspended":
        problems.append("VM is suspended; shut it down in OLVM before migrating")
    elif status not in ("up", "down", ""):
        problems.append(f"VM is {status or 'in an unknown state'}; wait until it is up or down")
    if not details.spec.disks:
        problems.append("VM has no disks")
    for att in attachments(vm):
        disk = att.get("disk") or {}
        name = str(disk.get("name") or disk.get("id") or "disk")
        storage = str(disk.get("storage_type") or "image").lower()
        if storage != "image":
            problems.append(f"disk {name} is a {storage} disk; only image disks can be transferred")
        disk_status = str(disk.get("status") or "ok").lower()
        if disk_status != "ok":
            problems.append(f"disk {name} is {disk_status}; it cannot be transferred until it is ok")
        if int(disk.get("provisioned_size") or 0) <= 0:
            problems.append(f"disk {name} has no size")
    return problems


def warnings(details: OlvmVmDetails) -> list[str]:
    notes = []
    if details.spec.guest_id.startswith("other"):
        notes.append("OLVM did not report a specific guest OS; confirm the OS version on the migration form")
    if details.spec.power_state == "poweredOn":
        notes.append("The VM is powered on: it is shut down right before the disk export and stays powered off")
    return notes
