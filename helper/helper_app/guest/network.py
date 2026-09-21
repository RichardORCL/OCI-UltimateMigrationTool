"""Make the copied Linux guest bring up its OCI network interface.

Why: the guest still carries VMware's network configuration.  The NIC name changes (``ens192`` on
VMware, ``ens3``/``enp0s5``/``eth0`` in OCI depending on the shape, the device model and the guest's
own kernel arguments), the old profile is bound to that name (``ifcfg-ens192``, ``interface-name=`` in
a NetworkManager keyfile, sometimes the old MAC) and it usually carries a static address of the VMware
network.  In OCI the primary VNIC's address - the fixed one from the form or the one OCI picked - is
always handed out by OCI's DHCP, so "DHCP on any Ethernet NIC" is the right configuration.

How: nothing here predicts the new name.  Every mechanism matches *any* Ethernet device or reads the
real names at the guest's first boot:

* NetworkManager: a keyfile connection of ``type=ethernet`` without ``interface-name``/MAC matches every
  Ethernet device (higher ``autoconnect-priority`` than the old profiles).
* legacy ``network-scripts`` (NetworkManager disabled): ``ifcfg-*`` files need the device name, so a
  one-shot systemd unit creates ``ifcfg-<name>`` with DHCP for every physical NIC that has none, on the
  first boot, and then disables itself.
* netplan (Ubuntu) and systemd-networkd: a drop-in matching ``e*`` / ``Type=ether`` with DHCP, plus a persistent
  override for ``systemd-networkd-wait-online`` (``--any -o routable``) so boot is not blocked ~120s waiting for
  a phantom ``eth0`` when the real OCI NIC is ``enp0s5``/``ens3``.
* ``70-persistent-net.rules`` (udev, pins ``eth0`` to the old MAC and would make the OCI NIC ``eth1``) is
  disabled.

Files written from the helper have no valid SELinux label for the guest; a confined NetworkManager
would not read them and systemd would not start the unit.  They are labelled with the *guest's own*
``setfiles`` (chroot), else with ``setfattr`` and known contexts, else the guest is told to relabel on
first boot (``/.autorelabel``).

Only the OCI copy is changed; old profiles stay in place (they cannot match the new device anyway).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Callable, Optional

from helper_app.guest.initramfs import CmdResult, Skip

log = logging.getLogger(__name__)

NM_KEYFILE = "oci-dhcp.nmconnection"
FIRSTBOOT_UNIT = "oci-umt-network-fixup.service"
FIRSTBOOT_SCRIPT = "usr/local/sbin/oci-umt-network-fixup.sh"
NETPLAN_FILE = "90-oci-dhcp.yaml"
NETWORKD_FILE = "90-oci-dhcp.network"
BACKUP_SUFFIX = ".oci-umt-bak"

# SELinux fallback labels (targeted policy) when the guest's setfiles cannot be used
SELINUX_CONTEXTS = (
    ("etc/NetworkManager/system-connections/", "system_u:object_r:NetworkManager_etc_rw_t:s0"),
    ("etc/sysconfig/network-scripts/", "system_u:object_r:net_conf_t:s0"),
    ("etc/systemd/system/", "system_u:object_r:systemd_unit_file_t:s0"),
    ("usr/local/sbin/", "system_u:object_r:bin_t:s0"),
    ("", "system_u:object_r:etc_t:s0"),
)

NM_KEYFILE_TEXT = """# added by the OCI Ultimate Migration Tool
# The VMware profile is bound to the old interface name (ens192, ...) and its static address; in OCI
# the primary VNIC's address is handed out by OCI's DHCP.  This profile matches any Ethernet device.
[connection]
id=OCI DHCP (OCI Ultimate Migration Tool)
uuid={uuid}
type=ethernet
autoconnect=true
autoconnect-priority=100

