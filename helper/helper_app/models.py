"""Pydantic models: source VM description, OCI target, job state and API payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from ipaddress import IPv4Address
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Source VM description (collected from vCenter)
# --------------------------------------------------------------------------- #
class Firmware(str, Enum):
    BIOS = "bios"
    EFI = "efi"


class DiskSpec(BaseModel):
    """One virtual disk of the source VM, in boot order (index 0 = boot disk)."""

    index: int
    label: str
    device_key: int
    capacity_bytes: int
    controller_type: str = Field(
        description="pvscsi | lsilogic | lsilogicsas | buslogic | ide | sata | nvme | unknown"
    )
    controller_class: str = Field(default="", description="vim class name, e.g. ParaVirtualSCSIController")
    controller_bus: int = 0
    unit_number: int = 0
    thin_provisioned: bool = False
    backing_file: str = ""

    @property
    def nfc_key_hint(self) -> str:
        """Suffix of HttpNfcLease.DeviceUrl.key for this disk, e.g. 'VirtualLsiLogicController0:0'."""
        return f"{self.controller_class}{self.controller_bus}:{self.unit_number}"

    @property
    def capacity_gb_rounded(self) -> int:
        gib = 1024**3
        return max(1, -(-self.capacity_bytes // gib))


class NicSpec(BaseModel):
    label: str
    adapter_type: str = Field(description="vmxnet3 | vmxnet2 | e1000 | e1000e | pcnet32 | unknown")
    mac_address: str = ""
    network: str = ""
    ip_addresses: list[str] = Field(default_factory=list,
                                    description="Last addresses VMware Tools reported for this adapter, if known")


class VmSpec(BaseModel):
    moid: str
    name: str
    instance_uuid: str = ""
    num_cpu: int
    memory_mb: int
    guest_id: str
    guest_full_name: str = ""
    firmware: Firmware = Firmware.BIOS
    secure_boot: bool = False
    power_state: str = "poweredOff"
    has_snapshots: bool = False
    host_name: str = Field(default="", description="ESXi host the VM is registered on (vm.runtime.host.name)")
    encrypted: bool = Field(default=False, description="vSphere VM encryption: the VM home is encrypted "
                                                       "(config.keyId); vSphere refuses to export such a VM")
    has_vtpm: bool = Field(default=False, description="A Virtual TPM device is present (it requires VM encryption)")
    encrypted_disks: list[str] = Field(default_factory=list, description="Labels of disks with an encrypted backing")
    disks: list[DiskSpec]
    nics: list[NicSpec] = Field(default_factory=list)

    @property
    def is_windows(self) -> bool:
        return "windows" in self.guest_id.lower() or "windows" in self.guest_full_name.lower()


class VmSummary(BaseModel):
    """One row of the VM list in the web UI."""

    moid: str
    name: str
    folder: str = ""
    power_state: str = "poweredOff"
    guest_full_name: str = ""
    guest_id: str = ""
    num_cpu: int = 0
    memory_mb: int = 0
    num_disks: int = 0
    disk_capacity_bytes: int = 0
    is_template: bool = False
    encrypted: bool = False  # VM encryption (or a vTPM, which requires it): not exportable until decrypted
    vm_size: str = ""  # Azure: the VM size name (Standard_D2s_v3); the list has no vCPU/RAM figures
    location: str = ""  # Azure: region of the VM


class GuestOsMapping(BaseModel):
    """How the guest OS will be recorded on the OCI image, and whether the user has to pick the release."""

    operating_system: str
    operating_system_version: str
    version_detected: bool = Field(description="False when vSphere does not report the release and the "
                                               "version is a default that the user should confirm or change")
    version_choices: list[str] = Field(default_factory=list, description="Releases OCI knows for this OS")


class AzureRevokeExportDisk(BaseModel):
    disk_id: str
    name: str


class VmInspection(BaseModel):
    vm: VmSpec
    can_export: bool
    problems: list[str]
    warnings: list[str]
    os: Optional[GuestOsMapping] = None
    needs_power_off: bool = Field(default=False, description="VM is powered on: the migration shuts it down "
                                                             "before the export and must be confirmed")
    tools_running: bool = Field(default=False, description="VMware Tools is running (graceful shutdown possible)")
    azure_revoke_export_disks: list[AzureRevokeExportDisk] = Field(
        default_factory=list,
        description="Azure managed disks with an export SAS still granted (ActiveSAS); revoke before deallocate export",
    )


class RevokeExportAccessRequest(BaseModel):
    disk_ids: Optional[list[str]] = None
    vm_id: Optional[str] = Field(default=None, description="Revoke export access on every ActiveSAS disk of this VM")

    @model_validator(mode="after")
    def one_target(self) -> "RevokeExportAccessRequest":
        if bool(self.disk_ids) == bool(self.vm_id):
            raise ValueError("provide disk_ids or vm_id, not both")
        return self


class RevokeExportAccessResult(BaseModel):
    disk_id: str
    name: str
    ok: bool
    message: str


class RevokeExportAccessResponse(BaseModel):
    results: list[RevokeExportAccessResult]


# --------------------------------------------------------------------------- #
# OCI target description (chosen by the user in the web UI)
# --------------------------------------------------------------------------- #
class WindowsLicenseType(str, Enum):
    OCI_PROVIDED = "OCI_PROVIDED"
    BRING_YOUR_OWN_LICENSE = "BRING_YOUR_OWN_LICENSE"


class BootVolumeType(str, Enum):
    ISCSI = "ISCSI"
    SCSI = "SCSI"
    IDE = "IDE"
    VFIO = "VFIO"
    PARAVIRTUALIZED = "PARAVIRTUALIZED"


class NetworkType(str, Enum):
    E1000 = "E1000"
    VFIO = "VFIO"
    PARAVIRTUALIZED = "PARAVIRTUALIZED"


class OciTarget(BaseModel):
    compartment_id: str
    availability_domain: str
    subnet_id: str
    display_name: Optional[str] = None
    shape: Optional[str] = Field(default=None, description="Flex shape name; migration tool default when omitted")
    ocpus: Optional[float] = Field(
        default=None, gt=0, le=512,
        description="OCPU override; derived from the source vCPUs (2 vCPU = 1 OCPU) when omitted",
    )
    memory_gb: Optional[float] = Field(
        default=None, gt=0, le=4096, description="Memory override in GB; derived from the source RAM when omitted"
    )
    private_ip: Optional[str] = Field(
        default=None,
        description="Fixed private IPv4 address for the primary VNIC; must lie in the subnet's CIDR and be free "
                    "(checked against OCI when the job is created). Empty: OCI assigns one (DHCP)",
    )
    assign_public_ip: bool = False
    start_after_migration: bool = True
    operating_system_version: Optional[str] = Field(
        default=None, max_length=64,
        description="Release recorded on the OCI image (e.g. Ubuntu '24.04'); required when vSphere does not "
                    "report it (VmInspection.os.version_detected is false), otherwise overrides the detected one",
    )
    windows_license_type: Optional[WindowsLicenseType] = None
    compatibility_mode: bool = Field(
        default=False, description="Force IDE boot volume + E1000 NIC for guests without virtio drivers"
    )
    boot_volume_type_override: Optional[BootVolumeType] = None
    network_type_override: Optional[NetworkType] = None
    nfc_direct_to_esxi: bool = Field(
        default=False,
        description="Download the disks from the ESXi host the VM is registered on instead of through the "
                    "vCenter proxy (same effect as HELPER_NFC_HOST_OVERRIDE, resolved per job)",
    )
    pipelined_decode: bool = Field(
        default=False,
        description="Decode and write the VMDK stream on a separate thread (bounded queue of "
                    "HELPER_NFC_PIPELINE_DEPTH chunks) so the download is not stalled by inflate/pwrite",
    )
    volume_vpus_per_gb: Literal[10, 20, 30] = Field(
        default=10,
        description="Volume performance units per GB for the boot and block volumes created for the VM: "
                    "10 = Balanced, 20 = Higher Performance, 30 = Ultra High Performance",
    )
    rebuild_initramfs: bool = Field(
        default=True,
        description="Linux guests: after the copy, mount the target boot volume on the migration tool VM and rebuild "
                    "the initramfs of every installed kernel with virtio drivers (chroot + dracut) when it lacks "
                    "them, so hostonly initramfs images built on VMware (RHEL/CentOS/Oracle Linux) boot in OCI. "
                    "Skipped for Windows and for guests without dracut; never fails the migration",
    )
    fix_network: bool = Field(
        default=True,
        description="Linux guests: after the copy, make the guest bring up its OCI network interface with DHCP "
                    "although it has a new name (ens3/enp0s5/eth0 instead of ens192): a NetworkManager profile "
                    "for any Ethernet device, a first-boot unit creating ifcfg files for legacy network-scripts, "
                    "netplan/networkd drop-ins; MAC-pinned udev naming rules are disabled. Skipped for Windows; "
                    "never fails the migration",
    )
    azure_cleanup: bool = Field(
        default=True,
        description="Azure Linux guests: after the copy, remove Azure cloud-init and waagent hooks, comment out "
                    "the Azure CD-ROM (sr0) in fstab, enable serial console on ttyS0, and prefer the OCI cloud-init "
                    "datasource so the guest boots cleanly in OCI. Ignored for VMware jobs and Windows; never fails "
                    "the migration",
    )
    gcp_cleanup: bool = Field(
        default=True,
        description="GCP Linux guests: after the copy, remove Google guest-agent / GCE cloud-init hooks and prefer "
                    "the OCI cloud-init datasource so the guest boots cleanly in OCI. Ignored for VMware/Azure "
                    "jobs and Windows; never fails the migration",
    )
    aws_cleanup: bool = Field(
        default=True,
        description="EC2 Linux guests: after the copy, remove Amazon cloud-init / SSM / EC2 instance-connect hooks "
                    "and prefer the OCI cloud-init datasource so the guest boots cleanly in OCI. Ignored for "
                    "non-AWS jobs and Windows; never fails the migration",
    )

    @field_validator("private_ip", mode="before")
    @classmethod
    def _normalise_private_ip(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None  # empty field on the form: DHCP
        try:
            return str(IPv4Address(v))
        except ValueError as exc:
            raise ValueError(f"private_ip must be an IPv4 address such as 10.0.1.25 (got {v!r})") from exc


# --------------------------------------------------------------------------- #
# ISO based instance creation (no vSphere involved)
# --------------------------------------------------------------------------- #
class IsoSpec(BaseModel):
    """The installer ISO in Object Storage and how the instance booting it is built.  Everything about
    *where* the instance goes (compartment, subnet, shape, ...) lives in the shared ``OciTarget``."""

    namespace: str
    bucket: str
    object_name: str
    size_bytes: int = 0
    etag: str = ""  # identifies the exact object version; a re-uploaded ISO gets a new image
    operating_system: str = Field(default="Custom Linux", max_length=64,
                                  description="Recorded on the imported image (OCI catalog name, e.g. Ubuntu)")
    operating_system_version: str = Field(default="unknown", max_length=64)
    firmware: Literal["BIOS", "UEFI_64"] = "UEFI_64"
    secure_boot: bool = False  # UEFI only: launch a shielded instance
    boot_disk_gb: int = Field(default=50, ge=50, le=32768,
                              description="Size of the blank boot volume the OS is installed onto")

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.bucket}/{self.object_name}"

    @property
    def is_windows(self) -> bool:
        return self.operating_system.lower().startswith("windows")

    @field_validator("secure_boot")
    @classmethod
    def _secure_boot_needs_uefi(cls, v, info):
        if v and info.data.get("firmware") == "BIOS":
            raise ValueError("Secure Boot requires UEFI_64 firmware")
        return v


class ExportDiskSource(BaseModel):
    """A volume as attached to the source OCI instance before export."""

    volume_id: str
    is_boot: bool = False
    attachment_id: Optional[str] = None
    device: Optional[str] = None
    attachment_type: Optional[str] = None
    is_read_only: bool = False
    is_shareable: bool = False
    size_gb: int = 0
    display_name: str = ""


class OvaExportSpec(BaseModel):
    """Export an existing OCI instance to an OVF set in Object Storage."""

    instance_id: str
    instance_name: str = ""
    compartment_id: str = ""
    namespace: str = ""
    bucket: str
    prefix: str = ""
    include_data_volumes: bool = True
    was_running: bool = False
    shape: str = ""
    ocpus: Optional[float] = None
    memory_gb: Optional[float] = None
    firmware: Optional[str] = None
    secure_boot: bool = False
    operating_system: str = ""
    operating_system_version: str = ""
    sources: list[ExportDiskSource] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    ovf_object: Optional[str] = None
    manifest_object: Optional[str] = None

    @property
    def is_windows(self) -> bool:
        return self.operating_system.lower().startswith("windows")


class OvaSpec(BaseModel):
    """An OVA (or boot ``.vmdk``) in Object Storage and how the imported instance is built."""

    namespace: str
    bucket: str
    object_name: str
    size_bytes: int = 0
    etag: str = ""
    operating_system: str = Field(default="Custom Linux", max_length=64)
    operating_system_version: str = Field(default="unknown", max_length=64)
    firmware: Literal["BIOS", "UEFI_64"] = "UEFI_64"
    secure_boot: bool = False

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.bucket}/{self.object_name}"

    @property
    def is_windows(self) -> bool:
        return self.operating_system.lower().startswith("windows")

    @field_validator("secure_boot")
    @classmethod
    def _secure_boot_needs_uefi(cls, v, info):
        if v and info.data.get("firmware") == "BIOS":
            raise ValueError("Secure Boot requires UEFI_64 firmware")
        return v


class LaunchOptionsSpec(BaseModel):
    """Resolved OCI LaunchOptions for the target instance."""

    firmware: str = Field(description="BIOS | UEFI_64")
    boot_volume_type: BootVolumeType = BootVolumeType.PARAVIRTUALIZED
    network_type: NetworkType = NetworkType.PARAVIRTUALIZED
    remote_data_volume_type: str = "PARAVIRTUALIZED"
    # informational: comes from the seed image's capability schema (Linux true / Windows false); OCI does not
    # accept it in LaunchOptions ("Overriding ConsistentVolumeNamingEnabled ... is not supported")
    is_consistent_volume_naming_enabled: bool = True
    secure_boot: bool = False  # source had UEFI Secure Boot -> launch as a shielded instance with Secure Boot


# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #
class JobPhase(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    EXPORTING = "EXPORTING"
    FINALIZING = "FINALIZING"
    INSTALLING = "INSTALLING"  # ISO jobs: the instance runs the installer; the user finishes the job
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (JobPhase.COMPLETED, JobPhase.FAILED, JobPhase.CANCELLED)


class DiskStatus(str, Enum):
    PENDING = "PENDING"
    ATTACHED = "ATTACHED"
    COPYING = "COPYING"
    COPIED = "COPIED"
    FAILED = "FAILED"


class DiskState(BaseModel):
    index: int
    label: str = ""
    capacity_bytes: int
    volume_id: Optional[str] = None
    is_boot: bool = False
    size_gb: int = 0
    helper_attachment_id: Optional[str] = None
    device: Optional[str] = None
    target_attachment_id: Optional[str] = None
    status: DiskStatus = DiskStatus.PENDING
    attempts: int = 0
    bytes_received: int = 0
    bytes_written: int = 0
    grains_written: int = 0
    stream_bytes: Optional[int] = None  # size of the exported VMDK stream when the NFC lease reports it
    percent: int = 0  # progress of this disk's stream (exact with stream_bytes, else bounded by capacity)
    throughput_bps: float = 0.0  # received bytes/s over the last minute while copying
    error: Optional[str] = None


class TransferStats(BaseModel):
    """Export-phase figures: what vCenter's *Export OVF template* task shows, plus the totals for the summary."""

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    bytes_received: int = 0  # every byte pulled from vCenter, retried attempts included
    bytes_written: int = 0  # non-zero grain bytes written onto the OCI volumes
    percent: int = 0  # the percentage reported to the NFC lease (= the vCenter task progress)
    throughput_bps: float = 0.0  # over the last minute while exporting; 0 when idle

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def average_bps(self) -> Optional[float]:
        d = self.duration_s
        return self.bytes_received / d if d else None


