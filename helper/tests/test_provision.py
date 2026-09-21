import pytest

from helper_app.config import Settings
from helper_app.jobs.store import JobStore, utcnow
from helper_app.models import (
    DiskSpec,
    DiskStatus,
    Firmware,
    Job,
    JobPhase,
    NicSpec,
    OciTarget,
    VmSpec,
    WindowsLicenseType,
)
from helper_app.oci.clients import OciError
from helper_app.oci.provision import Provisioner
from helper_app.oci.seed_image import SeedImageService

from .fake_oci import FakeOci, service_error

GIB = 1024**3


def make_vm(windows=False, firmware=Firmware.EFI, disks=2, secure_boot=False) -> VmSpec:
    return VmSpec(
        moid="vm-42",
        name="app-server-01",
        num_cpu=4,
        memory_mb=16384,
        guest_id="windows2019srvNext_64Guest" if windows else "oracleLinux8_64Guest",
        guest_full_name="Microsoft Windows Server 2022 (64-bit)" if windows else "Oracle Linux 8 (64-bit)",
        firmware=firmware,
        secure_boot=secure_boot,
        disks=[
            DiskSpec(index=i, label=f"Hard disk {i + 1}", device_key=2000 + i, capacity_bytes=(40 + 60 * i) * GIB,
                     controller_type="pvscsi")
            for i in range(disks)
        ],
        nics=[NicSpec(label="Network adapter 1", adapter_type="vmxnet3")],
    )


def make_target(**kw) -> OciTarget:
    base = dict(compartment_id="ocid1.compartment.oc1..migr", availability_domain="Uocm:EU-FRANKFURT-1-AD-1",
                subnet_id="ocid1.subnet.oc1..1")
    base.update(kw)
    return OciTarget(**base)


def make_job(vm: VmSpec, target: OciTarget) -> Job:
    now = utcnow()
    return Job(id="job0001", vm=vm, vcenter_host="vc.test:4443", target=target, created_at=now, updated_at=now)


@pytest.fixture
def env(tmp_path):
    settings = Settings(device_prefix=str(tmp_path / "dev" / "oraclevd"), db_path=str(tmp_path / "jobs.db"),
                        seed_bucket="oci-umt-seed", launch_timeout_s=5, volume_timeout_s=5,
                        image_import_timeout_s=5)
    fake = FakeOci(settings.device_prefix)
    clients = fake.clients()
    store = JobStore(settings.db_path)
    prov = Provisioner(clients, settings, store.put, SeedImageService(clients, settings),
                       scan_devices=fake.scan_devices)
    return settings, fake, store, prov


