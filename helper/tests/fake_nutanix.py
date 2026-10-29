"""Fake Nutanix Prism: Prism Central (v3) or Prism Element (v2) VM, task and image APIs via ``httpx.MockTransport``."""

from __future__ import annotations

import base64
import itertools
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
DP = "/api/dataprotection/"
CLUSTER_IP = "10.0.0.9"  # Prism Element address that discover-cluster hands out
JWT = "fake-igw-session"
CLUSTER_ID = "1234567890"
INCARNATION = "1600000000000000"
CONTAINER = "default-container"
REGION_BLOCK = 64 * 1024
REGION_PAGE = 3  # regions per changed-regions answer, so callers have to page


def _json(body, status: int = 200, headers: Optional[dict] = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def data_regions(data: bytes, start: int = 0) -> list[dict]:
    """REGULAR/ZEROED regions of ``data`` from ``start`` in REGION_BLOCK steps; the zero tail is left out."""
    regions: list[dict] = []
    end = len(data.rstrip(b"\0"))
    for off in range(start - start % REGION_BLOCK, end, REGION_BLOCK):
        kind = "ZEROED" if not data[off:off + REGION_BLOCK].strip(b"\0") else "REGULAR"
        length = min(REGION_BLOCK, len(data) - off)
        if regions and regions[-1]["type"] == kind:
            regions[-1]["length"] += length
        else:
            regions.append({"offset": off, "length": length, "type": kind})
    return regions


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
    def __init__(self, vms: list[FakeVm], *, flavor: str = "pc", ranges: bool = True, regions: bool = True):
        self.vms = {vm.uuid: vm for vm in vms}
        self.flavor = flavor
        self.ranges = ranges
        self.v08_ranges = False  # Prism Element v0.8 image file path answers range requests
        self.regions = regions  # v4 recovery points (pc) / protection domains + changed_regions (pe)
        self.fail_download = False
        self.requests: list[str] = []
        self.bytes_served = 0
        self.images: dict[str, dict] = {}  # uuid -> {"name", "data"}
        self.images_created: list[str] = []
        self.images_deleted: list[str] = []
        self.recovery_points: dict[str, dict] = {}  # rp -> {"vm", "vmrp", "disks": {drp: vdisk}}
        self.recovery_points_deleted: list[str] = []
        self.protection_domains: dict[str, dict] = {}  # name -> {"vms": [uuid], "snapshots": [id]}
        self.protection_domains_deleted: list[str] = []
        self.snapshot_ids = itertools.count(70839205)
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
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        if request.url.host == CLUSTER_IP:
            if (request.headers.get("cookie") != f"NTNX_IGW_SESSION={JWT}"
                    or request.headers.get("authorization")):
                return _json({"message": "Intent Gateway Login Required"}, status=401)
            if not path.endswith("/$actions/compute-changed-regions"):
                return _json({"message": f"no cluster route {path}"}, status=404)
            return self._changed_regions_v4(path, body)
        auth = request.headers.get("authorization") or ""
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if auth != expected:
            return _json({"message_list": [{"message": "Authentication required"}]}, status=401)
        if path.startswith(V3 + "/images/") and path.endswith("/file"):
            return self._file(request, path.split("/")[5], self.ranges)
        if path.startswith("/api/nutanix/v0.8/images/") and path.endswith("/file") and self.flavor == "pe":
            return self._file(request, path.split("/")[5], self.v08_ranges)
        if path.startswith(DP) or path.startswith("/api/prism/v4.0/config/tasks/"):
            if self.flavor != "pc" or not self.regions:
                return _json({"message": "not found"}, status=404)
            return self._v4(request, path, body)
        if path == V3 + "/data/changed_regions":
            if self.flavor != "pe" or not self.regions:
                return _json({"message": "not found"}, status=404)
            return self._changed_regions_v3(body)
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

    # ------------------------------------------------------------- v4 data protection (Prism Central)
    def _v4(self, request: httpx.Request, path: str, body: dict) -> httpx.Response:
        method = request.method
        if method != "GET" and not request.headers.get("ntnx-request-id"):
            return _json({"message": "NTNX-Request-Id header is required"}, status=400)
        if method == "GET" and path.startswith("/api/prism/v4.0/config/tasks/"):
            task = self._tasks.get(path.rsplit("/", 1)[1])
            if task is None:
                return _json({"message": "task not found"}, status=404)
            return _json({"data": {"status": "SUCCEEDED", "entitiesAffected": task["entities"]}})
        rps = "/api/dataprotection/v4.0/config/recovery-points"
        if method == "POST" and path == rps:
            vm_uuid = body["vmRecoveryPoints"][0]["vmExtId"]
            vm = self.vms.get(vm_uuid)
            if vm is None or not body.get("expirationTime"):
                return _json({"message": "bad recovery point request"}, status=400)
            rp_id = str(uuid.uuid4())
            with self._lock:
                self.recovery_points[rp_id] = {
                    "vm": vm_uuid, "vmrp": str(uuid.uuid4()),
                    "disks": {str(uuid.uuid4()): d.uuid for d in vm.disks if not d.cdrom}}
            task = "ZXJnb24=:" + uuid.uuid4().hex
            with self._lock:
                self._tasks[task] = {"entities": [{"extId": vm_uuid, "rel": "vmm:ahv:config:vm"},
                                                  {"extId": rp_id, "rel": "dataprotection:config:recovery-point"}]}
            return _json({"data": {"extId": task}}, status=202)
        if path.startswith(rps + "/"):
            rp_id = path[len(rps) + 1:]
            rp = self.recovery_points.get(rp_id)
            if rp is None:
                return _json({"message": "recovery point not found"}, status=404)
            etag = f'"rp-{rp_id[:8]}"'
            if method == "GET":
                disks = [{"diskRecoveryPointExtId": drp, "diskExtId": vd} for drp, vd in rp["disks"].items()]
                return _json({"data": {"extId": rp_id, "vmRecoveryPoints": [
                    {"extId": rp["vmrp"], "vmExtId": rp["vm"], "diskRecoveryPoints": disks}]}},
                    headers={"ETag": etag})
            if method == "DELETE":
                if request.headers.get("if-match") != etag:
                    return _json({"message": "If-Match is required"}, status=428)
                with self._lock:
                    self.recovery_points.pop(rp_id, None)
                    self.recovery_points_deleted.append(rp_id)
                return _json({"data": {"extId": "ZXJnb24=:" + uuid.uuid4().hex}}, status=202)
        if method == "POST" and path.endswith("/$actions/discover-cluster"):
            if not path.startswith("/api/dataprotection/v4.3/"):
                return _json({"message": "not found"}, status=404)
            return _json({"data": {"jwtToken": JWT, "clusterIp": {"ipv4": {"value": CLUSTER_IP}}}})
        if method == "POST" and path.endswith("/$actions/compute-changed-regions"):
            return _json({"message": "send compute-changed-regions to the cluster from discover-cluster"},
                         status=400)
        return _json({"message": f"no v4 route {method} {path}"}, status=404)

    def _changed_regions_v4(self, path: str, body: dict) -> httpx.Response:
        parts = path.split("/")
        rp = self.recovery_points.get(parts[parts.index("recovery-points") + 1])
        if rp is None or rp["vmrp"] != parts[parts.index("vm-recovery-points") + 1]:
            return _json({"message": "recovery point not found"}, status=404)
        disk = self._disk(rp["disks"].get(parts[parts.index("disk-recovery-points") + 1], ""))
        if disk is None:
            return _json({"message": "disk recovery point not found"}, status=404)
        regions = data_regions(disk.data, int(body.get("offset") or 0))[:REGION_PAGE]
        return _json({"data": [{"offset": r["offset"], "length": r["length"], "regionType": r["type"]}
                               for r in regions]})

    # ------------------------------------------------------------- protection domains (Prism Element)
    def _pd_path(self, snapshot: int, disk_uuid: str) -> str:
        return (f"/{CONTAINER}/.snapshot/{snapshot % 100}/{CLUSTER_ID}-{INCARNATION}-{snapshot}"
                f"/.acropolis/vmdisk/{disk_uuid}")

    def _changed_regions_v3(self, body: dict) -> httpx.Response:
        wanted = body.get("snapshot_file_path")
        for pd in self.protection_domains.values():
            for snapshot in pd["snapshots"]:
                for vm_uuid in pd["vms"]:
                    for disk in self.vms[vm_uuid].disks:
                        if wanted == self._pd_path(snapshot, disk.uuid):
                            start = int(body.get("start_offset") or 0)
                            regions = data_regions(disk.data, start)
                            page = regions[:REGION_PAGE]
                            nxt = page[-1]["offset"] + page[-1]["length"] if len(regions) > REGION_PAGE else 0
                            return _json({"file_size": len(disk.data), "next_offset": nxt,
                                          "region_list": page})
        return _json({"message_list": [{"message": f"invalid snapshot file path {wanted}"}]}, status=400)

    def _protection_domains(self, method: str, parts: list[str], body) -> httpx.Response:
        if method == "POST" and len(parts) == 2:
            name = body["value"]
            if name in self.protection_domains:
                return _json({"message": "protection domain exists"}, status=400)
            self.protection_domains[name] = {"vms": [], "snapshots": []}
            return _json({"value": True})
        pd = self.protection_domains.get(parts[2])
        if pd is None:
            return _json({"message": "protection domain not found"}, status=404)
        action = parts[3:]
        if method == "GET" and not action:
            return _json({"name": parts[2], "vms": [{"vm_name": self.vms[u].name} for u in pd["vms"]]})
        if method == "DELETE" and not action:
            if pd["vms"]:
                return _json({"message": "protection domain still protects VMs"}, status=400)
            del self.protection_domains[parts[2]]
            self.protection_domains_deleted.append(parts[2])
            return _json({"value": True})
        if method == "POST" and action == ["protect_vms"]:
            added = [u for u in body["uuids"] if u in self.vms]
            pd["vms"].extend(added)
            return _json([{"vm_name": self.vms[u].name} for u in added])
        if method == "POST" and action == ["unprotect_vms"]:
            pd["vms"] = [u for u in pd["vms"] if self.vms[u].name not in body]
            return _json([{"vm_name": n} for n in body])
        if method == "POST" and action == ["oob_schedules"]:
            pd["snapshots"].append(next(self.snapshot_ids))
            return _json({"schedule_id": 1})
        if method == "GET" and action == ["dr_snapshots"]:
            return _json({"entities": [{"snapshot_id": str(s), "state": "AVAILABLE"} for s in pd["snapshots"]]})
        if method == "DELETE" and action[:1] == ["dr_snapshots"] and len(action) == 2:
            pd["snapshots"] = [s for s in pd["snapshots"] if str(s) != action[1]]
            return _json({"value": True})
        return _json({"message": f"no protection domain route {method} {'/'.join(parts)}"}, status=404)

    # ------------------------------------------------------------- v2
    def _v2(self, method: str, path: str, body: dict) -> httpx.Response:
        if method == "GET" and path == "/cluster":
            return _json({"name": CLUSTER, "uuid": CLUSTER_UUID, "id": f"{CLUSTER_UUID}::{CLUSTER_ID}",
                          "cluster_incarnation_id": int(INCARNATION)})
        if path.startswith("/protection_domains"):
            if not self.regions:
                return _json({"message": "not found"}, status=404)
            return self._protection_domains(method, path.split("/"), body)
        if method == "GET" and path == "/storage_containers/ctr-1":
            return _json({"name": CONTAINER, "storage_container_uuid": "ctr-1"})
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
    def _file(self, request: httpx.Request, image_uuid: str, ranges: bool) -> httpx.Response:
        image = self.images.get(image_uuid)
        if image is None:
            return _json({"message_list": [{"message": "image not found"}]}, status=404)
        if self.fail_download:
            return _json({"message_list": [{"message": "download failed"}]}, status=500)
        data = image["data"]
        header = request.headers.get("range") or ""
        if not ranges or not header.startswith("bytes="):
            if not header:  # a range probe that gets the whole body is closed after the headers
                with self._lock:
                    self.bytes_served += len(data)
            return httpx.Response(200, content=data, headers={"content-length": str(len(data))})
        start_s, end_s = header.removeprefix("bytes=").split("-", 1)
        start = int(start_s)
        end = int(end_s) if end_s else len(data) - 1
        with self._lock:
            self.bytes_served += len(data[start:end + 1])
        return httpx.Response(206, content=data[start:end + 1],
                              headers={"content-range": f"bytes {start}-{end}/{len(data)}", "accept-ranges": "bytes"})

    def _disk(self, disk_uuid: str) -> Optional[FakeDisk]:
        for vm in self.vms.values():
            for disk in vm.disks:
                if disk.uuid == disk_uuid:
                    return disk
        return None


def make_fleet(raws: dict[int, bytes], *, flavor: str = "pc", ranges: bool = True,
               regions: bool = True) -> FakeNutanix:
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
    ], flavor=flavor, ranges=ranges, regions=regions)
