"""AWS login for the web UI: IAM access key + secret + region become an ``AwsSession``.

Like the Azure login, nothing is persisted: the credentials live in the ``AwsClient`` of the UI
session and go away with it.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from helper_app.aws.client import AwsAuthError, AwsClient, AwsError
from helper_app.config import Settings

log = logging.getLogger(__name__)

_ACCESS_KEY = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16,}$")
_REGION = re.compile(r"^[a-z]{2}-[a-z0-9-]+-\d+$")


class AwsSession:
    """An authenticated IAM user bound to one UI session and one region."""

    def __init__(self, client: AwsClient, account_id: str, arn: str = ""):
        self.client = client
        self.account_id = account_id
        self.arn = arn
        self._closed = False

    @property
    def username(self) -> str:
        return f"aws:{self.account_id}"

    @property
    def region(self) -> str:
        return self.client.region

    @property
    def access_key_id(self) -> str:
        return self.client.access_key_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.client.close()


ClientFactory = Callable[[str, str, str], AwsClient]


class AwsConnector:
    """Turns the login form into an ``AwsSession``; ``client_factory`` is injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None):
        self.s = settings
        self._factory = client_factory or (lambda k, s, r: AwsClient(k, s, r))

    def login(self, access_key_id: str, secret_access_key: str, region: str) -> AwsSession:
        access_key_id = (access_key_id or "").strip()
        secret_access_key = secret_access_key or ""
        region = (region or "").strip()
        if not access_key_id or not secret_access_key or not region:
            raise AwsAuthError("access key ID, secret access key and region are required")
        if not _ACCESS_KEY.match(access_key_id):
            raise AwsAuthError("invalid access key ID (expected AKIA... or ASIA... from an IAM user)")
        if not _REGION.match(region):
            raise AwsAuthError(f"invalid region {region!r} (for example eu-west-1 or us-east-1)")
        client = self._factory(access_key_id, secret_access_key, region)
        log.info("AWS login: access key %s in %s", access_key_id[-4:], region)
        try:
            ident = client.get_caller_identity()
        except AwsAuthError:
            client.close()
            raise
        except AwsError as exc:
            client.close()
            raise AwsAuthError(f"AWS login failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise AwsError(f"AWS login failed: {exc}") from exc
        return AwsSession(client, ident["account"], ident.get("arn") or "")
