"""Retryable cleanup of temporary cloud-source resources using the matching login."""

from collections.abc import Callable

from helper_app.aws.export import release_aws_resources as release_aws
from helper_app.azure.export import release_azure_resources as release_azure
from helper_app.gcp.export import release_gcp_resources as release_gcp
from helper_app.models import Job
from helper_app.nutanix.export import release_nutanix_resources as release_nutanix
from helper_app.proxmox.export import release_proxmox_resources as release_proxmox
from helper_app.sessions import UserSession


def release_source(job: Job, session: UserSession | None, save: Callable[[Job], Job]) -> None:
    providers = {
        "aws": (job.aws, release_aws, "AWS", ("snapshot_ids",)),
        "azure": (job.azure, release_azure, "Azure", ("sas_granted", "snapshot_ids")),
        "gcp": (job.gcp, release_gcp, "GCP", ("snapshot_names", "gcs_objects", "export_prefix")),
        "nutanix": (job.nutanix, release_nutanix, "Nutanix",
                    ("image_uuids", "recovery_point_ids", "protection_domains")),
        "proxmox": (job.proxmox, release_proxmox, "Proxmox", ("nbd_exports",)),
    }
    entry = providers.get(job.kind)
    if entry is None:
        return
    info, release, label, fields = entry
    if info is None:
        return
    pending = []
    for field in fields:
        value = getattr(info, field)
        pending.extend([value] if isinstance(value, str) and value else value or [])
    if not pending:
        return
    backend = session.cloud_backend(job.kind) if session else None
    if backend is None:
        message = (
            f"{label} resources left behind (no {label} login to release them): "
            + ", ".join(pending)
            + f" - log in to {label} and retry cleanup"
        )
    else:
        # Proxmox exports are stopped over SSH, which the session (not the API client) holds
        actions = release(backend if job.kind == "proxmox" else backend.client, info)
        message = f"{label}: " + "; ".join(actions) if actions else ""
    if message:
        job.message = (job.message + "; " if job.message else "") + message
    save(job)
