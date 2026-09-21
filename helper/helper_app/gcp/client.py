"""Thin REST client for Google Compute Engine and Cloud Storage (service account JWT auth).

Built on ``httpx`` like the Azure client: token exchange, compute operations, LRO polling, and
authenticated GCS object reads with HTTP Range.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

import httpx
import jwt

from helper_app.branding import PREFIX

log = logging.getLogger(__name__)

COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
CLOUD_BUILD_BASE = "https://cloudbuild.googleapis.com/v1"
STORAGE_BASE = "https://storage.googleapis.com/storage/v1"
GCE_EXPORT_IMAGE = "gcr.io/compute-image-import/gce_vm_image_export:release"
RM_BASE = "https://cloudresourcemanager.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_write",
    "https://www.googleapis.com/auth/cloud-platform",
)

TOKEN_REFRESH_MARGIN_S = 300
DEFAULT_POLL_S = 2.0
MAX_POLL_S = 30.0

_INSTANCE = re.compile(
    r"^projects/(?P<project>[^/]+)/zones/(?P<zone>[^/]+)/instances/(?P<name>[^/]+)$",
    re.IGNORECASE,
)
_DISK = re.compile(
    r"^projects/(?P<project>[^/]+)/zones/(?P<zone>[^/]+)/disks/(?P<name>[^/]+)$",
    re.IGNORECASE,
)


class GcpError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GcpAuthError(GcpError):
    pass


def _api_error(resp: httpx.Response, what: str) -> GcpError:
    message, code = "", ""
    try:
        body = resp.json()
        err = body.get("error") or body
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("errors", [{}])[0].get("reason") or "")
            message = str(err.get("message") or "")
    except Exception:  # noqa: BLE001
        message = resp.text.strip()[:500]
    text = f"{what}: HTTP {resp.status_code}" + (f" ({code})" if code else "") + (f": {message}" if message else "")
    if resp.status_code in (401, 403):
        return GcpAuthError(text, code=code, status=resp.status_code)
    return GcpError(text, code=code, status=resp.status_code)


def parse_service_account_json(raw: str) -> dict[str, str]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GcpAuthError(f"service account JSON is not valid JSON: {exc}") from exc
    for key in ("type", "project_id", "private_key", "client_email"):
        if not doc.get(key):
            raise GcpAuthError(f"service account JSON is missing {key!r}")
    if doc.get("type") != "service_account":
        raise GcpAuthError('service account JSON must have "type": "service_account"')
    return {
        "project_id": str(doc["project_id"]),
        "client_email": str(doc["client_email"]),
        "private_key": str(doc["private_key"]),
    }


def normalize_instance_id(resource: str) -> str:
    """Canonical id: ``projects/.../zones/.../instances/...`` (no URL prefix)."""
    r = (resource or "").strip()
    if r.startswith("https://"):
        r = r.split("/compute/v1/", 1)[-1] if "/compute/v1/" in r else r.rsplit("/", 3)[-1]
    if r.startswith("compute/v1/"):
        r = r[len("compute/v1/"):]
    return r


def parse_instance_id(resource: str) -> dict[str, str]:
    m = _INSTANCE.match(normalize_instance_id(resource))
    if not m:
        raise GcpError(f"not a Compute Engine instance id: {resource!r}")
    return m.groupdict()


class GcpClient:
    def __init__(
        self,
        project_id: str,
        client_email: str,
        private_key: str,
        *,
        http: Optional[httpx.Client] = None,
        compute_base: str = COMPUTE_BASE,
        storage_base: str = STORAGE_BASE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.project_id = project_id.strip()
        self.client_email = client_email.strip()
        self._private_key = private_key
        self.compute_base = compute_base.rstrip("/")
        self.storage_base = storage_base.rstrip("/")
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._closed = False
        self._project_numbers: dict[str, str] = {}
        self._default_compute_sa: dict[str, str] = {}

    @classmethod
    def from_service_account_json(cls, raw: str, **kw: Any) -> "GcpClient":
        sa = parse_service_account_json(raw)
        return cls(sa["project_id"], sa["client_email"], sa["private_key"], **kw)

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expires - TOKEN_REFRESH_MARGIN_S:
                return self._token
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self.client_email,
                    "sub": self.client_email,
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                    "scope": " ".join(SCOPES),
                },
                self._private_key,
                algorithm="RS256",
            )
            try:
                resp = self.http.post(
                    TOKEN_URL,
                    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                )
            except httpx.HTTPError as exc:
                raise GcpError(f"cannot reach {TOKEN_URL}: {exc}") from exc
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    desc = body.get("error_description") or body.get("error") or resp.text[:300]
                except Exception:  # noqa: BLE001
                    desc = resp.text[:300]
                raise GcpAuthError(f"GCP login failed: {desc}", status=resp.status_code)
            body = resp.json()
            self._token = str(body["access_token"])
            self._token_expires = self._clock() + float(body.get("expires_in") or 3600)
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}

    def _url(self, base: str, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return base + (path if path.startswith("/") else "/" + path)

    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        what: str = "",
        auth: bool = True,
    ) -> httpx.Response:
        url = self._url(base, path)
        what = what or f"{method} {path.split('?', 1)[0]}"
        headers = self._headers() if auth else {}
        try:
            resp = self.http.request(method, url, params=params or None, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise GcpError(f"{what}: {exc}") from exc
        if resp.status_code >= 400:
            raise _api_error(resp, what)
        return resp

    def get_json(self, base: str, path: str, *, params: Optional[dict] = None, what: str = "") -> dict:
        resp = self._request("GET", base, path, params=params, what=what)
        return resp.json() if resp.content else {}

    def paged(self, base: str, path: str, *, what: str = "", items_key: str = "items") -> Iterator[dict]:
        page_token: Optional[str] = None
        while True:
            params = {"pageToken": page_token} if page_token else None
            body = self.get_json(base, path, params=params, what=what)
            for item in body.get(items_key) or []:
                yield item
            page_token = body.get("nextPageToken")
            if not page_token:
                return

    @staticmethod
    def _retry_after(resp: httpx.Response, default: float = DEFAULT_POLL_S) -> float:
        return default

    def wait_zone_operation(self, project: str, zone: str, op_name: str, timeout_s: float,
                            what: str = "", on_wait: Optional[Callable[[], None]] = None) -> dict:
        path = f"/projects/{project}/zones/{zone}/operations/{op_name}"
        deadline = self._clock() + timeout_s
        wait = DEFAULT_POLL_S
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise GcpError(f"{what or op_name}: operation did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            op = self.get_json(self.compute_base, path, what=what or "poll zone operation")
            if op.get("status") == "DONE":
                if op.get("error"):
                    err = op["error"]
                    raise GcpError(f"{what or op_name}: {err.get('errors', err)}")
                return op
            wait = self._retry_after(httpx.Response(200), wait)

    def wait_global_operation(self, project: str, op_name: str, timeout_s: float,
                              what: str = "", on_wait: Optional[Callable[[], None]] = None) -> dict:
        path = f"/projects/{project}/global/operations/{op_name}"
        deadline = self._clock() + timeout_s
        wait = DEFAULT_POLL_S
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise GcpError(f"{what or op_name}: operation did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            op = self.get_json(self.compute_base, path, what=what or "poll global operation")
            if op.get("status") == "DONE":
                if op.get("error"):
                    err = op["error"]
                    raise GcpError(f"{what or op_name}: {err.get('errors', err)}")
                return op
            wait = DEFAULT_POLL_S

    # -------------------------------------------------------- resource manager
    def project_number(self, project: str | None = None) -> str:
        pid = (project or self.project_id).strip()
        if pid in self._project_numbers:
            return self._project_numbers[pid]
        num = ""
        try:
            doc = self.get_json(RM_BASE, f"/projects/{pid}", what=f"get project {pid}")
            num = str(doc.get("projectNumber") or "")
        except GcpError:
            pass
        if not num:
            doc = self.get_json(self.compute_base, f"/projects/{pid}", what=f"get project {pid}")
            num = str(doc.get("numericId") or "")
            if not num:
                dsa = str(doc.get("defaultServiceAccount") or "")
                m = re.match(r"^(\d+)-compute@", dsa)
                if m:
                    num = m.group(1)
            # Compute ``id`` is an opaque legacy identifier — not the project number.
        if not num:
            raise GcpError(f"could not resolve GCP project number for {pid!r}")
        self._project_numbers[pid] = num
        return num

    def default_compute_service_email(self, project: str | None = None) -> str:
        pid = (project or self.project_id).strip()
        if pid in self._default_compute_sa:
            return self._default_compute_sa[pid]
        try:
            doc = self.get_json(self.compute_base, f"/projects/{pid}", what=f"get project {pid}")
            dsa = str(doc.get("defaultServiceAccount") or "").strip()
            if dsa:
                self._default_compute_sa[pid] = dsa
                return dsa
        except GcpError:
            pass
        email = f"{self.project_number(pid)}-compute@developer.gserviceaccount.com"
        self._default_compute_sa[pid] = email
        return email

    def list_projects(self) -> list[dict]:
        try:
            return [{"id": p.get("projectId") or "", "name": p.get("name") or p.get("displayName") or "",
                     "number": str(p.get("projectNumber") or "")}
                    for p in self.paged(RM_BASE, "/projects", what="list projects")]
        except GcpAuthError:
            raise
        except GcpError:
            return [{"id": self.project_id, "name": self.project_id, "number": ""}]

    # ------------------------------------------------------------ compute
    def aggregated_instances(self, project: str) -> list[dict]:
        path = f"/projects/{project}/aggregated/instances"
        out: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params = {"pageToken": page_token} if page_token else None
            resp = self._request("GET", self.compute_base, path, params=params,
                                 what=f"list instances in {project}")
            body = resp.json() if resp.content else {}
            for group in (body.get("items") or {}).values():
                for inst in group.get("instances") or []:
                    out.append(inst)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def get_instance(self, instance_id: str) -> dict:
        ids = parse_instance_id(instance_id)
        path = f"/projects/{ids['project']}/zones/{ids['zone']}/instances/{ids['name']}"
        return self.get_json(self.compute_base, path, what=f"get instance {ids['name']}")

    def get_disk(self, disk_url: str) -> dict:
        """``disk_url`` is a full disk URL or ``projects/.../zones/.../disks/...``."""
        if disk_url.startswith("http"):
            disk_url = disk_url.split("/compute/v1/", 1)[-1]
        return self.get_json(self.compute_base, f"/{disk_url.lstrip('/')}", what=f"get disk {disk_url.rsplit('/', 1)[-1]}")

    def get_machine_type(self, machine_type_url: str) -> dict:
        if machine_type_url.startswith("http"):
            machine_type_url = machine_type_url.split("/compute/v1/", 1)[-1]
        return self.get_json(self.compute_base, f"/{machine_type_url.lstrip('/')}", what="get machine type")

    def stop_instance(self, instance_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]] = None) -> None:
        ids = parse_instance_id(instance_id)
        path = f"/projects/{ids['project']}/zones/{ids['zone']}/instances/{ids['name']}/stop"
        resp = self._request("POST", self.compute_base, path, what=f"stop instance {ids['name']}")
        op = resp.json() if resp.content else {}
        name = op.get("name") or ""
        if op.get("status") == "DONE":
            return
        if name:
            self.wait_zone_operation(ids["project"], ids["zone"], name, timeout_s,
                                     what=f"stop {ids['name']}", on_wait=on_wait)

    def create_snapshot(
        self,
        project: str,
        zone: str,
        disk_name: str,
        snapshot_name: str,
        timeout_s: float,
        labels: Optional[dict[str, str]] = None,
        on_wait: Optional[Callable[[], None]] = None,
    ) -> dict:
        path = f"/projects/{project}/zones/{zone}/disks/{disk_name}/createSnapshot"
        body: dict[str, Any] = {"name": snapshot_name}
        if labels:
            body["labels"] = labels
        resp = self._request("POST", self.compute_base, path, json_body=body,
                             what=f"create snapshot {snapshot_name}")
        op = resp.json() if resp.content else {}
        if op.get("status") != "DONE" and op.get("name"):
            self.wait_zone_operation(project, zone, op["name"], timeout_s,
                                     what=f"snapshot {snapshot_name}", on_wait=on_wait)
        snap_path = f"/projects/{project}/global/snapshots/{snapshot_name}"
        return self.get_json(self.compute_base, snap_path, what=f"get snapshot {snapshot_name}")

    def wait_cloud_build(
        self,
        project: str,
        build_id: str,
        timeout_s: float,
        *,
        what: str = "",
        on_wait: Optional[Callable[[], None]] = None,
    ) -> dict:
        path = f"/projects/{project}/builds/{build_id}"
        deadline = self._clock() + timeout_s
        wait = DEFAULT_POLL_S
        label = what or f"cloud build {build_id}"
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise GcpError(f"{label}: build did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            build = self.get_json(CLOUD_BUILD_BASE, path, what=label)
            status = str(build.get("status") or "")
            if status == "SUCCESS":
                return build
            if status in ("FAILURE", "CANCELLED", "EXPIRED", "INTERNAL_ERROR", "TIMEOUT"):
                detail = build.get("statusDetail") or build.get("failureInfo") or status
                log_lines = (build.get("logUrl") or "") if isinstance(build.get("logUrl"), str) else ""
                raise GcpError(f"{label}: Cloud Build {status.lower()}" + (f": {detail}" if detail else "")
                               + (f" (logs: {log_lines})" if log_lines else ""))
            wait = min(MAX_POLL_S, wait * 1.2)

    def export_snapshot(
        self,
        project: str,
        zone: str,
        snapshot_name: str,
        bucket: str,
        object_name: str,
        timeout_s: float,
        on_wait: Optional[Callable[[], None]] = None,
    ) -> None:
        """Export a disk snapshot to GCS using Google's ``gce_vm_image_export`` Cloud Build workflow.

        Compute Engine has no ``snapshots.exportToCloudStorage`` REST method; exports run as a Cloud Build
        step and write a ``disk.raw`` member inside a ``.tar.gz`` object (see ``copy_from_gcs_export_tarball``).
        """
        snap_uri = f"projects/{project}/global/snapshots/{snapshot_name}"
        dest_uri = f"gs://{bucket}/{object_name.lstrip('/')}"
        if not object_name.endswith(".tar.gz"):
            raise GcpError(
                f"export object name must end with .tar.gz (Compute Engine export format), got {object_name!r}",
            )
        export_timeout = max(60, int(timeout_s) - 120)
        # Do not pass -compute_service_account: Cloud Build would require the caller to have
        # iam.serviceAccounts.actAs on that account. The export worker uses the project default
        # Compute Engine service account (grant it objectAdmin on the export bucket in setup).
        args = [
            f"-timeout={export_timeout}s",
            f"-source_disk_snapshot={snap_uri}",
            "-client_id=oci-umt",
            f"-destination_uri={dest_uri}",
            f"-project={project}",
            f"-zone={zone}",
            # Keep Daisy scratch in the user export bucket so we can delete it after the copy.
            f"-scratch_bucket_gcs_path=gs://{bucket}/{object_name.rsplit('/', 1)[0]}/daisy/",
            "-disable_gcs_logging",
        ]
        compute_sa = self.default_compute_service_email(project)
        build_body = {
            "timeout": f"{int(timeout_s)}s",
            "serviceAccount": f"projects/{project}/serviceAccounts/{compute_sa}",
            # Custom build SA requires explicit logging (no default GCS logs bucket).
            "options": {"logging": "CLOUD_LOGGING_ONLY"},
            "steps": [{
                "name": GCE_EXPORT_IMAGE,
                "args": args,
                "env": ["BUILD_ID=$BUILD_ID"],
            }],
            "tags": ["gce-daisy", "gce-daisy-image-export", PREFIX],
        }
        what = f"export snapshot {snapshot_name} to {dest_uri}"
        try:
            resp = self._request("POST", CLOUD_BUILD_BASE, f"/projects/{project}/builds",
                                 json_body=build_body, what=what)
        except GcpAuthError as exc:
            if exc.status == 403 and "act as service account" in str(exc).lower():
                raise GcpAuthError(
                    f"{what}: {exc}. Grant the migration service account permission to run Cloud Build as "
                    f"the default Compute Engine service account ({compute_sa}): "
                    f"gcloud iam service-accounts add-iam-policy-binding {compute_sa} "
                    f'--member="serviceAccount:{self.client_email}" '
                    f"--role=roles/iam.serviceAccountUser. "
                    f"Also grant {compute_sa} roles/storage.objectAdmin on gs://{bucket}/.",
                ) from exc
            raise
        except GcpError as exc:
            if exc.status == 404 and "<!DOCTYPE html>" in str(exc):
                raise GcpError(
                    f"{what}: Cloud Build API returned 404 — enable the Cloud Build API on project {project!r} "
                    f"and grant {self.client_email} roles/cloudbuild.builds.editor (or Cloud Build Editor)",
                ) from exc
            raise
        body = resp.json() if resp.content else {}
        # builds.create returns a long-running Operation; the Build is in metadata.build.
        build = (body.get("metadata") or {}).get("build") or body
        build_id = str(build.get("id") or "")
        if not build_id:
            raise GcpError(f"{what}: Cloud Build did not return a build id (response: {str(body)[:300]})")
        log.info("%s: Cloud Build %s started (%s)", what, build_id, build.get("logUrl") or "no log url")
        if str(build.get("status") or "") == "SUCCESS":
            return
        try:
            self.wait_cloud_build(project, build_id, timeout_s, what=what, on_wait=on_wait)
        except BaseException:
            # Job cancelled / timed out while the export worker is still running: stop it so no
            # temporary Daisy VM keeps running in the user's project.
            self.cancel_cloud_build(project, build_id)
            raise

    def cancel_cloud_build(self, project: str, build_id: str) -> None:
        try:
            self._request("POST", CLOUD_BUILD_BASE, f"/projects/{project}/builds/{build_id}:cancel",
                          json_body={}, what=f"cancel cloud build {build_id}")
            log.info("cancelled Cloud Build %s", build_id)
        except GcpError as exc:
            log.warning("could not cancel Cloud Build %s: %s", build_id, exc)

    def delete_snapshot(self, project: str, snapshot_name: str, timeout_s: float = 300.0) -> None:
        path = f"/projects/{project}/global/snapshots/{snapshot_name}"
        try:
            resp = self._request("DELETE", self.compute_base, path, what=f"delete snapshot {snapshot_name}")
        except GcpError as exc:
            if exc.status == 404:
                return
            raise
        op = resp.json() if resp.content else {}
        if op.get("name"):
            try:
                self.wait_global_operation(project, op["name"], timeout_s, what=f"delete snapshot {snapshot_name}")
            except GcpError as exc:
                if exc.status != 404:
                    raise

    # -------------------------------------------------------------- storage
    def get_bucket(self, bucket: str) -> dict:
        return self.get_json(self.storage_base, f"/b/{quote(bucket, safe='')}", what=f"get bucket {bucket}")

    def verify_export_bucket(self, bucket: str) -> None:
        """Confirm the service account can use this bucket for migration exports.

        ``roles/storage.objectAdmin`` on the bucket does not include ``storage.buckets.get``, so a
        failed bucket GET is followed by a one-object list (``storage.objects.list``), which exports need.
        """
        try:
            self.get_bucket(bucket)
            return
        except GcpAuthError as exc:
            if exc.status != 403:
                raise
            log.debug("bucket GET denied for %s (%s); trying objects.list", bucket, exc)
        except GcpError as exc:
            if exc.status == 404:
                raise GcpAuthError(
                    f"export bucket {bucket!r} was not found; create it in Google Cloud Storage first "
                    "(the name must be globally unique)",
                ) from exc
            if exc.status != 403:
                raise
            log.debug("bucket GET denied for %s (%s); trying objects.list", bucket, exc)
        try:
            self.get_json(
                self.storage_base,
                f"/b/{quote(bucket, safe='')}/o",
                params={"maxResults": 1},
                what=f"list objects in bucket {bucket}",
            )
        except GcpAuthError:
            raise
        except GcpError as exc:
            if exc.status == 404:
                raise GcpAuthError(
                    f"export bucket {bucket!r} was not found; create it in Google Cloud Storage first",
                ) from exc
            raise GcpAuthError(
                f"cannot access export bucket {bucket}: grant this service account "
                f"roles/storage.objectAdmin (or Storage Object Admin) on gs://{bucket}/ "
                f"(and compute roles on the VM project). Detail: {exc}",
            ) from exc

    def list_objects(self, bucket: str, prefix: str = "") -> list[str]:
        names: list[str] = []
        page_token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"maxResults": 1000}
            if prefix:
                params["prefix"] = prefix
            if page_token:
                params["pageToken"] = page_token
            body = self.get_json(
                self.storage_base,
                f"/b/{quote(bucket, safe='')}/o",
                params=params,
                what=f"list gs://{bucket}/{prefix}",
            )
            for item in body.get("items") or []:
                name = str(item.get("name") or "")
                if name:
                    names.append(name)
            page_token = body.get("nextPageToken")
            if not page_token:
                return names

    def delete_object(self, bucket: str, object_name: str) -> None:
        path = f"/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
        try:
            self._request("DELETE", self.storage_base, path, what=f"delete gs://{bucket}/{object_name}")
        except GcpError as exc:
            if exc.status == 404:
                return
            raise

    def delete_prefix(self, bucket: str, prefix: str) -> list[str]:
        """Delete every object under ``prefix`` (inclusive of a trailing slash)."""
        prefix = prefix.lstrip("/")
        actions: list[str] = []
        try:
            names = self.list_objects(bucket, prefix)
        except GcpError as exc:
            if exc.status in (403, 404):
                return [f"skipped: list gs://{bucket}/{prefix}: {exc}"]
            raise
        for name in names:
            try:
                self.delete_object(bucket, name)
                actions.append(f"ok: deleted gs://{bucket}/{name}")
            except GcpError as exc:
                actions.append(f"failed: delete gs://{bucket}/{name}: {exc}")
        return actions

    def delete_bucket(self, bucket: str) -> None:
        try:
            self._request("DELETE", self.storage_base, f"/b/{quote(bucket, safe='')}",
                          what=f"delete bucket {bucket}")
        except GcpError as exc:
            if exc.status in (403, 404):
                return
            raise

    def object_media_url(self, bucket: str, object_name: str) -> str:
        return f"{self.storage_base}/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}?alt=media"

    def object_head(self, bucket: str, object_name: str) -> dict:
        path = f"/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
        return self.get_json(self.storage_base, path, what=f"head gs://{bucket}/{object_name}")

    def object_get_range(self, bucket: str, object_name: str, start: int, end: int) -> bytes:
        url = self.object_media_url(bucket, object_name)
        headers = {**self._headers(), "Range": f"bytes={start}-{end}"}
        try:
            resp = self.http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GcpError(f"read gs://{bucket}/{object_name}: {exc}") from exc
        if resp.status_code not in (200, 206):
            raise _api_error(resp, f"read gs://{bucket}/{object_name}")
        return resp.content

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._own_http:
            self.http.close()
