"""Guest initramfs fix-up against a scripted shell: no block devices, LVM or chroot involved."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helper_app.guest.initramfs import (
    DRACUT_CONF_NAME,
    VIRTIO_DRIVERS,
    CmdResult,
    InitramfsFixer,
    _Session,
)

RHEL_KERNEL = "3.10.0-1160.el7.x86_64"
OLD_KERNEL = "3.10.0-1127.el7.x86_64"


class FakeShell:
    """Answers the commands the fixer runs.  ``mount`` materialises the guest tree under the mount point
    so the fixer's file checks work on a real (temporary) directory."""

    def __init__(self, layout: str = "lvm", boot_fstab="UUID=boot-uuid", virtio_in=(), tools_running=True,
                 lvm_foreign_vgs=("ocivolume",), dracut=True, dracut_rc=0, fail_mount=(), old_dracut=False,
                 udev_stale=False, lsblk_hides_lvs=False, blkid_fails=False, partial_virtio=(),
                 modules_missing=(), builtin=(), os_release=None):
        self.layout = layout
        self.partial_virtio = set(partial_virtio)  # kernels whose initramfs has virtio_blk/net but no virtio_scsi
        # driver names absent from the guest's /lib/modules: a set (all kernels) or {kernel: set}
        self.modules_missing = modules_missing if isinstance(modules_missing, dict) else set(modules_missing)
        self.builtin = set(builtin)  # driver names listed in modules.builtin instead of shipped as .ko
        self.os_release = os_release  # override the guest's /etc/os-release PRETTY_NAME
        self.udev_stale = udev_stale  # lsblk shows the LVs but without FSTYPE (udev has not probed them)
        self.lsblk_hides_lvs = lsblk_hides_lvs  # lsblk does not list the LVs at all
        self.blkid_fails = blkid_fails  # blkid cannot classify LVs (seen on some Oracle Linux imports)
        self.boot_fstab = boot_fstab
        self.virtio_in = set(virtio_in)  # kernels whose initramfs already has virtio
        self.foreign = lvm_foreign_vgs
        self.dracut = dracut
        self.dracut_rc = dracut_rc
        self.fail_mount = set(fail_mount)
        self.old_dracut = old_dracut
        self.calls: list[list[str]] = []
        self.active_vgs: list[str] = []
        self.mounted: dict[str, str] = {}  # mountpoint -> device
        self.rebuilt: list[str] = []

    # -- the disk as lsblk would show it
    def nodes(self):
        disk = {"name": "/dev/sdb", "type": "disk", "fstype": None, "uuid": None, "label": None, "children": []}
        if self.layout == "lvm":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "LVM2_member", "uuid": "pv-uuid", "label": None,
                 "children": [
                     {"name": "/dev/mapper/rhel-root", "type": "lvm", "fstype": None if self.udev_stale else "xfs",
                      "uuid": None if self.udev_stale else "root-uuid"},
                     {"name": "/dev/mapper/rhel-swap", "type": "lvm", "fstype": None if self.udev_stale else "swap",
                      "uuid": None if self.udev_stale else "swap-uuid"},
                 ] if "rhel" in self.active_vgs and not self.lsblk_hides_lvs else []},
            ]
        elif self.layout == "ol_lvm":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "vfat", "uuid": "efi-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": "BOOT"},
                {"name": "/dev/sdb3", "type": "part", "fstype": "LVM2_member", "uuid": "pv-uuid", "label": None,
                 "children": [
                     {"name": "/dev/mapper/ol-root", "type": "lvm", "fstype": None if self.udev_stale else "xfs",
                      "uuid": None if self.udev_stale else "root-uuid"},
                     {"name": "/dev/mapper/ol-swap", "type": "lvm", "fstype": None if self.udev_stale else "swap",
                      "uuid": None if self.udev_stale else "swap-uuid"},
                 ] if "ol" in self.active_vgs and not self.lsblk_hides_lvs else []},
            ]
        elif self.layout == "rhel_split_usr":  # RHEL 8/9: / on rootlv, /usr (modules, dracut) on usrlv
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "LVM2_member", "uuid": "pv-uuid", "label": None,
                 "children": [
                     {"name": "/dev/mapper/rootvg-rootlv", "type": "lvm", "fstype": "xfs", "uuid": "root-uuid"},
                     {"name": "/dev/mapper/rootvg-usrlv", "type": "lvm", "fstype": "xfs", "uuid": "usr-uuid"},
                 ] if "rootvg" in self.active_vgs else []},
            ]
        elif self.layout == "plain":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "ext4", "uuid": "boot-uuid", "label": "boot",
                 "partuuid": "boot-partuuid"},
                {"name": "/dev/sdb2", "type": "part", "fstype": "ext4", "uuid": "root-uuid", "label": None},
            ]
        elif self.layout == "ubuntu":  # subiquity: EFI, /boot, LVM ubuntu-vg/ubuntu-lv, no dracut, netplan
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "vfat", "uuid": "AAAA-BBBB", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "ext4", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb3", "type": "part", "fstype": "LVM2_member", "uuid": "pv-uuid", "label": None,
                 "children": [
                     {"name": "/dev/mapper/ubuntu--vg-ubuntu--lv", "type": "lvm", "fstype": "ext4", "uuid": "root-uuid"},
                 ] if "ubuntu-vg" in self.active_vgs else []},
            ]
        elif self.layout == "luks":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "crypto_LUKS", "uuid": "luks-uuid", "label": None},
            ]
        elif self.layout == "windows":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "ntfs", "uuid": "0000-1111", "label": "System"},
            ]
        return {"blockdevices": [disk]}

    # -- guest file trees materialised on mount
    def fill_root(self, mnt: Path):
        (mnt / "etc").mkdir(parents=True, exist_ok=True)
        if self.layout == "ubuntu":
            (mnt / "etc" / "fstab").write_text(
                "/dev/disk/by-id/dm-uuid-LVM-abc / ext4 defaults 0 1\n"
                f"{self.boot_fstab} /boot ext4 defaults 0 1\n"
                "/dev/disk/by-uuid/AAAA-BBBB /boot/efi vfat defaults 0 1\n")
            (mnt / "lib" / "modules" / "5.15.0-91-generic").mkdir(parents=True)  # (/lib -> usr/lib on the real thing)
            (mnt / "lib" / "modules" / "5.15.0-91-generic" / "modules.dep").write_text("")
            (mnt / "etc" / "os-release").write_text('PRETTY_NAME="Ubuntu 22.04.3 LTS"\n')
            (mnt / "etc" / "netplan").mkdir()
            (mnt / "etc" / "netplan" / "00-installer-config.yaml").write_text(
                "network:\n  ethernets:\n    ens33:\n      dhcp4: false\n      addresses: [192.168.99.51/24]\n  version: 2\n")
            (mnt / "usr" / "lib" / "systemd" / "system").mkdir(parents=True)
            (mnt / "usr" / "lib" / "systemd" / "systemd").write_text("")
            (mnt / "etc" / "systemd" / "system" / "multi-user.target.wants").mkdir(parents=True)
            return
        (mnt / "etc" / "fstab").write_text(
            "# guest fstab\n"
            f"{'/dev/mapper/ol-root' if self.layout == 'ol_lvm' else '/dev/mapper/rhel-root' if self.layout == 'lvm' else 'UUID=root-uuid'} / xfs defaults 0 0\n"
            + (f"{self.boot_fstab} /boot xfs defaults 0 0\n" if self.boot_fstab else "")
            + ("/dev/mapper/ol-swap swap swap defaults 0 0\n" if self.layout == "ol_lvm"
               else "/dev/mapper/rhel-swap swap swap defaults 0 0\n" if self.layout == "lvm" else ""))
        for ver in (RHEL_KERNEL, OLD_KERNEL):
            self.fill_modules(mnt / "lib" / "modules" / ver)
        (mnt / "lib" / "modules" / "3.10.0-957.el7.x86_64").mkdir()  # removed kernel: no modules.dep
        if self.dracut:
            (mnt / "usr" / "bin").mkdir(parents=True, exist_ok=True)
            (mnt / "usr" / "bin" / "dracut").write_text("#!/bin/bash\n")
        if not self.boot_fstab:  # /boot on the root fs
            self.fill_boot(mnt / "boot")
        # a RHEL 7 style network stack: NetworkManager enabled, profile bound to ens192
        pretty = self.os_release or "Red Hat Enterprise Linux Server 7.9 (Maipo)"
        (mnt / "etc" / "os-release").write_text(f'PRETTY_NAME="{pretty}"\n')
        (mnt / "usr" / "lib" / "systemd" / "system").mkdir(parents=True, exist_ok=True)
        (mnt / "usr" / "lib" / "systemd" / "system" / "NetworkManager.service").write_text("[Unit]")
        (mnt / "etc" / "systemd" / "system" / "multi-user.target.wants").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "systemd" / "system" / "multi-user.target.wants" / "NetworkManager.service").write_text("")
        (mnt / "etc" / "NetworkManager" / "system-connections").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "NetworkManager" / "system-connections" / "ens192.nmconnection").write_text(
            "[connection]\ntype=ethernet\ninterface-name=ens192\n[ipv4]\nmethod=manual\n")

    def fill_split_root(self, mnt: Path, usr_spec="/dev/mapper/rootvg-usrlv"):
        """RHEL-style root LV: /etc/fstab on / but kernel modules live on a separate /usr LV."""
        (mnt / "etc").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "fstab").write_text(
            "/dev/mapper/rootvg-rootlv / xfs defaults 0 0\n"
            f"{self.boot_fstab} /boot xfs defaults 0 0\n"
            f"{usr_spec} /usr xfs defaults 0 0\n")
        (mnt / "usr").mkdir(exist_ok=True)  # mount point until usrlv is mounted
        (mnt / "etc" / "os-release").write_text('PRETTY_NAME="Red Hat Enterprise Linux 9.4 (Plow)"\n')
        (mnt / "etc" / "NetworkManager" / "system-connections").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "NetworkManager" / "system-connections" / "ens192.nmconnection").write_text(
            "[connection]\ntype=ethernet\ninterface-name=ens192\n[ipv4]\nmethod=manual\n")

    def fill_modules(self, mod_dir: Path):
        """/lib/modules/<ver> as a distro kernel package installs it (compressed .ko files, modules.builtin)."""
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "modules.dep").write_text("")
        shipped = {"virtio_pci": "kernel/drivers/virtio", "virtio_scsi": "kernel/drivers/scsi",
                   "virtio_blk": "kernel/drivers/block", "virtio_net": "kernel/drivers/net",
                   "vmw_pvscsi": "kernel/drivers/scsi"}
        for name, sub in shipped.items():
            if name in self.missing_for(mod_dir.name) or name in self.builtin:
                continue
            (mod_dir / sub).mkdir(parents=True, exist_ok=True)
            (mod_dir / sub / f"{name}.ko.xz").write_text("elf")
        (mod_dir / "modules.builtin").write_text(
            "kernel/drivers/virtio/virtio.ko\nkernel/drivers/virtio/virtio_ring.ko\n"
            + "".join(f"kernel/drivers/{n}.ko\n" for n in self.builtin))

    def missing_for(self, ver: str) -> set:
        if isinstance(self.modules_missing, dict):
            return set(self.modules_missing.get(ver, ()))
        return self.modules_missing

    def fill_usr(self, usr: Path):
        for ver in (RHEL_KERNEL, OLD_KERNEL):
            self.fill_modules(usr / "lib" / "modules" / ver)
        if self.dracut:
            (usr / "bin").mkdir(parents=True, exist_ok=True)
            (usr / "bin" / "dracut").write_text("#!/bin/bash\n")
        (usr / "lib" / "systemd" / "system").mkdir(parents=True, exist_ok=True)
        (usr / "lib" / "systemd" / "system" / "NetworkManager.service").write_text("[Unit]")

    def fill_boot(self, boot: Path):
        boot.mkdir(parents=True, exist_ok=True)
        for ver in (RHEL_KERNEL, OLD_KERNEL):
            (boot / f"vmlinuz-{ver}").write_text("kernel")
            # lsinitrd-style listing; modules appear as (compressed) .ko files under usr/lib/modules
            files = ["usr/lib/modules/x/kernel/drivers/scsi/vmw_pvscsi.ko.xz"]
            if ver in self.virtio_in or ver in self.partial_virtio:
                files += ["usr/lib/modules/x/kernel/drivers/virtio/virtio_pci.ko.xz",
                          "usr/lib/modules/x/kernel/drivers/block/virtio_blk.ko.xz",
                          "usr/lib/modules/x/kernel/drivers/net/virtio_net.ko.xz"]
            if ver in self.virtio_in and "virtio_scsi" not in self.builtin:
                files.append("usr/lib/modules/x/kernel/drivers/scsi/virtio_scsi.ko.xz")
            (boot / f"initramfs-{ver}.img").write_text("\n".join(files) + "\n")
        (boot / "initramfs-0-rescue-abc.img").write_text("rescue")

    # -- command dispatcher
    def __call__(self, argv: list[str], timeout_s: int) -> CmdResult:
        self.calls.append(argv)
        cmd = argv[0]
        if cmd in ("partx", "udevadm", "sync"):
            return CmdResult(0, "", "")
        if cmd == "lsblk":
            return CmdResult(0, json.dumps(self.nodes()), "")
        if cmd == "pvs":
            if "--config" in argv:  # our disk only
                if self.layout == "ubuntu":
                    return CmdResult(0, "  /dev/sdb3 ubuntu-vg\n", "")
                if self.layout == "rhel_split_usr":
                    return CmdResult(0, "  /dev/sdb2 rootvg\n", "")
                if self.layout == "ol_lvm":
                    return CmdResult(0, "  /dev/sdb3 ol\n", "")
                if self.layout != "lvm":
                    return CmdResult(0, "", "")
                return CmdResult(0, "  /dev/sdb2 rhel\n", "")
            return CmdResult(0, "".join(f"  /dev/sda3 {vg}\n" for vg in self.foreign), "")
        if cmd == "vgchange":
            vg = argv[-1]
            if "-ay" in argv:
                self.active_vgs.append(vg)
            else:
                self.active_vgs.remove(vg)
            return CmdResult(0, "", "")
        if cmd == "lvs":
            assert "--config" in argv and argv[-1] in self.active_vgs
            if self.layout == "ubuntu":
                return CmdResult(0, "  /dev/mapper/ubuntu--vg-ubuntu--lv\n", "")
            if self.layout == "rhel_split_usr":
                return CmdResult(0, "  /dev/mapper/rootvg-rootlv\n  /dev/mapper/rootvg-usrlv\n", "")
            if self.layout == "ol_lvm":
                return CmdResult(0, "  /dev/mapper/ol-root\n  /dev/mapper/ol-swap\n", "")
            return CmdResult(0, "  /dev/mapper/rhel-root\n  /dev/mapper/rhel-swap\n", "")
        if cmd == "blkid":
            if self.blkid_fails:
                return CmdResult(2, "", "")
            assert "-p" in argv or "-o" in argv, "direct probe expected (udev cache is what failed us)"
            probes = {"/dev/mapper/rhel-root": "TYPE=xfs\nUUID=root-uuid\n",
                      "/dev/mapper/ol-root": "TYPE=xfs\nUUID=root-uuid\n",
                      "/dev/mapper/rhel-swap": "TYPE=swap\nUUID=swap-uuid\n",
                      "/dev/mapper/rootvg-rootlv": "TYPE=xfs\nUUID=root-uuid\n",
                      "/dev/mapper/rootvg-usrlv": "TYPE=xfs\nUUID=usr-uuid\n"}
            self.probed = getattr(self, "probed", []) + [argv[-1]]
            if "-o" in argv and "value" in argv:
                path = argv[-1]
                if path in probes:
                    for line in probes[path].splitlines():
                        if line.startswith("TYPE="):
                            return CmdResult(0, line.split("=", 1)[1] + "\n", "")
                return CmdResult(2, "", "")
            return CmdResult(0, probes[argv[-1]], "") if argv[-1] in probes else CmdResult(2, "", "")
        if cmd == "mount":
            if "--bind" in argv:
                self.mounted[argv[-1]] = argv[-2]
                return CmdResult(0, "", "")
            if "remount,rw" in argv[2:3]:
                return CmdResult(0, "", "")
            dev, where = argv[-2], Path(argv[-1])
            if dev in self.fail_mount:
                return CmdResult(32, "", f"mount: {dev}: wrong fs type, bad option, bad superblock")
            self.mounted[str(where)] = dev
            if dev == "/dev/mapper/rootvg-rootlv":
                self.fill_split_root(where, usr_spec=getattr(self, "usr_fstab_spec", "/dev/mapper/rootvg-usrlv"))
            elif dev == "/dev/mapper/rootvg-usrlv":
                self.fill_usr(where)
            elif dev in ("/dev/sdb1", "/dev/sdb2") and where.name == "boot":
                self.fill_boot(where)
            elif dev in ("/dev/mapper/rhel-root", "/dev/mapper/ol-root", "/dev/sdb2") and self.layout in ("lvm", "ol_lvm", "plain"):
                self.fill_root(where)
            elif dev == "/dev/mapper/ubuntu--vg-ubuntu--lv":
                self.fill_root(where)
                if self.layout == "ubuntu":
                    (where / "initrd.img-5.15.0-91-generic").write_text("virtio_blk.ko virtio_scsi.ko")
            else:
                where.mkdir(parents=True, exist_ok=True)  # /boot partition probed as root: no fstab there
                (where / "vmlinuz-x").write_text("")
            return CmdResult(0, "", "")
        if cmd == "umount":
            where = argv[-1]
            self.mounted.pop(where, None)
            p = Path(where)
            if p.exists() and "--bind" not in argv:
                for child in p.iterdir():
                    if str(child) not in self.mounted:  # keep still-mounted sub mounts (never the case here)
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
            return CmdResult(0, "", "")
        if cmd == "setfattr":
            return CmdResult(0, "", "")
        if cmd == "chroot":
            root, prog = Path(argv[1]), argv[2]
            if prog.endswith("setfiles"):
                return CmdResult(0, "", "")
            if prog == "lsinitrd":
                img = root / argv[3].lstrip("/")
                return CmdResult(0, img.read_text(), "") if img.exists() else CmdResult(1, "", "no such file")
            if prog == "dracut":
                if argv[3] == "--help":
                    return CmdResult(0, "" if self.old_dracut else "  -N, --no-hostonly  Host-Only mode off", "")
                if self.dracut_rc:
                    return CmdResult(self.dracut_rc, "", "dracut: Failed to install module kernel-modules")
                img, ver = root / argv[-2].lstrip("/"), argv[-1]
                assert "--add-drivers" in argv and VIRTIO_DRIVERS in argv
                assert ("--no-hostonly" in argv) is (not self.old_dracut)
                img.write_text(f"generic image {ver}\nusr/lib/modules/x/kernel/drivers/block/virtio_blk.ko.xz\n"
                               "usr/lib/modules/x/kernel/drivers/virtio/virtio_pci.ko.xz\n"
                               + ("" if "virtio_scsi" in self.missing_for(ver) or "virtio_scsi" in self.builtin
                                  else "usr/lib/modules/x/kernel/drivers/scsi/virtio_scsi.ko.xz\n"))
                self.rebuilt.append(ver)
                return CmdResult(0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def base(tmp_path):
    return tmp_path / "mnt"


def run(shell: FakeShell, base: Path, device="/dev/sdb"):
    fixer = InitramfsFixer(run=shell, mount_base=base)
    msgs = []
    result = fixer.rebuild(device, notify=msgs.append)
    # everything is undone whatever happened
    if isinstance(shell, FakeShell):
        assert shell.mounted == {}, shell.mounted
        assert shell.active_vgs == []
    assert not base.exists() or not any(base.iterdir())
    return result, msgs


def test_rhel_lvm_root_rebuilt(base):
    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL})
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert result.kernels == [RHEL_KERNEL]  # the other one already had virtio
    assert shell.rebuilt == [RHEL_KERNEL]
    assert any("activated guest volume group(s) rhel" in m for m in msgs)
    assert any("/boot on /dev/sdb1" in m for m in msgs)
    assert any("already has virtio" in m and OLD_KERNEL in m for m in msgs)
    # the dracut config snippet went into the guest before dracut ran
    conf_writes = [c for c in shell.calls if c[0] == "chroot" and c[2] == "dracut" and c[3] == "--force"]
    assert len(conf_writes) == 1
    # order: LVM activated with a filter on our disk only, deactivated at the end
    vgchange = [c for c in shell.calls if c[0] == "vgchange"]
    assert [c[-2] for c in vgchange] == ["-ay", "-an"] and all("/dev/sdb" in c[2] for c in vgchange)
    assert all("use_devicesfile=0" in c[2] for c in vgchange)
    # bind mounts for the chroot were made and removed
    binds = [c[-1] for c in shell.calls if c[0] == "mount" and "--bind" in c]
    assert [Path(b).name for b in binds] == ["dev", "proc", "sys"]
    # xfs mounted with nouuid (the same image may be attached twice)
    root_mount = next(c for c in shell.calls if c[0] == "mount" and c[-2] == "/dev/mapper/rhel-root")
    assert "nouuid" in root_mount[2]