class GuestFixup(BaseModel):
    """Outcome of one post-copy guest fix-up step (initramfs rebuild, network configuration) on the target
    boot volume."""

    status: Literal["done", "not_needed", "skipped", "failed"]
    detail: str  # what was done, or why not
    kernels: list[str] = Field(default_factory=list, description="Kernel versions whose initramfs was rebuilt "
                                                                 "(initramfs step only)")
    log: list[str] = Field(default_factory=list, description="Step-by-step notes for diagnostics")


JobKind = Literal["vmware", "iso", "ova", "ovaexport", "azure", "gcp", "aws"]

AzureCaptureMode = Literal["deallocate", "snapshot"]
GcpCaptureMode = Literal["stop", "snapshot"]
AwsCaptureMode = Literal["stop", "snapshot"]


class AzureSourceInfo(BaseModel):
    """Where an Azure job's VM lives and how its disks are captured."""

    tenant_id: str
    subscription_id: str
    subscription_name: str = ""
    resource_group: str
    location: str = ""
    vm_size: str = ""
    capture_mode: AzureCaptureMode = Field(
        default="deallocate",
        description="deallocate: stop (deallocate) the VM and export its disks - consistent copy, the VM is down "
                    "during the copy; snapshot: snapshot the disks and export the snapshots while the VM keeps "
                    "running - crash-consistent copy, snapshot storage cost until the job ends",
    )
    disk_ids: list[str] = Field(default_factory=list, description="Managed disk resource IDs, in DiskSpec order")
    snapshot_ids: list[str] = Field(default_factory=list, description="Snapshots created by this job (snapshot mode)")
    sas_granted: list[str] = Field(default_factory=list,
                                   description="Resource IDs (disks or snapshots) with an export SAS still granted")
    sas_expires_at: Optional[datetime] = None


