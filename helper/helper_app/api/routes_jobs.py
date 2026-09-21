"""Migration jobs: create, list, monitor, cancel, resume finalize."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from helper_app import diagnostics
from helper_app.api import routes_aws_vms, routes_azure_vms, routes_gcp_vms
from helper_app.api.routes_vms import inspect
from helper_app.auth import (
    require_aws_session,
    require_azure_session,
    require_gcp_session,
    require_session,
    require_vcenter_session,
)
from helper_app.jobs.store import utcnow
from helper_app.models import (
    AwsSourceInfo,
    AzureSourceInfo,
    CreateAwsJobRequest,
    CreateAzureJobRequest,
    CreateGcpJobRequest,
    CreateIsoJobRequest,
    CreateJobRequest,
    CreateOvaExportJobRequest,
    CreateOvaJobRequest,
    DiskState,
    GcpSourceInfo,
    InstanceStatus,
    Job,
    JobPhase,
    OciTarget,
    OvaExportSpec,
    VmInspection,
    WindowsLicenseType,
)
from helper_app.oci.clients import describe_error
from helper_app.oci.mapping import (
    OS_VERSION_CHOICES,
    WINDOWS_CLIENT_VERSIONS,
    is_arm_shape,
    is_bare_metal_shape,
    map_guest_os,
    normalize_os_version_for_oci,
    os_version_choices,
    with_os_version,
)
from helper_app.oci.options import PrivateIpError, check_private_ip, primary_vnic_ips
from helper_app.sessions import UserSession

log = logging.getLogger(__name__)


def _require_catalog_os_version(operating_system: str, version: str) -> str:
    """Normalize and validate a guest OS release for ISO/OVA import (OCI image metadata)."""
    norm = normalize_os_version_for_oci(operating_system, version)
    choices = OS_VERSION_CHOICES.get(operating_system)
    if choices and (not norm or norm == "unknown" or norm not in choices):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"select a {operating_system} release OCI knows ({', '.join(choices)})",
        )
    return norm or version

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_session)])


def _get_job(request: Request, job_id: str) -> Job:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return job


@router.get("", response_model=list[Job])
def list_jobs(request: Request, vm_moid: Optional[str] = None):
    return request.app.state.store.list(vm_moid=vm_moid)


async def _check_target(st, target, allow_arm: bool = False) -> None:
    """Validation shared by both job kinds: the helper's AD, the shape's architecture (Ampere only for ISO
    installations, a vSphere guest is x86), a usable fixed private IP."""
    helper_ad = st.clients.identity_info.availability_domain
    if target.availability_domain != helper_ad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"the availability domain must be the migration tool VM's ({helper_ad})")
    shape = target.shape or st.settings.default_shape
    if is_arm_shape(shape) and not allow_arm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{shape} is an Ampere (ARM) shape; an x86 guest needs an x86 shape")
    if target.private_ip:
        # fixed address: must fit the subnet and be free right now (OCI would otherwise fail the launch later)
        try:
            await asyncio.to_thread(check_private_ip, st.clients, target.subnet_id, target.private_ip)
        except PrivateIpError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                f"cannot verify private IP {target.private_ip}: {describe_error(exc)}")


async def _check_guest_os(st, inspection: VmInspection, target, source: str) -> None:
    """Windows license and OS release checks shared by the VMware and Azure jobs."""
    if inspection.vm.is_windows and target.windows_license_type is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a Windows license type must be selected")
    os_meta = map_guest_os(inspection.vm.guest_id, inspection.vm.guest_full_name)
    if not os_meta.version_detected and not target.operating_system_version and os_version_choices(os_meta):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{source} does not report which {os_meta.operating_system} release the guest runs; select the "
            f"OS version ({', '.join(os_version_choices(os_meta))})",
        )
    os_meta = with_os_version(os_meta, target.operating_system_version)
    if (os_meta.operating_system_version in WINDOWS_CLIENT_VERSIONS
            and target.windows_license_type == WindowsLicenseType.OCI_PROVIDED):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "OCI does not provide licenses for Windows 10/11; select Bring your own license")
    await _check_target(st, target)


@router.post("", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_job(body: CreateJobRequest, request: Request,
                     session: UserSession = Depends(require_vcenter_session)):
    st = request.app.state
    inspection = await asyncio.to_thread(inspect, session, body.vm_moid)
    if not inspection.can_export:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(inspection.problems))
    if inspection.needs_power_off and not body.power_off_source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{inspection.vm.name} is powered on; confirm that it may be shut down for the "
                            "migration (power_off_source), or power it off in vCenter first")
    await _check_guest_os(st, inspection, body.target, "vSphere")
    active = st.store.active_for_vm(body.vm_moid)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    info = session.info()
    job = Job(
        id=uuid.uuid4().hex,
        vm=inspection.vm,
        vcenter_host=info.vcenter_host + (f":{info.vcenter_port}" if info.vcenter_port != 443 else ""),
        target=body.target,
        power_off_source=inspection.needs_power_off,
        phase=JobPhase.QUEUED,
        message="Queued" + (" (the VM is shut down right before the disk export)" if inspection.needs_power_off
                            else ""),
        disks=[DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
               for d in inspection.vm.disks],
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit(job.id, session)
    return job


@router.post("/azure", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_azure_job(body: CreateAzureJobRequest, request: Request,
                           session: UserSession = Depends(require_azure_session)):
    """Migrate an Azure VM: its managed disks are exported (after deallocating the VM, or from snapshots
    while it runs) and copied onto OCI volumes like a vSphere VM's."""
    st = request.app.state
    details = await asyncio.to_thread(routes_azure_vms.inspect_details, session, body.vm_id)
    inspection = routes_azure_vms.inspect(session, body.vm_id, body.capture_mode, details)
    if not inspection.can_export:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(inspection.problems))
    if inspection.needs_power_off and not body.power_off_source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{inspection.vm.name} is {inspection.vm.power_state.replace('poweredOn', 'running')}; "
                            "confirm that it may be deallocated for the migration (power_off_source), deallocate "
                            "it in Azure first, or choose snapshot mode")
    await _check_guest_os(st, inspection, body.target, "Azure")
    vm_key = inspection.vm.moid
    active = st.store.active_for_vm(vm_key)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    az = session.azure
    job = Job(
        id=uuid.uuid4().hex,
        kind="azure",
        vm=inspection.vm,
        azure=AzureSourceInfo(
            tenant_id=az.tenant_id, subscription_id=details.subscription_id,
            subscription_name=az.subscription_name(details.subscription_id), resource_group=details.resource_group,
            location=details.location, vm_size=details.vm_size, capture_mode=body.capture_mode,
            disk_ids=details.disk_ids,
        ),
        target=body.target,
        power_off_source=inspection.needs_power_off,
        phase=JobPhase.QUEUED,
        message="Queued" + (" (the VM is deallocated right before the disk export)" if inspection.needs_power_off
                            else (" (the disks are snapshotted right before the export)"
                                  if body.capture_mode == "snapshot" else "")),
        disks=[DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
               for d in inspection.vm.disks],
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit(job.id, session)
    return job


@router.post("/gcp", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_gcp_job(body: CreateGcpJobRequest, request: Request,
                         session: UserSession = Depends(require_gcp_session)):
    st = request.app.state
    details = await asyncio.to_thread(routes_gcp_vms.inspect_details, session, body.vm_id)
    inspection = routes_gcp_vms.inspect(session, body.vm_id, body.capture_mode, details)
    if not inspection.can_export:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(inspection.problems))
    if inspection.needs_power_off and not body.power_off_source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{inspection.vm.name} is {inspection.vm.power_state.replace('poweredOn', 'running')}; "
                            "confirm that it may be stopped for the migration (power_off_source), stop it in GCP "
                            "first, or choose snapshot mode")
    await _check_guest_os(st, inspection, body.target, "Google Cloud")
    vm_key = inspection.vm.moid
    active = st.store.active_for_vm(vm_key)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    gcp_sess = session.gcp
    job_id = uuid.uuid4().hex
    job = Job(
        id=job_id,
        kind="gcp",
        vm=inspection.vm,
        gcp=GcpSourceInfo(
            project_id=details.project_id,
            zone=details.zone,
            machine_type=details.machine_type,
            export_bucket=gcp_sess.export_bucket,
            export_prefix=f"oci-umt/{job_id}",
            capture_mode=body.capture_mode,
            disk_urls=details.disk_urls,
        ),
        target=body.target,
        power_off_source=inspection.needs_power_off,
        phase=JobPhase.QUEUED,
        message="Queued" + (" (the VM is stopped right before the disk export)" if inspection.needs_power_off
                            else (" (the disks are snapshotted right before the export)"
                                  if body.capture_mode == "snapshot" else "")),
        disks=[DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
               for d in inspection.vm.disks],
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit(job.id, session)
    return job


@router.post("/aws", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_aws_job(body: CreateAwsJobRequest, request: Request,
                         session: UserSession = Depends(require_aws_session)):
    """Migrate an EC2 instance: its EBS volumes are snapshotted and copied onto OCI volumes."""
    st = request.app.state
    details = await asyncio.to_thread(routes_aws_vms.inspect_details, session, body.vm_id)
    inspection = routes_aws_vms.inspect(session, body.vm_id, body.capture_mode, details)
    if not inspection.can_export:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(inspection.problems))
    if inspection.needs_power_off and not body.power_off_source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{inspection.vm.name} is {inspection.vm.power_state.replace('poweredOn', 'running')}; "
                            "confirm that it may be stopped for the migration (power_off_source), stop "
                            "it in AWS first, or choose snapshot mode")
    await _check_guest_os(st, inspection, body.target, "Amazon EC2")
    vm_key = inspection.vm.moid
    active = st.store.active_for_vm(vm_key)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    aws_sess = session.aws
    job = Job(
        id=uuid.uuid4().hex,
        kind="aws",
        vm=inspection.vm,
        aws=AwsSourceInfo(
            account_id=aws_sess.account_id,
            region=aws_sess.region,
            instance_id=details.instance_id,
            instance_type=details.instance_type,
            capture_mode=body.capture_mode,
            volume_ids=details.volume_ids,
        ),
        target=body.target,
        power_off_source=inspection.needs_power_off,
        phase=JobPhase.QUEUED,
        message="Queued" + (" (the instance is stopped right before the disk export)" if inspection.needs_power_off
                            else (" (the volumes are snapshotted right before the export)"
                                  if body.capture_mode == "snapshot" else "")),
        disks=[DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
               for d in inspection.vm.disks],
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit(job.id, session)
    return job


@router.post("/iso", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_iso_job(body: CreateIsoJobRequest, request: Request,
                         session: UserSession = Depends(require_session)):
    """Create an instance that boots an installer ISO from Object Storage.  Works from an anonymous session
    (no vCenter involved)."""
    st = request.app.state
    iso, target = body.iso, body.target
    if not iso.object_name.lower().endswith(".iso"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{iso.object_name} is not an .iso object")
    if not (target.display_name or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "an instance name is required")
    os_ver = _require_catalog_os_version(iso.operating_system, iso.operating_system_version)
    if os_ver != iso.operating_system_version:
        iso = iso.model_copy(update={"operating_system_version": os_ver})
    if iso.is_windows:
        if target.windows_license_type is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "a Windows license type must be selected")
        if (iso.operating_system_version in WINDOWS_CLIENT_VERSIONS
                and target.windows_license_type == WindowsLicenseType.OCI_PROVIDED):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "OCI does not provide licenses for Windows 10/11; select Bring your own license")
    shape = target.shape or st.settings.default_shape
    if is_bare_metal_shape(shape):
        target.ocpus = target.memory_gb = None  # bare metal: cores and memory come with the shape
    elif not target.ocpus or not target.memory_gb:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OCPUs and memory are required for a VM shape")
    if is_arm_shape(shape):
        # Ampere: UEFI only, no shielded instances; the ISO has to be an aarch64 build (not checkable here)
        if iso.firmware != "UEFI_64":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{shape} is an Ampere (Arm) shape and boots with UEFI only; select UEFI firmware "
                                "(and an aarch64 installer ISO)")
        if iso.secure_boot:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{shape} is an Ampere (Arm) shape; Secure Boot (shielded instance) is not "
                                "available there")
    await _check_target(st, target, allow_arm=True)
    now = utcnow()
    job = Job(
        id=uuid.uuid4().hex,
        kind="iso",
        iso=iso,
        target=target,
        phase=JobPhase.QUEUED,
        message="Queued",
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit_iso(job.id)
    return job


@router.post("/ova", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_ova_job(body: CreateOvaJobRequest, request: Request,
                         session: UserSession = Depends(require_session)):
    """Import an OVA from Object Storage and launch an instance.  Works from an anonymous session."""
    st = request.app.state
    ova, target = body.ova, body.target
    low = ova.object_name.lower()
    if not (low.endswith(".ova") or low.endswith(".ovf") or low.endswith(".vmdk")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{ova.object_name} must be an .ova, .ovf, or .vmdk object")
    if not (target.display_name or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "an instance name is required")
    os_ver = _require_catalog_os_version(ova.operating_system, ova.operating_system_version)
    if os_ver != ova.operating_system_version:
        ova = ova.model_copy(update={"operating_system_version": os_ver})
    if ova.is_windows:
        if target.windows_license_type is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "a Windows license type must be selected")
        if (ova.operating_system_version in WINDOWS_CLIENT_VERSIONS
                and target.windows_license_type == WindowsLicenseType.OCI_PROVIDED):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "OCI does not provide licenses for Windows 10/11; select Bring your own license")
    shape = target.shape or st.settings.default_shape
    if is_bare_metal_shape(shape):
        target.ocpus = target.memory_gb = None
    elif not target.ocpus or not target.memory_gb:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OCPUs and memory are required for a VM shape")
    if is_arm_shape(shape):
        if ova.firmware != "UEFI_64":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{shape} is an Ampere (Arm) shape and boots with UEFI only")
        if ova.secure_boot:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{shape} is an Ampere (Arm) shape; Secure Boot is not available there")
    await _check_target(st, target, allow_arm=True)
    if not ova.namespace:
        from helper_app.oci.options import object_storage_namespace
        ova = ova.model_copy(update={"namespace": object_storage_namespace(st.clients)})
    now = utcnow()
    job = Job(
        id=uuid.uuid4().hex,
        kind="ova",
        ova=ova,
        target=target,
        phase=JobPhase.QUEUED,
        message="Queued",
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit_ova(job.id)
    return job


@router.post("/ova-export", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_ova_export_job(body: CreateOvaExportJobRequest, request: Request,
                                session: UserSession = Depends(require_session)):
    """Stop an OCI instance, export its volumes as an OVF set, reattach the boot volume, leave STOPPED."""
    st = request.app.state
    helper_id = st.clients.identity_info.instance_id
    if body.instance_id == helper_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot export the migration tool VM")
    try:
        inst = await asyncio.to_thread(lambda: st.clients.compute.get_instance(body.instance_id).data)
    except Exception as exc:  # noqa: BLE001
        code = status.HTTP_404_NOT_FOUND if getattr(exc, "status", None) == 404 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, f"instance {body.instance_id}: {describe_error(exc)}")
    if inst.lifecycle_state not in ("RUNNING", "STOPPED"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"instance {inst.display_name} is {inst.lifecycle_state}; export needs RUNNING or STOPPED",
        )
    source_key = f"ovaexport:{inst.id}"
    active = st.store.active_for_vm(source_key)
    if active:
        raise HTTPException(status.HTTP_409_CONFLICT, f"an export of this instance is already running ({active.id})")

    def bucket_check():
        return st.clients.object_storage.get_bucket(
            body.namespace or st.clients.object_storage.get_namespace().data, body.bucket
        )

    try:
        await asyncio.to_thread(bucket_check)
    except Exception as exc:  # noqa: BLE001
        code = status.HTTP_404_NOT_FOUND if getattr(exc, "status", None) == 404 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, f"bucket {body.bucket}: {describe_error(exc)}")

    from helper_app.oci.options import object_storage_namespace
    from helper_app.oci.ova_export import sanitize_export_name

    namespace = body.namespace or await asyncio.to_thread(object_storage_namespace, st.clients)
    name = inst.display_name or inst.id
    prefix = (body.prefix or "").strip().strip("/") or sanitize_export_name(name)
    cfg = getattr(inst, "shape_config", None)
    lo = getattr(inst, "launch_options", None)
    pc = getattr(inst, "platform_config", None)

    def subnet_of() -> str:
        atts = st.clients.compute.list_vnic_attachments(
            compartment_id=inst.compartment_id, instance_id=inst.id
        ).data
        for att in atts:
            if getattr(att, "lifecycle_state", "") != "ATTACHED" or not getattr(att, "vnic_id", None):
                continue
            vnic = st.clients.network.get_vnic(att.vnic_id).data
            if getattr(vnic, "is_primary", False):
                return getattr(vnic, "subnet_id", "") or ""
        if atts and getattr(atts[0], "vnic_id", None):
            return getattr(st.clients.network.get_vnic(atts[0].vnic_id).data, "subnet_id", "") or ""
        return "unknown"

    subnet_id = await asyncio.to_thread(subnet_of)
    target = OciTarget(
        compartment_id=inst.compartment_id,
        availability_domain=inst.availability_domain,
        subnet_id=subnet_id,
        display_name=name,
        shape=getattr(inst, "shape", None),
        ocpus=getattr(cfg, "ocpus", None) if cfg is not None else None,
        memory_gb=getattr(cfg, "memory_in_gbs", None) if cfg is not None else None,
        start_after_migration=False,
    )
    spec = OvaExportSpec(
        instance_id=inst.id,
        instance_name=name,
        compartment_id=inst.compartment_id,
        namespace=namespace,
        bucket=body.bucket,
        prefix=prefix,
        include_data_volumes=body.include_data_volumes,
        shape=getattr(inst, "shape", None) or "",
        ocpus=getattr(cfg, "ocpus", None) if cfg is not None else None,
        memory_gb=getattr(cfg, "memory_in_gbs", None) if cfg is not None else None,
        firmware=getattr(lo, "firmware", None) if lo is not None else None,
        secure_boot=bool(getattr(pc, "is_secure_boot_enabled", False)) if pc is not None else False,
    )
    now = utcnow()
    job = Job(
        id=uuid.uuid4().hex,
        kind="ovaexport",
        ova_export=spec,
        target=target,
        instance_id=inst.id,
        instance_display_name=name,
        phase=JobPhase.QUEUED,
        message="Queued",
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit_ova_export(job.id)
    return job


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: str, request: Request):
    return _get_job(request, job_id)


@router.get("/{job_id}/instance", response_model=InstanceStatus)
async def job_instance(job_id: str, request: Request):
    """Current OCI lifecycle state of the target instance (the job record only knows what the helper did)."""
    st = request.app.state
    job = _get_job(request, job_id)
    if not job.instance_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no OCI instance associated with this job yet")
    try:
        inst = (await asyncio.to_thread(st.clients.compute.get_instance, job.instance_id)).data
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) == 404:  # terminated instances disappear from the API after a while
            return InstanceStatus(instance_id=job.instance_id, display_name=job.instance_display_name,
                                  lifecycle_state="NOT_FOUND", checked_at=utcnow())
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, describe_error(exc))
    private_ip = public_ip = None
    if inst.lifecycle_state not in ("TERMINATING", "TERMINATED"):
        try:
            private_ip, public_ip = await asyncio.to_thread(primary_vnic_ips, st.clients, inst)
        except Exception as exc:  # noqa: BLE001 - the addresses are informational; the state matters more
            log.debug("cannot read the VNIC of %s: %s", job.instance_id, describe_error(exc))
    return InstanceStatus(instance_id=job.instance_id, display_name=inst.display_name or job.instance_display_name,
                          lifecycle_state=inst.lifecycle_state, private_ip=private_ip, public_ip=public_ip,
                          checked_at=utcnow())


