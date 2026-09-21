"""Fake Azure: Entra ID token endpoint, the ARM compute/disk endpoints the migration tool uses, and the page
blob behind an export SAS - all served through an ``httpx.MockTransport``."""

from __future__ import annotations

import json
import threading
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from helper_app.azure.client import AzureClient
from helper_app.azure.session import AzureConnector

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "s3cret~value"
SUB = "00000000-0000-0000-0000-000000000001"
SUB_NAME = "Prod Subscription"
BLOB_HOST = "https://md-fake.z1.blob.storage.azure.net"
PAGE = 512


def rid(kind: str, name: str, rg: str = "rg-prod", sub: str = SUB) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/{kind}/{name}"


class FakeDisk:
    def __init__(self, name: str, data: bytes, os_type: Optional[str] = None, gen: str = "V2", rg: str = "rg-prod",
                 ade: bool = False, network_policy: str = "AllowAll", size_gb: Optional[int] = None):
        self.id = rid("disks", name, rg)
        self.name = name
        self.data = data
        self.os_type = os_type
        self.gen = gen
        self.rg = rg
        self.ade = ade
        self.network_policy = network_policy
        self.size_gb = size_gb if size_gb is not None else max(1, -(-len(data) // 1024**3))
        self.managed_by: Optional[str] = None
        self.sas_token: Optional[str] = None

    def doc(self) -> dict:
        props = {"diskSizeGB": self.size_gb, "diskSizeBytes": len(self.data), "hyperVGeneration": self.gen,
                 "provisioningState": "Succeeded", "networkAccessPolicy": self.network_policy,
                 "publicNetworkAccess": "Enabled", "diskState": "ActiveSAS" if self.sas_token else
                 ("Attached" if self.managed_by else "Unattached"), "encryption": {"type": "EncryptionAtRestWithPlatformKey"}}
        if self.os_type:
            props["osType"] = self.os_type
        if self.ade:
            props["encryptionSettingsCollection"] = {"enabled": True, "encryptionSettings": [{}]}
        if self.managed_by:
            props["managedBy"] = self.managed_by
        return {"id": self.id, "name": self.name, "location": "westeurope", "type": "Microsoft.Compute/disks",
                "sku": {"name": "Premium_LRS"}, "properties": props}


class FakeSnapshot:
    def __init__(self, snap_id: str, source: FakeDisk, body: dict):
        self.id = snap_id
        self.name = snap_id.rsplit("/", 1)[-1]
        self.data = bytes(source.data)  # frozen copy
        self.body = body
        self.sas_token: Optional[str] = None
        self.polls = 0

    def doc(self) -> dict:
        return {"id": self.id, "name": self.name, "location": self.body.get("location"), "tags": self.body.get("tags"),
                "type": "Microsoft.Compute/snapshots",
                "properties": {"provisioningState": "Succeeded", "diskSizeBytes": len(self.data),
                               "creationData": self.body["properties"]["creationData"], "incremental": True,
                               "diskState": "ActiveSAS" if self.sas_token else "Unattached"}}


class FakeVm:
    def __init__(self, name: str, os_disk: FakeDisk, data_disks: list[FakeDisk] = (), *, rg: str = "rg-prod",
                 size: str = "Standard_D2s_v3", power: str = "running", image: Optional[dict] = None,
                 security: Optional[str] = None, secure_boot: bool = False, os_name: str = "", os_version: str = "",
                 ephemeral: bool = False, unmanaged: bool = False):
        self.id = rid("virtualMachines", name, rg)
        self.name = name
        self.rg = rg
        self.size = size
        self.power = power
        self.os_disk = os_disk
        self.data_disks = list(data_disks)
        self.image = image
        self.security = security
        self.secure_boot = secure_boot
        self.os_name = os_name
        self.os_version = os_version
        self.ephemeral = ephemeral
        self.unmanaged = unmanaged
        self.ops: list[str] = []
        for d in [os_disk, *data_disks]:
            d.managed_by = self.id

    @property
    def disks(self) -> list[FakeDisk]:
        return [self.os_disk, *self.data_disks]

    def doc(self) -> dict:
        def disk_ref(d: FakeDisk, extra: dict) -> dict:
            ref = {"name": d.name, "diskSizeGB": d.size_gb, "caching": "ReadWrite", "createOption": "FromImage"}
            if self.unmanaged:
                ref["vhd"] = {"uri": f"https://legacy.blob.core.windows.net/vhds/{d.name}.vhd"}
            else:
                ref["managedDisk"] = {"id": d.id, "storageAccountType": "Premium_LRS"}
            ref.update(extra)
            return ref

        os_ref = disk_ref(self.os_disk, {"osType": self.os_disk.os_type or "Linux"})
        if self.ephemeral:
            os_ref["diffDiskSettings"] = {"option": "Local", "placement": "CacheDisk"}
        if self.os_disk.ade:
            os_ref["encryptionSettings"] = {"enabled": True}
        storage = {"imageReference": self.image or {}, "osDisk": os_ref,
                   "dataDisks": [disk_ref(d, {"lun": i}) for i, d in enumerate(self.data_disks)],
                   "diskControllerType": "SCSI"}
        props = {"vmId": f"vmid-{self.name}", "hardwareProfile": {"vmSize": self.size}, "storageProfile": storage,
                 "osProfile": {"computerName": self.name},
                 "networkProfile": {"networkInterfaces": [{"id": rid("networkInterfaces", f"{self.name}-nic", self.rg)
                                                            .replace("Microsoft.Compute", "Microsoft.Network")}]},
                 "provisioningState": "Succeeded",
                 "instanceView": {"hyperVGeneration": self.os_disk.gen, "osName": self.os_name,
                                  "osVersion": self.os_version,
                                  "statuses": [{"code": "ProvisioningState/succeeded"},
                                               {"code": f"PowerState/{self.power}", "displayStatus": f"VM {self.power}"}]}}
        if self.security:
            props["securityProfile"] = {"securityType": self.security,
                                        "uefiSettings": {"secureBootEnabled": self.secure_boot,
                                                         "vTpmEnabled": self.secure_boot}}
        return {"id": self.id, "name": self.name, "location": "westeurope", "type": "Microsoft.Compute/virtualMachines",
                "properties": props}


SIZES = {"Standard_D2s_v3": {"name": "Standard_D2s_v3", "numberOfCores": 2, "memoryInMB": 8192},
         "Standard_D4s_v3": {"name": "Standard_D4s_v3", "numberOfCores": 4, "memoryInMB": 16384},
         "Standard_B1ms": {"name": "Standard_B1ms", "numberOfCores": 1, "memoryInMB": 2048}}


def _xml_page_list(ranges: list[tuple[int, int]]) -> str:
    items = "".join(f"<PageRange><Start>{s}</Start><End>{e}</End></PageRange>" for s, e in ranges)
    return f'<?xml version="1.0" encoding="utf-8"?><PageList>{items}</PageList>'


def allocated_pages(data: bytes) -> list[tuple[int, int]]:
    """Azure reports the pages that were ever written; the fake treats non-zero 512-byte pages as written."""
    out: list[tuple[int, int]] = []
    for off in range(0, len(data), PAGE):
        page = data[off:off + PAGE]
        if not any(page):
            continue
        if out and out[-1][1] + 1 == off:
            out[-1] = (out[-1][0], off + len(page) - 1)
        else:
            out.append((off, off + len(page) - 1))
    return out


class FakeAzure:
    """State + request handler.  ``token_ok`` gates the token endpoint, ``fail_ranges`` lists blob ranges whose
    first GET fails (``(offset, status)``), ``block_event`` stalls every range GET until set."""

    def __init__(self, vms: list[FakeVm], subscriptions: Optional[list[dict]] = None):
        self.vms: dict[str, FakeVm] = {vm.id.lower(): vm for vm in vms}
        self.disks: dict[str, FakeDisk] = {d.id.lower(): d for vm in vms for d in vm.disks}
        self.snapshots: dict[str, FakeSnapshot] = {}
        self.subscriptions = subscriptions if subscriptions is not None else [
            {"id": f"/subscriptions/{SUB}", "subscriptionId": SUB, "displayName": SUB_NAME, "state": "Enabled"}]
        self.tokens_issued = 0
        self.requests: list[str] = []
        self.fail_ranges: dict[int, int] = {}
        self.block_event: Optional[threading.Event] = None
        self.deallocate_polls = 2  # 202 answers before a deallocate reports Succeeded
        self.sas_granted: list[str] = []  # every beginGetAccess (resource id)
        self.sas_revoked: list[str] = []
        self.snapshots_created: list[str] = []
        self.snapshots_deleted: list[str] = []
        self._ops: dict[str, dict] = {}  # async operation url -> state
        self._lock = threading.Lock()
        self._sas_counter = 0
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------ plumbing
    def http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport)

    def client_factory(self, tenant: str, client_id: str, secret: str) -> AzureClient:
        return AzureClient(tenant, client_id, secret, http=self.http(), sleep=lambda s: None)

    def connector(self, settings) -> AzureConnector:
        return AzureConnector(settings, client_factory=self.client_factory)

    def add_vm(self, vm: FakeVm) -> FakeVm:
        self.vms[vm.id.lower()] = vm
        for d in vm.disks:
            self.disks[d.id.lower()] = d
        return vm

    def blob_for(self, token: str):
        for res in [*self.disks.values(), *self.snapshots.values()]:
            if res.sas_token == token:
                return res
        return None

    # ------------------------------------------------------------- handler
    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path
        query = parse_qs(url.query)
        self.requests.append(f"{request.method} {path}")
        if url.netloc == "login.microsoftonline.com":
            return self._token(request, path)
        if url.netloc == urlsplit(BLOB_HOST).netloc:
            return self._blob(request, path, query)
        if url.netloc != "management.azure.com":
            return httpx.Response(502, text=f"unexpected host {url.netloc}")
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer tok-"):
            return self._err(401, "InvalidAuthenticationToken", "The access token is invalid.")
        with self._lock:
            return self._arm(request, path, query)

    @staticmethod
    def _err(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message}})

    def _token(self, request: httpx.Request, path: str) -> httpx.Response:
        form = parse_qs(request.content.decode())
        tenant = path.split("/")[1]
        if tenant != TENANT:
            return httpx.Response(400, json={"error": "invalid_request", "error_description":
                                             f"AADSTS90002: Tenant '{tenant}' not found. Check to make sure you have "
                                             "the correct tenant ID. Trace ID: x"})
        if form.get("client_id", [""])[0] != CLIENT_ID:
            return httpx.Response(401, json={"error": "unauthorized_client", "error_description":
                                             "AADSTS700016: Application with identifier 'x' was not found in the "
                                             "directory 'Contoso'. Trace ID: y"})
        if form.get("client_secret", [""])[0] != SECRET:
            return httpx.Response(401, json={"error": "invalid_client", "error_description":
                                             "AADSTS7000215: Invalid client secret provided. Ensure the secret being "
                                             "sent in the request is the client secret value. Trace ID: z"})
        self.tokens_issued += 1
        return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599,
                                         "access_token": f"tok-{self.tokens_issued}"})

    # -- ARM
    def _arm(self, request: httpx.Request, path: str, query: dict) -> httpx.Response:
        m = request.method
        low = path.lower()
        if path in self._ops:
            return self._poll(path)
        if m == "GET" and low == "/subscriptions":
            return httpx.Response(200, json={"value": self.subscriptions})
        if m == "GET" and low.endswith("/providers/microsoft.compute/virtualmachines"):
            sub = path.split("/")[2]
            if "$expand" in query:
                return httpx.Response(400, json={"error": {"code": "BadRequest",
                                                           "message": "Expand Instance View is only supported when "
                                                           "Virtual Machine Scale Set resource filter is applied"}})
            vms = [vm.doc() for vm in self.vms.values() if vm.id.split("/")[2] == sub]
            for doc in vms:
                doc["properties"].pop("instanceView", None)
            return httpx.Response(200, json={"value": vms})
        if m == "GET" and "/providers/microsoft.compute/locations/" in low and low.endswith("/vmsizes"):
            return httpx.Response(200, json={"value": list(SIZES.values())})
        vm_path = low.removesuffix("/deallocate").removesuffix("/instanceview")
        vm = self.vms.get(vm_path)
        if vm is not None and m == "GET" and low.endswith("/instanceview"):
            return httpx.Response(200, json=vm.doc()["properties"]["instanceView"])
        if vm is not None:
            if m == "GET":
                return httpx.Response(200, json=vm.doc())
            if m == "POST" and low.endswith("/deallocate"):
                vm.ops.append("deallocate")
                vm.power = "deallocating"
                return self._start_op(f"/ops/deallocate-{vm.name}-{len(vm.ops)}", self.deallocate_polls,
                                      on_done=lambda: setattr(vm, "power", "deallocated"), style="async")
        res: Optional[FakeDisk | FakeSnapshot] = None
        base = low
        for suffix in ("/begingetaccess", "/endgetaccess"):
            if low.endswith(suffix):
                base = low[: -len(suffix)]
        if "/providers/microsoft.compute/disks/" in base:
            res = self.disks.get(base)
        elif "/providers/microsoft.compute/snapshots/" in base:
            res = self.snapshots.get(base)
            if m == "PUT" and res is None:
                body = json.loads(request.content)
                src = self.disks.get(body["properties"]["creationData"]["sourceResourceId"].lower())
                if src is None:
                    return self._err(404, "NotFound", "source disk not found")
                snap = FakeSnapshot(path, src, body)
                self.snapshots[low] = snap
                self.snapshots_created.append(path)
                return self._start_op(f"/ops/snapshot-{snap.name}", 1, style="location",
                                      result=snap.doc())
            if m == "DELETE":
                if res is None:
                    return httpx.Response(204)
                del self.snapshots[low]
                self.snapshots_deleted.append(res.id)
                return httpx.Response(200)
        if res is None:
            return self._err(404, "ResourceNotFound", f"The Resource '{path}' under resource group was not found.")
        if m == "GET":
            return httpx.Response(200, json=res.doc())
        if m == "POST" and low.endswith("/begingetaccess"):
            if isinstance(res, FakeDisk) and res.managed_by:
                owner = self.vms.get(res.managed_by.lower())
                if owner is not None and owner.power != "deallocated":
                    return self._err(409, "OperationNotAllowed",
                                     f"Disk {res.name} is attached to VM {owner.name} which is not deallocated; "
                                     "deallocate the VM before granting access.")
            if res.sas_token:
                return self._err(409, "OperationNotAllowed", f"Disk {res.name} already has an active SAS (ActiveSAS).")
            self._sas_counter += 1
            res.sas_token = f"sas{self._sas_counter}"
            self.sas_granted.append(res.id)
            sas_url = f"{BLOB_HOST}/{res.sas_token}/abcd?sv=2018-03-28&sr=b&sig=fake&se=2030-01-01"
            return self._start_op(f"/ops/access-{res.name}-{self._sas_counter}", 1, style="location",
                                  result={"accessSAS": sas_url})
        if m == "POST" and low.endswith("/endgetaccess"):
            if res.sas_token:
                self.sas_revoked.append(res.id)
            res.sas_token = None
            return self._start_op(f"/ops/revoke-{res.name}-{len(self.sas_revoked)}", 1, style="location", result={})
        return self._err(405, "MethodNotAllowed", f"{m} {path}")

    def _start_op(self, op_path: str, polls: int, style: str, result: Optional[dict] = None, on_done=None):
        self._ops[op_path] = {"left": polls, "style": style, "result": result or {}, "on_done": on_done}
        headers = {"Retry-After": "0"}
        if style == "async":
            headers["Azure-AsyncOperation"] = f"https://management.azure.com{op_path}"
        else:
            headers["Location"] = f"https://management.azure.com{op_path}"
        return httpx.Response(202, headers=headers)

    def _poll(self, op_path: str) -> httpx.Response:
        op = self._ops[op_path]
        op["left"] -= 1
        if op["left"] > 0:
            if op["style"] == "async":
                return httpx.Response(200, json={"status": "InProgress"}, headers={"Retry-After": "0"})
            return httpx.Response(202, headers={"Location": f"https://management.azure.com{op_path}", "Retry-After": "0"})
        if op["on_done"]:
            op["on_done"]()
            op["on_done"] = None
        if op["style"] == "async":
            return httpx.Response(200, json={"status": "Succeeded"})
        return httpx.Response(200, json=op["result"])

    # -- blob
    def _blob(self, request: httpx.Request, path: str, query: dict) -> httpx.Response:
        token = path.split("/")[1]
        res = self.blob_for(token)
        if res is None or "expired" in query:
            return httpx.Response(403, text="<Error><Code>AuthenticationFailed</Code></Error>")
        blob = res.data + bytes(PAGE)  # + VHD footer (zeros are fine for the fake)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(blob)), "x-ms-blob-type": "PageBlob"})
        if "comp" in query and query["comp"][0] == "pagelist":
            ranges = allocated_pages(res.data) + [(len(res.data), len(blob) - 1)]  # footer is always written
            marker = query.get("marker", [None])[0]
            half = max(1, len(ranges) // 2)
            if marker is None and len(ranges) > 1:
                return httpx.Response(200, text=_xml_page_list(ranges[:half]).replace(
                    "</PageList>", "<NextMarker>m2</NextMarker></PageList>"))
            return httpx.Response(200, text=_xml_page_list(ranges[half:] if marker else ranges))
        rng = request.headers.get("x-ms-range") or request.headers.get("Range")
        if not rng:
            return httpx.Response(200, content=blob)
        start, end = (int(x) for x in rng.replace("bytes=", "").split("-"))
        if self.block_event is not None:
            self.block_event.wait(timeout=10)
        with self._lock:
            status = self.fail_ranges.pop(start, None)
        if status is not None:
            if status == 0:
                raise httpx.ReadError("connection reset by peer")
            return httpx.Response(status, text="<Error><Code>ServerBusy</Code></Error>")
        return httpx.Response(206, content=blob[start:end + 1],
                              headers={"Content-Range": f"bytes {start}-{end}/{len(blob)}"})


# --------------------------------------------------------------------------- canned inventory
def make_fleet(raws: dict[int, bytes]) -> FakeAzure:
    """The VMs the API tests work with.  ``raws`` are the disk contents for lin-01 (index 0 = OS disk)."""
    lin = FakeVm("lin-01", FakeDisk("lin-01_OsDisk", raws[0], os_type="Linux", gen="V2"),
                 [FakeDisk("lin-01-data0", raws[1])], power="running",
                 image={"publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy", "sku": "22_04-lts-gen2",
                        "version": "latest"}, security="TrustedLaunch", secure_boot=False, os_name="ubuntu",
                 os_version="22.04")
    win = FakeVm("win-01", FakeDisk("win-01_OsDisk", raws[0], os_type="Windows", gen="V1"), power="deallocated",
                 size="Standard_D4s_v3", image={"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer",
                                                "sku": "2022-datacenter-azure-edition", "version": "latest"},
                 os_name="Windows Server 2022 Datacenter Azure Edition", os_version="10.0.20348")
    enc = FakeVm("enc-01", FakeDisk("enc-01_OsDisk", raws[0], os_type="Linux", ade=True), power="deallocated",
                 image={"publisher": "RedHat", "offer": "RHEL", "sku": "9_4", "version": "latest"})
    conf = FakeVm("conf-01", FakeDisk("conf-01_OsDisk", raws[0], os_type="Linux"), power="deallocated",
                  security="ConfidentialVM", secure_boot=True,
                  image={"publisher": "Canonical", "offer": "0001-com-ubuntu-confidential-vm-jammy",
                         "sku": "22_04-lts-cvm", "version": "latest"})
    ol = FakeVm("ol-01", FakeDisk("ol-01_OsDisk", raws[0], os_type="Linux", gen="V2"), power="stopped",
                rg="rg-db", image={"publisher": "Oracle", "offer": "Oracle-Linux", "sku": "ol94-lvm-gen2",
                                   "version": "latest"}, os_name="ol", os_version="9.4")
    return FakeAzure([lin, win, enc, conf, ol])
