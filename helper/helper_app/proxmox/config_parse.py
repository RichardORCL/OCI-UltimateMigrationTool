"""Parse a Proxmox QEMU VM config (``GET /nodes/<node>/qemu/<vmid>/config``) into disks, NICs and
firmware.

Drive lines look like ``scsi0: local-lvm:vm-101-disk-0,iothread=1,size=32G``; NIC lines like
``net0: virtio=BC:24:11:AA:BB:CC,bridge=vmbr0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

BUSES = ("scsi", "virtio", "sata", "ide")
DRIVE_KEY = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
NET_KEY = re.compile(r"^net(\d+)$")
NIC_MODELS = ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "pcnet", "i82551", "i82557b",
              "i82559er", "ne2k_isa")
# storage:volume, for example local-lvm:vm-101-disk-0 or local:101/vm-101-disk-0.qcow2
VOLID = re.compile(r"^[A-Za-z][A-Za-z0-9\-_.]*:[A-Za-z0-9][A-Za-z0-9\-_.+/=]*$")
UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


@dataclass
class PveDisk:
    key: str  # scsi0, virtio1, ...
    bus: str
    index: int
    volume: str  # volid (storage:volume), a /dev path (passthrough) or "none"
    size_bytes: int = 0
    media: str = "disk"  # disk | cdrom
    options: dict[str, str] = field(default_factory=dict)

    @property
    def is_cdrom(self) -> bool:
        return self.media == "cdrom"

    @property
    def is_passthrough(self) -> bool:
        return self.volume.startswith("/")

    @property
    def storage(self) -> str:
        return self.volume.split(":", 1)[0] if ":" in self.volume else ""

    @property
    def fmt(self) -> str:
        """Image format from the volume name; block storages (LVM, ZFS, RBD) are always raw."""
        name = self.volume.rsplit("/", 1)[-1].lower()
        for ext in ("qcow2", "vmdk", "raw"):
            if name.endswith("." + ext):
                return ext
        return self.options.get("format", "raw")


@dataclass
class PveNic:
    key: str
    model: str
    mac: str = ""
    bridge: str = ""
    vlan: str = ""


@dataclass
class PveConfig:
    name: str = ""
    ostype: str = "other"
    bios: str = "seabios"
    cores: int = 1
    sockets: int = 1
    vcpus: int = 0
    memory_mb: int = 512
    machine: str = ""
    smbios_uuid: str = ""
    agent: bool = False
    template: bool = False
    lock: str = ""
    boot_order: list[str] = field(default_factory=list)
    disks: list[PveDisk] = field(default_factory=list)  # data disks and CD-ROMs
    nics: list[PveNic] = field(default_factory=list)
    efidisk: str = ""
    tpmstate: str = ""
    secure_boot_keys: bool = False
    hostpci: list[str] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)

    @property
    def uefi(self) -> bool:
        return self.bios == "ovmf"

    @property
    def cpus(self) -> int:
        return max(1, self.vcpus or self.cores * self.sockets)

    @property
    def data_disks(self) -> list[PveDisk]:
        """Disks to copy, boot disk first, then in bus order."""
        disks = [d for d in self.disks if not d.is_cdrom]
        boot = next((k for k in self.boot_order if any(d.key == k for d in disks)), "")
        disks.sort(key=lambda d: (d.key != boot, BUSES.index(d.bus), d.index))
        return disks


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", (text or "").upper())
    if not match:
        return 0
    return int(float(match.group(1)) * UNITS[match.group(2)])


def split_options(value: str) -> tuple[str, dict[str, str]]:
    """``("first", {key: value})`` from ``first,key=value,flag``. The first item may itself be ``k=v``."""
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if not parts:
        return "", {}
    opts: dict[str, str] = {}
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        opts[key.strip()] = val.strip() if sep else "1"
    return parts[0], opts


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _boot_order(cfg: dict) -> list[str]:
    boot = str(cfg.get("boot") or "")
    if boot.startswith("order="):
        return [k for k in boot[len("order="):].split(";") if k]
    # legacy "boot: cdn" plus "bootdisk: scsi0"
    return [str(cfg["bootdisk"])] if cfg.get("bootdisk") else []


def _nic(key: str, value: str) -> PveNic:
    first, opts = split_options(value)
    model, _, mac = first.partition("=")
    model = model.strip()
    if model not in NIC_MODELS and "model" in opts:
        model, mac = opts["model"], opts.get("macaddr", "")
    return PveNic(key=key, model=model or "virtio", mac=mac.strip().lower(), bridge=opts.get("bridge", ""),
                  vlan=opts.get("tag", ""))


def parse_config(cfg: dict) -> PveConfig:
    out = PveConfig(
        name=str(cfg.get("name") or ""),
        ostype=str(cfg.get("ostype") or "other"),
        bios=str(cfg.get("bios") or "seabios"),
        cores=max(1, _int(cfg.get("cores"), 1)),
        sockets=max(1, _int(cfg.get("sockets"), 1)),
        vcpus=max(0, _int(cfg.get("vcpus"), 0)),
        memory_mb=max(16, _int(split_options(str(cfg.get("memory") or "512"))[0].replace("current=", ""), 512)),
        machine=str(cfg.get("machine") or ""),
        template=_int(cfg.get("template"), 0) == 1,
        lock=str(cfg.get("lock") or ""),
        boot_order=_boot_order(cfg),
    )
    agent_first, agent_opts = split_options(str(cfg.get("agent") or "0"))
    out.agent = agent_first in ("1", "enabled=1") or agent_opts.get("enabled") == "1"
    _first, smbios = split_options("x," + str(cfg.get("smbios1") or ""))
    out.smbios_uuid = smbios.get("uuid", "")
    for key, value in cfg.items():
        key = str(key)
        match = DRIVE_KEY.match(key)
        if match:
            volume, opts = split_options(str(value))
            out.disks.append(PveDisk(
                key=key, bus=match.group(1), index=int(match.group(2)), volume=volume,
                size_bytes=parse_size(opts.get("size", "")), media=opts.get("media", "disk"), options=opts,
            ))
            continue
        if NET_KEY.match(key):
            out.nics.append(_nic(key, str(value)))
        elif key == "efidisk0":
            volume, opts = split_options(str(value))
            out.efidisk = volume
            out.secure_boot_keys = opts.get("pre-enrolled-keys") == "1"
        elif key == "tpmstate0":
            out.tpmstate = split_options(str(value))[0]
        elif key.startswith("hostpci"):
            out.hostpci.append(split_options(str(value))[0])
        elif key.startswith("unused"):
            out.unused.append(str(value))
    out.disks.sort(key=lambda d: (BUSES.index(d.bus), d.index))
    out.nics.sort(key=lambda n: _int(n.key[3:], 0))
    return out


def valid_volid(volid: str) -> bool:
    return bool(VOLID.match(volid or "")) and ".." not in volid
