"""AWS post-copy guest fix-up on a fake mounted root."""

from __future__ import annotations

from pathlib import Path

from helper_app.guest.aws_cloud import CLOUD_CFG_DROPIN, AwsCloudFixer


def _layout(root: Path) -> None:
    (root / "etc" / "cloud" / "cloud.cfg.d").mkdir(parents=True)
    (root / "etc" / "cloud" / "cloud.cfg.d" / "90-amazon.cfg").write_text("datasource_list: [ Ec2 ]\n")
    wants = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "amazon-ssm-agent.service").write_text("stub")


def test_aws_cloud_fixer_adjusts_guest(tmp_path):
    root = tmp_path / "mnt"
    root.mkdir()
    _layout(root)
    notes: list[str] = []
    status, detail = AwsCloudFixer(root, notes.append).apply()
    assert status == "done"
    assert not (root / "etc/cloud/cloud.cfg.d/90-amazon.cfg").exists()
    assert (root / "etc/cloud/cloud.cfg.d" / CLOUD_CFG_DROPIN).exists()
    assert not (root / "etc/systemd/system/multi-user.target.wants/amazon-ssm-agent.service").exists()
