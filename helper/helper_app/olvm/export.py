"""Download one OLVM disk through an oVirt image transfer.

The engine locks the disk, hands back a proxy URL and a signed ticket, and the bytes are
read from that URL. The transfer is finalized on success and cancelled on failure or exit
so the disk lock is released.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from helper_app.olvm.client import OlvmClient, OlvmError

log = logging.getLogger(__name__)

_FAILED = {"finished_failure", "finalizing_failure", "cancelled", "paused_by_system"}


@dataclass
class ImageTransfer:
    id: str
    url: str
    ticket: str


def transfer_download_url(transfer: dict) -> str:
    """Prefer the engine image proxy so the tool VM only has to reach the manager."""
    return str(transfer.get("proxy_url") or transfer.get("transfer_url") or "").rstrip("/")


class OlvmDiskExport:
    """Opens image transfers for the job's disks and closes whatever is still open on exit."""

    def __init__(self, client: OlvmClient, *, inactivity_timeout_s: int, ready_timeout_s: float,
                 sleep: Callable[[float], None] = time.sleep,
                 check_cancel: Optional[Callable[[], None]] = None):
        self.client = client
        self.inactivity_timeout_s = inactivity_timeout_s
        self.ready_timeout_s = ready_timeout_s
        self.sleep = sleep
        self.check_cancel = check_cancel
        self.open_ids: list[str] = []

    def __enter__(self) -> "OlvmDiskExport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for transfer_id in list(self.open_ids):
            try:
                self.client.set_transfer_phase(transfer_id, "cancelled")
            except OlvmError as err:
                log.warning("could not cancel image transfer %s: %s", transfer_id, err)
            self._drop(transfer_id)

    def open(self, disk_id: str) -> ImageTransfer:
        self._cancel_open()
        if self.check_cancel is not None:
            self.check_cancel()
        created = self.client.create_transfer(disk_id, self.inactivity_timeout_s)
        transfer_id = str(created["id"])
        self.open_ids.append(transfer_id)
        ready = self._wait_until_transferring(transfer_id)
        url = transfer_download_url(ready)
        ticket = str(ready.get("signed_ticket") or "")
        if not url or not ticket:
            raise OlvmError(f"image transfer {transfer_id} has no proxy URL or ticket")
        return ImageTransfer(id=transfer_id, url=url, ticket=ticket)

    def refresh(self, transfer: ImageTransfer) -> ImageTransfer:
        current = self.client.get_transfer(transfer.id)
        ticket = str(current.get("signed_ticket") or "")
        if ticket:
            transfer.ticket = ticket
        url = transfer_download_url(current)
        if url:
            transfer.url = url
        return transfer

    def finish(self, transfer: ImageTransfer) -> None:
        self.client.set_transfer_phase(transfer.id, "finalizing_success")
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
                self.client.set_transfer_phase(transfer_id, "cancelled")
            except OlvmError as err:
                log.warning("could not cancel image transfer %s: %s", transfer_id, err)
            self._drop(transfer_id)

    def _drop(self, transfer_id: str) -> None:
        self.open_ids = [item for item in self.open_ids if item != transfer_id]