class GcpSourceInfo(BaseModel):
    """Where a GCP job's VM lives and how its disks are exported via GCS."""

    project_id: str
    zone: str
    machine_type: str = ""
    export_bucket: str
    export_prefix: str = ""
    capture_mode: GcpCaptureMode = Field(
        default="stop",
        description="stop: stop the VM and snapshot its disks for export; snapshot: snapshot while the VM keeps "
                    "running (crash-consistent)",
    )
    disk_urls: list[str] = Field(default_factory=list, description="Persistent disk URLs in DiskSpec order")
    snapshot_names: list[str] = Field(default_factory=list, description="Snapshots created by this job")
    gcs_objects: list[str] = Field(default_factory=list,
                                   description="Object names under export_bucket written by this job")


class AwsSourceInfo(BaseModel):
    """Where an AWS job's instance lives and how its EBS volumes are captured."""

    account_id: str
    region: str
    instance_id: str
    instance_type: str = ""
    capture_mode: AwsCaptureMode = Field(
        default="stop",
        description="stop: stop the instance and snapshot its EBS volumes; snapshot: snapshot while the "
                    "instance keeps running (crash-consistent)",
    )
    volume_ids: list[str] = Field(default_factory=list, description="EBS volume IDs in DiskSpec order")
    snapshot_ids: list[str] = Field(default_factory=list, description="Snapshots created by this job")


