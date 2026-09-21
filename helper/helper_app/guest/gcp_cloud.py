"""Prepare a copied GCP Linux guest for first boot in OCI (offline, on the helper VM)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from helper_app.branding import PREFIX
from helper_app.guest.initramfs import Skip

CLOUD_CFG_DROPIN = f"99-{PREFIX}-after-gcp.cfg"
CLOUD_CFG_TEXT = """# added by the OCI Ultimate Migration Tool after migration from Google Cloud
datasource_list: [ Oracle, OracleCloud, NoCloud, ConfigDrive, None ]
"""


class GcpCloudFixer:
    def __init__(self, mnt: Path, note: Callable[[str], None]):
        self.mnt = mnt
        self.note = note
        self.changes: list[str] = []

    def apply(self) -> tuple[str, str]:
        if not (self.mnt / "etc").is_dir():
            raise Skip("no /etc on the guest root (not a Linux layout?)")
        self.disable_gce_units()
        self.remove_gce_cloud_cfg()
        self.write_oci_datasource()
        if not self.changes:
            return "skipped", "no GCP-specific configuration found"
        return "done", "; ".join(self.changes)

    def disable_gce_units(self) -> None:
        wants = self.mnt / "etc/systemd/system/multi-user.target.wants"
        if not wants.is_dir():
            return
        for path in wants.iterdir():
            name = path.name.lower()
            if "google" in name or "gce" in name:
                path.unlink(missing_ok=True)
                self.changes.append(f"disabled {path.name}")
                self.note(f"removed systemd want {path.name}")

    def remove_gce_cloud_cfg(self) -> None:
        drop = self.mnt / "etc/cloud/cloud.cfg.d"
        if not drop.is_dir():
            return
        for path in drop.glob("*google*"):
            path.unlink(missing_ok=True)
            self.changes.append(f"removed {path.name}")

    def write_oci_datasource(self) -> None:
        drop = self.mnt / "etc/cloud/cloud.cfg.d"
        drop.mkdir(parents=True, exist_ok=True)
        path = drop / CLOUD_CFG_DROPIN
        if path.exists() and path.read_text(encoding="utf-8") == CLOUD_CFG_TEXT:
            return
        path.write_text(CLOUD_CFG_TEXT, encoding="utf-8")
        self.changes.append(f"wrote {path.relative_to(self.mnt)}")
        self.note(f"cloud-init prefers OCI metadata ({path.name})")
