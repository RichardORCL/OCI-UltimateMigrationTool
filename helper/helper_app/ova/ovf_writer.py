"""Build a VMware-style OVF 1.x envelope and SHA-256 manifest for an OVF set."""

from __future__ import annotations

from typing import Iterable, Sequence
from xml.sax.saxutils import escape

# (substring of OCI operating_system + version, os id, vmw:osType)
_OS_TABLE: list[tuple[str, str, str]] = [
    ("windows 11", "windows11_64Guest", "94"),
    ("windows10", "windows9_64Guest", "94"),
    ("windows 10", "windows9_64Guest", "94"),
    ("server 2025", "windows2022srvNext_64Guest", "113"),
    ("server 2022", "windows2019srvNext_64Guest", "112"),
    ("server 2019", "windows9Server64Guest", "111"),
    ("server 2016", "windows9Server64Guest", "92"),
    ("server 2012", "windows8Server64Guest", "103"),
    ("windows", "windows9Server64Guest", "111"),
    ("oracle linux 9", "oracleLinux9_64Guest", "107"),
    ("oracle linux 8", "oracleLinux8_64Guest", "107"),
    ("oracle linux 7", "oracleLinux7_64Guest", "104"),
    ("oracle linux", "oracleLinux64Guest", "104"),
    ("red hat", "rhel9_64Guest", "80"),
    ("rhel", "rhel9_64Guest", "80"),
    ("centos", "centos8_64Guest", "108"),
    ("ubuntu", "ubuntu64Guest", "94"),
    ("debian", "debian11_64Guest", "96"),
    ("suse", "sles15_64Guest", "85"),
    ("sles", "sles15_64Guest", "85"),
]


def map_os_to_ovf(operating_system: str, operating_system_version: str = "") -> tuple[str, str, str]:
    """Return ``(vmw:osType, ovf os id, description)`` for OCI image metadata."""
    blob = f"{operating_system} {operating_system_version}".strip()
    key = blob.lower()
    description = blob or "Linux"
    if not key:
        return "otherLinux64Guest", "36", "Linux"
    for needle, guest, os_id in _OS_TABLE:
        if needle in key:
            return guest, os_id, description
    if key.startswith("windows"):
        return "windows9Server64Guest", "111", description
    return "otherLinux64Guest", "36", description


def build_manifest(entries: Iterable[tuple[str, str]]) -> bytes:
    """``SHA256(<hex>)= <filename>`` lines, one per file (VMware .mf)."""
    lines = [f"SHA256({name})= {digest}\n" for name, digest in entries]
    return "".join(lines).encode("utf-8")


