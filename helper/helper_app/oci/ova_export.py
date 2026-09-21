"""Export an OCI instance to an OVF set (descriptor, VMDKs, manifest) in Object Storage."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Callable

from helper_app.config import Settings
from helper_app.disk.block_reader import iter_device_grains
from helper_app.disk.vmdk_stream import encode_stream_optimized
from helper_app.jobs.progress import RateMeter
from helper_app.jobs.store import utcnow
from helper_app.models import DiskStatus, Job, JobPhase
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.object_upload import MultipartObjectWriter, put_small_object
from helper_app.oci.provision import Provisioner
from helper_app.ova.ovf_writer import build_manifest, build_ovf

log = logging.getLogger(__name__)


def sanitize_export_name(name: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", (name or "").strip()).strip(".-")
    return (s or "instance")[:120]


class OvaExporter:
    def __init__(self, clients: OciClients, settings: Settings, provisioner: Provisioner, save):
        self.c = clients
        self.s = settings
        self.prov = provisioner
        self.save = save
        self._open_writers: list[MultipartObjectWriter] = []

    def run(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        spec = job.ova_export
        if spec is None:
            raise OciError("job has no OVA export source")

        job.phase = JobPhase.PROVISIONING
        self._step(job, "prepare_export", "Stopping the instance and attaching its volumes", check_cancel)
        self.prov.prepare_export(job, check_cancel=check_cancel)

        name = sanitize_export_name(spec.instance_name or spec.instance_id)
        prefix = (spec.prefix or name).strip("/")
        spec.prefix = prefix

        job.phase = JobPhase.EXPORTING
        job.transfer.started_at = job.transfer.started_at or utcnow()
        self.save(job)
        disk_files: list[dict] = []
        hashes: list[tuple[str, str]] = []
        objects: list[str] = []
        meter = RateMeter()

        for i, disk in enumerate(job.disks):
            if check_cancel:
                check_cancel()
            if not disk.device:
                raise OciError(f"disk {disk.index} is not attached to the migration tool VM")
            href = f"{name}-disk{i + 1}.vmdk"
            object_name = f"{prefix}/{href}"
            disk.status = DiskStatus.COPYING
            disk.attempts += 1
            self._step(job, "export_disk", f"Exporting {disk.label} to {object_name}", check_cancel)
            self.save(job)

            writer = MultipartObjectWriter(self.c, spec.namespace, spec.bucket, object_name)
            self._open_writers.append(writer)
            last_recv = 0
            last_save = time.monotonic()
            last_saved_recv = 0

            def on_progress(recv: int, _emitted: int, d=disk, w=writer) -> None:
                nonlocal last_recv, last_save, last_saved_recv
                delta = recv - last_recv
                last_recv = recv
                if delta:
                    meter.add(delta)
                d.bytes_received = recv
                d.bytes_written = w.bytes_written
                d.percent = min(99, int(recv * 100 / max(1, d.capacity_bytes)))
                d.throughput_bps = meter.rate()
                job.transfer.bytes_received = sum(x.bytes_received for x in job.disks)
                job.transfer.bytes_written = sum(x.bytes_written for x in job.disks)
                total_cap = max(1, sum(x.capacity_bytes for x in job.disks))
                job.transfer.percent = min(99, int(job.transfer.bytes_received * 100 / total_cap))
                job.transfer.throughput_bps = meter.rate()
                now = time.monotonic()
                if recv - last_saved_recv >= 128 * 1024 * 1024 or now - last_save >= 2.0:
                    last_save = now
                    last_saved_recv = recv
                    self.save(job)

            try:
                grains = iter_device_grains(
                    disk.device,
                    disk.capacity_bytes,
                    check_cancel=check_cancel,
                    on_progress=on_progress,
                )
                encode_stream_optimized(writer, disk.capacity_bytes, grains, extent_name=href)
                writer.close()
            except Exception:
                writer.abort()
                disk.status = DiskStatus.FAILED
                self.save(job)
                raise
            if writer in self._open_writers:
                self._open_writers.remove(writer)

            disk.bytes_written = writer.bytes_written
            disk.stream_bytes = writer.bytes_written
            disk.percent = 100
            disk.status = DiskStatus.COPIED
            objects.append(object_name)
            hashes.append((href, writer.sha256_hex))
            disk_files.append({
                "file_id": f"file{i + 1}",
                "href": href,
                "size_bytes": writer.bytes_written,
                "capacity_bytes": disk.capacity_bytes,
                "populated_size": disk.bytes_received,
                "label": disk.label,
            })
            self.save(job)

        job.transfer.percent = 100
        job.transfer.finished_at = utcnow()
        job.phase = JobPhase.FINALIZING
        self._step(job, "write_ovf", "Writing the OVF descriptor and manifest", check_cancel)

        inst = self.c.compute.get_instance(spec.instance_id).data
        os_name, os_ver = spec.operating_system, spec.operating_system_version
        image_id = getattr(inst, "image_id", None)
        if image_id:
            try:
                image = self.c.compute.get_image(image_id).data
                os_name = os_name or (getattr(image, "operating_system", None) or "")
                os_ver = os_ver or (getattr(image, "operating_system_version", None) or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("job %s: get_image %s failed: %s", job.id, image_id, exc)
        spec.operating_system = os_name
        spec.operating_system_version = os_ver

        num_vcpu = int(spec.ocpus or 1)
        if spec.ocpus:
            num_vcpu = max(1, int(round(float(spec.ocpus) * 2)))
        memory_mb = int(round(float(spec.memory_gb or 1) * 1024))
        firmware = spec.firmware or "UEFI_64"

        ovf_name = f"{name}.ovf"
        mf_name = f"{name}.mf"
        ovf_object = f"{prefix}/{ovf_name}"
        mf_object = f"{prefix}/{mf_name}"
        ovf_bytes = build_ovf(
            name,
            (os_name, os_ver),
            num_vcpu=num_vcpu,
            memory_mb=memory_mb,
            firmware=firmware,
            secure_boot=spec.secure_boot,
            disks=disk_files,
        )
        hashes.insert(0, (ovf_name, hashlib.sha256(ovf_bytes).hexdigest()))
        mf_bytes = build_manifest(hashes)
        put_small_object(self.c, spec.namespace, spec.bucket, ovf_object, ovf_bytes)
        put_small_object(self.c, spec.namespace, spec.bucket, mf_object, mf_bytes)
        objects.extend([ovf_object, mf_object])
        spec.objects = objects
        spec.ovf_object = ovf_object
        spec.manifest_object = mf_object
        self.save(job)

        self._step(job, "restore_source", "Reattaching volumes to the source instance", check_cancel)
        self.prov.restore_export_source(job)
        job.phase = JobPhase.COMPLETED
        job.message = (
            f"Exported {len(job.disks)} disk(s) to {spec.bucket}/{prefix}/ "
            f"({ovf_name}, {len(disk_files)} VMDK, {mf_name}); instance left STOPPED"
        )
        self.save(job)
        return job

    def cleanup(self, job: Job) -> None:
        for writer in list(self._open_writers):
            writer.abort()
        self._open_writers.clear()
        try:
            self.prov.restore_export_source(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("job %s: export restore during cleanup: %s", job.id, exc)
        if job.phase not in (JobPhase.COMPLETED, JobPhase.FAILED, JobPhase.CANCELLED):
            job.phase = JobPhase.CANCELLED
            job.message = job.message or "Cancelled; boot volume reattached, instance left STOPPED"
        self.save(job)

    def _step(self, job: Job, step: str, message: str, check_cancel: Callable[[], None] | None) -> None:
        if check_cancel is not None:
            check_cancel()
        job.step = step
        job.message = message
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)