class Job(BaseModel):
    id: str
    kind: JobKind = "vmware"  # vmware: VM from vSphere; iso/ova: Object Storage; azure: VM from Azure
    phase: JobPhase = JobPhase.QUEUED
    step: str = ""
    step_percent: Optional[int] = None  # progress of the current step when OCI reports one (work requests)
    message: str = ""
    error: Optional[str] = None
    vm: Optional[VmSpec] = None  # the source VM (vmware and azure jobs)
    iso: Optional[IsoSpec] = None  # the installer ISO (iso jobs)
    iso_image_id: Optional[str] = None  # custom image imported from the ISO (iso jobs)
    ova: Optional[OvaSpec] = None  # the OVA object (ova jobs)
    ova_export: Optional[OvaExportSpec] = None  # OCI instance exported to an OVF set (ovaexport jobs)
    ova_image_id: Optional[str] = None  # custom image imported from the OVA boot disk (ova jobs)
    ova_staging_prefix: str = ""  # extracted VMDKs under this object prefix in the same bucket
    azure: Optional[AzureSourceInfo] = None  # subscription / resource group / capture mode (azure jobs)
    gcp: Optional[GcpSourceInfo] = None  # project / zone / GCS export (gcp jobs)
    aws: Optional[AwsSourceInfo] = None  # account / region / capture mode (aws jobs)
    vcenter_host: str = ""  # vCenter the VM was inspected on ("host" or "host:port"); tagged onto the instance
    target: OciTarget
    power_off_source: bool = False  # VM was powered on when the job was created; shut it down before the export
    # already_off | guest_shutdown | powered_off (hard) | deallocated (Azure) | snapshotted (Azure snapshot mode)
    power_off_result: Optional[str] = None
    launch_options: Optional[LaunchOptionsSpec] = None
    seed_image_id: Optional[str] = None
    instance_id: Optional[str] = None
    instance_display_name: Optional[str] = None
    boot_volume_id: Optional[str] = None
    nfc_host: Optional[str] = None  # host the disk streams were downloaded from (vCenter or ESXi)
    guest_fixup: Optional[GuestFixup] = None  # post-copy initramfs rebuild on the target boot volume
    network_fixup: Optional[GuestFixup] = None  # post-copy network configuration (DHCP on the renamed NIC)
    azure_fixup: Optional[GuestFixup] = None  # Azure source: cloud-init / waagent / serial console (Linux only)
    gcp_fixup: Optional[GuestFixup] = None  # GCP source: GCE guest agent / cloud-init (Linux only)
    aws_fixup: Optional[GuestFixup] = None  # AWS source: cloud-init / SSM / instance-connect (Linux only)
    disks: list[DiskState] = Field(default_factory=list)
    transfer: TransferStats = Field(default_factory=TransferStats)
    created_by: str = ""
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None  # set when the job reaches a terminal phase

    @property
    def total_bytes(self) -> int:
        return sum(d.capacity_bytes for d in self.disks)

    @property
    def source_key(self) -> str:
        """What the job was created from, as stored in the job table's source column (the VM's moid, or the
        ISO object)."""
        if self.vm is not None:
            return self.vm.moid
        if self.iso is not None:
            return f"iso:{self.iso.key}"
        if self.ova is not None:
            return f"ova:{self.ova.key}"
        if self.ova_export is not None:
            return f"ovaexport:{self.ova_export.instance_id}"
        return ""

    @property
    def source_name(self) -> str:
        if self.vm is not None:
            return self.vm.name
        if self.iso is not None:
            return self.iso.object_name
        if self.ova is not None:
            return self.ova.object_name
        if self.ova_export is not None:
            return self.ova_export.instance_name or self.ova_export.instance_id
        return ""

    @property
    def is_windows(self) -> bool:
        if self.vm is not None:
            return self.vm.is_windows
        if self.iso is not None:
            return self.iso.is_windows
        if self.ova is not None:
            return self.ova.is_windows
        return bool(self.ova_export and self.ova_export.is_windows)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> "JobSummary":
        """Duration / volume / bandwidth figures for the job view (derived, not stored)."""
        end = self.finished_at or (datetime.now(timezone.utc) if not self.phase.terminal else self.updated_at)
        return JobSummary(
            duration_s=max(0.0, (end - self.created_at).total_seconds()),
            transfer_duration_s=self.transfer.duration_s,
            bytes_received=self.transfer.bytes_received,
            bytes_written=self.transfer.bytes_written,
            average_bps=self.transfer.average_bps,
        )


