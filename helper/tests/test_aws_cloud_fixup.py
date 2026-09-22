"""AWS post-copy guest fix-up on a fake mounted root."""

from __future__ import annotations

from pathlib import Path

from helper_app.guest.aws_cloud import CLOUD_CFG_DROPIN, EARLYCON_ARG, AwsCloudFixer, adjust_kernel_args

AL2023_ARGS = ("root=UUID=abc ro console=tty0 console=ttyS0,115200n8 nvme_core.io_timeout=4294967295 "
               "rd.emergency=poweroff rd.shell=0 selinux=1 security=selinux quiet")


def _layout(root: Path) -> None:
    (root / "etc" / "cloud" / "cloud.cfg.d").mkdir(parents=True)
    (root / "etc" / "cloud" / "cloud.cfg.d" / "90-amazon.cfg").write_text("datasource_list: [ Ec2 ]\n")
    wants = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "amazon-ssm-agent.service").write_text("stub")


def _grubenv(kernelopts: str) -> bytes:
    body = f"# GRUB Environment Block\nkernelopts={kernelopts}\nboot_success=1\n"
    return (body + "#" * (1024 - len(body))).encode()


def _boot_layout(root: Path) -> None:
    entries = root / "boot" / "loader" / "entries"
    entries.mkdir(parents=True)
    (entries / "m-6.18.48-109.150.amzn2023.x86_64.conf").write_text(
        "title Amazon Linux (6.18.48-109.150.amzn2023.x86_64) 2023\n"
        "version 6.18.48-109.150.amzn2023.x86_64\n"
        "linux /boot/vmlinuz-6.18.48-109.150.amzn2023.x86_64\n"
        "initrd /boot/initramfs-6.18.48-109.150.amzn2023.x86_64.img\n"
        f"options {AL2023_ARGS}\n"
        "grub_users $grub_users\n")
    (root / "boot" / "grub2").mkdir()
    (root / "boot" / "grub2" / "grubenv").write_bytes(_grubenv(AL2023_ARGS))
    (root / "boot" / "grub2" / "grub.cfg").write_text(
        "menuentry 'x' {\n"
        f"\tlinuxefi /boot/vmlinuz-6.18 {AL2023_ARGS}\n"
        "\tinitrdefi /boot/initramfs-6.18.img\n"
        "}\n")
    (root / "etc" / "default").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "default" / "grub").write_text(
        'GRUB_TIMEOUT=0\n'
        'GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8 rd.emergency=poweroff rd.shell=0 quiet"\n'
        'GRUB_DISABLE_RECOVERY="true"\n')


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


def test_adjust_kernel_args_makes_boot_visible():
    new, changed = adjust_kernel_args(AL2023_ARGS)
    assert changed
    tokens = new.split()
    for gone in ("quiet", "rd.shell=0", "rd.emergency=poweroff"):
        assert gone not in tokens
    assert EARLYCON_ARG in tokens
    # everything else is kept, in order
    assert tokens[:4] == ["root=UUID=abc", "ro", "console=tty0", "console=ttyS0,115200n8"]
    assert "selinux=1" in tokens and "security=selinux" in tokens


def test_adjust_kernel_args_is_idempotent_and_keeps_variables():
    once, _ = adjust_kernel_args(AL2023_ARGS)
    twice, changed = adjust_kernel_args(once)
    assert twice == once and not changed
    new, changed = adjust_kernel_args("$kernelopts")
    assert changed and new.split() == ["$kernelopts", EARLYCON_ARG]


def test_aws_cloud_fixer_rewrites_kernel_options_everywhere(tmp_path):
    root = tmp_path / "mnt"
    root.mkdir()
    _layout(root)
    _boot_layout(root)
    notes: list[str] = []
    status, detail = AwsCloudFixer(root, notes.append).apply()
    assert status == "done"
    assert "kernel boot options" in detail

    entry = (root / "boot/loader/entries/m-6.18.48-109.150.amzn2023.x86_64.conf").read_text()
    opts = next(ln for ln in entry.splitlines() if ln.startswith("options "))
    assert "quiet" not in opts.split() and "rd.emergency=poweroff" not in opts and EARLYCON_ARG in opts
    assert "title Amazon Linux" in entry and "grub_users $grub_users" in entry

    env = (root / "boot/grub2/grubenv").read_bytes()
    assert len(env) == 1024 and env.endswith(b"#")
    kernelopts = next(ln for ln in env.decode().splitlines() if ln.startswith("kernelopts="))
    assert "quiet" not in kernelopts.split() and EARLYCON_ARG in kernelopts
    assert "boot_success=1" in env.decode()

    cfg = (root / "boot/grub2/grub.cfg").read_text()
    linux = next(ln for ln in cfg.splitlines() if ln.strip().startswith("linuxefi"))
    assert linux.startswith("\tlinuxefi /boot/vmlinuz-6.18 ") and "quiet" not in linux.split()
    assert "initrdefi /boot/initramfs-6.18.img" in cfg

    default = (root / "etc/default/grub").read_text()
    assert 'GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8 ' + EARLYCON_ARG + '"\n' in default
    assert "GRUB_TIMEOUT=0\n" in default and 'GRUB_DISABLE_RECOVERY="true"\n' in default


def test_aws_cloud_fixer_kernel_options_second_run_is_noop(tmp_path):
    root = tmp_path / "mnt"
    root.mkdir()
    _layout(root)
    _boot_layout(root)
    AwsCloudFixer(root, lambda _m: None).apply()
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    status, detail = AwsCloudFixer(root, lambda _m: None).apply()
    assert status == "not_needed"
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_aws_cloud_fixer_leaves_oversized_grubenv_alone(tmp_path):
    root = tmp_path / "mnt"
    root.mkdir()
    _layout(root)
    (root / "boot" / "grub2").mkdir(parents=True)
    # 1016 bytes of content: fits before the edit, not after (+28 bytes: quiet out, earlycon in)
    huge = "kernelopts=" + "x=" + "y" * 971 + " quiet"
    body = f"# GRUB Environment Block\n{huge}\n"
    (root / "boot" / "grub2" / "grubenv").write_bytes((body + "#" * (1024 - len(body))).encode())
    notes: list[str] = []
    AwsCloudFixer(root, notes.append).apply()
    env = (root / "boot/grub2/grubenv").read_bytes()
    assert len(env) == 1024 and b" quiet" in env
    assert any("grubenv" in n and "1024" in n for n in notes)
