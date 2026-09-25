"""Translate the vSphere VM description into OCI concepts.

* guestId / guestFullName      -> custom image operatingSystem / operatingSystemVersion
* firmware / controller / NIC  -> LaunchOptions (firmware, bootVolumeType, networkType)
* vCPU / RAM                   -> flex shape OCPUs and memory
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Optional

from helper_app.branding import TAG_FIRMWARE, TAG_OS, TAG_SECURE_BOOT, TAG_SEED
from helper_app.models import BootVolumeType, Firmware, LaunchOptionsSpec, NetworkType, OciTarget, VmSpec

OCI_FIRMWARE_BIOS = "BIOS"
OCI_FIRMWARE_UEFI = "UEFI_64"


@dataclass(frozen=True)
class OsMetadata:
    operating_system: str
    operating_system_version: str
    family: str  # "linux" | "windows"
    # False when vSphere does not encode the release (e.g. ubuntu64Guest / "Ubuntu Linux (64-bit)") and the
    # version is only a guess; the UI then asks the user to pick it
    version_detected: bool = True

    @property
    def is_windows(self) -> bool:
        return self.family == "windows"

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", f"{self.operating_system}-{self.operating_system_version}".lower()).strip("-")


# vSphere guestId (lowercase, without the trailing "guest") -> (OS, version)
# NB: VMware names a new server release "<previous>srvNext" until the next major vSphere release, so
#     windows2019srvNext = Windows Server 2022 (vSphere 7.0 U2+) and windows2022srvNext = Server 2025 (8.0 U2+).
_WINDOWS_VERSIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"windows2025srv"), "Server 2025 Standard"),
    (re.compile(r"windows2022srvnext"), "Server 2025 Standard"),
    (re.compile(r"windows2022srv"), "Server 2022 Standard"),
    (re.compile(r"windows2019srvnext"), "Server 2022 Standard"),
    (re.compile(r"windows2019srv"), "Server 2019 Standard"),
    (re.compile(r"windows9srv|windows9server"), "Server 2016 Standard"),
    (re.compile(r"windows8srv|windows8server"), "Server 2012 R2 Standard"),
    (re.compile(r"windows7srv|windows7server"), "Server 2008 R2 Standard"),
    # client editions: OCI's CreateImage/UpdateImage only accept the catalog names "Windows10" / "Windows11"
    # ("Invalid operatingSystemVersion: 10 Enterprise (The operating system version is not supported.)")
    (re.compile(r"windows1[12]_64|windows11|windows12"), "Windows11"),
    (re.compile(r"windows9_64|windows9"), "Windows10"),
]

WINDOWS_CLIENT_VERSIONS = {"Windows10", "Windows11"}

_LINUX_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    # pattern, operating_system, fixed version (None -> derive from the numeric suffix)
    (re.compile(r"^oraclelinux(\d+)"), "Oracle Linux", None),
    (re.compile(r"^rhel(\d+)"), "Red Hat Enterprise Linux", None),
    (re.compile(r"^centos(\d+)"), "CentOS", None),
    (re.compile(r"^rockylinux"), "Rocky Linux", "9"),
    (re.compile(r"^almalinux"), "AlmaLinux", "9"),
    (re.compile(r"^ubuntu"), "Ubuntu", "22.04"),
    (re.compile(r"^debian(\d+)"), "Debian", None),
    (re.compile(r"^sles(\d+)"), "SUSE Linux Enterprise Server", None),
    (re.compile(r"^opensuse"), "openSUSE", "15"),
    (re.compile(r"^fedora"), "Fedora", "40"),
    (re.compile(r"^amazonlinux(\d+)"), "Amazon Linux", None),
    (re.compile(r"^freebsd(\d+)"), "FreeBSD", None),
]

_FULLNAME_VERSION = re.compile(r"(\d+(?:\.\d+)?)")
_BITNESS = re.compile(r"\(?\b(32|64)-bit\)?")


def _no_bitness(full_name: str) -> str:
    """'Rocky Linux (64-bit)' -> 'Rocky Linux ' so the bitness is not mistaken for a release."""
    return _BITNESS.sub("", full_name)


def map_guest_os(guest_id: str, guest_full_name: str = "") -> OsMetadata:
    gid = (guest_id or "").lower()
    gid = gid[:-5] if gid.endswith("guest") else gid
    full = (guest_full_name or "").lower()

    if gid.startswith("windows") or "windows" in full:
        # The release year vCenter displays (guestFullName) is unambiguous; the guestId encoding is not
        # (e.g. windows2019srvNext is Server 2022), so the full name wins when it names a server release.
        m = re.search(r"server\s+(20\d\d)(\s*r2)?", full)
        if m:
            ver = f"Server {m.group(1)}{' R2' if m.group(2) else ''} Standard"
            return OsMetadata("Windows", ver, "windows")
        # client editions likewise ("Microsoft Windows 11 (64-bit)"); vSphere still reports Windows 11 as
        # windows9_64Guest on older releases, so the displayed name is the reliable source
        m = re.search(r"windows\s+(10|11)\b", full)
        if m:
            return OsMetadata("Windows", f"Windows{m.group(1)}", "windows")
        for pattern, version in _WINDOWS_VERSIONS:
            if pattern.search(gid):
                return OsMetadata("Windows", version, "windows")
        return OsMetadata("Windows", "Server 2019 Standard", "windows", version_detected=False)

    for pattern, os_name, fixed in _LINUX_RULES:
        m = pattern.search(gid)
        if m:
            version = fixed
            detected = version is None  # numeric suffix in the guestId names the release
            if version is None:
                version = m.group(1)
            # guestIds without a release (ubuntu64Guest, rockylinux_64Guest, ...): the display name may
            # carry it ("Ubuntu 24.04 LTS", "Rocky Linux 9") when VMware Tools reported it
            fm = re.search(r"(\d\d\.\d\d)" if os_name == "Ubuntu" else r"\b(\d+)(?:\.\d+)?\b", _no_bitness(full))
            if not detected and fm:
                version, detected = fm.group(1), True
            return OsMetadata(os_name, version, "linux", version_detected=detected)

    fm = _FULLNAME_VERSION.search(_no_bitness(full))
    return OsMetadata("Custom Linux", fm.group(1) if fm else "unknown", "linux", version_detected=fm is not None)


def map_ovf_description(description: str = "", product: str = "", version: str = "") -> OsMetadata:
    """Map VMware OVF ``OperatingSystemSection`` / ``ProductSection`` text to OCI image metadata."""
    parts = [p.strip() for p in (description, product, version) if p and p.strip()]
    text = " ".join(parts)
    if not text:
        return OsMetadata("Custom Linux", "unknown", "linux", version_detected=False)

    low = text.lower()
    if "windows" in low:
        m = re.search(r"server\s+(20\d\d)(\s*r2)?", low)
        if m:
            ver = f"Server {m.group(1)}{' R2' if m.group(2) else ''} Standard"
            return OsMetadata("Windows", ver, "windows")
        m = re.search(r"windows\s+(10|11)\b", low)
        if m:
            return OsMetadata("Windows", f"Windows{m.group(1)}", "windows")
        m = re.search(r"\b(20\d\d)\b", low)
        if m and "server" in low:
            return OsMetadata("Windows", f"Server {m.group(1)} Standard", "windows", version_detected=True)
        return OsMetadata("Windows", "Server 2019 Standard", "windows", version_detected=False)

    name_rules: list[tuple[str, str, str | None]] = [
        ("oracle linux", "Oracle Linux", None),
        ("red hat enterprise", "Red Hat Enterprise Linux", None),
        ("rhel", "Red Hat Enterprise Linux", None),
        ("centos", "CentOS", None),
        ("rocky linux", "Rocky Linux", None),
        ("almalinux", "AlmaLinux", None),
        ("ubuntu", "Ubuntu", None),
        ("debian", "Debian", None),
        ("suse linux enterprise", "SUSE Linux Enterprise Server", None),
        ("opensuse", "openSUSE", "15"),
        ("fedora", "Fedora", None),
        ("amazon linux", "Amazon Linux", None),
        ("freebsd", "FreeBSD", None),
    ]
    plain = _no_bitness(text)
    for needle, os_name, fixed in name_rules:
        if needle in low:
            if fixed is not None:
                return OsMetadata(os_name, fixed, "linux", version_detected=True)
            fm = re.search(r"(\d\d\.\d\d)" if os_name == "Ubuntu" else r"\b(\d+(?:\.\d+)?)\b", plain)
            if not fm and version.strip():
                fm = re.search(r"(\d+(?:\.\d+)?)", version)
            if fm:
                return OsMetadata(os_name, fm.group(1), "linux", version_detected=True)
            return OsMetadata(os_name, "unknown", "linux", version_detected=False)

    fm = _FULLNAME_VERSION.search(plain)
    return OsMetadata("Custom Linux", fm.group(1) if fm else "unknown", "linux", version_detected=fm is not None)


# Releases OCI knows for the image metadata (operatingSystem / operatingSystemVersion); offered as the
# choices when vSphere does not tell us which one the guest runs.  OCI does not publish a closed list for
# Linux, so these are the releases with OCI platform images / documented custom image support.
OS_VERSION_CHOICES: dict[str, list[str]] = {
    "Ubuntu": ["18.04", "20.04", "22.04", "24.04", "26.04"],
    "Rocky Linux": ["8", "9", "10"],
    "AlmaLinux": ["8", "9", "10"],
    "openSUSE": ["15"],
    "Fedora": ["40", "41", "42", "43", "44"],
    "Oracle Linux": ["6", "7", "8", "9", "10"],
    "Red Hat Enterprise Linux": ["7", "8", "9", "10"],
    "CentOS": ["7", "8"],
    "Debian": ["10", "11", "12", "13"],
    "SUSE Linux Enterprise Server": ["12", "15", "16"],
    "Windows": [
        "Server 2012 R2 Standard", "Server 2016 Standard", "Server 2019 Standard", "Server 2022 Standard",
        "Server 2025 Standard", "Windows10", "Windows11",
    ],
}


def normalize_os_version_for_oci(operating_system: str, version: str) -> str:
    """Map a detected or user-entered release to an entry from ``OS_VERSION_CHOICES`` when possible."""
    version = (version or "").strip()
    choices = OS_VERSION_CHOICES.get(operating_system, [])
    if not choices or not version or version == "unknown":
        return version
    if version in choices:
        return version
    if operating_system == "Windows":
        low = version.lower().replace("_", " ")
        if re.search(r"\bwindows\s*10\b|\bwin\s*10\b", low):
            return "Windows10"
        if re.search(r"\bwindows\s*11\b|\bwin\s*11\b", low):
            return "Windows11"
        m = re.search(r"(20\d{2})\s*r2", low)
        if m:
            cand = f"Server {m.group(1)} R2 Standard"
            if cand in choices:
                return cand
        m = re.search(r"(20\d{2})", low)
        if m:
            year = m.group(1)
            for c in choices:
                if year in c:
                    return c
    if operating_system == "Ubuntu":
        m = re.search(r"(\d{2}\.\d{2})", version)
        if m and m.group(1) in choices:
            return m.group(1)
    # Linux major release: "9.8", "9.8.0" -> "9" when that is the catalog entry
    major = version.split(".")[0]
    if major in choices:
        return major
    for c in choices:
        if version.startswith(c + ".") or version.startswith(c + "-"):
            return c
    return version


def os_version_choices(os_meta: OsMetadata) -> list[str]:
    """Selectable releases for the guest's OS (empty when we have no catalog, e.g. Custom Linux).  A version
    detected from vSphere that is not in the catalog is kept selectable."""
    choices = list(OS_VERSION_CHOICES.get(os_meta.operating_system, []))
    if choices and os_meta.version_detected and os_meta.operating_system_version not in choices:
        choices.append(os_meta.operating_system_version)
    return choices


def with_os_version(os_meta: OsMetadata, version: Optional[str]) -> OsMetadata:
    """Apply the release the user picked in the target form (no-op when empty)."""
    if not version:
        return os_meta
    return replace(os_meta, operating_system_version=version.strip(), version_detected=True)


def resolve_target_os(guest_id: str, guest_full_name: str, operating_system: Optional[str],
                      operating_system_version: Optional[str]) -> OsMetadata:
    """Detected guest OS, with the target form's OS and release applied over it.

    ``operating_system`` empty keeps the detected family and only applies a release override.
    A chosen family (Ubuntu, Oracle Linux, Windows, …) replaces the detection; the release is
    normalized onto the OCI catalog when that catalog has an entry for it.
    """
    detected = map_guest_os(guest_id, guest_full_name)
    name = (operating_system or "").strip()
    version = (operating_system_version or "").strip()
    if not name:
        return with_os_version(detected, version or None)
    family = "windows" if name.lower().startswith("windows") else "linux"
    if not version and name == detected.operating_system:
        version = detected.operating_system_version
    if version:
        version = normalize_os_version_for_oci(name, version)
    return OsMetadata(name, version or "unknown", family, version_detected=bool(version and version != "unknown"))


def oci_firmware(firmware: Firmware) -> str:
    return OCI_FIRMWARE_UEFI if firmware == Firmware.EFI else OCI_FIRMWARE_BIOS


def remote_data_volume_type_for(boot_type: BootVolumeType) -> str:
    """OCI refuses to mix paravirtualized and emulated volumes in one instance, so the data volumes follow
    the boot volume's device class: virtio with virtio, emulated SCSI with IDE/SCSI, iSCSI with iSCSI."""
    if boot_type == BootVolumeType.PARAVIRTUALIZED:
        return "PARAVIRTUALIZED"
    if boot_type == BootVolumeType.ISCSI:
        return "ISCSI"
    return "SCSI"