class JobSummary(BaseModel):
    duration_s: float
    transfer_duration_s: Optional[float] = None
    bytes_received: int = 0
    bytes_written: int = 0
    average_bps: Optional[float] = None


class CreateJobRequest(BaseModel):
    vm_moid: str
    target: OciTarget
    power_off_source: bool = Field(
        default=False,
        description="Required for a powered-on VM: the user confirmed that the migration tool shuts it down right "
                    "before the disk export (guest shutdown via VMware Tools, hard power-off as fallback)",
    )


class CreateIsoJobRequest(BaseModel):
    """Create an instance that boots an installer ISO from Object Storage (no vSphere involved)."""

    iso: IsoSpec
    target: OciTarget


class CreateOvaJobRequest(BaseModel):
    """Import an OVA from Object Storage and launch an instance from it (no vSphere involved)."""

    ova: OvaSpec
    target: OciTarget


class CreateOvaExportJobRequest(BaseModel):
    """Export an existing OCI instance to an OVF set in Object Storage."""

    instance_id: str
    bucket: str
    namespace: Optional[str] = None
    prefix: Optional[str] = None
    include_data_volumes: bool = True


class ObjectUploadRequest(BaseModel):
    bucket: str
    object_name: str
    content_type: str = "application/octet-stream"


class ObjectUploadResponse(BaseModel):
    namespace: str
    bucket: str
    object_name: str
    upload_url: str
    expires_at: str


