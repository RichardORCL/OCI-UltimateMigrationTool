"""IsoInstaller against the fake OCI: ISO image import and reuse, launch with a blank boot volume,
INSTALLING hand-over, finish and cancel/cleanup."""

from __future__ import annotations

import pytest

from helper_app.config import Settings
from helper_app.jobs.runner import JobCancelled
from helper_app.jobs.store import JobStore, utcnow
from helper_app.models import BootVolumeType, IsoSpec, Job, JobPhase, NetworkType, OciTarget, WindowsLicenseType
from helper_app.oci.clients import OciError
from helper_app.oci.iso_install import (
    INSTALL_INSTRUCTIONS,
    ISO_ETAG_TAG,
    ISO_SOURCE_TAG,
    ISO_TAG,
    STEP_ISO_IMAGE,
    STEP_LAUNCH,
    IsoInstaller,
    iso_image_tags,
    iso_launch_options,
)

from .fake_oci import FakeOci

AD = "Uocm:EU-FRANKFURT-1-AD-1"
NS = "testnamespace"


def make_iso(**kw) -> IsoSpec:
    base = dict(namespace=NS, bucket="isos", object_name="images/ubuntu-24.04-live-server-amd64.iso",
                size_bytes=3 * 1024**3, etag="etag-ubuntu-1", operating_system="Ubuntu",
                operating_system_version="24.04", firmware="UEFI_64", boot_disk_gb=80)
    base.update(kw)
    return IsoSpec(**base)


def make_target(**kw) -> OciTarget:
    base = dict(compartment_id="ocid1.compartment.oc1..migr", availability_domain=AD,
                subnet_id="ocid1.subnet.oc1..1", display_name="ubuntu-from-iso", shape="VM.Standard.E5.Flex",
                ocpus=2, memory_gb=16)
    base.update(kw)
    return OciTarget(**base)


def make_job(iso: IsoSpec, target: OciTarget, job_id="iso0001") -> Job:
    now = utcnow()
    return Job(id=job_id, kind="iso", iso=iso, target=target, created_at=now, updated_at=now)


@pytest.fixture
def env(tmp_path):
    settings = Settings(device_prefix=str(tmp_path / "dev" / "oraclevd"), db_path=str(tmp_path / "jobs.db"),
                        seed_bucket="oci-umt-seed", launch_timeout_s=5, volume_timeout_s=5,
                        image_import_timeout_s=5)
    fake = FakeOci(settings.device_prefix)
    fake.object_storage.add_object("isos", "images/ubuntu-24.04-live-server-amd64.iso", etag="etag-ubuntu-1")
    fake.object_storage.add_object("isos", "win/SERVER_EVAL_x64FRE_en-us.iso", etag="etag-win-1")
    clients = fake.clients()
    store = JobStore(settings.db_path)
    installer = IsoInstaller(clients, settings, store.put)
    return settings, fake, store, installer


# --------------------------------------------------------------------------- launch options / tags
def test_launch_options_follow_firmware_and_compatibility_mode():
    lo = iso_launch_options(make_iso(), make_target())
    assert (lo.firmware, lo.boot_volume_type, lo.network_type) == ("UEFI_64", BootVolumeType.PARAVIRTUALIZED,
                                                                   NetworkType.PARAVIRTUALIZED)
    assert lo.remote_data_volume_type == "PARAVIRTUALIZED"
    assert lo.is_consistent_volume_naming_enabled is True and lo.secure_boot is False

    # Maximum compatibility: IDE + E1000 (installers without virtio drivers), remote data volumes emulated too
    lo = iso_launch_options(make_iso(firmware="BIOS"), make_target(compatibility_mode=True))
    assert (lo.firmware, lo.boot_volume_type, lo.network_type) == ("BIOS", BootVolumeType.IDE, NetworkType.E1000)
    assert lo.remote_data_volume_type != "PARAVIRTUALIZED"

    # explicit overrides win over the compatibility default
    lo = iso_launch_options(make_iso(), make_target(compatibility_mode=True,
                                                    boot_volume_type_override=BootVolumeType.PARAVIRTUALIZED,
                                                    network_type_override=NetworkType.VFIO))
    assert (lo.boot_volume_type, lo.network_type) == (BootVolumeType.PARAVIRTUALIZED, NetworkType.VFIO)

    # Windows: no consistent device naming (Linux-only /dev/oracleoci paths); Secure Boot needs UEFI
    win = make_iso(operating_system="Windows", operating_system_version="Server 2022 Standard", secure_boot=True)
    lo = iso_launch_options(win, make_target())
    assert lo.is_consistent_volume_naming_enabled is False and lo.secure_boot is True


