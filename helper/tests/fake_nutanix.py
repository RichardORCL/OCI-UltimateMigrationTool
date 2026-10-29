"""Fake Nutanix Prism: Prism Central (v3) or Prism Element (v2) VM, task and image APIs via ``httpx.MockTransport``."""

from __future__ import annotations

import base64
import json
import threading
import uuid
from typing import Optional

import httpx

from helper_app.nutanix.client import NutanixClient
from helper_app.nutanix.session import NutanixConnector

PRISM = "prism.test"
PRISM_URL = f"https://{PRISM}:9440"
USER = "admin"
PASSWORD = "secret"
CLUSTER = "ahv-cluster-1"
CLUSTER_UUID = "c1c1c1c1-0000-0000-0000-000000000001"
SUBNET_UUID = "5b5b5b5b-0000-0000-0000-000000000001"

WEB = "aaaaaaaa-0000-0000-0000-000000000001"
WIN = "aaaaaaaa-0000-0000-0000-000000000002"
VG = "aaaaaaaa-0000-0000-0000-000000000003"
CDONLY = "aaaaaaaa-0000-0000-0000-000000000004"
STUCK = "aaaaaaaa-0000-0000-0000-000000000005"

V3 = "/api/nutanix/v3"
V2 = "/PrismGateway/services/rest/v2.0"


