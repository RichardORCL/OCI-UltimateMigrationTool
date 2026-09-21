"""Prepare a copied Azure Linux guest for first boot in OCI (offline, on the helper VM).

Azure images ship cloud-init Azure datasource snippets, walinuxagent, and sometimes ``/dev/sr0`` in
``fstab``.  In OCI those cause long metadata timeouts and serial-console boot stalls.  This step runs
on the mounted boot volume after the disk copy, like the network and initramfs fix-ups.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from helper_app.branding import PREFIX
from helper_app.guest.initramfs import Skip

CLOUD_CFG_DROPIN = f"99-{PREFIX}-after-azure.cfg"
CLOUD_CFG_TEXT = """# added by the OCI Ultimate Migration Tool after migration from Azure
# Prefer OCI metadata; do not wait on the Azure instance metadata service.
datasource_list: [ Oracle, OracleCloud, NoCloud, ConfigDrive, None ]
"""

WantsScan = tuple[str, ...]  # unit name fragments under *.wants


class AzureCloudFixer:
    def __init__(self, mnt: Path, note: Callable[[str], None]):
        self.mnt = mnt
        self.note = note
        self.changes: list[str] = []

    def apply(self) -> tuple[str, str]:
        if not (self.mnt / "etc").is_dir():
            raise Skip("no /etc on the guest root (not a Linux layout?)")
        self.remove_azure_cloud_cfg()
        self.fix_fstab_sr0()
        self.disable_waagent_units()
        self.enable_serial_console()
        self.write_oci_datasource()
        if not self.changes:
            return "not_needed", "no Azure-specific boot configuration found to adjust"
        return "done", "; ".join(self.changes)

    def remove_azure_cloud_cfg(self) -> None:
        cfg_dir = self.mnt / "etc" / "cloud" / "cloud.cfg.d"
        if not cfg_dir.is_dir():
            return
        for path in sorted(cfg_dir.iterdir()):
            if not path.is_file():
                continue
            low = path.name.lower()
            if "azure" in low or "walinux" in low or "waagent" in low:
                path.unlink()
                self.note(f"removed cloud-init drop-in {path.name}")
                self.changes.append(f"removed {path.name}")

    def fix_fstab_sr0(self) -> None:
        fstab = self.mnt / "etc" / "fstab"
        if not fstab.is_file():
            return
        lines = fstab.read_text(errors="replace").splitlines()
        out: list[str] = []
        changed = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                low = stripped.lower()
                if "sr0" in low or "/dev/sr" in low or ("cdrom" in low and "iso9660" in low):
                    out.append(f"# {PREFIX}: disabled after Azure migration — {line}")
                    changed = True
                    continue
            out.append(line)
        if changed:
            fstab.write_text("\n".join(out) + ("\n" if out else ""))
            self.note("commented out /dev/sr0 (or CD-ROM) entries in /etc/fstab")
            self.changes.append("fstab sr0 entries disabled")

    def disable_waagent_units(self) -> None:
        systemd = self.mnt / "etc" / "systemd" / "system"
        if not systemd.is_dir():
            return
        fragments = ("walinuxagent", "waagent", "azure-agent", "azuremonitor")
        removed = 0
        for wants in systemd.glob("*.wants"):
            if not wants.is_dir():
                continue
            for link in wants.iterdir():
                if not link.is_symlink() and not link.is_file():
                    continue
                name = link.name.lower()
                if any(f in name for f in fragments):
                    link.unlink()
                    removed += 1
                    self.note(f"disabled systemd unit link {wants.name}/{link.name}")
        if removed:
            self.changes.append(f"disabled {removed} Azure agent systemd link(s)")

    def enable_serial_console(self) -> None:
        template = self._first_existing(
            "lib/systemd/system/serial-getty@.service",
            "usr/lib/systemd/system/serial-getty@.service",
        )
        if template is None:
            self.note("serial-getty@.service not found (skip enabling OCI serial console login)")
            return
        wants = self.mnt / "etc" / "systemd" / "system" / "getty.target.wants"
        link = wants / "serial-getty@ttyS0.service"
        if link.is_symlink() or link.is_file():
            self.note("serial-getty@ttyS0 already enabled")
            return
        wants.mkdir(parents=True, exist_ok=True)
        target = "/" + template.relative_to(self.mnt).as_posix()
        if os.path.lexists(link):
            link.unlink()
        os.symlink(target, link)
        self.note("enabled serial-getty@ttyS0 for the OCI serial console")
        self.changes.append("serial login on ttyS0 enabled")

    def write_oci_datasource(self) -> None:
        cfg_dir = self.mnt / "etc" / "cloud" / "cloud.cfg.d"
        path = cfg_dir / CLOUD_CFG_DROPIN
        if path.exists():
            self.note(f"{CLOUD_CFG_DROPIN} already present")
            return
        if not cfg_dir.is_dir() and not (self.mnt / "etc" / "cloud").is_dir():
            self.note("cloud-init not installed (no /etc/cloud)")
            return
        cfg_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(CLOUD_CFG_TEXT)
        os.chmod(path, 0o644)
        self.note(f"wrote {CLOUD_CFG_DROPIN} (OCI cloud-init datasource first)")
        self.changes.append("OCI cloud-init datasource preference added")

    def _first_existing(self, *rels: str) -> Optional[Path]:
        for rel in rels:
            p = self.mnt / rel
            if p.is_file():
                return p
        return None
