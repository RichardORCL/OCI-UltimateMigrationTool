"""Thin REST client for Azure Resource Manager and the blob endpoint behind a managed disk export SAS.

Deliberately built on ``httpx`` instead of the ``azure-*`` SDKs: the migration tool needs a dozen
endpoints (token, subscriptions, VMs, disks, deallocate, snapshots, ``beginGetAccess`` /
``endGetAccess``, page ranges, range reads), and a small client is easy to fake in tests.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
LOGIN_BASE = "https://login.microsoftonline.com"
ARM_SCOPE = "https://management.azure.com/.default"

API_SUBSCRIPTIONS = "2022-12-01"
API_COMPUTE = "2024-07-01"  # virtual machines, VM sizes
API_DISKS = "2024-03-02"  # disks and snapshots (beginGetAccess / endGetAccess)
BLOB_API_VERSION = "2021-08-06"

TOKEN_REFRESH_MARGIN_S = 300  # renew the bearer token this long before it expires
DEFAULT_POLL_S = 5.0


def _append_sas_query(sas_url: str, params: dict[str, str]) -> str:
    """Append query parameters without re-encoding the existing SAS (httpx ``params=`` breaks ``sig``)."""
    if not params:
        return sas_url
    extra = "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in params.items())
    return sas_url + ("&" if "?" in sas_url else "?") + extra
MAX_POLL_S = 30.0

_AADSTS = re.compile(r"AADSTS(\d+)")
_AADSTS_HINTS = {
    "7000215": "invalid client secret",
    "7000222": "the client secret has expired; create a new one in Entra ID",
    "700016": "application (client ID) not found in this tenant",
    "90002": "tenant not found; check the tenant ID",
    "900023": "tenant ID is not a valid GUID or domain name",
    "7000216": "client secret is required",
    "70011": "invalid scope",
    "50034": "user account not found",
    "700027": "client assertion failed",
}


class AzureError(RuntimeError):
    """An Azure API call failed (network, HTTP status, or an ARM error body)."""

    def __init__(self, message: str, code: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AzureAuthError(AzureError):
    """Authentication or authorization failure (wrong credentials, missing RBAC)."""


def _arm_error(resp: httpx.Response, what: str) -> AzureError:
    code, message = "", ""
    try:
        body = resp.json()
        err = body.get("error") or body
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
            # nested details (e.g. deallocate failures) carry the useful text
            details = err.get("details") or []
            if not message and details:
                message = str(details[0].get("message") or "")
    except Exception:  # noqa: BLE001 - not JSON
        message = resp.text.strip()[:500]
    text = f"{what}: HTTP {resp.status_code}" + (f" {code}" if code else "") + (f": {message}" if message else "")
    if resp.status_code in (401, 403) or code in ("AuthorizationFailed", "InvalidAuthenticationToken",
                                                  "ExpiredAuthenticationToken", "AuthenticationFailed"):
        return AzureAuthError(text, code=code, status=resp.status_code)
    return AzureError(text, code=code, status=resp.status_code)


def describe_token_error(body: dict) -> str:
    """Turn an Entra ID token error into a one-line, user facing explanation."""
    desc = str(body.get("error_description") or body.get("error") or "token request failed")
    m = _AADSTS.search(desc)
    if m:
        hint = _AADSTS_HINTS.get(m.group(1))
        first = desc.split("\n", 1)[0].split(" Trace ID", 1)[0].strip()
        return f"{hint} ({first})" if hint else first
    return desc.split("\n", 1)[0]


class AzureClient:
    """Bearer-token ARM client plus anonymous blob access for SAS URLs.

    ``http`` is injectable (tests pass a client with an ``httpx.MockTransport``); ``sleep`` and
    ``clock`` likewise so long-running operations can be exercised without waiting.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *,
                 http: Optional[httpx.Client] = None, arm_base: str = ARM_BASE, login_base: str = LOGIN_BASE,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self._secret = client_secret
        self.arm_base = arm_base.rstrip("/")
        self.login_base = login_base.rstrip("/")
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ token
    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expires - TOKEN_REFRESH_MARGIN_S:
                return self._token
            url = f"{self.login_base}/{self.tenant_id}/oauth2/v2.0/token"
            data = {"client_id": self.client_id, "client_secret": self._secret, "scope": ARM_SCOPE,
                    "grant_type": "client_credentials"}
            try:
                resp = self.http.post(url, data=data)
            except httpx.HTTPError as exc:
                raise AzureError(f"cannot reach {self.login_base}: {exc}") from exc
            if resp.status_code != 200:
                try:
                    body = resp.json()
                except Exception:  # noqa: BLE001
                    body = {"error_description": resp.text[:300]}
                raise AzureAuthError(f"Azure login failed: {describe_token_error(body)}",
                                     code=str(body.get("error") or ""), status=resp.status_code)
            body = resp.json()
            self._token = str(body["access_token"])
            self._token_expires = self._clock() + float(body.get("expires_in") or 3600)
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}

    # -------------------------------------------------------------- ARM basics
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.arm_base + (path if path.startswith("/") else "/" + path)

    def _request(self, method: str, path: str, *, api_version: Optional[str], params: Optional[dict] = None,
                 json: Any = None, what: str = "") -> httpx.Response:
        params = dict(params or {})
        if api_version and "api-version" not in params and "api-version=" not in path:
            params["api-version"] = api_version
        url = self._url(path)
        what = what or f"{method} {path.split('?', 1)[0]}"
        try:
            resp = self.http.request(method, url, params=params or None, json=json, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AzureError(f"{what}: {exc}") from exc
        if resp.status_code >= 400:
            raise _arm_error(resp, what)
        return resp

    def get(self, path: str, api_version: str, params: Optional[dict] = None, what: str = "") -> dict:
        resp = self._request("GET", path, api_version=api_version, params=params, what=what)
        return resp.json() if resp.content else {}

    def paged(self, path: str, api_version: str, params: Optional[dict] = None, what: str = "") -> Iterator[dict]:
        """Iterate the ``value`` items of a list endpoint, following ``nextLink``."""
        body = self.get(path, api_version, params=params, what=what)
        while True:
            for item in body.get("value") or []:
                yield item
            nxt = body.get("nextLink")
            if not nxt:
                return
            resp = self._request("GET", nxt, api_version=None, what=what)
            body = resp.json() if resp.content else {}

    def post_lro(self, path: str, api_version: str, json: Any = None, timeout_s: float = 900.0,
                 what: str = "", on_wait: Optional[Callable[[], None]] = None) -> dict:
        """POST that may start a long-running operation; waits for it and returns the final body."""
        resp = self._request("POST", path, api_version=api_version, json=json, what=what)
        return self._finish_lro(resp, timeout_s, what or path, on_wait)

    def put_lro(self, path: str, api_version: str, json: Any, timeout_s: float = 900.0, what: str = "",
                on_wait: Optional[Callable[[], None]] = None) -> dict:
        resp = self._request("PUT", path, api_version=api_version, json=json, what=what)
        return self._finish_lro(resp, timeout_s, what or path, on_wait)

    def delete_lro(self, path: str, api_version: str, timeout_s: float = 900.0, what: str = "",
                   on_wait: Optional[Callable[[], None]] = None) -> None:
        try:
            resp = self._request("DELETE", path, api_version=api_version, what=what)
        except AzureError as exc:
            if exc.status == 404:
                return
            raise
        if resp.status_code == 204:
            return
        self._finish_lro(resp, timeout_s, what or path, on_wait)

    def _finish_lro(self, resp: httpx.Response, timeout_s: float, what: str,
                    on_wait: Optional[Callable[[], None]]) -> dict:
        if resp.status_code in (200, 201, 204) and not resp.headers.get("Azure-AsyncOperation"):
            return resp.json() if resp.content and resp.status_code != 204 else {}
        async_url = resp.headers.get("Azure-AsyncOperation")
        location = resp.headers.get("Location")
        deadline = self._clock() + timeout_s
        wait = self._retry_after(resp)
        headers = None
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise AzureError(f"{what}: Azure operation did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            headers = self._headers()
            if async_url:
                try:
                    poll = self.http.get(async_url, headers=headers)
                except httpx.HTTPError as exc:
                    raise AzureError(f"{what}: polling failed: {exc}") from exc
                if poll.status_code >= 400:
                    raise _arm_error(poll, what)
                body = poll.json() if poll.content else {}
                status = str(body.get("status") or "").lower()
                if status in ("succeeded",):
                    output = (body.get("properties") or {}).get("output")
                    if isinstance(output, dict) and output:
                        return output
                    if location:
                        return self._fetch_location(location, what)
                    return body
                if status in ("failed", "canceled", "cancelled"):
                    err = body.get("error") or {}
                    raise AzureError(f"{what}: Azure operation {status}: {err.get('code', '')} "
                                     f"{err.get('message', '')}".strip(), code=str(err.get("code") or ""))
                wait = self._retry_after(poll, wait)
            elif location:
                try:
                    poll = self.http.get(location, headers=headers)
                except httpx.HTTPError as exc:
                    raise AzureError(f"{what}: polling failed: {exc}") from exc
                if poll.status_code >= 400:
                    raise _arm_error(poll, what)
                if poll.status_code == 202:
                    wait = self._retry_after(poll, wait)
                    continue
                return poll.json() if poll.content else {}
            else:
                raise AzureError(f"{what}: HTTP {resp.status_code} without an operation URL to poll")

    def _fetch_location(self, location: str, what: str) -> dict:
        try:
            poll = self.http.get(location, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AzureError(f"{what}: result fetch failed: {exc}") from exc
        if poll.status_code >= 400:
            raise _arm_error(poll, what)
        return poll.json() if poll.content and poll.status_code != 202 else {}

    @staticmethod
    def _retry_after(resp: httpx.Response, default: float = DEFAULT_POLL_S) -> float:
        ra = resp.headers.get("Retry-After")
        try:
            return max(0.0, min(MAX_POLL_S, float(ra))) if ra else default
        except ValueError:
            return default

    # ------------------------------------------------------------ compute API
    def list_subscriptions(self) -> list[dict]:
        return [{"id": s.get("subscriptionId") or s.get("id", "").rsplit("/", 1)[-1],
                 "name": s.get("displayName") or "", "state": s.get("state") or ""}
                for s in self.paged("/subscriptions", API_SUBSCRIPTIONS, what="list subscriptions")]

    def list_vms(self, subscription_id: str) -> list[dict]:
        """All VMs of a subscription (without instance view).

        Azure's current compute API rejects ``$expand=instanceView`` on a subscription-wide list
        (400: only supported with a VM scale set filter).  Call ``get_vm`` per VM when power state
        or hypervisor generation from the instance view is needed.
        """
        path = f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines"
        return list(self.paged(path, API_COMPUTE, what=f"list virtual machines of subscription {subscription_id}"))

    def get_vm_instance_view(self, vm_id: str) -> dict:
        return self.get(f"{vm_id}/instanceView", API_COMPUTE,
                        what=f"instance view of {vm_id.rsplit('/', 1)[-1]}")

    def get_vm(self, vm_id: str) -> dict:
        name = vm_id.rsplit("/", 1)[-1]
        try:
            return self.get(vm_id, API_COMPUTE, params={"$expand": "instanceView"},
                            what=f"get virtual machine {name}")
        except AzureError as exc:
            if exc.status == 404:
                raise
            if exc.status != 400:
                raise
            vm = self.get(vm_id, API_COMPUTE, what=f"get virtual machine {name}")
            try:
                view = self.get_vm_instance_view(vm_id)
            except AzureError:
                return vm
            props = vm.setdefault("properties", {})
            props["instanceView"] = view.get("properties") if isinstance(view.get("properties"), dict) else view
            return vm

    def list_vm_sizes(self, subscription_id: str, location: str) -> dict[str, dict]:
        path = f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/locations/{location}/vmSizes"
        return {s["name"]: s for s in self.paged(path, API_COMPUTE, what=f"list VM sizes in {location}")}

    def get_disk(self, disk_id: str) -> dict:
        return self.get(disk_id, API_DISKS, what=f"get disk {disk_id.rsplit('/', 1)[-1]}")

    def deallocate_vm(self, vm_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]] = None) -> None:
        self.post_lro(f"{vm_id}/deallocate", API_COMPUTE, timeout_s=timeout_s,
                      what=f"deallocate {vm_id.rsplit('/', 1)[-1]}", on_wait=on_wait)

    def create_snapshot(self, snapshot_id: str, source_disk_id: str, location: str, timeout_s: float,
                        tags: Optional[dict[str, str]] = None,
                        on_wait: Optional[Callable[[], None]] = None) -> dict:
        body = {"location": location, "tags": tags or {},
                "properties": {"creationData": {"createOption": "Copy", "sourceResourceId": source_disk_id},
                               "incremental": True}}
        self.put_lro(snapshot_id, API_DISKS, json=body, timeout_s=timeout_s,
                     what=f"create snapshot {snapshot_id.rsplit('/', 1)[-1]}", on_wait=on_wait)
        # the PUT may answer before the snapshot is usable; make sure it reached Succeeded
        deadline = self._clock() + timeout_s
        while True:
            snap = self.get(snapshot_id, API_DISKS, what="get snapshot")
            state = str((snap.get("properties") or {}).get("provisioningState") or "").lower()
            if state == "succeeded":
                return snap
            if state in ("failed", "canceled"):
                raise AzureError(f"snapshot {snapshot_id.rsplit('/', 1)[-1]} entered state {state}")
            if self._clock() > deadline:
                raise AzureError(f"snapshot {snapshot_id.rsplit('/', 1)[-1]} not ready within {int(timeout_s)} s")
            if on_wait is not None:
                on_wait()
            self._sleep(DEFAULT_POLL_S)

    def delete_snapshot(self, snapshot_id: str, timeout_s: float = 600.0) -> None:
        self.delete_lro(snapshot_id, API_DISKS, timeout_s=timeout_s,
                        what=f"delete snapshot {snapshot_id.rsplit('/', 1)[-1]}")

    def begin_get_access(self, resource_id: str, duration_s: int, timeout_s: float = 600.0,
                         on_wait: Optional[Callable[[], None]] = None) -> str:
        """Grant a read SAS on a disk or snapshot; returns the SAS URL of its VHD page blob."""
        body = {"access": "Read", "durationInSeconds": int(duration_s), "fileFormat": "VHD"}
        out = self.post_lro(f"{resource_id}/beginGetAccess", API_DISKS, json=body, timeout_s=timeout_s,
                            what=f"beginGetAccess {resource_id.rsplit('/', 1)[-1]}", on_wait=on_wait)
        sas = out.get("accessSAS") or out.get("accessSas") or (out.get("properties") or {}).get("output", {}).get(
            "accessSAS")
        if not sas:
            raise AzureError(f"beginGetAccess on {resource_id.rsplit('/', 1)[-1]} returned no accessSAS")
        return str(sas)

    def end_get_access(self, resource_id: str, timeout_s: float = 600.0) -> None:
        self.post_lro(f"{resource_id}/endGetAccess", API_DISKS, timeout_s=timeout_s,
                      what=f"endGetAccess {resource_id.rsplit('/', 1)[-1]}")

    # ----------------------------------------------------------------- blobs
    def blob_head(self, sas_url: str) -> httpx.Response:
        try:
            resp = self.http.head(sas_url, headers={"x-ms-version": BLOB_API_VERSION})
        except httpx.HTTPError as exc:
            raise AzureError(f"HEAD export blob: {exc}") from exc
        if resp.status_code >= 400:
            raise AzureError(f"HEAD export blob returned HTTP {resp.status_code}", status=resp.status_code)
        return resp

    def blob_get(self, sas_url: str, params: Optional[dict] = None,
                 headers: Optional[dict[str, str]] = None) -> httpx.Response:
        hdrs = {"x-ms-version": BLOB_API_VERSION}
        if headers:
            hdrs.update(headers)
        url = _append_sas_query(sas_url, {k: str(v) for k, v in (params or {}).items()})
        try:
            return self.http.get(url, headers=hdrs)
        except httpx.HTTPError as exc:
            raise AzureError(f"GET export blob: {exc}") from exc

    # -------------------------------------------------------------- lifecycle
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._own_http:
            try:
                self.http.close()
            except Exception:  # noqa: BLE001
                pass