def test_secure_boot_is_refused_with_bios_firmware():
    with pytest.raises(ValueError, match="Secure Boot requires UEFI_64"):
        make_iso(firmware="BIOS", secure_boot=True)


def test_image_tags_identify_object_firmware_and_device_model():
    iso = make_iso()
    tags = iso_image_tags(iso, iso_launch_options(iso, make_target()))
    assert tags[ISO_TAG] == "true"
    assert tags[ISO_SOURCE_TAG] == f"{NS}/isos/images/ubuntu-24.04-live-server-amd64.iso"
    assert tags[ISO_ETAG_TAG] == "etag-ubuntu-1"
    assert tags["oci-umt-firmware"] == "UEFI_64" and tags["oci-umt-secure-boot"] == "false"
    assert tags["oci-umt-launch-mode"] == "PARAVIRTUALIZED" and tags["oci-umt-os"] == "ubuntu-24-04"
    # the same ISO with emulated devices is a different image
    emulated = iso_image_tags(iso, iso_launch_options(iso, make_target(compatibility_mode=True)))
    assert emulated["oci-umt-launch-mode"] == "EMULATED" and emulated != tags
    # OCI refuses tag keys with periods or spaces
    assert all("." not in k and " " not in k for k in tags)


# --------------------------------------------------------------------------- run
def test_run_imports_iso_launches_with_blank_boot_volume_and_hands_over(env):
    settings, fake, store, installer = env
    job = make_job(make_iso(), make_target())
    store.put(job)
    steps: list[tuple[str, int | None]] = []
    orig_put = store.put

    def spy(j):
        steps.append((j.step, j.step_percent))
        return orig_put(j)

    installer.save = spy
    installer.run(job)

    # 1. the image: imported from the ISO object with source type ISO, tagged, capability schema applied
    assert job.iso_image_id in fake.compute.images
    img = fake.compute.images[job.iso_image_id]
    assert img.source_image_type == "VMDK" and img.launch_mode == "PARAVIRTUALIZED"  # ISOs import as VMDK
    assert (img.namespace_name, img.bucket_name, img.object_name) == (NS, "isos",
                                                                      "images/ubuntu-24.04-live-server-amd64.iso")
    assert (img.operating_system, img.operating_system_version) == ("Ubuntu", "24.04")
    assert img.compartment_id == installer.image_compartment == fake.identity.compartment_id
    assert img.freeform_tags[ISO_TAG] == "true" and img.freeform_tags[ISO_SOURCE_TAG].endswith("amd64.iso")
    assert img.display_name.startswith("oci-umt-iso-ubuntu-24-04-live-server-amd64-uefi_64-paravirtualized-")
    schemas = [s for s in fake.compute.capability_schemas if s.image_id == job.iso_image_id]
    assert len(schemas) == 1
    assert schemas[0].schema_data["Compute.Firmware"].default_value == "UEFI_64"
    assert schemas[0].schema_data["Storage.ConsistentVolumeNaming"].default_value is True
    assert STEP_ISO_IMAGE in [s for s, _ in steps]

    # 2. the instance: the ISO image as source, a blank boot volume of the requested size, our launch options
    assert len(fake.compute.launch_details) == 1
    d = fake.compute.launch_details[0]
    assert d.source_details.image_id == job.iso_image_id
    assert d.source_details.boot_volume_size_in_gbs == 80
    assert d.source_details.boot_volume_vpus_per_gb == 10  # OciTarget default: Balanced
    assert (d.shape, d.shape_config.ocpus, d.shape_config.memory_in_gbs) == ("VM.Standard.E5.Flex", 2.0, 16.0)
    assert d.display_name == "ubuntu-from-iso" and d.create_vnic_details.hostname_label == "ubuntu-from-iso"
    assert d.create_vnic_details.subnet_id == "ocid1.subnet.oc1..1"
    assert (d.launch_options.firmware, d.launch_options.boot_volume_type,
            d.launch_options.network_type) == ("UEFI_64", "PARAVIRTUALIZED", "PARAVIRTUALIZED")
    assert d.licensing_configs is None and getattr(d, "platform_config", None) is None
    assert d.freeform_tags["oci-umt-job"] == job.id
    assert d.freeform_tags["oci-umt-source-iso"] == f"{NS}/isos/images/ubuntu-24.04-live-server-amd64.iso"
    assert "80 GB boot volume" in d.freeform_tags["oci-umt-source-details"]
    assert "UEFI" in d.freeform_tags["oci-umt-source-details"]
    inst = fake.compute.instances[job.instance_id]
    assert inst.lifecycle_state == "RUNNING" and job.instance_display_name == "ubuntu-from-iso"
    bv = [b for b in fake.blockstorage.boot_volumes.values() if b.image_id == job.iso_image_id]
    assert len(bv) == 1 and bv[0].size_in_gbs == 80
    assert STEP_LAUNCH in [s for s, _ in steps]

    # 3. handed over to the user: nothing copied, no volumes created, the job waits in INSTALLING
    assert job.phase == JobPhase.INSTALLING and job.step == "installing"
    assert job.message == INSTALL_INSTRUCTIONS and job.error is None
    assert fake.blockstorage.volumes == {} and fake.compute.vol_attachments == {}
    assert job.launch_options is not None and job.launch_options.firmware == "UEFI_64"
    stored = store.get(job.id)
    assert stored.phase == JobPhase.INSTALLING and stored.iso_image_id == job.iso_image_id
    assert stored.kind == "iso" and stored.vm is None and stored.source_key == f"iso:{NS}/isos/images/ubuntu-24.04-live-server-amd64.iso"
    assert store.list(vm_moid=stored.source_key)[0].id == job.id  # stored in the source column


