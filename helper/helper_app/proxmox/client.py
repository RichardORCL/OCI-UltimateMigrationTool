"""REST client for the Proxmox VE API (``https://<node>:8006/api2/json``).

Two ways to authenticate:

* an API token (``user@realm!tokenid`` plus its secret), sent as ``Authorization: PVEAPIToken=...``;
* a user and password, exchanged for a ticket at ``/access/ticket``. The ticket goes in the
  ``PVEAuthCookie`` cookie, writes also carry the ``CSRFPreventionToken`` header, and the ticket
  is renewed before its 2-hour lifetime runs out.

The API covers inventory and power only; disk data is read over SSH (``proxmox/ssh.py``).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import httpx

from helper_app.redaction import redact

log = logging.getLogger(__name__)

DEFAULT_PORT = 8006
API = "/api2/json"
TICKET_RENEW_S = 90 * 60  # tickets are valid for 2 hours
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,62}$")
TOKEN_ID_RE = re.compile(r"^[^\s!@]+@[^\s!@]+![A-Za-z][A-Za-z0-9._\-]*$")


class ProxmoxError(RuntimeError):
    """A Proxmox API call failed (network or HTTP status)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(redact(message))
        self.status = status


class ProxmoxAuthError(ProxmoxError):
    """Authentication failure (wrong token, user or password) or missing permission."""


@dataclass
class PveVm:
    vmid: int
    name: str
    node: str
    status: str = ""  # running | stopped | paused | ...
    template: bool = False
    maxmem: int = 0
    maxcpu: int = 0
    maxdisk: int = 0
    lock: str = ""


def parse_pve_url(raw: str) -> tuple[str, str]:
    """``(base_url, display_host)`` from the login form. The API port defaults to 8006."""
    text = (raw or "").strip()
    if not text:
        raise ProxmoxAuthError("Proxmox address is required")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise ProxmoxAuthError(f"Proxmox URL must be https, not {parts.scheme!r}")
    if parts.username or parts.password:
        raise ProxmoxAuthError("put the user name in the user field, not in the Proxmox URL")
    if not parts.hostname:
        raise ProxmoxAuthError(f"Proxmox URL {raw!r} has no host")
    path = (parts.path or "").rstrip("/")
    if path.endswith(API):
        path = path[: -len(API)]
    if path not in ("", "/"):
        raise ProxmoxAuthError("enter the Proxmox host (for example https://pve.example.com:8006), not a page path")
    port = parts.port or DEFAULT_PORT
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host else host
    return f"https://{netloc}:{port}", host if port == DEFAULT_PORT else f"{host}:{port}"


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {str(v).strip()}" for k, v in errors.items())[:500]
        if body.get("message"):
            return str(body["message"]).strip()[:500]
    return (resp.reason_phrase or resp.text or "").strip()[:500]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_node(node: str) -> str:
    if not NODE_RE.match(node or ""):
        raise ProxmoxError(f"invalid Proxmox node name {node!r}")
    return node


def vm_path(node: str, vmid: int) -> str:
    return f"/nodes/{check_node(node)}/qemu/{int(vmid)}"