def test_lvm_root_mount_when_blkid_fails(base):
    """Oracle Linux / RHEL: activated LVs listed without FSTYPE and blkid returns nothing."""
    shell = FakeShell(layout="ol_lvm", udev_stale=True, blkid_fails=True)
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert any("guest root file system on /dev/mapper/ol-root" in m for m in msgs)
    assert any("/boot on /dev/sdb2" in m for m in msgs)


def test_lvs_found_even_when_udev_has_not_probed_them(base):
    """What happened on the first real RHEL 7 run: lsblk listed the freshly activated LVs without a file
    system type, so the root LV was not a candidate.  blkid -p fills the gap."""
    shell = FakeShell(layout="lvm", udev_stale=True)
    result, msgs = run(shell, base)
    assert result.status == "done" and result.kernels == [OLD_KERNEL, RHEL_KERNEL], result
    assert "/dev/mapper/rhel-root" in shell.probed
    assert any("block devices:" in m and "/dev/mapper/rhel-root (lvm, xfs)" in m for m in msgs)
    # lsblk does not even list the LVs: lvs supplies them
    shell = FakeShell(layout="lvm", lsblk_hides_lvs=True)
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert any(c[0] == "lvs" for c in shell.calls)
    assert any("guest root file system on /dev/mapper/rhel-root" in m for m in msgs)


