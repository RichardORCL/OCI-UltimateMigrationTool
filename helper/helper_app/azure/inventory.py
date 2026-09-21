"""Azure inventory: the VM list for the web UI and ``VmSpec`` extraction for a selected VM.

The rest of the pipeline (OCI provisioning, guest fix-ups, the job view) only knows ``VmSpec``; this
module translates ARM's virtual machine / disk documents into it.  The guest OS is expressed as a
vSphere-style ``guest_id`` + display name so ``oci.mapping.map_guest_os`` works unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from helper_app.azure.client import AzureClient, AzureError
from helper_app.azure.session import AzureSession
from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary

log = logging.getLogger(__name__)

GIB = 1024**3
POWER_STATES = {"running": "poweredOn", "deallocated": "poweredOff", "stopped": "stopped",
                "starting": "starting", "stopping": "stopping", "deallocating": "deallocating"}

_RESOURCE_ID = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)/resourceGroups/(?P<rg>[^/]+)/providers/(?P<provider>[^/]+/[^/]+)/(?P<name>[^/]+)$",
    re.IGNORECASE,
)


def parse_resource_id(resource_id: str) -> dict[str, str]:
    m = _RESOURCE_ID.match(resource_id or "")
    if not m:
        raise AzureError(f"not an Azure resource ID: {resource_id!r}")
    return {"subscription": m.group("sub"), "resource_group": m.group("rg"), "provider": m.group("provider"),
            "name": m.group("name")}


def power_state(vm: dict) -> str:
    for st in ((vm.get("properties") or {}).get("instanceView") or {}).get("statuses") or []:
        code = str(st.get("code") or "")
        if code.lower().startswith("powerstate/"):
            key = code.split("/", 1)[1].lower()
            return POWER_STATES.get(key, key)
    return "unknown"


# --------------------------------------------------------------------------- guest OS guess
_VERSION_IN_SKU = re.compile(r"(\d+)(?:[_.\-](\d+))?")


def _ubuntu_version(text: str) -> str:
    m = re.search(r"(\d\d)[_.](\d\d)", text)
    return f"{m.group(1)}.{m.group(2)}" if m else ""


_GEN_TOKEN = re.compile(r"\bgen\s*[12]\b|-gen[12]\b|_gen[12]\b|\bg[12]\b|\bx86_64\b|\barm64\b")


def _first_number(text: str) -> str:
    """First release-looking number in ``text``; "gen2" / "x86_64" style tokens are not releases."""
    m = _VERSION_IN_SKU.search(_GEN_TOKEN.sub(" ", text))
    return m.group(1) if m else ""


def guess_guest_os(vm: dict, os_disk: Optional[dict] = None) -> tuple[str, str]:
    """(guest_id, guest_full_name) in vSphere terms, from the image reference, the instance view and the
    OS disk.  Both strings are consumed by ``map_guest_os``."""
    props = vm.get("properties") or {}
    storage = props.get("storageProfile") or {}
    img = storage.get("imageReference") or {}
    view = props.get("instanceView") or {}
    os_type = str((storage.get("osDisk") or {}).get("osType") or ((os_disk or {}).get("properties") or {}).get(
        "osType") or "").lower()
    publisher = str(img.get("publisher") or "").lower()
    offer = str(img.get("offer") or "").lower()
    sku = str(img.get("sku") or "").lower()
    os_name = str(view.get("osName") or "").lower()
    os_version = str(view.get("osVersion") or "")
    blob = " ".join([publisher, offer, sku, os_name])

    if os_type == "windows" or "windows" in blob:
        client_text = f"{offer} {sku} {os_name}"
        if "desktop" in publisher or re.search(r"win(dows)?[-_ ]?1[01]\b", client_text):
            rel = "11" if re.search(r"win(dows)?[-_ ]?11\b", client_text) else "10"
            return f"windows{rel}_64Guest", f"Microsoft Windows {rel}"
        m = re.search(r"(20\d\d)(?:[-_]?(r2))?", f"{sku} {offer} {os_version}")
        if m:
            year = m.group(1)
            return f"windows{year}srvGuest", f"Microsoft Windows Server {year}{' R2' if m.group(2) else ''}"
        # release unknown (custom image without metadata): map_guest_os flags the version as undetected so the
        # user picks it in the export form
        return "windowsGuest", "Microsoft Windows"

    text = f"{offer} {sku} {os_name} {os_version}"
    if "ubuntu" in blob or publisher == "canonical":
        ver = _ubuntu_version(sku) or _ubuntu_version(os_version) or _ubuntu_version(offer)
        return "ubuntu64Guest", f"Ubuntu {ver} LTS".replace("  ", " ").strip() if ver else "Ubuntu Linux (64-bit)"
    if "rhel" in blob or "redhat" in publisher or "red hat" in os_name:
        ver = _first_number(sku) or _first_number(os_version)
        return (f"rhel{ver}_64Guest" if ver else "rhel9_64Guest"), f"Red Hat Enterprise Linux {ver}".strip()
    if publisher == "oracle" or "oracle" in offer or os_name in ("ol", "oracle linux") or "oracle" in os_name:
        m = re.search(r"ol(\d)(\d)?", sku) or re.search(r"(\d+)", os_version)
        ver = m.group(1) if m else ""
        return (f"oraclelinux{ver}_64Guest" if ver else "oraclelinux9_64Guest"), f"Oracle Linux {ver}".strip()
    if "centos" in blob:
        ver = _first_number(sku) or _first_number(os_version)
        return (f"centos{ver}_64Guest" if ver else "centos8_64Guest"), f"CentOS {ver}".strip()
    if "rocky" in blob:
        ver = _first_number(sku) or _first_number(os_version)
        return "rockylinux_64Guest", f"Rocky Linux {ver}".strip()
    if "alma" in blob:
        ver = _first_number(sku) or _first_number(os_version)
        return "almalinux_64Guest", f"AlmaLinux {ver}".strip()
    if "sles" in blob or "suse" in blob:
        if "opensuse" in blob:
            return "opensuse64Guest", f"openSUSE {_first_number(sku) or _first_number(os_version)}".strip()
        ver = _first_number(sku) or _first_number(offer.replace("sles-", "sles ")) or _first_number(os_version)
        return (f"sles{ver}_64Guest" if ver else "sles15_64Guest"), f"SUSE Linux Enterprise Server {ver}".strip()
    if "debian" in blob:
        ver = _first_number(sku) or _first_number(os_version)
        return (f"debian{ver}_64Guest" if ver else "debian12_64Guest"), f"Debian {ver}".strip()
    if "fedora" in blob:
        return "fedora64Guest", f"Fedora {_first_number(text)}".strip()
    if "freebsd" in blob:
        ver = _first_number(sku) or _first_number(os_version)
        return (f"freebsd{ver}_64Guest" if ver else "freebsd13_64Guest"), f"FreeBSD {ver}".strip()
    label = " ".join(x for x in (img.get("publisher"), img.get("offer"), img.get("sku")) if x) or os_name or "Linux"
    return "otherLinux64Guest", f"{label} ({os_version})" if os_version else label


# --------------------------------------------------------------------------- VmSpec
def _firmware(vm: dict, os_disk: Optional[dict]) -> Firmware:
    gen = str(((vm.get("properties") or {}).get("instanceView") or {}).get("hyperVGeneration")
              or ((os_disk or {}).get("properties") or {}).get("hyperVGeneration") or "V1")
    return Firmware.EFI if gen.upper() == "V2" else Firmware.BIOS


def _disk_capacity(vm_disk: dict, disk: Optional[dict]) -> int:
    props = (disk or {}).get("properties") or {}
    if props.get("diskSizeBytes"):
        return int(props["diskSizeBytes"])
    gb = props.get("diskSizeGB") or vm_disk.get("diskSizeGB") or 0
    return int(gb) * GIB


def _is_ade(vm_disk: dict, disk: Optional[dict]) -> bool:
    """Azure Disk Encryption (BitLocker / dm-crypt inside the guest): the copy would not boot."""
    coll = ((disk or {}).get("properties") or {}).get("encryptionSettingsCollection") or {}
    legacy = vm_disk.get("encryptionSettings") or {}
    return bool(coll.get("enabled")) or bool(legacy.get("enabled"))


def vm_spec_from_azure(vm: dict, disks: dict[str, dict], sizes: dict[str, dict]) -> VmSpec:
    """``vm`` is the ARM VM document (instance view expanded), ``disks`` maps disk resource ID (lower case)
    -> ARM disk document, ``sizes`` maps VM size name -> ARM size document."""
    props = vm.get("properties") or {}
    storage = props.get("storageProfile") or {}
    os_vm_disk = storage.get("osDisk") or {}
    os_disk_id = str((os_vm_disk.get("managedDisk") or {}).get("id") or "")
    os_disk = disks.get(os_disk_id.lower())
    controller = str(storage.get("diskControllerType") or "SCSI").lower()

    disk_specs: list[DiskSpec] = []
    encrypted: list[str] = []

    def add(vm_disk: dict, label: str, lun: int) -> None:
        did = str((vm_disk.get("managedDisk") or {}).get("id") or "")
        doc = disks.get(did.lower())
        spec = DiskSpec(index=len(disk_specs), label=label, device_key=lun, capacity_bytes=_disk_capacity(vm_disk, doc),
                        controller_type=controller, controller_class="AzureManagedDisk", controller_bus=0,
                        unit_number=lun, thin_provisioned=True, backing_file=did)
        if _is_ade(vm_disk, doc):
            encrypted.append(label)
        disk_specs.append(spec)

    add(os_vm_disk, os_vm_disk.get("name") or "OS disk", -1)
    for data in sorted(storage.get("dataDisks") or [], key=lambda d: int(d.get("lun", 0))):
        add(data, data.get("name") or f"LUN {data.get('lun')}", int(data.get("lun", 0)))

    size_name = str((props.get("hardwareProfile") or {}).get("vmSize") or "")
    size = sizes.get(size_name) or {}
    sec = props.get("securityProfile") or {}
    uefi = sec.get("uefiSettings") or {}
    guest_id, full_name = guess_guest_os(vm, os_disk)
    nics = [NicSpec(label=str(n.get("id", "")).rsplit("/", 1)[-1], adapter_type="azure-vnic")
            for n in ((props.get("networkProfile") or {}).get("networkInterfaces") or [])]
    return VmSpec(
        moid=str(vm.get("id") or "").lower(),
        name=str(vm.get("name") or ""),
        instance_uuid=str(props.get("vmId") or ""),
        num_cpu=int(size.get("numberOfCores") or 0) or 1,
        memory_mb=int(size.get("memoryInMB") or 0) or 1024,
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=_firmware(vm, os_disk),
        secure_boot=bool(uefi.get("secureBootEnabled")),
        has_vtpm=bool(uefi.get("vTpmEnabled")),
        power_state=power_state(vm),
        has_snapshots=False,
        host_name="",  # no ESXi host; the region lives in AzureSourceInfo.location
        encrypted=False,
        encrypted_disks=encrypted,
        disks=disk_specs,
        nics=nics,
    )


def vm_summary_from_azure(vm: dict, subscription_name: str) -> VmSummary:
    props = vm.get("properties") or {}
    storage = props.get("storageProfile") or {}
    os_disk = storage.get("osDisk") or {}
    data = storage.get("dataDisks") or []
    capacity = sum(int(d.get("diskSizeGB") or 0) for d in [os_disk, *data]) * GIB
    guest_id, full_name = guess_guest_os(vm)
    ids = parse_resource_id(str(vm.get("id") or ""))
    return VmSummary(
        moid=str(vm.get("id") or "").lower(),
        name=str(vm.get("name") or ""),
        folder=f"{subscription_name or ids['subscription']}/{ids['resource_group']}",
        power_state=power_state(vm),
        guest_full_name=full_name,
        guest_id=guest_id,
        num_cpu=0,
        memory_mb=0,
        num_disks=1 + len(data),
        disk_capacity_bytes=capacity,
        is_template=False,
        encrypted=bool((os_disk.get("encryptionSettings") or {}).get("enabled")),
        vm_size=str((props.get("hardwareProfile") or {}).get("vmSize") or ""),
        location=str(vm.get("location") or ""),
    )


# --------------------------------------------------------------------------- session level helpers
def list_vm_summaries(session: AzureSession) -> list[VmSummary]:
    rows: list[VmSummary] = []
    for sub in session.subscriptions:
        try:
            listed = session.client.list_vms(sub.id)
        except AzureError as exc:
            log.warning("listing VMs of subscription %s failed: %s", sub.id, exc)
            continue
        for vm in listed:
            vm_id = str(vm.get("id") or "")
            if vm_id:
                try:
                    vm = session.client.get_vm(vm_id)
                except AzureError as exc:
                    log.warning("get VM %s failed: %s", vm_id.rsplit("/", 1)[-1], exc)
                    try:
                        view = session.client.get_vm_instance_view(vm_id)
                        merged = dict(vm)
                        props = merged.setdefault("properties", {})
                        props["instanceView"] = view.get("properties") if isinstance(view.get("properties"), dict) else view
                        vm = merged
                    except AzureError as exc2:
                        log.warning("instance view of %s unavailable: %s", vm_id.rsplit("/", 1)[-1], exc2)
            rows.append(vm_summary_from_azure(vm, sub.name))
    rows.sort(key=lambda r: (r.folder.lower(), r.name.lower()))
    return rows


def _sizes_for(client: AzureClient, cache: dict, subscription: str, location: str) -> dict[str, dict]:
    key = (subscription, location.lower())
    if key not in cache:
        try:
            cache[key] = client.list_vm_sizes(subscription, location)
        except AzureError as exc:
            log.warning("VM sizes of %s unavailable: %s", location, exc)
            cache[key] = {}
    return cache[key]


class AzureVmDetails:
    """Everything the API and the job need about one VM: the spec plus the raw documents behind it."""

    def __init__(self, vm: dict, disks: dict[str, dict], spec: VmSpec):
        self.vm = vm
        self.disks = disks
        self.spec = spec
        ids = parse_resource_id(spec.moid)
        self.subscription_id = ids["subscription"]
        self.resource_group = ids["resource_group"]
        self.location = str(vm.get("location") or "")
        self.vm_size = str(((vm.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize") or "")

    @property
    def disk_ids(self) -> list[str]:
        return [d.backing_file for d in self.spec.disks]

    def disk_doc(self, disk_id: str) -> dict:
        return self.disks.get(disk_id.lower()) or {}

    @property
    def security_type(self) -> str:
        return str(((self.vm.get("properties") or {}).get("securityProfile") or {}).get("securityType") or "")

    @property
    def ephemeral_os_disk(self) -> bool:
        os_disk = ((self.vm.get("properties") or {}).get("storageProfile") or {}).get("osDisk") or {}
        return bool((os_disk.get("diffDiskSettings") or {}).get("option"))


def inspect_vm(session: AzureSession, vm_id: str, size_cache: Optional[dict] = None) -> AzureVmDetails:
    client = session.client
    ids = parse_resource_id(vm_id)
    vm = client.get_vm(vm_id)
    storage = (vm.get("properties") or {}).get("storageProfile") or {}
    disks: dict[str, dict] = {}
    for vm_disk in [storage.get("osDisk") or {}, *(storage.get("dataDisks") or [])]:
        did = str((vm_disk.get("managedDisk") or {}).get("id") or "")
        if not did:
            continue  # unmanaged (storage account) disk: preflight refuses the VM
        try:
            disks[did.lower()] = client.get_disk(did)
        except AzureError as exc:
            log.warning("disk %s of %s unreadable: %s", did.rsplit("/", 1)[-1], vm.get("name"), exc)
    sizes = _sizes_for(client, size_cache if size_cache is not None else {}, ids["subscription"],
                       str(vm.get("location") or ""))
    return AzureVmDetails(vm, disks, vm_spec_from_azure(vm, disks, sizes))