def is_emulated(boot_type: BootVolumeType, net_type: NetworkType) -> bool:
    return boot_type != BootVolumeType.PARAVIRTUALIZED or net_type == NetworkType.E1000


def map_launch_options(vm: VmSpec, target: OciTarget) -> LaunchOptionsSpec:
    """Device model for the target.  Always paravirtualized (virtio) - the vSphere controller / NIC model says
    nothing about what the guest can drive in OCI, and virtio is what OCI images run on.  Only the
    *Maximum compatibility* preset (IDE + E1000, for guests without virtio drivers) or an explicit override
    picks emulated devices."""
    boot_type = BootVolumeType.PARAVIRTUALIZED
    net_type = NetworkType.PARAVIRTUALIZED
    if target.compatibility_mode:
        boot_type = BootVolumeType.IDE
        net_type = NetworkType.E1000
    if target.boot_volume_type_override:
        boot_type = target.boot_volume_type_override
    if target.network_type_override:
        net_type = target.network_type_override
    firmware = oci_firmware(vm.firmware)
    windows = vm.is_windows or map_guest_os(vm.guest_id, vm.guest_full_name).is_windows
    return LaunchOptionsSpec(
        firmware=firmware,
        boot_volume_type=boot_type,
        network_type=net_type,
        remote_data_volume_type=remote_data_volume_type_for(boot_type),
        # consistent device paths (/dev/oracleoci/...) exist for Linux guests only
        is_consistent_volume_naming_enabled=not windows,
        # Secure Boot only exists with UEFI; vSphere reports the flag on EFI VMs only, but stay defensive
        secure_boot=bool(vm.secure_boot) and firmware == OCI_FIRMWARE_UEFI,
    )


