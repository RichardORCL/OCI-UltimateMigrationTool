"""Export GCP persistent disks via snapshots to a user GCS bucket."""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from helper_app.branding import PREFIX, TAG_JOB
from helper_app.gcp.client import GcpClient, GcpError
from helper_app.models import GcpSourceInfo

log = logging.getLogger(__name__)

_SNAP = re.compile(r"[^a-z0-9-]")


def snapshot_name(job_id: str, disk_index: int, disk_name: str) -> str:
    base = _SNAP.sub("-", disk_name.lower())[:40]
    return f"{PREFIX}-{job_id[:8]}-d{disk_index}-{base}"[:63].rstrip("-")


def object_name(prefix: str, index: int) -> str:
    p = prefix.rstrip("/")
    return f"{p}/{index}-disk.tar.gz"


def daisy_scratch_bucket(project: str, zone: str) -> str:
    """Default bucket name ``gce_vm_image_export`` creates when no scratch path is set."""
    region = zone.rsplit("-", 1)[0] if zone else ""
    return f"{project}-daisy-bkt-{region}" if region else ""


class GcpDiskExport:
    """On enter: snapshots + export to GCS; on exit: delete objects and snapshots."""

    def __init__(
        self,
        client: GcpClient,
        job_id: str,
        info: GcpSourceInfo,
        *,
        snapshot_timeout_s: float,
        export_timeout_s: float,
        save: Callable[[], None],
        check_cancel: Optional[Callable[[], None]] = None,
        labels: Optional[dict[str, str]] = None,
    ):
        self.client = client
        self.job_id = job_id
        self.info = info
        self.snapshot_timeout_s = snapshot_timeout_s
        self.export_timeout_s = export_timeout_s
        self._save = save
        self._check = check_cancel or (lambda: None)
        self.labels = labels or {TAG_JOB: job_id}
        self._objects: dict[int, str] = {}

    def __enter__(self) -> "GcpDiskExport":
        try:
            self._ensure_snapshots_and_exports()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_snapshots_and_exports(self) -> None:
        project, zone = self.info.project_id, self.info.zone
        for index, disk_url in enumerate(self.info.disk_urls):
            self._check()
            snap = self.info.snapshot_names[index] if index < len(self.info.snapshot_names) and self.info.snapshot_names[index] else ""
            if not snap:
                disk_name = disk_url.rsplit("/disks/", 1)[-1].split("/", 1)[0]
                snap = snapshot_name(self.job_id, index, disk_name)
                while len(self.info.snapshot_names) <= index:
                    self.info.snapshot_names.append("")
                self.info.snapshot_names[index] = snap
                self._save()
                log.info("job %s: snapshot %s of %s", self.job_id, snap, disk_name)
                self.client.create_snapshot(
                    project, zone, disk_name, snap, self.snapshot_timeout_s,
                    labels=self.labels, on_wait=self._check,
                )
            obj = self.info.gcs_objects[index] if index < len(self.info.gcs_objects) and self.info.gcs_objects[index] else ""
            if not obj:
                obj = object_name(self.info.export_prefix, index)
                while len(self.info.gcs_objects) <= index:
                    self.info.gcs_objects.append("")
                self.info.gcs_objects[index] = obj
                self._save()
                log.info("job %s: export %s to gs://%s/%s", self.job_id, snap, self.info.export_bucket, obj)
                self.client.export_snapshot(
                    project, zone, snap, self.info.export_bucket, obj, self.export_timeout_s, on_wait=self._check,
                )
            self._objects[index] = obj

    def gcs_object(self, index: int) -> str:
        return self._objects[index]

    def close(self) -> None:
        release_gcp_resources(self.client, self.info)


def release_gcp_resources(client: GcpClient, info: GcpSourceInfo) -> list[str]:
    actions: list[str] = []
    project = info.project_id
    if info.export_bucket and info.export_prefix:
        actions.extend(client.delete_prefix(info.export_bucket, info.export_prefix.rstrip("/") + "/"))
        info.gcs_objects.clear()
    else:
        for obj in list(info.gcs_objects):
            if not obj:
                continue
            try:
                client.delete_object(info.export_bucket, obj)
                info.gcs_objects.remove(obj)
                actions.append(f"ok: deleted gs://{info.export_bucket}/{obj}")
            except GcpError as exc:
                actions.append(f"failed: delete gs://{info.export_bucket}/{obj}: {exc}")
    daisy = daisy_scratch_bucket(project, info.zone)
    if daisy:
        actions.extend(client.delete_prefix(daisy, "daisy-image-export"))
        try:
            if not client.list_objects(daisy):
                client.delete_bucket(daisy)
                actions.append(f"ok: deleted scratch bucket gs://{daisy}")
        except GcpError as exc:
            if exc.status not in (403, 404):
                actions.append(f"skipped: delete scratch bucket gs://{daisy}: {exc}")
    for snap in list(info.snapshot_names):
        if not snap:
            info.snapshot_names.remove(snap)
            continue
        try:
            client.delete_snapshot(project, snap)
            info.snapshot_names.remove(snap)
            actions.append(f"ok: deleted snapshot {snap}")
        except GcpError as exc:
            actions.append(f"failed: delete snapshot {snap}: {exc}")
    return actions