def test_prepare_linux_two_disks(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)

    assert job.phase == JobPhase.PROVISIONING and job.step == "ready"
    assert job.launch_options.firmware == "UEFI_64"
    assert len(job.disks) == 2 and job.disks[0].is_boot
    assert job.disks[0].size_gb == 50 and job.disks[1].size_gb == 100

    # seed image imported as PARAVIRTUALIZED (CUSTOM is not importable), VMDK placeholder, OS metadata, schema
    img = fake.compute.images[job.seed_image_id]
    assert img.compartment_id == fake.identity.compartment_id  # helper compartment from discovered identity
    assert all("." not in k and " " not in k for k in img.freeform_tags), "OCI rejects freeform tag keys with periods"
    assert img.launch_mode == "PARAVIRTUALIZED"
    assert img.source_image_type == "VMDK"
    assert (img.operating_system, img.operating_system_version) == ("Oracle Linux", "8")
    assert img.freeform_tags["oci-umt-firmware"] == "UEFI_64"
    assert fake.object_storage.deleted == [img.object_name]  # placeholder object removed
    assert "oci-umt-seed" in fake.object_storage.buckets
    schema = fake.compute.capability_schemas[0].schema_data
    assert schema["Compute.Firmware"].values == ["UEFI_64"]
    assert schema["Compute.LaunchMode"].default_value == "PARAVIRTUALIZED"
    # every device model stays selectable so per-job LaunchOptions are accepted at launch
    assert set(schema["Storage.BootVolumeType"].values) >= {"PARAVIRTUALIZED", "IDE", "SCSI", "ISCSI"}
    assert set(schema["Network.AttachmentType"].values) >= {"PARAVIRTUALIZED", "E1000", "VFIO"}

    # instance launched from the seed with explicit launch options and matching shape
    ld = fake.compute.launch_details[0]
    assert ld.source_details.image_id == job.seed_image_id
    assert ld.source_details.boot_volume_size_in_gbs == 50
    assert ld.source_details.boot_volume_vpus_per_gb == 10  # Balanced tier by default
    assert fake.blockstorage.volumes[job.disks[1].volume_id].vpus_per_gb == 10
    assert ld.launch_options.firmware == "UEFI_64"
    assert ld.launch_options.boot_volume_type == "PARAVIRTUALIZED"
    assert ld.launch_options.network_type == "PARAVIRTUALIZED"
    assert ld.shape_config.ocpus == 2 and ld.shape_config.memory_in_gbs == 16
    assert ld.licensing_configs is None
    assert ld.create_vnic_details.hostname_label == "app-server-01"
    assert getattr(ld, "platform_config", None) is None  # no Secure Boot on the source -> plain launch
    assert schema["Compute.SecureBoot"].default_value is False
    # provenance tags: job, source vCenter, VM name/moid and its sizing
    tags = ld.freeform_tags
    assert tags["oci-umt-job"] == job.id and tags["oci-umt-source-vm"] == "app-server-01"
    assert tags["oci-umt-source-moid"] == "vm-42" and tags["oci-umt-source-vcenter"] == "vc.test:4443"
    assert tags["oci-umt-source-vm-details"] == ("4 vCPU, 16 GB RAM, 2 disk(s) 140 GB [40 GB, 100 GB], 1 NIC(s), "
                                                "Oracle Linux 8 (64-bit), UEFI")
    assert all(len(v) <= 256 for v in tags.values())

    # the data volume was attached to the target while it still ran from the seed (OCI attaches data volumes
    # to running instances only) as read/write shareable, so it is in place for the guest's first boot
    target_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]
    assert [a.volume_id for a in target_atts] == [job.disks[1].volume_id]
    assert target_atts[0].is_shareable and target_atts[0].instance_state_at_attach == "RUNNING"
    assert target_atts[0].attachment_type == "paravirtualized" and target_atts[0].lifecycle_state == "ATTACHED"
    assert target_atts[0].device == f"{settings.device_prefix}b"
    assert job.disks[1].target_attachment_id == target_atts[0].id
    # then stopped, boot volume detached from the target and attached to the helper
    assert (job.instance_id, "STOP") in fake.compute.actions
    assert fake.compute.instances[job.instance_id].lifecycle_state == "STOPPED"
    boot_att = [a for a in fake.compute.boot_attachments.values() if a.instance_id == job.instance_id]
    assert boot_att[0].lifecycle_state == "DETACHED"
    assert job.boot_volume_id == boot_att[0].boot_volume_id
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    assert {a.volume_id for a in helper_atts} == {job.disks[0].volume_id, job.disks[1].volume_id}
    assert all(a.attachment_type == "paravirtualized" for a in helper_atts)
    # boot volume: no device path allowed, so it is found as the disk that appeared; data volume: consistent path
    assert helper_atts[0].device is None and job.disks[0].device.endswith("sdb")
    assert job.disks[1].device.endswith("oraclevdb")
    # the helper's copy of the shared data volume is the second shareable attachment; the boot volume is not shared
    assert not helper_atts[0].is_shareable and helper_atts[1].is_shareable
    assert all(d.status == DiskStatus.ATTACHED for d in job.disks)
    assert [d.label for d in job.disks] == ["Hard disk 1", "Hard disk 2"]
    # store mirrors the in-memory job
    assert store.get(job.id).step == "ready"


@pytest.mark.parametrize("vpus", [20, 30])
def test_prepare_applies_volume_performance_to_boot_and_data_volumes(env, vpus):
    settings, fake, store, prov = env
    job = make_job(make_vm(disks=3), make_target(volume_vpus_per_gb=vpus))
    store.put(job)
    prov.prepare(job)

    assert fake.compute.launch_details[0].source_details.boot_volume_vpus_per_gb == vpus
    data_volumes = [fake.blockstorage.volumes[d.volume_id] for d in job.disks[1:]]
    assert len(data_volumes) == 2 and all(v.vpus_per_gb == vpus for v in data_volumes)


