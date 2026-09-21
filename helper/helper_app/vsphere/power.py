"""Shutting down a powered-on source VM right before its disks are exported.

Order of preference: guest OS shutdown through VMware Tools (clean file systems), then - when Tools is
not running or the guest does not stop within the timeout - a hard power-off.  The caller confirmed
this on the export page, so no further questions are asked here.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

POWERED_OFF = "poweredOff"
POWERED_ON = "poweredOn"

Notify = Callable[[str], None]


class PowerError(RuntimeError):
    pass


def power_state(vm) -> str:
    return str(vm.runtime.powerState)


def tools_running(vm) -> bool:
    try:
        return str(getattr(vm.guest, "toolsRunningStatus", "") or "") == "guestToolsRunning"
    except Exception:  # noqa: BLE001 - property fetch may fail on a stale object
        return False


def _wait_powered_off(vm, timeout_s: float, poll_s: float, check_cancel: Optional[Callable[[], None]]) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check_cancel is not None:
            check_cancel()
        if power_state(vm) == POWERED_OFF:
            return True
        time.sleep(poll_s)
    return power_state(vm) == POWERED_OFF


def _wait_task(task, timeout_s: float, poll_s: float) -> None:
    """Wait for a vSphere task; ``task`` may be None (fakes) or already finished."""
    if task is None:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = getattr(task, "info", None)
        state = str(getattr(info, "state", "") or "")
        if state == "success":
            return
        if state == "error":
            err = getattr(info, "error", None)
            raise PowerError(f"PowerOffVM failed: {getattr(err, 'msg', None) or err}")
        time.sleep(poll_s)
    raise PowerError("PowerOffVM task did not finish in time")


def shut_down(vm, name: str, timeout_s: float, notify: Notify, check_cancel: Optional[Callable[[], None]] = None,
              poll_s: float = 2.0) -> str:
    """Bring ``vm`` to poweredOff.  Returns how: ``already_off``, ``guest_shutdown`` or ``powered_off``."""
    state = power_state(vm)
    if state == POWERED_OFF:
        return "already_off"
    if state != POWERED_ON:
        # suspended: resuming just to shut down again is not worth the surprise; the operator decides
        raise PowerError(f"VM is {state}; resume and shut it down, or power it off in vCenter, then retry")

    if tools_running(vm):
        notify(f"Shutting down the guest OS of {name} through VMware Tools "
               f"(waiting up to {int(timeout_s)} s, then powering off)")
        try:
            vm.ShutdownGuest()
        except Exception as exc:  # noqa: BLE001 - e.g. ToolsUnavailable raced with a Tools stop
            log.warning("ShutdownGuest for %s failed (%s); powering off", name, exc)
        else:
            if _wait_powered_off(vm, timeout_s, poll_s, check_cancel):
                return "guest_shutdown"
            notify(f"{name} did not shut down within {int(timeout_s)} s; powering it off")
    else:
        notify(f"VMware Tools is not running in {name}; powering the VM off")

    task = vm.PowerOffVM_Task()
    _wait_task(task, timeout_s=120, poll_s=min(poll_s, 1.0))
    if not _wait_powered_off(vm, timeout_s=60, poll_s=min(poll_s, 1.0), check_cancel=check_cancel):
        raise PowerError(f"{name} is still {power_state(vm)} after PowerOffVM")
    return "powered_off"
