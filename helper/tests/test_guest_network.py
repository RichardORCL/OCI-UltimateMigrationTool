"""Guest network fix-up on temporary guest trees (no chroot, mounts or SELinux involved)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from helper_app.guest import network
from helper_app.guest.initramfs import CmdResult, Skip
from helper_app.guest.network import (
    WAIT_ONLINE_DROPIN,
    BACKUP_SUFFIX,
    FIRSTBOOT_SCRIPT,
    FIRSTBOOT_UNIT,
    NETPLAN_FILE,
    NETWORKD_FILE,
    NM_KEYFILE,
    NetworkFixer,
)

WANTS = "etc/systemd/system/multi-user.target.wants"


class Shell:
    """Records the helper commands the fixer runs (chroot setfiles, setfattr) with scripted results."""

    def __init__(self, setfiles_rc=0, setfattr_rc=0):
        self.calls: list[list[str]] = []
        self.setfiles_rc = setfiles_rc
        self.setfattr_rc = setfattr_rc

    def __call__(self, argv, timeout_s=120, ok=True):
        self.calls.append(argv)
        if argv[0] == "chroot":
            return CmdResult(self.setfiles_rc, "", "" if not self.setfiles_rc else "setfiles: invalid context")
        if argv[0] == "setfattr":
            return CmdResult(self.setfattr_rc, "", "")
        raise AssertionError(argv)


def guest(tmp_path: Path, *, os_name="Red Hat Enterprise Linux Server 7.9 (Maipo)", nm=True, nm_enabled=True,
          legacy=False, systemd=True, selinux="enforcing", netplan=False, networkd=False, persistent_rules=False,
          sysv_network=False) -> Path:
    mnt = tmp_path / "mnt"
    (mnt / "etc").mkdir(parents=True)
    (mnt / "etc" / "os-release").write_text(f'NAME="X"\nPRETTY_NAME="{os_name}"\n')
    if systemd:
        (mnt / "usr" / "lib" / "systemd" / "system").mkdir(parents=True)
        (mnt / "usr" / "lib" / "systemd" / "systemd").write_text("")
        (mnt / WANTS).mkdir(parents=True)
    if nm:
        (mnt / "usr" / "lib" / "systemd" / "system" / "NetworkManager.service").write_text("[Unit]")
        if nm_enabled:
            (mnt / WANTS / "NetworkManager.service").write_text("link")
        conns = mnt / "etc" / "NetworkManager" / "system-connections"
        conns.mkdir(parents=True)
        (conns / "ens192.nmconnection").write_text(
            "[connection]\nid=ens192\ntype=ethernet\ninterface-name=ens192\n\n[ipv4]\nmethod=manual\n"
            "address1=10.20.30.40/24,10.20.30.1\n")
    if legacy:
        (mnt / WANTS / "network.service").write_text("link")
        scripts = mnt / "etc" / "sysconfig" / "network-scripts"
        scripts.mkdir(parents=True)
        (scripts / "ifcfg-ens192").write_text("DEVICE=ens192\nBOOTPROTO=none\nIPADDR=10.20.30.40\nONBOOT=yes\n")
    if sysv_network:
        (mnt / "etc" / "rc.d" / "rc3.d").mkdir(parents=True)
        (mnt / "etc" / "rc.d" / "rc3.d" / "S10network").write_text("")
    if selinux:
        (mnt / "etc" / "selinux").mkdir(parents=True)
        (mnt / "etc" / "selinux" / "config").write_text(f"SELINUX={selinux}\nSELINUXTYPE=targeted\n")
        if selinux != "disabled":
            fc = mnt / "etc" / "selinux" / "targeted" / "contexts" / "files"
            fc.mkdir(parents=True)
            (fc / "file_contexts").write_text("/etc/.*  system_u:object_r:etc_t:s0\n")
            (mnt / "sbin").mkdir(exist_ok=True)
            (mnt / "sbin" / "setfiles").write_text("")
    if netplan:
        (mnt / "etc" / "netplan").mkdir()
        (mnt / "etc" / "netplan" / "00-installer-config.yaml").write_text(
            "network:\n  version: 2\n  ethernets:\n    ens192:\n      dhcp4: true\n")
    if networkd:
        (mnt / WANTS / "systemd-networkd.service").write_text("link")
    if persistent_rules:
        (mnt / "etc" / "udev" / "rules.d").mkdir(parents=True)
        (mnt / "etc" / "udev" / "rules.d" / "70-persistent-net.rules").write_text(
            'SUBSYSTEM=="net", ATTR{address}=="00:50:56:aa:bb:cc", NAME="eth0"\n')
    return mnt


@pytest.fixture
def fake_symlink(monkeypatch):
    """Windows development hosts cannot create symlinks without a privilege; record them as marker files."""
    links = {}

    def symlink(src, dst, *a, **kw):
        links[str(dst)] = src
        Path(dst).write_text(f"-> {src}")

    monkeypatch.setattr(network.os, "symlink", symlink)
    return links


def run(mnt: Path, shell: Shell | None = None):
    shell = shell or Shell()
    notes: list[str] = []
    status, detail = NetworkFixer(mnt, shell, notes.append).apply()
    return status, detail, notes, shell


def test_networkmanager_gets_wildcard_dhcp_profile(tmp_path):
    mnt = guest(tmp_path, persistent_rules=True)
    status, detail, notes, shell = run(mnt)
    assert status == "done", detail
    kf = mnt / "etc" / "NetworkManager" / "system-connections" / NM_KEYFILE
    text = kf.read_text()
    assert "type=ethernet" in text and "interface-name" not in text and "mac-address" not in text
    assert "[ipv4]\nmethod=auto" in text and "autoconnect-priority=100" in text
    assert "uuid=" in text
    if os.name != "nt":
        assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    # the old profile is untouched (it cannot match the OCI device)
    assert "interface-name=ens192" in (kf.parent / "ens192.nmconnection").read_text()
    # udev rule disabled with a backup
    rules = mnt / "etc" / "udev" / "rules.d"
    assert not (rules / "70-persistent-net.rules").exists()
    assert (rules / ("70-persistent-net.rules" + BACKUP_SUFFIX)).exists()
    assert "udev rule 70-persistent-net.rules disabled" in detail and "NetworkManager DHCP profile" in detail
    # SELinux: labelled with the guest's own setfiles, in a chroot, for exactly the file we wrote
    setfiles = [c for c in shell.calls if c[0] == "chroot"]
    assert len(setfiles) == 1 and setfiles[0][1] == str(mnt) and setfiles[0][2] == "/sbin/setfiles"
    assert "-F" in setfiles[0] and setfiles[0][-1] == "/etc/NetworkManager/system-connections/" + NM_KEYFILE
    assert not any(c[0] == "setfattr" for c in shell.calls)
    assert any("Red Hat Enterprise Linux Server 7.9" in n for n in notes)
    assert any("NetworkManager (enabled)" in n for n in notes)


def test_existing_wildcard_profile_is_enough(tmp_path):
    mnt = guest(tmp_path)
    conns = mnt / "etc" / "NetworkManager" / "system-connections"
    (conns / "Wired connection 1.nmconnection").write_text(
        "[connection]\nid=Wired connection 1\ntype=ethernet\nautoconnect=true\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=manual\n")
    status, detail, notes, shell = run(mnt)
    assert status == "not_needed", detail
    assert not (conns / NM_KEYFILE).exists()
    assert shell.calls == []  # nothing written, nothing to label
    assert any("already has a DHCP profile" in n and "Wired connection 1" in n for n in notes)
    # a profile bound to an interface, a MAC, a static address or disabled does not count
    for text in ("[connection]\ntype=ethernet\ninterface-name=eth0\n[ipv4]\nmethod=auto\n",
                 "[connection]\ntype=ethernet\n[ethernet]\nmac-address=00:11:22:33:44:55\n[ipv4]\nmethod=auto\n",
                 "[connection]\ntype=ethernet\n[ipv4]\nmethod=manual\n[ipv6]\nmethod=auto\n",
                 "[connection]\ntype=ethernet\nautoconnect=false\n[ipv4]\nmethod=auto\n"):
        (conns / "Wired connection 1.nmconnection").write_text(text)
        status, _, _, _ = run(mnt)
        assert status == "done"
        (conns / NM_KEYFILE).unlink()


def test_networkmanager_installed_but_not_enabled_without_legacy_service(tmp_path):
    # e.g. a minimal image where nothing enables NM explicitly (presets): still the manager to configure
    mnt = guest(tmp_path, nm_enabled=False)
    status, detail, notes, _ = run(mnt)
    assert status == "done" and "NetworkManager DHCP profile" in detail
    assert any("installed, not enabled" in n for n in notes)


def test_legacy_network_scripts_get_first_boot_unit(tmp_path, fake_symlink):
    mnt = guest(tmp_path, os_name="Oracle Linux Server 7.9", nm=True, nm_enabled=False, legacy=True)
    status, detail, notes, shell = run(mnt)
    assert status == "done", detail
    assert "first-boot unit" in detail and "NetworkManager" not in detail  # NM disabled: not configured
    script = mnt / FIRSTBOOT_SCRIPT
    unit = mnt / "etc" / "systemd" / "system" / FIRSTBOOT_UNIT
    assert script.exists() and unit.exists()
    text = script.read_text()
    assert text.startswith("#!/bin/sh") and "BOOTPROTO=dhcp" in text and '[ -e "$dev/device" ] || continue' in text
    assert f"systemctl disable {FIRSTBOOT_UNIT}" in text
    if os.name != "nt":
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert f"ExecStart=/{FIRSTBOOT_SCRIPT}" in unit.read_text()
    assert "Before=network-pre.target network.service" in unit.read_text()
    # enabled the way systemctl enable would: wants symlink to the unit
    assert fake_symlink == {str(mnt / WANTS / FIRSTBOOT_UNIT): f"/etc/systemd/system/{FIRSTBOOT_UNIT}"}
    # all three labelled
    setfiles = [c for c in shell.calls if c[0] == "chroot"][0]
    assert set(setfiles[-3:]) == {"/" + FIRSTBOOT_SCRIPT, f"/etc/systemd/system/{FIRSTBOOT_UNIT}",
                                  f"/{WANTS}/{FIRSTBOOT_UNIT}"}
    # the old ifcfg stays
    assert (mnt / "etc" / "sysconfig" / "network-scripts" / "ifcfg-ens192").exists()
    # second run: nothing new
    status, detail, _, _ = run(mnt)
    assert status == "not_needed"


def test_rhel6_style_without_systemd_is_explained(tmp_path):
    mnt = guest(tmp_path, os_name="Red Hat Enterprise Linux Server 6.10", nm=False, systemd=False,
                sysv_network=True, persistent_rules=True, selinux="")
    status, detail, notes, _ = run(mnt)
    assert status == "done" and detail == "udev rule 70-persistent-net.rules disabled"
    assert any("RHEL 6 style" in n for n in notes)


def test_netplan_and_networkd(tmp_path):
    mnt = guest(tmp_path, os_name="Ubuntu 22.04.3 LTS", nm=False, netplan=True, selinux="")
    status, detail, _, shell = run(mnt)
    assert status == "done" and "netplan" in detail
    yaml = (mnt / "etc" / "netplan" / NETPLAN_FILE).read_text()
    assert 'name: "e*"' in yaml and "dhcp4: true" in yaml
    assert (mnt / "etc" / "netplan" / "00-installer-config.yaml").exists()
    wait = mnt / "etc" / "systemd" / "system" / "systemd-networkd-wait-online.service.d" / WAIT_ONLINE_DROPIN
    assert wait.exists() and "--any -o routable" in wait.read_text()
    assert "systemd-networkd-wait-online boot delay fix" in detail
    assert shell.calls == []  # no SELinux on this guest

    mnt = guest(tmp_path / "b", os_name="openSUSE Leap 15.5", nm=False, networkd=True, selinux="")
    status, detail, _, _ = run(mnt)
    assert status == "done" and "systemd-networkd" in detail
    text = (mnt / "etc" / "systemd" / "network" / NETWORKD_FILE).read_text()
    assert "Type=ether" in text and "DHCP=yes" in text


def test_unknown_stack_is_skipped(tmp_path):
    mnt = guest(tmp_path, nm=False, selinux="")
    with pytest.raises(Skip, match="no NetworkManager, network-scripts, netplan"):
        run(mnt)


def test_selinux_fallbacks(tmp_path):
    # guest setfiles fails -> setfattr with known contexts
    mnt = guest(tmp_path)
    status, _, notes, shell = run(mnt, Shell(setfiles_rc=1))
    assert status == "done"
    setfattr = [c for c in shell.calls if c[0] == "setfattr"]
    assert len(setfattr) == 1 and "system_u:object_r:NetworkManager_etc_rw_t:s0" in setfattr[0]
    assert any("guest setfiles failed" in n for n in notes) and any("setfattr" in n for n in notes)
    assert not (mnt / ".autorelabel").exists()
    # both fail -> full relabel on first boot
    mnt = guest(tmp_path / "b")
    status, detail, notes, _ = run(mnt, Shell(setfiles_rc=1, setfattr_rc=1))
    assert status == "done" and (mnt / ".autorelabel").exists()
    assert "SELinux relabel scheduled" in detail
    # SELinux disabled in the guest: nothing to label
    mnt = guest(tmp_path / "c", selinux="disabled")
    status, _, _, shell = run(mnt)
    assert status == "done" and shell.calls == []
    # no setfiles in the guest: straight to setfattr
    mnt = guest(tmp_path / "d")
    (mnt / "sbin" / "setfiles").unlink()
    status, _, _, shell = run(mnt)
    assert [c[0] for c in shell.calls] == ["setfattr"]
