import pytest

from helper_app.models import BootVolumeType, DiskSpec, Firmware, NetworkType, NicSpec, OciTarget, VmSpec
from helper_app.oci import mapping as m


def vm(**kw) -> VmSpec:
    base = dict(
        moid="vm-1",
        name="test",
        num_cpu=4,
        memory_mb=8192,
        guest_id="oracleLinux9_64Guest",
        guest_full_name="Oracle Linux 9 (64-bit)",
        firmware=Firmware.EFI,
        disks=[DiskSpec(index=0, label="Hard disk 1", device_key=2000, capacity_bytes=40 * 1024**3, controller_type="pvscsi")],
        nics=[NicSpec(label="Network adapter 1", adapter_type="vmxnet3")],
    )
    base.update(kw)
    return VmSpec(**base)


def target(**kw) -> OciTarget:
    base = dict(compartment_id="c", availability_domain="ad", subnet_id="s")
    base.update(kw)
    return OciTarget(**base)


@pytest.mark.parametrize(
    "guest_id,full,os,version,family",
    [
        ("oracleLinux9_64Guest", "", "Oracle Linux", "9", "linux"),
        ("oracleLinux8_64Guest", "", "Oracle Linux", "8", "linux"),
        ("rhel8_64Guest", "", "Red Hat Enterprise Linux", "8", "linux"),
        ("centos7_64Guest", "", "CentOS", "7", "linux"),
        ("ubuntu64Guest", "Ubuntu Linux (64-bit)", "Ubuntu", "22.04", "linux"),
        ("ubuntu64Guest", "Ubuntu 24.04 LTS", "Ubuntu", "24.04", "linux"),
        ("debian12_64Guest", "", "Debian", "12", "linux"),
        ("windows2019srv_64Guest", "", "Windows", "Server 2019 Standard", "windows"),
        ("windows2019srv_64Guest", "Microsoft Windows Server 2019 (64-bit)", "Windows", "Server 2019 Standard", "windows"),
        # vSphere identifies Windows Server 2022 as "2019srvNext" (7.0 U2+) and Server 2025 as "2022srvNext" (8.0 U2+)
        ("windows2019srvNext_64Guest", "", "Windows", "Server 2022 Standard", "windows"),
        ("windows2019srvNext_64Guest", "Microsoft Windows Server 2022 (64-bit)", "Windows", "Server 2022 Standard", "windows"),
        ("windows2022srvNext_64Guest", "", "Windows", "Server 2025 Standard", "windows"),
        ("windows2022srvNext_64Guest", "Microsoft Windows Server 2025 (64-bit)", "Windows", "Server 2025 Standard", "windows"),
        # the year shown by vCenter (guestFullName) wins over the guestId encoding
        ("windows9Server64Guest", "Microsoft Windows Server 2022 (64-bit)", "Windows", "Server 2022 Standard", "windows"),
        ("windows9Server64Guest", "Microsoft Windows Server 2016 (64-bit)", "Windows", "Server 2016 Standard", "windows"),
        # client editions use OCI's catalog names; CreateImage rejects "10 Enterprise" and the like
        ("windows9_64Guest", "Microsoft Windows 10 (64-bit)", "Windows", "Windows10", "windows"),
        ("windows9_64Guest", "", "Windows", "Windows10", "windows"),
        ("windows9_64Guest", "Microsoft Windows 11 (64-bit)", "Windows", "Windows11", "windows"),  # older vSphere
        ("windows11_64Guest", "Microsoft Windows 11 (64-bit)", "Windows", "Windows11", "windows"),
        ("otherGuest64", "Other 5.x Linux (64-bit)", "Custom Linux", "5", "linux"),
        ("other3xLinux64Guest", "Other 3.x or later Linux (64-bit)", "Custom Linux", "3", "linux"),
    ],
)
def test_map_guest_os(guest_id, full, os, version, family):
    meta = m.map_guest_os(guest_id, full)
    assert (meta.operating_system, meta.operating_system_version, meta.family) == (os, version, family)


