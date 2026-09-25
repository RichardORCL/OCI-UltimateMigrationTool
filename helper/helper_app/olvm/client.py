"""Thin REST client for an OLVM / oVirt engine and its image-transfer proxy.

Built on ``httpx``, like the Azure client: login, inventory, power actions and image
transfers are a handful of JSON calls, and tests inject a client with a mock transport.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

from helper_app.redaction import redact

log = logging.getLogger(__name__)

API_PREFIX = "/ovirt-engine/api"
TOKEN_PATH = "/ovirt-engine/sso/oauth/token"
# ``bios`` and ``os`` are already on the VM document. Following them makes current OLVM
# answer 500 on both the collection and a single VM.
FOLLOW = "disk_attachments.disk,nics,cluster"
TOKEN_REFRESH_MARGIN_S = 60
# Keycloak-backed engines (current OLVM) keep this authz profile. The portal user is often
# ``admin@ovirt``, which the token endpoint reads as user ``admin`` in a profile named ``ovirt``.
# That profile does not exist; the same person is ``admin@ovirt@internal``.
DEFAULT_AUTHZ_PROFILE = "internal"


class OlvmError(RuntimeError):
    """An OLVM API call failed (network, HTTP status, or an engine fault)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(redact(message))
        self.status = status


class OlvmAuthError(OlvmError):
    """Authentication failure (wrong user, password, or a rejected token)."""


def _login_names(username: str) -> list[str]:
    """User names to try at the token endpoint, first the one that was typed.

    A Keycloak portal user such as ``admin@ovirt`` has an ``@`` that is part of the user, not an
    engine authz profile. The token endpoint's profile is ``internal``, so the working form is
    ``admin@ovirt@internal``. A name that already ends in that profile is sent once.
    """
    name = username.strip()
    names = [name]
    suffix = f"@{DEFAULT_AUTHZ_PROFILE}"
    if not name.lower().endswith(suffix):
        names.append(f"{name}{suffix}")
    return names


def parse_engine_url(raw: str) -> tuple[str, str]:
    """``(base_url, display_host)`` from the login form.

    ``base_url`` is ``scheme://host[:port]`` with no path. A pasted ``/ovirt-engine`` suffix is
    dropped. ``display_host`` is what the UI shows (port included when it is not the default).
    """
    text = (raw or "").strip()
    if not text:
        raise OlvmAuthError("engine URL is required")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http"):
        raise OlvmAuthError(f"engine URL must be http or https, not {parts.scheme!r}")
    if parts.username or parts.password:
        raise OlvmAuthError("put the user name in the user field, not in the engine URL")
    if not parts.hostname:
        raise OlvmAuthError(f"engine URL {raw!r} has no host")
    path = (parts.path or "").rstrip("/")
    for suffix in ("/ovirt-engine/api", "/ovirt-engine"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if path not in ("", "/"):
        raise OlvmAuthError("enter the engine host (for example https://olvm.example.com), not a page path")
    base = f"{parts.scheme}://{parts.netloc}"
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname
    if parts.port and parts.port != default_port:
        host = f"{host}:{parts.port}"
    return base, host


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _fault_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "").strip()[:500]
    if not isinstance(body, dict):
        return str(body)[:500]
    fault = body.get("fault") if isinstance(body.get("fault"), dict) else body
    detail = str(fault.get("detail") or fault.get("error_description") or fault.get("message") or "")
    reason = str(fault.get("reason") or fault.get("error") or "")
    text = detail or reason
    if reason and detail and reason not in detail:
        text = f"{reason}: {detail}"
    return text.strip()[:500]