def test_rhel_split_usr_root_and_modules_on_separate_lvs(base):
    """RHEL 9 on Azure: / has fstab, kernel modules and dracut live on a separate /usr LV."""
    shell = FakeShell(layout="rhel_split_usr", boot_fstab="UUID=boot-uuid")
    result, msgs = run(shell, base)
    assert result.status == "done" and result.kernels == [OLD_KERNEL, RHEL_KERNEL], result
    assert any("guest root file system on /dev/mapper/rootvg-rootlv" in m for m in msgs)
    assert any("mounted /usr from /dev/mapper/rootvg-usrlv" in m for m in msgs)
    assert any("/boot on /dev/sdb1" in m for m in msgs)
    assert shell.rebuilt == [OLD_KERNEL, RHEL_KERNEL]


def test_rhel_split_usr_fstab_by_id_dm_name(base):
    """RHEL anaconda often lists LVs as /dev/disk/by-id/dm-name-vg-lv in fstab."""
    shell = FakeShell(layout="rhel_split_usr", boot_fstab="UUID=boot-uuid")
    shell.usr_fstab_spec = "/dev/disk/by-id/dm-name-rootvg-usrlv"
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert any("mounted /usr from /dev/mapper/rootvg-usrlv" in m for m in msgs)


def test_rejected_candidates_are_explained(base):
    shell = FakeShell(layout="plain", boot_fstab="")
    _, msgs = run(shell, base)
    # /dev/sdb1 (the boot partition) was probed first and rejected with a reason
    assert any("/dev/sdb1 is not the root fs (fstab=no" in m for m in msgs)