class CreateGcpJobRequest(BaseModel):
    """Migrate a GCP VM (needs a GCP login on the session)."""

    vm_id: str = Field(description="Compute Engine instance id (projects/.../zones/.../instances/...)")
    target: OciTarget
    capture_mode: GcpCaptureMode = "stop"
    power_off_source: bool = Field(
        default=False,
        description="Stop mode with a running VM: the user confirmed that the migration tool stops the instance "
                    "right before the disk export",
    )


class CreateAzureJobRequest(BaseModel):
    """Migrate an Azure VM (needs an Azure login on the session)."""

    vm_id: str = Field(description="Azure resource ID of the virtual machine")
    target: OciTarget
    capture_mode: AzureCaptureMode = "deallocate"
    power_off_source: bool = Field(
        default=False,
        description="Deallocate mode with a running VM: the user confirmed that the migration tool deallocates "
                    "(stops) the VM right before the disk export",
    )


class CreateAwsJobRequest(BaseModel):
    """Migrate an EC2 instance (needs an AWS login on the session)."""

    vm_id: str = Field(description="EC2 instance ARN (arn:aws:ec2:region:account:instance/i-...)")
    target: OciTarget
    capture_mode: AwsCaptureMode = "stop"
    power_off_source: bool = Field(
        default=False,
        description="Stop mode with a running instance: the user confirmed that the migration tool stops "
                    "the instance right before the disk export",
    )


