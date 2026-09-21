"""Provisioning state machine for one migration job.

prepare():   seed image -> launch target -> create data volumes -> attach them to the (running) target as
             read/write shareable -> stop -> detach boot volume -> attach everything to the helper
             (paravirtualized; data volumes as the second shareable attachment); the disks are then ATTACHED
finalize():  detach from helper -> attach boot volume to target -> (start) -> COMPLETED
cleanup():   best-effort teardown after a failure or cancellation

OCI only attaches data volumes to a RUNNING instance, and the target must be STOPPED while its boot volume is
swapped.  Attaching the data volumes while the target still runs from the seed image (and keeping those
attachments) means the guest sees all its disks on its very first boot instead of having them hot-plugged
afterwards.  Emulated attachments (Maximum compatibility) cannot be shareable; those are attached after the
start in finalize().
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from helper_app.branding import (
    PREFIX,
    TAG_DISK_INDEX,
    TAG_JOB,
    TAG_SOURCE_AWS,
    TAG_SOURCE_AZURE,
    TAG_SOURCE_ESXI_HOST,
    TAG_SOURCE_GCP,
    TAG_SOURCE_MOID,
    TAG_SOURCE_VCENTER,
    TAG_SOURCE_VM,
    TAG_SOURCE_VM_DETAILS,
)
from helper_app.config import Settings
from helper_app.disk.devices import DeviceScanner, rescan_scsi_hosts, scan_block_devices, wait_for_new_device
from helper_app.models import (
    DiskState,
    DiskStatus,
    ExportDiskSource,
    Job,
    JobPhase,
    LaunchOptionsSpec,
    OvaExportSpec,
    WindowsLicenseType,
)
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.image_import import ensure_shape_compatible
from helper_app.oci.launch import free_hostname_label, secure_boot_platform_config
from helper_app.oci.mapping import (
    OsMetadata,
    ShapeConfig,
    is_bare_metal_shape,
    map_guest_os,
    map_launch_options,
    map_shape,
    oci_firmware,
    volume_size_gb,
    with_os_version,
)
from helper_app.oci.seed_image import SeedImageService

log = logging.getLogger(__name__)


class Provisioner:
    def __init__(
        self,
        clients: OciClients,
        settings: Settings,
        save: Callable[[Job], Job],
        seed_service: SeedImageService | None = None,
        scan_devices: DeviceScanner = scan_block_devices,
    ):
        self.c = clients
        self.s = settings
        self.save = save
        self.seeds = seed_service or SeedImageService(clients, settings)
        self.scan_devices = scan_devices
        # boot volumes are attached without a device path and identified by "which disk appeared", so only
        # one such attachment may be in flight at a time even with concurrent jobs
        self._attach_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _step(self, job: Job, step: str, message: str = "", check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()  # give a pending cancellation a chance before the next long OCI operation
        job.step = step
        job.step_percent = None
        job.message = message or step
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)

    def _step_progress(self, job: Job, percent: int, message: str,
                       check: Callable[[], None] | None = None) -> None:
        """Progress inside the current step (e.g. ``percentComplete`` of an OCI work request)."""
        if check is not None:
            check()  # a long import is a good place to notice a cancellation
        job.step_percent = max(0, min(100, int(percent)))
        job.message = message
        log.info("job %s: %s %s%% %s", job.id, job.step, job.step_percent, message)
        self.save(job)

    @property
    def helper_id(self) -> str:
        return self.c.identity_info.instance_id

    # ------------------------------------------------------------------ prepare
    def prepare(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        """``check_cancel`` is called before every step and may raise to abort provisioning."""
        import oci.core.models as M

        def step(name: str, message: str = "") -> None:
            self._step(job, name, message, check_cancel)

        vm, target = job.vm, job.target
        if not vm.disks:
            raise OciError("source VM has no virtual disks")
        if target.availability_domain != self.c.identity_info.availability_domain:
            raise OciError(
                f"target availability domain {target.availability_domain} differs from the migration tool VM's "
                f"{self.c.identity_info.availability_domain}; boot volumes can only be attached within one AD"
            )

        job.phase = JobPhase.PROVISIONING
        os_meta = with_os_version(map_guest_os(vm.guest_id, vm.guest_full_name), target.operating_system_version)
        firmware = oci_firmware(vm.firmware)
        launch_options = map_launch_options(vm, target)
        shape = map_shape(vm, target, self.s.default_shape, self.s.max_memory_gb_per_ocpu)
        job.launch_options = launch_options
        # Secure Boot on the source -> shielded instance; decided up front so an unsuitable shape fails
        # before any OCI resource exists
        platform_config = (secure_boot_platform_config(shape.shape, vm.is_windows or os_meta.is_windows)
                           if launch_options.secure_boot else None)
        if not job.disks:
            job.disks = [
                DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
                for d in sorted(vm.disks, key=lambda d: d.index)
            ]
        # the API pre-creates the disk records without a size (so the job view can list them right away)
        for disk in job.disks:
            if disk.size_gb <= 0:
                disk.size_gb = volume_size_gb(disk.capacity_bytes, self.s.min_volume_gb)

        # 1. seed image
        if not job.seed_image_id:
            step("seed_image", f"Resolving seed image for {firmware} / {os_meta.operating_system} "
                                           f"{os_meta.operating_system_version}")
            job.seed_image_id = self.seeds.get_or_create(
                os_meta, firmware, launch_options,
                on_progress=lambda pct, text: self._step_progress(job, pct, text, check_cancel))
            job.step_percent = None
            self.save(job)

        # 2. launch the target instance
        if not job.instance_id:
            display = target.display_name or vm.name
            shielded = ""
            if platform_config is not None:
                shielded = ", shielded: Secure Boot"
                if platform_config.is_measured_boot_enabled:
                    shielded += " + Measured Boot + TPM"
            # the seed image must list the shape as compatible (imported images start with a default list)
            step("launch_instance", f"Checking that seed image allows shape {shape.shape}")
            ensure_shape_compatible(self.c, job.seed_image_id, shape.shape)
            step("launch_instance", f"Launching {display} ({shape.shape}, {shape.ocpus:g} OCPU, "
                                               f"{shape.memory_gb:g} GB, firmware {firmware}{shielded})")
            details = M.LaunchInstanceDetails(
                availability_domain=target.availability_domain,
                compartment_id=target.compartment_id,
                display_name=display,
                shape=shape.shape,
                shape_config=M.LaunchInstanceShapeConfigDetails(ocpus=shape.ocpus, memory_in_gbs=shape.memory_gb),
                create_vnic_details=M.CreateVnicDetails(
                    subnet_id=target.subnet_id,
                    assign_public_ip=target.assign_public_ip,
                    private_ip=target.private_ip or None,  # None: OCI picks a free address (DHCP)
                    display_name=display,
                    hostname_label=free_hostname_label(self.c, target.subnet_id, display),
                ),
                source_details=M.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=job.seed_image_id,
                    boot_volume_size_in_gbs=job.disks[0].size_gb,
                    boot_volume_vpus_per_gb=target.volume_vpus_per_gb,
                ),
                # isConsistentVolumeNamingEnabled is deliberately absent: OCI rejects any value that differs
                # from the image's Storage.ConsistentVolumeNaming ("Overriding ... is not supported"), so the
                # seed image's schema carries it (true for Linux, false for Windows)
                launch_options=M.LaunchOptions(
                    firmware=launch_options.firmware,
                    boot_volume_type=launch_options.boot_volume_type.value,
                    network_type=launch_options.network_type.value,
                    remote_data_volume_type=launch_options.remote_data_volume_type,
                ),
                freeform_tags=source_tags(job),
                metadata={},
            )
            if platform_config is not None:
                details.platform_config = platform_config
            if vm.is_windows or os_meta.is_windows:
                lic = target.windows_license_type or WindowsLicenseType.BRING_YOUR_OWN_LICENSE
                details.licensing_configs = [
                    M.LaunchInstanceWindowsLicensingConfig(type="WINDOWS", license_type=lic.value)
                ]
            instance = self.c.compute.launch_instance(details).data
            job.instance_id = instance.id
            job.instance_display_name = instance.display_name
            self.save(job)
            try:
                self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                                ["RUNNING"], self.s.launch_timeout_s, what="target instance")
            except OciError as exc:
                # a launch that fails asynchronously (VNIC, capacity, ...) only explains itself on its work request
                reason = self.c.work_request_errors(target.compartment_id, job.instance_id)
                raise OciError(f"{exc}; {reason}" if reason else str(exc)) from exc

        # 3. create data volumes
        for disk in job.disks[1:]:
            if disk.volume_id:
                continue
            step("create_volume", f"Creating {disk.size_gb} GB block volume for disk {disk.index} "
                                  f"({target.volume_vpus_per_gb} VPU/GB)")
            vol = self.c.blockstorage.create_volume(
                M.CreateVolumeDetails(
                    availability_domain=target.availability_domain,
                    compartment_id=target.compartment_id,
                    display_name=f"{job.instance_display_name or vm.name}-disk{disk.index}",
                    size_in_gbs=disk.size_gb,
                    vpus_per_gb=target.volume_vpus_per_gb,
                    freeform_tags={TAG_JOB: job.id, TAG_DISK_INDEX: str(disk.index)},
                )
            ).data
            disk.volume_id = vol.id
            self.save(job)
            self.c.wait_for(lambda vid=vol.id: self.c.blockstorage.get_volume(vid), "lifecycle_state",
                            ["AVAILABLE"], self.s.volume_timeout_s, what=f"volume for disk {disk.index}")

        # 4. attach the data volumes to the target while it is still running (OCI refuses data volume
        #    attachments on a stopped instance) as read/write shareable, so the helper can take a second
        #    attachment for the copy and the guest finds every disk in place on its first boot
        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state == "RUNNING" and self._shareable_target_attachments(job):
            for n, disk in enumerate(job.disks[1:], start=1):
                if disk.target_attachment_id:
                    continue
                step("attach_data_volume", f"Attaching disk {disk.index} to target (shareable, before its first boot)")
                self._attach_to_target(job, disk, n, shareable=True)

        # 5. stop it (hard stop: the placeholder image has no OS to react to ACPI)
        if inst.lifecycle_state != "STOPPED":
            step("stop_instance", "Stopping target instance")
            if inst.lifecycle_state in ("RUNNING", "STARTING", "PROVISIONING"):
                self.c.compute.instance_action(job.instance_id, "STOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="target instance")

        # 6. detach boot volume from the target
        if not job.boot_volume_id:
            step("detach_boot_volume", "Detaching boot volume from target")
            atts = self.c.compute.list_boot_volume_attachments(
                target.availability_domain, target.compartment_id, instance_id=job.instance_id
            ).data
            atts = [a for a in atts if a.lifecycle_state in ("ATTACHED", "ATTACHING")]
            if not atts:
                raise OciError("target instance has no attached boot volume")
            att = atts[0]
            job.boot_volume_id = att.boot_volume_id
            job.disks[0].volume_id = att.boot_volume_id
            self.save(job)
            self.c.compute.detach_boot_volume(att.id)
            self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(att.id), "lifecycle_state",
                            ["DETACHED"], self.s.volume_timeout_s, what="boot volume attachment")

        # 7. attach everything to the helper
        for disk in job.disks:
            if disk.helper_attachment_id and disk.device:
                continue
            if disk.is_boot:
                # OCI refuses a device path for a boot volume attached as a data volume
                # ("the specified device attribute ... is invalid"); find the disk by its appearance instead
                step("attach_to_helper", f"Attaching disk {disk.index} (boot volume) to the migration tool VM")
                self._attach_boot_volume_to_helper(job, disk, check_cancel)
            else:
                device = self._pick_free_device(job)
                step("attach_to_helper", f"Attaching disk {disk.index} volume to the migration tool VM as {device}")
                # a volume that already hangs off the target must be shared on every attachment
                att = self._attach_to_helper(job, disk, device, shareable=bool(disk.target_attachment_id))
                disk.device = att.device or device
            disk.status = DiskStatus.ATTACHED
            self.save(job)

        step("ready", "Volumes attached to the migration tool VM; ready to receive disk streams")
        return job

    def prepare_placeholder(
        self,
        job: Job,
        *,
        os_meta: OsMetadata,
        firmware: str,
        shape: ShapeConfig,
        launch_options: LaunchOptionsSpec,
        instance_tags: dict[str, str],
        display_name: str,
        is_windows: bool,
        windows_license: WindowsLicenseType | None,
        check_cancel: Callable[[], None] | None = None,
    ) -> Job:
        """Like ``prepare`` for OVA import: seed image, placeholder instance, data volumes, helper attachments."""
        import oci.core.models as M

        def step(name: str, message: str = "") -> None:
            self._step(job, name, message, check_cancel)

        target = job.target
        if not job.disks:
            raise OciError("job has no disks to import")
        if target.availability_domain != self.c.identity_info.availability_domain:
            raise OciError(
                f"target availability domain {target.availability_domain} differs from the migration tool VM's "
                f"{self.c.identity_info.availability_domain}; boot volumes can only be attached within one AD"
            )

        job.phase = JobPhase.PROVISIONING
        job.launch_options = launch_options
        platform_config = (
            secure_boot_platform_config(shape.shape, is_windows, what="Secure Boot was requested")
            if launch_options.secure_boot
            else None
        )
        for disk in job.disks:
            if disk.size_gb <= 0:
                disk.size_gb = volume_size_gb(disk.capacity_bytes, self.s.min_volume_gb)

        if not job.seed_image_id:
            step(
                "seed_image",
                f"Resolving seed image for {firmware} / {os_meta.operating_system} {os_meta.operating_system_version}",
            )
            job.seed_image_id = self.seeds.get_or_create(
                os_meta,
                firmware,
                launch_options,
                on_progress=lambda pct, text: self._step_progress(job, pct, text, check_cancel),
            )
            job.step_percent = None
            self.save(job)

        bare_metal = is_bare_metal_shape(shape.shape)
        if not job.instance_id:
            display = display_name
            shielded = ""
            if platform_config is not None:
                shielded = ", shielded: Secure Boot"
                if platform_config.is_measured_boot_enabled:
                    shielded += " + Measured Boot + TPM"
            step("launch_instance", f"Checking that seed image allows shape {shape.shape}")
            ensure_shape_compatible(self.c, job.seed_image_id, shape.shape)
            step(
                "launch_instance",
                f"Launching placeholder {display} ({shape.shape}, {shape.ocpus:g} OCPU, "
                f"{shape.memory_gb:g} GB, firmware {firmware}{shielded})",
            )
            details = M.LaunchInstanceDetails(
                availability_domain=target.availability_domain,
                compartment_id=target.compartment_id,
                display_name=display,
                shape=shape.shape,
                shape_config=None
                if bare_metal
                else M.LaunchInstanceShapeConfigDetails(ocpus=shape.ocpus, memory_in_gbs=shape.memory_gb),
                create_vnic_details=M.CreateVnicDetails(
                    subnet_id=target.subnet_id,
                    assign_public_ip=target.assign_public_ip,
                    private_ip=target.private_ip or None,
                    display_name=display,
                    hostname_label=free_hostname_label(self.c, target.subnet_id, display),
                ),
                source_details=M.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=job.seed_image_id,
                    boot_volume_size_in_gbs=job.disks[0].size_gb,
                    boot_volume_vpus_per_gb=target.volume_vpus_per_gb,
                ),
                launch_options=M.LaunchOptions(
                    firmware=launch_options.firmware,
                    boot_volume_type=launch_options.boot_volume_type.value,
                    network_type=launch_options.network_type.value,
                    remote_data_volume_type=launch_options.remote_data_volume_type,
                ),
                freeform_tags=instance_tags,
                metadata={},
            )
            if platform_config is not None:
                details.platform_config = platform_config
            if is_windows:
                lic = windows_license or WindowsLicenseType.BRING_YOUR_OWN_LICENSE
                details.licensing_configs = [
                    M.LaunchInstanceWindowsLicensingConfig(type="WINDOWS", license_type=lic.value)
                ]
            instance = self.c.compute.launch_instance(details).data
            job.instance_id = instance.id
            job.instance_display_name = instance.display_name
            self.save(job)
            try:
                self.c.wait_for(
                    lambda: self.c.compute.get_instance(job.instance_id),
                    "lifecycle_state",
                    ["RUNNING"],
                    self.s.launch_timeout_s,
                    what="placeholder instance",
                )
            except OciError as exc:
                reason = self.c.work_request_errors(target.compartment_id, job.instance_id)
                raise OciError(f"{exc}; {reason}" if reason else str(exc)) from exc

        for disk in job.disks[1:]:
            if disk.volume_id:
                continue
            step(
                "create_volume",
                f"Creating {disk.size_gb} GB block volume for disk {disk.index} ({target.volume_vpus_per_gb} VPU/GB)",
            )
            vol = self.c.blockstorage.create_volume(
                M.CreateVolumeDetails(
                    availability_domain=target.availability_domain,
                    compartment_id=target.compartment_id,
                    display_name=f"{job.instance_display_name or display_name}-disk{disk.index}",
                    size_in_gbs=disk.size_gb,
                    vpus_per_gb=target.volume_vpus_per_gb,
                    freeform_tags={TAG_JOB: job.id, TAG_DISK_INDEX: str(disk.index)},
                )
            ).data
            disk.volume_id = vol.id
            self.save(job)
            self.c.wait_for(
                lambda vid=vol.id: self.c.blockstorage.get_volume(vid),
                "lifecycle_state",
                ["AVAILABLE"],
                self.s.volume_timeout_s,
                what=f"volume for disk {disk.index}",
            )

        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state == "RUNNING" and self._shareable_target_attachments(job):
            for n, disk in enumerate(job.disks[1:], start=1):
                if disk.target_attachment_id:
                    continue
                step("attach_data_volume", f"Attaching disk {disk.index} to placeholder (shareable, before import)")
                self._attach_to_target(job, disk, n, shareable=True)

        if inst.lifecycle_state != "STOPPED":
            step("stop_instance", "Stopping placeholder instance")
            if inst.lifecycle_state in ("RUNNING", "STARTING", "PROVISIONING"):
                self.c.compute.instance_action(job.instance_id, "STOP")
            self.c.wait_for(
                lambda: self.c.compute.get_instance(job.instance_id),
                "lifecycle_state",
                ["STOPPED"],
                self.s.launch_timeout_s,
                what="placeholder instance",
            )

        if not job.boot_volume_id:
            step("detach_boot_volume", "Detaching boot volume from placeholder")
            atts = self.c.compute.list_boot_volume_attachments(
                target.availability_domain, target.compartment_id, instance_id=job.instance_id
            ).data
            atts = [a for a in atts if a.lifecycle_state in ("ATTACHED", "ATTACHING")]
            if not atts:
                raise OciError("placeholder instance has no attached boot volume")
            att = atts[0]
            job.boot_volume_id = att.boot_volume_id
            job.disks[0].volume_id = att.boot_volume_id
            self.save(job)
            self.c.compute.detach_boot_volume(att.id)
            self.c.wait_for(
                lambda: self.c.compute.get_boot_volume_attachment(att.id),
                "lifecycle_state",
                ["DETACHED"],
                self.s.volume_timeout_s,
                what="boot volume attachment",
            )

        for disk in job.disks:
            if disk.helper_attachment_id and disk.device:
                continue
            if disk.is_boot:
                step("attach_to_helper", f"Attaching disk {disk.index} (boot volume) to the migration tool VM")
                self._attach_boot_volume_to_helper(job, disk, check_cancel)
            else:
                device = self._pick_free_device(job)
                step("attach_to_helper", f"Attaching disk {disk.index} volume to the migration tool VM as {device}")
                att = self._attach_to_helper(job, disk, device, shareable=bool(disk.target_attachment_id))
                disk.device = att.device or device
            disk.status = DiskStatus.ATTACHED
            self.save(job)

        step("ready", "Volumes attached to the migration tool VM; ready to receive OVA disk streams")
        return job

    @staticmethod
    def _remote_data_volume_type(job: Job) -> str:
        """Device class announced in ``launchOptions.remoteDataVolumeType``; every data volume attachment
        must use it."""
        return job.launch_options.remote_data_volume_type if job.launch_options else "PARAVIRTUALIZED"

    def _shareable_target_attachments(self, job: Job) -> bool:
        """Multi-attach (read/write shareable) exists for paravirtualized and iSCSI attachments only, not for
        emulated (SCSI/IDE) ones."""
        return self._remote_data_volume_type(job) not in ("SCSI", "IDE")

    def _target_device(self, job: Job, n: int) -> str | None:
        """Consistent device path of the ``n``-th data disk on the target (Linux only: OCI rejects the
        attribute for Windows instances, "device attribute ... is not supported ... for Windows")."""
        return None if _is_windows_job(job) else f"{self.s.device_prefix}{chr(ord('a') + n)}"

    def _attach_to_target(self, job: Job, disk: DiskState, n: int, shareable: bool) -> Any:
        import oci.core.models as M

        remote_type = self._remote_data_volume_type(job)
        src_name = job.vm.name if job.vm is not None else (job.source_name or "instance")
        common = dict(instance_id=job.instance_id, volume_id=disk.volume_id, device=self._target_device(job, n),
                      display_name=f"{job.instance_display_name or src_name}-disk{disk.index}")
        if remote_type == "ISCSI":
            details = M.AttachIScsiVolumeDetails(type="iscsi", **common)
        elif remote_type in ("SCSI", "IDE"):
            details = M.AttachEmulatedVolumeDetails(type="emulated", **common)
        else:
            details = M.AttachParavirtualizedVolumeDetails(type="paravirtualized", **common)
        if shareable:
            details.is_shareable = True
        att = self.c.compute.attach_volume(details).data
        disk.target_attachment_id = att.id
        self.save(job)
        return self.c.wait_for(lambda: self.c.compute.get_volume_attachment(att.id), "lifecycle_state",
                               ["ATTACHED"], self.s.volume_timeout_s, what=f"target attachment disk {disk.index}")

    def _attach_to_helper(self, job: Job, disk: DiskState, device: str | None, shareable: bool = False) -> Any:
        import oci.core.models as M

        details = M.AttachParavirtualizedVolumeDetails(
            type="paravirtualized",
            instance_id=self.helper_id,
            volume_id=disk.volume_id,
            device=device,
            display_name=f"{PREFIX}-{job.id[:8]}-disk{disk.index}",
        )
        if shareable:
            details.is_shareable = True
        att = self.c.compute.attach_volume(details).data
        disk.helper_attachment_id = att.id
        self.save(job)
        return self.c.wait_for(lambda: self.c.compute.get_volume_attachment(att.id), "lifecycle_state",
                               ["ATTACHED"], self.s.volume_timeout_s,
                               what=f"migration tool VM attachment disk {disk.index}")

    def _boot_volume_expected_bytes(self, disk: DiskState) -> int:
        expected = disk.capacity_bytes or disk.size_gb * 1024**3
        if not disk.volume_id:
            return expected
        try:
            if disk.is_boot or disk.volume_id.startswith("ocid1.bootvolume."):
                vol = self.c.blockstorage.get_boot_volume(disk.volume_id).data
            else:
                vol = self.c.blockstorage.get_volume(disk.volume_id).data
        except Exception as exc:  # noqa: BLE001
            log.warning("job could not re-read volume %s for size: %s", disk.volume_id, exc)
            return expected
        mbs = getattr(vol, "size_in_mbs", None)
        if mbs:
            return int(mbs) * 1024 * 1024
        gbs = getattr(vol, "size_in_gbs", None)
        if gbs:
            return int(gbs) * 1024**3
        return expected

    def _attach_boot_volume_to_helper(
        self, job: Job, disk: DiskState, check_cancel: Callable[[], None] | None = None,
    ) -> None:
        expected = self._boot_volume_expected_bytes(disk)
        with self._attach_lock:
            before = self.scan_devices()
            log.info(
                "job %s: attaching boot volume %s; expecting a new disk of %s bytes "
                "(helper already has %s)",
                job.id, disk.volume_id, expected, sorted(before) or "no disks",
            )
            self._attach_to_helper(job, disk, None)
            rescan_scsi_hosts()
            try:
                disk.device = wait_for_new_device(
                    before, expected, self.s.volume_timeout_s, self.scan_devices,
                    on_progress=lambda new: self._note_boot_device_wait(job, expected, new),
                    check_cancel=check_cancel,
                )
            except RuntimeError as exc:
                if type(exc) is not RuntimeError:
                    raise
                raise OciError("boot volume attached to the migration tool VM but its disk was not found: "
                               f"{exc}") from exc
        log.info("job %s: boot volume %s appeared on the migration tool VM as %s", job.id, disk.volume_id, disk.device)

    def _note_boot_device_wait(self, job: Job, expected: int, new: dict[str, int]) -> None:
        seen = ", ".join(f"{p} ({s} bytes)" for p, s in sorted(new.items())) or "none"
        job.message = (
            f"Waiting for the boot volume ({expected} bytes) to appear on the migration tool VM "
            f"(new disks: {seen})"
        )
        self.save(job)

    def _pick_free_device(self, job: Job) -> str:
        used = {d.device for d in job.disks if d.device}
        devices = self.c.compute.list_instance_devices(self.helper_id, is_available=True).data
        # oraclevdb..oraclevdz come before oraclevdaa..; sort by length first, then name
        names = sorted((d.name for d in devices if d.is_available and d.name.startswith(self.s.device_prefix)),
                       key=lambda n: (len(n), n))
        for name in names:
            if name not in used:
                return name
        raise OciError("no free consistent device path on the migration tool VM (max 32 attachments)")

    # ----------------------------------------------------------------- finalize
    def finalize(self, job: Job) -> Job:
        import oci.core.models as M

        not_copied = [d.index for d in job.disks if d.status != DiskStatus.COPIED]
        if not_copied:
            raise OciError(f"disks {not_copied} have not been copied")
        job.phase = JobPhase.FINALIZING
        self._detach_all_from_helper(job)

        target = job.target
        boot = job.disks[0]
        if not boot.target_attachment_id:
            self._step(job, "attach_boot_volume", "Attaching boot volume to target")
            att = self.c.compute.attach_boot_volume(
                M.AttachBootVolumeDetails(boot_volume_id=boot.volume_id, instance_id=job.instance_id)
            ).data
            boot.target_attachment_id = att.id
            self.save(job)
            self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(att.id), "lifecycle_state",
                            ["ATTACHED"], self.s.volume_timeout_s, what="target boot volume attachment")

        # Data volumes were normally attached (shareable) in prepare() while the target still ran.  Whatever is
        # left (emulated attachments cannot be shared) can only be attached to a RUNNING instance, so the
        # guest is started first and the disks are hot-plugged.
        pending = [(n, d) for n, d in enumerate(job.disks[1:], start=1) if not d.target_attachment_id]
        started_for_attach = False
        if pending:
            self._start_target(job, "Starting target instance (OCI attaches data volumes to running instances only)")
            started_for_attach = True
            for n, disk in pending:
                self._step(job, "attach_data_volume", f"Attaching disk {disk.index} to target")
                self._attach_to_target(job, disk, n, shareable=False)

        if target.start_after_migration:
            if not started_for_attach:
                self._start_target(job, "Starting target instance")
        elif started_for_attach:
            # the user asked for a stopped result; the guest already boots, so give it an orderly shutdown
            self._step(job, "stop_instance", "Stopping target instance again (start after migration is off)")
            self.c.compute.instance_action(job.instance_id, "SOFTSTOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="target instance")

        job.phase = JobPhase.COMPLETED
        self._step(job, "completed", "Migration finished")
        return job

    def prepare_export(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        """Stop the source, detach only the boot volume, and attach disks to the helper.

        Block volumes stay on the source.  OCI multi-attach needs every attachment to be
        shareable, so a non-shareable data attachment is converted (detach + reattach
        shareable) while the instance is still RUNNING, then a second shareable attachment
        is taken on the helper.
        """
        spec = job.ova_export
        if spec is None:
            raise OciError("job has no OVA export source")
        if spec.instance_id == self.helper_id:
            raise OciError("cannot export the migration tool VM")

        def step(name: str, message: str = "") -> None:
            self._step(job, name, message, check_cancel)

        inst = self.c.compute.get_instance(spec.instance_id).data
        spec.was_running = inst.lifecycle_state == "RUNNING"
        spec.instance_name = spec.instance_name or (inst.display_name or spec.instance_id)
        spec.compartment_id = spec.compartment_id or inst.compartment_id
        spec.shape = spec.shape or (inst.shape or "")
        cfg = getattr(inst, "shape_config", None)
        if cfg is not None:
            spec.ocpus = spec.ocpus or getattr(cfg, "ocpus", None)
            spec.memory_gb = spec.memory_gb or getattr(cfg, "memory_in_gbs", None)
        lo = getattr(inst, "launch_options", None)
        if lo is not None and not spec.firmware:
            spec.firmware = getattr(lo, "firmware", None)
        pc = getattr(inst, "platform_config", None)
        if pc is not None:
            spec.secure_boot = bool(getattr(pc, "is_secure_boot_enabled", False))
        job.instance_id = spec.instance_id
        job.instance_display_name = spec.instance_name
        if inst.lifecycle_state in ("STOPPED", "STOPPING"):
            job.power_off_result = "already_off"
        self.save(job)

        step("list_attachments", "Listing boot and block volume attachments")
        boot_atts = [
            a for a in self.c.compute.list_boot_volume_attachments(
                inst.availability_domain, inst.compartment_id, instance_id=inst.id
            ).data
            if getattr(a, "lifecycle_state", "ATTACHED") in ("ATTACHED", "ATTACHING")
        ]
        if not boot_atts:
            raise OciError(f"instance {spec.instance_name} has no boot volume attached")
        vol_atts = []
        try:
            vol_atts = [
                a for a in self.c.compute.list_volume_attachments(
                    inst.compartment_id, instance_id=inst.id
                ).data
                if getattr(a, "lifecycle_state", "ATTACHED") in ("ATTACHED", "ATTACHING")
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("job %s: list_volume_attachments failed: %s", job.id, exc)
            if spec.include_data_volumes:
                raise OciError(f"cannot list block volume attachments: {exc}") from exc

        sources: list[ExportDiskSource] = []
        boot = boot_atts[0]
        bv = self.c.blockstorage.get_boot_volume(boot.boot_volume_id).data
        sources.append(ExportDiskSource(
            volume_id=boot.boot_volume_id,
            is_boot=True,
            attachment_id=boot.id,
            size_gb=int(bv.size_in_gbs),
            display_name=getattr(bv, "display_name", None) or "boot",
        ))
        if spec.include_data_volumes:
            for att in sorted(vol_atts, key=lambda a: getattr(a, "device", None) or getattr(a, "id", "") or ""):
                vol = self.c.blockstorage.get_volume(att.volume_id).data
                sources.append(ExportDiskSource(
                    volume_id=att.volume_id,
                    is_boot=False,
                    attachment_id=att.id,
                    device=getattr(att, "device", None),
                    attachment_type=getattr(att, "attachment_type", None) or getattr(att, "type", None),
                    is_read_only=bool(getattr(att, "is_read_only", False)),
                    is_shareable=bool(getattr(att, "is_shareable", False)),
                    size_gb=int(vol.size_in_gbs),
                    display_name=getattr(vol, "display_name", None) or f"disk-{att.volume_id[-6:]}",
                ))
        spec.sources = sources
        job.disks = [
            DiskState(
                index=i,
                label=src.display_name or ("boot" if src.is_boot else f"disk {i}"),
                capacity_bytes=max(1, src.size_gb) * 1024**3,
                volume_id=src.volume_id,
                is_boot=src.is_boot,
                size_gb=src.size_gb,
                status=DiskStatus.PENDING,
            )
            for i, src in enumerate(sources)
        ]
        job.boot_volume_id = sources[0].volume_id
        self.save(job)

        data_pairs = list(zip(sources[1:], job.disks[1:], strict=False))
        if data_pairs:
            self._export_attach_data_volumes(job, spec, data_pairs, step)

        inst = self.c.compute.get_instance(spec.instance_id).data
        if inst.lifecycle_state not in ("STOPPED", "STOPPING"):
            step("stop_instance", f"Stopping {spec.instance_name} for export")
            try:
                self.c.compute.instance_action(spec.instance_id, "SOFTSTOP")
            except Exception:  # noqa: BLE001
                self.c.compute.instance_action(spec.instance_id, "STOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(spec.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="source instance")
            job.power_off_result = "stopped"
        elif inst.lifecycle_state == "STOPPING":
            self.c.wait_for(lambda: self.c.compute.get_instance(spec.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="source instance")

        step("detach_boot_volume", "Detaching boot volume from the source instance")
        if boot.lifecycle_state not in ("DETACHED", "DETACHING"):
            self.c.compute.detach_boot_volume(boot.id)
        self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(boot.id), "lifecycle_state",
                        ["DETACHED"], self.s.volume_timeout_s, what="source boot volume detach")

        boot_disk = job.disks[0]
        if not boot_disk.helper_attachment_id:
            step("attach_boot_to_helper", "Attaching boot volume to the migration tool VM")
            self._attach_boot_volume_to_helper(job, boot_disk, check_cancel)
            boot_disk.status = DiskStatus.ATTACHED
            self.save(job)
        return job

    def _export_attach_data_volumes(
        self,
        job: Job,
        spec: OvaExportSpec,
        pairs: list[tuple[ExportDiskSource, DiskState]],
        step: Callable[[str, str], None],
    ) -> None:
        """Keep data volumes on the source; attach a second shareable handle to the helper."""
        need_running = any(
            (src.attachment_type or "paravirtualized").lower() != "emulated" and not src.is_shareable
            for src, _ in pairs
        )
        inst = self.c.compute.get_instance(spec.instance_id).data
        if need_running and inst.lifecycle_state != "RUNNING":
            step("start_for_shareable",
                 "Starting the instance so data volumes can be attached as shareable")
            if inst.lifecycle_state not in ("STARTING", "PROVISIONING"):
                self.c.compute.instance_action(spec.instance_id, "START")
            self.c.wait_for(lambda: self.c.compute.get_instance(spec.instance_id), "lifecycle_state",
                            ["RUNNING"], self.s.launch_timeout_s, what="source instance for shareable attach")

        for src, disk in pairs:
            kind = (src.attachment_type or "paravirtualized").lower()
            if kind == "emulated":
                if src.attachment_id:
                    step("detach_data_volume",
                         f"Detaching emulated disk {disk.index} (cannot be shared with the helper)")
                    att = self.c.compute.get_volume_attachment(src.attachment_id).data
                    if att.lifecycle_state not in ("DETACHED", "DETACHING"):
                        self.c.compute.detach_volume(src.attachment_id)
                    self.c.wait_for(lambda aid=src.attachment_id: self.c.compute.get_volume_attachment(aid),
                                    "lifecycle_state", ["DETACHED"], self.s.volume_timeout_s,
                                    what=f"source detach emulated disk {disk.index}")
                    src.attachment_id = None
                if not disk.helper_attachment_id:
                    step("attach_data_to_helper", f"Attaching disk {disk.index} to the migration tool VM")
                    device = self._pick_free_device(job)
                    self._attach_to_helper(job, disk, device, shareable=False)
                    disk.device = device
                    disk.status = DiskStatus.ATTACHED
                    self.save(job)
                continue
            if src.attachment_id and not src.is_shareable:
                step("shareable_data_volume",
                     f"Reattaching disk {disk.index} as shareable so it can stay on the source")
                self._reattach_source_shareable(spec.instance_id, src)
                disk.target_attachment_id = src.attachment_id
                self.save(job)
            elif src.attachment_id:
                disk.target_attachment_id = src.attachment_id
            if disk.helper_attachment_id:
                continue
            step("attach_data_to_helper",
                 f"Attaching disk {disk.index} to the migration tool VM (shareable)")
            device = self._pick_free_device(job)
            self._attach_to_helper(job, disk, device, shareable=True)
            disk.device = device
            disk.status = DiskStatus.ATTACHED
            self.save(job)

    def _reattach_source_shareable(self, instance_id: str, src: ExportDiskSource) -> None:
        """Turn a non-shareable source attachment into a shareable one (instance must be RUNNING)."""
        import oci.core.models as M

        att = self.c.compute.get_volume_attachment(src.attachment_id).data
        if att.lifecycle_state not in ("DETACHED", "DETACHING"):
            self.c.compute.detach_volume(src.attachment_id)
        self.c.wait_for(lambda: self.c.compute.get_volume_attachment(src.attachment_id),
                        "lifecycle_state", ["DETACHED"], self.s.volume_timeout_s,
                        what=f"detach {src.display_name or src.volume_id} to make it shareable")
        common = dict(instance_id=instance_id, volume_id=src.volume_id, is_shareable=True)
        if src.device:
            common["device"] = src.device
        if src.display_name:
            common["display_name"] = src.display_name
        if src.is_read_only:
            common["is_read_only"] = True
        kind = (src.attachment_type or "paravirtualized").lower()
        if kind == "iscsi":
            details = M.AttachIScsiVolumeDetails(type="iscsi", **common)
        else:
            details = M.AttachParavirtualizedVolumeDetails(type="paravirtualized", **common)
        new = self.c.compute.attach_volume(details).data
        self.c.wait_for(lambda: self.c.compute.get_volume_attachment(new.id), "lifecycle_state",
                        ["ATTACHED"], self.s.volume_timeout_s,
                        what=f"shareable reattach {src.display_name or src.volume_id}")
        src.attachment_id = new.id
        src.is_shareable = True
        src.attachment_type = kind

    def restore_export_source(self, job: Job) -> Job:
        """Detach helper attachments, reattach the boot volume, leave the instance STOPPED.

        Data volumes are left on the source (shareable).  Only volumes that are no longer
        attached there — typically emulated disks that could not be shared — are reattached.
        """
        spec = job.ova_export
        if spec is None:
            return job
        import oci.core.models as M

        try:
            self._detach_all_from_helper(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("job %s: detach from helper during export restore: %s", job.id, exc)

        instance_id = spec.instance_id
        sources_by_vol = {s.volume_id: s for s in spec.sources}
        boot = next((d for d in job.disks if d.is_boot), job.disks[0] if job.disks else None)
        if boot and boot.volume_id:
            attached = [
                a for a in self.c.compute.list_boot_volume_attachments(
                    job.target.availability_domain, spec.compartment_id or job.target.compartment_id,
                    instance_id=instance_id,
                ).data
                if getattr(a, "lifecycle_state", "") in ("ATTACHED", "ATTACHING")
                and a.boot_volume_id == boot.volume_id
            ]
            if not attached:
                self._step(job, "reattach_boot", "Reattaching boot volume to the source instance")
                att = self.c.compute.attach_boot_volume(
                    M.AttachBootVolumeDetails(boot_volume_id=boot.volume_id, instance_id=instance_id)
                ).data
                boot.target_attachment_id = att.id
                self.save(job)
                self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(att.id), "lifecycle_state",
                                ["ATTACHED"], self.s.volume_timeout_s, what="source boot volume reattach")
            else:
                boot.target_attachment_id = attached[0].id
                self.save(job)

        data = [d for d in job.disks if not d.is_boot]
        current: dict = {}
        if data:
            current = {
                a.volume_id: a
                for a in self.c.compute.list_volume_attachments(
                    spec.compartment_id or job.target.compartment_id, instance_id=instance_id
                ).data
                if getattr(a, "lifecycle_state", "") in ("ATTACHED", "ATTACHING")
            }
        missing = [d for d in data if d.volume_id not in current]
        if missing:
            inst = self.c.compute.get_instance(instance_id).data
            if inst.lifecycle_state != "RUNNING":
                self._step(job, "start_for_reattach",
                           "Starting source instance so detached data volumes can be reattached")
                if inst.lifecycle_state not in ("STARTING", "PROVISIONING"):
                    self.c.compute.instance_action(instance_id, "START")
                self.c.wait_for(lambda: self.c.compute.get_instance(instance_id), "lifecycle_state",
                                ["RUNNING"], self.s.launch_timeout_s, what="source instance for reattach")
            current = {
                a.volume_id: a
                for a in self.c.compute.list_volume_attachments(
                    spec.compartment_id or job.target.compartment_id, instance_id=instance_id
                ).data
                if getattr(a, "lifecycle_state", "") in ("ATTACHED", "ATTACHING")
            }
        for disk in data:
                if disk.volume_id in current:
                    disk.target_attachment_id = current[disk.volume_id].id
                    self.save(job)
                    continue
                src = sources_by_vol.get(disk.volume_id)
                self._step(job, "reattach_data_volume", f"Reattaching disk {disk.index} to the source instance")
                common = dict(instance_id=instance_id, volume_id=disk.volume_id)
                if src and src.device:
                    common["device"] = src.device
                if src and src.display_name:
                    common["display_name"] = src.display_name
                if src and src.is_read_only:
                    common["is_read_only"] = True
                if src and src.is_shareable:
                    common["is_shareable"] = True
                kind = (src.attachment_type if src else None) or "paravirtualized"
                kind = kind.lower()
                if kind == "iscsi":
                    details = M.AttachIScsiVolumeDetails(type="iscsi", **common)
                elif kind == "emulated":
                    details = M.AttachEmulatedVolumeDetails(type="emulated", **common)
                else:
                    details = M.AttachParavirtualizedVolumeDetails(type="paravirtualized", **common)
                att = self.c.compute.attach_volume(details).data
                disk.target_attachment_id = att.id
                self.save(job)
                self.c.wait_for(lambda aid=att.id: self.c.compute.get_volume_attachment(aid),
                                "lifecycle_state", ["ATTACHED"], self.s.volume_timeout_s,
                                what=f"source reattach disk {disk.index}")

        inst = self.c.compute.get_instance(instance_id).data
        if inst.lifecycle_state not in ("STOPPED", "STOPPING"):
            self._step(job, "stop_instance", "Leaving the source instance stopped after export")
            try:
                self.c.compute.instance_action(instance_id, "SOFTSTOP")
            except Exception:  # noqa: BLE001
                self.c.compute.instance_action(instance_id, "STOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="source instance")
        elif inst.lifecycle_state == "STOPPING":
            self.c.wait_for(lambda: self.c.compute.get_instance(instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="source instance")
        return job

    def _start_target(self, job: Job, message: str) -> None:
        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state == "RUNNING":
            return
        self._step(job, "start_instance", message)
        if inst.lifecycle_state not in ("STARTING", "PROVISIONING"):
            self.c.compute.instance_action(job.instance_id, "START")
        self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                        ["RUNNING"], self.s.launch_timeout_s, what="target instance")

    def _detach_all_from_helper(self, job: Job) -> None:
        for disk in job.disks:
            if not disk.helper_attachment_id:
                continue
            self._step(job, "detach_from_helper", f"Detaching disk {disk.index} from the migration tool VM")
            att = self.c.compute.get_volume_attachment(disk.helper_attachment_id).data
            if att.lifecycle_state not in ("DETACHED", "DETACHING"):
                self.c.compute.detach_volume(disk.helper_attachment_id)
            self.c.wait_for(lambda aid=disk.helper_attachment_id: self.c.compute.get_volume_attachment(aid),
                            "lifecycle_state", ["DETACHED"], self.s.volume_timeout_s,
                            what=f"migration tool VM detach disk {disk.index}")
            disk.helper_attachment_id = None
            disk.device = None
            self.save(job)

    def _detach_from_target(self, disk: DiskState) -> None:
        att = self.c.compute.get_volume_attachment(disk.target_attachment_id).data
        if att.lifecycle_state not in ("DETACHED", "DETACHING"):
            self.c.compute.detach_volume(disk.target_attachment_id)
        self.c.wait_for(lambda: self.c.compute.get_volume_attachment(disk.target_attachment_id), "lifecycle_state",
                        ["DETACHED"], self.s.volume_timeout_s, what=f"target detach disk {disk.index}")
        disk.target_attachment_id = None

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, job: Job) -> list[str]:
        """Best-effort teardown of everything this job created.  Returns a list of actions."""
        actions: list[str] = []

        def attempt(desc: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
                actions.append(f"ok: {desc}")
            except Exception as exc:  # noqa: BLE001
                actions.append(f"failed: {desc}: {exc}")

        try:
            self._detach_all_from_helper(job)
        except Exception as exc:  # noqa: BLE001
            actions.append(f"failed: detach from the migration tool VM: {exc}")
        # data volumes attached to the target in prepare(): release them first, otherwise deleting the volume
        # races the detach that terminating the instance triggers
        for disk in job.disks[1:]:
            if disk.target_attachment_id:
                attempt(f"detach disk {disk.index} from target",
                        lambda d=disk: self._detach_from_target(d))
        if job.instance_id:
            attempt(f"terminate instance {job.instance_id}",
                    lambda: self.c.compute.terminate_instance(job.instance_id, preserve_boot_volume=False))
        for disk in job.disks:
            if disk.volume_id and disk.is_boot:
                attempt(f"delete boot volume {disk.volume_id}",
                        lambda vid=disk.volume_id: self.c.blockstorage.delete_boot_volume(vid))
            elif disk.volume_id:
                attempt(f"delete volume {disk.volume_id}",
                        lambda vid=disk.volume_id: self.c.blockstorage.delete_volume(vid))
        job.phase = JobPhase.CANCELLED
        job.step = "cancelled"
        job.message = "; ".join(actions) or "nothing to clean up"
        self.save(job)
        return actions


TAG_VALUE_MAX = 256  # OCI freeform tag values are limited to 256 characters (keys to 100)


def source_tags(job: Job) -> dict[str, str]:
    """Freeform tags that record where the instance came from: the job, the source vCenter, the VM and its
    sizing (vCPU, memory, disks with their capacities, guest OS, firmware)."""
    vm = job.vm
    disks = ", ".join(f"{_gb(d.capacity_bytes):g} GB" for d in sorted(vm.disks, key=lambda d: d.index))
    total = sum(d.capacity_bytes for d in vm.disks)
    firmware = "UEFI" if vm.firmware.value.lower() == "efi" else "BIOS"
    if vm.secure_boot:
        firmware += " Secure Boot"
    prefix = ""
    if job.azure is not None and job.azure.vm_size:
        prefix = f"Azure shape {job.azure.vm_size}, "
    elif job.gcp is not None and job.gcp.machine_type:
        prefix = f"GCP shape {job.gcp.machine_type}, "
    elif job.aws is not None and job.aws.instance_type:
        prefix = f"AWS shape {job.aws.instance_type}, "
    details = (prefix + f"{vm.num_cpu} vCPU, {vm.memory_mb / 1024:g} GB RAM, {len(vm.disks)} disk(s) "
               f"{_gb(total):g} GB [{disks}], {len(vm.nics)} NIC(s), {vm.guest_full_name or vm.guest_id}, {firmware}")
    tags = {
        TAG_JOB: job.id,
        TAG_SOURCE_VM: vm.name[:TAG_VALUE_MAX],
        TAG_SOURCE_MOID: vm.moid,
        TAG_SOURCE_VM_DETAILS: details[:TAG_VALUE_MAX],
    }
    if job.vcenter_host:
        tags[TAG_SOURCE_VCENTER] = job.vcenter_host[:TAG_VALUE_MAX]
    if vm.host_name:
        tags[TAG_SOURCE_ESXI_HOST] = vm.host_name[:TAG_VALUE_MAX]
    if job.azure is not None:
        tags[TAG_SOURCE_AZURE] = f"{job.azure.subscription_id}/{job.azure.resource_group}"[:TAG_VALUE_MAX]
    if job.gcp is not None:
        tags[TAG_SOURCE_GCP] = f"{job.gcp.project_id}/{job.gcp.zone}"[:TAG_VALUE_MAX]
    if job.aws is not None:
        tags[TAG_SOURCE_AWS] = f"{job.aws.account_id}/{job.aws.region}"[:TAG_VALUE_MAX]
    return tags


def _gb(n: int) -> float:
    return round(n / 1024**3, 1)


def _is_windows_job(job: Job) -> bool:
    if job.vm is not None:
        return job.vm.is_windows or map_guest_os(job.vm.guest_id, job.vm.guest_full_name).is_windows
    return job.is_windows
