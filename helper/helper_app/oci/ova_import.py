"""Import an OVA from Object Storage and launch an OCI instance."""

from __future__ import annotations

import logging
from typing import Callable

from helper_app.branding import TAG_JOB, TAG_SOURCE_DETAILS, TAG_SOURCE_OVA
from helper_app.config import Settings
from helper_app.disk.object_vmdk_copy import copy_vmdk_from_ova, copy_vmdk_object
from helper_app.models import BootVolumeType, DiskState, DiskStatus, Job, JobPhase, LaunchOptionsSpec, NetworkType, OciTarget, OvaSpec
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.image_import import ProgressCallback
from helper_app.oci.mapping import OsMetadata, ShapeConfig, remote_data_volume_type_for, volume_size_gb
from helper_app.oci.provision import Provisioner
from helper_app.jobs.store import utcnow
from helper_app.ova.package import OvaDiskSource, OvaPackageError, parse_ova_layout, staging_namespace

log = logging.getLogger(__name__)

TAG_VALUE_MAX = 256
STEP_STAGE = "ova_stage"
STEP_COPY = "copying"


def ova_launch_options(ova: OvaSpec, target: OciTarget) -> LaunchOptionsSpec:
    boot_type = BootVolumeType.PARAVIRTUALIZED
    net_type = NetworkType.PARAVIRTUALIZED
    if target.compatibility_mode:
        boot_type = BootVolumeType.IDE
        net_type = NetworkType.E1000
    if target.boot_volume_type_override:
        boot_type = target.boot_volume_type_override
    if target.network_type_override:
        net_type = target.network_type_override
    return LaunchOptionsSpec(
        firmware=ova.firmware,
        boot_volume_type=boot_type,
        network_type=net_type,
        remote_data_volume_type=remote_data_volume_type_for(boot_type),
        is_consistent_volume_naming_enabled=not ova.is_windows,
        secure_boot=bool(ova.secure_boot) and ova.firmware == "UEFI_64",
    )


def ova_source_tags(job: Job) -> dict[str, str]:
    ova = job.ova
    assert ova is not None
    firmware = "UEFI" if ova.firmware == "UEFI_64" else "BIOS"
    if ova.secure_boot:
        firmware += " Secure Boot"
    boot_gb = volume_size_gb(job.disks[0].capacity_bytes) if job.disks else 0
    details = f"{ova.operating_system} {ova.operating_system_version}, {firmware}, boot {boot_gb} GB"
    return {
        TAG_JOB: job.id,
        TAG_SOURCE_OVA: ova.key[-TAG_VALUE_MAX:],
        TAG_SOURCE_DETAILS: details[:TAG_VALUE_MAX],
    }


class OvaImporter:
    def __init__(self, clients: OciClients, settings: Settings, provisioner: Provisioner, save: Callable[[Job], Job]):
        self.c = clients
        self.s = settings
        self.prov = provisioner
        self.save = save

    def _step(self, job: Job, step: str, message: str = "", check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()
        job.step = step
        job.step_percent = None
        job.message = message or step
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)

    def run(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        if job.ova is None:
            raise OciError("job has no OVA source")
        ova, target = job.ova, job.target
        if target.availability_domain != self.c.identity_info.availability_domain:
            raise OciError(
                f"target availability domain {target.availability_domain} differs from the migration tool VM's "
                f"{self.c.identity_info.availability_domain}"
            )

        job.phase = JobPhase.PROVISIONING
        lo = ova_launch_options(ova, target)
        job.launch_options = lo
        namespace = ova.namespace or staging_namespace(self.c)

        self._step(job, STEP_STAGE, f"Parsing {ova.object_name} and preparing volumes", check_cancel)
        try:
            layout = parse_ova_layout(self.c, namespace, ova.bucket, ova.object_name)
        except OvaPackageError as exc:
            raise OciError(str(exc)) from exc
        job.ova_staging_prefix = ""
        sources = {d.index: d for d in layout.disks}
        job.disks = [
            DiskState(
                index=d.index,
                label=d.label,
                capacity_bytes=d.capacity_bytes,
                is_boot=d.is_boot,
                size_gb=volume_size_gb(d.capacity_bytes, self.s.min_volume_gb),
            )
            for d in layout.disks
        ]
        self.save(job)

        display = target.display_name or ova.object_name.rsplit("/", 1)[-1]
        os_meta = OsMetadata(
            operating_system=ova.operating_system,
            operating_system_version=ova.operating_system_version,
            family="windows" if ova.is_windows else "linux",
            version_detected=True,
        )
        shape = ShapeConfig(
            shape=target.shape or self.s.default_shape,
            ocpus=float(target.ocpus or 1),
            memory_gb=float(target.memory_gb or 8),
        )

        if not job.boot_volume_id:
            self.prov.prepare_placeholder(
                job,
                os_meta=os_meta,
                firmware=ova.firmware,
                shape=shape,
                launch_options=lo,
                instance_tags=ova_source_tags(job),
                display_name=display,
                is_windows=ova.is_windows,
                windows_license=target.windows_license_type,
                check_cancel=check_cancel,
            )

        job.phase = JobPhase.EXPORTING
        job.transfer.started_at = job.transfer.started_at or utcnow()
        for disk in sorted(job.disks, key=lambda d: d.index):
            if disk.status == DiskStatus.COPIED:
                continue
            if check_cancel:
                check_cancel()
            src = sources[disk.index]
            self._step(
                job,
                STEP_COPY,
                f"Copying {disk.label} from Object Storage onto OCI volume",
                check_cancel,
            )
            disk.status = DiskStatus.COPYING
            self.save(job)
            if not disk.device:
                raise OciError(f"disk {disk.index} is not attached to the migration tool VM")

            def prog(recv: int, written: int) -> None:
                disk.bytes_received = recv
                disk.bytes_written = written
                total = disk.capacity_bytes or 1
                disk.percent = min(99, int(recv * 100 / total))
                job.transfer.bytes_received = sum(d.bytes_received for d in job.disks)
                job.transfer.bytes_written = sum(d.bytes_written for d in job.disks)
                total_cap = max(1, sum(d.capacity_bytes for d in job.disks))
                job.transfer.percent = min(99, int(job.transfer.bytes_received * 100 / total_cap))
                self.save(job)

            recv, written = self._copy_vmdk_source(
                namespace, ova.bucket, src, disk.device, disk.capacity_bytes, prog,
            )
            disk.bytes_received = recv
            disk.bytes_written = written
            disk.percent = 100
            disk.status = DiskStatus.COPIED
            self.save(job)

        job.transfer.percent = 100
        job.transfer.finished_at = utcnow()
        return job

    def _copy_vmdk_source(
        self,
        namespace: str,
        bucket: str,
        src: OvaDiskSource,
        device: str,
        capacity_bytes: int,
        on_progress: ProgressCallback | None,
    ) -> tuple[int, int]:
        if src.bucket_object:
            return copy_vmdk_object(
                self.c,
                namespace,
                bucket,
                src.bucket_object,
                device,
                capacity_bytes,
                skip_zero_grains=self.s.skip_zero_grains,
                on_progress=on_progress,
            )
        if src.ova_object:
            return copy_vmdk_from_ova(
                self.c,
                namespace,
                bucket,
                src.ova_object,
                src.vmdk_href,
                device,
                capacity_bytes,
                skip_zero_grains=self.s.skip_zero_grains,
                on_progress=on_progress,
            )
        raise OciError(f"disk {src.index} has no Object Storage source")

    def cleanup(self, job: Job) -> list[str]:
        return self.prov.cleanup(job)
