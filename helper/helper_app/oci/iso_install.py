"""Create an OCI instance that boots an installer ISO.

The ISO in Object Storage is imported as a custom image with source image type ``VMDK`` (documented for
disk images; OCI recognises ISO content and treats the image as boot media, which is not documented but
works).  An instance launched from such an image boots the ISO as installation media and gets a
blank boot volume of the requested size to install onto; after the installation the instance boots from
the boot volume.  Nothing is copied by the helper: the job ends in ``INSTALLING`` and hands the user the
remote console to run the installer; *Installation finished* completes the job.

ISO images are reused across jobs: they are tagged with the ISO object (and its ETag), the firmware and
the device model, so a second instance from the same ISO skips the import.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Callable

from helper_app.branding import (
    ISO_DISPLAY_PREFIX,
    ISO_ETAG_TAG,
    ISO_SOURCE_TAG,
    ISO_TAG,
    TAG_FIRMWARE,
    TAG_JOB,
    TAG_LAUNCH_MODE,
    TAG_OS,
    TAG_SECURE_BOOT,
    TAG_SOURCE_DETAILS,
    TAG_SOURCE_ISO,
)
from helper_app.config import Settings
from helper_app.models import (
    BootVolumeType,
    IsoSpec,
    Job,
    JobPhase,
    LaunchOptionsSpec,
    NetworkType,
    OciTarget,
    WindowsLicenseType,
)
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.image_import import (
    ProgressCallback,
    apply_capability_schema,
    ensure_data_volume_types,
    ensure_shape_compatible,
    import_failure_detail,
    import_launch_mode,
    wait_import,
)
from helper_app.oci.launch import free_hostname_label, secure_boot_platform_config
from helper_app.oci.mapping import WINDOWS_CLIENT_VERSIONS, is_bare_metal_shape, remote_data_volume_type_for

log = logging.getLogger(__name__)

TAG_VALUE_MAX = 256

STEP_ISO_IMAGE = "iso_image"
STEP_LAUNCH = "launch_instance"

INSTALL_INSTRUCTIONS = (
    "The instance is running and boots the installer from the ISO. Open the remote console, install the "
    "operating system onto the boot volume, then reboot; the instance boots from the boot volume once the "
    "installation is complete. Click 'Installation finished' when you are done."
)


def iso_launch_options(iso: IsoSpec, target: OciTarget) -> LaunchOptionsSpec:
    """Device model of the instance (and of its image): paravirtualized (virtio) unless *Maximum
    compatibility* or an explicit override picks emulated devices.  Installers without virtio drivers
    (Windows Setup) need IDE + E1000 to see their disk and network."""
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
        firmware=iso.firmware,
        boot_volume_type=boot_type,
        network_type=net_type,
        remote_data_volume_type=remote_data_volume_type_for(boot_type),
        is_consistent_volume_naming_enabled=not iso.is_windows,
        secure_boot=bool(iso.secure_boot) and iso.firmware == "UEFI_64",
    )


def iso_image_tags(iso: IsoSpec, lo: LaunchOptionsSpec) -> dict[str, str]:
    """Identity of an ISO image: the exact object (name + ETag), firmware, Secure Boot and device class.
    The capability schema pins these, so a differing request needs its own image."""
    return {
        ISO_TAG: "true",
        ISO_SOURCE_TAG: iso.key[-TAG_VALUE_MAX:],
        ISO_ETAG_TAG: (iso.etag or "-")[:TAG_VALUE_MAX],
        TAG_FIRMWARE: lo.firmware,
        TAG_SECURE_BOOT: "true" if lo.secure_boot else "false",
        TAG_LAUNCH_MODE: import_launch_mode(lo),
        TAG_OS: _slug(f"{iso.operating_system}-{iso.operating_system_version}"),
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "custom"


def _display_name(iso: IsoSpec, lo: LaunchOptionsSpec) -> str:
    base = _slug(re.sub(r"\.iso$", "", iso.object_name.rsplit("/", 1)[-1], flags=re.I))[:40]
    return f"{ISO_DISPLAY_PREFIX}-{base}-{lo.firmware.lower()}-{import_launch_mode(lo).lower()}-{uuid.uuid4().hex[:6]}"


class IsoInstaller:
    """The steps of an ISO job: find/import the image, launch the instance, wait for the user."""

    def __init__(self, clients: OciClients, settings: Settings, save: Callable[[Job], Job]):
        self.c = clients
        self.s = settings
        self.save = save

    @property
    def image_compartment(self) -> str:
        return self.s.iso_image_compartment_id or self.s.seed_compartment_id or self.c.identity_info.compartment_id

    # ------------------------------------------------------------------ helpers
    def _step(self, job: Job, step: str, message: str = "", check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()
        job.step = step
        job.step_percent = None
        job.message = message or step
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)

    def _step_progress(self, job: Job, percent: int, message: str, check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()
        job.step_percent = max(0, min(100, int(percent)))
        job.message = message
        log.info("job %s: %s %s%% %s", job.id, job.step, job.step_percent, message)
        self.save(job)

    # ------------------------------------------------------------------ run
    def run(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        """Import the ISO image (or reuse one) and launch the instance; leaves the job in ``INSTALLING``."""
        import oci.core.models as M

        if job.iso is None:
            raise OciError("job has no ISO source")
        iso, target = job.iso, job.target
        if target.availability_domain != self.c.identity_info.availability_domain:
            raise OciError(
                f"target availability domain {target.availability_domain} differs from the migration tool VM's "
                f"{self.c.identity_info.availability_domain}"
            )
        job.phase = JobPhase.PROVISIONING
        lo = iso_launch_options(iso, target)
        job.launch_options = lo
        shape = target.shape or self.s.default_shape
        bare_metal = is_bare_metal_shape(shape)  # fixed cores and memory: no shape config
        ocpus = float(target.ocpus or 1)
        memory_gb = float(target.memory_gb or max(1.0, ocpus * 8))
        sizing = "bare metal, fixed size" if bare_metal else f"{ocpus:g} OCPU, {memory_gb:g} GB"
        # Secure Boot -> shielded instance; decided first so an unsuitable shape fails before anything exists
        platform_config = (secure_boot_platform_config(shape, iso.is_windows, what="Secure Boot was requested")
                           if lo.secure_boot else None)

        # 1. custom image from the ISO
        if not job.iso_image_id:
            self._step(job, STEP_ISO_IMAGE, f"Looking for an image of {iso.object_name} ({lo.firmware}, "
                                           f"{import_launch_mode(lo).lower()})", check_cancel)
            job.iso_image_id = self.get_or_create_image(
                iso, lo, on_progress=lambda pct, text: self._step_progress(job, pct, text, check_cancel))
            job.step_percent = None
            self.save(job)

        # 2. launch the instance: the ISO image as boot media, a blank boot volume of the requested size
        if not job.instance_id:
            display = target.display_name or iso.object_name.rsplit("/", 1)[-1]
            # the image (new or reused) must list the shape as compatible, or OCI refuses the launch with
            # "Shape X is not valid for image Y"; the default list of an imported image has no BM shapes
            self._step(job, STEP_LAUNCH, f"Checking that image allows shape {shape}", check_cancel)
            if ensure_shape_compatible(self.c, job.iso_image_id, shape):
                log.info("job %s: shape %s added to image %s", job.id, shape, job.iso_image_id)
            shielded = ""
            if platform_config is not None:
                shielded = ", shielded: Secure Boot"
                if platform_config.is_measured_boot_enabled:
                    shielded += " + Measured Boot + TPM"
            self._step(job, STEP_LAUNCH, f"Launching {display} ({shape}, {sizing}, "
                                        f"{iso.boot_disk_gb} GB boot volume, firmware {lo.firmware}{shielded})",
                       check_cancel)
            details = M.LaunchInstanceDetails(
                availability_domain=target.availability_domain,
                compartment_id=target.compartment_id,
                display_name=display,
                shape=shape,
                shape_config=None if bare_metal
                else M.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory_gb),
                create_vnic_details=M.CreateVnicDetails(
                    subnet_id=target.subnet_id,
                    assign_public_ip=target.assign_public_ip,
                    private_ip=target.private_ip or None,
                    display_name=display,
                    hostname_label=free_hostname_label(self.c, target.subnet_id, display),
                ),
                source_details=M.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=job.iso_image_id,
                    boot_volume_size_in_gbs=iso.boot_disk_gb,
                    boot_volume_vpus_per_gb=target.volume_vpus_per_gb,
                ),
                launch_options=M.LaunchOptions(
                    firmware=lo.firmware,
                    boot_volume_type=lo.boot_volume_type.value,
                    network_type=lo.network_type.value,
                    remote_data_volume_type=lo.remote_data_volume_type,
                ),
                freeform_tags=iso_source_tags(job),
                metadata={},
            )
            if platform_config is not None:
                details.platform_config = platform_config
            if iso.is_windows:
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
                                ["RUNNING"], self.s.launch_timeout_s, what="instance")
            except OciError as exc:
                reason = self.c.work_request_errors(target.compartment_id, job.instance_id)
                raise OciError(f"{exc}; {reason}" if reason else str(exc)) from exc

        # 3. over to the user
        job.phase = JobPhase.INSTALLING
        job.step = "installing"
        job.step_percent = None
        job.message = INSTALL_INSTRUCTIONS
        self.save(job)
        return job

    # ------------------------------------------------------------------ image
    def find_image(self, tags: dict[str, str]) -> str | None:
        """An AVAILABLE image carrying exactly these identity tags, if any."""
        import oci

        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images, compartment_id=self.image_compartment, lifecycle_state="AVAILABLE"
        ).data
        for img in images:
            have = img.freeform_tags or {}
            if all(have.get(k) == v for k, v in tags.items()):
                return img.id
        return None

    def get_or_create_image(self, iso: IsoSpec, lo: LaunchOptionsSpec,
                            on_progress: ProgressCallback | None = None) -> str:
        tags = iso_image_tags(iso, lo)
        existing = self.find_image(tags)
        if existing:
            log.info("reusing ISO image %s for %s", existing, iso.key)
            ensure_data_volume_types(self.c, self.image_compartment, existing, lo)
            return existing
        return self.create_image(iso, lo, tags, on_progress)

    def create_image(self, iso: IsoSpec, lo: LaunchOptionsSpec, tags: dict[str, str],
                     on_progress: ProgressCallback | None = None) -> str:
        import oci.core.models as M

        launch_mode = import_launch_mode(lo)
        display = _display_name(iso, lo)
        # like the seeds: CreateImage only knows the Windows *server* catalog; client editions are set afterwards
        client_edition = iso.is_windows and iso.operating_system_version in WINDOWS_CLIENT_VERSIONS
        os_name = None if client_edition else iso.operating_system
        os_version = None if client_edition else iso.operating_system_version
        log.info("importing ISO %s as image %s (%s, %s, os=%s %s)", iso.key, display, lo.firmware, launch_mode,
                 iso.operating_system, iso.operating_system_version)
        if on_progress:
            on_progress(0, f"Importing ISO {iso.object_name} as image {display}")
        resp = self.c.compute.create_image(
            M.CreateImageDetails(
                compartment_id=self.image_compartment,
                display_name=display,
                launch_mode=launch_mode,
                freeform_tags=tags,
                image_source_details=M.ImageSourceViaObjectStorageTupleDetails(
                    source_type="objectStorageTuple",
                    namespace_name=iso.namespace,
                    bucket_name=iso.bucket,
                    object_name=iso.object_name,
                    # an ISO is imported as VMDK: OCI recognises the ISO content and boots it as install media
                    source_image_type=self.s.iso_source_image_type,
                    operating_system=os_name,
                    operating_system_version=os_version,
                ),
            )
        )
        image = resp.data
        work_request_id = (getattr(resp, "headers", None) or {}).get("opc-work-request-id", "")
        try:
            wait_import(self.c, self.s, image.id, work_request_id, display, on_progress, what="ISO image")
        except OciError as exc:
            detail = import_failure_detail(self.c, work_request_id, iso.bucket, iso.object_name)
            raise OciError(f"{exc}; {detail}") from exc
        if on_progress:
            on_progress(100, f"ISO image {display} imported; applying capability schema")
        if client_edition:
            self.c.compute.update_image(image.id, M.UpdateImageDetails(
                operating_system=iso.operating_system, operating_system_version=iso.operating_system_version))
        apply_capability_schema(self.c, self.image_compartment, image.id, lo.firmware, lo, display, tags,
                                launch_mode, consistent_naming=not iso.is_windows)
        return image.id

    def cleanup_images(self) -> list[str]:
        """Delete every ISO image the helper imported.  Returns the deleted image ids."""
        import oci

        deleted: list[str] = []
        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images, compartment_id=self.image_compartment
        ).data
        for img in images:
            if (img.freeform_tags or {}).get(ISO_TAG) == "true" and img.lifecycle_state != "DELETED":
                self.c.compute.delete_image(img.id)
                deleted.append(img.id)
        return deleted

    # ------------------------------------------------------------------ finish / cleanup
    def finish(self, job: Job) -> Job:
        """The user reports the installation as done."""
        job.phase = JobPhase.COMPLETED
        job.step = "done"
        job.step_percent = None
        job.message = "Installation finished; the instance boots from its boot volume."
        job.error = None
        self.save(job)
        return job

    def cleanup(self, job: Job) -> list[str]:
        """Cancel: terminate the instance (with its boot volume).  The imported image stays for reuse."""
        actions: list[str] = []

        def attempt(desc: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
                actions.append(f"ok: {desc}")
            except Exception as exc:  # noqa: BLE001
                actions.append(f"failed: {desc}: {exc}")

        if job.instance_id:
            attempt(f"terminate instance {job.instance_id}",
                    lambda: self.c.compute.terminate_instance(job.instance_id, preserve_boot_volume=False))
        job.phase = JobPhase.CANCELLED
        job.step = "cancelled"
        job.step_percent = None
        job.message = "; ".join(actions) or "nothing to clean up"
        self.save(job)
        return actions


def iso_source_tags(job: Job) -> dict[str, str]:
    """Freeform tags recording where the instance came from."""
    iso = job.iso
    assert iso is not None
    firmware = "UEFI" if iso.firmware == "UEFI_64" else "BIOS"
    if iso.secure_boot:
        firmware += " Secure Boot"
    details = (f"{iso.operating_system} {iso.operating_system_version}, {firmware}, "
               f"{iso.boot_disk_gb} GB boot volume")
    return {
        TAG_JOB: job.id,
        TAG_SOURCE_ISO: iso.key[-TAG_VALUE_MAX:],
        TAG_SOURCE_DETAILS: details[:TAG_VALUE_MAX],
    }