class ProxmoxClient:
    """Client for one Proxmox VE endpoint. ``http`` and ``clock`` are injectable for tests."""

    def __init__(self, base_url: str, *, token_id: str = "", token_secret: str = "", username: str = "",
                 password: str = "", verify_ssl: bool = True, http: Optional[httpx.Client] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.token_id = token_id.strip()
        self._token_secret = token_secret.strip()
        self.username = username.strip()
        self._password = password
        self.verify_ssl = verify_ssl
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0), verify=verify_ssl)
        self._clock = clock
        self._ticket = ""
        self._csrf = ""
        self._ticket_at = 0.0
        self._lock = threading.Lock()

    @property
    def auth_mode(self) -> str:
        return "token" if self.token_id else "password"

    @property
    def user(self) -> str:
        """The account the API calls run as (``user@realm``)."""
        return self.token_id.split("!", 1)[0] if self.token_id else self.username

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    # ------------------------------------------------------------- auth
    def login(self) -> str:
        """Check the credentials and return the Proxmox VE version."""
        if self.token_id:
            if not TOKEN_ID_RE.match(self.token_id):
                raise ProxmoxAuthError("API token ID must look like user@realm!tokenid")
            if not self._token_secret:
                raise ProxmoxAuthError("API token secret is required")
        else:
            if not self.username or not self._password:
                raise ProxmoxAuthError("user name and password are required")
            if "@" not in self.username:
                self.username += "@pam"
            self._new_ticket()
        return self.version()

    def _new_ticket(self) -> None:
        try:
            resp = self.http.post(self.base_url + API + "/access/ticket",
                                  data={"username": self.username, "password": self._password},
                                  headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ProxmoxError(f"POST /access/ticket: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProxmoxAuthError("invalid user name or password", status=resp.status_code)
        if resp.status_code >= 400:
            raise ProxmoxError(f"POST /access/ticket: HTTP {resp.status_code}: {_error_text(resp)}",
                               status=resp.status_code)
        data = (resp.json() or {}).get("data") or {}
        if data.get("NeedTFA"):
            raise ProxmoxAuthError("the account uses two-factor authentication; log in with an API token instead")
        if not data.get("ticket"):
            raise ProxmoxAuthError("Proxmox returned no ticket for this user")
        self._ticket = str(data["ticket"])
        self._csrf = str(data.get("CSRFPreventionToken") or "")
        self._ticket_at = self._clock()

    def _auth_headers(self, method: str) -> dict[str, str]:
        if self.token_id:
            return {"Authorization": f"PVEAPIToken={self.token_id}={self._token_secret}"}
        with self._lock:
            if not self._ticket or self._clock() - self._ticket_at > TICKET_RENEW_S:
                self._new_ticket()
            headers = {"Cookie": f"PVEAuthCookie={self._ticket}"}
            if method != "GET" and self._csrf:
                headers["CSRFPreventionToken"] = self._csrf
            return headers

    # ------------------------------------------------------------- plumbing
    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                data: Optional[dict] = None) -> Any:
        """The ``data`` member of the JSON response."""
        url = self.base_url + API + path
        headers = {"Accept": "application/json", **self._auth_headers(method)}
        try:
            resp = self.http.request(method, url, params=params, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise ProxmoxError(f"{method} {path}: {exc}") from exc
        if resp.status_code == 401:
            raise ProxmoxAuthError(f"{method} {path}: HTTP 401: {_error_text(resp) or 'authentication failure'}",
                                   status=401)
        if resp.status_code == 403:
            raise ProxmoxAuthError(f"{method} {path}: HTTP 403: {_error_text(resp) or 'permission denied'}",
                                   status=403)
        if resp.status_code >= 400:
            raise ProxmoxError(f"{method} {path}: HTTP {resp.status_code}: {_error_text(resp)}",
                               status=resp.status_code)
        try:
            return (resp.json() or {}).get("data")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- inventory
    def version(self) -> str:
        data = self.request("GET", "/version") or {}
        return str(data.get("version") or data.get("release") or "")

    def cluster_vms(self) -> list[PveVm]:
        """Every QEMU VM in the cluster (containers are not listed)."""
        vms = []
        for row in self.request("GET", "/cluster/resources", params={"type": "vm"}) or []:
            if not isinstance(row, dict) or row.get("type") != "qemu":
                continue
            vmid = _int(row.get("vmid"))
            if not vmid:
                continue
            vms.append(PveVm(
                vmid=vmid,
                name=str(row.get("name") or f"VM {vmid}"),
                node=str(row.get("node") or ""),
                status=str(row.get("status") or ""),
                template=bool(_int(row.get("template"))),
                maxmem=_int(row.get("maxmem")),
                maxcpu=_int(row.get("maxcpu")),
                maxdisk=_int(row.get("maxdisk")),
                lock=str(row.get("lock") or ""),
            ))
        return vms

    def find_vm(self, vmid: int) -> PveVm:
        for vm in self.cluster_vms():
            if vm.vmid == int(vmid):
                return vm
        raise ProxmoxError(f"VM {vmid} not found in the cluster", status=404)

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.request("GET", vm_path(node, vmid) + "/config", params={"current": 1}) or {}

    def vm_status(self, node: str, vmid: int) -> dict:
        return self.request("GET", vm_path(node, vmid) + "/status/current") or {}

    def shutdown(self, node: str, vmid: int, timeout_s: int) -> str:
        return str(self.request("POST", vm_path(node, vmid) + "/status/shutdown",
                                data={"timeout": max(1, int(timeout_s)), "forceStop": 0}) or "")

    def stop(self, node: str, vmid: int) -> str:
        return str(self.request("POST", vm_path(node, vmid) + "/status/stop") or "")

    def agent_interfaces(self, node: str, vmid: int) -> list[dict]:
        """Guest agent network interfaces; empty when the agent is not running."""
        try:
            data = self.request("GET", vm_path(node, vmid) + "/agent/network-get-interfaces") or {}
        except ProxmoxAuthError:
            raise
        except ProxmoxError:
            return []
        result = data.get("result") if isinstance(data, dict) else data
        return [r for r in result or [] if isinstance(r, dict)]

    def agent_osinfo(self, node: str, vmid: int) -> dict:
        """Guest agent ``get-osinfo`` (``id``, ``name``, ``pretty-name``, ``version-id``); empty without an agent."""
        try:
            data = self.request("GET", vm_path(node, vmid) + "/agent/get-osinfo") or {}
        except ProxmoxAuthError:
            raise
        except ProxmoxError:
            return {}
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def snapshots(self, node: str, vmid: int) -> list[str]:
        rows = self.request("GET", vm_path(node, vmid) + "/snapshot") or []
        return [str(r.get("name")) for r in rows if isinstance(r, dict) and r.get("name") not in (None, "current")]

    def node_addresses(self) -> dict[str, str]:
        """Node name to cluster IP from ``/cluster/status`` (empty for nodes that report none)."""
        try:
            rows = self.request("GET", "/cluster/status") or []
        except ProxmoxAuthError:
            raise
        except ProxmoxError:
            return {}
        return {str(r.get("name")): str(r.get("ip") or "") for r in rows
                if isinstance(r, dict) and r.get("type") == "node" and r.get("name")}

    def storage(self, storage_id: str) -> dict:
        """Storage definition (``type``, ``pool``, ...); empty when the user may not read it."""
        try:
            return self.request("GET", "/storage/" + quote(storage_id, safe="")) or {}
        except ProxmoxError:
            return {}
