"""Snapshot the EBS volumes of an EC2 instance and expose them for EBS Direct block reads."""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from helper_app.aws.client import AwsClient, AwsError
from helper_app.branding import PREFIX, TAG_JOB
from helper_app.models import AwsSourceInfo

log = logging.getLogger(__name__)

_SNAP = re.compile(r"[^A-Za-z0-9_.\-]")


def snapshot_description(job_id: str, volume_id: str, index: int) -> str:
    name = f"{PREFIX}-{job_id[:8]}-{index}-{volume_id}"
    return _SNAP.sub("-", name)[:255]


class AwsDiskExport:
    """Context manager: on enter every volume has a completed snapshot; on exit those snapshots are deleted."""

    def __init__(self, client: AwsClient, job_id: str, info: AwsSourceInfo, *,
                 snapshot_timeout_s: float, save: Callable[[], None],
                 check_cancel: Optional[Callable[[], None]] = None,
                 tags: Optional[dict[str, str]] = None):
        self.client = client
        self.job_id = job_id
        self.info = info
        self.snapshot_timeout_s = snapshot_timeout_s
        self._save = save
        self._check = check_cancel or (lambda: None)
        self.tags = tags or {TAG_JOB: job_id}

    def __enter__(self) -> "AwsDiskExport":
        try:
            self._create_snapshots()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def snapshot_id(self, index: int) -> str:
        return self.info.snapshot_ids[index]

    def _create_snapshots(self) -> None:
        existing = list(self.info.snapshot_ids)
        for index, volume_id in enumerate(self.info.volume_ids):
            if index < len(existing) and existing[index]:
                continue
            self._check()
            desc = snapshot_description(self.job_id, volume_id, index)
            log.info("job %s: snapshotting %s", self.job_id, volume_id)
            while len(self.info.snapshot_ids) <= index:
                self.info.snapshot_ids.append("")
            snap = self.client.create_snapshot(volume_id, desc, self.snapshot_timeout_s,
                                               tags=self.tags, on_wait=self._check)
            self.info.snapshot_ids[index] = snap
            self._save()

    def close(self) -> None:
        release_aws_resources(self.client, self.info)
        self._save()


def release_aws_resources(client: AwsClient, info: AwsSourceInfo) -> list[str]:
    """Delete every snapshot recorded on ``info``. Best effort; leftovers stay listed."""
    actions: list[str] = []
    for snap in list(info.snapshot_ids):
        if not snap:
            info.snapshot_ids.remove(snap)
            continue
        try:
            client.delete_snapshot(snap)
            info.snapshot_ids.remove(snap)
            actions.append(f"ok: deleted snapshot {snap}")
        except AwsError as exc:
            if exc.status == 404 or exc.code in ("InvalidSnapshot.NotFound",):
                info.snapshot_ids.remove(snap)
                continue
            actions.append(f"failed: delete snapshot {snap}: {exc}")
    return actions