def test_plain_partitions_and_boot_on_root(base):
    shell = FakeShell(layout="plain", boot_fstab="")  # no separate /boot
    result, _ = run(shell, base)
    assert result.status == "done" and sorted(result.kernels) == sorted([OLD_KERNEL, RHEL_KERNEL])
    assert not any(c[0] == "vgchange" for c in shell.calls)


def test_boot_by_guest_device_name_maps_to_our_disk(base):
    # fstab says /dev/sda1: must become partition 1 of *our* disk, never the helper's /dev/sda1
    shell = FakeShell(layout="lvm", boot_fstab="/dev/sda1")
    result, msgs = run(shell, base)
    assert result.status == "done"
    assert any("/boot on /dev/sdb1" in m for m in msgs)


def test_not_needed_when_virtio_present(base):
    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL, RHEL_KERNEL})
    result, _ = run(shell, base)
    assert result.status == "not_needed" and shell.rebuilt == []
    assert "2 kernel(s) already" in result.detail


def test_initramfs_with_virtio_blk_but_no_virtio_scsi_is_rebuilt(base):
    """The OCI boot volume is a virtio-scsi disk: virtio_blk/virtio_net alone (what dracut puts into a
    generic image on many distros) is not enough.  'virtio' as a substring must not count as present."""
    shell = FakeShell(layout="plain", boot_fstab="", partial_virtio={OLD_KERNEL, RHEL_KERNEL})
    result, msgs = run(shell, base)
    assert result.status == "done" and sorted(shell.rebuilt) == sorted([OLD_KERNEL, RHEL_KERNEL])
    assert any("lacks virtio" in m for m in msgs)