def _json(body, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


class FakeDisk:
    def __init__(self, disk_uuid: str, data: bytes, *, adapter: str = "SCSI", index: int = 0,
                 cdrom: bool = False, volume_group: bool = False):
        self.uuid = disk_uuid
        self.data = data
        self.adapter = adapter
        self.index = index
        self.cdrom = cdrom
        self.volume_group = volume_group

    def v3(self) -> dict:
        body = {
            "uuid": self.uuid,
            "device_properties": {"device_type": "CDROM" if self.cdrom else "DISK",
                                  "disk_address": {"adapter_type": self.adapter, "device_index": self.index}},
            "disk_size_bytes": 0 if self.cdrom else len(self.data),
            "storage_config": {"storage_container_reference": {"kind": "storage_container", "uuid": "ctr-1"}},
        }
        if self.volume_group:
            body["volume_group_reference"] = {"kind": "volume_group", "uuid": "vg-1"}
        return body

    def v2(self) -> dict:
        addr = {"device_bus": self.adapter.lower(), "device_index": self.index, "vmdisk_uuid": self.uuid}
        if self.volume_group:
            addr["volume_group_uuid"] = "vg-1"
        return {"disk_address": addr, "is_cdrom": self.cdrom, "size": 0 if self.cdrom else len(self.data),
                "storage_container_uuid": "ctr-1"}


class FakeVm:
    def __init__(self, vm_uuid: str, name: str, disks: list[FakeDisk], *, power: str = "OFF",
                 boot: str = "LEGACY", guest_os: str = "", sockets: int = 2, cores: int = 1,
                 memory_mib: int = 2048, ignores_acpi: bool = False):
        self.uuid = vm_uuid
        self.name = name
        self.disks = disks
        self.power = power
        self.boot = boot
        self.guest_os = guest_os
        self.sockets = sockets
        self.cores = cores
        self.memory_mib = memory_mib
        self.ignores_acpi = ignores_acpi
        self.spec_version = 1
        self.ops: list[str] = []

    def v3(self) -> dict:
        resources = {
            "power_state": self.power,
            "num_sockets": self.sockets,
            "num_vcpus_per_socket": self.cores,
            "memory_size_mib": self.memory_mib,
            "boot_config": {"boot_type": self.boot},
            "disk_list": [d.v3() for d in self.disks],
            "nic_list": [{"mac_address": "50:6b:8d:00:00:01", "model": "VIRTIO",
                          "subnet_reference": {"kind": "subnet", "name": "vlan0", "uuid": SUBNET_UUID},
                          "ip_endpoint_list": [{"ip": "10.0.0.15"}]}],
        }
        if self.guest_os:
            resources["guest_tools"] = {"nutanix_guest_tools": {"ngt_state": "INSTALLED",
                                                                "guest_os_version": self.guest_os}}
        cluster = {"kind": "cluster", "name": CLUSTER, "uuid": CLUSTER_UUID}
        return {
            "api_version": "3.1",
            "metadata": {"kind": "vm", "uuid": self.uuid, "spec_version": self.spec_version},
            "spec": {"name": self.name, "cluster_reference": cluster, "resources": dict(resources)},
            "status": {"name": self.name, "cluster_reference": cluster, "resources": resources},
        }

    def v2(self) -> dict:
        body = {
            "uuid": self.uuid,
            "name": self.name,
            "power_state": self.power.lower(),
            "num_vcpus": self.sockets,
            "num_cores_per_vcpu": self.cores,
            "memory_mb": self.memory_mib,
            "boot": {"uefi_boot": self.boot in ("UEFI", "SECURE_BOOT"), "secure_boot": self.boot == "SECURE_BOOT"},
            "vm_disk_info": [d.v2() for d in self.disks],
            "vm_nics": [{"mac_address": "50:6b:8d:00:00:01", "network_uuid": SUBNET_UUID, "model": "virtio",
                         "ip_address": "10.0.0.15"}],
        }
        if self.guest_os:
            body["nutanix_guest_tools"] = {"enabled": True, "installed_version": "4.0",
                                           "guest_os_version": self.guest_os}
        return body


class FakeNutanix:
    def __init__(self, vms: list[FakeVm], *, flavor: str = "pc", ranges: bool = True):
        self.vms = {vm.uuid: vm for vm in vms}
        self.flavor = flavor
        self.ranges = ranges
        self.fail_download = False
        self.requests: list[str] = []
        self.images: dict[str, dict] = {}  # uuid -> {"name", "data"}
        self.images_created: list[str] = []
        self.images_deleted: list[str] = []
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    def client_factory(self, base: str, username: str, password: str, verify_ssl: bool) -> NutanixClient:
        return NutanixClient(base, username, password, verify_ssl=verify_ssl,
                             http=httpx.Client(transport=self.transport), sleep=lambda _s: None)

    def connector(self, settings) -> NutanixConnector:
        return NutanixConnector(settings, client_factory=self.client_factory)

    # ------------------------------------------------------------- dispatch
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        self.requests.append(f"{request.method} {path}")
        if request.url.port != 9440:
            return httpx.Response(502, text="wrong port")
        auth = request.headers.get("authorization") or ""
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if auth != expected:
            return _json({"message_list": [{"message": "Authentication required"}]}, status=401)
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        if path.startswith(V3 + "/images/") and path.endswith("/file"):
            return self._file(request, path.split("/")[5])
        if path.startswith(V3):
            return self._v3(request.method, path[len(V3):], body)
        if path.startswith(V2):
            if self.flavor != "pe":
                return _json({"message": "not found"}, status=404)
            return self._v2(request.method, path[len(V2):], body)
        return _json({"message": f"no route {path}"}, status=404)

    def _task(self, entity_uuids: list[str]) -> str:
        task_uuid = uuid.uuid4().hex
        with self._lock:
            self._tasks[task_uuid] = {"entities": entity_uuids}
        return task_uuid

    def _clone(self, name: str, disk_uuid: str) -> Optional[str]:
        disk = self._disk(disk_uuid)
        if disk is None:
            return None
        image_uuid = str(uuid.uuid4())
        with self._lock:
            self.images[image_uuid] = {"name": name, "data": disk.data}
            self.images_created.append(image_uuid)
        return image_uuid

    def _delete(self, image_uuid: str) -> bool:
        with self._lock:
            if self.images.pop(image_uuid, None) is None:
                return False
            self.images_deleted.append(image_uuid)
        return True

    # ------------------------------------------------------------- v3
    def _v3(self, method: str, path: str, body: dict) -> httpx.Response:
        if method == "POST" and path == "/clusters/list":
            entities = [{"metadata": {"uuid": CLUSTER_UUID},
                         "status": {"name": CLUSTER, "resources": {"config": {"service_list": ["AOS"]}}}}]
            if self.flavor == "pc":
                entities.append({"metadata": {"uuid": "pc-0"},
                                 "status": {"name": "pc", "resources": {"config": {"service_list": ["PRISM_CENTRAL"]}}}})
            return _json({"entities": entities})
        if self.flavor != "pc":
            return _json({"message": "v3 VM API is served by Prism Central"}, status=404)
        if method == "POST" and path == "/vms/list":
            vms = list(self.vms.values())
            offset, length = int(body.get("offset") or 0), int(body.get("length") or 20)
            return _json({"entities": [vm.v3() for vm in vms[offset:offset + length]],
                          "metadata": {"total_matches": len(vms), "offset": offset, "length": length}})
        if path.startswith("/vms/"):
            vm = self.vms.get(path.split("/")[2])
            if vm is None:
                return _json({"message_list": [{"message": "VM not found"}]}, status=404)
            if method == "GET":
                return _json(vm.v3())
            if method == "PUT":
                if body["metadata"]["spec_version"] != vm.spec_version:
                    return _json({"message_list": [{"message": "spec_version mismatch"}]}, status=409)
                res = body["spec"]["resources"]
                mechanism = (res.get("power_state_mechanism") or {}).get("mechanism", "HARD")
                vm.ops.append(f"{res['power_state']}:{mechanism}")
                if res["power_state"] == "OFF" and not (mechanism == "ACPI" and vm.ignores_acpi):
                    vm.power = "OFF"
                vm.spec_version += 1
                return _json({"status": {"execution_context": {"task_uuid": self._task([vm.uuid])}}}, status=202)
        if method == "POST" and path == "/images":
            ref = body["spec"]["resources"]["data_source_reference"]
            image_uuid = self._clone(body["spec"]["name"], ref["uuid"])
            if image_uuid is None:
                return _json({"message_list": [{"message": "vm_disk not found"}]}, status=404)
            return _json({"metadata": {"uuid": image_uuid, "kind": "image"},
                          "status": {"execution_context": {"task_uuid": self._task([image_uuid])}}}, status=202)
        if method == "POST" and path == "/images/list":
            wanted = str(body.get("filter") or "").removeprefix("name==")
            with self._lock:
                rows = [{"metadata": {"uuid": u}, "spec": {"name": i["name"]}}
                        for u, i in self.images.items() if not wanted or i["name"] == wanted]
            return _json({"entities": rows})
        if path.startswith("/images/"):
            image_uuid = path.split("/")[2]
            if method == "DELETE":
                if not self._delete(image_uuid):
                    return _json({"message_list": [{"message": "image not found"}]}, status=404)
                return _json({"status": {"execution_context": {"task_uuid": self._task([image_uuid])}}}, status=202)
            if method == "GET":
                image = self.images.get(image_uuid)
                if image is None:
                    return _json({"message_list": [{"message": "image not found"}]}, status=404)
                return _json({"metadata": {"uuid": image_uuid},
                              "status": {"resources": {"size_bytes": len(image["data"])}}})
        if method == "GET" and path.startswith("/tasks/"):
            task = self._tasks.get(path.split("/")[2])
            if task is None:
                return _json({"message_list": [{"message": "task not found"}]}, status=404)
            return _json({"status": "SUCCEEDED", "percentage_complete": 100,
                          "entity_reference_list": [{"uuid": u} for u in task["entities"]]})
        return _json({"message": f"no v3 route {method} {path}"}, status=404)

    # ------------------------------------------------------------- v2
    def _v2(self, method: str, path: str, body: dict) -> httpx.Response:
        if method == "GET" and path == "/cluster":
            return _json({"name": CLUSTER, "uuid": CLUSTER_UUID})
        if method == "GET" and path == "/networks":
            return _json({"entities": [{"uuid": SUBNET_UUID, "name": "vlan0"}]})
        if method == "GET" and path == "/vms":
            return _json({"entities": [vm.v2() for vm in self.vms.values()]})
        if path.startswith("/vms/"):
            parts = path.split("/")
            vm = self.vms.get(parts[2])
            if vm is None:
                return _json({"message": "VM not found"}, status=404)
            if method == "GET" and len(parts) == 3:
                return _json(vm.v2())
            if method == "POST" and parts[3:] == ["set_power_state"]:
                transition = body["transition"]
                vm.ops.append(transition)
                if transition == "OFF" or (transition == "ACPI_SHUTDOWN" and not vm.ignores_acpi):
                    vm.power = "OFF"
                return _json({"task_uuid": self._task([vm.uuid])}, status=201)
        if method == "POST" and path == "/images":
            clone = body.get("vm_disk_clone_spec") or {}
            vmdisk = (clone.get("disk_address") or {}).get("vmdisk_uuid")
            if bool(vmdisk) == bool(body.get("image_import_spec")):
                return _json({"message": "Exactly one of VMDisk UUID or Image path needs to be provided."},
                             status=422)
            if not clone.get("storage_container_uuid"):
                return _json({"message": "storage_container_uuid is required"}, status=400)
            image_uuid = self._clone(body["name"], clone["disk_address"]["vmdisk_uuid"])
            if image_uuid is None:
                return _json({"message": "vm_disk not found"}, status=404)
            return _json({"task_uuid": self._task([image_uuid])}, status=201)
        if method == "GET" and path == "/images":
            with self._lock:
                return _json({"entities": [{"uuid": u, "name": i["name"]} for u, i in self.images.items()]})
        if path.startswith("/images/"):
            image_uuid = path.split("/")[2]
            if method == "DELETE":
                if not self._delete(image_uuid):
                    return _json({"message": "image not found"}, status=404)
                return _json({"task_uuid": self._task([image_uuid])}, status=201)
            if method == "GET":
                image = self.images.get(image_uuid)
                if image is None:
                    return _json({"message": "image not found"}, status=404)
                return _json({"uuid": image_uuid, "vm_disk_size": len(image["data"])})
        if method == "GET" and path.startswith("/tasks/"):
            task = self._tasks.get(path.split("/")[2])
            if task is None:
                return _json({"message": "task not found"}, status=404)
            return _json({"progress_status": "Succeeded", "percentage_complete": 100,
                          "entity_list": [{"entity_id": u, "entity_type": "Image"} for u in task["entities"]]})
        return _json({"message": f"no v2 route {method} {path}"}, status=404)

    # ------------------------------------------------------------- image file
    def _file(self, request: httpx.Request, image_uuid: str) -> httpx.Response:
        image = self.images.get(image_uuid)
        if image is None:
            return _json({"message_list": [{"message": "image not found"}]}, status=404)
        if self.fail_download:
            return _json({"message_list": [{"message": "download failed"}]}, status=500)
        data = image["data"]
        header = request.headers.get("range") or ""
        if not self.ranges or not header.startswith("bytes="):
            return httpx.Response(200, content=data, headers={"content-length": str(len(data))})
        start_s, end_s = header.removeprefix("bytes=").split("-", 1)
        start = int(start_s)
        end = int(end_s) if end_s else len(data) - 1
        return httpx.Response(206, content=data[start:end + 1],
                              headers={"content-range": f"bytes {start}-{end}/{len(data)}", "accept-ranges": "bytes"})

    def _disk(self, disk_uuid: str) -> Optional[FakeDisk]:
        for vm in self.vms.values():
            for disk in vm.disks:
                if disk.uuid == disk_uuid:
                    return disk
        return None


def make_fleet(raws: dict[int, bytes], *, flavor: str = "pc", ranges: bool = True) -> FakeNutanix:
    return FakeNutanix([
        FakeVm(WEB, "web-01", [
            FakeDisk("vd-web-os", raws[0], adapter="SCSI", index=0),
            FakeDisk("vd-web-data", raws[1], adapter="SCSI", index=1),
            FakeDisk("vd-web-cd", b"", adapter="IDE", index=0, cdrom=True),
        ], power="ON", boot="UEFI", guest_os="linux:64:Red Hat Enterprise Linux-9.2"),
        FakeVm(WIN, "win-01", [FakeDisk("vd-win-os", raws[0], adapter="SATA", index=0)],
               power="OFF", boot="LEGACY", guest_os="windows:64:Windows Server 2022"),
        FakeVm(VG, "vg-01", [FakeDisk("vd-vg", raws[0], volume_group=True)], power="OFF"),
        FakeVm(CDONLY, "cd-01", [FakeDisk("vd-cd", b"", adapter="IDE", cdrom=True)], power="OFF"),
        FakeVm(STUCK, "stuck-01", [FakeDisk("vd-stuck", raws[1])], power="ON", ignores_acpi=True),
    ], flavor=flavor, ranges=ranges)