def build_ovf(
    name: str,
    os_meta: tuple[str, str] | dict,
    num_vcpu: int,
    memory_mb: int,
    firmware: str,
    secure_boot: bool,
    disks: Sequence[dict],
    network: str = "VM Network",
) -> bytes:
    """Serialize an OVF envelope the existing ``parse_ovf`` / ``inspect_ovf`` can read.

    ``os_meta`` is ``(operating_system, operating_system_version)`` or a mapping with
    those keys.  Each disk dict needs ``file_id``, ``href``, ``size_bytes``,
    ``capacity_bytes`` and optionally ``populated_size`` / ``label``.
    """
    if isinstance(os_meta, dict):
        os_name = str(os_meta.get("operating_system") or "")
        os_version = str(os_meta.get("operating_system_version") or "")
    else:
        os_name, os_version = os_meta
    guest, os_id, os_desc = map_os_to_ovf(os_name, os_version)
    fw = "efi" if str(firmware).upper() in ("UEFI", "UEFI_64", "EFI") else "bios"
    files_xml = []
    disks_xml = []
    disk_items = []
    for i, d in enumerate(disks):
        fid = escape(str(d.get("file_id") or f"file{i + 1}"))
        href = escape(str(d["href"]))
        size = int(d.get("size_bytes") or 0)
        cap = int(d["capacity_bytes"])
        pop = d.get("populated_size")
        label = escape(str(d.get("label") or f"disk {i}"))
        disk_id = escape(str(d.get("disk_id") or f"vmdisk{i + 1}"))
        files_xml.append(
            f'    <File ovf:id="{fid}" ovf:href="{href}" ovf:size="{size}"/>'
        )
        pop_attr = f' ovf:populatedSize="{int(pop)}"' if pop is not None else ""
        disks_xml.append(
            f'    <Disk ovf:diskId="{disk_id}" ovf:fileRef="{fid}" ovf:capacity="{cap}" '
            f'ovf:capacityAllocationUnits="byte" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"'
            f'{pop_attr} ovf:description="{label}"/>'
        )
        disk_items.append(_rasd_item(
            instance_id=str(10 + i),
            resource_type="17",
            address_on_parent=str(i),
            parent="3",
            host_resource=f"ovf:/disk/{disk_id}",
            element_name=label,
        ))

    secure_xml = ""
    if fw == "efi":
        secure_xml = (
            f'\n      <vmw:Config ovf:required="false" vmw:key="bootOptions.efiSecureBootEnabled" '
            f'vmw:value="{str(bool(secure_boot)).lower()}"/>'
        )

    ncpu = max(1, int(num_vcpu))
    mem = max(1, int(memory_mb))
    _cpu_item = _rasd_item("1", "3", virtual_quantity=str(ncpu), element_name=f"{ncpu} virtual CPU(s)")
    _mem_item = _rasd_item(
        "2", "4", virtual_quantity=str(mem), allocation_units="byte * 2^20",
        element_name=f"{mem} MB of memory",
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
          xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
          xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
{chr(10).join(files_xml)}
  </References>
  <DiskSection>
    <Info>Virtual disk information</Info>
{chr(10).join(disks_xml)}
  </DiskSection>
  <NetworkSection>
    <Info>The list of logical networks</Info>
    <Network ovf:name="{escape(network)}">
      <Description>{escape(network)}</Description>
    </Network>
  </NetworkSection>
  <VirtualSystem ovf:id="{escape(name)}">
    <Info>A virtual machine</Info>
    <Name>{escape(name)}</Name>
    <OperatingSystemSection ovf:id="{os_id}" vmw:osType="{escape(guest)}">
      <Info>The kind of installed guest operating system</Info>
      <Description>{escape(os_desc)}</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware requirements</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>{escape(name)}</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>vmx-14</vssd:VirtualSystemType>
      </System>
{_cpu_item}
{_mem_item}
{_rasd_item("3", "6", resource_sub_type="lsilogic", element_name="SCSI Controller 0")}
{chr(10).join(disk_items)}
{_rasd_item("20", "10", resource_sub_type="E1000", element_name="Network adapter 1", connection=network)}
      <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="{fw}"/>{secure_xml}
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
"""
    return xml.encode("utf-8")


def _rasd_item(
    instance_id: str,
    resource_type: str,
    *,
    address_on_parent: str | None = None,
    parent: str | None = None,
    host_resource: str | None = None,
    element_name: str = "",
    virtual_quantity: str | None = None,
    allocation_units: str | None = None,
    resource_sub_type: str | None = None,
    connection: str | None = None,
) -> str:
    parts = [
        "      <Item>",
        f"        <rasd:InstanceID>{escape(instance_id)}</rasd:InstanceID>",
        f"        <rasd:ResourceType>{escape(resource_type)}</rasd:ResourceType>",
    ]
    if resource_sub_type:
        parts.append(f"        <rasd:ResourceSubType>{escape(resource_sub_type)}</rasd:ResourceSubType>")
    if virtual_quantity:
        parts.append(f"        <rasd:VirtualQuantity>{escape(virtual_quantity)}</rasd:VirtualQuantity>")
    if allocation_units:
        parts.append(f"        <rasd:AllocationUnits>{escape(allocation_units)}</rasd:AllocationUnits>")
    if parent:
        parts.append(f"        <rasd:Parent>{escape(parent)}</rasd:Parent>")
    if address_on_parent is not None:
        parts.append(f"        <rasd:AddressOnParent>{escape(address_on_parent)}</rasd:AddressOnParent>")
    if host_resource:
        parts.append(f"        <rasd:HostResource>{escape(host_resource)}</rasd:HostResource>")
    if connection:
        parts.append(f"        <rasd:Connection>{escape(connection)}</rasd:Connection>")
    if element_name:
        parts.append(f"        <rasd:ElementName>{escape(element_name)}</rasd:ElementName>")
    parts.append("      </Item>")
    return "\n".join(parts)