class InstanceStatus(BaseModel):
    """Live state of the job's target instance as OCI reports it (GET /api/jobs/{id}/instance)."""

    instance_id: str
    display_name: Optional[str] = None
    lifecycle_state: str  # OCI lifecycle state, or NOT_FOUND when OCI no longer knows the OCID
    private_ip: Optional[str] = None  # primary VNIC, as assigned by OCI (fixed or DHCP)
    public_ip: Optional[str] = None
    checked_at: datetime


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str
    vcenter_host: str = ""  # defaults to HELPER_VCENTER_HOST; may be "host" or "host:port"
    vcenter_port: Optional[int] = Field(default=None, ge=1, le=65535)
    verify_ssl: Optional[bool] = Field(
        default=None, description="Verify the vCenter/ESXi TLS certificate (API and NFC download); "
                                  "None = HELPER_VCENTER_VERIFY_SSL")


class AzureLoginRequest(BaseModel):
    tenant_id: str = Field(description="Entra ID tenant: GUID or verified domain (contoso.onmicrosoft.com)")
    client_id: str = Field(description="Application (client) ID of the service principal")
    client_secret: str


class AzureSubscription(BaseModel):
    id: str
    name: str = ""
    state: str = ""


class GcpProject(BaseModel):
    id: str
    name: str = ""


class GcpLoginRequest(BaseModel):
    service_account_json: str = Field(description="JSON key of a Google Cloud service account")
    export_bucket: str = Field(description="GCS bucket for temporary snapshot exports (objects deleted after the job)")


class AwsLoginRequest(BaseModel):
    access_key_id: str = Field(description="IAM user access key ID (AKIA...)")
    secret_access_key: str
    region: str = Field(description="EC2 region to list and export, e.g. eu-west-1")


