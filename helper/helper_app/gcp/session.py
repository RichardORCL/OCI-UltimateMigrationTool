"""GCP login for the web UI: service account JSON + export bucket → ``GcpSession``."""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from helper_app.config import Settings
from helper_app.gcp.client import GcpAuthError, GcpClient, GcpError, parse_service_account_json
from helper_app.models import GcpProject

log = logging.getLogger(__name__)

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$|^[a-z0-9]$")


class GcpSession:
    def __init__(self, client: GcpClient, export_bucket: str, projects: list[GcpProject]):
        self.client = client
        self.export_bucket = export_bucket
        self.projects = projects
        self._closed = False

    @property
    def username(self) -> str:
        return f"gcp:{self.client.client_email}"

    @property
    def project_id(self) -> str:
        return self.client.project_id

    def project_name(self, project_id: str) -> str:
        for p in self.projects:
            if p.id == project_id:
                return p.name or p.id
        return project_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.client.close()


ClientFactory = Callable[[str], GcpClient]


class GcpConnector:
    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None):
        self.s = settings
        self._factory = client_factory or (lambda raw: GcpClient.from_service_account_json(raw))

    def login(self, service_account_json: str, export_bucket: str) -> GcpSession:
        raw = (service_account_json or "").strip()
        bucket = (export_bucket or "").strip().lower()
        if not raw:
            raise GcpAuthError("service account JSON is required")
        if not bucket:
            raise GcpAuthError("export bucket name is required")
        if bucket.startswith("gs://"):
            bucket = bucket[5:].split("/", 1)[0]
        if not _BUCKET.match(bucket):
            raise GcpAuthError(f"invalid GCS bucket name {bucket!r}")
        sa = parse_service_account_json(raw)
        client = self._factory(raw)
        log.info("GCP login: %s project %s bucket %s", sa["client_email"], sa["project_id"], bucket)
        try:
            client.token()
            client.verify_export_bucket(bucket)
        except GcpAuthError:
            raise
        except GcpError as exc:
            raise GcpAuthError(f"cannot access export bucket {bucket}: {exc}") from exc
        try:
            listed = client.list_projects()
            projects = [GcpProject(id=p["id"], name=p.get("name") or p["id"]) for p in listed if p.get("id")]
        except GcpError as exc:
            log.warning("list projects failed, using service account project only: %s", exc)
            projects = [GcpProject(id=sa["project_id"], name=sa["project_id"])]
        if not projects:
            projects = [GcpProject(id=sa["project_id"], name=sa["project_id"])]
        return GcpSession(client, bucket, projects)