def test_amazon_linux_without_virtio_scsi_module_fails_with_package_hint(base):
    """AL2023 job d45e6be1: the AMI kernel has virtio_pci/blk/net but ships virtio_scsi only in the
    kernel-modules-extra package.  dracut silently built an image without it, the check passed on the
    'virtio' substring, and the guest waited forever for its root disk.  Now: no dracut run, a failed step
    with the fix spelled out."""
    shell = FakeShell(layout="plain", boot_fstab="", modules_missing={"virtio_scsi"},
                      os_release="Amazon Linux 2023.12.20260918")
    result, msgs = run(shell, base)
    assert result.status == "failed", result
    assert "virtio_scsi" in result.detail and RHEL_KERNEL in result.detail
    assert "modules-extra" in result.detail and "dnf install" in result.detail
    assert shell.rebuilt == []  # no point running dracut


def test_amazon_modules_extra_package_name():
    """AL2023 job d45e6be1 ran the kernel6.18 flavour: 'kernel-modules-extra-$(uname -r)' does not exist."""
    from helper_app.guest.initramfs import amazon_modules_extra_package
    assert amazon_modules_extra_package("6.18.48-109.150.amzn2023.x86_64") == "kernel6.18-modules-extra"
    assert amazon_modules_extra_package("6.12.20-30.100.amzn2023.x86_64") == "kernel6.12-modules-extra"
    assert amazon_modules_extra_package("6.1.186-228.376.amzn2023.x86_64") == "kernel-modules-extra"


