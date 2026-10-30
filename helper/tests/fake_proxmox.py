"""Fake Proxmox VE for tests: the REST API on an httpx mock transport, SSH shells that answer the
node commands the migration runs, and an in-process NBD server behind the forwarded unix socket."""

from __future__ import annotations

import json
import re
import shlex
import socket
import struct
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

from helper_app.proxmox import nbd
from helper_app.proxmox.client import ProxmoxClient
from helper_app.proxmox.session import ProxmoxConnector
from helper_app.proxmox.ssh import CommandResult, SshAuthError, SshCredentials, SshError

HOST = "pve.test"
API_URL = f"https://{HOST}:8006"
TOKEN_ID = "root@pam!umt"
TOKEN_SECRET = "0b7e4c2e-token-secret"
USER = "root@pam"
PASSWORD = "pve-pass"
SSH_PASSWORD = "ssh-pass"
SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----"
NODE_IPS = {"pve1": "10.0.0.11", "pve2": "10.0.0.12"}
TICKET = "PVE:root@pam:65F00000::ticket"
CSRF = "65F00000:csrf"
BLOCK = 64 * 1024

WEB, WIN, PASS, STUCK, LOCKED = 101, 102, 103, 104, 105


@dataclass
class FakeVolume:
    volid: str
    path: str
    fmt: str
    data: bytes
    block_device: bool = False  # no hole information: qemu-img map reports everything as data


@dataclass
class FakeVm:
    vmid: int
    name: str
    node: str
    config: dict
    status: str = "stopped"
    osinfo: dict = field(default_factory=dict)
    interfaces: list = field(default_factory=list)
    ignores_acpi: bool = False
    snapshots: list = field(default_factory=list)
    ops: list = field(default_factory=list)


def map_entries(data: bytes, block_device: bool) -> list[dict]:
    if block_device:
        return [{"start": 0, "length": len(data), "depth": 0, "present": True, "zero": False, "data": True,
                 "offset": 0}]
    out: list[dict] = []
    for off in range(0, len(data), BLOCK):
        chunk = data[off:off + BLOCK]
        has = chunk != bytes(len(chunk))
        if out and out[-1]["data"] == has:
            out[-1]["length"] += len(chunk)
        else:
            out.append({"start": off, "length": len(chunk), "depth": 0, "present": has, "zero": not has,
                        "data": has})
    return out


class FakeNbdServer:
    """Serves one export to one client over a socket (fixed newstyle, simple replies)."""

    def __init__(self, sock: socket.socket, fleet: "FakeProxmox", data: bytes):
        self.sock = sock
        self.fleet = fleet
        self.data = data
        threading.Thread(target=self._run, daemon=True).start()

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise EOFError
            buf += part
        return buf

    def _run(self) -> None:
        try:
            self.sock.sendall(nbd.NBDMAGIC + struct.pack(">QH", nbd.IHAVEOPT,
                                                       nbd.FLAG_FIXED_NEWSTYLE | nbd.FLAG_NO_ZEROES))
            self._recv(4)
            magic, option, length = struct.unpack(">QII", self._recv(16))
            assert magic == nbd.IHAVEOPT and option == nbd.OPT_GO
            self._recv(length)
            info = struct.pack(">HQH", nbd.INFO_EXPORT, len(self.data), 1)
            self.sock.sendall(struct.pack(">QIII", nbd.REPLY_MAGIC, nbd.OPT_GO, nbd.REP_INFO, len(info)) + info)
            self.sock.sendall(struct.pack(">QIII", nbd.REPLY_MAGIC, nbd.OPT_GO, nbd.REP_ACK, 0))
            while True:
                magic, _flags, cmd, handle, offset, length = struct.unpack(">IHHQQI", self._recv(28))
                assert magic == nbd.REQUEST_MAGIC
                if cmd == nbd.CMD_DISC:
                    return
                if self.fleet.fail_reads:
                    self.sock.sendall(struct.pack(">IIQ", nbd.SIMPLE_REPLY_MAGIC, 5, handle))
                    continue
                with self.fleet.lock:
                    self.fleet.bytes_served += length
                    self.fleet.reads += 1
                self.sock.sendall(struct.pack(">IIQ", nbd.SIMPLE_REPLY_MAGIC, 0, handle)
                                  + self.data[offset:offset + length])
        except (EOFError, OSError, AssertionError):
            pass
        finally:
            self.sock.close()


