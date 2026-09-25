"""Hyper-V login for the web UI: host, user and password become a ``HypervSession``.

Like the vCenter login, nothing is persisted. The password lives on the session for WinRM and for
the SMB disk read, and goes away with the session.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from helper_app.config import Settings
from helper_app.hyperv.client import HypervAuthError, HypervClient, HypervError, parse_host
from helper_app.hyperv.smb import SmbFile, open_smb_file

log = logging.getLogger(__name__)

ClientFactory = Callable[..., HypervClient]
OpenerFactory = Callable[[str, str, str], Callable[[str], SmbFile]]


class HypervSession:
    """An authenticated Hyper-V host login bound to one UI session."""

    def __init__(self, client: HypervClient, hostname: str, display: str, username: str, password: str,
                 verify_ssl: bool, opener: Optional[Callable[[str], SmbFile]] = None):
        self.client = client
        self.hostname = hostname
        self.host = display
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._opener = opener
        self._files: list[SmbFile] = []
        self._lock = threading.RLock()
        self._closed = False

    @property
    def label(self) -> str:
        return f"{self.username} @ {self.host}"

    def open_disk(self, unc: str) -> SmbFile:
        """Open one VHD/VHDX read-only. The caller closes it; session close closes anything left."""
        if self._opener is not None:
            handle = self._opener(unc)
        else:
            handle = open_smb_file(self.username, self.password, unc)
        with self._lock:
            self._files.append(handle)
        return handle

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            files = list(self._files)
        for handle in files:
            handle.close()
        self.client.close()


class HypervConnector:
    """Turns the login form into a ``HypervSession``. ``client_factory`` is injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None,
                 opener_factory: Optional[OpenerFactory] = None):
        self.s = settings
        self._factory = client_factory or (
            lambda host, user, password, use_https, port, verify: HypervClient(
                host, user, password, use_https=use_https, port=port, verify_ssl=verify)
        )
        self._opener_factory = opener_factory

    def login(self, host: str, username: str, password: str, *, use_https: bool = True,
              verify_ssl: bool = False) -> HypervSession:
        username = (username or "").strip()
        if not username or not password:
            raise HypervAuthError("user name and password are required")
        hostname, port, display = parse_host(host, use_https)
        client = self._factory(hostname, username, password, bool(use_https), port, bool(verify_ssl))
        log.info("Hyper-V login: %s at %s", username, display)
        try:
            client.probe()
        except HypervError:
            client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise HypervError(f"Hyper-V login failed: {exc}") from exc
        opener = self._opener_factory(hostname, username, password) if self._opener_factory else None
        return HypervSession(client, hostname, display, username, password, bool(verify_ssl), opener=opener)