def test_volume_performance_must_be_a_supported_tier():
    for bad in (0, 15, 40):
        with pytest.raises(ValueError):
            make_target(volume_vpus_per_gb=bad)


def test_prepare_windows_bios_licensing_and_seed_reuse(env):
    settings, fake, store, prov = env
    target = make_target(windows_license_type=WindowsLicenseType.OCI_PROVIDED, compatibility_mode=True,
                         display_name="Win Box #1")
    job = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=1), target)
    store.put(job)
    prov.prepare(job)

    ld = fake.compute.launch_details[0]
    assert ld.launch_options.firmware == "BIOS"
    assert ld.launch_options.boot_volume_type == "IDE" and ld.launch_options.network_type == "E1000"
    assert ld.launch_options.remote_data_volume_type == "SCSI"  # emulated like the boot volume, never mixed
    assert ld.licensing_configs[0].type == "WINDOWS"
    assert ld.licensing_configs[0].license_type == "OCI_PROVIDED"
    assert ld.create_vnic_details.hostname_label == "win-box-1"
    img = fake.compute.images[job.seed_image_id]
    assert img.operating_system == "Windows" and img.operating_system_version == "Server 2022 Standard"
    assert img.launch_mode == "EMULATED"  # IDE + E1000 requested
    assert img.display_name.endswith("-emulated") and img.freeform_tags["oci-umt-launch-mode"] == "EMULATED"
    # the schema's data volume defaults follow the emulated boot volume (OCI resolves them from the schema)
    schema = [s for s in fake.compute.capability_schemas if s.image_id == job.seed_image_id][-1]
    assert schema.schema_data["Storage.RemoteDataVolumeType"].default_value == "SCSI"
    assert schema.schema_data["Storage.LocalDataVolumeType"].default_value == "SCSI"

    # a second Windows/BIOS job reuses the seed image
    job2 = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=1), target)
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert job2.seed_image_id == job.seed_image_id
    assert len(fake.compute.images) == 1

    # the same VM without "Maximum compatibility" must not reuse the EMULATED seed: OCI rejects a
    # paravirtualized launch from it ("Mixing paravirtualized and emulated volumes ...")
    job3 = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=2),
                    make_target(windows_license_type=WindowsLicenseType.OCI_PROVIDED))
    job3.id = "job0003"
    store.put(job3)
    prov.prepare(job3)
    assert job3.seed_image_id != job.seed_image_id
    assert fake.compute.images[job3.seed_image_id].launch_mode == "PARAVIRTUALIZED"
    assert fake.compute.launch_details[-1].launch_options.remote_data_volume_type == "PARAVIRTUALIZED"
    assert len(fake.compute.images) == 2


def test_emulated_data_volumes_attach_as_emulated_after_the_start(env):
    """Emulated attachments cannot be shareable, so they are not pre-attached in prepare(); OCI only attaches
    data volumes to a running instance, so finalize() starts the target first and hot-plugs them."""
    settings, fake, store, prov = env
    job = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=2),
                   make_target(compatibility_mode=True, windows_license_type=WindowsLicenseType.OCI_PROVIDED))
    store.put(job)
    prov.prepare(job)
    assert not [a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]
    assert job.disks[1].target_attachment_id is None
    for d in job.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job)
    target_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]
    assert [a.attachment_type for a in target_atts] == ["emulated"]
    assert target_atts[0].instance_state_at_attach == "RUNNING" and not target_atts[0].is_shareable
    assert fake.compute.actions.count((job.instance_id, "START")) == 1
    assert fake.compute.instances[job.instance_id].lifecycle_state == "RUNNING"


