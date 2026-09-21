"""Can this EC2 instance be migrated?  Blocking problems and warnings, decided before anything is created in OCI."""

from __future__ import annotations

from helper_app.aws.inventory import AwsVmDetails
from helper_app.models import AwsCaptureMode

MAX_OCI_VOLUME_BYTES = 32 * 1024**4


def preflight(details: AwsVmDetails, capture_mode: AwsCaptureMode = "stop") -> list[str]:
    spec = details.spec
    problems: list[str] = []
    if details.root_device_type == "instance-store":
        problems.append("the root device is instance store (ephemeral) and cannot be snapshotted; only EBS-backed "
                        "instances can be migrated")
    if not spec.disks:
        problems.append("instance has no EBS volumes")
    for d in spec.disks:
        if not d.backing_file:
            problems.append(f"{d.label} is not an EBS volume and cannot be exported")
        if d.capacity_bytes > MAX_OCI_VOLUME_BYTES:
            problems.append(f"{d.label} is larger than the 32 TB OCI volume maximum")
    if details.product_codes:
        problems.append("the AMI has an AWS Marketplace product code; those images often forbid snapshots. "
                        "Confirm the instance can be snapshotted in EC2, or recreate it from a non-marketplace AMI")
    if spec.power_state in ("pending", "stopping", "shutting-down"):
        problems.append(f"instance is {spec.power_state}; wait until it is running or stopped")
    elif spec.power_state == "terminated":
        problems.append("instance is terminated")
    elif spec.power_state == "unknown" and capture_mode == "stop":
        problems.append("AWS reports no power state for the instance; check it in the console, or use snapshot mode")
    return problems


def warnings(details: AwsVmDetails, capture_mode: AwsCaptureMode = "stop") -> list[str]:
    spec = details.spec
    notes: list[str] = []
    if capture_mode == "snapshot":
        notes.append("Snapshot mode: EBS snapshots are taken while the instance runs; the copy is crash-consistent "
                     "(like a power loss) and does not include changes made after the snapshot. The snapshots "
                     "are deleted when the job ends")
    encrypted = [vid for vid in details.volume_ids
                 if str(details.volume_doc(vid).get("encrypted") or "").lower() in ("true", "1")]
    if encrypted:
        notes.append("one or more EBS volumes are encrypted: the IAM user needs kms:Decrypt / CreateGrant on the "
                     "volume keys or snapshot creation fails")
    store = [d for d in spec.disks if not d.backing_file]
    if store:
        notes.append("instance-store data disks are skipped (only EBS volumes are copied)")
    if spec.secure_boot:
        notes.append("UEFI Secure Boot is enabled on the source; the OCI instance is launched as a shielded "
                     "instance with Secure Boot (x86 shape required)")
    if spec.num_cpu % 2:
        notes.append(f"{spec.num_cpu} vCPUs round up to {(spec.num_cpu + 1) // 2} OCPUs")
    if spec.is_windows:
        notes.append("Windows guest: install the Oracle VirtIO drivers before the migration, or choose Maximum "
                     "compatibility (IDE + E1000) and install them afterwards")
    else:
        notes.append("Linux guest from EC2: cloud-init may wait on the Amazon metadata service after the move; "
                     "amazon-ssm-agent and EC2 instance-connect can be disabled on the copied boot volume")
    for disk in spec.disks:
        if disk.capacity_bytes and disk.capacity_bytes < 50 * 1024**3:
            notes.append(f"{disk.label} is smaller than 50 GB; the OCI volume will be 50 GB (minimum)")
    if len(spec.nics) > 1:
        notes.append(f"the instance has {len(spec.nics)} network interfaces; the OCI instance gets one VNIC")
    return notes