class SocketStream:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                break
            buf += part
        return buf

    def close(self) -> None:
        self.sock.close()


class FakeShell:
    def __init__(self, fleet: "FakeProxmox", host: str, fingerprint: str):
        self.fleet = fleet
        self.host = host
        self.fingerprint = fingerprint
        self.closed = False

    def run(self, command: str, timeout: float = 300.0) -> CommandResult:
        self.fleet.commands.append((self.host, command))
        argv = shlex.split(command)
        if argv[:1] == ["sudo"]:
            argv = argv[2:]
        if argv[:2] == ["sh", "-c"]:
            script = argv[2]
            if script.startswith("for t in"):
                missing = "".join(f"missing {t}\n" for t in self.fleet.missing_tools)
                return CommandResult(0, missing, "")
            sock = re.search(r"/run/\S+?\.sock", script)
            if sock and "kill" in script:
                self.fleet.exports.pop(sock.group(0), None)
                self.fleet.stopped.append(sock.group(0))
                return CommandResult(0, "", "")
            return CommandResult(1, "", f"unexpected script {script!r}")
        if argv[:2] == ["pvesm", "path"]:
            volume = self.fleet.volumes.get(argv[2])
            if volume is None:
                return CommandResult(255, "", f"unable to parse volume ID '{argv[2]}'")
            return CommandResult(0, volume.path + "\n", "")
        if argv[:2] == ["qemu-img", "info"]:
            volume = self.fleet.by_path(argv[-1])
            if volume is None or f"-f {volume.fmt}" not in command:
                return CommandResult(1, "", "qemu-img: Could not open image")
            return CommandResult(0, json.dumps({"virtual-size": len(volume.data), "format": volume.fmt}), "")
        if argv[:2] == ["qemu-img", "map"]:
            volume = self.fleet.by_path(argv[-1])
            if volume is None:
                return CommandResult(1, "", "qemu-img: Could not open image")
            return CommandResult(0, json.dumps(map_entries(volume.data, volume.block_device)), "")
        if argv[:1] == ["qemu-nbd"]:
            opts = dict(a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a)
            volume = self.fleet.by_path(argv[-1])
            if volume is None or "--read-only" not in argv or opts.get("format") != volume.fmt:
                return CommandResult(1, "", "qemu-nbd: Failed to blk_new_open")
            self.fleet.exports[opts["socket"]] = volume
            self.fleet.started.append(opts["socket"])
            return CommandResult(0, "", "")
        return CommandResult(127, "", f"sh: {argv[0]}: not found")

    def open_unix(self, path: str):
        volume = self.fleet.exports.get(path)
        if volume is None:
            raise nbd.NbdError(f"cannot open {path} on {self.host} over SSH: No such file or directory")
        ours, theirs = socket.socketpair()
        FakeNbdServer(theirs, self.fleet, volume.data)
        return SocketStream(ours)

    def close(self) -> None:
        self.closed = True


