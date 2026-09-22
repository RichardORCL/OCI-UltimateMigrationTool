"""Prepare a copied EC2 Linux guest for first boot in OCI (offline, on the helper VM).

Besides the cloud-init / agent cleanup this also makes the first boot *visible*: Amazon Linux boots with
``quiet rd.shell=0 rd.emergency=poweroff``, so a guest that cannot find its root disk in OCI prints nothing
on the serial console and silently powers itself off after the dracut timeout.  Those options are dropped
from every place the kernel command line lives (BLS entries, grubenv, grub.cfg, /etc/default/grub,
/etc/kernel/cmdline) and ``earlycon`` is added so even a kernel that dies before its console is up leaves a
trace on the OCI serial console.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from helper_app.branding import PREFIX
from helper_app.guest.initramfs import Skip

CLOUD_CFG_DROPIN = f"99-{PREFIX}-after-aws.cfg"
CLOUD_CFG_TEXT = """# added by the OCI Ultimate Migration Tool after migration from Amazon EC2
datasource_list: [ Oracle, NoCloud, ConfigDrive, None ]
# the migrated server keeps its SSH host identity (cloud-init sees a new instance-id after the move)
ssh_deletekeys: false
"""

# kernel arguments that hide a failing first boot in OCI
REMOVE_KERNEL_ARGS = ("quiet", "rd.shell=0", "rd.emergency=poweroff", "rd.emergency=reboot", "rd.emergency=halt")
# the OCI serial console is the first 16550 UART on every x86 shape
EARLYCON_ARG = "earlycon=uart8250,io,0x3f8,115200"
ADD_KERNEL_ARGS = (EARLYCON_ARG,)
GRUBENV_SIZE = 1024
_GRUB_DEFAULT_LINE = re.compile(r'^(GRUB_CMDLINE_LINUX(?:_DEFAULT)?=)(["\']?)(.*)\2\s*$')
_GRUB_CFG_LINUX_LINE = re.compile(r"^(\s*linux(?:efi|16)?\s+\S+)(\s+.*)?$")
_BLS_OPTIONS_LINE = re.compile(r"^options\s+(.*)$")


def adjust_kernel_args(args: str) -> tuple[str, bool]:
    """Drop the silencing arguments and add earlycon; ``(new args, changed)``.  Token order is kept and
    opaque tokens such as ``$kernelopts`` survive untouched."""
    tokens = args.split()
    out = [t for t in tokens if t not in REMOVE_KERNEL_ARGS]
    for arg in ADD_KERNEL_ARGS:
        key = arg.split("=", 1)[0] + "="
        if not any(t == arg or t.startswith(key) for t in out):
            out.append(arg)
    return " ".join(out), out != tokens


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
        self.fix_kernel_options()
        if not self.changes:
            return "not_needed", "no EC2-specific boot configuration found to adjust"
        return "done", "; ".join(self.changes)

    # ------------------------------------------------------------ kernel command line
    def fix_kernel_options(self) -> None:
        """Make the first boot talk on the OCI serial console (see the module docstring)."""
        edited: list[str] = []
        for path in sorted((self.mnt / "boot" / "loader" / "entries").glob("*.conf")):
            if self._edit_lines(path, self._edit_bls_line):
                edited.append(f"loader/entries/{path.name}")
        for rel in ("boot/grub2/grubenv", "boot/grub/grubenv"):
            if self._edit_grubenv(self.mnt / rel):
                edited.append(rel.split("/", 1)[1])
        for rel in ("boot/grub2/grub.cfg", "boot/grub/grub.cfg"):
            if self._edit_lines(self.mnt / rel, self._edit_grub_cfg_line):
                edited.append(rel.split("/", 1)[1])
        if self._edit_lines(self.mnt / "etc" / "default" / "grub", self._edit_default_grub_line):
            edited.append("/etc/default/grub")
        if self._edit_lines(self.mnt / "etc" / "kernel" / "cmdline", self._edit_cmdline_line):
            edited.append("/etc/kernel/cmdline")
        if not edited:
            return
        removed = ", ".join(REMOVE_KERNEL_ARGS[:3])
        self.note(f"kernel boot options: removed {removed}; added {EARLYCON_ARG} in {', '.join(edited)}")
        self.changes.append(f"kernel boot options made visible on the OCI serial console ({len(edited)} file(s))")

    @staticmethod
    def _edit_bls_line(line: str) -> str:
        m = _BLS_OPTIONS_LINE.match(line)
        if not m:
            return line
        new, changed = adjust_kernel_args(m.group(1))
        return f"options {new}" if changed else line

    @staticmethod
    def _edit_cmdline_line(line: str) -> str:
        if not line.strip() or line.lstrip().startswith("#"):
            return line
        new, changed = adjust_kernel_args(line)
        return new if changed else line

    @staticmethod
    def _edit_grub_cfg_line(line: str) -> str:
        m = _GRUB_CFG_LINUX_LINE.match(line)
        if not m or not m.group(2):
            return line
        new, changed = adjust_kernel_args(m.group(2))
        return f"{m.group(1)} {new}" if changed else line

    @staticmethod
    def _edit_default_grub_line(line: str) -> str:
        m = _GRUB_DEFAULT_LINE.match(line)
        if not m:
            return line
        new, changed = adjust_kernel_args(m.group(3))
        quote = m.group(2) or '"'
        return f"{m.group(1)}{quote}{new}{quote}" if changed else line

    def _edit_lines(self, path: Path, edit: Callable[[str], str]) -> bool:
        """Apply ``edit`` to every line of ``path`` (same inode, so SELinux labels and mode survive)."""
        if not path.is_file():
            return False
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            self.note(f"cannot read {path.relative_to(self.mnt)}: {exc}")
            return False
        lines = text.split("\n")
        new_lines = [edit(ln) for ln in lines]
        if new_lines == lines:
            return False
        path.write_text("\n".join(new_lines))
        return True

    def _edit_grubenv(self, path: Path) -> bool:
        """grubenv is a fixed 1024-byte block padded with ``#``; rewrite ``kernelopts=`` and re-pad."""
        if not path.is_file():
            return False
        raw = path.read_bytes()
        text = raw.decode(errors="replace").rstrip("#")
        lines = text.split("\n")
        changed = False
        for i, ln in enumerate(lines):
            if ln.startswith("kernelopts="):
                new, ch = adjust_kernel_args(ln[len("kernelopts="):])
                if ch:
                    lines[i] = "kernelopts=" + new
                    changed = True
        if not changed:
            return False
        body = "\n".join(lines)
        if not body.endswith("\n"):
            body += "\n"
        if len(body.encode()) > GRUBENV_SIZE:
            self.note(f"{path.relative_to(self.mnt)} left unchanged: the new kernelopts would exceed the "
                      f"{GRUBENV_SIZE}-byte grubenv block")
            return False
        path.write_bytes(body.encode() + b"#" * (GRUBENV_SIZE - len(body.encode())))
        return True

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
            if not path.is_file() or path.name == CLOUD_CFG_DROPIN:
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
