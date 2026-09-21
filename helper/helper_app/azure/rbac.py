"""Hints when Azure RBAC denies migration-tool actions (built-in roles are easy to get wrong)."""

from __future__ import annotations

from helper_app.azure.client import AzureError


def revoke_export_access_hint(exc: AzureError, disk_name: str) -> str:
    msg = str(exc)
    if exc.status == 403 and "disks/endGetAccess" in msg:
        msg += (
            " The built-in Disk Snapshot Contributor role grants disk export (beginGetAccess) but not "
            "revoke (endGetAccess). Assign Disk Restore Operator on the resource group (or add "
            "Microsoft.Compute/disks/endGetAccess/action to your custom role), wait about a minute, "
            "log in to Azure again in the UI, and retry Revoke export access. A subscription Owner can "
            f"also run: az disk revoke-access -g <resource-group> -n {disk_name}"
        )
    return msg