class SessionInfo(BaseModel):
    username: str
    anonymous: bool = False  # ISO flow: a UI session without a vCenter or Azure login
    vcenter_host: str = ""
    vcenter_port: int = 443
    vcenter_version: str = ""
    verify_ssl: bool = False
    azure_tenant_id: str = ""  # set when the session is an Azure (service principal) login
    azure_client_id: str = ""
    azure_subscriptions: list[AzureSubscription] = Field(default_factory=list)
    gcp_client_email: str = ""
    gcp_project_id: str = ""
    gcp_export_bucket: str = ""
    gcp_projects: list[GcpProject] = Field(default_factory=list)
    aws_access_key_id: str = ""
    aws_region: str = ""
    aws_account_id: str = ""
    created_at: datetime
    expires_at: datetime


# --------------------------------------------------------------------------- #
# OCI inventory for the UI dropdowns
# --------------------------------------------------------------------------- #
class OciCompartment(BaseModel):
    id: str
    name: str
    path: str = ""


class OciVcn(BaseModel):
    id: str
    name: str
    cidr_blocks: list[str] = Field(default_factory=list)


class OciSubnet(BaseModel):
    id: str
    name: str
    vcn_id: str
    vcn_name: str = ""
    cidr_block: str = ""
    availability_domain: Optional[str] = None
    prohibit_public_ip: bool = False


class OciShape(BaseModel):
    name: str
    kind: Literal["VM", "BM"] = "VM"  # virtual machine (flexible sizing) or bare metal (fixed cores and memory)
    arch: Literal["x86_64", "aarch64"] = "x86_64"  # Ampere (A1/A2/A4) shapes are aarch64: ISO form only
    is_flex: bool
    min_ocpus: Optional[float] = None
    max_ocpus: Optional[float] = None
    min_memory_gb: Optional[float] = None
    max_memory_gb: Optional[float] = None


class PrivateIpCheck(BaseModel):
    """Result of GET /api/oci/private-ip-check: can this fixed address be used in the subnet right now?"""

    ip: str
    subnet_id: str
    available: bool
    message: str  # user facing explanation (why not, or confirmation)


class OciBucket(BaseModel):
    name: str
    namespace: str
    compartment_id: str
    time_created: Optional[datetime] = None


class OciInstance(BaseModel):
    """A compute instance as listed for the OCI Remote Console page (compartment listing or name search)."""

    id: str
    name: str
    compartment_id: str
    compartment_path: str = ""
    lifecycle_state: str
    shape: Optional[str] = None  # Resource Search summaries carry no shape
    availability_domain: Optional[str] = None
    time_created: Optional[datetime] = None
    job_id: Optional[str] = None  # the migration tool job that created the instance, if any


class OciObject(BaseModel):
    """An object in a bucket (the ISO picker lists ``.iso`` objects)."""

    name: str
    size_bytes: int = 0
    etag: str = ""
    time_modified: Optional[datetime] = None


class OvaInspectResponse(BaseModel):
    """OVF metadata from an ``.ova`` object for pre-filling the import form."""

    object_name: str
    operating_system: str
    operating_system_version: str
    version_detected: bool = False
    family: Literal["linux", "windows"]
    firmware: Literal["BIOS", "UEFI_64"]
    secure_boot: bool = False
    num_vcpu: Optional[int] = None
    memory_mb: Optional[int] = None
    suggested_ocpus: Optional[int] = None
    suggested_memory_gb: Optional[int] = None
    boot_disk_gb: Optional[int] = None
    disk_count: int = 0
    product: Optional[str] = None
    os_description: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class OsCatalogEntry(BaseModel):
    """One operating system OCI knows for custom image metadata, with its selectable releases."""

    operating_system: str
    family: Literal["linux", "windows"]
    versions: list[str]


class OciOptions(BaseModel):
    region: str
    helper_instance_id: str
    helper_compartment_id: str
    helper_availability_domain: str
    default_shape: str
    compartments: list[OciCompartment]
    availability_domains: list[str]
    vcns: list[OciVcn]
    subnets: list[OciSubnet]
    shapes: list[OciShape]
