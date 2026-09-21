"""Can this Azure VM be migrated?  Blocking problems and warnings, decided before anything is created in OCI."""

from __future__ import annotations

from helper_app.azure.inventory import AzureVmDetails
from helper_app.models import AzureCaptureMode

MAX_OCI_VOLUME_BYTES = 32 * 1024**4  # 32 TB block / boot volume limit


def disks_with_active_sas(details: AzureVmDetails) -> list[tuple[str, str]]:
    """``(disk resource id, disk name)`` for managed disks in ``ActiveSAS`` export state."""
    out: list[tuple[str, str]] = []
    for disk_id in details.disk_ids:
        props = details.disk_doc(disk_id).get("properties") or {}
        if str(props.get("diskState") or "") == "ActiveSAS":
            out.append((disk_id, disk_id.rsplit("/", 1)[-1]))
    return out


def preflight(details: AzureVmDetails, capture_mode: AzureCaptureMode = "deallocate") -> list[str]:
    spec = details.spec
    problems: list[str] = []
    if not spec.disks:
        problems.append("VM has no managed disks")
    for d in spec.disks:
        if not d.backing_file:
            problems.append(f"{d.label} is an unmanaged (storage account) disk; convert the VM to managed disks first")
    if details.ephemeral_os_disk:
        problems.append("the OS disk is ephemeral (local to the host) and cannot be exported; recreate the VM with a "
                        "managed OS disk first")
    sec = details.security_type.lower()
    if sec == "confidentialvm":
        problems.append("Confidential VMs cannot be migrated: their OS disk is bound to the confidential guest "
                        "state (VMGS/VMMD) that OCI cannot consume")
    if spec.encrypted_disks:
        problems.append(f"Azure Disk Encryption is enabled on {', '.join(spec.encrypted_disks)}; the copy would ask "
                        "for BitLocker/LUKS keys OCI does not have. Disable ADE on the VM first "
                        "(az vm encryption disable), then migrate")
    if spec.power_state in ("starting", "stopping", "deallocating"):
        problems.append(f"VM is {spec.power_state}; wait until it is running or deallocated")
    elif spec.power_state == "unknown" and capture_mode == "deallocate":
        problems.append("Azure reports no power state for the VM (instance view unavailable); check the VM in the "
                        "portal, or use snapshot mode")
    for disk_id in details.disk_ids:
        doc = details.disk_doc(disk_id)
        props = doc.get("properties") or {}
        name = disk_id.rsplit("/", 1)[-1]
        policy = str(props.get("networkAccessPolicy") or "AllowAll")
        if policy.lower() == "denyall":
            problems.append(f"disk {name} has networkAccessPolicy DenyAll, which forbids exporting it; set it to "
                            "AllowAll (az disk update --network-access-policy AllowAll) and try again")
        public = str(props.get("publicNetworkAccess") or "Enabled").lower()
        if public == "disabled" and policy.lower() != "allowprivate":
            problems.append(f"disk {name} has public network access disabled; the migration tool downloads the "
                            "disk over the internet, enable it (az disk update --public-network-access Enabled)")
        if props.get("diskSizeBytes") and int(props["diskSizeBytes"]) > MAX_OCI_VOLUME_BYTES:
            problems.append(f"disk {name} is larger than the 32 TB OCI volume maximum")
        state = str(props.get("diskState") or "")
        if state == "ActiveSAS" and capture_mode == "deallocate":
            problems.append(f"disk {name} already has an export SAS granted (state ActiveSAS); revoke it "
                            "with Revoke export access below (or az disk revoke-access) - another export or a "
                            "previous job may still be using it")
    return problems


def warnings(details: AzureVmDetails, capture_mode: AzureCaptureMode = "deallocate") -> list[str]:
    spec = details.spec
    notes: list[str] = []
    if capture_mode == "snapshot":
        notes.append("Snapshot mode: the disks are snapshotted while the VM runs; the copy is crash-consistent "
                     "(like a power loss) and does not include changes made after the snapshot. The snapshots "
                     "are deleted when the job ends")
    elif spec.power_state == "stopped":
        notes.append("The VM is stopped but still allocated; Azure only exports the disks of a deallocated VM, so "
                     "the migration tool deallocates it before the copy")
    for disk_id in details.disk_ids:
        props = details.disk_doc(disk_id).get("properties") or {}
        if str(props.get("networkAccessPolicy") or "").lower() == "allowprivate":
            notes.append(f"disk {disk_id.rsplit('/', 1)[-1]} allows exports through its private endpoint only; the "
                         "migration tool VM must reach that endpoint (Private Link to OCI) or the download fails")
    if spec.secure_boot:
        notes.append("Trusted Launch with Secure Boot is enabled on the source; the OCI instance is launched as a "
                     "shielded instance with Secure Boot (x86 shape required, the guest's boot loader must be signed)")
    if spec.num_cpu % 2:
        notes.append(f"{spec.num_cpu} vCPUs round up to {(spec.num_cpu + 1) // 2} OCPUs")
    if spec.is_windows:
        notes.append("Windows guest: install the Oracle VirtIO drivers before the migration, or choose Maximum "
                     "compatibility (IDE + E1000) and install them afterwards")
    else:
        notes.append("Linux guest from Azure: the Azure Linux Agent (waagent) and the cloud-init Azure datasource "
                     "keep looking for the Azure metadata service after the move; they time out and can be removed "
                     "once the instance runs in OCI")
    for disk in spec.disks:
        if disk.capacity_bytes < 50 * 1024**3:
            notes.append(f"{disk.label} is smaller than 50 GB; the OCI volume will be 50 GB (minimum)")
    if len(spec.nics) > 1:
        notes.append(f"the VM has {len(spec.nics)} network interfaces; the OCI instance gets one VNIC")
    return notes
