"""Proxmox VE inventory: the VM list for the web UI and ``VmSpec`` extraction for a selected VM.

The guest OS is expressed as a vSphere-style ``guest_id`` plus a display name so
``oci.mapping.map_guest_os`` works unchanged. The QEMU guest agent names the installed OS when
it runs; otherwise the VM's ``ostype`` gives the family and the user confirms the release.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary
from helper_app.olvm.inventory import _from_guest_agent
from helper_app.proxmox.client import ProxmoxClient, ProxmoxError, PveVm
from helper_app.proxmox.config_parse import PveConfig, parse_config, valid_volid
from helper_app.proxmox.session import ProxmoxSession

POWER = {"running": "poweredOn", "stopped": "poweredOff", "paused": "suspended", "suspended": "suspended"}
SUPPORTED_FORMATS = ("raw", "qcow2", "vmdk")
LIST_WORKERS = 8

OSTYPE = {
    "l26": ("otherLinux64Guest", "Linux (64-bit)"),
    "l24": ("otherLinux64Guest", "Linux 2.4 (64-bit)"),
    # Proxmox groups Windows releases: win11 = 11/2022/2025, win10 = 10/2016/2019, win8 = 8/2012
    "win11": ("other64Guest", "Microsoft Windows"),
    "win10": ("other64Guest", "Microsoft Windows"),
    "win8": ("windows8Server64Guest", "Microsoft Windows Server 2012"),
    "win7": ("windows7Server64Guest", "Microsoft Windows Server 2008 R2"),
    "w2k8": ("windows7Server64Guest", "Microsoft Windows Server 2008"),
}


def power_state(status: str) -> str:
    return POWER.get(status, status or "unknown")


def guest_os(cfg: PveConfig, osinfo: dict) -> tuple[str, str, bool]:
    """``(guest_id, guest_full_name, reported_by_agent)``."""
    if osinfo:
        windows = str(osinfo.get("id") or "").lower() == "mswindows"
        name = str(osinfo.get("pretty-name") or osinfo.get("name") or "")
        if windows:
            name = name.replace("Microsoft ", "")
        reported = _from_guest_agent({
            "distribution": name if windows else str(osinfo.get("name") or name),
            "family": "windows" if windows else "linux",
            "version": {"full_version": "" if windows else str(osinfo.get("version-id") or "")},
        })
        if reported:
            return reported[0], reported[1], True
    gid, full = OSTYPE.get(cfg.ostype, ("otherLinux64Guest", "Linux (64-bit)"))
    return gid, full, False


def _nics(cfg: PveConfig, interfaces: list[dict]) -> list[NicSpec]:
    ips: dict[str, list[str]] = {}
    for iface in interfaces:
        mac = str(iface.get("hardware-address") or "").lower()
        addrs = [str(a.get("ip-address")) for a in iface.get("ip-addresses") or []
                 if isinstance(a, dict) and a.get("ip-address") and not str(a.get("ip-address")).startswith(
                     ("127.", "::1", "fe80"))]
        if mac:
            ips.setdefault(mac, []).extend(addrs)
    return [
        NicSpec(label=nic.key, adapter_type=nic.model, mac_address=nic.mac,
                network=nic.bridge + (f" (VLAN {nic.vlan})" if nic.vlan else ""), ip_addresses=ips.get(nic.mac, []))
        for nic in cfg.nics
    ]


def vm_spec_from_config(vm: PveVm, cfg: PveConfig, osinfo: dict | None = None,
                        interfaces: list[dict] | None = None) -> VmSpec:
    gid, full, _agent = guest_os(cfg, osinfo or {})
    disks = []
    for disk in cfg.data_disks:
        disks.append(DiskSpec(
            index=len(disks),
            label=disk.key,
            device_key=len(disks),
            capacity_bytes=disk.size_bytes,
            controller_type={"virtio": "virtio", "scsi": "scsi", "sata": "sata", "ide": "ide"}[disk.bus],
            controller_class="PveDisk",
            controller_bus=0,
            unit_number=disk.index,
            thin_provisioned=True,
            backing_file=disk.volume,
        ))
    return VmSpec(
        moid=str(vm.vmid),
        name=cfg.name or vm.name,
        instance_uuid=cfg.smbios_uuid,
        num_cpu=cfg.cpus,
        memory_mb=cfg.memory_mb,
        guest_id=gid,
        guest_full_name=full,
        firmware=Firmware.EFI if cfg.uefi else Firmware.BIOS,
        secure_boot=cfg.uefi and cfg.secure_boot_keys,
        power_state=power_state(vm.status),
        host_name=vm.node,
        has_vtpm=bool(cfg.tpmstate),
        disks=disks,
        nics=_nics(cfg, interfaces or []),
    )


@dataclass
class ProxmoxVmDetails:
    vm: PveVm
    config: PveConfig
    spec: VmSpec
    snapshots: list[str] = field(default_factory=list)
    agent_os: bool = False

    @property
    def volids(self) -> list[str]:
        return [d.volume for d in self.config.data_disks]

    @property
    def formats(self) -> list[str]:
        return [d.fmt for d in self.config.data_disks]


def parse_vmid(vm_id: str) -> int:
    try:
        vmid = int(str(vm_id).strip())
    except ValueError:
        raise ProxmoxError(f"invalid Proxmox VM id {vm_id!r}", status=404) from None
    if not 100 <= vmid <= 999_999_999:
        raise ProxmoxError(f"invalid Proxmox VM id {vm_id!r}", status=404)
    return vmid


def list_vm_summaries(session: ProxmoxSession) -> list[VmSummary]:
    client = session.client
    vms = client.cluster_vms()

    def row(vm: PveVm) -> VmSummary:
        try:
            cfg = parse_config(client.vm_config(vm.node, vm.vmid))
        except ProxmoxError:
            cfg = PveConfig(name=vm.name, cores=max(1, vm.maxcpu), memory_mb=max(1, vm.maxmem // (1 << 20)))
        spec = vm_spec_from_config(vm, cfg)
        return VmSummary(
            moid=spec.moid,
            name=spec.name,
            folder=vm.node,
            power_state=spec.power_state,
            guest_full_name=spec.guest_full_name,
            guest_id=spec.guest_id,
            num_cpu=spec.num_cpu,
            memory_mb=spec.memory_mb,
            num_disks=len(spec.disks),
            disk_capacity_bytes=sum(d.capacity_bytes for d in spec.disks),
            is_template=vm.template,
        )

    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
        rows = list(pool.map(row, vms))
    rows.sort(key=lambda r: r.name.lower())
    return rows


def inspect_vm(client: ProxmoxClient, vm_id: str) -> ProxmoxVmDetails:
    vm = client.find_vm(parse_vmid(vm_id))
    cfg = parse_config(client.vm_config(vm.node, vm.vmid))
    osinfo: dict = {}
    interfaces: list[dict] = []
    if vm.status == "running" and cfg.agent:
        osinfo = client.agent_osinfo(vm.node, vm.vmid)
        interfaces = client.agent_interfaces(vm.node, vm.vmid)
    spec = vm_spec_from_config(vm, cfg, osinfo, interfaces)
    try:
        snapshots = client.snapshots(vm.node, vm.vmid)
    except ProxmoxError:
        snapshots = []
    spec.has_snapshots = bool(snapshots)
    return ProxmoxVmDetails(vm=vm, config=cfg, spec=spec, snapshots=snapshots, agent_os=bool(osinfo))


def preflight(details: ProxmoxVmDetails) -> list[str]:
    problems: list[str] = []
    cfg = details.config
    if details.vm.status not in ("running", "stopped"):
        problems.append(f"VM is {details.vm.status or 'in an unknown state'}; start or stop it in Proxmox first")
    if cfg.lock:
        problems.append(f"VM is locked ({cfg.lock}); wait for the {cfg.lock} to finish or unlock it")
    if not cfg.data_disks:
        problems.append("VM has only CD-ROM drives, no disks" if cfg.disks else "VM has no disks")
    for disk in cfg.data_disks:
        if disk.is_passthrough:
            problems.append(f"disk {disk.key} is a passed-through host device ({disk.volume}); only "
                            "Proxmox storage volumes can be copied")
            continue
        if not valid_volid(disk.volume):
            problems.append(f"disk {disk.key} has an unsupported volume id {disk.volume!r}")
        if disk.fmt not in SUPPORTED_FORMATS:
            problems.append(f"disk {disk.key} uses the {disk.fmt} format; raw, qcow2 and vmdk are supported")
        if disk.size_bytes <= 0:
            problems.append(f"disk {disk.key} has no size in the VM config")
    return problems


def warnings(details: ProxmoxVmDetails) -> list[str]:
    notes = []
    cfg = details.config
    if not details.agent_os:
        notes.append("The QEMU guest agent did not report the guest OS; confirm the OS version on the migration "
                     "form")
    if details.vm.template:
        notes.append("This is a template: its disks are copied as they are")
    if details.spec.power_state == "poweredOn":
        notes.append("The VM is running: it is shut down right before the disk copy and stays stopped")
        if not cfg.agent:
            notes.append("The QEMU guest agent is not enabled: the shutdown is an ACPI request, then a hard stop "
                         "if the guest does not stop in time")
    if cfg.tpmstate:
        notes.append("The VM has a TPM state disk; its keys are not migrated (BitLocker or LUKS bound to the TPM "
                     "needs the recovery key in OCI)")
    if cfg.efidisk:
        notes.append("The EFI vars disk is not copied; OCI boots the VM with its own UEFI variables")
    if any(d.is_cdrom for d in cfg.disks):
        notes.append("CD-ROM drives are not migrated")
    if cfg.unused:
        notes.append(f"{len(cfg.unused)} unused disk(s) are not migrated")
    if details.snapshots:
        notes.append(f"The VM has {len(details.snapshots)} snapshot(s); the current state of each disk is copied")
    if cfg.hostpci:
        notes.append("PCI passthrough devices are not migrated")
    return notes