def test_os_version_detection_and_choices():
    # ubuntu64Guest carries no release: the version is a guess and the user must pick from OCI's list
    meta = m.map_guest_os("ubuntu64Guest", "Ubuntu Linux (64-bit)")
    assert meta.version_detected is False
    assert m.os_version_choices(meta) == ["18.04", "20.04", "22.04", "24.04", "26.04"]
    # ... unless VMware Tools put it into the display name
    assert m.map_guest_os("ubuntu64Guest", "Ubuntu 24.04 LTS").version_detected is True
    # the bitness in the display name is not a release
    rocky = m.map_guest_os("rockylinux_64Guest", "Rocky Linux (64-bit)")
    assert (rocky.operating_system_version, rocky.version_detected) == ("9", False)
    rocky9 = m.map_guest_os("rockylinux_64Guest", "Rocky Linux 9 (64-bit)")
    assert (rocky9.operating_system_version, rocky9.version_detected) == ("9", True)
    # a release encoded in the guestId is trusted
    assert m.map_guest_os("oracleLinux9_64Guest").version_detected is True
    assert m.map_guest_os("windows2022srv_64Guest").version_detected is True
    assert m.map_guest_os("windowsUnknownGuest").version_detected is False
    # detected releases outside the catalog stay selectable
    ol = m.map_guest_os("oraclelinux5_64Guest")
    assert m.os_version_choices(ol)[-1] == "5"
    # no catalog for generic Linux -> nothing to choose from
    assert m.os_version_choices(m.map_guest_os("otherGuest64", "Other Linux (64-bit)")) == []

    picked = m.with_os_version(meta, "24.04")
    assert (picked.operating_system, picked.operating_system_version, picked.version_detected) == ("Ubuntu", "24.04", True)
    assert m.with_os_version(meta, None) is meta
    assert picked.slug == "ubuntu-24-04"


def test_windows_from_full_name_only():
    meta = m.map_guest_os("otherGuest", "Microsoft Windows Server 2012 R2 (64-bit)")
    assert meta.operating_system == "Windows"
    assert meta.operating_system_version == "Server 2012 R2 Standard"


def test_launch_options_defaults():
    lo = m.map_launch_options(vm(), target())
    assert lo.firmware == "UEFI_64"
    assert lo.boot_volume_type == BootVolumeType.PARAVIRTUALIZED
    assert lo.network_type == NetworkType.PARAVIRTUALIZED
    assert lo.is_consistent_volume_naming_enabled


def test_launch_options_source_device_model_does_not_matter():
    """IDE / LSI Logic / e1000 on vSphere still map to virtio in OCI; only firmware follows the source."""
    for controller, nic in (("ide", "e1000"), ("lsilogicsas", "e1000e"), ("buslogic", "pcnet32")):
        v = vm(
            firmware=Firmware.BIOS,
            disks=[DiskSpec(index=0, label="d", device_key=1, capacity_bytes=10, controller_type=controller)],
            nics=[NicSpec(label="n", adapter_type=nic)],
        )
        lo = m.map_launch_options(v, target())
        assert lo.firmware == "BIOS"
        assert lo.boot_volume_type == BootVolumeType.PARAVIRTUALIZED, controller
        assert lo.network_type == NetworkType.PARAVIRTUALIZED, nic


def test_launch_options_compat_mode_and_overrides():
    lo = m.map_launch_options(vm(), target(compatibility_mode=True))
    assert (lo.boot_volume_type, lo.network_type) == (BootVolumeType.IDE, NetworkType.E1000)
    lo = m.map_launch_options(
        vm(), target(compatibility_mode=True, boot_volume_type_override=BootVolumeType.SCSI,
                     network_type_override=NetworkType.VFIO)
    )
    assert (lo.boot_volume_type, lo.network_type) == (BootVolumeType.SCSI, NetworkType.VFIO)


