"""OLVM login for the web UI: engine URL, user and password become an ``OlvmSession``.

Like the vCenter login, nothing is persisted: the password and bearer token live on the
``OlvmClient`` of the UI session and go away with it.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from helper_app.config import Settings
from helper_app.olvm.client import OlvmAuthError, OlvmClient, OlvmError, parse_engine_url

log = logging.getLogger(__name__)


class OlvmSession:
    """An authenticated engine login bound to one UI session."""

    def __init__(self, client: OlvmClient, engine_host: str, username: str, verify_ssl: bool):
        self.client = client
        self.engine_host = engine_host
        self.username = username
        self.verify_ssl = verify_ssl
        self._lock = threading.RLock()
        self._closed = False

    @property
    def label(self) -> str:
        return f"{self.username} @ {self.engine_host}"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.client.close()


ClientFactory = Callable[[str, str, str, bool], OlvmClient]


class OlvmConnector:
    """Turns the login form into an ``OlvmSession``; ``client_factory`` is injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None):
        self.s = settings
        self._factory = client_factory or (
            lambda base, user, password, verify: OlvmClient(base, user, password, verify_ssl=verify)
        )

    def login(self, engine_url: str, username: str, password: str, verify_ssl: bool = False) -> OlvmSession:
        username = (username or "").strip()
        if not username or not password:
            raise OlvmAuthError("user name and password are required")
        base, display = parse_engine_url(engine_url)
        client = self._factory(base, username, password, bool(verify_ssl))
        log.info("OLVM login: %s at %s", username, display)
        try:
            client.token()
            client.get_api()
        except OlvmError:
            client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise OlvmError(f"OLVM login failed: {exc}") from exc
        return OlvmSession(client, display, username, bool(verify_ssl))