def test_run_bare_metal_shape_launches_without_shape_config(env):
    """BM shapes have fixed cores and memory: no shapeConfig (OCI rejects one), and Secure Boot uses the
    generic BM platform config (Secure Boot only for Linux: Measured Boot + TPM stay off)."""
    settings, fake, store, installer = env
    job = make_job(make_iso(secure_boot=True), make_target(shape="BM.Standard.E5.192", ocpus=None, memory_gb=None))
    store.put(job)
    messages: list[str] = []
    orig_put = store.put
    installer.save = lambda j: (messages.append(j.message or ""), orig_put(j))[1]
    installer.run(job)
    assert job.phase == JobPhase.INSTALLING, job.error
    d = fake.compute.launch_details[-1]
    assert d.shape == "BM.Standard.E5.192" and d.shape_config is None
    # the imported image only listed VM shapes as compatible: the BM shape was added before the launch
    assert "BM.Standard.E5.192" in fake.compute.images[job.iso_image_id].compatible_shapes
    assert any("Checking that image allows shape BM.Standard.E5.192" in m for m in messages)
    assert d.platform_config.is_secure_boot_enabled
    assert not d.platform_config.is_measured_boot_enabled and not d.platform_config.is_trusted_platform_module_enabled
    # the step text tells the fixed size rather than OCPU / GB numbers
    assert any("BM.Standard.E5.192, bare metal, fixed size" in m for m in messages)
    assert not any("OCPU" in m for m in messages)

    # sizing that slipped through with a BM shape is ignored (the API drops it anyway)
    job2 = make_job(make_iso(secure_boot=True), make_target(shape="BM.Standard3.64", ocpus=4, memory_gb=32),
                    job_id="iso0002")
    store.put(job2)
    installer.run(job2)
    assert job2.phase == JobPhase.INSTALLING, job2.error
    assert fake.compute.launch_details[-1].shape_config is None
    # a reused image gets the missing shape added as well
    assert job2.iso_image_id == job.iso_image_id
    assert fake.compute.images[job.iso_image_id].compatible_shapes >= {"BM.Standard.E5.192", "BM.Standard3.64"}