class OlvmClient:
    """Bearer-token client for one engine. ``http`` and ``sleep`` are injectable for tests."""

    def __init__(self, base_url: str, username: str, password: str, *, verify_ssl: bool = True,
                 http: Optional[httpx.Client] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.verify_ssl = verify_ssl
        self._password = password
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), verify=verify_ssl,
                                         follow_redirects=True)
        self.sleep = sleep
        self._clock = clock
        self._token = ""
        self._deadline = 0.0

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    def token(self) -> str:
        if self._token and self._clock() < self._deadline:
            return self._token
        names = _login_names(self.username)
        last: Optional[OlvmAuthError] = None
        for candidate in names:
            try:
                self._fetch_token(candidate)
            except OlvmAuthError as exc:
                last = exc
                if "no valid profile" not in str(exc).lower():
                    raise
                continue
            if candidate != self.username:
                log.info("OLVM login: %s is not an auth profile, using %s", self.username, candidate)
            self.username = candidate
            return self._token
        assert last is not None
        raise last

    def _fetch_token(self, username: str) -> None:
        try:
            resp = self.http.post(
                self.base_url + TOKEN_PATH,
                data={
                    "grant_type": "password",
                    "username": username,
                    "password": self._password,
                    "scope": "ovirt-app-api",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OlvmError(f"OLVM login failed: {exc}") from exc
        if resp.status_code in (400, 401, 403):
            raise OlvmAuthError(_fault_text(resp) or "invalid user name or password", status=resp.status_code)
        if resp.status_code >= 400:
            raise OlvmError(f"OLVM login: HTTP {resp.status_code}: {_fault_text(resp)}", status=resp.status_code)
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise OlvmError("OLVM login returned no token") from exc
        access = str(body.get("access_token") or "")
        if not access:
            raise OlvmAuthError(_fault_text(resp) or "OLVM login returned no token")
        self._token = access
        self._deadline = self._clock() + self._lifetime(body)

    def _lifetime(self, body: dict) -> float:
        if body.get("expires_in"):
            return max(30.0, float(body["expires_in"]) - TOKEN_REFRESH_MARGIN_S)
        if body.get("exp"):
            remaining = float(body["exp"]) - time.time() - TOKEN_REFRESH_MARGIN_S
            return max(30.0, remaining)
        return 600.0

    def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                auth: bool = True) -> Any:
        url = path if path.startswith("http") else self.base_url + path
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
            headers["Version"] = "4"
        try:
            resp = self.http.request(method, url, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise OlvmError(f"{method} {path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OlvmAuthError(f"{method} {path}: HTTP {resp.status_code}: {_fault_text(resp)}",
                                status=resp.status_code)
        if resp.status_code >= 400:
            raise OlvmError(f"{method} {path}: HTTP {resp.status_code}: {_fault_text(resp)}",
                            status=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    def get_api(self) -> dict:
        data = self.request("GET", API_PREFIX)
        return data if isinstance(data, dict) else {}

    def list_vms(self) -> list[dict]:
        data = self.request("GET", API_PREFIX + "/vms", params={"follow": FOLLOW})
        return _as_list((data or {}).get("vm") if isinstance(data, dict) else None)

    def get_vm(self, vm_id: str) -> dict:
        data = self.request("GET", f"{API_PREFIX}/vms/{vm_id}", params={"follow": FOLLOW})
        return data if isinstance(data, dict) else {}

    def vm_action(self, vm_id: str, action: str, body: Optional[dict] = None) -> dict:
        data = self.request("POST", f"{API_PREFIX}/vms/{vm_id}/{action}", json=body or {})
        return data if isinstance(data, dict) else {}

    def create_transfer(self, disk_id: str, inactivity_timeout_s: int) -> dict:
        data = self.request("POST", API_PREFIX + "/imagetransfers", json={
            "disk": {"id": disk_id},
            "direction": "download",
            "format": "raw",
            "inactivity_timeout": int(inactivity_timeout_s),
        })
        if isinstance(data, dict) and "image_transfer" in data and "id" not in data:
            data = data["image_transfer"]
        if not isinstance(data, dict) or not data.get("id"):
            raise OlvmError(f"image transfer for disk {disk_id} returned no id")
        return data

    def get_transfer(self, transfer_id: str) -> dict:
        data = self.request("GET", f"{API_PREFIX}/imagetransfers/{transfer_id}")
        return data if isinstance(data, dict) else {}

    def set_transfer_phase(self, transfer_id: str, phase: str) -> None:
        try:
            self.request("PUT", f"{API_PREFIX}/imagetransfers/{transfer_id}", json={"phase": phase})
        except OlvmError as exc:
            if exc.status in (400, 404, 409):
                log.info("image transfer %s phase %s: %s", transfer_id, phase, exc)
                return
            raise

    def image_extents(self, image_url: str, ticket: str) -> list[dict]:
        body = self._image_json(image_url.rstrip("/") + "/extents", ticket)
        if isinstance(body, dict):
            body = body.get("extents") or []
        if not isinstance(body, list):
            raise OlvmError(f"image extents at {image_url} were not a list")
        return body

    def read_image(self, image_url: str, ticket: str, start: int, length: int) -> bytes:
        end = start + length - 1
        headers = {"Authorization": f"Bearer {ticket}", "Range": f"bytes={start}-{end}"}
        try:
            resp = self.http.get(image_url, headers=headers)
        except httpx.HTTPError as exc:
            raise OlvmError(f"image read {start}-{end}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OlvmAuthError(f"image read {start}-{end}: HTTP {resp.status_code}", status=resp.status_code)
        if resp.status_code not in (200, 206):
            raise OlvmError(f"image read {start}-{end}: HTTP {resp.status_code}: {_fault_text(resp)}",
                            status=resp.status_code)
        return resp.content

    def _image_json(self, url: str, ticket: str) -> Any:
        headers = {"Authorization": f"Bearer {ticket}", "Accept": "application/json"}
        try:
            resp = self.http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise OlvmError(f"image extents: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OlvmAuthError(f"image extents: HTTP {resp.status_code}", status=resp.status_code)
        if resp.status_code >= 400:
            raise OlvmError(f"image extents: HTTP {resp.status_code}: {_fault_text(resp)}", status=resp.status_code)
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise OlvmError("image extents were not JSON") from exc
