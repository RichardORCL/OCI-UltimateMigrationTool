"""Minimal OVF 1.x parsing for VMware-style exports."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


class OvfParseError(ValueError):
    pass


@dataclass
class OvfDisk:
    disk_id: str
    file_id: str
    capacity_bytes: int
    label: str = ""


@dataclass
class OvfFile:
    file_id: str
    href: str


@dataclass
class ParsedOvf:
    disks: list[OvfDisk] = field(default_factory=list)
    files: dict[str, OvfFile] = field(default_factory=dict)
    boot_disk_ids: list[str] = field(default_factory=list)


@dataclass
class OvfInspectResult:
    """Guest metadata read from an OVF descriptor (no VMDK import)."""

    os_description: str = ""
    product: str = ""
    product_version: str = ""
    num_vcpu: int | None = None
    memory_mb: int | None = None
    firmware: str | None = None  # BIOS | UEFI_64
    secure_boot: bool | None = None
    disk_count: int = 0
    boot_disk_bytes: int | None = None


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _capacity_bytes(elem: ET.Element) -> int:
    cap = elem.get("capacity") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}capacity")
    if cap is None:
        raise OvfParseError("Disk element missing capacity")
    try:
        value = int(cap)
    except ValueError as exc:
        raise OvfParseError(f"invalid disk capacity {cap!r}") from exc
    units = (
        elem.get("capacityAllocationUnits")
        or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}capacityAllocationUnits")
        or "byte"
    ).lower()
    if "byte" in units:
        return value
    if "sector" in units or units.endswith("*^2"):
        return value * 512
    if "gib" in units or "gb" in units:
        return value * 1024**3
    if "mib" in units or "mb" in units:
        return value * 1024**2
    return value * 512


def prepare_ovf_bytes(xml_bytes: bytes) -> bytes:
    """Normalize encoding (UTF-16 exports, BOM) before XML parse."""
    if not xml_bytes or not xml_bytes.strip():
        raise OvfParseError("OVF descriptor is empty")
    if xml_bytes.startswith(b"\xff\xfe") or xml_bytes.startswith(b"\xfe\xff"):
        try:
            return xml_bytes.decode("utf-16").encode("utf-8")
        except UnicodeDecodeError as exc:
            raise OvfParseError(f"OVF is not valid UTF-16: {exc}") from exc
    if xml_bytes.startswith(b"\xef\xbb\xbf"):
        return xml_bytes[3:]
    return xml_bytes


def parse_ovf(xml_bytes: bytes) -> ParsedOvf:
    xml_bytes = prepare_ovf_bytes(xml_bytes)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise OvfParseError(f"invalid OVF XML: {exc}") from exc

    files: dict[str, OvfFile] = {}
    disks: list[OvfDisk] = []
    boot_refs: list[tuple[int | None, str]] = []

    for elem in root.iter():
        name = _local(elem.tag)
        if name == "File":
            fid = elem.get("id") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}id")
            href = elem.get("href") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}href")
            if fid and href:
                files[fid] = OvfFile(file_id=fid, href=href.split("/")[-1])
        elif name == "Disk":
            did = elem.get("diskId") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}diskId")
            fref = elem.get("fileRef") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}fileRef")
            if not did or not fref:
                continue
            label = elem.get("description") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}description") or ""
            disks.append(
                OvfDisk(
                    disk_id=did,
                    file_id=fref,
                    capacity_bytes=_capacity_bytes(elem),
                    label=label,
                )
            )
        elif name == "Boot":
            order = elem.get("order") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}order")
            ref = elem.get("deviceRef") or elem.get("{http://schemas.dmtf.org/ovf/envelope/1}deviceRef")
            if ref:
                try:
                    priority = int(order) if order is not None else None
                except ValueError as exc:
                    raise OvfParseError(f"invalid boot order {order!r}") from exc
                if priority is not None and priority < 0:
                    raise OvfParseError(f"invalid boot order {order!r}")
                boot_refs.append((priority, ref))

    if not disks:
        raise OvfParseError("OVF contains no Disk elements")

    # Explicit priorities first; ties and missing priorities retain document order.
    boot_refs.sort(key=lambda item: (item[0] is None, item[0] or 0))
    return ParsedOvf(disks=disks, files=files, boot_disk_ids=[ref for _, ref in boot_refs])


def boot_disk_index(parsed: ParsedOvf) -> int:
    """Index into ``parsed.disks`` for the boot disk."""
    if parsed.boot_disk_ids:
        boot_id = parsed.boot_disk_ids[0]
        for i, d in enumerate(parsed.disks):
            if d.disk_id == boot_id:
                return i
    return 0


def _child_text(elem: ET.Element, local: str) -> str | None:
    for child in elem:
        if _local(child.tag) == local and child.text:
            return child.text.strip()
    return None


def _attr(elem: ET.Element, name: str) -> str | None:
    for key, val in elem.attrib.items():
        if key == name or key.endswith("}" + name):
            return val
    return None


def _memory_mb(quantity: int, units: str | None) -> int:
    u = (units or "byte * 2^20").lower()
    if "2^20" in u or "mib" in u or "mb" in u or "megabyte" in u:
        return max(1, quantity)
    if "2^30" in u or "gib" in u or "gb" in u or "gigabyte" in u:
        return max(1, quantity * 1024)
    if "byte" in u and "2^" not in u:
        return max(1, quantity // (1024 * 1024))
    return max(1, quantity)


def _firmware_value(raw: str) -> str | None:
    v = raw.lower()
    if "efi" in v or "uefi" in v:
        return "UEFI_64"
    if "bios" in v:
        return "BIOS"
    return None


def inspect_ovf(xml_bytes: bytes) -> OvfInspectResult:
    """Parse OS, sizing, and firmware hints from an OVF envelope."""
    xml_bytes = prepare_ovf_bytes(xml_bytes)
    parsed = parse_ovf(xml_bytes)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise OvfParseError(f"invalid OVF XML: {exc}") from exc

    os_desc = ""
    product = ""
    product_version = ""
    num_vcpu: int | None = None
    memory_mb: int | None = None
    firmware: str | None = None
    secure_boot: bool | None = None

    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "OperatingSystemSection" and not os_desc:
            os_desc = (_child_text(elem, "Description") or _child_text(elem, "Info") or "").strip()
        elif tag == "ProductSection":
            product = (_child_text(elem, "Product") or product).strip()
            product_version = (_child_text(elem, "Version") or product_version).strip()
        elif tag == "Item":
            rtype = _child_text(elem, "ResourceType")
            qty_text = _child_text(elem, "VirtualQuantity")
            if not rtype or not qty_text:
                continue
            try:
                qty = int(qty_text)
            except ValueError:
                continue
            if rtype == "3" and num_vcpu is None:
                num_vcpu = max(1, qty)
            elif rtype == "4" and memory_mb is None:
                units = _child_text(elem, "AllocationUnits")
                memory_mb = _memory_mb(qty, units)
        elif tag in ("Property", "Config"):
            key = (_attr(elem, "key") or "").lower()
            val = _attr(elem, "value") or ""
            if key == "firmware" and val:
                firmware = _firmware_value(val) or firmware
            if key in ("secureboot", "secure_boot") or key.endswith("efisecurebootenabled"):
                if val.lower() in ("true", "1", "yes"):
                    secure_boot = True
                elif val.lower() in ("false", "0", "no"):
                    secure_boot = False

    for elem in root.iter():
        fw = _attr(elem, "firmware")
        if fw:
            firmware = _firmware_value(fw) or firmware

    boot_i = boot_disk_index(parsed)
    boot_bytes = parsed.disks[boot_i].capacity_bytes if parsed.disks else None

    return OvfInspectResult(
        os_description=os_desc,
        product=product,
        product_version=product_version,
        num_vcpu=num_vcpu,
        memory_mb=memory_mb,
        firmware=firmware,
        secure_boot=secure_boot,
        disk_count=len(parsed.disks),
        boot_disk_bytes=boot_bytes,
    )