def test_emulated_without_start_after_migration_stops_the_target_again(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=2),
                   make_target(compatibility_mode=True, windows_license_type=WindowsLicenseType.OCI_PROVIDED,
                               start_after_migration=False))
    store.put(job)
    prov.prepare(job)
    for d in job.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job)
    target_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]
    assert len(target_atts) == 1 and target_atts[0].lifecycle_state == "ATTACHED"
    actions = [a for i, a in fake.compute.actions if i == job.instance_id]
    assert actions == ["STOP", "START", "SOFTSTOP"]
    assert fake.compute.instances[job.instance_id].lifecycle_state == "STOPPED"
    assert job.phase == JobPhase.COMPLETED


def test_windows_data_volumes_attach_without_device_path(env):
    """Consistent device paths are Linux-only; OCI rejects the device attribute on Windows instances."""
    settings, fake, store, prov = env
    job = make_job(make_vm(windows=True, disks=3), make_target(windows_license_type=WindowsLicenseType.OCI_PROVIDED))
    store.put(job)
    prov.prepare(job)
    # the launch never sends the flag (OCI refuses overriding the image); the Windows seed's schema has it off
    assert fake.compute.launch_details[-1].launch_options.is_consistent_volume_naming_enabled is None
    assert fake.compute.capability_schemas[-1].schema_data["Storage.ConsistentVolumeNaming"].default_value is False
    for d in job.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job)
    target_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]
    assert len(target_atts) == 2 and all(a.device is None for a in target_atts)
    assert job.phase == JobPhase.COMPLETED

    # Linux keeps the consistent paths on the target
    job2 = make_job(make_vm(disks=2), make_target())
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert fake.compute.launch_details[-1].launch_options.is_consistent_volume_naming_enabled is None
    assert fake.compute.capability_schemas[-1].schema_data["Storage.ConsistentVolumeNaming"].default_value is True
    for d in job2.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job2)
    atts2 = [a for a in fake.compute.vol_attachments.values() if a.instance_id == job2.instance_id]
    assert [a.device for a in atts2] == [f"{settings.device_prefix}b"]


