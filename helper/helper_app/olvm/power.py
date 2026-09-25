"""Shut an OLVM VM down before its disks are transferred.

Guest shutdown first (the engine asks the guest agent), then a forced stop if the VM is
still up when the timeout expires. The VM is left powered off, the same as a vSphere export.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from helper_app.olvm.client import OlvmClient, OlvmError


def _is_down(client: OlvmClient, vm_id: str) -> bool:
    vm = client.get_vm(vm_id)
    return str(vm.get("status") or "").lower() == "down"


def _wait_down(client: OlvmClient, vm_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]],
               sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _is_down(client, vm_id):
            return True
        if time.monotonic() >= deadline:
            return False
        if on_wait is not None:
            on_wait()
        sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def shut_down(client: OlvmClient, vm_id: str, timeout_s: float, *,
              on_wait: Optional[Callable[[], None]] = None,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """Return ``already_off``, ``guest_shutdown`` or ``powered_off``.

    Raises ``OlvmError`` when the VM is still not down after the guest shutdown and the forced stop.
    """
    if _is_down(client, vm_id):
        return "already_off"
    client.vm_action(vm_id, "shutdown")
    if _wait_down(client, vm_id, timeout_s, on_wait, sleep):
        return "guest_shutdown"
    client.vm_action(vm_id, "stop", {"force": True})
    if _wait_down(client, vm_id, timeout_s, on_wait, sleep):
        return "powered_off"
    raise OlvmError(f"VM {vm_id} did not power off after shutdown and stop")
