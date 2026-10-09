"""Migration orchestration: one thread per job runs the whole pipeline.

    provision OCI target (Provisioner.prepare) -> shut down the source if it is still powered on
    (confirmed by the user) -> open NFC lease on the user's vCenter session
    -> for each disk: GET stream-optimized VMDK, decode grains onto the attached OCI volume
       (retried from the beginning on failure) -> finalize (Provisioner.finalize)

Azure jobs follow the same shape: provision -> deallocate the VM (or snapshot its disks) -> grant an export
SAS per disk -> copy the allocated page ranges onto the attached OCI volume -> finalize.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from helper_app.aws.client import AwsError
from helper_app.aws.export import AwsDiskExport
from helper_app.aws.inventory import power_state as aws_power_state
from helper_app.azure.client import AzureError
from helper_app.azure.export import AzureDiskExport
from helper_app.azure.inventory import power_state as azure_power_state
from helper_app.config import Settings
from helper_app.disk.ebs_range_copy import EbsCopyError, copy_blocks, list_blocks
from helper_app.disk.gcs_range_copy import (
    GcsCopyError,
    copy_from_gcs_export_tarball,
    copy_parallel_ranges,
    object_size,
)
from helper_app.disk.imageio_range_copy import ImageioAuthExpired, ImageioCopyError, allocated_ranges, copy_extents
from helper_app.disk.pipeline import PipelinedDecoder
from helper_app.disk.vhd_image import ImageError, open_chain
from helper_app.disk.vhd_range_copy import VhdCopyError, blob_length, copy_ranges, list_page_ranges
from helper_app.disk.vmdk_stream import DecodeStats, StreamOptimizedDecoder, VmdkFormatError
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.gcp.client import GcpError
from helper_app.gcp.export import GcpDiskExport
from helper_app.gcp.inventory import power_state as gcp_power_state
from helper_app.guest.fixup import GuestFixer, GuestFixerFn
from helper_app.hyperv.client import HypervError
from helper_app.hyperv.inventory import power_state as hyperv_power_state
from helper_app.hyperv.power import shut_down as hyperv_shut_down
from helper_app.hyperv.smb import to_unc
from helper_app.jobs.progress import RateMeter
from helper_app.jobs.source_cleanup import release_source
from helper_app.jobs.store import JobStore, utcnow
from helper_app.models import DiskState, DiskStatus, GuestFixup, Job, JobPhase
from helper_app.oci.clients import describe_error
from helper_app.oci.iso_install import IsoInstaller
from helper_app.oci.ova_export import OvaExporter
from helper_app.oci.ova_import import OvaImporter
from helper_app.oci.provision import Provisioner
from helper_app.olvm.client import OlvmAuthError, OlvmError
from helper_app.olvm.export import OlvmDiskExport
from helper_app.olvm.inventory import power_state as olvm_power_state
from helper_app.olvm.power import shut_down as olvm_shut_down
from helper_app.runtime_settings import MAX_CONCURRENT_JOBS
from helper_app.sessions import UserSession
from helper_app.vsphere.export import ExportError, NfcExport, match_disk_urls
from helper_app.vsphere.inventory import esxi_host_name
from helper_app.vsphere.power import shut_down

log = logging.getLogger(__name__)

PROGRESS_SAVE_BYTES = 128 * 1024 * 1024
PROGRESS_SAVE_SECONDS = 2.0  # also persist progress this often, so slow links still show movement
# threads for migrations (running or waiting for a slot, bounded by the Setup page limit) plus headroom for
# cleanups and finalize retries
MAX_POOL_WORKERS = MAX_CONCURRENT_JOBS + 8


class JobCancelled(RuntimeError):
    pass


class MigrationRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        provisioner: Provisioner,
        export_factory: Optional[Callable[[object, str, bool], NfcExport]] = None,
        guest_fixer: Optional[GuestFixerFn] = None,
        iso_installer: Optional[IsoInstaller] = None,
        ova_importer: Optional[OvaImporter] = None,
        ova_exporter: Optional[OvaExporter] = None,
    ):
        self.s = settings
        self.store = store
        self.prov = provisioner
        self.iso = iso_installer or IsoInstaller(provisioner.c, settings, store.put)
        self.ova = ova_importer or OvaImporter(provisioner.c, settings, provisioner, store.put)
        self.ova_export = ova_exporter or OvaExporter(provisioner.c, settings, provisioner, store.put)
        self.export_factory = export_factory or self._default_export_factory
        self.guest_fixer: GuestFixerFn = guest_fixer or GuestFixer().fix
        # The pool only provides threads; how many migrations copy at the same time is gated by
        # ``settings.max_concurrent_jobs`` in _acquire_slot, so the limit can be changed at runtime.
        self.pool = ThreadPoolExecutor(max_workers=max(MAX_POOL_WORKERS, settings.max_concurrent_jobs),
                                       thread_name_prefix="migration")
        self._sessions: dict[str, UserSession] = {}
        self._running: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._lock = threading.Lock()
        self._slots = threading.Condition()
        self._migrating = 0  # migrations holding a slot

    def _default_export_factory(self, vm, nfc_host: str, verify_ssl: bool) -> NfcExport:
        return NfcExport(
            vm,
            nfc_host=nfc_host,
            verify_ssl=verify_ssl,
            progress_interval_s=self.s.lease_progress_interval_s,
            ready_timeout_s=self.s.lease_ready_timeout_s,
            chunk_bytes=self.s.nfc_chunk_bytes,
        )

    # --------------------------------------------------------------- control
    def submit(self, job_id: str, session: UserSession) -> Future:
        """Start the migration ``job_id`` using ``session``'s vCenter connection (VMware jobs) or Azure
        service principal (Azure jobs)."""
        session.pin(job_id)
        with self._lock:
            self._sessions[job_id] = session
            self._running.add(job_id)
        return self.pool.submit(self._run_safely, job_id)

    def submit_iso(self, job_id: str) -> Future:
        """Start an ISO job: no vCenter session and no copy slot (the helper moves no data for it)."""
        with self._lock:
            self._running.add(job_id)
        return self.pool.submit(self._run_iso_safely, job_id)

    def submit_ova(self, job_id: str) -> Future:
        """Start an OVA import job (uses a migration slot when data disks are copied)."""
        with self._lock:
            self._running.add(job_id)
        return self.pool.submit(self._run_ova_safely, job_id)

    def submit_ova_export(self, job_id: str) -> Future:
        """Start an OVA/OVF export of an existing OCI instance."""
        with self._lock:
            self._running.add(job_id)
        return self.pool.submit(self._run_ova_export_safely, job_id)

    def cleanup(self, job_id: str, session: Optional[UserSession] = None) -> Future:
        """Tear down the OCI resources of a job that is not running (failed or restarted).  ``session`` (an
        cloud login) lets cleanup release export access, snapshots and staging objects left behind."""
        with self._lock:
            self._running.add(job_id)
            if session is not None:
                session.pin(job_id)
                self._sessions[job_id] = session
        return self.pool.submit(self._cleanup_safely, job_id)

    def resume_finalize(self, job_id: str) -> Future:
        """Re-run the finalize step of a failed job whose disks are all copied (e.g. an attach rejected by
        OCI).  Needs no vCenter session: the copied volumes already exist in OCI."""
        with self._lock:
            self._running.add(job_id)
        return self.pool.submit(self._finalize_safely, job_id)

    @staticmethod
    def can_resume_finalize(job: Job) -> bool:
        return (job.phase == JobPhase.FAILED and bool(job.instance_id) and bool(job.disks)
                and all(d.status == DiskStatus.COPIED for d in job.disks))

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancel_requested.add(job_id)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    def fail_stale_jobs(self) -> list[str]:
        """Jobs that were in flight when the helper stopped cannot resume (their vCenter session is gone).
        ISO jobs in ``INSTALLING`` need nothing from the helper and stay as they are."""
        failed = []
        for job in self.store.active():
            if job.phase == JobPhase.INSTALLING:
                continue
            job.error = "migration tool restarted during the migration; cancel the job to clean up its OCI resources"
            job.phase = JobPhase.FAILED
            job.message = job.error
            self.store.put(job)
            failed.append(job.id)
        if failed:
            log.warning("marked %d interrupted job(s) as FAILED: %s", len(failed), failed)
        return failed

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- execution
    def _save(self, job: Job, phase: Optional[JobPhase] = None, message: Optional[str] = None) -> None:
        if phase is not None:
            job.phase = phase
        if message is not None:
            job.message = message
            log.info("job %s [%s] %s", job.id, job.phase.value, message)
        self.store.put(job)

    def _finish(self, job_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(job_id, None)
            self._running.discard(job_id)
            self._cancel_requested.discard(job_id)
        if session is not None:
            session.unpin(job_id)

    # ------------------------------------------------------------ concurrency
    @property
    def max_concurrent(self) -> int:
        return max(1, min(MAX_CONCURRENT_JOBS, int(self.s.max_concurrent_jobs)))

    def set_max_concurrent(self, n: int) -> None:
        """Change the migration concurrency at once; queued jobs start as slots open up, running ones
        are never interrupted when the limit shrinks."""
        with self._slots:
            self.s.max_concurrent_jobs = max(1, min(MAX_CONCURRENT_JOBS, int(n)))
            self._slots.notify_all()

    def _acquire_slot(self, job: Job) -> None:
        waited = False
        with self._slots:
            while self._migrating >= self.max_concurrent:
                if self._cancelled(job.id):
                    raise JobCancelled()
                if not waited:
                    waited = True
                    self._save(job, message=f"Waiting for a free migration slot "
                                            f"({self._migrating} of {self.max_concurrent} in use)")
                self._slots.wait(timeout=1.0)
            self._migrating += 1

    def _release_slot(self) -> None:
        with self._slots:
            self._migrating = max(0, self._migrating - 1)
            self._slots.notify_all()

    def _run_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        slot = False
        try:
            self._acquire_slot(job)
            slot = True
            if job.kind == "azure":
                self._run_azure(job)
            elif job.kind == "gcp":
                self._run_gcp(job)
            elif job.kind == "aws":
                self._run_aws(job)
            elif job.kind == "olvm":
                self._run_olvm(job)
            elif job.kind == "hyperv":
                self._run_hyperv(job)
            else:
                self._run(job)
        except JobCancelled:
            self._save(job, message="Cancelled; cleaning up OCI resources")
            try:
                self.prov.cleanup(job)
                self._release_source(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup for %s failed: %s", job.id, describe_error(exc))
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
        finally:
            if slot:
                self._release_slot()
            self._finish(job_id)

    def _release_source(self, job: Job) -> None:
        release_source(job, self._sessions.get(job.id), self.store.put)

    def _run_ova_export_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        slot = False
        try:
            self._acquire_slot(job)
            slot = True
            self._save(job, JobPhase.PROVISIONING, "Starting OVA/OVF export")
            self.ova_export.run(job, check_cancel=lambda: self._check_cancel(job))
        except JobCancelled:
            self._save(job, message="Cancelled; restoring the source instance")
            try:
                self.ova_export.cleanup(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("export cleanup for %s failed: %s", job.id, describe_error(exc))
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
            try:
                self.ova_export.cleanup(job)
            except Exception as clean_exc:  # noqa: BLE001
                log.warning("export cleanup for %s failed: %s", job.id, describe_error(clean_exc))
        finally:
            if slot:
                self._release_slot()
            self._finish(job_id)

    def _run_ova_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        slot = False
        try:
            self._acquire_slot(job)
            slot = True
            self._save(job, JobPhase.PROVISIONING, "Starting OVA import")
            self.ova.run(job, check_cancel=lambda: self._check_cancel(job))
            self._check_cancel(job)
            self._guest_fixup(job)
            self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
            self.prov.finalize(job)
            self._save(job, message=f"Migration complete: instance {job.instance_id}")
        except JobCancelled:
            self._save(job, message="Cancelled; cleaning up OCI resources")
            try:
                self.ova.cleanup(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup for %s failed: %s", job.id, describe_error(exc))
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
        finally:
            if slot:
                self._release_slot()
            self._finish(job_id)

    def _run_iso_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        try:
            self._save(job, JobPhase.PROVISIONING, "Preparing the ISO image and the instance in OCI")
            self.iso.run(job, check_cancel=lambda: self._check_cancel(job))
        except JobCancelled:
            self._save(job, message="Cancelled; cleaning up OCI resources")
            try:
                self.iso.cleanup(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup for %s failed: %s", job.id, describe_error(exc))
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
        finally:
            self._finish(job_id)

    def _finalize_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        try:
            job.error = None
            self._save(job, JobPhase.FINALIZING, "Resuming: attaching volumes to the target instance")
            self.prov.finalize(job)
            self._save(job, message=f"Migration complete: instance {job.instance_id}")
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed again at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
        finally:
            self._finish(job_id)

    def _cleanup_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            if job is not None and job.kind == "iso":
                self.iso.cleanup(job)
            elif job is not None and job.kind == "ova":
                self.ova.cleanup(job)
            elif job is not None and job.kind == "ovaexport":
                self.ova_export.cleanup(job)
            elif job is not None:
                self.prov.cleanup(job)
                self._release_source(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("cleanup of %s failed", job_id)
            if job is not None:
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        finally:
            self._finish(job_id)

    def _check_cancel(self, job: Job) -> None:
        if self._cancelled(job.id):
            raise JobCancelled()

    def _run(self, job: Job) -> None:
        session = self._sessions[job.id]

        # 1. provision the OCI target and attach its volumes to the helper
        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        # 2. export - first make sure the source is powered off (shutting it down here, after the OCI side
        #    is ready, keeps the downtime of a running VM as short as possible)
        self._save(job, JobPhase.EXPORTING, "Checking the source VM")
        vm = session.vc.vm(job.vm.moid)
        power = str(vm.runtime.powerState)
        if power != "poweredOff":
            if not job.power_off_source:
                raise ExportError(f"VM is {power}; it must stay powered off during the export")
            job.step = "power_off"
            job.power_off_result = shut_down(
                vm, job.vm.name, timeout_s=self.s.guest_shutdown_timeout_s,
                notify=lambda msg: self._save(job, message=msg), check_cancel=lambda: self._check_cancel(job),
            )
            job.vm.power_state = "poweredOff"
            self._save(job, message={"guest_shutdown": f"{job.vm.name} shut down cleanly through VMware Tools",
                                     "powered_off": f"{job.vm.name} powered off"}.get(job.power_off_result, ""))
        elif job.power_off_source:
            job.power_off_result = "already_off"  # someone shut it down in the meantime
        nfc_host = self._resolve_nfc_host(job, vm, session)
        job.nfc_host = nfc_host
        job.step = "export_lease"  # ExportVm; failures here must not be blamed on the power-off step
        self._save(job, message=f"Opening NFC export lease (disk download via {nfc_host})")
        # the TLS choice made at login covers the disk download as well
        verify_ssl = bool(getattr(session.vc, "verify_ssl", False))
        with self.export_factory(vm, nfc_host, verify_ssl) as export:
            urls = match_disk_urls(job.vm.disks, export.disk_urls())
            for disk in job.disks:
                disk.stream_bytes = urls[disk.index].file_size or None
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    try:
                        self._export_disk(job, disk, export, urls[disk.index].url)
                    except BaseException:
                        export.mark_failed(f"disk {disk.index}")
                        raise
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0

        # 3. guest fix-up on the copied boot volume (still attached to the helper): make sure the initramfs
        #    knows virtio (else RHEL-family guests built on VMware drop into the dracut emergency shell) and
        #    that the guest configures its renamed network interface with DHCP
        self._check_cancel(job)
        self._guest_fixup(job)

        # 4. finalize
        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    # ------------------------------------------------------------------ Azure
    def _run_azure(self, job: Job) -> None:
        session = self._sessions[job.id]
        if session.azure is None:
            raise ExportError("the session that created the job has no Azure login")
        client = session.azure.client
        info = job.azure
        if info is None:
            raise ExportError("job has no Azure source information")

        # 1. provision the OCI target and attach its volumes to the helper (identical to the VMware path)
        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        # 2. capture the source: deallocate the VM (its disks can only be exported then), or leave it running
        #    and snapshot the disks (done inside AzureDiskExport)
        self._save(job, JobPhase.EXPORTING, "Checking the source VM in Azure")
        vm_id = job.vm.moid
        vm = client.get_vm(vm_id)
        state = azure_power_state(vm)
        job.vm.power_state = state
        if info.capture_mode == "deallocate":
            if state != "poweredOff":
                if not job.power_off_source:
                    raise ExportError(f"VM is {state.replace('poweredOn', 'running')}; it must be deallocated during "
                                      "the export (or use snapshot mode)")
                job.step = "power_off"
                self._save(job, message=f"Deallocating {job.vm.name} in Azure")
                client.deallocate_vm(vm_id, timeout_s=self.s.azure_deallocate_timeout_s,
                                     on_wait=lambda: self._check_cancel(job))
                job.power_off_result = "deallocated"
                job.vm.power_state = "poweredOff"
                self._save(job, message=f"{job.vm.name} deallocated")
            elif job.power_off_source:
                job.power_off_result = "already_off"
        else:
            job.power_off_result = "snapshotted"

        job.step = "export_access"
        what = "snapshots" if info.capture_mode == "snapshot" else "disks"
        self._save(job, message=f"Granting export access to the {what} of {job.vm.name}")
        export = AzureDiskExport(client, job.id, info, sas_duration_s=self.s.azure_sas_duration_s,
                                 snapshot_timeout_s=self.s.azure_snapshot_timeout_s, save=lambda: self.store.put(job),
                                 check_cancel=lambda: self._check_cancel(job))
        with export:
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    self._copy_azure_disk(job, disk, export, client)
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0
        job.step = "export_released"
        self._save(job, message="Export access revoked" + (", snapshots deleted" if info.capture_mode == "snapshot"
                                                               else ""))

        # 3. guest fix-up (Linux guests built on Hyper-V drivers need virtio in the initramfs like VMware ones)
        self._check_cancel(job)
        self._guest_fixup(job)

        # 4. finalize
        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _record_cloud_progress(self, job: Job, disk: DiskState, received: int, written: int, chunks: int,
                               rate_bps: float, written_before: int) -> None:
        disk.bytes_received = received
        disk.bytes_written = written
        disk.grains_written = chunks
        disk.throughput_bps = rate_bps
        total = disk.stream_bytes or disk.capacity_bytes
        disk.percent = max(0, min(99, int(received * 100 / total))) if total else 0
        job.transfer.bytes_written = written_before + written
        job.transfer.throughput_bps = rate_bps
        job_total = sum(d.stream_bytes or d.capacity_bytes for d in job.disks)
        done = sum(d.bytes_written for d in job.disks if d is not disk) + written
        job.transfer.percent = max(0, min(99, int(done * 100 / job_total))) if job_total else 0

    def _cloud_progress_callback(self, job: Job, disk: DiskState, meter: RateMeter,
                                 written_before: int) -> Callable[[int], None]:
        """Per-chunk progress hook for ``copy_ranges`` (called from several download threads): counts the
        bytes and persists the job every PROGRESS_SAVE_BYTES / PROGRESS_SAVE_SECONDS."""
        lock = threading.Lock()
        state = {"received": 0, "chunks": 0, "last_saved": 0, "last_saved_at": time.monotonic()}

        def on_progress(n: int) -> None:
            with lock:
                state["received"] += n
                state["chunks"] += 1
                job.transfer.bytes_received += n
                meter.add(n)
                now = time.monotonic()
                if (state["received"] - state["last_saved"] >= PROGRESS_SAVE_BYTES
                        or now - state["last_saved_at"] >= PROGRESS_SAVE_SECONDS):
                    state["last_saved"], state["last_saved_at"] = state["received"], now
                    self._record_cloud_progress(job, disk, state["received"], state["received"], state["chunks"],
                                                meter.rate(), written_before)
                    self.store.put(job)

        return on_progress

    def _copy_azure_disk(self, job: Job, disk: DiskState, export: AzureDiskExport, client) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Listing the allocated pages of {label} "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")
            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)
            try:
                writer.ensure_size(disk.capacity_bytes)
                if export.expires_soon:
                    export.refresh(disk.index)
                sas = export.sas_url(disk.index)
                length = blob_length(client, sas)
                disk_bytes = min(disk.capacity_bytes, max(0, length - 512))
                ranges = list_page_ranges(client, sas, disk_bytes)
                allocated = sum(ln for _, ln in ranges)
                disk.stream_bytes = allocated or None
                self._save(job, message=f"Copying {label} to {disk.device}: {allocated:,} of {disk_bytes:,} bytes "
                                        f"allocated in {len(ranges)} range(s)")
                stats = copy_ranges(
                    client, lambda: export.sas_url(disk.index), ranges, writer,
                    chunk_bytes=self.s.azure_range_chunk_bytes, workers=self.s.azure_range_workers,
                    check_cancel=lambda: self._check_cancel(job),
                    on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                    refresh=lambda: export.refresh(disk.index),
                )
                self._record_cloud_progress(job, disk, stats.bytes_received, stats.bytes_written,
                                            stats.chunks_written, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({stats.bytes_received:,} bytes in "
                                        f"{stats.chunks_written:,} range request(s)"
                                        + (f", {stats.retries} retried" if stats.retries else "") + ")")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, VhdCopyError, AzureError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")

    # ------------------------------------------------------------------- GCP
    def _run_gcp(self, job: Job) -> None:
        session = self._sessions[job.id]
        if session.gcp is None:
            raise ExportError("the session that created the job has no GCP login")
        client = session.gcp.client
        info = job.gcp
        if info is None:
            raise ExportError("job has no GCP source information")

        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        self._save(job, JobPhase.EXPORTING, "Checking the source VM in Google Cloud")
        vm_id = job.vm.moid
        instance = client.get_instance(vm_id)
        state = gcp_power_state(instance)
        job.vm.power_state = state
        if info.capture_mode == "stop":
            if state != "poweredOff":
                if not job.power_off_source:
                    raise ExportError(f"VM is {state.replace('poweredOn', 'running')}; it must be stopped during "
                                      "the export (or use snapshot mode)")
                job.step = "power_off"
                self._save(job, message=f"Stopping {job.vm.name} in Google Cloud")
                client.stop_instance(vm_id, timeout_s=self.s.gcp_stop_timeout_s,
                                     on_wait=lambda: self._check_cancel(job))
                job.power_off_result = "stopped"
                job.vm.power_state = "poweredOff"
                self._save(job, message=f"{job.vm.name} stopped")
            elif job.power_off_source:
                job.power_off_result = "already_off"
        else:
            job.power_off_result = "snapshotted"

        job.step = "export_access"
        self._save(job, message=f"Exporting disks of {job.vm.name} to Cloud Storage")
        export = GcpDiskExport(
            client, job.id, info,
            snapshot_timeout_s=self.s.gcp_snapshot_timeout_s,
            export_timeout_s=self.s.gcp_export_timeout_s,
            save=lambda: self.store.put(job),
            check_cancel=lambda: self._check_cancel(job),
        )
        with export:
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    self._copy_gcp_disk(job, disk, export, client)
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0
        job.step = "export_released"
        self._save(job, message="GCS export objects, Daisy scratch and snapshots deleted")

        self._check_cancel(job)
        self._guest_fixup(job)

        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _copy_gcp_disk(self, job: Job, disk: DiskState, export: GcpDiskExport, client) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        bucket = job.gcp.export_bucket if job.gcp else ""
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Copying {label} from GCS (attempt {attempt}/{self.s.disk_retry_attempts})")
            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)
            obj = export.gcs_object(disk.index)
            try:
                writer.ensure_size(disk.capacity_bytes)
                if obj.endswith(".tar.gz"):
                    total = disk.capacity_bytes
                    disk.stream_bytes = total
                    self._save(
                        job,
                        message=f"Copying {label} to {disk.device}: streaming disk.raw from gs://{bucket}/{obj} "
                                f"({total:,} bytes; unused zeros are not written)",
                    )
                    stats = copy_from_gcs_export_tarball(
                        client, bucket, obj, writer, expected_bytes=total,
                        check_cancel=lambda: self._check_cancel(job),
                        on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                    )
                else:
                    total = object_size(client, bucket, obj)
                    if total != disk.capacity_bytes:
                        raise GcsCopyError(f"raw export size {total} does not match disk size {disk.capacity_bytes}")
                    disk.stream_bytes = total
                    self._save(job, message=f"Copying {label} to {disk.device}: {total:,} bytes from gs://{bucket}/{obj}")
                    stats = copy_parallel_ranges(
                        client, bucket, obj, writer, total_bytes=total,
                        chunk_bytes=self.s.gcp_range_chunk_bytes, workers=self.s.gcp_range_workers,
                        check_cancel=lambda: self._check_cancel(job),
                        on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                    )
                self._record_cloud_progress(job, disk, stats.bytes_received, stats.bytes_written,
                                            stats.chunks_written, meter.rate(), written_before)
                disk.percent = 100
                disk.status = DiskStatus.COPIED
                skipped = stats.bytes_received - stats.bytes_written
                if skipped > 0:
                    self._save(
                        job,
                        message=f"{label} copied: wrote {stats.bytes_written:,} of {stats.bytes_received:,} bytes "
                                f"(skipped {skipped:,} zero bytes)",
                    )
                else:
                    self._save(job, message=f"{label} copied ({stats.bytes_received:,} bytes)")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, GcsCopyError, GcpError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")

    def _run_aws(self, job: Job) -> None:
        session = self._sessions[job.id]
        if session.aws is None:
            raise ExportError("the session that created the job has no AWS login")
        client = session.aws.client
        info = job.aws
        if info is None:
            raise ExportError("job has no AWS source information")

        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        self._save(job, JobPhase.EXPORTING, "Checking the source instance in Amazon EC2")
        inst = client.get_instance(info.instance_id)
        state = aws_power_state(inst)
        job.vm.power_state = state
        if info.capture_mode == "stop":
            if state != "poweredOff":
                if not job.power_off_source:
                    raise ExportError(f"instance is {state.replace('poweredOn', 'running')}; it must be stopped "
                                      "during the export (or use snapshot mode)")
                job.step = "power_off"
                self._save(job, message=f"Stopping {job.vm.name} in EC2")
                client.stop_instance(info.instance_id, timeout_s=self.s.aws_stop_timeout_s,
                                     on_wait=lambda: self._check_cancel(job))
                job.power_off_result = "stopped"
                job.vm.power_state = "poweredOff"
                self._save(job, message=f"{job.vm.name} stopped")
            elif job.power_off_source:
                job.power_off_result = "already_off"
        else:
            job.power_off_result = "snapshotted"

        job.step = "export_access"
        self._save(job, message=f"Snapshotting the EBS volumes of {job.vm.name}")
        export = AwsDiskExport(client, job.id, info, snapshot_timeout_s=self.s.aws_snapshot_timeout_s,
                               save=lambda: self.store.put(job),
                               check_cancel=lambda: self._check_cancel(job))
        with export:
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    self._copy_aws_disk(job, disk, export, client)
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0
        job.step = "export_released"
        self._save(job, message="EBS snapshots deleted")

        self._check_cancel(job)
        self._guest_fixup(job)
        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _copy_aws_disk(self, job: Job, disk: DiskState, export: AwsDiskExport, client) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Listing snapshot blocks of {label} "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")
            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)
            try:
                writer.ensure_size(disk.capacity_bytes)
                snap = export.snapshot_id(disk.index)
                block_size, blocks = list_blocks(client, snap, disk.capacity_bytes)
                allocated = sum(min(block_size, disk.capacity_bytes - i * block_size) for i, _ in blocks)
                disk.stream_bytes = allocated or None
                self._save(job, message=f"Copying {label} to {disk.device}: {allocated:,} of "
                                        f"{disk.capacity_bytes:,} bytes in {len(blocks)} block(s)")
                stats = copy_blocks(
                    client, snap, blocks, block_size, writer, disk.capacity_bytes,
                    workers=self.s.aws_range_workers,
                    check_cancel=lambda: self._check_cancel(job),
                    on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                )
                self._record_cloud_progress(job, disk, stats.bytes_received, stats.bytes_written,
                                            stats.chunks_written, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({stats.bytes_received:,} bytes in "
                                        f"{stats.chunks_written:,} block(s)"
                                        + (f", {stats.retries} retried" if stats.retries else "") + ")")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, EbsCopyError, AwsError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")

    # ------------------------------------------------------------------ OLVM
    def _run_olvm(self, job: Job) -> None:
        session = self._sessions[job.id]
        if session.olvm is None:
            raise ExportError("the session that created the job has no OLVM login")
        client = session.olvm.client
        info = job.olvm
        if info is None:
            raise ExportError("job has no OLVM source information")

        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        self._save(job, JobPhase.EXPORTING, "Checking the source VM in OLVM")
        vm_id = job.vm.moid
        vm = client.get_vm(vm_id)
        state = olvm_power_state(vm)
        job.vm.power_state = state
        if state != "poweredOff":
            if not job.power_off_source:
                raise ExportError(f"VM is {state}; it must be shut down during the export")
            job.step = "power_off"
            self._save(job, message=f"Shutting down {job.vm.name} in OLVM")
            result = olvm_shut_down(client, vm_id, self.s.olvm_shutdown_timeout_s,
                                    on_wait=lambda: self._check_cancel(job), sleep=time.sleep)
            job.power_off_result = result
            job.vm.power_state = "poweredOff"
            self._save(job, message=f"{job.vm.name} powered off ({result.replace('_', ' ')})")
        else:
            job.power_off_result = "already_off"

        job.step = "export_access"
        self._save(job, message=f"Opening image transfers for {job.vm.name}")
        export = OlvmDiskExport(
            client, inactivity_timeout_s=self.s.olvm_transfer_inactivity_s,
            ready_timeout_s=self.s.olvm_transfer_timeout_s,
            direct_from_host=job.target.olvm_direct_from_host,
            check_cancel=lambda: self._check_cancel(job),
        )
        with export:
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    self._copy_olvm_disk(job, disk, export, client)
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0
        job.step = "export_released"
        self._save(job, message="Image transfers closed")

        self._check_cancel(job)
        self._guest_fixup(job)

        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _copy_olvm_disk(self, job: Job, disk: DiskState, export: OlvmDiskExport, client) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        info = job.olvm
        if info is None or disk.index >= len(info.disk_ids):
            raise ExportError(f"{label} has no OLVM disk id")
        disk_id = info.disk_ids[disk.index]
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Opening an image transfer for {label} "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")
            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)
            try:
                writer.ensure_size(disk.capacity_bytes)
                transfer = export.open(disk_id)
                host = urlsplit(transfer.url).hostname or ""
                if host:
                    job.nfc_host = host
                via = "KVM host" if job.target.olvm_direct_from_host else "OLVM manager"
                extents = client.image_extents(transfer.url, transfer.ticket)
                ranges = allocated_ranges(extents, disk.capacity_bytes)
                allocated = sum(length for _, length in ranges)
                disk.stream_bytes = allocated or None
                self._save(job, message=f"Copying {label} via {host or transfer.url} ({via}) to {disk.device}: "
                                        f"{allocated:,} of {disk.capacity_bytes:,} bytes allocated "
                                        f"in {len(ranges)} range(s)")

                def fetch(off: int, length: int, current=transfer) -> bytes:
                    try:
                        return client.read_image(current.url, current.ticket, off, length)
                    except OlvmAuthError as exc:
                        raise ImageioAuthExpired(str(exc)) from exc

                def refresh(current=transfer) -> None:
                    export.refresh(current)

                stats = copy_extents(
                    fetch, ranges, writer,
                    chunk_bytes=self.s.olvm_range_chunk_bytes, workers=self.s.olvm_range_workers,
                    retries=self.s.disk_retry_attempts,
                    check_cancel=lambda: self._check_cancel(job),
                    on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                    refresh=refresh,
                )
                export.finish(transfer)
                self._record_cloud_progress(job, disk, stats.bytes_received, stats.bytes_written,
                                            stats.chunks_written, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({stats.bytes_received:,} bytes in "
                                        f"{stats.chunks_written:,} range request(s)"
                                        + (f", {stats.retries} retried" if stats.retries else "") + ")")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, ImageioCopyError, OlvmError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")

    # ------------------------------------------------------------------ Hyper-V
    def _run_hyperv(self, job: Job) -> None:
        session = self._sessions[job.id]
        if session.hyperv is None:
            raise ExportError("the session that created the job has no Hyper-V login")
        client = session.hyperv.client
        info = job.hyperv
        if info is None:
            raise ExportError("job has no Hyper-V source information")

        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        self._save(job, JobPhase.EXPORTING, "Checking the source VM in Hyper-V")
        vm_id = job.vm.moid
        vm = client.get_vm(vm_id)
        state = hyperv_power_state(vm)
        job.vm.power_state = state
        if state != "poweredOff":
            if not job.power_off_source:
                raise ExportError(f"VM is {state}; it must be shut down during the export")
            job.step = "power_off"
            self._save(job, message=f"Shutting down {job.vm.name} in Hyper-V")
            result = hyperv_shut_down(client, vm_id, self.s.hyperv_shutdown_timeout_s,
                                      on_wait=lambda: self._check_cancel(job), sleep=time.sleep)
            job.power_off_result = result
            job.vm.power_state = "poweredOff"
            self._save(job, message=f"{job.vm.name} powered off ({result.replace('_', ' ')})")
        else:
            job.power_off_result = "already_off"

        job.step = "export_access"
        job.nfc_host = session.hyperv.hostname
        self._save(job, message=f"Reading disks from {session.hyperv.hostname} over SMB")
        job.transfer.started_at = job.transfer.started_at or utcnow()
        job.transfer.percent = 0
        self.store.put(job)
        try:
            for disk in job.disks:
                if disk.status == DiskStatus.COPIED:
                    continue
                self._check_cancel(job)
                self._copy_hyperv_disk(job, disk, session.hyperv)
            job.transfer.percent = 100
        finally:
            job.transfer.finished_at = utcnow()
            job.transfer.throughput_bps = 0.0
        job.step = "export_released"
        self._save(job, message="Disk files closed")

        self._check_cancel(job)
        self._guest_fixup(job)

        self._save(job, JobPhase.FINALIZING, "Attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _copy_hyperv_disk(self, job: Job, disk: DiskState, session) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        info = job.hyperv
        if info is None or disk.index >= len(info.disks):
            raise ExportError(f"{label} has no Hyper-V disk path")
        chain = info.disks[disk.index]
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Opening {label} over SMB "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")
            readers = []
            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)
            try:
                writer.ensure_size(disk.capacity_bytes)
                for path in chain:
                    readers.append(session.open_disk(to_unc(session.hostname, path)))
                image = open_chain(readers)
                ranges = [(start, length) for start, length in image.allocated() if length > 0]
                allocated = sum(length for _, length in ranges)
                disk.stream_bytes = allocated or None
                host = session.hostname
                self._save(job, message=f"Copying {label} via SMB from {host} to {disk.device}: "
                                        f"{allocated:,} of {disk.capacity_bytes:,} bytes allocated "
                                        f"in {len(ranges)} range(s)")

                def fetch(off: int, length: int, current=image) -> bytes:
                    return current.read(off, length)

                stats = copy_extents(
                    fetch, ranges, writer,
                    chunk_bytes=self.s.hyperv_range_chunk_bytes, workers=self.s.hyperv_range_workers,
                    retries=self.s.disk_retry_attempts,
                    check_cancel=lambda: self._check_cancel(job),
                    on_progress=self._cloud_progress_callback(job, disk, meter, written_before),
                )
                self._record_cloud_progress(job, disk, stats.bytes_received, stats.bytes_written,
                                            stats.chunks_written, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({stats.bytes_received:,} bytes in "
                                        f"{stats.chunks_written:,} range request(s)"
                                        + (f", {stats.retries} retried" if stats.retries else "") + ")")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, ImageError, ImageioCopyError, HypervError, OSError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
                for reader in readers:
                    reader.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")

    def _guest_fixup(self, job: Job) -> None:
        want_initramfs, want_network = job.target.rebuild_initramfs, job.target.fix_network
        want_azure = job.kind == "azure" and job.target.azure_cleanup
        want_gcp = job.kind == "gcp" and job.target.gcp_cleanup
        want_aws = job.kind == "aws" and job.target.aws_cleanup
        disabled = GuestFixup(status="skipped", detail="disabled for this job")

        def all_steps(fx: GuestFixup) -> None:
            job.guest_fixup = fx if want_initramfs else disabled
            job.network_fixup = fx if want_network else disabled
            job.azure_fixup = (fx if want_azure else disabled) if job.kind == "azure" else None
            job.gcp_fixup = (fx if want_gcp else disabled) if job.kind == "gcp" else None
            job.aws_fixup = (fx if want_aws else disabled) if job.kind == "aws" else None

        if job.is_windows:
            job.guest_fixup = GuestFixup(status="skipped", detail="Windows guest (VirtIO drivers are installed inside "
                                                                  "Windows, see the note on the export page)")
            job.network_fixup = GuestFixup(status="skipped", detail="Windows guest (the VirtIO network adapter "
                                                                    "uses DHCP by default)")
            job.azure_fixup = None
            job.gcp_fixup = None
            job.aws_fixup = None
            return
        if not want_initramfs and not want_network and not want_azure and not want_gcp and not want_aws:
            all_steps(disabled)
            return
        boot = next((d for d in job.disks if d.is_boot), job.disks[0])
        if not boot.device:
            all_steps(GuestFixup(status="skipped", detail="boot volume device unknown"))
            return
        job.step = "guest_fixup"
        what = " and ".join(filter(None, ["initramfs" if want_initramfs else "", "network" if want_network else "",
                                          "Azure cloud-init" if want_azure else "",
                                          "GCP cloud-init" if want_gcp else "",
                                          "AWS cloud-init" if want_aws else ""]))
        self._save(job, JobPhase.FINALIZING, f"All disks copied; preparing the guest for OCI ({what})")
        try:
            res = self.guest_fixer(boot.device, want_initramfs, want_network, want_azure, want_gcp,
                                   lambda msg: self._save(job, message=f"Guest fix-up: {msg}"), want_aws)
            job.guest_fixup = res.initramfs or disabled
            job.network_fixup = res.network or disabled
            job.azure_fixup = (res.azure_cloud if want_azure else disabled) if job.kind == "azure" else None
            job.gcp_fixup = (res.gcp_cloud if want_gcp else disabled) if job.kind == "gcp" else None
            job.aws_fixup = (res.aws_cloud if want_aws else disabled) if job.kind == "aws" else None
        except Exception as exc:  # noqa: BLE001 - a fix-up problem must not fail the migration
            log.exception("guest fix-up for job %s crashed", job.id)
            fail = GuestFixup(status="failed", detail=describe_error(exc))
            all_steps(fail)
        parts = [f"{name} {fx.status.replace('_', ' ')}: {fx.detail}"
                 for name, fx in (("initramfs", job.guest_fixup), ("network", job.network_fixup),
                                  ("Azure", job.azure_fixup), ("GCP", job.gcp_fixup), ("AWS", job.aws_fixup))
                 if fx is not None and fx is not disabled]
        self._save(job, message="Guest fix-up - " + "; ".join(parts))

    def _resolve_nfc_host(self, job: Job, vm, session: UserSession) -> str:
        """Host substituted for the ``*`` placeholder in the lease URLs.

        Per-job *download directly from ESXi* wins: the host the VM is registered on right now (it may
        have moved since the inspection; the inspected name is the fallback).  Otherwise the deployment
        wide ``HELPER_NFC_HOST_OVERRIDE`` applies, and by default the download is proxied by the vCenter
        this session is logged in to.
        """
        if job.target.nfc_direct_to_esxi:
            host = esxi_host_name(vm) or job.vm.host_name
            if not host:
                raise ExportError("direct ESXi download requested but vCenter reports no host for the VM")
            return host
        return self.s.nfc_host_override or session.vc.host.strip("[]")

    @staticmethod
    def _record_progress(job: Job, disk: DiskState, export: NfcExport, received: int, stats: DecodeStats,
                         rate_bps: float, written_before: int) -> None:
        """Copy the live counters of the disk being copied into the job record (what the UI polls)."""
        disk.bytes_received = received
        disk.bytes_written = stats.bytes_written
        disk.grains_written = stats.grains_written
        disk.throughput_bps = rate_bps
        total = disk.stream_bytes or disk.capacity_bytes
        disk.percent = max(0, min(99, int(received * 100 / total))) if total else 0
        job.transfer.bytes_written = written_before + disk.bytes_written
        job.transfer.throughput_bps = rate_bps
        job.transfer.percent = export.percent  # identical to the figure sent to the NFC lease / vCenter task

    def _export_disk(self, job: Job, disk: DiskState, export: NfcExport, url: str) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Copying {label} to {disk.device} "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")

            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)  # other disks' share
            pipeline: Optional[PipelinedDecoder] = None
            try:
                writer.ensure_size(disk.capacity_bytes)
                decoder = StreamOptimizedDecoder(writer.write_at, expected_capacity_bytes=disk.capacity_bytes,
                                                 skip_zero_grains=self.s.skip_zero_grains)
                # optional: inflate + pwrite on a worker thread so the NFC socket is drained meanwhile
                sink: StreamOptimizedDecoder | PipelinedDecoder = decoder
                if job.target.pipelined_decode:
                    pipeline = PipelinedDecoder(decoder, depth=self.s.nfc_pipeline_depth,
                                                name=f"vmdk-decode-{job.id[:8]}-{disk.index}")
                    sink = pipeline
                received = 0
                last_saved = 0
                last_saved_at = time.monotonic()
                for chunk in export.iter_disk(url):
                    self._check_cancel(job)
                    sink.feed(chunk)
                    received += len(chunk)
                    job.transfer.bytes_received += len(chunk)
                    meter.add(len(chunk))
                    now = time.monotonic()
                    if received - last_saved >= PROGRESS_SAVE_BYTES or now - last_saved_at >= PROGRESS_SAVE_SECONDS:
                        last_saved, last_saved_at = received, now
                        self._record_progress(job, disk, export, received, sink.stats, meter.rate(),
                                              written_before)
                        self.store.put(job)
                stats = sink.finish()
                self._record_progress(job, disk, export, received, stats, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({received:,} bytes received, "
                                        f"{stats.grains_written:,} grains written)")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, VmdkFormatError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                if pipeline is not None:
                    pipeline.abort()  # no-op after a clean finish; stops the worker before the fd goes away
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")