def test_newer_kernel_with_virtio_scsi_is_rebuilt_while_old_one_is_reported(base):
    """After 'dnf install kernel kernel-modules-extra' on AL2023 two kernels are installed: the old one
    still has no virtio_scsi, the new (default) one does.  Rebuild what can boot, say what cannot."""
    shell = FakeShell(layout="plain", boot_fstab="", modules_missing={OLD_KERNEL: {"virtio_scsi"}},
                      os_release="Amazon Linux 2023.12.20260918")
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert shell.rebuilt == [RHEL_KERNEL] and result.kernels == [RHEL_KERNEL]
    assert OLD_KERNEL in result.detail and "left as is" in result.detail and "virtio_scsi" in result.detail
    assert any("left as is" in m and OLD_KERNEL in m for m in msgs)


def test_missing_virtio_pci_module_fails_generically(base):
    shell = FakeShell(layout="plain", boot_fstab="", modules_missing={"virtio_pci"})
    result, _ = run(shell, base)
    assert result.status == "failed" and "virtio_pci" in result.detail
    assert "kernel-modules-extra" not in result.detail  # not an Amazon Linux guest
    assert "install" in result.detail and "migrate again" in result.detail


def test_builtin_virtio_drivers_count_as_present(base):
    """Kernels with CONFIG_SCSI_VIRTIO=y have no virtio_scsi.ko anywhere: modules.builtin says so."""
    shell = FakeShell(layout="plain", boot_fstab="", builtin={"virtio_scsi"}, partial_virtio={OLD_KERNEL, RHEL_KERNEL})
    result, _ = run(shell, base)
    assert result.status == "not_needed", result
    assert shell.rebuilt == []


