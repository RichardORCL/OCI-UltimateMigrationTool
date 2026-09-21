"""Best-effort OCI teardown; retries tolerate resources already deleted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from helper_app.models import Job, JobPhase
from helper_app.oci.clients import describe_error

if TYPE_CHECKING:
    from helper_app.oci.provision import Provisioner


def cleanup_resources(provisioner: Provisioner, job: Job) -> list[str]:
    """Best-effort teardown of everything this job created.  Returns a list of actions."""
    actions: list[str] = []

    def attempt(desc: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            actions.append(f"ok: {desc}")
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                actions.append(f"ok: {desc} (already absent)")
            else:
                actions.append(f"failed: {desc}: {describe_error(exc)}")

    try:
        provisioner._detach_all_from_helper(job)
    except Exception as exc:  # noqa: BLE001
        actions.append(f"failed: detach from the migration tool VM: {exc}")
    # data volumes attached to the target in prepare(): release them first, otherwise deleting the volume
    # races the detach that terminating the instance triggers
    for disk in job.disks[1:]:
        if disk.target_attachment_id:
            attempt(f"detach disk {disk.index} from target", lambda d=disk: provisioner._detach_from_target(d))
    if job.instance_id:
        attempt(
            f"terminate instance {job.instance_id}",
            lambda: provisioner.c.compute.terminate_instance(job.instance_id, preserve_boot_volume=False),
        )
    for disk in job.disks:
        if disk.volume_id and disk.is_boot:
            attempt(
                f"delete boot volume {disk.volume_id}",
                lambda vid=disk.volume_id: provisioner.c.blockstorage.delete_boot_volume(vid),
            )
        elif disk.volume_id:
            attempt(
                f"delete volume {disk.volume_id}",
                lambda vid=disk.volume_id: provisioner.c.blockstorage.delete_volume(vid),
            )
    job.phase = JobPhase.CANCELLED
    job.step = "cancelled"
    job.message = "; ".join(actions) or "nothing to clean up"
    provisioner.save(job)
    return actions
