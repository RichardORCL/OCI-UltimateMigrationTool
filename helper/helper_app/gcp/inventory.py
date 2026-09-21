"""GCP inventory: VM list and VmSpec extraction (vSphere-style guest IDs for map_guest_os)."""

from __future__ import annotations

import logging
import re
from typing import Optional

from helper_app.gcp.client import GcpClient, GcpError, normalize_instance_id, parse_instance_id
from helper_app.gcp.session import GcpSession
from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary

log = logging.getLogger(__name__)

GIB = 1024**3
POWER_STATES = {
    "running": "poweredOn",
    "terminated": "poweredOff",
    "stopping": "stopping",
    "starting": "starting",
    "provisioning": "starting",
    "staging": "starting",
    "suspending": "stopping",
    "suspended": "stopped",
    "repairing": "starting",
}


def power_state(instance: dict) -> str:
    return POWER_STATES.get(str(instance.get("status") or "").lower(), str(instance.get("status") or "unknown"))


def instance_moid(instance: dict) -> str:
    self_link = str(instance.get("selfLink") or "")
    if "/compute/v1/" in self_link:
        return normalize_instance_id(self_link.split("/compute/v1/", 1)[-1])
    zone_url = str(instance.get("zone") or "")
    zone = zone_url.rsplit("/", 1)[-1]
    m = re.search(r"/projects/([^/]+)/zones/", zone_url)
    project = m.group(1) if m else ""
    name = str(instance.get("name") or "")
    return f"projects/{project}/zones/{zone}/instances/{name}"


def _zone_name(instance: dict) -> str:
    z = str(instance.get("zone") or "")
    return z.rsplit("/", 1)[-1] if z else ""


def _machine_short(instance: dict) -> str:
    mt = str(instance.get("machineType") or "")
    return mt.rsplit("/", 1)[-1] if mt else ""


_VERSION = re.compile(r"(\d+)(?:[_.\-](\d+))?")


def guess_guest_os(instance: dict, boot_disk: Optional[dict] = None) -> tuple[str, str]:
    labels = instance.get("labels") or {}
    desc = str(instance.get("description") or "").lower()
    src = ""
    if boot_disk:
        src = str(boot_disk.get("sourceImage") or boot_disk.get("licenses") or "")
    blob = " ".join([desc, src.lower(), " ".join(f"{k} {v}" for k, v in labels.items())])
    if "windows" in blob:
        if re.search(r"win(dows)?[-_ ]?1[01]\b", blob):
            rel = "11" if re.search(r"win(dows)?[-_ ]?11\b", blob) else "10"
            return f"windows{rel}_64Guest", f"Microsoft Windows {rel}"
        m = re.search(r"(20\d\d)", blob)
        if m:
            return f"windows{m.group(1)}srvGuest", f"Microsoft Windows Server {m.group(1)}"
        return "windowsGuest", "Microsoft Windows"
    if "ubuntu" in blob:
        m = re.search(r"(\d\d)[_.](\d\d)", blob)
        ver = f"{m.group(1)}.{m.group(2)}" if m else ""
        return "ubuntu64Guest", f"Ubuntu {ver} LTS".strip() if ver else "Ubuntu Linux (64-bit)"
    if "rhel" in blob or "red hat" in blob:
        m = _VERSION.search(blob)
        ver = m.group(1) if m else ""
        return (f"rhel{ver}_64Guest" if ver else "rhel9_64Guest"), f"Red Hat Enterprise Linux {ver}".strip()
    if "oracle" in blob or " ol" in blob:
        m = re.search(r"ol(\d+)", blob) or _VERSION.search(blob)
        ver = m.group(1) if m else ""
        return (f"oraclelinux{ver}_64Guest" if ver else "oraclelinux9_64Guest"), f"Oracle Linux {ver}".strip()
    if "debian" in blob:
        m = _VERSION.search(blob)
        ver = m.group(1) if m else ""
        return (f"debian{ver}_64Guest" if ver else "debian12_64Guest"), f"Debian {ver}".strip()
    if "centos" in blob:
        m = _VERSION.search(blob)
        ver = m.group(1) if m else ""
        return (f"centos{ver}_64Guest" if ver else "centos8_64Guest"), f"CentOS {ver}".strip()
    return "otherLinux64Guest", labels.get("name") or instance.get("name") or "Linux"


def _disk_capacity(disk: dict) -> int:
    return int(disk.get("sizeGb") or 0) * GIB