def test_prepare_rejects_other_ad(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2"))
    store.put(job)
    with pytest.raises(OciError, match="availability domain"):
        prov.prepare(job)


def test_secure_boot_source_launches_shielded_instance(env):
    """A source with UEFI Secure Boot gets its own seed image (schema declares Compute.SecureBoot) and is
    launched with a platform config that enables Secure Boot, matching the shape's CPU family."""
    import oci.core.models as M

    settings, fake, store, prov = env
    job = make_job(make_vm(secure_boot=True), make_target())
    store.put(job)
    prov.prepare(job)

    assert job.launch_options.secure_boot is True
    img = fake.compute.images[job.seed_image_id]
    assert img.freeform_tags["oci-umt-secure-boot"] == "true" and img.display_name.endswith("-secureboot")
    assert fake.compute.capability_schemas[-1].schema_data["Compute.SecureBoot"].default_value is True
    ld = fake.compute.launch_details[-1]
    assert isinstance(ld.platform_config, M.AmdVmLaunchInstancePlatformConfig)  # VM.Standard.E5.Flex default
    # VM shapes: OCI only accepts Secure Boot together with Measured Boot and the vTPM
    assert ld.platform_config.is_secure_boot_enabled is True
    assert ld.platform_config.is_measured_boot_enabled is True
    assert ld.platform_config.is_trusted_platform_module_enabled is True
    assert ld.launch_options.firmware == "UEFI_64"

    # Intel shape family -> Intel platform config
    job2 = make_job(make_vm(secure_boot=True), make_target(shape="VM.Standard3.Flex"))
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    ld2 = fake.compute.launch_details[-1]
    assert isinstance(ld2.platform_config, M.IntelVmLaunchInstancePlatformConfig)
    assert ld2.platform_config.is_measured_boot_enabled and ld2.platform_config.is_trusted_platform_module_enabled
    assert job2.seed_image_id == job.seed_image_id  # the secure-boot seed is reused

    # Windows with Secure Boot (the case OCI reports as "secured with Credential Guard"): all three flags
    jobw = make_job(make_vm(windows=True, secure_boot=True), make_target())
    jobw.id = "job000w"
    store.put(jobw)
    prov.prepare(jobw)
    ldw = fake.compute.launch_details[-1]
    assert ldw.platform_config.is_secure_boot_enabled and ldw.platform_config.is_measured_boot_enabled
    assert ldw.platform_config.is_trusted_platform_module_enabled
    assert ldw.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"

    # bare metal Linux: Secure Boot alone (Measured Boot is VM-only on Linux); bare metal Windows: the set
    jobbm = make_job(make_vm(secure_boot=True), make_target(shape="BM.Standard.E4.128"))
    jobbm.id = "job00bm"
    store.put(jobbm)
    prov.prepare(jobbm)
    ldbm = fake.compute.launch_details[-1]
    assert isinstance(ldbm.platform_config, M.GenericBmLaunchInstancePlatformConfig)
    assert ldbm.platform_config.is_secure_boot_enabled and not ldbm.platform_config.is_measured_boot_enabled
    jobbmw = make_job(make_vm(windows=True, secure_boot=True), make_target(shape="BM.Standard.E4.128"))
    jobbmw.id = "job0bmw"
    store.put(jobbmw)
    prov.prepare(jobbmw)
    assert fake.compute.launch_details[-1].platform_config.is_trusted_platform_module_enabled

    # a VM without Secure Boot must not pick up the secure-boot seed, and vice versa
    job3 = make_job(make_vm(), make_target())
    job3.id = "job0003"
    store.put(job3)
    prov.prepare(job3)
    assert job3.seed_image_id != job.seed_image_id
    assert getattr(fake.compute.launch_details[-1], "platform_config", None) is None


def test_secure_boot_refuses_shapes_without_platform_config_before_creating_anything(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(secure_boot=True), make_target(shape="VM.Standard.A1.Flex"))
    store.put(job)
    with pytest.raises(OciError, match="Secure Boot.*VM.Standard.A1.Flex"):
        prov.prepare(job)
    assert not fake.compute.images and not fake.compute.launch_details  # nothing was created


def test_seed_images_without_the_secure_boot_tag_are_still_reused(env):
    """Seeds created before the Secure Boot dimension existed lack the tag; they count as 'false'."""
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)
    img = fake.compute.images[job.seed_image_id]
    del img.freeform_tags["oci-umt-secure-boot"]

    job2 = make_job(make_vm(), make_target())
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert job2.seed_image_id == job.seed_image_id

    job3 = make_job(make_vm(secure_boot=True), make_target())
    job3.id = "job0003"
    store.put(job3)
    prov.prepare(job3)
    assert job3.seed_image_id != job.seed_image_id  # an untagged seed never serves a Secure Boot job


def test_seed_import_progress_is_tracked_from_the_work_request(env):
    """While the CreateImage work request runs, its percentComplete lands in job.step_percent / message
    (every save is visible to the UI); the field is cleared once the step is over."""
    settings, fake, store, prov = env
    fake.import_polls = 4  # 25% -> 50% -> 75% -> 100% (image still IMPORTING) -> AVAILABLE
    seen: list[tuple[str, int | None, str]] = []
    saved = store.put

    def spy(job):
        seen.append((job.step, job.step_percent, job.message))
        return saved(job)

    prov.save = spy
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)

    import_updates = [(pct, msg) for step, pct, msg in seen if step == "seed_image" and pct is not None]
    # 0 when the import is requested; the work request's percent while it runs (capped at 99 until the image
    # is really AVAILABLE); 100 once it is
    assert [pct for pct, _ in import_updates] == [0, 25, 50, 75, 99, 100]
    assert import_updates[2][1] == "Importing seed image oci-umt-seed-uefi_64-oracle-linux-8: 50% (in progress)"
    assert import_updates[-1][1].startswith("Seed image oci-umt-seed-uefi_64-oracle-linux-8 imported")
    # the next step starts with a clean percentage, and a finished prepare has none
    assert all(pct is None for step, pct, _ in seen if step == "launch_instance")
    assert job.step_percent is None and job.seed_image_id in fake.compute.images

    # progress reading is best effort: without `read work-requests` the import still completes
    fake.work_requests.error = service_error(404, "NotAuthorizedOrNotFound", "Authorization failed", "get_work_request")
    fake.compute.images.clear()
    job2 = make_job(make_vm(), make_target())
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert job2.seed_image_id in fake.compute.images


