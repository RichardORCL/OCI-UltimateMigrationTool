"""vCenter connectivity.

Users log in to the web UI with their vCenter credentials.  ``VCenterConnector.login`` opens a
pyVmomi session for them and wraps it in a ``VCenterSession`` that is used for inventory
lookups and for the long running export.
"""

from __future__ import annotations

import logging
import re
import ssl
import threading
from typing import Optional
from urllib.parse import urlsplit

from helper_app.config import Settings
from helper_app.models import VmSummary

log = logging.getLogger(__name__)


class VCenterError(RuntimeError):
    pass


class VCenterAuthError(VCenterError):
    """Wrong user name or password."""


class VCenterSession:
    """A logged-in pyVmomi ``ServiceInstance`` bound to one user."""

    def __init__(self, si, username: str, host: str, port: int = 443, verify_ssl: bool = False):
        self._si = si
        self.username = username
        self.host = host
        self.port = port
        self.verify_ssl = verify_ssl  # chosen on the login page; also applies to the NFC disk download
        self._lock = threading.RLock()
        self._closed = False

    @property
    def service_instance(self):
        return self._si

    @property
    def version(self) -> str:
        try:
            about = self._si.content.about
            return f"{about.fullName}"
        except Exception:  # noqa: BLE001
            return ""

    def vm(self, moid: str):
        """Return a vim.VirtualMachine for ``moid`` (e.g. 'vm-123'), raising if it does not exist."""
        from pyVmomi import vim, vmodl

        vm = vim.VirtualMachine(moid)
        vm._stub = self._si._stub
        try:
            _ = vm.name
        except vmodl.fault.ManagedObjectNotFound as exc:
            raise VCenterError(f"virtual machine {moid} not found") from exc
        return vm

    def list_vms(self) -> list[VmSummary]:
        from helper_app.vsphere.inventory import list_vm_summaries

        with self._lock:
            return list_vm_summaries(self._si)

    def keepalive(self) -> bool:
        """Touch the session so vCenter does not expire it; returns False when it is gone."""
        with self._lock:
            if self._closed:
                return False
            try:
                self._si.CurrentTime()
                return True
            except Exception as exc:  # noqa: BLE001
                log.info("vCenter session of %s is no longer valid: %s", self.username, exc)
                return False

    def close(self) -> None:
        from pyVim.connect import Disconnect

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                Disconnect(self._si)
            except Exception as exc:  # noqa: BLE001
                log.debug("disconnect for %s failed: %s", self.username, exc)


_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?$|^\[[0-9A-Fa-f:.]+\]$")


def parse_vcenter_address(value: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Parse ``host``, ``host:port`` or ``https://host[:port]/`` as typed on the login page."""
    value = (value or "").strip()
    if not value:
        if not default_host:
            raise VCenterError("no vCenter server given and HELPER_VCENTER_HOST is not configured")
        return default_host, default_port
    if "://" in value:
        parts = urlsplit(value)
        host, port = parts.hostname or "", parts.port
        if host and ":" in host:
            host = f"[{host}]"
    else:
        host, port = value, None
        if value.startswith("["):  # [ipv6]:port
            host, _, rest = value.partition("]")
            host += "]"
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        elif value.count(":") == 1:
            host, _, p = value.partition(":")
            if not p.isdigit():
                raise VCenterError(f"invalid vCenter port in {value!r}")
            port = int(p)
    if not host or not _HOST_RE.match(host):
        raise VCenterError(f"invalid vCenter server name {value!r}")
    port = default_port if port is None else port
    if not 1 <= port <= 65535:
        raise VCenterError(f"invalid vCenter port {port}")
    return host, port


def _is_tls_failure(exc: BaseException) -> bool:
    """SmartConnect wraps certificate errors in URLError/OSError; look for the SSL error in the chain."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(cur):
            return True
        reason = getattr(cur, "reason", None)
        cur = reason if isinstance(reason, BaseException) else (cur.__cause__ or cur.__context__)
    return False


class VCenterConnector:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def host(self) -> str:
        return self.s.vcenter_host

    @staticmethod
    def _ssl_context(verify: bool) -> Optional[ssl.SSLContext]:
        if verify:
            return None  # pyVmomi's default: verify against the system CA store
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def login(self, username: str, password: str, host: str = "", port: Optional[int] = None,
              verify_ssl: Optional[bool] = None) -> VCenterSession:
        """Log in to ``host`` (default: the configured vCenter) and return the session.

        ``verify_ssl`` is the choice made on the login page (``None``: the configured default) and is
        remembered on the session so the NFC disk download uses the same setting."""
        from pyVim.connect import SmartConnect
        from pyVmomi import vim

        host, port = parse_vcenter_address(host, self.s.vcenter_host, port or self.s.vcenter_port)
        if not username or not password:
            raise VCenterAuthError("user name and password are required")
        verify = self.s.vcenter_verify_ssl if verify_ssl is None else verify_ssl
        kwargs = {}
        ctx = self._ssl_context(verify)
        if ctx is not None:
            kwargs["sslContext"] = ctx
        log.info("vCenter login %s@%s:%d (TLS verification %s)", username, host, port, "on" if verify else "off")
        try:
            si = SmartConnect(host=host.strip("[]"), port=port, user=username, pwd=password, **kwargs)
        except vim.fault.InvalidLogin as exc:
            raise VCenterAuthError("invalid vCenter user name or password") from exc
        except vim.fault.NoPermission as exc:
            raise VCenterAuthError("the account is not allowed to log in to vCenter") from exc
        except Exception as exc:  # noqa: BLE001
            if _is_tls_failure(exc):
                raise VCenterError(f"the TLS certificate of {host}:{port} could not be verified ({exc}); untick "
                                   "'Verify the server certificate' for a self-signed or VMCA certificate, or "
                                   "install its CA on the migration tool VM") from exc
            raise VCenterError(f"cannot connect to vCenter {host}:{port}: {exc}") from exc
        return VCenterSession(si, username, host, port, verify_ssl=verify)