def test_map_shape():
    s = m.map_shape(vm(num_cpu=4, memory_mb=8192), target(), "VM.Standard.E5.Flex")
    assert s == m.ShapeConfig("VM.Standard.E5.Flex", 2.0, 8.0)
    s = m.map_shape(vm(num_cpu=1, memory_mb=512), target(shape="VM.Standard.E4.Flex"), "x")
    assert s == m.ShapeConfig("VM.Standard.E4.Flex", 1.0, 1.0)
    # memory clamped to 64 GB per OCPU
    s = m.map_shape(vm(num_cpu=2, memory_mb=256 * 1024), target(), "x")
    assert s.memory_gb == 64.0
    # odd vCPU count rounds up
    assert m.map_shape(vm(num_cpu=3, memory_mb=4096), target(), "x").ocpus == 2.0


def test_map_shape_user_overrides():
    # explicit sizing wins over the source-derived mapping and is not clamped (OCI validates it)
    s = m.map_shape(vm(num_cpu=4, memory_mb=8192), target(ocpus=6, memory_gb=96), "x")
    assert (s.ocpus, s.memory_gb) == (6.0, 96.0)
    # one side only: the other is still derived, memory keeps following the (overridden) OCPU ratio
    s = m.map_shape(vm(num_cpu=2, memory_mb=256 * 1024), target(ocpus=8), "x")
    assert (s.ocpus, s.memory_gb) == (8.0, 256.0)
    s = m.map_shape(vm(num_cpu=4, memory_mb=8192), target(memory_gb=32), "x")
    assert (s.ocpus, s.memory_gb) == (2.0, 32.0)


def test_is_arm_shape():
    assert m.is_arm_shape("VM.Standard.A1.Flex") and m.is_arm_shape("BM.Standard.A2.150")
    assert not m.is_arm_shape("VM.Standard.E5.Flex")
    assert not m.is_arm_shape("VM.Standard3.Flex")
    assert not m.is_arm_shape("VM.GPU.A10.1")  # Intel host with NVIDIA A10 cards
    assert not m.is_arm_shape("")


def test_volume_size_gb():
    assert m.volume_size_gb(10 * 1024**3) == 50
    assert m.volume_size_gb(50 * 1024**3) == 50
    assert m.volume_size_gb(50 * 1024**3 + 1) == 51
    assert m.volume_size_gb(200 * 1024**3, min_volume_gb=100) == 200


def test_seed_tags():
    tags = m.seed_image_tags(m.map_guest_os("windows2019srvNext_64Guest"), "UEFI_64")
    assert tags == {"oci-umt-seed": "true", "oci-umt-firmware": "UEFI_64", "oci-umt-os": "windows-server-2022-standard",
                    "oci-umt-secure-boot": "false"}
    assert m.seed_image_tags(m.map_guest_os("ubuntu64Guest"), "UEFI_64", secure_boot=True)["oci-umt-secure-boot"] == "true"


def test_platform_config_type_by_shape_family():
    assert m.platform_config_type("VM.Standard.E5.Flex") == m.PLATFORM_AMD_VM
    assert m.platform_config_type("VM.DenseIO.E4.Flex") == m.PLATFORM_AMD_VM
    assert m.platform_config_type("VM.Standard3.Flex") == m.PLATFORM_INTEL_VM
    assert m.platform_config_type("VM.Optimized3.Flex") == m.PLATFORM_INTEL_VM
    assert m.platform_config_type("VM.Standard2.4") == m.PLATFORM_INTEL_VM
    assert m.platform_config_type("BM.Standard.E4.128") == m.PLATFORM_GENERIC_BM
    assert m.platform_config_type("VM.Standard.A1.Flex") is None  # Ampere: no Secure Boot
    assert m.platform_config_type("VM.Standard.A2.Flex") is None
    assert m.platform_config_type("") is None
