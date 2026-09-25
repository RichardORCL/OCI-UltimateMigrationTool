"""Shut a Hyper-V VM down before its disks are read.

Guest shutdown first (``Stop-VM``, via integration services), then ``Stop-VM -TurnOff`` if the VM
is still running when the timeout expires. The VM is left off, the same as a vSphere export.
A saved or paused VM is not turned off here: that would discard the saved state.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from helper_app.hyperv.client import HypervClient, HypervError


def _is_off(client: HypervClient, vm_id: str) -> bool:
    return client.state(vm_id).lower() == "off"


def _wait_off(client: HypervClient, vm_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]],
              sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _is_off(client, vm_id):
            return True
        if time.monotonic() >= deadline:
            return False
        if on_wait is not None:
            on_wait()
        sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def shut_down(client: HypervClient, vm_id: str, timeout_s: float, *,
              on_wait: Optional[Callable[[], None]] = None,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """Return ``already_off``, ``guest_shutdown`` or ``powered_off``.

    Raises ``HypervError`` when the VM is still not off after the guest shutdown and the hard turn-off.
    """
    if _is_off(client, vm_id):
        return "already_off"
    client.shutdown(vm_id)
    if _wait_off(client, vm_id, timeout_s, on_wait, sleep):
        return "guest_shutdown"
    client.turn_off(vm_id)
    if _wait_off(client, vm_id, timeout_s, on_wait, sleep):
        return "powered_off"
    raise HypervError(f"VM {vm_id} did not power off after shutdown and turn-off")
