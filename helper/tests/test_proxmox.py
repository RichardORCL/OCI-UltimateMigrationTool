"""Unit tests for the Proxmox VE source: config parsing, OS mapping, the NBD client, qemu-img map
parsing, node command quoting, ticket renewal and export cleanup."""

import json
import os
import shlex

import httpx
import pytest

from helper_app.config import Settings
from helper_app.models import ProxmoxSourceInfo
from helper_app.proxmox import nbd
from helper_app.proxmox.client import ProxmoxAuthError, ProxmoxClient, ProxmoxError, parse_pve_url
from helper_app.proxmox.config_parse import parse_config, parse_size, valid_volid
from helper_app.proxmox.export import (
    SOCKET_RE,
    parse_map,
    release_proxmox_resources,
    socket_path,
    start_command,
    stop_command,
)
from helper_app.proxmox.inventory import guest_os, parse_vmid
from helper_app.proxmox.session import ProxmoxConnector

from . import fake_proxmox
from .fake_proxmox import API_URL, PASSWORD, SSH_PASSWORD, TOKEN_ID, TOKEN_SECRET, USER, WEB, make_fleet


@pytest.fixture
def fleet():
    return make_fleet({0: os.urandom(256 * 1024), 1: bytes(128 * 1024)})


def token_session(fleet):
    return ProxmoxConnector(Settings(), client_factory=fleet.client_factory, shell_factory=fleet.shell_factory).login(
        API_URL, auth_mode="token", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, ssh_password=SSH_PASSWORD)


def test_parse_config():
    cfg = parse_config({
        "name": "db", "ostype": "l26", "bios": "ovmf", "cores": 4, "sockets": 2, "vcpus": 6, "memory": "16384",
        "boot": "order=virtio1;net0", "smbios1": "uuid=abc,manufacturer=x",
        "virtio0": "ceph:vm-1-disk-0,size=10G", "virtio1": "local:1/vm-1-disk-1.qcow2,cache=writeback,size=512M",
        "sata0": "local:iso/x.iso,media=cdrom", "ide2": "none,media=cdrom",
        "unused0": "local-lvm:vm-1-disk-9", "efidisk0": "local-lvm:vm-1-disk-2,size=4M",
        "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr1,tag=30", "net1": "e1000,bridge=vmbr0",
        "hostpci0": "0000:01:00.0", "description": "hello",
    })
    assert cfg.name == "db" and cfg.uefi and cfg.cpus == 6 and cfg.memory_mb == 16384 and cfg.smbios_uuid == "abc"
    assert [d.key for d in cfg.data_disks] == ["virtio1", "virtio0"]  # boot disk first
    boot = cfg.data_disks[0]
    assert boot.fmt == "qcow2" and boot.storage == "local" and boot.size_bytes == 512 << 20
    assert cfg.data_disks[1].fmt == "raw" and cfg.data_disks[1].size_bytes == 10 << 30
    assert sum(d.is_cdrom for d in cfg.disks) == 2
    assert cfg.nics[0].mac == "bc:24:11:00:00:01" and cfg.nics[0].vlan == "30" and cfg.nics[0].bridge == "vmbr1"
    assert cfg.nics[1].model == "e1000" and cfg.nics[1].mac == ""
    assert cfg.efidisk and cfg.hostpci and cfg.unused


def test_parse_size_and_volid():
    assert parse_size("32G") == 32 << 30 and parse_size("1T") == 1 << 40 and parse_size("4096") == 4096
    assert parse_size("garbage") == 0
    assert valid_volid("local-lvm:vm-100-disk-0") and valid_volid("local:100/vm-100-disk-0.qcow2")
    for bad in ("/dev/sda", "local:../etc/passwd", "local:x;rm -rf /", "local:$(id)", ":x", "local:"):
        assert not valid_volid(bad), bad


def test_guest_os_mapping():
    linux = parse_config({"ostype": "l26"})
    assert guest_os(linux, {})[0] == "otherLinux64Guest"
    gid, full, agent = guest_os(linux, {"id": "rhel", "pretty-name": "Red Hat Enterprise Linux 9.4",
                                        "version-id": "9.4"})
    assert agent and "Red Hat" in full
    gid, full, agent = guest_os(parse_config({"ostype": "win11"}), {})
    assert full == "Microsoft Windows" and not agent


def test_parse_vmid_and_url():
    assert parse_vmid("101") == 101
    for bad in ("0", "99", "abc", "1/../2", "10000000000"):
        with pytest.raises(ProxmoxError):
            parse_vmid(bad)
    assert parse_pve_url("pve.lab") == ("https://pve.lab:8006", "pve.lab")
    assert parse_pve_url("https://10.0.0.5:8443/")[0] == "https://10.0.0.5:8443"
    with pytest.raises(ProxmoxAuthError):
        parse_pve_url("http://pve.lab")


