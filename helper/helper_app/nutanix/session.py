"""Nutanix login for the web UI: Prism URL, user and password become a ``NutanixSession``.

Like the vCenter login, nothing is persisted: the password lives on the ``NutanixClient`` of
the UI session and goes away with it.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from helper_app.config import Settings
from helper_app.nutanix.client import NutanixAuthError, NutanixClient, NutanixError, parse_prism_url

log = logging.getLogger(__name__)


class NutanixSession:
    """An authenticated Prism Central or Prism Element login bound to one UI session."""

    def __init__(self, client: NutanixClient, prism_host: str, username: str, verify_ssl: bool):
        self.client = client
        self.prism_host = prism_host
        self.username = username
        self.verify_ssl = verify_ssl
        self._lock = threading.RLock()
        self._closed = False

    @property
    def flavor(self) -> str:
        return self.client.flavor

    @property
    def label(self) -> str:
        kind = "Prism Central" if self.flavor == "pc" else "Prism Element"
        return f"{self.username} @ {self.prism_host} ({kind})"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.client.close()


ClientFactory = Callable[[str, str, str, bool], NutanixClient]


class NutanixConnector:
    """Turns the login form into a ``NutanixSession``; ``client_factory`` is injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None):
        self.s = settings
        self._factory = client_factory or (
            lambda base, user, password, verify: NutanixClient(base, user, password, verify_ssl=verify)
        )

    def login(self, prism_url: str, username: str, password: str, verify_ssl: bool = False) -> NutanixSession:
        username = (username or "").strip()
        if not username or not password:
            raise NutanixAuthError("user name and password are required")
        base, display = parse_prism_url(prism_url)
        client = self._factory(base, username, password, bool(verify_ssl))
        log.info("Nutanix login: %s at %s", username, display)
        try:
            flavor = client.detect()
        except NutanixError:
            client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise NutanixError(f"Nutanix login failed: {exc}") from exc
        log.info("Nutanix login: %s is %s", display, "Prism Central" if flavor == "pc" else "Prism Element")
        return NutanixSession(client, display, username, bool(verify_ssl))