# OCI "platform config" type per shape family; Secure Boot (shielded instances) is switched on there.
PLATFORM_AMD_VM = "AMD_VM"
PLATFORM_INTEL_VM = "INTEL_VM"
PLATFORM_GENERIC_BM = "GENERIC_BM"


def platform_config_type(shape: str) -> Optional[str]:
    """Which ``LaunchInstancePlatformConfig`` subtype a shape takes, or None when the shape family has no
    platform config with Secure Boot (Ampere A1/A2 and other ARM shapes)."""
    s = (shape or "").upper()
    if is_arm_shape(s):
        return None  # Ampere (ARM), VM and BM alike; note VM.GPU.A10.* is an Intel host with NVIDIA A10 cards
    if s.startswith("BM."):
        return PLATFORM_GENERIC_BM
    if not s.startswith("VM."):
        return None
    family = s[3:]  # e.g. STANDARD.E5.FLEX, STANDARD3.FLEX, DENSEIO2.8
    if re.search(r"\.E\d", family):
        return PLATFORM_AMD_VM  # E2..E6 AMD EPYC
    return PLATFORM_INTEL_VM  # Standard2/3, Optimized3, DenseIO2, GPU shapes: Intel


@dataclass(frozen=True)
class ShapeConfig:
    shape: str
    ocpus: float
    memory_gb: float


