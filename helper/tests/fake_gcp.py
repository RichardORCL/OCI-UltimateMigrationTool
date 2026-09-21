"""Fake GCP: OAuth token, Compute Engine and Cloud Storage for migration tests."""

from __future__ import annotations

import io
import json
import re
import tarfile
import threading
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from helper_app.gcp.client import GcpClient, parse_service_account_json
from helper_app.gcp.session import GcpConnector


class _FakeGcpClient(GcpClient):
    """Skip real JWT signing in tests."""

    def token(self) -> str:
        self._token = "tok-1"
        self._token_expires = 1e18
        return self._token

PROJECT = "test-project"
ZONE = "europe-west3-a"
BUCKET = "test-export-bucket"
CLIENT_EMAIL = "umt@test-project.iam.gserviceaccount.com"

SA_JSON = json.dumps({
    "type": "service_account",
    "project_id": PROJECT,
    "private_key_id": "fake",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIBOwIBAAJBAK0+8X7LQ8exampleFAKEKEYNOTREAL0=\n-----END RSA PRIVATE KEY-----\n",
    "client_email": CLIENT_EMAIL,
    "client_id": "123",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
})


class FakeDisk:
    def __init__(self, name: str, data: bytes, size_gb: Optional[int] = None):
        self.name = name
        self.data = data
        self.size_gb = size_gb if size_gb is not None else max(1, -(-len(data) // 1024**3))
        self.url = f"projects/{PROJECT}/zones/{ZONE}/disks/{name}"

    def doc(self) -> dict:
        return {"name": self.name, "sizeGb": self.size_gb, "selfLink": f"https://www.googleapis.com/compute/v1/{self.url}",
                "sourceImage": "projects/debian-cloud/global/images/debian-12-bookworm-v20240110"}


class FakeVm:
    def __init__(self, name: str, os_disk: FakeDisk, data_disks: list[FakeDisk] = (), *, power: str = "RUNNING",
                 machine: str = "n2-standard-2", labels: Optional[dict] = None):
        self.name = name
        self.os_disk = os_disk
        self.data_disks = list(data_disks)
        self.power = power
        self.machine = machine
        self.labels = labels or {}
        self.id = f"projects/{PROJECT}/zones/{ZONE}/instances/{name}"

    @property
    def disks(self) -> list[FakeDisk]:
        return [self.os_disk, *self.data_disks]

    def doc(self) -> dict:
        disk_refs = []
        for i, d in enumerate(self.disks):
            disk_refs.append({
                "type": "PERSISTENT", "boot": i == 0, "deviceName": d.name,
                "source": f"https://www.googleapis.com/compute/v1/{d.url}",
            })
        return {
            "name": self.name,
            "id": "999",
            "status": self.power,
            "zone": f"projects/{PROJECT}/zones/{ZONE}",
            "machineType": f"projects/{PROJECT}/zones/{ZONE}/machineTypes/{self.machine}",
            "selfLink": f"https://www.googleapis.com/compute/v1/{self.id}",
            "disks": disk_refs,
            "labels": self.labels,
            "networkInterfaces": [{"networkIP": "10.0.0.2"}],
        }


MACHINES = {"n2-standard-2": {"name": "n2-standard-2", "guestCpus": 2, "memoryMb": 8192}}


class FakeGcp:
    def __init__(self, vms: list[FakeVm]):
        self.vms: dict[str, FakeVm] = {vm.id.lower(): vm for vm in vms}
        self.disks: dict[str, FakeDisk] = {d.url.lower(): d for vm in vms for d in vm.disks}
        self.snapshots: dict[str, bytes] = {}
        self.objects: dict[str, bytes] = {}
        self._builds: dict[str, str] = {}
        self._build_seq = 0
        self.tokens_issued = 0
        self.requests: list[str] = []
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    def http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport)

    def client_factory(self, raw: str) -> GcpClient:
        sa = parse_service_account_json(raw)
        return _FakeGcpClient(sa["project_id"], sa["client_email"], sa["private_key"],
                              http=self.http(), sleep=lambda s: None)

    def connector(self, settings) -> GcpConnector:
        return GcpConnector(settings, client_factory=self.client_factory)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.requests.append(f"{request.method} {url.path}")
        host = url.netloc
        if host == "oauth2.googleapis.com":
            return self._token(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer tok-"):
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        if host == "cloudresourcemanager.googleapis.com":
            return httpx.Response(200, json={"projects": [{"projectId": PROJECT, "name": "Test Project"}]})
        if host == "storage.googleapis.com":
            return self._storage(request, url)
        if host == "compute.googleapis.com":
            return self._compute(request, url.path, request.method)
        if host == "cloudbuild.googleapis.com":
            return self._cloud_build(request, url.path, request.method)
        return httpx.Response(502, text=f"unexpected host {host}")

    @staticmethod
    def _tar_gz_disk_raw(data: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="disk.raw")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("grant_type", [""])[0] != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.tokens_issued += 1
        return httpx.Response(200, json={"access_token": f"tok-{self.tokens_issued}", "expires_in": 3600,
                                         "token_type": "Bearer"})

    def _storage(self, request: httpx.Request, url) -> httpx.Response:
        path, method = url.path, request.method
        q = parse_qs(url.query)
        if method == "GET" and path == f"/storage/v1/b/{BUCKET}":
            # objectAdmin on the bucket does not grant storage.buckets.get (login uses objects.list instead).
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": (
                            f"{CLIENT_EMAIL} does not have storage.buckets.get access to the "
                            f"Google Cloud Storage bucket."
                        ),
                    },
                },
            )
        if method == "GET" and path == f"/storage/v1/b/{BUCKET}/o":
            prefix = (q.get("prefix") or [""])[0]
            items = [{"name": n, "size": len(d)} for n, d in self.objects.items() if n.startswith(prefix)]
            return httpx.Response(200, json={"items": items})
        m = re.match(rf"/storage/v1/b/{BUCKET}/o/([^/?]+)", path)
        if m and method == "GET" and q.get("alt") != ["media"]:
            name = unquote(m.group(1))
            data = self.objects.get(name, b"")
            return httpx.Response(200, json={"name": name, "size": len(data)})
        if m and method == "GET":
            name = unquote(m.group(1))
            data = self.objects.get(name, b"")
            rng = request.headers.get("Range", "")
            if rng.startswith("bytes="):
                s, e = (int(x) for x in rng.replace("bytes=", "").split("-"))
                chunk = data[s:e + 1]
                return httpx.Response(206, content=chunk,
                                      headers={"Content-Range": f"bytes {s}-{e}/{len(data)}"})
            return httpx.Response(200, content=data)
        if m and method == "DELETE":
            name = unquote(m.group(1))
            self.objects.pop(name, None)
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    def _compute(self, request: httpx.Request, path: str, method: str) -> httpx.Response:
        if method == "GET" and path == f"/compute/v1/projects/{PROJECT}":
            return httpx.Response(
                200,
                json={
                    "name": PROJECT,
                    "numericId": "123456789",
                    "id": "1906115310847722906",
                    "defaultServiceAccount": "123456789-compute@developer.gserviceaccount.com",
                },
            )
        if method == "GET" and path == f"/compute/v1/projects/{PROJECT}/aggregated/instances":
            items = {f"zones/{ZONE}/instances": {"instances": [vm.doc() for vm in self.vms.values()]}}
            return httpx.Response(200, json={"items": items})
        inst = re.match(rf"/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/([^/]+)(/stop)?$", path, re.I)
        if inst:
            name, stop_suffix = inst.group(1), inst.group(2)
            vm = self.vms.get(f"projects/{PROJECT}/zones/{ZONE}/instances/{name}".lower())
            if vm is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            if stop_suffix and method == "POST":
                vm.power = "TERMINATED"
                return httpx.Response(200, json={"name": "op-stop", "status": "DONE"})
            if method == "GET" and not stop_suffix:
                return httpx.Response(200, json=vm.doc())
        disk = re.match(rf"/compute/v1/projects/{PROJECT}/zones/{ZONE}/disks/([^/]+)$", path)
        if disk and method == "GET":
            d = self.disks.get(f"projects/{PROJECT}/zones/{ZONE}/disks/{disk.group(1)}".lower())
            if d is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            return httpx.Response(200, json=d.doc())
        snap_create = re.match(
            rf"/compute/v1/projects/{PROJECT}/zones/{ZONE}/disks/([^/]+)/createsnapshot$", path, re.I)
        if snap_create and method == "POST":
            snap_name = json.loads(request.content).get("name", "snap")
            disk = self.disks.get(f"projects/{PROJECT}/zones/{ZONE}/disks/{snap_create.group(1)}".lower())
            self.snapshots[snap_name] = bytes(disk.data) if disk else b""
            return httpx.Response(200, json={"name": "op-snap", "status": "DONE"})
        snap_get = re.match(rf"/compute/v1/projects/{PROJECT}/global/snapshots/([^/]+)$", path)
        if snap_get and method == "GET":
            return httpx.Response(200, json={"name": snap_get.group(1), "status": "READY"})
        snap_del = re.match(rf"/compute/v1/projects/{PROJECT}/global/snapshots/([^/]+)$", path)
        if snap_del and method == "DELETE":
            self.snapshots.pop(snap_del.group(1), None)
            return httpx.Response(200, json={"name": "op-del", "status": "DONE"})
        mt = re.match(rf"/compute/v1/projects/{PROJECT}/zones/{ZONE}/machineTypes/([^/]+)$", path)
        if mt and method == "GET":
            return httpx.Response(200, json=MACHINES.get(mt.group(1), MACHINES["n2-standard-2"]))
        return httpx.Response(404, json={"error": {"message": f"unhandled {method} {path}"}})

    def _cloud_build(self, request: httpx.Request, path: str, method: str) -> httpx.Response:
        if method == "POST" and path == f"/v1/projects/{PROJECT}/builds":
            body = json.loads(request.content)
            dest_uri = snap_name = ""
            for arg in (body.get("steps") or [{}])[0].get("args") or []:
                if str(arg).startswith("-destination_uri="):
                    dest_uri = str(arg).split("=", 1)[1]
                if str(arg).startswith("-source_disk_snapshot="):
                    snap_name = str(arg).rsplit("/", 1)[-1]
            m = re.match(r"gs://([^/]+)/(.+)", dest_uri)
            if m and m.group(1) == BUCKET:
                obj = unquote(m.group(2))
                self.objects[obj] = self._tar_gz_disk_raw(self.snapshots.get(snap_name, b""))
            self._build_seq += 1
            bid = f"build-{self._build_seq}"
            self._builds[bid] = "SUCCESS"
            # Real API: builds.create returns a long-running Operation wrapping the Build.
            return httpx.Response(200, json={
                "name": f"operations/build/{PROJECT}/{bid}",
                "metadata": {
                    "@type": "type.googleapis.com/google.devtools.cloudbuild.v1.BuildOperationMetadata",
                    "build": {"id": bid, "status": "QUEUED"},
                },
            })
        cancel = re.match(rf"/v1/projects/{PROJECT}/builds/([^/:]+):cancel$", path)
        if method == "POST" and cancel:
            self._builds[cancel.group(1)] = "CANCELLED"
            return httpx.Response(200, json={"id": cancel.group(1), "status": "CANCELLED"})
        get = re.match(rf"/v1/projects/{PROJECT}/builds/([^/]+)$", path)
        if method == "GET" and get:
            bid = get.group(1)
            return httpx.Response(200, json={"id": bid, "status": self._builds.get(bid, "SUCCESS")})
        return httpx.Response(404, json={"error": {"message": f"unhandled cloud build {method} {path}"}})


def make_fleet(raws: dict[int, bytes]) -> FakeGcp:
    lin = FakeVm("lin-01", FakeDisk("lin-01-boot", raws[0]), [FakeDisk("lin-01-data", raws[1])], power="RUNNING",
                 labels={"name": "lin-01"})
    win = FakeVm("win-01", FakeDisk("win-01-boot", raws[0]), power="TERMINATED",
                 labels={"name": "win-01", "os": "windows"})
    return FakeGcp([lin, win])