def test_parse_map_merges_and_clips():
    rows = [{"start": 0, "length": 65536, "data": True, "present": True},
            {"start": 65536, "length": 65536, "data": True, "present": True},
            {"start": 131072, "length": 65536, "data": False, "zero": True, "present": False},
            {"start": 196608, "length": 1 << 20, "data": True, "present": True}]
    assert parse_map(json.dumps(rows), 262144) == [(0, 131072), (196608, 65536)]
    assert parse_map("[]", 100) == []


def test_node_commands_are_quoted():
    sock = socket_path("0123456789abcdef-job", 2)
    assert SOCKET_RE.match(sock) and sock == "/run/oci-umt-01234567-disk2.sock"
    assert not SOCKET_RE.match("/run/../etc/passwd") and not SOCKET_RE.match("/tmp/oci-umt-01234567-disk0.sock")
    cmd = start_command("sudo -n ", "ops", sock, "/var/lib/vz/images/1/a b;id.qcow2", "qcow2", 5)
    argv = shlex.split(cmd.split(" && ")[0])
    assert argv[:3] == ["sudo", "-n", "qemu-nbd"] and argv[-1] == "/var/lib/vz/images/1/a b;id.qcow2"
    assert "--read-only" in argv and "--shared=5" in argv and cmd.endswith(f"chown ops {sock}")
    assert "chown" not in start_command("", "root", sock, "/dev/pve/x", "raw", 3)
    stop = shlex.split(stop_command("", sock))
    assert stop[:2] == ["sh", "-c"] and "grep -q qemu-nbd" in stop[2] and sock in stop[2]


def test_nbd_client_and_pool(fleet):
    shell = fleet.shell_factory("10.0.0.11", fake_proxmox.SshCredentials("root", SSH_PASSWORD), "")
    volume = fleet.volumes["local:101/vm-101-disk-0.qcow2"]
    fleet.exports["/run/oci-umt-00000000-disk0.sock"] = volume
    client = nbd.NbdClient(shell.open_unix("/run/oci-umt-00000000-disk0.sock"))
    assert client.size == len(volume.data)
    assert client.read(4096, 8192) == volume.data[4096:12288]
    client.close()
    pool = nbd.NbdPool(lambda: nbd.NbdClient(shell.open_unix("/run/oci-umt-00000000-disk0.sock")), 2)
    assert pool.fetch(0, len(volume.data)) == volume.data
    fleet.fail_reads = True
    with pytest.raises(nbd.NbdError):
        pool.fetch(0, 512)
    pool.close()
    with pytest.raises(nbd.NbdError):
        shell.open_unix("/run/oci-umt-ffffffff-disk0.sock")


def test_ticket_renewal_and_csrf(fleet):
    now = [1000.0]
    client = ProxmoxClient(API_URL, username=USER, password=PASSWORD,
                           http=httpx.Client(transport=httpx.MockTransport(fleet.handle)), clock=lambda: now[0])
    assert client.login() == "8.2.4" and fleet.tickets_issued == 1
    client.cluster_vms()
    assert fleet.tickets_issued == 1
    now[0] += 91 * 60
    client.shutdown("pve1", WEB, 60)  # a POST: needs the CSRF token from the renewed ticket
    assert fleet.tickets_issued == 2 and fleet.vms[WEB].ops == ["shutdown"]
    bad = ProxmoxClient(API_URL, username=USER, password="nope",
                        http=httpx.Client(transport=httpx.MockTransport(fleet.handle)))
    with pytest.raises(ProxmoxAuthError):
        bad.login()


def test_host_key_is_pinned(fleet):
    session = token_session(fleet)
    session.shell("pve1")
    fleet.host_keys["10.0.0.11"] = "SHA256:other"
    session._shells.clear()
    with pytest.raises(fake_proxmox.SshError, match="host key"):
        session.shell("pve1")


def test_release_stops_leftover_exports(fleet):
    session = token_session(fleet)
    sock = socket_path("abcdef0123", 0)
    fleet.exports[sock] = fleet.volumes["local-lvm:vm-101-disk-1"]
    info = ProxmoxSourceInfo(api_host="pve.test", node="pve1", vmid=WEB, nbd_exports=[sock, "/etc/passwd"])
    results = release_proxmox_resources(session, info)
    assert results[0].startswith("ok: stopped") and results[1].startswith("ignored")
    assert info.nbd_exports == [] and fleet.exports == {}
    assert release_proxmox_resources(session, info) == []
