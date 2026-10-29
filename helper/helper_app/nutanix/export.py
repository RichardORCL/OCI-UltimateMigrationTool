"""Expose AHV vDisks for download as temporary Prism images.

Each disk is cloned into a ``DISK_IMAGE`` (raw), downloaded, then the image is deleted. Image
uuids are recorded on ``NutanixSourceInfo.image_uuids`` as soon as they are known, so a job
that fails or is cancelled can still delete them with a later cleanup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from helper_app.branding import PREFIX
from helper_app.models import NutanixSourceInfo
from helper_app.nutanix.client import AhvDisk, NutanixClient, NutanixError
from helper_app.nutanix.regions import Extent, RegionMapper, release_region_resources

log = logging.getLogger(__name__)

_NAME = re.compile(r"[^A-Za-z0-9_.\-]")


def image_name(job_id: str, index: int) -> str:
    return _NAME.sub("-", f"{PREFIX}-{job_id[:8]}-disk{index}")[:80]


@dataclass
class DiskImage:
    uuid: str
    url: str
    size: int
    ranges: bool
    probe: str = ""  # what each download URL answered to a range request


def _short(url: str) -> str:
    path = urlsplit(url).path
    return path.split("/images/")[0] or path


class NutanixDiskExport:
    """Context manager: ``open`` clones one disk into an image; ``close``/exit deletes every image left."""

    def __init__(self, client: NutanixClient, job_id: str, info: NutanixSourceInfo, *,
                 image_timeout_s: float, save: Callable[[], None],
                 check_cancel: Optional[Callable[[], None]] = None):
        self.client = client
        self.job_id = job_id
        self.info = info
        self.image_timeout_s = image_timeout_s
        self._save = save
        self._check = check_cancel or (lambda: None)

    def __enter__(self) -> "NutanixDiskExport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self, index: int) -> DiskImage:
        """Clone disk ``index`` of the job into a fresh image and return where to download it."""
        self._check()
        disk = AhvDisk(uuid=self.info.disk_ids[index],
                       container_uuid=(self.info.container_uuids[index]
                                       if index < len(self.info.container_uuids) else ""))
        name = image_name(self.job_id, index)
        stale = self.client.find_image(name)
        if stale:
            log.info("job %s: deleting stale image %s (%s) from an earlier attempt", self.job_id, name, stale)
            self._delete(stale)
        image_uuid, task = self.client.create_disk_image(name, disk)
        if image_uuid:
            self._record(image_uuid)
        if task:
            result = self.client.wait_task(task, self.image_timeout_s, check_cancel=self._check)
            if not image_uuid:
                image_uuid = next((u for u in result.entity_uuids if u and u != disk.uuid), "")
        if not image_uuid:
            image_uuid = self.client.find_image(name)
        if not image_uuid:
            raise NutanixError(f"Prism did not report the image created from vDisk {disk.uuid}")
        self._record(image_uuid)
        # first URL that answers range reads, else the first that downloads at all
        found: list[tuple[str, int, bool]] = []
        notes: list[str] = []
        last: Optional[NutanixError] = None
        for candidate in self.client.image_file_urls(image_uuid):
            try:
                size, ranges, note = self.client.probe_file(candidate)
            except NutanixError as exc:
                if exc.status not in (404, 405):
                    raise
                last = exc
                notes.append(f"{_short(candidate)}: HTTP {exc.status}")
                continue
            notes.append(f"{_short(candidate)}: {note}")
            found.append((candidate, size, ranges))
            if ranges:
                break
        if not found:
            raise last or NutanixError(f"image {image_uuid} has no download URL")
        url, size, ranges = next((f for f in found if f[2]), found[0])
        if not size:
            size = self.client.image_size(image_uuid)
        return DiskImage(uuid=image_uuid, url=url, size=size, ranges=ranges, probe="; ".join(notes))

    def map_regions(self, vm_uuid: str) -> dict[str, list[Extent]]:
        """Data extents per vDisk uuid (``RegionsUnavailable`` when Prism cannot say)."""
        mapper = RegionMapper(self.client, self.job_id, self.info, timeout_s=self.image_timeout_s,
                              save=self._save, check_cancel=self._check)
        return mapper.map(vm_uuid, list(self.info.disk_ids))

    def finish(self, image: DiskImage) -> None:
        self._delete(image.uuid)

    def close(self) -> None:
        release_nutanix_resources(self.client, self.info)
        self._save()

    def _record(self, image_uuid: str) -> None:
        if image_uuid not in self.info.image_uuids:
            self.info.image_uuids.append(image_uuid)
            self._save()

    def _delete(self, image_uuid: str) -> None:
        try:
            self.client.delete_image(image_uuid)
        except NutanixError as exc:
            log.warning("job %s: could not delete image %s: %s", self.job_id, image_uuid, exc)
            return
        if image_uuid in self.info.image_uuids:
            self.info.image_uuids.remove(image_uuid)
            self._save()


def release_nutanix_resources(client: NutanixClient, info: NutanixSourceInfo) -> list[str]:
    """Delete every temporary image, recovery point and protection domain recorded on ``info``.
    Best effort; leftovers stay listed."""
    actions: list[str] = release_region_resources(client, info)
    for image_uuid in list(info.image_uuids):
        if not image_uuid:
            info.image_uuids.remove(image_uuid)
            continue
        try:
            client.delete_image(image_uuid)
            info.image_uuids.remove(image_uuid)
            actions.append(f"ok: deleted image {image_uuid}")
        except NutanixError as exc:
            actions.append(f"failed: delete image {image_uuid}: {exc}")
    return actions