class FakeProxmox:
    def __init__(self, vms: list[FakeVm], volumes: list[FakeVolume]):
        self.vms = {vm.vmid: vm for vm in vms}
        self.volumes = {v.volid: v for v in volumes}
        self.requests: list[str] = []
        self.commands: list[tuple[str, str]] = []
        self.ssh_logins: list[tuple[str, str, str]] = []
        self.host_keys: dict[str, str] = {}
        self.missing_tools: list[str] = []
        self.exports: dict[str, FakeVolume] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail_reads = False
        self.tickets_issued = 0
        self.bytes_served = 0
        self.reads = 0
        self.lock = threading.Lock()

    def by_path(self, path: str) -> FakeVolume | None:
        return next((v for v in self.volumes.values() if v.path == path), None)

    # ------------------------------------------------------------- factories
    def client_factory(self, base: str, **kw) -> ProxmoxClient:
        return ProxmoxClient(base, http=httpx.Client(transport=httpx.MockTransport(self.handle)), **kw)

    def shell_factory(self, host: str, creds: SshCredentials, expected: str) -> FakeShell:
        ok = creds.username == "root" and (creds.password == SSH_PASSWORD or creds.password == PASSWORD
                                           or creds.private_key == SSH_KEY)
        if not ok:
            raise SshAuthError(f"SSH login to {host} as {creds.username} was rejected")
        fingerprint = self.host_keys.get(host, f"SHA256:fake-{host}")
        if expected and expected != fingerprint:
            raise SshError(f"SSH host key of {host} changed")
        self.ssh_logins.append((host, creds.username, creds.method))
        return FakeShell(self, host, fingerprint)

    def connector(self, settings) -> ProxmoxConnector:
        return ProxmoxConnector(settings, client_factory=self.client_factory, shell_factory=self.shell_factory)

    # ------------------------------------------------------------- API
    @staticmethod
    def _ok(data) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    @staticmethod
    def _err(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"data": None, "message": message})

    def _authorized(self, request: httpx.Request) -> bool:
        auth = request.headers.get("authorization", "")
        if auth:
            return auth == f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}"
        if f"PVEAuthCookie={TICKET}" not in request.headers.get("cookie", ""):
            return False
        return request.method == "GET" or request.headers.get("csrfpreventiontoken") == CSRF

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json").rstrip("/")
        self.requests.append(f"{request.method} {path}")
        if path == "/access/ticket" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("username") != USER or form.get("password") not in (PASSWORD, TICKET):
                return self._err(401, "authentication failure")
            self.tickets_issued += 1
            return self._ok({"ticket": TICKET, "CSRFPreventionToken": CSRF, "username": USER})
        if not self._authorized(request):
            return self._err(401, "permission denied - invalid PVE ticket")
        if path == "/version":
            return self._ok({"version": "8.2.4", "release": "8.2"})
        if path == "/cluster/status":
            return self._ok([{"type": "cluster", "name": "lab"}]
                            + [{"type": "node", "name": n, "ip": ip, "online": 1} for n, ip in NODE_IPS.items()])
        if path == "/cluster/resources":
            rows = [{"id": f"qemu/{vm.vmid}", "type": "qemu", "vmid": vm.vmid, "name": vm.name, "node": vm.node,
                     "status": vm.status, "template": int(vm.config.get("template", 0)), "maxcpu": 2,
                     "maxmem": 2 << 30, "maxdisk": 2 << 20} for vm in self.vms.values()]
            rows.append({"id": "lxc/200", "type": "lxc", "vmid": 200, "name": "ct", "node": "pve1",
                         "status": "running"})
            return self._ok(rows)
        if path.startswith("/storage/"):
            return self._ok({"storage": path.split("/")[-1], "type": "dir"})
        m = re.match(r"^/nodes/([^/]+)/qemu/(\d+)(/.*)$", path)
        if not m:
            return self._err(501, f"no route {request.method} {path}")
        node, vmid, rest = m.group(1), int(m.group(2)), m.group(3)
        vm = self.vms.get(vmid)
        if vm is None or vm.node != node:
            return self._err(500, f"Configuration file 'nodes/{node}/qemu-server/{vmid}.conf' does not exist")
        if rest == "/config" and request.method == "GET":
            return self._ok(vm.config)
        if rest == "/status/current":
            return self._ok({"status": vm.status, "qmpstatus": vm.status, "vmid": vmid})
        if rest == "/status/shutdown" and request.method == "POST":
            vm.ops.append("shutdown")
            if not vm.ignores_acpi:
                vm.status = "stopped"
            return self._ok(f"UPID:{node}:0001:qmshutdown:{vmid}:root@pam:")
        if rest == "/status/stop" and request.method == "POST":
            vm.ops.append("stop")
            vm.status = "stopped"
            return self._ok(f"UPID:{node}:0002:qmstop:{vmid}:root@pam:")
        if rest == "/agent/get-osinfo":
            if vm.status != "running" or not vm.osinfo:
                return self._err(500, "QEMU guest agent is not running")
            return self._ok({"result": vm.osinfo})
        if rest == "/agent/network-get-interfaces":
            if vm.status != "running" or not vm.interfaces:
                return self._err(500, "QEMU guest agent is not running")
            return self._ok({"result": vm.interfaces})
        if rest == "/snapshot":
            return self._ok([{"name": s} for s in vm.snapshots] + [{"name": "current", "running": 0}])
        return self._err(501, f"no route {request.method} {path}")


