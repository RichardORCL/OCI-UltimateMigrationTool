"""EC2 inventory: the instance list for the web UI and ``VmSpec`` extraction for a selected instance."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from helper_app.aws.client import AwsClient, AwsError
from helper_app.aws.session import AwsSession
from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary

log = logging.getLogger(__name__)

GIB = 1024**3
POWER_STATES = {
    "running": "poweredOn",
    "stopped": "poweredOff",
    "stopping": "stopping",
    "pending": "pending",
    "shutting-down": "shutting-down",
    "terminated": "terminated",
}


def instance_arn(region: str, account: str, instance_id: str) -> str:
    return f"arn:aws:ec2:{region}:{account}:instance/{instance_id}"


def parse_instance_arn(moid: str) -> dict[str, str]:
    m = re.match(r"^arn:aws:ec2:([^:]+):(\d+):instance/(i-[0-9a-z]+)$", (moid or "").strip(), re.I)
    if not m:
        raise AwsError(f"not an EC2 instance ARN: {moid!r}", status=400)
    return {"region": m.group(1), "account": m.group(2), "instance_id": m.group(3)}


def _state_name(inst: dict) -> str:
    state = inst.get("instanceState") or inst.get("state") or {}
    if isinstance(state, dict):
        return str(state.get("name") or "").lower()
    return str(state).lower()


def power_state(inst: dict) -> str:
    return POWER_STATES.get(_state_name(inst), _state_name(inst) or "unknown")


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _tag_name(inst: dict) -> str:
    tags = inst.get("tagSet") or inst.get("tags")
    rows = tags if isinstance(tags, list) else _as_list((tags or {}).get("item") if isinstance(tags, dict) else tags)
    for tag in rows:
        if isinstance(tag, dict) and str(tag.get("key") or tag.get("Key") or "") == "Name":
            return str(tag.get("value") or tag.get("Value") or "")
    return ""


def _block_devices(inst: dict) -> list[dict]:
    mapping = inst.get("blockDeviceMapping")
    rows = (
        mapping
        if isinstance(mapping, list)
        else _as_list((mapping or {}).get("item") if isinstance(mapping, dict) else mapping)
    )
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ebs = row.get("ebs") if isinstance(row.get("ebs"), dict) else {}
        out.append(
            {
                "device": str(row.get("deviceName") or row.get("device") or ""),
                "volume_id": str(ebs.get("volumeId") or row.get("volumeId") or ""),
                "status": str(ebs.get("status") or ""),
            }
        )
    return [d for d in out if d["volume_id"]]


def _product_codes(inst: dict) -> list[str]:
    raw = inst.get("productCodes") or inst.get("productCodesSet")
    rows = raw if isinstance(raw, list) else _as_list((raw or {}).get("item") if isinstance(raw, dict) else raw)
    codes = []
    for row in rows:
        if isinstance(row, dict):
            code = str(row.get("productCode") or row.get("productCodeId") or "")
            if code:
                codes.append(code)
        elif row:
            codes.append(str(row))
    return codes


def _first_number(text: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return m.group(1) if m else ""


def guess_guest_os(inst: dict, image: Optional[dict] = None) -> tuple[str, str]:
    platform = str(inst.get("platform") or "").lower()
    details = str(inst.get("platformDetails") or "").lower()
    image = image or {}
    name = str(image.get("name") or "").lower()
    desc = str(image.get("description") or "").lower()
    blob = " ".join([platform, details, name, desc])

    if platform == "windows" or "windows" in blob:
        client_text = f"{name} {desc} {details}"
        if (
            re.search(r"win(dows)?[-_ ]?1[01]\b", client_text)
            or "windows 10" in client_text
            or "windows 11" in client_text
        ):
            rel = "11" if "11" in client_text else "10"
            return f"windows{rel}_64Guest", f"Microsoft Windows {rel}"
        m = re.search(r"(20\d\d)", client_text)
        if m:
            year = m.group(1)
            return f"windows{year}srvGuest", f"Microsoft Windows Server {year}"
        return "windowsGuest", "Microsoft Windows"

    if "ubuntu" in blob:
        ver = _first_number(name) or _first_number(desc)
        if re.search(r"(\d\d)[._](\d\d)", f"{name} {desc}"):
            m = re.search(r"(\d\d)[._](\d\d)", f"{name} {desc}")
            ver = f"{m.group(1)}.{m.group(2)}" if m else ver
        return "ubuntu64Guest", f"Ubuntu {ver} LTS".replace("  ", " ").strip() if ver else "Ubuntu Linux (64-bit)"
    if "amazon linux" in blob or "amzn" in blob or "al2023" in blob or "al2" in name:
        ver = "2023" if "2023" in blob else (_first_number(name) or "2")
        return f"amazonlinux{ver}_64Guest", f"Amazon Linux {ver}"
    if "rhel" in blob or "red hat" in blob:
        ver = _first_number(name) or _first_number(desc)
        return (f"rhel{ver}_64Guest" if ver else "rhel9_64Guest"), f"Red Hat Enterprise Linux {ver}".strip()
    if "oracle" in blob:
        ver = _first_number(name) or _first_number(desc)
        return (f"oraclelinux{ver}_64Guest" if ver else "oraclelinux9_64Guest"), f"Oracle Linux {ver}".strip()
    if "debian" in blob:
        ver = _first_number(name) or _first_number(desc)
        return (f"debian{ver}_64Guest" if ver else "debian12_64Guest"), f"Debian {ver}".strip()
    if "sles" in blob or "suse" in blob:
        ver = _first_number(name) or _first_number(desc)
        return (f"sles{ver}_64Guest" if ver else "sles15_64Guest"), f"SUSE Linux Enterprise Server {ver}".strip()
    if "centos" in blob:
        ver = _first_number(name) or _first_number(desc)
        return (f"centos{ver}_64Guest" if ver else "centos8_64Guest"), f"CentOS {ver}".strip()
    if "rocky" in blob:
        ver = _first_number(name) or _first_number(desc)
        return "rockylinux_64Guest", f"Rocky Linux {ver}".strip()
    label = str(image.get("name") or image.get("description") or inst.get("platformDetails") or "Linux")
    return "otherLinux64Guest", label


def _firmware(inst: dict) -> Firmware:
    mode = str(inst.get("currentInstanceBootMode") or inst.get("bootMode") or "").lower()
    return Firmware.EFI if mode in ("uefi", "uefi-preferred") else Firmware.BIOS


def _vcpu_memory(inst_type: dict) -> tuple[int, int]:
    vcpu = inst_type.get("vCpuInfo") or inst_type.get("vcpuInfo") or {}
    if isinstance(vcpu, dict):
        cores = int(vcpu.get("defaultVCpus") or vcpu.get("defaultVcpus") or 0)
    else:
        cores = int(inst_type.get("vcpus") or 0)
    mem = inst_type.get("memoryInfo") or {}
    if isinstance(mem, dict) and mem.get("sizeInMiB"):
        memory_mb = int(mem["sizeInMiB"])
    else:
        memory_mb = int(inst_type.get("memoryInMiB") or 0)
    return max(cores, 1), max(memory_mb, 1024)


def vm_spec_from_aws(
    inst: dict, volumes: dict[str, dict], inst_type: dict, image: Optional[dict], region: str, account: str
) -> VmSpec:
    instance_id = str(inst.get("instanceId") or "")
    devices = _block_devices(inst)
    root_name = str(inst.get("rootDeviceName") or "")
    root = next((d for d in devices if d["device"] == root_name), devices[0] if devices else None)
    ordered = ([root] if root else []) + [d for d in devices if d is not root]
    disks: list[DiskSpec] = []
    for i, dev in enumerate(ordered):
        vol = volumes.get(dev["volume_id"]) or {}
        gb = int(vol.get("size") or 0)
        disks.append(
            DiskSpec(
                index=i,
                label=dev["device"] or f"vol {i}",
                device_key=i,
                capacity_bytes=gb * GIB if gb else 0,
                controller_type="nvme" if "nvme" in (dev["device"] or "").lower() else "scsi",
                controller_class="EBS",
                controller_bus=0,
                unit_number=i,
                thin_provisioned=True,
                backing_file=dev["volume_id"],
            )
        )
    guest_id, full_name = guess_guest_os(inst, image)
    nics = [
        NicSpec(label=str(n.get("networkInterfaceId") or n.get("networkInterfaceId") or "eni"), adapter_type="ena")
        for n in _as_list(
            (inst.get("networkInterfaceSet") or {}).get("item")
            if isinstance(inst.get("networkInterfaceSet"), dict)
            else inst.get("networkInterfaceSet")
        )
    ]
    if not nics and inst.get("vpcId"):
        nics = [NicSpec(label=str(inst.get("vpcId")), adapter_type="ena")]
    cores, memory_mb = _vcpu_memory(inst_type)
    tpm = str(inst.get("tpmSupport") or "").lower() in ("v2.0", "2.0", "true")
    return VmSpec(
        moid=instance_arn(region, account, instance_id),
        name=_tag_name(inst) or instance_id,
        instance_uuid=instance_id,
        num_cpu=cores,
        memory_mb=memory_mb,
        guest_id=guest_id,
        guest_full_name=full_name,
        firmware=_firmware(inst),
        secure_boot=bool(inst.get("secureBoot")),
        has_vtpm=tpm,
        power_state=power_state(inst),
        has_snapshots=False,
        host_name=str((inst.get("placement") or {}).get("availabilityZone") or ""),
        encrypted=False,
        encrypted_disks=[],
        disks=disks,
        nics=nics or [NicSpec(label="eni", adapter_type="ena")],
    )


def vm_summary_from_aws(inst: dict, image: Optional[dict], region: str, account: str) -> VmSummary:
    devices = _block_devices(inst)
    guest_id, full_name = guess_guest_os(inst, image)
    az = str((inst.get("placement") or {}).get("availabilityZone") or region)
    vpc = str(inst.get("vpcId") or "")
    return VmSummary(
        moid=instance_arn(region, account, str(inst.get("instanceId") or "")),
        name=_tag_name(inst) or str(inst.get("instanceId") or ""),
        folder=f"{region}/{vpc or az}",
        power_state=power_state(inst),
        guest_full_name=full_name,
        guest_id=guest_id,
        num_cpu=0,
        memory_mb=0,
        num_disks=len(devices) or 1,
        disk_capacity_bytes=0,
        is_template=False,
        encrypted=False,
        vm_size=str(inst.get("instanceType") or ""),
        location=az,
    )


def list_vm_summaries(session: AwsSession) -> list[VmSummary]:
    client = session.client
    instances = client.describe_instances()
    image_ids = list({str(i.get("imageId") or "") for i in instances if i.get("imageId")})
    images = client.describe_images(image_ids)
    rows = [
        vm_summary_from_aws(inst, images.get(str(inst.get("imageId") or "")), session.region, session.account_id)
        for inst in instances
        if _state_name(inst) != "terminated"
    ]
    rows.sort(key=lambda r: (r.folder.lower(), r.name.lower()))
    return rows


class AwsVmDetails:
    def __init__(self, inst: dict, volumes: dict[str, dict], spec: VmSpec, image: Optional[dict] = None):
        self.inst = inst
        self.volumes = volumes
        self.spec = spec
        self.image = image or {}
        self.instance_id = str(inst.get("instanceId") or "")
        self.instance_type = str(inst.get("instanceType") or "")
        ids = parse_instance_arn(spec.moid)
        self.region = ids["region"]
        self.account_id = ids["account"]

    @property
    def volume_ids(self) -> list[str]:
        return [d.backing_file for d in self.spec.disks if d.backing_file]

    @property
    def root_device_type(self) -> str:
        return str(self.inst.get("rootDeviceType") or "").lower()

    @property
    def product_codes(self) -> list[str]:
        return _product_codes(self.inst)

    def volume_doc(self, volume_id: str) -> dict:
        return self.volumes.get(volume_id) or {}


def inspect_vm(session: AwsSession, vm_id: str, type_cache: Optional[dict] = None) -> AwsVmDetails:
    ids = parse_instance_arn(vm_id)
    if ids["region"] != session.region:
        raise AwsError(f"instance is in {ids['region']}, this login is for {session.region}", status=400)
    client: AwsClient = session.client
    inst = client.get_instance(ids["instance_id"])
    devices = _block_devices(inst)
    volumes = client.describe_volumes([d["volume_id"] for d in devices])
    itype = str(inst.get("instanceType") or "")
    cache = type_cache if type_cache is not None else {}
    if itype and itype not in cache:
        cache.update(client.describe_instance_types([itype]))
    images = client.describe_images([str(inst.get("imageId") or "")])
    image = images.get(str(inst.get("imageId") or ""))
    spec = vm_spec_from_aws(inst, volumes, cache.get(itype) or {}, image, session.region, session.account_id)
    # fill capacities from volume size when inspect has them
    for d in spec.disks:
        vol = volumes.get(d.backing_file) or {}
        if vol.get("size") and not d.capacity_bytes:
            d.capacity_bytes = int(vol["size"]) * GIB
    return AwsVmDetails(inst, volumes, spec, image)