def test_skips(base):
    # LUKS
    result, _ = run(FakeShell(layout="luks"), base)
    assert result.status == "skipped" and "LUKS" in result.detail
    # Windows / no Linux root
    result, _ = run(FakeShell(layout="windows"), base)
    assert result.status == "skipped" and "no Linux root" in result.detail
    # no dracut in the guest
    result, _ = run(FakeShell(layout="plain", boot_fstab="", dracut=False), base)
    assert result.status == "skipped" and "no dracut" in result.detail
    # volume group name clash with the helper
    shell = FakeShell(layout="lvm", lvm_foreign_vgs=("rhel",))
    result, _ = run(shell, base)
    assert result.status == "skipped" and "same name" in result.detail
    assert not any(c[0] == "vgchange" for c in shell.calls)  # never activated
    # /boot listed in fstab but not on this disk
    result, _ = run(FakeShell(layout="lvm", boot_fstab="UUID=elsewhere"), base)
    assert result.status == "skipped" and "/boot" in result.detail


def test_failures_are_reported_not_raised(base):
    shell = FakeShell(layout="lvm", dracut_rc=1)
    result, _ = run(shell, base)
    assert result.status == "failed" and "dracut failed for kernel" in result.detail
    assert "Failed to install module" in result.detail
    # the disk cannot be mounted at all
    result, _ = run(FakeShell(layout="lvm", fail_mount={"/dev/mapper/rhel-root", "/dev/sdb1"}), base)
    assert result.status == "skipped" and "no Linux root" in result.detail
    # missing tool on the helper
    def no_lsblk(argv, timeout_s):
        if argv[0] == "lsblk":
            raise FileNotFoundError("lsblk")
        return CmdResult(0, "", "")
    result, _ = run(no_lsblk, base)  # type: ignore[arg-type]
    assert result.status == "failed" and "cannot run lsblk" in result.detail


def test_old_dracut_without_no_hostonly(base):
    shell = FakeShell(layout="plain", boot_fstab="", old_dracut=True)
    result, _ = run(shell, base)
    assert result.status == "done"


def test_dracut_conf_written_into_guest(base, monkeypatch):
    captured = {}
    shell = FakeShell(layout="plain", boot_fstab="")
    orig = shell.__call__

    def spy(argv, timeout_s):
        if argv[0] == "chroot" and argv[2] == "dracut" and argv[3] == "--force":
            conf = Path(argv[1]) / "etc" / "dracut.conf.d" / DRACUT_CONF_NAME
            captured["conf"] = conf.read_text()
        return orig(argv, timeout_s)

    result, _ = run(spy, base)  # type: ignore[arg-type]
    assert result.status == "done"
    assert f'add_drivers+=" {VIRTIO_DRIVERS} "' in captured["conf"]


def test_both_steps_share_one_mount_session(base):
    from helper_app.guest.fixup import GuestFixer
    from helper_app.guest.network import NM_KEYFILE

    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL})
    written = {}
    orig = shell.__call__

    def spy(argv, timeout_s):
        # the network step runs while the root is still mounted; capture what it wrote before umount
        if argv[0] == "umount":
            kf = Path(argv[-1]) / "etc" / "NetworkManager" / "system-connections" / NM_KEYFILE
            if kf.exists():
                written["keyfile"] = kf.read_text()
        return orig(argv, timeout_s)

    msgs = []
    res = GuestFixer(run=spy, mount_base=base).fix("/dev/sdb", initramfs=True, network=True, notify=msgs.append)
    assert res.initramfs.status == "done" and res.initramfs.kernels == [RHEL_KERNEL]
    assert res.network.status == "done" and "NetworkManager DHCP profile" in res.network.detail
    assert "type=ethernet" in written["keyfile"]
    # one scan, one root mount, everything undone
    assert sum(1 for c in shell.calls if c[0] == "lsblk") == 2  # before and after LVM activation
    assert sum(1 for c in shell.calls if c[0] == "mount" and c[-2] == "/dev/mapper/rhel-root") == 1
    assert shell.mounted == {} and shell.active_vgs == []
    # each step has its own log; the network log does not repeat the disk scan
    assert any("guest root file system on" in ln for ln in res.initramfs.log)
    assert not any("guest root file system on" in ln for ln in res.network.log)
    assert any("network stack: NetworkManager (enabled)" in ln for ln in res.network.log)

    # one step failing does not stop the other
    shell = FakeShell(layout="lvm", dracut_rc=1)
    res = GuestFixer(run=shell, mount_base=base).fix("/dev/sdb", initramfs=True, network=True)
    assert res.initramfs.status == "failed" and res.network.status == "done"
    # a step that is not requested is not reported
    res = GuestFixer(run=FakeShell(layout="lvm"), mount_base=base).fix("/dev/sdb", initramfs=False, network=True)
    assert res.initramfs is None and res.network.status == "done"
    # no root at all: both requested steps get the same answer
    res = GuestFixer(run=FakeShell(layout="luks"), mount_base=base).fix("/dev/sdb")
    assert res.initramfs.status == "skipped" and res.network.status == "skipped" and "LUKS" in res.network.detail