def make_fleet(raws: dict[int, bytes]) -> FakeProxmox:
    size = {i: f"{len(r) // (1 << 20)}M" for i, r in raws.items()}
    volumes = [
        FakeVolume("local:101/vm-101-disk-0.qcow2", "/var/lib/vz/images/101/vm-101-disk-0.qcow2", "qcow2", raws[0]),
        FakeVolume("local-lvm:vm-101-disk-1", "/dev/pve/vm-101-disk-1", "raw", raws[1]),
        FakeVolume("local-zfs:vm-102-disk-0", "/dev/zvol/rpool/data/vm-102-disk-0", "raw", raws[0],
                   block_device=True),
        FakeVolume("local-lvm:vm-104-disk-0", "/dev/pve/vm-104-disk-0", "raw", raws[1]),
    ]
    vms = [
        FakeVm(WEB, "web-01", "pve1", {
            "name": "web-01", "ostype": "l26", "bios": "ovmf", "cores": 2, "sockets": 1, "memory": "4096",
            "agent": "1,fstrim_cloned_disks=1", "boot": "order=scsi0;ide2;net0", "scsihw": "virtio-scsi-single",
            "smbios1": "uuid=6b2f0d5e-1111-4c2e-9a7b-0123456789ab",
            "scsi0": f"local:101/vm-101-disk-0.qcow2,iothread=1,size={size[0]}",
            "scsi1": f"local-lvm:vm-101-disk-1,size={size[1]}",
            "ide2": "local:iso/ubuntu-22.04.iso,media=cdrom,size=1G",
            "efidisk0": "local-lvm:vm-101-disk-2,efitype=4m,pre-enrolled-keys=0,size=4M",
            "net0": "virtio=BC:24:11:AA:BB:01,bridge=vmbr0,tag=20,firewall=1",
        }, status="running",
            osinfo={"id": "ubuntu", "name": "Ubuntu", "pretty-name": "Ubuntu 22.04.4 LTS", "version-id": "22.04"},
            interfaces=[{"name": "ens18", "hardware-address": "bc:24:11:aa:bb:01",
                         "ip-addresses": [{"ip-address": "192.168.20.15", "ip-address-type": "ipv4"},
                                          {"ip-address": "fe80::1", "ip-address-type": "ipv6"}]}],
            snapshots=["before-upgrade"]),
        FakeVm(WIN, "win-01", "pve2", {
            "name": "win-01", "ostype": "win11", "bios": "seabios", "cores": 2, "sockets": 2, "memory": 8192,
            "boot": "order=sata0", "sata0": f"local-zfs:vm-102-disk-0,size={size[0]}",
            "net0": "e1000=BC:24:11:AA:BB:02,bridge=vmbr0", "tpmstate0": "local-zfs:vm-102-disk-1,size=4M",
        }),
        FakeVm(PASS, "pass-01", "pve1", {
            "name": "pass-01", "ostype": "l26", "scsi0": "/dev/disk/by-id/ata-SAMSUNG_123,size=100G",
        }),
        FakeVm(STUCK, "stuck-01", "pve1", {
            "name": "stuck-01", "ostype": "l26", "virtio0": f"local-lvm:vm-104-disk-0,size={size[1]}",
            "net0": "virtio=BC:24:11:AA:BB:04,bridge=vmbr0",
        }, status="running", ignores_acpi=True),
        FakeVm(LOCKED, "locked-01", "pve2", {
            "name": "locked-01", "ostype": "l26", "lock": "backup", "scsi0": f"local-lvm:vm-104-disk-0,size={size[1]}",
        }),
    ]
    return FakeProxmox(vms, volumes)