def test_seed_import_failure_explains_work_request(env):
    """OCI deletes an image whose import failed; the reason lives on the work request, so it is copied into the
    job error (or, when OCI recorded nothing, the usual cause: the import service cannot create a PAR)."""
    settings, fake, store, prov = env
    fake.import_outcome = "DELETED"
    fake.import_errors = [("InternalError", "An internal error occurred. reference ID: abc")]
    fake.import_logs = ["Downloading image from Object Storage.", "Converting image."]
    job = make_job(make_vm(), make_target())
    store.put(job)
    with pytest.raises(OciError) as exc:
        prov.prepare(job)
    msg = str(exc.value)
    assert "entered state DELETED" in msg
    assert "import work request ocid1.coreservicesworkrequest" in msg
    assert "OCI error InternalError: An internal error occurred" in msg
    assert "import log: Downloading image from Object Storage. / Converting image." in msg
    assert "PAR_MANAGE" not in msg
    img = next(iter(fake.compute.images.values()))
    assert fake.object_storage.deleted == [img.object_name]  # placeholder removed even on failure

    # silent failure (what a missing PAR_MANAGE permission looks like) -> policy hint naming the bucket
    fake.import_errors, fake.import_logs = [], []
    job2 = make_job(make_vm(), make_target())
    job2.id = "job0002"
    store.put(job2)
    with pytest.raises(OciError, match="PAR_MANAGE") as exc2:
        prov.prepare(job2)
    assert f"target.bucket.name = '{settings.seed_bucket}'" in str(exc2.value)

    # the explanation must never mask the failure itself (e.g. no 'read work-requests' permission)
    fake.work_requests.error = service_error(404, "NotAuthorizedOrNotFound", "Authorization failed",
                                             "list_work_request_errors")
    job3 = make_job(make_vm(), make_target())
    job3.id = "job0003"
    store.put(job3)
    with pytest.raises(OciError, match="entered state DELETED.*could not read it: OCI list_work_request_errors"):
        prov.prepare(job3)


def test_hostname_label_avoids_existing_dns_names_in_subnet(env):
    """DNS labels are unique per subnet.  A VM whose name is already used by another instance in the target
    subnet must get a free label instead of an asynchronous 'Hostname ... is already used' launch failure."""
    from types import SimpleNamespace as NS

    settings, fake, store, prov = env
    fake.network.private_ips += [
        NS(hostname_label="app-server-01", subnet_id="ocid1.subnet.oc1..1", vnic_id="v1"),   # existing instance
        NS(hostname_label="app-server-01-2", subnet_id="ocid1.subnet.oc1..1", vnic_id="v2"),
        NS(hostname_label="app-server-01", subnet_id="ocid1.subnet.oc1..2", vnic_id="v3"),   # other subnet: no clash
    ]
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)
    assert fake.compute.launch_details[0].create_vnic_details.hostname_label == "app-server-01-3"
    assert fake.compute.instances[job.instance_id].lifecycle_state == "STOPPED"

    # a second copy of the same VM into the same subnet gets the next free label
    job2 = make_job(make_vm(), make_target())
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert fake.compute.launch_details[1].create_vnic_details.hostname_label == "app-server-01-4"

    # the other subnet is untouched by those launches
    job3 = make_job(make_vm(), make_target(subnet_id="ocid1.subnet.oc1..2"))
    job3.id = "job0003"
    store.put(job3)
    prov.prepare(job3)
    assert fake.compute.launch_details[2].create_vnic_details.hostname_label == "app-server-01-2"


def test_hostname_label_suffix_respects_63_chars(env):
    from types import SimpleNamespace as NS

    settings, fake, store, prov = env
    long_name = "x" * 70
    fake.network.private_ips.append(NS(hostname_label="x" * 63, subnet_id="ocid1.subnet.oc1..1", vnic_id="v"))
    job = make_job(make_vm(), make_target(display_name=long_name))
    store.put(job)
    prov.prepare(job)
    label = fake.compute.launch_details[0].create_vnic_details.hostname_label
    assert label == "x" * 61 + "-2" and len(label) == 63