def test_ubuntu_layout_boot_by_disk_by_uuid_and_netplan(base):
    """What went wrong on the first Ubuntu migration: subiquity writes /boot as /dev/disk/by-uuid/<uuid> in
    fstab, which was not resolved, and that aborted the whole session - the netplan drop-in was never
    written.  Now /boot resolves, and a /boot problem only skips the initramfs step."""
    from helper_app.guest.fixup import GuestFixer
    from helper_app.guest.network import NETPLAN_FILE

    shell = FakeShell(layout="ubuntu", boot_fstab="/dev/disk/by-uuid/boot-uuid")
    seen = {}
    orig = shell.__call__

    def spy(argv, timeout_s):
        if argv[0] == "umount" and (Path(argv[-1]) / "etc" / "netplan").is_dir():
            seen["netplan"] = sorted(p.name for p in (Path(argv[-1]) / "etc" / "netplan").iterdir())
        return orig(argv, timeout_s)

    msgs = []
    res = GuestFixer(run=spy, mount_base=base).fix("/dev/sdb", notify=msgs.append)
    assert any("/boot on /dev/sdb2" in m for m in msgs), msgs
    assert res.initramfs.status == "skipped" and "no dracut" in res.initramfs.detail
    assert res.network.status == "done" and "netplan" in res.network.detail, res.network
    assert seen["netplan"] == ["00-installer-config.yaml", NETPLAN_FILE]
    assert shell.mounted == {} and shell.active_vgs == []

    # /boot really not on this disk: initramfs step skipped with the reason, network step still runs
    shell = FakeShell(layout="ubuntu", boot_fstab="/dev/disk/by-uuid/elsewhere")
    res = GuestFixer(run=shell, mount_base=base).fix("/dev/sdb", notify=msgs.append)
    assert res.initramfs.status == "skipped" and "/boot" in res.initramfs.detail and "elsewhere" in res.initramfs.detail
    assert res.network.status == "done"


def test_resolve_spec():
    from helper_app.guest.initramfs import BlockNode
    nodes = [BlockNode("/dev/sdb1", "part", "xfs", "u1", "BOOT"), BlockNode("/dev/sdb2", "part", "LVM2_member", "u2", ""),
             BlockNode("/dev/mapper/my--vg-root", "lvm", "xfs", "u3", ""),
             BlockNode("/dev/sdb15", "part", "vfat", "u4", "", partuuid="p15", partlabel="EFI System")]
    r = _Session._resolve_spec_from_nodes
    assert r("UUID=u1", nodes) == "/dev/sdb1"
    assert r("LABEL=BOOT", nodes) == "/dev/sdb1"
    assert r("/dev/disk/by-uuid/u1", nodes) == "/dev/sdb1"  # Ubuntu's installer
    assert r("/dev/disk/by-label/BOOT", nodes) == "/dev/sdb1"
    assert r("PARTUUID=p15", nodes) == "/dev/sdb15"
    assert r("/dev/disk/by-partuuid/p15", nodes) == "/dev/sdb15"
    assert r("PARTLABEL=EFI System", nodes) == "/dev/sdb15"
    assert r("/dev/disk/by-id/dm-uuid-LVM-abc", nodes) is None  # not resolvable, and not mistaken for /dev/vg/lv
    assert r("UUID=", nodes) is None  # empty value must not match nodes without a UUID
    assert r("/dev/mapper/my--vg-root", nodes) == "/dev/mapper/my--vg-root"
    assert r("/dev/my-vg/root", nodes) == "/dev/mapper/my--vg-root"
    assert r("/dev/sda1", nodes) == "/dev/sdb1"
    assert r("/dev/nvme0n1p15", nodes) == "/dev/sdb15"
    assert r("/dev/sda3", nodes) is None
    assert r("/dev/sda", nodes) is None  # whole disk: not a partition
    assert r("PARTUUID=abc", nodes) is None


def test_resolve_spec_dm_name_and_helper_symlinks(monkeypatch):
    from helper_app.guest.initramfs import BlockNode

    nodes = [BlockNode("/dev/mapper/rootvg-usrlv", "lvm", "xfs", "usr-uuid", "")]
    sess = _Session(type("F", (), {"run": None, "mount_base": Path("/tmp")})(), "/dev/sdb", lambda _m: None)
    assert sess.resolve_spec("/dev/disk/by-id/dm-name-rootvg-usrlv", nodes) == "/dev/mapper/rootvg-usrlv"

    link = "/dev/disk/by-id/dm-uuid-LVM-abc123"
    monkeypatch.setattr("helper_app.guest.initramfs.os.path.exists", lambda p: p == link)
    monkeypatch.setattr("helper_app.guest.initramfs.os.path.realpath", lambda p: "/dev/mapper/rootvg-usrlv")
    assert sess.resolve_spec(link, nodes) == "/dev/mapper/rootvg-usrlv"
