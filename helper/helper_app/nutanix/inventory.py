"""Nutanix AHV inventory: the VM list for the web UI and ``VmSpec`` extraction for a selected VM.

The guest OS is expressed as a vSphere-style ``guest_id`` plus a display name so
``oci.mapping.map_guest_os`` works unchanged. AHV only knows the installed OS when Nutanix
Guest Tools (NGT) report it; otherwise the user confirms the OS on the migration form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary
from helper_app.nutanix.client import AhvDisk, AhvVm, NutanixClient
from helper_app.nutanix.session import NutanixSession
from helper_app.olvm.inventory import _from_guest_agent

POWER = {
    "ON": "poweredOn",
    "OFF": "poweredOff",
    "PAUSED": "suspended",
    "SUSPENDED": "suspended",
    "POWERING_ON": "poweringOn",
    "POWERING_OFF": "poweringOff",
}

CONTROLLER = {"SCSI": "scsi", "SATA": "sata", "IDE": "ide", "PCI": "pci", "SPAPR": "spapr"}


def power_state(vm: AhvVm) -> str:
    return POWER.get(vm.power_state, vm.power_state.lower() or "unknown")


def data_disks(vm: AhvVm) -> list[AhvDisk]:
    """Disks that carry data (no CD-ROMs), in bus order with the first SCSI/SATA/IDE disk first."""
    order = {"SCSI": 0, "SATA": 1, "IDE": 2, "PCI": 3}
    disks = [d for d in vm.disks if d.device_type == "DISK"]
    disks.sort(key=lambda d: (order.get(d.adapter, 9), d.index))
    return disks


def firmware_of(vm: AhvVm) -> tuple[Firmware, bool]:
    if vm.boot_type == "SECURE_BOOT":
        return Firmware.EFI, True
    if vm.boot_type == "UEFI":
        return Firmware.EFI, False
    return Firmware.BIOS, False


def guest_os_from_vm(vm: AhvVm) -> tuple[str, str]:
    """``(guest_id, guest_full_name)`` from the NGT guest OS string, for example
    ``linux:64:CentOS Linux-7.9.2009`` or ``windows:64:Windows Server 2019``."""
    text = vm.guest_os.strip()
    if not text:
        return "otherLinux64Guest", "Linux (64-bit)"
    parts = text.split(":", 2)
    family = parts[0].lower() if len(parts) == 3 else ""
    rest = parts[2] if len(parts) == 3 else text
    match = re.match(r"^(.*?)[-\s]+(\d[\w.]*)$", rest.strip())
    distro, version = (match.group(1), match.group(2)) if match else (rest.strip(), "")
    if family == "windows" or "windows" in rest.lower():
        family = "windows"
    reported = _from_guest_agent({"distribution": distro, "family": family,
                                  "version": {"full_version": version}})
    return reported or ("otherLinux64Guest", rest or "Linux (64-bit)")


def _nics(vm: AhvVm) -> list[NicSpec]:
    return [
        NicSpec(label=f"nic {i}", adapter_type=(nic.model or "virtio").lower(), mac_address=nic.mac,
                network=nic.subnet, ip_addresses=list(nic.ips))
        for i, nic in enumerate(vm.nics)
    ]


def vm_spec_from_ahv(vm: AhvVm) -> tuple[VmSpec, list[str]]:
    """``(VmSpec, vDisk uuids)`` with uuids in the same order as ``VmSpec.disks``."""
    guest_id, full_name = guest_os_from_vm(vm)
    firmware, secure = firmware_of(vm)
    disks: list[DiskSpec] = []
    disk_ids: list[str] = []
    for disk in data_disks(vm):
        controller = CONTROLLER.get(disk.adapter, disk.adapter.lower() or "unknown")
        disks.append(DiskSpec(
            index=len(disks),
            label=f"{controller}.{disk.index}",
            device_key=len(disks),
            capacity_bytes=disk.size_bytes,
            controller_type=controller,
            controller_class="AhvDisk",
            controller_bus=0,
            unit_number=disk.index,
            thin_provisioned=True,
            backing_file=disk.uuid,
        ))
        disk_ids.append(disk.uuid)
    spec = VmSpec(
        moid=vm.uuid,
        name=vm.name,
        instance_uuid=vm.uuid,
        num_cpu=max(1, vm.sockets * vm.cores_per_socket),
        memory_mb=vm.memory_mb,
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=firmware,
        secure_boot=secure,
        power_state=power_state(vm),
        host_name=vm.cluster,
        has_vtpm=vm.vtpm,
        disks=disks,
        nics=_nics(vm),
    )
    return spec, disk_ids


@dataclass
class NutanixVmDetails:
    spec: VmSpec
    cluster: str
    disk_ids: list[str]
    vm: AhvVm

    @property
    def container_uuids(self) -> list[str]:
        return [d.container_uuid for d in data_disks(self.vm)]


def list_vm_summaries(session: NutanixSession) -> list[VmSummary]:
    rows = []
    for vm in session.client.list_vms():
        if vm.is_cvm:
            continue
        spec, _ids = vm_spec_from_ahv(vm)
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


def inspect_vm(client: NutanixClient, vm_uuid: str) -> NutanixVmDetails:
    vm = client.get_vm(vm_uuid)
    spec, disk_ids = vm_spec_from_ahv(vm)
    return NutanixVmDetails(spec=spec, cluster=vm.cluster, disk_ids=disk_ids, vm=vm)


def preflight(details: NutanixVmDetails) -> list[str]:
    problems: list[str] = []
    vm = details.vm
    if vm.is_cvm:
        problems.append("a Nutanix controller VM (CVM) cannot be migrated")
    if vm.power_state not in ("ON", "OFF", ""):
        problems.append(f"VM is {vm.power_state.lower().replace('_', ' ')}; power it on or off in Prism first")
    if not details.spec.disks:
        problems.append("VM has no disks" if not vm.disks else "VM has only CD-ROM drives, no disks")
    for disk in data_disks(vm):
        name = f"{CONTROLLER.get(disk.adapter, disk.adapter.lower())}.{disk.index}"
        if disk.volume_group:
            problems.append(f"disk {name} belongs to a volume group; only VM disks can be cloned to an image")
        if not disk.uuid:
            problems.append(f"disk {name} has no vDisk uuid")
        if disk.size_bytes <= 0:
            problems.append(f"disk {name} has no size")
    return problems


def warnings(details: NutanixVmDetails) -> list[str]:
    notes = []
    if not details.vm.guest_os:
        notes.append("Nutanix Guest Tools did not report the guest OS; confirm the OS version on the migration form")
    if details.spec.has_vtpm:
        notes.append("The VM has a vTPM; its keys are not migrated (BitLocker or LUKS bound to the TPM needs "
                     "the recovery key in OCI)")
    if details.spec.power_state == "poweredOn":
        notes.append("The VM is powered on: it is shut down right before the disk export and stays powered off")
        if not details.vm.ngt_installed:
            notes.append("Nutanix Guest Tools are not installed: the shutdown is an ACPI request, then a hard "
                         "power off if the guest does not stop in time")
    if any(d.device_type == "CDROM" for d in details.vm.disks):
        notes.append("CD-ROM drives are not migrated")
    return notes
