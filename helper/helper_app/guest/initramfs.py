"""Rebuild a Linux guest's initramfs with virtio drivers on the copied boot volume.

Why: dracut builds *hostonly* initramfs images on RHEL/CentOS/Oracle Linux (and SUSE).  An image
built while the VM ran on VMware only contains ``vmw_pvscsi``/``mptspi``; in OCI the boot volume is a
virtio device, so the kernel never finds the disk, LVM never finds ``rhel/root`` and dracut drops to
the emergency shell ("dracut-initqueue timeout ... /dev/mapper/rhel-root does not exist").

How: the target boot volume is still attached to the helper right after the copy.  We find the guest
root file system (plain partition or LVM), mount it together with ``/boot``, and run the *guest's
own* dracut in a chroot (``dracut --force --no-hostonly --add-drivers "virtio ..."``) for every
installed kernel whose initramfs lacks virtio modules.  Only the target copy is touched; the source
VM stays as it was.  Anything unexpected (LUKS, btrfs, no dracut, volume group name clash with the
helper) ends in a *skipped*/*failed* result with the reason - never in a failed migration.

All shell interaction goes through an injectable ``Runner`` so the logic is unit-testable without
block devices.

``_Session`` (find and mount the guest root, undo everything) is shared with the other post-copy
steps; ``helper_app.guest.fixup`` runs them all in one session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from helper_app.models import GuestFixup

log = logging.getLogger(__name__)

# sd_mod is the SCSI disk driver: a kernel that booted from NVMe (Amazon Linux on EC2) never had it in its
# initramfs, and dracut-config-ec2 does not add it either - without it the volume shows up as
# "scsi 0:0:0:1: Direct-Access ORACLE BlockVolume" and no /dev/sda ever appears.
VIRTIO_DRIVERS = "virtio virtio_pci virtio_ring virtio_blk virtio_scsi virtio_net sd_mod"
# What the guest must have to find its root disk in OCI: paravirtualized boot volumes are virtio-scsi disks
# behind a virtio-pci transport, attached by the SCSI disk driver.  virtio_blk/virtio_net alone (what many
# kernels ship by default) do not help.
REQUIRED_BOOT_DRIVERS = ("virtio_pci", "virtio_scsi", "sd_mod")
DRACUT_CONF_NAME = "oci-virtio.conf"
ROOT_FS_TYPES = {"xfs", "ext4", "ext3", "ext2"}
# RHEL (and similar) often place /usr (kernel modules, dracut) on its own LV; mount these from fstab
# before deciding whether a candidate is the guest root.
FSTAB_SPLIT_ROOT_MOUNTS = ("/usr", "/var", "/home")
DRACUT_TIMEOUT_S = 900
MIN_FREE_BOOT_BYTES = 150 * 1024 * 1024  # a non-hostonly initramfs is 50-90 MB

# one fix-up at a time: two RHEL guests both call their volume group "rhel"
_LOCK = threading.Lock()

Notify = Callable[[str], None]


class CmdResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], int], CmdResult]  # (argv, timeout_s) -> result; OSError if the binary is missing


def default_runner(argv: list[str], timeout_s: int = 120) -> CmdResult:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return CmdResult(124, out, f"timed out after {timeout_s} s")
    return CmdResult(p.returncode, p.stdout, p.stderr)


class Skip(Exception):
    """The fix-up does not apply to this guest / layout (reason is user facing)."""


class Fail(Exception):
    """The fix-up was attempted but did not succeed."""


class BlockNode(NamedTuple):
    path: str
    type: str  # disk | part | lvm | crypt ...
    fstype: str
    uuid: str
    label: str
    partuuid: str = ""
    partlabel: str = ""


def _tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


class InitramfsFixer:
    """Initramfs-only entry point (kept for callers that want just this step)."""

    def __init__(self, run: Runner = default_runner, mount_base: str | Path = "/run/vc-oci-helper/guest"):
        self.run = run
        self.mount_base = Path(mount_base)

    def rebuild(self, device: str, notify: Optional[Notify] = None) -> GuestFixup:
        """Fix the initramfs images on the guest disk ``device`` (the copied boot volume)."""
        from helper_app.guest.fixup import GuestFixer  # local import: fixup builds on this module

        return GuestFixer(run=self.run, mount_base=self.mount_base).fix(
            device, initramfs=True, network=False, notify=notify).initramfs


def amazon_modules_extra_package(ver: str) -> str:
    """Amazon Linux 2023 packages virtio_scsi in kernel-modules-extra for its default 6.1 kernel and in
    kernel6.12-modules-extra / kernel6.18-modules-extra for the alternative kernel flavours."""
    m = re.match(r"(\d+)\.(\d+)\.", ver)
    if m and (m.group(1), m.group(2)) != ("6", "1"):
        return f"kernel{m.group(1)}.{m.group(2)}-modules-extra"
    return "kernel-modules-extra"


def initramfs_outcome(kernels: list[str], rebuilt: list[str], notes: list[str],
                      unfixable: Optional[dict[str, list[str]]] = None) -> GuestFixup:
    unfixable = unfixable or {}
    tail = ""
    if unfixable:
        parts = [f"kernel {ver} has no {', '.join(names)} driver" for ver, names in unfixable.items()]
        tail = f"; {'; '.join(parts)} and was left as is (boot the other kernel)"
    if not rebuilt:
        good = len(kernels) - len(unfixable)
        return GuestFixup(status="not_needed", kernels=[], log=notes,
                          detail=f"initramfs of {good} kernel(s) already contains virtio drivers{tail}")
    return GuestFixup(status="done", kernels=rebuilt, log=notes,
                      detail=f"initramfs rebuilt with virtio drivers for {', '.join(rebuilt)}{tail}")


class _Session:
    """One fix-up run with its mounts and activated volume groups (undone in cleanup()).

    ``open_root()`` finds and mounts the guest root (plus /boot); the steps then work on ``self.mnt``.
    """

    def __init__(self, fixer, device: str, note: Notify):
        self.f = fixer
        self.run = fixer.run
        self.note = note
        self.device = device
        self.real = os.path.realpath(device) if os.path.exists(device) else device
        self.mnt = fixer.mount_base / uuid.uuid4().hex[:8]
        self.mounts: list[Path] = []  # mounted paths, unmounted in reverse order
        self.vgs: list[str] = []  # guest volume groups we activated
        self.boot_problem: Optional[str] = None  # why a separate /boot could not be mounted (initramfs step skips)
        self.unfixable: dict[str, list[str]] = {}  # kernels whose initramfs cannot get the drivers: {ver: [names]}
        # LVM must look at this disk only (and ignore the helper's devices file, which does not list it)
        self.lvm_config = ("devices { use_devicesfile=0 filter=[ "
                           f'"a|^{re.escape(self.real)}.*|", "r|.*|" ] }}')

    # ---------------------------------------------------------------- helpers
    def sh(self, argv: list[str], timeout_s: int = 120, ok: bool = True) -> CmdResult:
        try:
            r = self.run(argv, timeout_s)
        except OSError as exc:
            raise Fail(f"cannot run {argv[0]} on the migration tool VM: {exc}") from exc
        if ok and r.returncode != 0:
            raise Fail(f"{' '.join(argv[:3])} failed (rc {r.returncode}): {_tail(r.stderr or r.stdout)}")
        return r

    def mount(self, what: str, where: Path, opts: list[str], fstype: str = "") -> None:
        where.mkdir(parents=True, exist_ok=True)
        argv = ["mount"]
        if fstype:
            argv += ["-t", fstype]
        if opts:
            argv += ["-o", ",".join(opts)]
        argv += [what, str(where)]
        self.sh(argv)
        self.mounts.append(where)

    def bind(self, src: str, where: Path) -> None:
        where.mkdir(parents=True, exist_ok=True)
        self.sh(["mount", "--bind", src, str(where)])
        self.mounts.append(where)

    def umount(self, where: Path) -> None:
        r = self.sh(["umount", str(where)], ok=False)
        if r.returncode != 0:
            self.sh(["umount", "-l", str(where)], ok=False)
        if where in self.mounts:
            self.mounts.remove(where)

    def pop_guest_mounts(self, keep: int) -> None:
        """Unmount guest mounts added after ``keep`` (children before the root mount)."""
        while len(self.mounts) > keep:
            self.umount(self.mounts[-1])

    def has_kernel_modules(self) -> bool:
        return ((self.mnt / "lib" / "modules").is_dir()
                or (self.mnt / "usr" / "lib" / "modules").is_dir())

    def iter_fstab(self):
        fstab = self.mnt / "etc" / "fstab"
        if not fstab.exists():
            return
        for line in fstab.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith("#"):
                continue
            spec, mountpoint = parts[0], parts[1]
            if mountpoint == "swap" or not mountpoint.startswith("/"):
                continue
            fstype = parts[2] if len(parts) > 2 else ""
            yield spec, mountpoint, fstype

    def mount_opts(self, dev: str, fstype: str, nodes: list[BlockNode]) -> list[str]:
        xfs = fstype == "xfs" or any(n.path == dev and n.fstype == "xfs" for n in nodes)
        return ["nouuid"] if xfs else []

    def try_mount_fstab(self, mountpoint: str, nodes: list[BlockNode]) -> bool:
        """Mount one fstab entry on this disk.  Returns True if ``mountpoint`` is mounted."""
        where = self.mnt / mountpoint.lstrip("/")
        if where in self.mounts:
            return True
        for spec, mp, fstype in self.iter_fstab():
            if mp != mountpoint:
                continue
            dev = self.resolve_spec(spec, nodes)
            if dev is None:
                self.note(f"fstab entry for {mountpoint} ({spec}) did not resolve on this disk")
                return False
            self.mount(dev, where, self.mount_opts(dev, fstype, nodes))
            self.note(f"mounted {mp} from {dev}")
            return True
        return False

    def mount_split_root_filesystems(self, nodes: list[BlockNode]) -> None:
        """Mount separate /usr, /var, /home volumes listed in fstab (RHEL LVM layouts)."""
        for mp in FSTAB_SPLIT_ROOT_MOUNTS:
            try:
                self.try_mount_fstab(mp, nodes)
            except Fail as exc:
                self.note(f"cannot mount {mp} from fstab: {exc}")

    def remount_guest_rw(self) -> None:
        for where in self.mounts:
            self.sh(["mount", "-o", "remount,rw", str(where)], ok=False)

    def lsblk(self) -> list[BlockNode]:
        r = self.sh(["lsblk", "-J", "-p", "-o", "NAME,TYPE,FSTYPE,UUID,LABEL,PARTUUID,PARTLABEL", self.real])
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise Fail(f"cannot parse lsblk output: {exc}") from exc
        nodes: list[BlockNode] = []

        def walk(items):
            for it in items or []:
                nodes.append(BlockNode(path=it.get("name") or "", type=it.get("type") or "",
                                       fstype=it.get("fstype") or "", uuid=it.get("uuid") or "",
                                       label=it.get("label") or "", partuuid=it.get("partuuid") or "",
                                       partlabel=it.get("partlabel") or ""))
                walk(it.get("children"))

        walk(data.get("blockdevices"))
        return nodes

    # ------------------------------------------------------------------ steps
    def open_root(self) -> None:
        """Find the guest root file system on the disk and mount it (rw) with its /boot."""
        self.note(f"scanning {self.real} for the guest root file system")
        self.sh(["partx", "-u", self.real], ok=False)
        self.sh(["udevadm", "settle"], timeout_s=30, ok=False)
        nodes = self.lsblk()

        if any(n.fstype == "LVM2_member" for n in nodes):
            self.activate_lvm()
            nodes = self.lsblk()
            nodes = self.add_logical_volumes(nodes)
        nodes = self.probe_unknown_fstypes(nodes)
        listed = ", ".join(f"{n.path} ({n.type}, {n.fstype or 'no fs'})" for n in nodes if n.type != "disk")
        self.note(f"block devices: {listed or 'none'}")

        root_node = self.find_root(nodes)
        self.note(f"guest root file system on {root_node.path} ({root_node.fstype})")
        try:
            self.mount_boot(nodes)
        except Skip as exc:  # only the initramfs step needs /boot; the others work on the root fs
            self.boot_problem = str(exc)
            self.note(f"/boot not mounted: {exc}")

    def bind_system_dirs(self) -> None:
        """/dev, /proc and /sys of the helper inside the guest tree, for chroot'ed tools (idempotent)."""
        for name in ("dev", "proc", "sys"):
            if (self.mnt / name) not in self.mounts:
                self.bind("/" + name, self.mnt / name)

    def execute(self) -> tuple[list[str], list[str]]:
        self.open_root()
        return self.rebuild_initramfs()

    def rebuild_initramfs(self) -> tuple[list[str], list[str]]:
        """(installed kernels, kernels whose initramfs was rebuilt); the root must be open."""
        if self.boot_problem:
            raise Skip(self.boot_problem)
        dracut = next((p for p in ("usr/bin/dracut", "usr/sbin/dracut", "sbin/dracut") if (self.mnt / p).exists()),
                      None)
        if dracut is None:
            raise Skip("guest has no dracut (Ubuntu/Debian initramfs-tools images already include virtio drivers)")

        kernels = self.installed_kernels()
        if not kernels:
            raise Skip("no installed kernels found under /lib/modules with an initramfs in /boot")

        self.bind_system_dirs()

        todo = []
        for ver, img in kernels:
            missing = self.missing_in_initramfs(img, ver)
            if missing:
                todo.append((ver, img))
                self.note(f"kernel {ver}: {img.name} lacks OCI boot drivers ({', '.join(missing)})")
            else:
                self.note(f"kernel {ver}: {img.name} already has virtio drivers")
        if not todo:
            return [v for v, _ in kernels], []

        # dracut only warns (and exits 0) when an add_drivers module does not exist; check ourselves first
        unavailable = {ver: self.missing_boot_drivers(ver) for ver, _ in todo}
        unavailable = {ver: names for ver, names in unavailable.items() if names}
        if unavailable and len(unavailable) == len(kernels):
            raise Fail(self.missing_driver_message(unavailable))  # no installed kernel can boot in OCI
        for ver, names in unavailable.items():
            self.note(f"kernel {ver}: this kernel does not ship {', '.join(names)} - initramfs left as is "
                      "(another installed kernel has the drivers; make sure it is the default boot entry)")
        self.unfixable = unavailable
        todo = [(ver, img) for ver, img in todo if ver not in unavailable]

        conf_dir = self.mnt / "etc" / "dracut.conf.d"
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / DRACUT_CONF_NAME).write_text(
            "# added by the OCI Ultimate Migration Tool: virtio drivers for OCI paravirtualized devices\n"
            f'add_drivers+=" {VIRTIO_DRIVERS} "\n')

        # --no-hostonly: the image must not be tailored to the helper's hardware/mounts either (dracut 004 on
        # RHEL 6 does not know the flag and is not hostonly anyway)
        helptext = self.sh(["chroot", str(self.mnt), "dracut", "--help"], ok=False)
        extra = ["--no-hostonly"] if "--no-hostonly" in (helptext.stdout + helptext.stderr) else []
        rebuilt = []
        for ver, img in todo:
            self.check_boot_space(img)
            self.note(f"rebuilding {img.name} for kernel {ver} (dracut, this takes a minute)")
            rel = "/" + str(img.relative_to(self.mnt)).replace(os.sep, "/")
            r = self.sh(["chroot", str(self.mnt), "dracut", "--force", *extra,
                         "--add-drivers", VIRTIO_DRIVERS, rel, ver], timeout_s=DRACUT_TIMEOUT_S, ok=False)
            if r.returncode != 0:
                raise Fail(f"dracut failed for kernel {ver} (rc {r.returncode}): {_tail(r.stderr or r.stdout)}")
            still_missing = self.missing_in_initramfs(img, ver)
            if still_missing:
                raise Fail(f"dracut finished for kernel {ver} but {img.name} still lacks {', '.join(still_missing)} "
                           "- check the guest's /etc/dracut.conf.d for omit_drivers/drivers settings")
            rebuilt.append(ver)
        self.sh(["sync"], ok=False)
        return [v for v, _ in kernels], rebuilt

    def activate_lvm(self) -> None:
        r = self.sh(["pvs", "--config", self.lvm_config, "--noheadings", "-o", "pv_name,vg_name"], ok=False)
        if r.returncode != 0:
            raise Fail(f"pvs failed: {_tail(r.stderr)}")
        guest_vgs = sorted({ln.split()[1] for ln in r.stdout.splitlines() if len(ln.split()) >= 2})
        if not guest_vgs:
            return
        # volume groups of the helper itself (and of anything else attached) = VGs with a PV that is not on
        # our disk; a guest VG with the same name cannot be activated alongside it
        active = self.sh(["pvs", "--noheadings", "-o", "pv_name,vg_name"], ok=False)
        foreign = {ln.split()[1] for ln in (active.stdout if active.returncode == 0 else "").splitlines()
                   if len(ln.split()) >= 2 and not ln.split()[0].startswith(self.real)}
        clash = [vg for vg in guest_vgs if vg in foreign]
        if clash:
            raise Skip(f"guest volume group '{clash[0]}' has the same name as a volume group on the migration tool VM; "
                       "it cannot be activated here - rebuild the initramfs inside the guest instead")
        for vg in guest_vgs:
            self.sh(["vgchange", "--config", self.lvm_config, "-ay", vg], timeout_s=120)
            self.vgs.append(vg)
        self.sh(["udevadm", "settle"], timeout_s=30, ok=False)
        self.note(f"activated guest volume group(s) {', '.join(guest_vgs)}")

    def add_logical_volumes(self, nodes: list[BlockNode]) -> list[BlockNode]:
        """lsblk may not show the LVs we just activated (or show them without a file system type when udev
        has not probed them yet); ask LVM itself for their device-mapper paths."""
        r = self.sh(["lvs", "--config", self.lvm_config, "--noheadings", "-o", "lv_dm_path", *self.vgs], ok=False)
        if r.returncode != 0:
            self.note(f"lvs failed ({_tail(r.stderr)}); relying on lsblk only")
            return nodes
        known = {n.path for n in nodes}
        for line in r.stdout.splitlines():
            path = line.strip()
            if path and path not in known:
                nodes.append(BlockNode(path=path, type="lvm", fstype="", uuid="", label=""))
        return nodes

    def probe_unknown_fstypes(self, nodes: list[BlockNode]) -> list[BlockNode]:
        """Fill in FSTYPE/UUID/LABEL with a direct blkid probe where lsblk (udev cache) has nothing."""
        out = []
        for n in nodes:
            if n.type in ("part", "lvm") and not n.fstype:
                r = self.sh(["blkid", "-p", "-o", "export", n.path], ok=False)
                if r.returncode == 0:
                    kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
                    n = n._replace(fstype=kv.get("TYPE", ""), uuid=kv.get("UUID", n.uuid),
                                   label=kv.get("LABEL", n.label), partuuid=kv.get("PART_ENTRY_UUID", n.partuuid),
                                   partlabel=kv.get("PART_ENTRY_NAME", n.partlabel))
                elif n.type == "lvm":
                    r2 = self.sh(["blkid", "-o", "value", "-s", "TYPE", n.path], ok=False)
                    if r2.returncode == 0 and r2.stdout.strip():
                        n = n._replace(fstype=r2.stdout.strip())
            out.append(n)
        return out

    def _root_mount_attempts(self, node: BlockNode) -> list[tuple[str, list[str]]]:
        """(optional mount -t, mount -o list) pairs to try for a root candidate."""
        if node.fstype in ROOT_FS_TYPES:
            opts = ["ro"] + (["nouuid"] if node.fstype == "xfs" else [])
            return [("", opts)]
        if node.type != "lvm":
            return []
        attempts: list[tuple[str, list[str]]] = [("", ["ro"])]  # let mount(8) auto-detect once
        for fst in ROOT_FS_TYPES:
            opts = ["ro"] + (["nouuid"] if fst == "xfs" else [])
            attempts.append((fst, opts))
        return attempts

    def find_root(self, nodes: list[BlockNode]) -> BlockNode:
        candidates: list[BlockNode] = []
        for n in nodes:
            if n.type not in ("part", "lvm", "disk"):
                continue
            if n.fstype in ("swap", "LVM2_member", "vfat", "vfat32"):
                continue
            if "swap" in Path(n.path).name.lower():
                continue
            if n.fstype in ROOT_FS_TYPES:
                candidates.append(n)
            elif n.type == "lvm" and not n.fstype:
                candidates.append(n)
        # LVs first: when both exist, the plain partition is usually /boot
        candidates.sort(key=lambda n: 0 if n.type == "lvm" else 1)
        for n in candidates:
            mount_base = len(self.mounts)
            mounted = False
            for fst, opts in self._root_mount_attempts(n):
                try:
                    self.mount(n.path, self.mnt, opts, fstype=fst)
                    mounted = True
                    break
                except Fail as exc:
                    hint = f" as {fst}" if fst else ""
                    self.note(f"cannot mount {n.path}{hint}: {exc}")
            if not mounted:
                continue
            has_fstab = (self.mnt / "etc" / "fstab").exists()
            if has_fstab:
                self.mount_split_root_filesystems(nodes)
            has_modules = self.has_kernel_modules()
            if has_fstab and has_modules:
                self.remount_guest_rw()
                return n
            top = ", ".join(sorted(p.name for p in self.mnt.iterdir())[:12])
            self.note(f"{n.path} is not the root fs (fstab={'yes' if has_fstab else 'no'}, "
                      f"lib/modules={'yes' if has_modules else 'no'}; contains: {top or 'nothing'})")
            self.pop_guest_mounts(mount_base)
        if any(n.fstype == "crypto_LUKS" for n in nodes):
            raise Skip("the guest root file system is LUKS encrypted; rebuild the initramfs inside the guest")
        if any(n.fstype == "btrfs" for n in nodes):
            raise Skip("the guest uses btrfs, which the fix-up does not handle")
        raise Skip("no Linux root file system (with /etc/fstab and /lib/modules) found on the boot disk")

    def mount_boot(self, nodes: list[BlockNode]) -> None:
        """Mount a separate /boot from the guest's fstab; nothing to do when /boot lives on the root fs."""
        if (self.mnt / "boot") in self.mounts:
            return
        for spec, mp, fstype in self.iter_fstab():
            if mp != "/boot":
                continue
            dev = self.resolve_spec(spec, nodes)
            if dev is None:
                raise Skip(f"/boot ({spec} in fstab) not found on the boot disk")
            self.mount(dev, self.mnt / "boot", self.mount_opts(dev, fstype, nodes))
            self.note(f"/boot on {dev}")
            return

    def resolve_spec(self, spec: str, nodes: list[BlockNode]) -> Optional[str]:
        """Map a guest fstab device spec onto a block node of the copied disk attached to the helper."""
        dev = self._resolve_spec_from_nodes(spec, nodes)
        if dev:
            return dev
        # RHEL anaconda often writes /dev/disk/by-id/dm-name-vg-lv or dm-uuid-LVM-...; after vgchange the
        # same symlinks exist on the helper and point at the guest logical volumes.
        m = re.match(r"^/dev/disk/by-id/dm-name-(.+)$", spec)
        if m:
            return next((n.path for n in nodes if n.type == "lvm" and Path(n.path).name == m.group(1)), None)
        if spec.startswith(("/dev/disk/by-id/", "/dev/mapper/")) and os.path.exists(spec):
            real = os.path.realpath(spec)
            if any(n.path == real for n in nodes):
                return real
        return None

    @staticmethod
    def _resolve_spec_from_nodes(spec: str, nodes: list[BlockNode]) -> Optional[str]:
        """Resolve using lsblk metadata only (no helper /dev paths).

        A guest ``/dev/sda1`` maps to partition 1 of *our* disk, never the helper's own ``/dev/sda1``.
        """
        by_attr = {"UUID": "uuid", "LABEL": "label", "PARTUUID": "partuuid", "PARTLABEL": "partlabel"}
        m = (re.match(r"^(UUID|LABEL|PARTUUID|PARTLABEL)=(.+)$", spec)
             or re.match(r"^/dev/disk/by-(uuid|label|partuuid|partlabel)/(.+)$", spec))
        if m:
            attr, value = by_attr[m.group(1).upper()], m.group(2)
            return next((n.path for n in nodes if value and getattr(n, attr) == value), None)
        m = re.match(r"^/dev/(?:mapper/([^/]+)|(?!mapper|disk)([^/]+)/([^/]+))$", spec)
        if m:  # /dev/mapper/vg-lv or /dev/vg/lv -> the LV among the lvm nodes (dm escapes '-' as '--')
            dm = m.group(1) if m.group(1) else f"{m.group(2).replace('-', '--')}-{m.group(3).replace('-', '--')}"
            return next((n.path for n in nodes if n.type == "lvm" and Path(n.path).name == dm), None)
        m = re.match(r"^/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+p)(\d+)$", spec)
        if m:  # /dev/sda2 in the guest -> partition 2 of our disk
            number = m.group(1)
            return next((n.path for n in nodes if n.type == "part" and re.search(r"(\d+)$", n.path)
                         and re.search(r"(\d+)$", n.path).group(1) == number), None)
        return None

    def installed_kernels(self) -> list[tuple[str, Path]]:
        mod_dir = next((self.mnt / p for p in ("lib/modules", "usr/lib/modules") if (self.mnt / p).is_dir()), None)
        result = []
        for d in sorted(mod_dir.iterdir()) if mod_dir else []:
            if not (d / "modules.dep").exists():
                continue  # leftovers of removed kernels
            ver = d.name
            for name in (f"initramfs-{ver}.img", f"initrd-{ver}", f"initrd.img-{ver}"):
                img = self.mnt / "boot" / name
                if img.exists():
                    result.append((ver, img))
                    break
        return result

    # ------------------------------------------------------- driver availability
    @staticmethod
    def _module_file_pattern(name: str) -> re.Pattern:
        # kernel/drivers/scsi/virtio_scsi.ko, .ko.xz, .ko.zst ... ('-' and '_' are interchangeable in names)
        stem = re.escape(name).replace("_", "[_-]")
        return re.compile(rf"(?:^|[/\s]){stem}\.ko(?:\.[A-Za-z0-9]+)?(?=\s|$)", re.M)

    def modules_dir(self, ver: str) -> Optional[Path]:
        return next((self.mnt / p / ver for p in ("lib/modules", "usr/lib/modules") if (self.mnt / p / ver).is_dir()),
                    None)

    def builtin_drivers(self, ver: str) -> set[str]:
        """Driver names compiled into the kernel (``modules.builtin``): no .ko file exists for those."""
        mod_dir = self.modules_dir(ver)
        if mod_dir is None or not (mod_dir / "modules.builtin").is_file():
            return set()
        names = set()
        for line in (mod_dir / "modules.builtin").read_text(errors="replace").splitlines():
            base = line.strip().rsplit("/", 1)[-1]
            if base.endswith(".ko"):
                names.add(base[:-3].replace("-", "_"))
        return names

    def shipped_drivers(self, ver: str) -> set[str]:
        """Driver names the guest kernel ``ver`` provides: .ko files under /lib/modules plus built-ins."""
        names = self.builtin_drivers(ver)
        mod_dir = self.modules_dir(ver)
        if mod_dir is not None:
            for p in mod_dir.rglob("*.ko*"):
                if p.is_file() and ".ko" in p.name:
                    names.add(p.name.split(".ko", 1)[0].replace("-", "_"))
        return names

    def missing_boot_drivers(self, ver: str) -> list[str]:
        """Required drivers the guest kernel ``ver`` does not ship at all (neither as .ko nor built in)."""
        shipped = self.shipped_drivers(ver)
        return [name for name in REQUIRED_BOOT_DRIVERS if name not in shipped]

    def missing_in_initramfs(self, img: Path, ver: str) -> list[str]:
        """Required drivers not in the initramfs ``img`` (built-in drivers count as present)."""
        rel = "/" + str(img.relative_to(self.mnt)).replace(os.sep, "/")
        r = self.sh(["chroot", str(self.mnt), "lsinitrd", rel], timeout_s=120, ok=False)
        if r.returncode != 0:
            return list(REQUIRED_BOOT_DRIVERS)  # no lsinitrd, or an image it cannot read: rebuild to be safe
        builtin = self.builtin_drivers(ver)
        return [name for name in REQUIRED_BOOT_DRIVERS
                if name not in builtin and not self._module_file_pattern(name).search(r.stdout)]

    def os_pretty_name(self) -> str:
        osr = self.mnt / "etc" / "os-release"
        if not osr.is_file():
            return ""
        kv = {}
        for line in osr.read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip('"').strip("'")
        return kv.get("PRETTY_NAME") or kv.get("NAME") or ""

    def missing_driver_message(self, unavailable: dict[str, list[str]]) -> str:
        names = sorted({n for ns in unavailable.values() for n in ns})
        kernels = ", ".join(unavailable)
        msg = (f"kernel {kernels} has no {', '.join(names)} driver - the OCI paravirtualized boot volume is a "
               f"virtio-scsi disk (virtio_pci + virtio_scsi + sd_mod), so the guest cannot find its root file "
               f"system without it")
        if "amazon linux" in self.os_pretty_name().lower():
            pkgs = sorted({amazon_modules_extra_package(ver) for ver in unavailable})
            return (msg + f"; Amazon Linux ships it in the {', '.join(pkgs)} package: run "
                    f"'sudo dnf install -y {' '.join(pkgs)}' on the source instance (check the kernel package name "
                    "with rpm -qa 'kernel*'), then migrate again")
        return msg + "; install the kernel package that provides it in the source VM, then migrate again"

    def check_boot_space(self, img: Path) -> None:
        try:
            free = shutil.disk_usage(img.parent).free
        except OSError:
            return
        if free < MIN_FREE_BOOT_BYTES:
            raise Fail(f"only {free // (1024 * 1024)} MB free in the guest's /boot; a non-hostonly initramfs "
                       "needs about 150 MB - free space in /boot inside the guest and migrate again")

    # ---------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        for where in reversed(list(self.mounts)):
            try:
                self.umount(where)
            except Fail as exc:
                log.warning("cannot unmount %s: %s", where, exc)
        for vg in reversed(self.vgs):
            try:
                self.sh(["vgchange", "--config", self.lvm_config, "-an", vg], ok=False)
            except Fail as exc:
                log.warning("cannot deactivate %s: %s", vg, exc)
        try:
            if self.mnt.exists():
                shutil.rmtree(self.mnt, ignore_errors=True)
        except OSError:
            pass