@router.get("/{job_id}/diagnostics", response_class=PlainTextResponse)
async def job_diagnostics(job_id: str, request: Request):
    """Everything needed to analyse the job (record + relevant journal lines) as plain text."""
    st = request.app.state
    job = _get_job(request, job_id)
    return await asyncio.to_thread(diagnostics.collect, job, st.settings, st.clients.identity_info, st.commit,
                                   st.command_runner)


@router.post("/{job_id}/cancel", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(job_id: str, request: Request, session: UserSession = Depends(require_session)):
    st = request.app.state
    job = _get_job(request, job_id)
    if job.phase in (JobPhase.COMPLETED, JobPhase.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is already {job.phase.value}")
    if st.runner.is_running(job.id):
        st.runner.request_cancel(job.id)
        job.message = "Cancellation requested"
        st.store.put(job)
        return job
    # not running (failed or interrupted by a restart): tear down whatever was created.  An Azure job may
    # still hold export SAS / snapshots; the caller's Azure login (if any) is used to release them.
    job.step = "cancel_queued"
    job.message = "Cleaning up OCI resources"
    st.store.put(job)
    cloud = session if (session.azure is not None or session.gcp is not None) else None
    st.runner.cleanup(job.id, cloud)
    return job


@router.post("/{job_id}/finish", response_model=Job)
def finish_installation(job_id: str, request: Request):
    """ISO jobs: the user reports the installation as done; the job completes (the instance stays)."""
    st = request.app.state
    job = _get_job(request, job_id)
    if job.kind != "iso" or job.phase != JobPhase.INSTALLING:
        raise HTTPException(status.HTTP_409_CONFLICT, "only an ISO job in INSTALLING can be finished")
    return st.runner.iso.finish(job)


@router.post("/{job_id}/finalize", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
def resume_finalize(job_id: str, request: Request):
    """Retry the finalize step (attach volumes, start) of a failed job whose disks were all copied."""
    st = request.app.state
    job = _get_job(request, job_id)
    if st.runner.is_running(job.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "job is running")
    if not st.runner.can_resume_finalize(job):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "only a FAILED job with a launched instance and all disks copied can resume finalizing")
    job.step = "finalize_queued"
    job.message = "Resuming finalize"
    st.store.put(job)
    st.runner.resume_finalize(job.id)
    return job