def test_launch_failure_reports_work_request_error(env):
    """An instance that OCI terminates right after launch (capacity, VNIC, ...) explains itself only on its work
    request; that reason must end up in the job error."""
    settings, fake, store, prov = env
    fake.launch_outcome = "TERMINATING"
    fake.launch_errors = [("OutOfCapacity", "Out of host capacity.")]
    job = make_job(make_vm(), make_target())
    store.put(job)
    with pytest.raises(OciError, match="entered state TERMINATING.*LaunchInstance: Out of host capacity"):
        prov.prepare(job)
    assert job.instance_id  # recorded, so cleanup can terminate it


def test_prepare_cancel_hook_aborts_between_steps(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    def check():  # the hook runs before every step and on every in-step progress update
        if job.instance_id:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        prov.prepare(job, check_cancel=check)
    assert job.instance_id is not None  # launched, cancelled before the step after it
    assert all(d.device is None for d in job.disks)  # never got to the attachments


def test_finalize_requires_copied_then_reattaches_and_starts(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)

    with pytest.raises(OciError, match="not been copied"):
        prov.finalize(job)

    for d in job.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job)

    assert job.phase == JobPhase.COMPLETED
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    assert all(a.lifecycle_state == "DETACHED" for a in helper_atts)
    boot_atts = [a for a in fake.compute.boot_attachments.values()
                 if a.instance_id == job.instance_id and a.lifecycle_state == "ATTACHED"]
    assert len(boot_atts) == 1 and boot_atts[0].boot_volume_id == job.boot_volume_id
    target_atts = [a for a in fake.compute.vol_attachments.values()
                   if a.instance_id == job.instance_id and a.lifecycle_state == "ATTACHED"]
    assert [a.volume_id for a in target_atts] == [job.disks[1].volume_id]
    assert target_atts[0].device.endswith("oraclevdb")
    # the data volume attachment from prepare() is kept: nothing is attached after the boot volume, and the
    # instance is started once, with all disks already present
    assert len([a for a in fake.compute.vol_attachments.values() if a.instance_id == job.instance_id]) == 1
    assert target_atts[0].instance_state_at_attach == "RUNNING"
    assert fake.compute.actions.count((job.instance_id, "START")) == 1
    assert fake.compute.instances[job.instance_id].lifecycle_state == "RUNNING"

    # finalize is idempotent (Retry finalize): nothing is attached or started twice
    prov.finalize(job)
    assert fake.compute.actions.count((job.instance_id, "START")) == 1
    assert len([a for a in fake.compute.boot_attachments.values()
                if a.instance_id == job.instance_id and a.lifecycle_state == "ATTACHED"]) == 1


def test_finalize_without_start(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(disks=1), make_target(start_after_migration=False))
    store.put(job)
    prov.prepare(job)
    job.disks[0].status = DiskStatus.COPIED
    prov.finalize(job)
    assert (job.instance_id, "START") not in fake.compute.actions
    assert job.phase == JobPhase.COMPLETED


def test_cleanup_tears_down(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)
    actions = prov.cleanup(job)
    assert job.phase == JobPhase.CANCELLED
    assert job.instance_id in fake.compute.terminated
    assert set(fake.blockstorage.deleted) == {job.disks[0].volume_id, job.disks[1].volume_id}
    assert all(a.startswith("ok:") for a in actions), actions
    # every attachment (helper side and the pre-attached target side) was released before the deletes
    assert all(a.lifecycle_state == "DETACHED" for a in fake.compute.vol_attachments.values())
    assert job.disks[1].target_attachment_id is None and job.disks[1].helper_attachment_id is None


def test_wait_for_timeout_and_failure_state(env):
    settings, fake, store, prov = env
    clients = fake.clients()
    from types import SimpleNamespace

    class R:
        def __init__(self, state):
            self.data = SimpleNamespace(lifecycle_state=state)

    with pytest.raises(OciError, match="timed out"):
        clients.wait_for(lambda: R("PROVISIONING"), "lifecycle_state", ["RUNNING"], timeout_s=0.0, what="x")
    with pytest.raises(OciError, match="entered state TERMINATED"):
        clients.wait_for(lambda: R("TERMINATED"), "lifecycle_state", ["RUNNING"], timeout_s=1, what="x")
