"""Custom *seed* images.

OCI derives an instance's firmware (BIOS / UEFI_64) and device model from the image
it is launched from.  Platform images do not let us pick those freely, so for each
(firmware, OS) combination we import a tiny placeholder VMDK as a custom image
(``launchMode`` PARAVIRTUALIZED or EMULATED) and pin its capability schema: firmware
fixed, all boot volume / NIC types allowed.  Instances launched from the seed image
accept explicit ``LaunchOptions`` and, for Windows, ``licensingConfigs``.
The seed's boot volume content is irrelevant: it is overwritten by the block copy.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from helper_app.branding import SEED_DISPLAY_PREFIX, TAG_LAUNCH_MODE, TAG_SEED
from helper_app.config import Settings
from helper_app.disk.vmdk_stream import encode_empty_disk
from helper_app.models import LaunchOptionsSpec
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.image_import import (
    ProgressCallback,
    apply_capability_schema,
    ensure_data_volume_types,
    import_failure_detail,
    import_launch_mode,
    wait_import,
)
from helper_app.oci.mapping import (
    SEED_TAG_DEFAULTS,
    WINDOWS_CLIENT_VERSIONS,
    OsMetadata,
    seed_image_tags,
)

log = logging.getLogger(__name__)

SEED_TAG = TAG_SEED

__all__ = ["SEED_TAG", "ProgressCallback", "SeedImageService", "import_launch_mode"]


class SeedImageService:
    def __init__(self, clients: OciClients, settings: Settings):
        self.c = clients
        self.s = settings

    @property
    def seed_compartment(self) -> str:
        """Configured seed compartment, else the helper's own (discovered from the instance metadata)."""
        return self.s.seed_compartment_id or self.c.identity_info.compartment_id

    # ------------------------------------------------------------------ public
    def get_or_create(self, os_meta: OsMetadata, firmware: str, launch_options: LaunchOptionsSpec,
                      on_progress: Optional[ProgressCallback] = None) -> str:
        tags = seed_image_tags(os_meta, firmware, launch_options.secure_boot)
        launch_mode = import_launch_mode(launch_options)
        existing = self.find(tags, launch_mode)
        if existing is not None:
            log.info("reusing seed image %s (%s, launch mode %s)", existing.id, existing.display_name, launch_mode)
            ensure_data_volume_types(self.c, self.seed_compartment, existing.id, launch_options)
            return existing.id
        return self.create(os_meta, firmware, launch_options, tags, on_progress)

    def find(self, tags: dict[str, str], launch_mode: Optional[str] = None) -> Optional[Any]:
        """Seed image carrying ``tags`` whose import launch mode matches.  The mode matters: launching a
        paravirtualized instance from an EMULATED image fails with "Mixing paravirtualized and emulated
        volumes in the same VM is not supported" (and vice versa), so both variants may coexist."""
        import oci

        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images,
            compartment_id=self.seed_compartment,
            lifecycle_state="AVAILABLE",
        ).data
        for img in images:
            ft = img.freeform_tags or {}
            if not all(ft.get(k, SEED_TAG_DEFAULTS.get(k)) == v for k, v in tags.items()):
                continue
            if launch_mode and getattr(img, "launch_mode", None) not in (None, launch_mode):
                continue
            return img
        return None

    def create(
        self, os_meta: OsMetadata, firmware: str, launch_options: LaunchOptionsSpec, tags: dict[str, str],
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        import oci.core.models as M
        from oci.object_storage.models import CreateBucketDetails

        namespace = self.c.object_storage.get_namespace().data
        self._ensure_bucket(namespace, CreateBucketDetails)

        launch_mode = import_launch_mode(launch_options)
        display = f"{SEED_DISPLAY_PREFIX}-{firmware.lower()}-{os_meta.slug}"
        if launch_options.secure_boot:
            display += "-secureboot"
        if launch_mode == "EMULATED":
            display += "-emulated"
        object_name = f"{display}-{uuid.uuid4().hex[:8]}.vmdk"
        payload = encode_empty_disk(self.s.seed_disk_size_gb * 1024**3)
        log.info("uploading %d byte placeholder VMDK to %s/%s", len(payload), self.s.seed_bucket, object_name)
        self.c.object_storage.put_object(namespace, self.s.seed_bucket, object_name, payload)

        # Windows client editions: CreateImage refuses "Windows10"/"Windows11" ("Invalid operatingSystemVersion
        # ... not supported"), yet UpdateImage accepts exactly those.  So import without OS metadata and set
        # Windows / Windows1x afterwards, as Oracle's own Windows 10/11 import procedure does.
        client_edition = os_meta.is_windows and os_meta.operating_system_version in WINDOWS_CLIENT_VERSIONS
        source = M.ImageSourceViaObjectStorageTupleDetails(
            source_type="objectStorageTuple",
            namespace_name=namespace,
            bucket_name=self.s.seed_bucket,
            object_name=object_name,
            source_image_type="VMDK",
        )
        if not client_edition:
            source.operating_system = os_meta.operating_system
            source.operating_system_version = os_meta.operating_system_version
        try:
            details = M.CreateImageDetails(
                compartment_id=self.seed_compartment,
                display_name=display,
                launch_mode=launch_mode,
                freeform_tags={**tags, TAG_LAUNCH_MODE: launch_mode},
                image_source_details=source,
            )
            resp = self.c.compute.create_image(details)
            image = resp.data
            work_request_id = (getattr(resp, "headers", None) or {}).get("opc-work-request-id", "")
            log.info("importing seed image %s (%s), work request %s", image.id, display, work_request_id or "-")
            if on_progress:
                on_progress(0, f"Importing seed image {display}")
            try:
                wait_import(self.c, self.s, image.id, work_request_id, display, on_progress, what="seed image")
            except OciError as exc:
                # OCI deletes an image whose import failed; the reason only exists on the work request
                raise OciError(f"{exc}; {import_failure_detail(self.c, work_request_id, self.s.seed_bucket)}") from exc
            if on_progress:
                on_progress(100, f"Seed image {display} imported; applying capability schema")
            if client_edition:
                log.info("registering seed image %s as %s / %s", image.id, os_meta.operating_system,
                         os_meta.operating_system_version)
                self.c.compute.update_image(image.id, M.UpdateImageDetails(
                    operating_system=os_meta.operating_system,
                    operating_system_version=os_meta.operating_system_version))
            apply_capability_schema(self.c, self.seed_compartment, image.id, firmware, launch_options, display, tags,
                                    launch_mode, consistent_naming=not os_meta.is_windows)
            return image.id
        finally:
            try:
                self.c.object_storage.delete_object(namespace, self.s.seed_bucket, object_name)
            except Exception as exc:  # pragma: no cover
                log.warning("could not delete seed object %s: %s", object_name, exc)

    def cleanup(self) -> list[str]:
        """Delete every seed image in the seed compartment.  Returns deleted image ids."""
        import oci

        deleted: list[str] = []
        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images, compartment_id=self.seed_compartment
        ).data
        for img in images:
            if (img.freeform_tags or {}).get(SEED_TAG) == "true" and img.lifecycle_state != "DELETED":
                self.c.compute.delete_image(img.id)
                deleted.append(img.id)
        return deleted

    # ----------------------------------------------------------------- private
    def _ensure_bucket(self, namespace: str, create_bucket_details_cls) -> None:
        import oci

        try:
            self.c.object_storage.get_bucket(namespace, self.s.seed_bucket)
            return
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
        self.c.object_storage.create_bucket(
            namespace,
            create_bucket_details_cls(name=self.s.seed_bucket, compartment_id=self.seed_compartment,
                                      public_access_type="NoPublicAccess"),
        )