def test_run_reuses_an_image_with_matching_tags_and_skips_the_import(env):
    settings, fake, store, installer = env
    first = make_job(make_iso(), make_target(display_name="first"))
    installer.run(first)
    assert len(fake.compute.images) == 1

    # same ISO (object + ETag), same firmware and device model: no second import
    second = make_job(make_iso(), make_target(display_name="second"), job_id="iso0002")
    installer.run(second)
    assert second.iso_image_id == first.iso_image_id and len(fake.compute.images) == 1
    assert second.instance_id != first.instance_id and second.phase == JobPhase.INSTALLING

    # a re-uploaded ISO (new ETag), a different firmware or an emulated device model each need their own image
    for iso, target in ((make_iso(etag="etag-ubuntu-2"), make_target()),
                        (make_iso(firmware="BIOS"), make_target()),
                        (make_iso(), make_target(compatibility_mode=True))):
        before = len(fake.compute.images)
        job = make_job(iso, target, job_id=f"iso{before + 10}")
        installer.run(job)
        assert len(fake.compute.images) == before + 1 and job.iso_image_id != first.iso_image_id

    # images that are not AVAILABLE are not reused
    fake.compute.images[first.iso_image_id].lifecycle_state = "DELETED"
    again = make_job(make_iso(), make_target(display_name="again"), job_id="iso0099")
    before = len(fake.compute.images)
    installer.run(again)
    assert again.iso_image_id != first.iso_image_id and len(fake.compute.images) == before + 1


def test_run_repairs_the_data_volume_defaults_of_a_reused_emulated_image(env):
    """Images imported by earlier versions pinned Storage.RemoteDataVolumeType to PARAVIRTUALIZED whatever the
    boot volume; an emulated launch from them failed with "Mixing paravirtualized and emulated volumes".
    Reusing such an image fixes its schema first."""
    import oci.core.models as M

    settings, fake, store, installer = env
    first = make_job(make_iso(firmware="BIOS"), make_target(display_name="first", compatibility_mode=True))
    installer.run(first)
    assert first.phase == JobPhase.INSTALLING, first.error
    schema = [s for s in fake.compute.capability_schemas if s.image_id == first.iso_image_id][-1]
    # make the image look like an old import: paravirtualized data volume defaults, SCSI not even allowed
    old = M.EnumStringImageCapabilitySchemaDescriptor(source="IMAGE", values=["PARAVIRTUALIZED", "ISCSI"],
                                                      default_value="PARAVIRTUALIZED")
    schema.schema_data["Storage.RemoteDataVolumeType"] = old
    schema.schema_data.pop("Storage.LocalDataVolumeType")

    second = make_job(make_iso(firmware="BIOS"), make_target(display_name="second", compatibility_mode=True),
                      job_id="iso0002")
    installer.run(second)
    assert second.phase == JobPhase.INSTALLING, second.error
    assert second.iso_image_id == first.iso_image_id and len(fake.compute.images) == 1
    for key in ("Storage.RemoteDataVolumeType", "Storage.LocalDataVolumeType"):
        assert schema.schema_data[key].default_value == "SCSI"
    assert fake.compute.launch_details[-1].launch_options.remote_data_volume_type == "SCSI"


