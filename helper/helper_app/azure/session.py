"""Azure login for the web UI: a service principal (tenant, client ID, secret) becomes an ``AzureSession``.

Like the vCenter login, nothing is persisted: the credentials live in the ``AzureClient`` of the UI
session and go away with it.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Optional

from helper_app.azure.client import AzureAuthError, AzureClient, AzureError
from helper_app.config import Settings
from helper_app.models import AzureSubscription

log = logging.getLogger(__name__)

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")


class AzureSession:
    """An authenticated service principal bound to one UI session."""

    def __init__(self, client: AzureClient, subscriptions: list[AzureSubscription]):
        self.client = client
        self.tenant_id = client.tenant_id
        self.client_id = client.client_id
        self.subscriptions = subscriptions
        self._lock = threading.RLock()
        self._closed = False

    @property
    def username(self) -> str:
        return f"azure:{self.client_id}"

    @property
    def label(self) -> str:
        return f"{self.client_id} @ {self.tenant_id}"

    def subscription_name(self, subscription_id: str) -> str:
        for s in self.subscriptions:
            if s.id == subscription_id:
                return s.name or s.id
        return subscription_id

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.client.close()


ClientFactory = Callable[[str, str, str], AzureClient]


class AzureConnector:
    """Turns the login form into an ``AzureSession``; ``client_factory`` is injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None):
        self.s = settings
        self._factory = client_factory or (lambda t, c, s: AzureClient(t, c, s))

    def login(self, tenant_id: str, client_id: str, client_secret: str) -> AzureSession:
        tenant_id, client_id = (tenant_id or "").strip(), (client_id or "").strip()
        if not tenant_id or not client_id or not client_secret:
            raise AzureAuthError("tenant ID, client ID and client secret are required")
        if not _TENANT.match(tenant_id):
            raise AzureAuthError(f"invalid tenant ID {tenant_id!r} (a GUID or a verified domain such as "
                                 "contoso.onmicrosoft.com)")
        if not _GUID.match(client_id):
            raise AzureAuthError(f"invalid client ID {client_id!r} (the application's GUID)")
        client = self._factory(tenant_id, client_id, client_secret)
        log.info("Azure login: service principal %s in tenant %s", client_id, tenant_id)
        try:
            client.token()
            subs = [AzureSubscription(id=s["id"], name=s["name"], state=s.get("state", ""))
                    for s in client.list_subscriptions()]
        except AzureError:
            client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise AzureError(f"Azure login failed: {exc}") from exc
        subs = [s for s in subs if (s.state or "Enabled").lower() in ("enabled", "pastdue", "warned", "")]
        if not subs:
            client.close()
            raise AzureAuthError("the service principal can see no subscription; assign it the Reader role (plus "
                                 "the disk export actions) on the subscriptions holding the VMs to migrate")
        subs.sort(key=lambda s: (s.name.lower(), s.id))
        return AzureSession(client, subs)