def map_shape(
    vm: VmSpec,
    target: OciTarget,
    default_shape: str,
    max_memory_gb_per_ocpu: int = 64,
    min_memory_gb_per_ocpu: int = 1,
) -> ShapeConfig:
    shape = target.shape or default_shape
    # OCI counts OCPUs (physical cores); a vCPU is one hardware thread -> 2 vCPU per OCPU.
    auto_ocpus = max(1, math.ceil(vm.num_cpu / 2))
    ocpus = float(target.ocpus) if target.ocpus else float(auto_ocpus)
    if target.memory_gb:
        memory_gb = float(target.memory_gb)  # explicit: OCI validates it against the shape
    else:
        memory_gb = max(1, math.ceil(vm.memory_mb / 1024))
        memory_gb = min(max(memory_gb, ocpus * min_memory_gb_per_ocpu), ocpus * max_memory_gb_per_ocpu)
    return ShapeConfig(shape=shape, ocpus=ocpus, memory_gb=float(memory_gb))


def is_arm_shape(shape: str) -> bool:
    """Ampere (A1/A2, ...) shapes run aarch64 only; an x86 guest copied from vSphere cannot boot on them."""
    return bool(re.match(r"^(VM|BM)\.STANDARD\.A\d", (shape or "").upper()))


def is_bare_metal_shape(shape: str) -> bool:
    """Bare metal shapes have fixed cores and memory: no ``shapeConfig`` at launch."""
    return (shape or "").upper().startswith("BM.")


def volume_size_gb(capacity_bytes: int, min_volume_gb: int = 50) -> int:
    return max(min_volume_gb, math.ceil(capacity_bytes / 1024**3))


SEED_SECURE_BOOT_TAG = TAG_SECURE_BOOT
# tags older seed images may lack, with the value they implicitly had (so they keep being reused)
SEED_TAG_DEFAULTS = {SEED_SECURE_BOOT_TAG: "false"}


def seed_image_tags(os_meta: OsMetadata, firmware: str, secure_boot: bool = False) -> dict[str, str]:
    """Identity of a seed image.  Secure Boot is part of it: the image's capability schema must declare
    ``Compute.SecureBoot`` for OCI to accept a shielded launch from it."""
    return {
        TAG_SEED: "true",
        TAG_FIRMWARE: firmware,
        TAG_OS: os_meta.slug,
        SEED_SECURE_BOOT_TAG: "true" if secure_boot else "false",
    }
