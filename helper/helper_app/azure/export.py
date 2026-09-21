"""Export access to the managed disks of an Azure VM.

Azure hands out a time-limited read SAS on the page blob behind a managed disk (or a snapshot of it)
through ``beginGetAccess``.  ``AzureDiskExport`` grants those SAS URLs for every disk of the job -
directly on the disks (the VM is deallocated) or on snapshots it creates first (the VM keeps running) -
and revokes / deletes everything again on exit, whatever happened in between.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from helper_app.branding import PREFIX, TAG_JOB
from helper_app.azure.client import AzureClient, AzureError
from helper_app.models import AzureSourceInfo

log = logging.getLogger(__name__)

_SNAPSHOT_NAME = re.compile(r"[^A-Za-z0-9_.\-]")


def snapshot_name(job_id: str, disk_id: str, index: int) -> str:
    """``oci-umt-<job>-<n>-<disk name>``, within Azure's 80 character limit and character set."""
    base = disk_id.rsplit("/", 1)[-1]
    name = f"{PREFIX}-{job_id[:8]}-{index}-{base}"
    name = _SNAPSHOT_NAME.sub("-", name)
    return name[:80].rstrip("-.")


class AzureDiskExport:
    """Context manager: on enter every disk has a SAS URL; on exit nothing is left granted or created."""

    def __init__(self, client: AzureClient, job_id: str, info: AzureSourceInfo, *, sas_duration_s: int,
                 snapshot_timeout_s: float, save: Callable[[], None], check_cancel: Optional[Callable[[], None]] = None,
                 tags: Optional[dict[str, str]] = None):
        self.client = client
        self.job_id = job_id
        self.info = info
        self.sas_duration_s = int(sas_duration_s)
        self.snapshot_timeout_s = snapshot_timeout_s
        self._save = save
        self._check = check_cancel or (lambda: None)
        self.tags = tags or {TAG_JOB: job_id}
        self._sas: dict[int, str] = {}  # disk index -> SAS URL
        self._sources: dict[int, str] = {}  # disk index -> resource granted (disk or snapshot)

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "AzureDiskExport":
        try:
            if self.info.capture_mode == "snapshot":
                self._create_snapshots()
            for index, source in enumerate(self._export_sources()):
                self._check()
                self._grant(index, source)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _export_sources(self) -> list[str]:
        if self.info.capture_mode == "snapshot":
            return list(self.info.snapshot_ids)
        return list(self.info.disk_ids)

    def _create_snapshots(self) -> None:
        existing = list(self.info.snapshot_ids)
        for index, disk_id in enumerate(self.info.disk_ids):
            if index < len(existing) and existing[index]:
                continue  # a retry re-uses the snapshot it made before
            self._check()
            disk = self.client.get_disk(disk_id)
            location = str(disk.get("location") or self.info.location)
            name = snapshot_name(self.job_id, disk_id, index)
            snap_id = (f"/subscriptions/{self.info.subscription_id}/resourceGroups/{self.info.resource_group}"
                       f"/providers/Microsoft.Compute/snapshots/{name}")
            log.info("job %s: creating snapshot %s of %s", self.job_id, name, disk_id.rsplit("/", 1)[-1])
            # record the ID before the call so a crash mid-way still leaves a trace for the cleanup
            while len(self.info.snapshot_ids) <= index:
                self.info.snapshot_ids.append("")
            self.info.snapshot_ids[index] = snap_id
            self._save()
            self.client.create_snapshot(snap_id, disk_id, location, self.snapshot_timeout_s, tags=self.tags,
                                        on_wait=self._check)

    def _grant(self, index: int, source: str) -> str:
        sas = self.client.begin_get_access(source, self.sas_duration_s, on_wait=self._check)
        self._sas[index] = sas
        self._sources[index] = source
        if source not in self.info.sas_granted:
            self.info.sas_granted.append(source)
        self.info.sas_expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.sas_duration_s)
        self._save()
        return sas

    # ---------------------------------------------------------------- access
    def sas_url(self, index: int) -> str:
        return self._sas[index]

    def refresh(self, index: int) -> str:
        """Grant a fresh SAS for one disk (the previous one expired or was revoked)."""
        source = self._sources[index]
        try:
            self.client.end_get_access(source)
        except AzureError as exc:
            log.debug("endGetAccess before refresh failed (ignored): %s", exc)
        return self._grant(index, source)

    @property
    def expires_soon(self) -> bool:
        exp = self.info.sas_expires_at
        return exp is not None and datetime.now(timezone.utc) > exp - timedelta(minutes=10)

    # ---------------------------------------------------------------- cleanup
    def close(self) -> None:
        release_azure_resources(self.client, self.info)
        self._save()


def release_azure_resources(client: AzureClient, info: AzureSourceInfo) -> list[str]:
    """Revoke every export SAS and delete every snapshot recorded on ``info``.  Best effort: the outcome
    of each action is returned; resources that could not be released stay recorded so a later cleanup
    (or the operator) can find them."""
    actions: list[str] = []
    for source in list(info.sas_granted):
        try:
            client.end_get_access(source)
            info.sas_granted.remove(source)
            actions.append(f"ok: revoked export access on {source.rsplit('/', 1)[-1]}")
        except AzureError as exc:
            if exc.status == 404:
                info.sas_granted.remove(source)
                continue
            actions.append(f"failed: revoke export access on {source.rsplit('/', 1)[-1]}: {exc}")
    for snap in list(info.snapshot_ids):
        if not snap:
            info.snapshot_ids.remove(snap)
            continue
        try:
            client.delete_snapshot(snap)
            info.snapshot_ids.remove(snap)
            actions.append(f"ok: deleted snapshot {snap.rsplit('/', 1)[-1]}")
        except AzureError as exc:
            actions.append(f"failed: delete snapshot {snap.rsplit('/', 1)[-1]}: {exc}")
    if not info.sas_granted:
        info.sas_expires_at = None
    return actions