[ethernet]

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=stable-privacy
"""

FIRSTBOOT_SCRIPT_TEXT = """#!/bin/sh
# added by the OCI Ultimate Migration Tool (runs once, on the first boot in OCI)
# The network interfaces have new names in OCI; the old ifcfg files (ifcfg-ens192, ...) match nothing.
# Give every physical NIC that has no ifcfg file a DHCP configuration, then retire this unit.
scripts=/etc/sysconfig/network-scripts
for dev in /sys/class/net/*; do
  name=$(basename "$dev")
  [ "$name" = lo ] && continue
  [ -e "$dev/device" ] || continue            # physical NICs only (no bridges, veth, tunnels)
  cfg="$scripts/ifcfg-$name"
  [ -e "$cfg" ] && continue
  {
    echo "# created by the OCI Ultimate Migration Tool on the first boot in OCI"
    echo "TYPE=Ethernet"
    echo "DEVICE=$name"
    echo "NAME=$name"
    echo "BOOTPROTO=dhcp"
    echo "ONBOOT=yes"
    echo "DEFROUTE=yes"
    echo "PEERDNS=yes"
    echo "IPV6INIT=yes"
    echo "IPV6_AUTOCONF=yes"
  } > "$cfg"
  chmod 644 "$cfg"
  command -v restorecon >/dev/null 2>&1 && restorecon "$cfg"
  logger -t oci-umt-network-fixup "created $cfg (DHCP)" 2>/dev/null || true
done
systemctl disable {unit} >/dev/null 2>&1 || true
exit 0
"""

FIRSTBOOT_UNIT_TEXT = """# added by the OCI Ultimate Migration Tool
[Unit]
Description=OCI Ultimate Migration Tool: DHCP for the renamed network interfaces (first boot)
DefaultDependencies=no
After=local-fs.target systemd-udev-settle.service
Wants=systemd-udev-settle.service
Before=network-pre.target network.service NetworkManager.service

[Service]
Type=oneshot
ExecStart=/{script}

[Install]
WantedBy=multi-user.target
"""

NETPLAN_TEXT = """# added by the OCI Ultimate Migration Tool: DHCP on any Ethernet device (the name changed in OCI)
network:
  version: 2
  ethernets:
    oci-any:
      match:
        name: "e*"
      dhcp4: true
      dhcp6: false
"""

NETWORKD_TEXT = """# added by the OCI Ultimate Migration Tool: DHCP on any Ethernet device (the name changed in OCI)
[Match]
Type=ether

[Network]
DHCP=yes
"""

# Netplan generates /run/.../systemd-networkd-wait-online.service.d/10-netplan.conf that often lists eth0 plus
# the real NIC; when eth0 does not exist, wait-online hits the 120s timeout and login on the serial console is delayed.
WAIT_ONLINE_DROPIN = "99-oci-umt-wait-online.conf"
WAIT_ONLINE_TEXT = """# added by the OCI Ultimate Migration Tool
# Do not wait for every interface netplan names (often includes a missing eth0 on Azure Ubuntu images).
[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any -o routable
"""

Shell = Callable[..., CmdResult]  # session.sh(argv, timeout_s=..., ok=...)


class NetworkFixer:
    """Works on the mounted guest root ``mnt``; ``sh`` runs commands on the helper (for chroot/setfattr)."""

    def __init__(self, mnt: Path, sh: Shell, note: Callable[[str], None]):
        self.mnt = mnt
        self.sh = sh
        self.note = note
        self.changes: list[str] = []  # user facing summary
        self.written: list[str] = []  # relative paths to label for SELinux

    # ------------------------------------------------------------------ public
    def apply(self) -> tuple[str, str]:
        """Returns (status, detail) with status ``done`` or ``not_needed``; raises Skip/Fail otherwise."""
        pretty = self.os_release().get("PRETTY_NAME") or "unknown Linux"
        self.note(f"guest: {pretty}")
        self.disable_persistent_net_rules()

        nm_installed = self.first_existing("usr/lib/systemd/system/NetworkManager.service",
                                           "lib/systemd/system/NetworkManager.service") is not None
        nm_enabled = self.unit_enabled("NetworkManager.service")
        legacy_enabled = self.unit_enabled("network.service") or self.sysv_enabled("network")
        networkd_enabled = self.unit_enabled("systemd-networkd.service")
        netplan_dir = self.mnt / "etc" / "netplan"
        has_netplan = netplan_dir.is_dir() and any(netplan_dir.glob("*.yaml"))
        has_systemd = self.first_existing("usr/lib/systemd/systemd", "lib/systemd/systemd") is not None
        self.note("network stack: " + ", ".join(filter(None, [
            f"NetworkManager ({'enabled' if nm_enabled else 'installed, not enabled'})" if nm_installed else "",
            "legacy network-scripts (enabled)" if legacy_enabled else "",
            "systemd-networkd (enabled)" if networkd_enabled else "",
            "netplan" if has_netplan else "",
        ])) or "none recognised")

        handled = False
        if nm_installed and (nm_enabled or not legacy_enabled):
            self.configure_networkmanager()
            handled = True
        if legacy_enabled and not nm_enabled:
            if has_systemd:
                self.install_firstboot_unit()
            else:
                self.note("legacy network service without systemd (RHEL 6 style): ifcfg files cannot be created "
                          "without knowing the interface names; only the udev rule was handled")
            handled = True
        if has_netplan:
            self.write_netplan()
            self.write_networkd_wait_online_override()
            handled = True
        if networkd_enabled:
            self.write_networkd()
            handled = True
        if not handled:
            raise Skip("no NetworkManager, network-scripts, netplan or systemd-networkd configuration found in the "
                       "guest; configure DHCP on the new interface by hand (OCI console)")

        if self.written:
            self.label_for_selinux()
        if not self.changes:
            return "not_needed", "the guest already has a DHCP profile for any Ethernet interface"
        return "done", "; ".join(self.changes)

    # ------------------------------------------------------------------ pieces
    def disable_persistent_net_rules(self) -> None:
        rules_dir = self.mnt / "etc" / "udev" / "rules.d"
        if not rules_dir.is_dir():
            return
        for rule in sorted(rules_dir.glob("*persistent-net.rules")):
            rule.rename(rule.with_name(rule.name + BACKUP_SUFFIX))
            self.note(f"disabled udev rule {rule.name} (pinned interface names to the VMware MAC addresses)")
            self.changes.append(f"udev rule {rule.name} disabled")

    def configure_networkmanager(self) -> None:
        conn_dir = self.mnt / "etc" / "NetworkManager" / "system-connections"
        existing = self.wildcard_nm_profile(conn_dir)
        if existing:
            self.note(f"NetworkManager already has a DHCP profile for any Ethernet device ({existing})")
            return
        conn_dir.mkdir(parents=True, exist_ok=True)
        path = conn_dir / NM_KEYFILE
        path.write_text(NM_KEYFILE_TEXT.format(uuid=uuid.uuid4()))
        os.chmod(path, 0o600)  # NetworkManager refuses world-readable keyfiles
        self.written.append(self.rel(path))
        self.note(f"NetworkManager: wrote {NM_KEYFILE} (type=ethernet, no interface name, IPv4 DHCP)")
        self.changes.append("NetworkManager DHCP profile for any Ethernet interface added")

    def wildcard_nm_profile(self, conn_dir: Path) -> Optional[str]:
        """Name of an existing keyfile that already does what ours would (ethernet, any device, DHCP)."""
        if not conn_dir.is_dir():
            return None
        for kf in sorted(conn_dir.iterdir()):
            if not kf.is_file():
                continue
            try:
                text = kf.read_text(errors="replace")
            except OSError:
                continue
            keys = {m.group(1).strip(): m.group(2).strip()
                    for m in re.finditer(r"^([A-Za-z0-9_.-]+)=(.*)$", text, re.MULTILINE)}
            ipv4 = re.search(r"^\[ipv4\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
            if (re.search(r"^type=(ethernet|802-3-ethernet)\s*$", text, re.MULTILINE)
                    and "interface-name" not in keys and "mac-address" not in keys
                    and ipv4 and re.search(r"^method=auto\s*$", ipv4.group(1), re.MULTILINE)
                    and keys.get("autoconnect", "true").lower() != "false"):
                return kf.name
        return None

    def install_firstboot_unit(self) -> None:
        script = self.mnt / FIRSTBOOT_SCRIPT
        unit = self.mnt / "etc" / "systemd" / "system" / FIRSTBOOT_UNIT
        wants = self.mnt / "etc" / "systemd" / "system" / "multi-user.target.wants" / FIRSTBOOT_UNIT
        if unit.exists() and script.exists() and os.path.lexists(wants):
            self.note("first-boot network unit already installed")
            return
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(FIRSTBOOT_SCRIPT_TEXT.replace("{unit}", FIRSTBOOT_UNIT))
        os.chmod(script, 0o755)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(FIRSTBOOT_UNIT_TEXT.replace("{script}", FIRSTBOOT_SCRIPT))
        wants.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(wants):
            wants.unlink()
        os.symlink(f"/etc/systemd/system/{FIRSTBOOT_UNIT}", wants)  # what `systemctl enable` would do
        self.written += [self.rel(script), self.rel(unit), self.rel(wants)]
        self.note(f"legacy network-scripts: installed {FIRSTBOOT_UNIT} (creates ifcfg-<nic> with DHCP on the "
                  "first boot, for the real interface names)")
        self.changes.append("first-boot unit for DHCP ifcfg files installed (legacy network-scripts)")

    def write_netplan(self) -> None:
        path = self.mnt / "etc" / "netplan" / NETPLAN_FILE
        if path.exists():
            self.note("netplan drop-in already present")
            return
        path.write_text(NETPLAN_TEXT)
        os.chmod(path, 0o600)  # netplan warns about world-readable files
        self.written.append(self.rel(path))
        self.note(f"netplan: wrote {NETPLAN_FILE} (match name e*, dhcp4)")
        self.changes.append("netplan DHCP drop-in for any Ethernet interface added")

    def write_networkd(self) -> None:
        path = self.mnt / "etc" / "systemd" / "network" / NETWORKD_FILE
        if path.exists():
            self.note("systemd-networkd drop-in already present")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NETWORKD_TEXT)
        self.written.append(self.rel(path))
        self.note(f"systemd-networkd: wrote {NETWORKD_FILE} (Match Type=ether, DHCP=yes)")
        self.changes.append("systemd-networkd DHCP drop-in for any Ethernet interface added")

    def write_networkd_wait_online_override(self) -> None:
        dropin_dir = self.mnt / "etc" / "systemd" / "system" / "systemd-networkd-wait-online.service.d"
        path = dropin_dir / WAIT_ONLINE_DROPIN
        if path.exists() and WAIT_ONLINE_TEXT.strip() in path.read_text():
            self.note("systemd-networkd-wait-online override already present")
            return
        dropin_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(WAIT_ONLINE_TEXT)
        self.written.append(self.rel(path))
        self.note(f"systemd-networkd-wait-online: wrote {WAIT_ONLINE_DROPIN} (--any -o routable)")
        self.changes.append("systemd-networkd-wait-online boot delay fix (--any routable)")

    # ----------------------------------------------------------------- selinux
    def label_for_selinux(self) -> None:
        cfg = self.mnt / "etc" / "selinux" / "config"
        if not cfg.exists():
            return
        settings = dict(re.findall(r"^\s*(SELINUX|SELINUXTYPE)\s*=\s*(\S+)", cfg.read_text(errors="replace"), re.M))
        if settings.get("SELINUX", "disabled").lower() == "disabled":
            return
        policy = settings.get("SELINUXTYPE", "targeted")
        file_contexts = f"/etc/selinux/{policy}/contexts/files/file_contexts"
        setfiles = self.first_existing("sbin/setfiles", "usr/sbin/setfiles")
        paths = ["/" + p for p in self.written]
        if setfiles and (self.mnt / file_contexts.lstrip("/")).exists():
            r = self.sh(["chroot", str(self.mnt), "/" + setfiles, "-F", file_contexts, *paths], timeout_s=120, ok=False)
            if r.returncode == 0:
                self.note("SELinux labels set with the guest's setfiles")
                return
            self.note(f"guest setfiles failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-200:]}")
        ok = True
        for rel in self.written:
            ctx = next(ctx for prefix, ctx in SELINUX_CONTEXTS if rel.startswith(prefix))
            target = self.mnt / rel
            follow = ["-h"] if target.is_symlink() else []  # label the symlink itself, not its target
            r = self.sh(["setfattr", *follow, "-n", "security.selinux", "-v", ctx, str(target)], ok=False)
            ok = ok and r.returncode == 0
        if ok:
            self.note("SELinux labels set with setfattr (known contexts)")
            return
        (self.mnt / ".autorelabel").touch()
        self.note("could not set SELinux labels from the migration tool VM; the guest relabels its file system "
                  "on the first boot (/.autorelabel, takes a few minutes)")
        self.changes.append("SELinux relabel scheduled for the first boot")

    # ----------------------------------------------------------------- helpers
    def os_release(self) -> dict[str, str]:
        p = self.first_existing("etc/os-release", "usr/lib/os-release")
        if not p:
            return {}
        out = {}
        for line in (self.mnt / p).read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
        return out

    def unit_enabled(self, unit: str) -> bool:
        for target in ("multi-user.target.wants", "network-online.target.wants", "sysinit.target.wants"):
            if os.path.lexists(self.mnt / "etc" / "systemd" / "system" / target / unit):
                return True
        return False

    def sysv_enabled(self, service: str) -> bool:
        for level in ("rc3.d", "rc5.d"):
            d = self.mnt / "etc" / "rc.d" / level
            if d.is_dir() and any(p.name.startswith("S") and p.name.endswith(service) for p in d.iterdir()):
                return True
        return False

    def first_existing(self, *rel: str) -> Optional[str]:
        return next((p for p in rel if (self.mnt / p).exists()), None)

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.mnt)).replace(os.sep, "/")
