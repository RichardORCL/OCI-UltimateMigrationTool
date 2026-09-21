"""Azure post-copy guest fix-up on a fake mounted root."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helper_app.guest.azure_cloud import AzureCloudFixer, CLOUD_CFG_DROPIN


def _layout(root: Path) -> None:
    (root / "etc" / "cloud" / "cloud.cfg.d").mkdir(parents=True)
    (root / "etc" / "cloud" / "cloud.cfg.d" / "90-azure.cfg").write_text("datasource_list: [ Azure ]\n")
    (root / "etc" / "fstab").write_text("/dev/sda1 / ext4 defaults 0 1\n/dev/sr0 /mnt/cdrom iso9660 ro 0 0\n")
    wants = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "walinuxagent.service").write_text("stub")
    (root / "usr" / "lib" / "systemd" / "system").mkdir(parents=True)
    (root / "usr/lib/systemd/system/serial-getty@.service").write_text("[Unit]\nDescription=Serial Getty\n")


@pytest.mark.skipif(os.name == "nt", reason="serial-getty enable uses symlinks (Linux helper VM)")
def test_azure_cloud_fixer_adjusts_guest(tmp_path):
    root = tmp_path / "mnt"
    root.mkdir()
    _layout(root)
    notes: list[str] = []
    status, detail = AzureCloudFixer(root, notes.append).apply()
    assert status == "done"
    assert "removed" in detail.lower() or "90-azure" in detail
    assert not (root / "etc/cloud/cloud.cfg.d/90-azure.cfg").exists()
    assert (root / "etc/cloud/cloud.cfg.d" / CLOUD_CFG_DROPIN).exists()
    fstab = (root / "etc/fstab").read_text()
    assert "sr0" in fstab and fstab.strip().splitlines()[1].startswith("# oci-umt")
    assert not (root / "etc/systemd/system/multi-user.target.wants/walinuxagent.service").exists()
    serial = root / "etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"
    assert serial.is_symlink()