def test_run_windows_iso_defaults_to_emulated_devices_licensing_and_client_edition_update(env):
    settings, fake, store, installer = env
    iso = make_iso(bucket="isos", object_name="win/SERVER_EVAL_x64FRE_en-us.iso", etag="etag-win-1",
                   operating_system="Windows", operating_system_version="Windows11", firmware="UEFI_64",
                   secure_boot=True, boot_disk_gb=120)
    job = make_job(iso, make_target(display_name="win11-from-iso", compatibility_mode=True,
                                    windows_license_type=WindowsLicenseType.BRING_YOUR_OWN_LICENSE,
                                    volume_vpus_per_gb=20))
    installer.run(job)

    img = fake.compute.images[job.iso_image_id]
    # CreateImage does not know the client editions: imported without OS metadata, then set with UpdateImage
    assert (img.operating_system, img.operating_system_version) == ("Windows", "Windows11")
    assert img.launch_mode == "EMULATED"
    schema = [s for s in fake.compute.capability_schemas if s.image_id == job.iso_image_id][-1]
    assert schema.schema_data["Compute.SecureBoot"].default_value is True
    assert schema.schema_data["Storage.ConsistentVolumeNaming"].default_value is False

    # emulated boot -> the data volume defaults of the schema are emulated too (OCI resolves them from the
    # schema and refuses "Mixing paravirtualized and emulated volumes" when they stay PARAVIRTUALIZED)
    for key in ("Storage.RemoteDataVolumeType", "Storage.LocalDataVolumeType"):
        assert schema.schema_data[key].default_value == "SCSI" and "SCSI" in schema.schema_data[key].values
    assert schema.schema_data["Storage.BootVolumeType"].default_value == "IDE"

    d = fake.compute.launch_details[0]
    assert (d.launch_options.boot_volume_type, d.launch_options.network_type) == ("IDE", "E1000")
    assert d.launch_options.remote_data_volume_type == "SCSI"
    assert d.launch_options.firmware == "UEFI_64"
    assert d.source_details.boot_volume_size_in_gbs == 120 and d.source_details.boot_volume_vpus_per_gb == 20
    assert d.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"
    # Secure Boot -> shielded instance (VM shapes: Secure Boot + Measured Boot + TPM as a set)
    assert d.platform_config.is_secure_boot_enabled and d.platform_config.is_measured_boot_enabled
    assert d.platform_config.is_trusted_platform_module_enabled
    assert "UEFI Secure Boot" in d.freeform_tags["oci-umt-source-details"]
    assert job.phase == JobPhase.INSTALLING


