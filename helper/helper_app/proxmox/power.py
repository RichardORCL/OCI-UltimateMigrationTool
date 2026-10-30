"""Shut a Proxmox VM down before its disks are read.

ACPI shutdown first (through the QEMU guest agent when it is enabled), then a hard stop if the
VM is still running when the timeout expires. The VM is left stopped.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from helper_app.proxmox.client import ProxmoxClient, ProxmoxError


def _is_off(client: ProxmoxClient, node: str, vmid: int) -> bool:
    return str(client.vm_status(node, vmid).get("status") or "") == "stopped"


def _wait_off(client: ProxmoxClient, node: str, vmid: int, timeout_s: float,
              on_wait: Optional[Callable[[], None]], sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _is_off(client, node, vmid):
            return True
        if time.monotonic() >= deadline:
            return False
        if on_wait is not None:
            on_wait()
        sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def shut_down(client: ProxmoxClient, node: str, vmid: int, timeout_s: float, *,
              on_wait: Optional[Callable[[], None]] = None,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """Return ``already_off``, ``guest_shutdown`` or ``powered_off``.

    Raises ``ProxmoxError`` when the VM still runs after the shutdown and the hard stop.
    """
    if _is_off(client, node, vmid):
        return "already_off"
    client.shutdown(node, vmid, int(timeout_s))
    if _wait_off(client, node, vmid, timeout_s, on_wait, sleep):
        return "guest_shutdown"
    client.stop(node, vmid)
    if _wait_off(client, node, vmid, max(60.0, timeout_s / 2), on_wait, sleep):
        return "powered_off"
    raise ProxmoxError(f"VM {vmid} is still running after the ACPI shutdown and a hard stop")
