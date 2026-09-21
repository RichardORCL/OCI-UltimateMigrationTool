"""GCP migration preflight: blocking problems and warnings."""

from __future__ import annotations

from helper_app.gcp.inventory import GcpVmDetails, power_state
from helper_app.models import GcpCaptureMode

MAX_OCI_VOLUME_BYTES = 32 * 1024**4


def confidential_technology(instance: dict) -> str | None:
    """Return the Confidential VM technology (SEV, SEV_SNP, TDX, …) or None for a standard instance."""
    cfg = instance.get("confidentialInstanceConfig")
    if not isinstance(cfg, dict) or not cfg:
        return None
    if cfg.get("enableConfidentialCompute") is True:
        typ = str(cfg.get("confidentialInstanceType") or "").strip()
        return typ or "SEV"
    typ = str(cfg.get("confidentialInstanceType") or "").strip()
    return typ or None


def preflight(details: GcpVmDetails, capture_mode: GcpCaptureMode = "stop") -> list[str]:
    spec = details.spec
    problems: list[str] = []
    if not spec.disks:
        problems.append("instance has no persistent disks")
    inst = details.instance
    for ref in inst.get("disks") or []:
        if str(ref.get("type") or "").upper() == "SCRATCH":
            problems.append("local SSD/scratch disks cannot be exported; remove them or migrate without them")
    for d in spec.disks:
        if d.capacity_bytes > MAX_OCI_VOLUME_BYTES:
            problems.append(f"{d.label} exceeds the 32 TB OCI volume limit")
        doc = details.disk_doc(d.backing_file)
        if doc.get("diskEncryptionKey"):
            problems.append(f"{d.label} uses a customer-managed encryption key; export may fail without "
                            "cloudkms.cryptoKeyVersions.useToDecrypt on the key")
    st = power_state(inst)
    if st in ("starting", "stopping"):
        problems.append(f"instance is {st}; wait until it is running or stopped")
    elif st == "unknown" and capture_mode == "stop":
        problems.append("instance power state is unknown; check it in the console or use snapshot mode")
    return problems


def warnings(details: GcpVmDetails, capture_mode: GcpCaptureMode) -> list[str]:
    out: list[str] = []
    tech = confidential_technology(details.instance)
    if tech:
        out.append(
            f"This instance is a Confidential VM ({tech}): Google encrypts data-in-use in the guest; OCI has no "
            "equivalent. Disk snapshot export is still supported—plan to validate first boot on OCI (many use an "
            "NVMe root disk).",
        )
    if capture_mode == "snapshot":
        out.append("Snapshot mode copies a crash-consistent image while the VM keeps running.")
    if details.spec.guest_full_name and "google" in details.spec.guest_full_name.lower():
        out.append("Google-specific guest agents may need cleanup on first OCI boot (enable GCP cleanup).")
    return out