def test_run_rejects_other_ad_and_unsuitable_secure_boot_shape_before_creating_anything(env):
    settings, fake, store, installer = env
    job = make_job(make_iso(), make_target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2"))
    with pytest.raises(OciError, match="availability domain"):
        installer.run(job)
    assert fake.compute.images == {} and fake.compute.launch_details == []

    job = make_job(make_iso(secure_boot=True), make_target(shape="VM.Standard.A1.Flex"))
    with pytest.raises(OciError, match="Secure Boot.*VM.Standard.A1.Flex"):
        installer.run(job)
    assert fake.compute.images == {} and fake.compute.launch_details == []


def test_import_progress_is_tracked_and_failure_explains_the_work_request(env):
    settings, fake, store, installer = env
    fake.import_polls = 4
    job = make_job(make_iso(), make_target())
    percents: list[int] = []
    orig_put = store.put

    def spy(j):
        if j.step == STEP_ISO_IMAGE and j.step_percent is not None:
            percents.append(j.step_percent)
        return orig_put(j)

    installer.save = spy
    installer.run(job)
    assert percents and percents == sorted(percents) and percents[-1] == 100
    assert job.phase == JobPhase.INSTALLING

    # a missing object: OCI accepts the import and deletes the image; the job fails with the work request error
    fake.import_polls = 0
    missing = make_job(make_iso(object_name="images/does-not-exist.iso", etag="x"), make_target(), job_id="iso0002")
    with pytest.raises(OciError) as exc:
        installer.run(missing)
    assert "entered state DELETED" in str(exc.value)
    assert "does-not-exist.iso not found in bucket isos" in str(exc.value)
    assert missing.instance_id is None and len(fake.compute.launch_details) == 1  # only the first job's

    # a bucket the import service may not read: OCI claims it "does not exist"; the job names the policy gap
    fake.import_outcome, fake.import_errors = "DELETED", [
        ("InvalidParameter", "Specified namespace or bucket for image import does not exist.")]
    fake.import_logs = ["Preparing environment for image conversion."]
    denied = make_job(make_iso(etag="etag-ubuntu-3"), make_target(), job_id="iso0003")
    with pytest.raises(OciError) as exc:
        installer.run(denied)
    msg = str(exc.value)
    assert "bucket for image import does not exist" in msg and "permission problem" in msg
    assert "PAR_MANAGE" in msg and "bucket 'isos' exists" in msg


def test_launch_failure_reports_work_request_error(env):
    settings, fake, store, installer = env
    fake.launch_outcome = "TERMINATED"
    fake.launch_errors = [("LimitExceeded", "Out of host capacity")]
    job = make_job(make_iso(), make_target())
    with pytest.raises(OciError, match="Out of host capacity"):
        installer.run(job)
    assert job.instance_id is not None  # recorded so cancel can clean it up
    assert job.iso_image_id in fake.compute.images  # the image stays for the next attempt


def test_cancel_between_steps_and_cleanup_terminates_instance_but_keeps_image(env):
    settings, fake, store, installer = env
    job = make_job(make_iso(), make_target())

    def check():
        if job.iso_image_id:  # cancel requested while the image was imported: seen before the launch step
            raise JobCancelled()

    with pytest.raises(JobCancelled):
        installer.run(job, check_cancel=check)
    assert job.iso_image_id is not None and job.instance_id is None and fake.compute.launch_details == []
    actions = installer.cleanup(job)
    assert actions == [] and job.phase == JobPhase.CANCELLED and "nothing to clean up" in job.message

    # a cancelled job with an instance: the instance (and its boot volume) goes, the image stays for reuse
    job2 = make_job(make_iso(), make_target(display_name="second"), job_id="iso0002")
    installer.run(job2)
    actions = installer.cleanup(job2)
    assert actions == [f"ok: terminate instance {job2.instance_id}"]
    assert fake.compute.terminated == [job2.instance_id]
    assert fake.compute.images[job2.iso_image_id].lifecycle_state == "AVAILABLE"
    assert job2.phase == JobPhase.CANCELLED and store.get(job2.id).phase == JobPhase.CANCELLED


def test_finish_completes_the_job_and_leaves_the_instance(env):
    settings, fake, store, installer = env
    job = make_job(make_iso(), make_target())
    installer.run(job)
    installer.finish(job)
    assert job.phase == JobPhase.COMPLETED and job.step == "done" and job.error is None
    assert fake.compute.instances[job.instance_id].lifecycle_state == "RUNNING"
    assert store.get(job.id).phase == JobPhase.COMPLETED


def test_cleanup_images_deletes_only_iso_images(env):
    settings, fake, store, installer = env
    installer.run(make_job(make_iso(), make_target()))
    installer.run(make_job(make_iso(firmware="BIOS"), make_target(), job_id="iso0002"))
    # a seed image in the same compartment must survive
    import oci.core.models as M

    fake.object_storage.add_bucket("oci-umt-seed")
    fake.object_storage.put_object(NS, "oci-umt-seed", "seed.vmdk", b"KDMV")
    seed = fake.compute.create_image(M.CreateImageDetails(
        compartment_id=installer.image_compartment, display_name="oci-umt-seed", launch_mode="PARAVIRTUALIZED",
        freeform_tags={"oci-umt-seed": "true"},
        image_source_details=M.ImageSourceViaObjectStorageTupleDetails(
            source_type="objectStorageTuple", namespace_name=NS, bucket_name="oci-umt-seed", object_name="seed.vmdk",
            source_image_type="VMDK"))).data
    fake.compute.get_image(seed.id)  # settle the import
    deleted = installer.cleanup_images()
    assert len(deleted) == 2 and seed.id not in deleted
    assert fake.compute.images[seed.id].lifecycle_state == "AVAILABLE"
    assert all(fake.compute.images[i].lifecycle_state == "DELETED" for i in deleted)
    assert installer.cleanup_images() == []  # nothing left
