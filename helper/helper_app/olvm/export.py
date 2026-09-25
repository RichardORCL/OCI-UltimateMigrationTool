"""Download one OLVM disk through an oVirt image transfer.

The engine locks the disk and hands back a proxy URL. On current OLVM the URL is the
credential (there is no signed ticket). The transfer is finalized on success and cancelled
on failure or exit, using the engine actions, so the disk lock is released.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from helper_app.olvm.client import OlvmClient, OlvmError

log = logging.getLogger(__name__)

_FAILED = {
    "finished_failure", "finalizing_failure", "finished_cleanup",
    "cancelled", "cancelled_user", "cancelled_system", "paused_by_system", "paused_system",
}
# Phases that still hold the disk. finalizing_* is the engine finishing a close; cancelling
# again does not help, but the disk is not free yet.
_BUSY = {"", "initializing", "transferring", "resuming", "paused_user", "paused_system", "paused_by_system"}
_FINALIZING = {"finalizing_success", "finalizing_failure", "finalizing_cleanup", "cancelling"}


@dataclass
class ImageTransfer:
    id: str
    url: str
    ticket: str


def transfer_download_url(transfer: dict, *, direct_from_host: bool = False) -> str:
    """Manager proxy by default. ``direct_from_host`` reads the KVM host's imageio URL instead."""
    proxy = str(transfer.get("proxy_url") or "").rstrip("/")
    host = str(transfer.get("transfer_url") or "").rstrip("/")
    if direct_from_host:
        return host or proxy
    return proxy or host


def transfer_matches_disk(transfer: dict, disk_id: str) -> bool:
    image = transfer.get("image") if isinstance(transfer.get("image"), dict) else {}
    disk = transfer.get("disk") if isinstance(transfer.get("disk"), dict) else {}
    return disk_id in (str(disk.get("id") or ""), str(image.get("id") or ""))


class OlvmDiskExport:
    """Opens image transfers for the job's disks and closes whatever is still open on exit."""

    def __init__(self, client: OlvmClient, *, inactivity_timeout_s: int, ready_timeout_s: float,
                 direct_from_host: bool = False,
                 sleep: Callable[[float], None] = time.sleep,
                 check_cancel: Optional[Callable[[], None]] = None):
        self.client = client
        self.inactivity_timeout_s = inactivity_timeout_s
        self.ready_timeout_s = ready_timeout_s
        self.direct_from_host = direct_from_host
        self.sleep = sleep
        self.check_cancel = check_cancel
        self.open_ids: list[str] = []

    def __enter__(self) -> "OlvmDiskExport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cancel_open()

    def open(self, disk_id: str) -> ImageTransfer:
        self._cancel_open()
        if self.check_cancel is not None:
            self.check_cancel()
        self._release_disk(disk_id)
        try:
            created = self.client.create_transfer(disk_id, self.inactivity_timeout_s)
        except OlvmError as exc:
            if exc.status != 409:
                raise
            log.info("disk %s is locked; cancelling its image transfer and retrying", disk_id)
            self._release_disk(disk_id)
            created = self.client.create_transfer(disk_id, self.inactivity_timeout_s)
        transfer_id = str(created["id"])
        self.open_ids.append(transfer_id)
        ready = self._wait_until_transferring(transfer_id)
        url = transfer_download_url(ready, direct_from_host=self.direct_from_host)
        if self.direct_from_host and not str(ready.get("transfer_url") or "").strip():
            log.warning("direct KVM download requested but transfer %s has no host URL; using the manager proxy",
                        transfer_id)
        if not url:
            raise OlvmError(f"image transfer {transfer_id} has no download URL")
        # Current OLVM leaves signed_ticket empty. The id inside the proxy URL is the credential.
        ticket = str(ready.get("signed_ticket") or "")
        return ImageTransfer(id=transfer_id, url=url, ticket=ticket)

    def refresh(self, transfer: ImageTransfer) -> ImageTransfer:
        try:
            self.client.extend_transfer(transfer.id)
        except OlvmError as exc:
            log.info("could not extend image transfer %s: %s", transfer.id, exc)
        current = self.client.get_transfer(transfer.id)
        ticket = str(current.get("signed_ticket") or "")
        if ticket:
            transfer.ticket = ticket
        url = transfer_download_url(current, direct_from_host=self.direct_from_host)
        if url:
            transfer.url = url
        return transfer

    def finish(self, transfer: ImageTransfer) -> None:
        self.client.finalize_transfer(transfer.id)
        self._drop(transfer.id)

    def _wait_until_transferring(self, transfer_id: str) -> dict:
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            if self.check_cancel is not None:
                self.check_cancel()
            current = self.client.get_transfer(transfer_id)
            phase = str(current.get("phase") or "").lower()
            if phase == "transferring":
                return current
            if phase in _FAILED:
                raise OlvmError(f"image transfer {transfer_id} is {phase}")
            if time.monotonic() >= deadline:
                raise OlvmError(f"image transfer {transfer_id} did not become ready (phase {phase or 'unknown'})")
            self.sleep(1.0)

    def _cancel_open(self) -> None:
        for transfer_id in list(self.open_ids):
            try:
                self.client.cancel_transfer(transfer_id)
            except OlvmError as err:
                log.warning("could not cancel image transfer %s: %s", transfer_id, err)
            self._drop(transfer_id)

    def _release_disk(self, disk_id: str) -> None:
        """Cancel a transfer this disk is still in, then wait until the engine unlocks it."""
        waiting = False
        for transfer in self.client.list_transfers():
            if not transfer_matches_disk(transfer, disk_id):
                continue
            phase = str(transfer.get("phase") or "").lower()
            transfer_id = str(transfer.get("id") or "")
            if phase in _BUSY and transfer_id:
                log.info("cancelling image transfer %s holding disk %s (phase %s)",
                         transfer_id, disk_id, phase or "unknown")
                self.client.cancel_transfer(transfer_id)
                waiting = True
            elif phase in _FINALIZING:
                waiting = True
        if waiting:
            self._wait_disk_unlocked(disk_id)

    def _wait_disk_unlocked(self, disk_id: str) -> None:
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            if self.check_cancel is not None:
                self.check_cancel()
            status = str(self.client.get_disk(disk_id).get("status") or "ok").lower()
            if status == "ok":
                return
            if status not in ("locked", ""):
                raise OlvmError(f"disk {disk_id} is {status}; it cannot be transferred until it is ok")
            if time.monotonic() >= deadline:
                raise OlvmError(f"disk {disk_id} is still locked")
            self.sleep(1.0)

    def _drop(self, transfer_id: str) -> None:
        self.open_ids = [item for item in self.open_ids if item != transfer_id]
