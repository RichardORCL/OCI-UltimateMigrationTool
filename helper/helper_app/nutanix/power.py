"""Shut an AHV VM down before its disks are cloned.

ACPI shutdown first (handled by the guest, through NGT when installed), then a hard power off
if the VM is still on when the timeout expires. The VM is left powered off.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from helper_app.nutanix.client import NutanixClient, NutanixError


def _is_off(client: NutanixClient, vm_uuid: str) -> bool:
    return client.get_vm(vm_uuid).power_state == "OFF"


def _wait_off(client: NutanixClient, vm_uuid: str, timeout_s: float, on_wait: Optional[Callable[[], None]],
              sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _is_off(client, vm_uuid):
            return True
        if time.monotonic() >= deadline:
            return False
        if on_wait is not None:
            on_wait()
        sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def shut_down(client: NutanixClient, vm_uuid: str, timeout_s: float, *,
              on_wait: Optional[Callable[[], None]] = None,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """Return ``already_off``, ``guest_shutdown`` or ``powered_off``.

    Raises ``NutanixError`` when the VM is still on after the ACPI shutdown and the hard power off.
    """
    if _is_off(client, vm_uuid):
        return "already_off"
    client.set_power(vm_uuid, "ACPI_SHUTDOWN")
    if _wait_off(client, vm_uuid, timeout_s, on_wait, sleep):
        return "guest_shutdown"
    client.set_power(vm_uuid, "OFF")
    if _wait_off(client, vm_uuid, timeout_s, on_wait, sleep):
        return "powered_off"
    raise NutanixError(f"VM {vm_uuid} did not power off after ACPI shutdown and hard power off")