def vm_spec_from_gcp(instance: dict, disks: dict[str, dict], machine: dict) -> VmSpec:
    guest_id, full_name = guess_guest_os(instance, disks.get("boot"))
    disk_specs: list[DiskSpec] = []
    boot = instance.get("disks") or []
    for i, ref in enumerate(boot):
        if str(ref.get("type") or "").upper() == "SCRATCH":
            continue
        url = str(ref.get("source") or "")
        key = url.split("/compute/v1/", 1)[-1] if "/compute/v1/" in url else url
        doc = disks.get(key) or disks.get(url) or {}
        label = ref.get("deviceName") or doc.get("name") or f"disk-{i}"
        disk_specs.append(
            DiskSpec(
                index=len(disk_specs),
                label=label,
                device_key=i,
                capacity_bytes=_disk_capacity(doc) or GIB,
                controller_type="scsi",
                controller_class="GcpPersistentDisk",
                controller_bus=0,
                unit_number=i,
                thin_provisioned=True,
                backing_file=key,
            )
        )
    shielded = instance.get("shieldedInstanceConfig") or {}
    nics = [
        NicSpec(label=str(n.get("networkIP") or n.get("name") or f"nic-{i}"), adapter_type="gcp-vnic")
        for i, n in enumerate(instance.get("networkInterfaces") or [])
    ]
    return VmSpec(
        moid=instance_moid(instance),
        name=str(instance.get("name") or ""),
        instance_uuid=str(instance.get("id") or ""),
        num_cpu=int(machine.get("guestCpus") or 1),
        memory_mb=int(machine.get("memoryMb") or 1024),
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=Firmware.EFI if shielded.get("enableSecureBoot") else Firmware.EFI,
        secure_boot=bool(shielded.get("enableSecureBoot")),
        has_vtpm=bool(shielded.get("enableVtpm")),
        power_state=power_state(instance),
        disks=disk_specs,
        nics=nics,
    )


def vm_summary_from_gcp(instance: dict, project_name: str) -> VmSummary:
    zone = _zone_name(instance)
    boot = instance.get("disks") or []
    pd = [d for d in boot if str(d.get("type") or "").upper() != "SCRATCH"]
    guest_id, full_name = guess_guest_os(instance)
    return VmSummary(
        moid=instance_moid(instance),
        name=str(instance.get("name") or ""),
        folder=f"{project_name}/{zone}",
        power_state=power_state(instance),
        guest_full_name=full_name,
        guest_id=guest_id,
        num_cpu=0,
        memory_mb=0,
        num_disks=len(pd),
        disk_capacity_bytes=0,
        vm_size=_machine_short(instance),
        location=zone,
    )


def list_vm_summaries(session: GcpSession) -> list[VmSummary]:
    rows: list[VmSummary] = []
    client = session.client
    for proj in session.projects:
        try:
            instances = client.aggregated_instances(proj.id)
        except GcpError as exc:
            log.warning("listing VMs of project %s failed: %s", proj.id, exc)
            continue
        for inst in instances:
            rows.append(vm_summary_from_gcp(inst, session.project_name(proj.id)))
    rows.sort(key=lambda r: (r.folder.lower(), r.name.lower()))
    return rows


class GcpVmDetails:
    def __init__(self, instance: dict, disks: dict[str, dict], spec: VmSpec, machine: dict):
        self.instance = instance
        self.disks = disks
        self.spec = spec
        self.machine = machine
        ids = parse_instance_id(spec.moid)
        self.project_id = ids["project"]
        self.zone = ids["zone"]
        self.machine_type = _machine_short(instance)

    @property
    def disk_urls(self) -> list[str]:
        return [d.backing_file for d in self.spec.disks]

    def disk_doc(self, disk_url: str) -> dict:
        return self.disks.get(disk_url) or {}


def inspect_vm(session: GcpSession, vm_id: str, machine_cache: Optional[dict] = None) -> GcpVmDetails:
    client = session.client
    instance = client.get_instance(vm_id)
    disks: dict[str, dict] = {}
    boot_doc: Optional[dict] = None
    for ref in instance.get("disks") or []:
        if str(ref.get("type") or "").upper() == "SCRATCH":
            continue
        url = str(ref.get("source") or "")
        key = url.split("/compute/v1/", 1)[-1] if "/compute/v1/" in url else url
        try:
            doc = client.get_disk(url)
            disks[key] = doc
            if ref.get("boot") is True or (boot_doc is None and ref is (instance.get("disks") or [None])[0]):
                boot_doc = doc
        except GcpError as exc:
            log.warning("disk %s unreadable: %s", key.rsplit("/", 1)[-1], exc)
    if boot_doc is not None:
        disks["boot"] = boot_doc
    cache = machine_cache if machine_cache is not None else {}
    mt_url = str(instance.get("machineType") or "")
    if mt_url not in cache:
        try:
            cache[mt_url] = client.get_machine_type(mt_url)
        except GcpError:
            cache[mt_url] = {}
    machine = cache.get(mt_url) or {}
    spec = vm_spec_from_gcp(instance, disks, machine)
    if machine.get("guestCpus"):
        spec.num_cpu = int(machine["guestCpus"])
    if machine.get("memoryMb"):
        spec.memory_mb = int(machine["memoryMb"])
    return GcpVmDetails(instance, disks, spec, machine)
