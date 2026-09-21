"""Prepare a copied EC2 Linux guest for first boot in OCI (offline, on the helper VM)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from helper_app.branding import PREFIX
from helper_app.guest.initramfs import Skip

CLOUD_CFG_DROPIN = f"99-{PREFIX}-after-aws.cfg"
CLOUD_CFG_TEXT = """# added by the OCI Ultimate Migration Tool after migration from Amazon EC2
datasource_list: [ Oracle, OracleCloud, NoCloud, ConfigDrive, None ]
"""


class AwsCloudFixer:
    def __init__(self, mnt: Path, note: Callable[[str], None]):
        self.mnt = mnt
        self.note = note
        self.changes: list[str] = []

    def apply(self) -> tuple[str, str]:
        if not (self.mnt / "etc").is_dir():
            raise Skip("no /etc on the guest root (not a Linux layout?)")
        self.disable_ec2_units()
        self.remove_ec2_cloud_cfg()
        self.write_oci_datasource()
        if not self.changes:
            return "not_needed", "no EC2-specific boot configuration found to adjust"
        return "done", "; ".join(self.changes)

    def disable_ec2_units(self) -> None:
        systemd = self.mnt / "etc" / "systemd" / "system"
        if not systemd.is_dir():
            return
        fragments = ("amazon-ssm-agent", "amazon-ssm", "ec2-instance-connect", "awsagent", "amazon-cloudwatch")
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
            self.changes.append(f"disabled {removed} Amazon/EC2 agent systemd link(s)")

    def remove_ec2_cloud_cfg(self) -> None:
        cfg_dir = self.mnt / "etc" / "cloud" / "cloud.cfg.d"
        if not cfg_dir.is_dir():
            return
        for path in sorted(cfg_dir.iterdir()):
            if not path.is_file():
                continue
            low = path.name.lower()
            if any(tok in low for tok in ("amazon", "ec2", "aws")):
                path.unlink()
                self.note(f"removed cloud-init drop-in {path.name}")
                self.changes.append(f"removed {path.name}")

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
        self.changes.append(f"wrote {path.relative_to(self.mnt)}")
        self.note(f"cloud-init prefers OCI metadata ({path.name})")
