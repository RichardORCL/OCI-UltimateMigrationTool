"""Fake OLVM engine: OAuth, the VM API and the image-transfer proxy, via ``httpx.MockTransport``."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Optional
from urllib.parse import parse_qs

import httpx

from helper_app.olvm.client import OlvmClient
from helper_app.olvm.session import OlvmConnector

ENGINE = "https://olvm.test"
USER = "admin@internal"
PASSWORD = "secret"
CLUSTER = "Default"

WEB = "11111111-1111-1111-1111-111111111111"
WIN = "22222222-2222-2222-2222-222222222222"
HOSTED = "33333333-3333-3333-3333-333333333333"
LUN = "44444444-4444-4444-4444-444444444444"
LOCKED = "55555555-5555-5555-5555-555555555555"
MOVING = "66666666-6666-6666-6666-666666666666"


def _json(body: dict | list, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def extents_of(data: bytes) -> list[dict]:
    """Zero and non-zero runs, the shape imageio returns from ``GET /extents``."""
    if not data:
        return []
    out = []
    start = 0
    zero = data[0] == 0
    for i, byte in enumerate(data):
        if (byte == 0) != zero:
            out.append({"start": start, "length": i - start, "zero": zero})
            start = i
            zero = byte == 0
    out.append({"start": start, "length": len(data) - start, "zero": zero})
    return out


class FakeDisk:
    def __init__(self, disk_id: str, name: str, data: bytes, *, storage_type: str = "image", status: str = "ok",
                 interface: str = "virtio_scsi", bootable: bool = False):
        self.id = disk_id
        self.name = name
        self.data = data
        self.storage_type = storage_type
        self.status = status
        self.interface = interface
        self.bootable = bootable

    def attachment(self) -> dict:
        return {
            "id": self.id + "-att",
            "bootable": self.bootable,
            "interface": self.interface,
            "disk": {
                "id": self.id,
                "name": self.name,
                "provisioned_size": len(self.data),
                "status": self.status,
                "storage_type": self.storage_type,
                "format": "cow",
            },
        }


class FakeVm:
    def __init__(self, vm_id: str, name: str, disks: list[FakeDisk], *, status: str = "down",
                 os_type: str = "rhel_9x64", bios: str = "q35_ovmf", cores: int = 2,
                 memory_bytes: int = 2 * 1024**3, cluster: str = CLUSTER, origin: str = "ovirt",
                 fqdn: str = ""):
        self.id = vm_id
        self.name = name
        self.disks = disks
        self.status = status
        self.os_type = os_type
        self.bios = bios
        self.cores = cores
        self.memory_bytes = memory_bytes
        self.cluster = cluster
        self.origin = origin
        self.fqdn = fqdn
        self.ops: list[str] = []

    def doc(self) -> dict:
        body = {
            "id": self.id,
            "name": self.name,
            "href": f"/ovirt-engine/api/vms/{self.id}",
            "status": self.status,
            "memory": self.memory_bytes,
            "cpu": {"topology": {"sockets": 1, "cores": self.cores, "threads": 1}},
            "os": {"type": self.os_type},
            "bios": {"type": self.bios},
            "cluster": {"id": "cluster-1", "name": self.cluster},
            "origin": self.origin,
            "disk_attachments": {"disk_attachment": [d.attachment() for d in self.disks]},
            "nics": {"nic": [{
                "name": "nic1",
                "interface": "virtio",
                "mac": {"address": "56:6f:12:00:00:01"},
                "vnic_profile": {"name": "ovirtmgmt"},
            }]},
        }
        if self.fqdn:
            body["fqdn"] = self.fqdn
        return body


class FakeOlvm:
    def __init__(self, vms: list[FakeVm]):
        self.vms = {vm.id: vm for vm in vms}
        self.requests: list[str] = []
        self.transfers_finished: list[str] = []
        self.transfers_cancelled: list[str] = []
        self._transfers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.omit_ticket = False
        self.require_ticket = True
        self.transport = httpx.MockTransport(self.handle)

    def client_factory(self, base: str, username: str, password: str, verify_ssl: bool) -> OlvmClient:
        return OlvmClient(base, username, password, verify_ssl=verify_ssl,
                          http=httpx.Client(transport=self.transport), sleep=lambda _s: None)

    def connector(self, settings) -> OlvmConnector:
        return OlvmConnector(settings, client_factory=self.client_factory)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        self.requests.append(f"{request.method} {path}")
        if path.endswith("/oauth/token"):
            return self._token(request)
        if path == "/ovirt-engine/api":
            return _json({"product_info": {"name": "OLVM", "version": {"full_version": "4.5"}}})
        if path.startswith("/images/"):
            return self._image(request, path)
        if request.method == "GET" and path == "/ovirt-engine/api/vms":
            return _json({"vm": [vm.doc() for vm in self.vms.values()]})
        if request.method == "GET" and path.startswith("/ovirt-engine/api/vms/"):
            vm_id = path.split("/")[4]
            vm = self.vms.get(vm_id)
            if vm is None:
                return _json({"fault": {"reason": "Not Found", "detail": vm_id}}, status=404)
            return _json(vm.doc())
        if request.method == "POST" and path.startswith("/ovirt-engine/api/vms/"):
            parts = path.split("/")
            vm_id, action = parts[4], parts[5] if len(parts) > 5 else ""
            vm = self.vms.get(vm_id)
            if vm is None:
                return _json({"fault": {"reason": "Not Found"}}, status=404)
            vm.ops.append(action)
            if action in ("shutdown", "stop"):
                vm.status = "down"
            return _json({"status": "complete"})
        if path == "/ovirt-engine/api/imagetransfers" and request.method == "POST":
            return self._create_transfer(request)
        if path == "/ovirt-engine/api/imagetransfers" and request.method == "GET":
            return self._list_transfers()
        if request.method == "GET" and path.startswith("/ovirt-engine/api/disks/"):
            disk_id = path.rsplit("/", 1)[-1]
            return _json({"id": disk_id, "status": "locked" if self._disk_locked(disk_id) else "ok"})
        prefix = "/ovirt-engine/api/imagetransfers/"
        if path.startswith(prefix):
            parts = path[len(prefix):].split("/")
            transfer_id = parts[0]
            action = parts[1] if len(parts) > 1 else ""
            if request.method == "GET" and not action:
                return self._get_transfer(transfer_id)
            if request.method == "POST" and action in ("cancel", "finalize", "extend"):
                return self._action(transfer_id, action)
            if request.method == "PUT" and not action:
                return self._update_transfer(transfer_id, request)
        return _json({"fault": {"reason": "Not Found", "detail": path}}, status=404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        user = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        if user != USER or password != PASSWORD:
            return _json({"error": "invalid_grant", "error_description": "invalid user name or password"}, status=401)
        return _json({"access_token": "olvm-token", "token_type": "bearer", "expires_in": 3600})

    def _disk_locked(self, disk_id: str) -> bool:
        busy = {"initializing", "transferring", "resuming", "paused_user", "paused_system"}
        with self._lock:
            return any(item["disk_id"] == disk_id and item["phase"] in busy for item in self._transfers.values())

    def _list_transfers(self) -> httpx.Response:
        with self._lock:
            items = [{
                "id": transfer_id,
                "phase": item["phase"],
                "disk": {"id": item["disk_id"]},
                "image": {"id": item["disk_id"]},
            } for transfer_id, item in self._transfers.items()]
        return _json({"image_transfer": items})

    def _create_transfer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        disk_id = str((body.get("disk") or {}).get("id") or "")
        if self._disk_locked(disk_id):
            return _json({
                "fault": {
                    "reason": "Operation Failed",
                    "detail": "[Cannot transfer Virtual Disk: The following disks are locked: disk. "
                              "Please try again in a few minutes.]",
                },
            }, status=409)
        transfer_id = uuid.uuid4().hex
        with self._lock:
            self._transfers[transfer_id] = {"disk_id": disk_id, "phase": "initializing"}
        return _json({"id": transfer_id, "phase": "initializing", "direction": "download"}, status=201)

    def _get_transfer(self, transfer_id: str) -> httpx.Response:
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return _json({"fault": {"reason": "Not Found"}}, status=404)
            if transfer["phase"] == "initializing":
                transfer["phase"] = "transferring"
            phase = transfer["phase"]
            disk_id = transfer["disk_id"]
        body = {
            "id": transfer_id,
            "phase": phase,
            "proxy_url": f"https://olvm.test:54323/images/{transfer_id}",
            "transfer_url": f"https://host.internal:54322/images/{transfer_id}",
            "disk": {"id": disk_id},
            "image": {"id": disk_id},
        }
        if not self.omit_ticket:
            body["signed_ticket"] = f"ticket-{transfer_id}"
        return _json(body)

    def _action(self, transfer_id: str, action: str) -> httpx.Response:
        if action == "extend":
            with self._lock:
                transfer = self._transfers.get(transfer_id)
                if transfer is None:
                    return _json({"fault": {"reason": "Not Found"}}, status=404)
                phase = transfer["phase"]
            return _json({"id": transfer_id, "phase": phase})
        phase = "cancelled" if action == "cancel" else "finalizing_success"
        return self._set_phase(transfer_id, phase)

    def _update_transfer(self, transfer_id: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        return self._set_phase(transfer_id, str(body.get("phase") or ""))

    def _set_phase(self, transfer_id: str, phase: str) -> httpx.Response:
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return _json({"fault": {"reason": "Not Found"}}, status=404)
            transfer["phase"] = phase
            disk_id = transfer["disk_id"]
        if phase == "finalizing_success":
            self.transfers_finished.append(disk_id)
        elif phase == "cancelled":
            self.transfers_cancelled.append(disk_id)
        return _json({"id": transfer_id, "phase": phase})

    def _image(self, request: httpx.Request, path: str) -> httpx.Response:
        ticket = request.headers.get("authorization") or ""
        path_id = path.strip("/").split("/")[1]
        if self.require_ticket:
            if not ticket.startswith("Bearer ticket-"):
                return _json({"fault": {"reason": "Unauthorized"}}, status=401)
            transfer_id = ticket.removeprefix("Bearer ticket-")
        else:
            transfer_id = path_id
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None:
            return _json({"fault": {"reason": "Not Found"}}, status=404)
        disk = self._disk(transfer["disk_id"])
        if disk is None:
            return _json({"fault": {"reason": "Not Found"}}, status=404)
        if path.endswith("/extents"):
            return _json(extents_of(disk.data))
        header = request.headers.get("range") or ""
        if not header.startswith("bytes="):
            return httpx.Response(200, content=disk.data)
        start_s, end_s = header.removeprefix("bytes=").split("-", 1)
        start, end = int(start_s), int(end_s)
        return httpx.Response(206, content=disk.data[start:end + 1])

    def _disk(self, disk_id: str) -> Optional[FakeDisk]:
        for vm in self.vms.values():
            for disk in vm.disks:
                if disk.id == disk_id:
                    return disk
        return None


def make_fleet(raws: dict[int, bytes]) -> FakeOlvm:
    os_disk = FakeDisk("disk-web-os", "web_Disk1", raws[0], bootable=True)
    data_disk = FakeDisk("disk-web-data", "web_Disk2", raws[1], bootable=False)
    win_disk = FakeDisk("disk-win-os", "win_Disk1", raws[0], bootable=True)
    lun = FakeDisk("disk-lun", "lun_Disk1", raws[0], storage_type="lun", bootable=True)
    locked = FakeDisk("disk-locked", "locked_Disk1", raws[0], status="locked", bootable=True)
    return FakeOlvm([
        FakeVm(WEB, "web-01", [os_disk, data_disk], status="up", os_type="rhel_9x64", bios="q35_ovmf", fqdn="web-01.example.com"),
        FakeVm(WIN, "win-01", [win_disk], status="down", os_type="windows_2022", bios="i440fx_sea_bios"),
        FakeVm(HOSTED, "HostedEngine", [FakeDisk("disk-he", "he_Disk1", raws[0], bootable=True)],
               status="up", origin="hosted_engine"),
        FakeVm(LUN, "lun-01", [lun], status="down"),
        FakeVm(LOCKED, "locked-01", [locked], status="down"),
        FakeVm(MOVING, "moving-01", [FakeDisk("disk-moving", "moving_Disk1", raws[0], bootable=True)],
               status="migrating"),
    ])
