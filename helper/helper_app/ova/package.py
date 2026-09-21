"""Download an OVA from Object Storage, parse OVF, stage VMDKs for import/copy."""

from __future__ import annotations

import io
import logging
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, replace

from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.object_bytes import open_object_stream, read_object_bytes as _read_object_bytes
from helper_app.oci.options import object_storage_namespace
from helper_app.ova.ovf import OvfParseError, boot_disk_index, inspect_ovf, parse_ovf

log = logging.getLogger(__name__)

MAX_DISKS = 16
STAGING_ROOT = "oci-umt-ova"


class OvaPackageError(OciError):
    pass


@dataclass
class StagedDisk:
    index: int
    label: str
    capacity_bytes: int
    is_boot: bool
    source_href: str
    staged_object_name: str


@dataclass
class ParsedOva:
    staging_prefix: str
    ovf_name: str
    disks: list[StagedDisk]


@dataclass
class OvaDiskSource:
    """Where to read a VMDK: a bucket object and/or a member inside an ``.ova`` archive."""

    index: int
    label: str
    capacity_bytes: int
    is_boot: bool
    vmdk_href: str
    bucket_object: str | None = None
    ova_object: str | None = None


@dataclass
class ParsedOvaLayout:
    ovf_name: str
    disks: list[OvaDiskSource]


def _validate_object_name(name: str) -> None:
    if not name or ".." in name or name.startswith("/"):
        raise OvaPackageError(f"invalid object name {name!r}")


def _put_staged(c: OciClients, namespace: str, bucket: str, prefix: str, href: str, body: bytes) -> str:
    key = f"{prefix}/{href}"
    _validate_object_name(key)
    c.object_storage.put_object(namespace, bucket, key, body)
    return key


def _fetch_object_bytes(c: OciClients, namespace: str, bucket: str, object_name: str) -> bytes:
    try:
        body = _read_object_bytes(c, namespace, bucket, object_name)
    except Exception as exc:  # noqa: BLE001
        raise OvaPackageError(f"cannot read {bucket}/{object_name}: {exc}") from exc
    if not body:
        raise OvaPackageError(
            f"{bucket}/{object_name} is empty or could not be read from Object Storage "
            "(check the object size in the OCI console)"
        )
    return body


def _disk_object_for_href(ovf_object_name: str, href: str) -> str:
    """Object key for a VMDK referenced from a standalone ``.ovf`` (same folder as the descriptor)."""
    base = href.replace("\\", "/").split("/")[-1]
    if "/" in ovf_object_name:
        return f"{ovf_object_name.rsplit('/', 1)[0]}/{base}"
    return base


def read_ovf_from_object(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
) -> bytes:
    """Read an OVF descriptor from an ``.ova`` archive or a standalone ``.ovf`` object."""
    low = object_name.lower()
    if low.endswith(".ovf"):
        return _fetch_object_bytes(c, namespace, bucket, object_name)
    if not low.endswith(".ova"):
        raise OvaPackageError("Load requires an .ova or .ovf file (a standalone .vmdk has no OVF descriptor)")
    try:
        resp = c.object_storage.get_object(namespace, bucket, object_name)
    except Exception as exc:  # noqa: BLE001
        raise OvaPackageError(f"cannot read {bucket}/{object_name}: {exc}") from exc
    stream = open_object_stream(resp.data)
    ovf_bytes: bytes | None = None
    chunk = 1024 * 1024
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            base = member.name.split("/")[-1]
            f = tar.extractfile(member)
            if f is None:
                continue
            if base.lower().endswith(".ovf"):
                ovf_bytes = f.read()
                break
            while f.read(chunk):
                pass
    if ovf_bytes is None:
        raise OvaPackageError(f"{object_name} contains no .ovf descriptor")
    return ovf_bytes


def inspect_ova_object(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
):
    """Return OVF-derived suggestions for the OVA import form."""
    import math

    from helper_app.models import OvaInspectResponse
    from helper_app.oci.mapping import map_ovf_description, normalize_os_version_for_oci, os_version_choices, volume_size_gb

    ovf_bytes = read_ovf_from_object(c, namespace, bucket, object_name)
    try:
        meta = inspect_ovf(ovf_bytes)
    except OvfParseError as exc:
        raise OvaPackageError(str(exc)) from exc

    os_meta = map_ovf_description(meta.os_description, meta.product, meta.product_version)
    oci_version = normalize_os_version_for_oci(os_meta.operating_system, os_meta.operating_system_version)
    if oci_version != os_meta.operating_system_version:
        os_meta = replace(os_meta, operating_system_version=oci_version, version_detected=True)
    notes: list[str] = []
    if not os_meta.version_detected and os_meta.operating_system_version == "unknown":
        notes.append("Could not determine the OS release from the OVF; pick the version manually.")
    choices = os_version_choices(os_meta)
    if choices and os_meta.operating_system_version not in choices:
        notes.append(
            f"Suggested {os_meta.operating_system} {os_meta.operating_system_version} is not in the usual "
            f"OCI catalog; select a release from the list before import."
        )

    suggested_ocpus = None
    suggested_memory_gb = None
    if meta.num_vcpu is not None:
        suggested_ocpus = max(1, math.ceil(meta.num_vcpu / 2))
    if meta.memory_mb is not None:
        suggested_memory_gb = max(1, math.ceil(meta.memory_mb / 1024))

    firmware = meta.firmware
    if firmware is None:
        firmware = "UEFI_64"
        notes.append("Firmware not specified in the OVF; UEFI is suggested.")

    secure_boot = meta.secure_boot if meta.secure_boot is not None else False

    boot_gb = volume_size_gb(meta.boot_disk_bytes) if meta.boot_disk_bytes else None
    if meta.disk_count > 1:
        notes.append(f"{meta.disk_count} disks in the OVF; extra disks are copied as block volumes at import.")
    if object_name.lower().endswith(".ovf"):
        notes.append(
            "Standalone OVF: each disk file listed in the descriptor must exist in the same bucket folder "
            "as the .ovf (same object name prefix)."
        )

    return OvaInspectResponse(
        object_name=object_name,
        operating_system=os_meta.operating_system,
        operating_system_version=os_meta.operating_system_version,
        version_detected=os_meta.version_detected,
        family=os_meta.family,
        firmware=firmware,
        secure_boot=secure_boot,
        num_vcpu=meta.num_vcpu,
        memory_mb=meta.memory_mb,
        suggested_ocpus=suggested_ocpus,
        suggested_memory_gb=suggested_memory_gb,
        boot_disk_gb=boot_gb,
        disk_count=meta.disk_count,
        product=meta.product or None,
        os_description=meta.os_description or None,
        notes=notes,
    )


def _ovf_from_ova_stream(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
) -> bytes:
    try:
        resp = c.object_storage.get_object(namespace, bucket, object_name)
    except Exception as exc:  # noqa: BLE001
        raise OvaPackageError(f"cannot read {bucket}/{object_name}: {exc}") from exc
    stream = open_object_stream(resp.data)
    ovf_bytes: bytes | None = None
    chunk = 1024 * 1024
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            base = member.name.split("/")[-1]
            f = tar.extractfile(member)
            if f is None:
                continue
            if base.lower().endswith(".ovf"):
                ovf_bytes = f.read()
                break
            while f.read(chunk):
                pass
    if ovf_bytes is None:
        raise OvaPackageError(f"{object_name} contains no .ovf descriptor")
    return ovf_bytes


def _disks_from_parsed_ovf(
    parsed,
    *,
    bucket_object_for_href: Callable[[str], str] | None = None,
    ova_object: str | None = None,
) -> list[OvaDiskSource]:
    boot_i = boot_disk_index(parsed)
    disks: list[OvaDiskSource] = []
    for i, d in enumerate(parsed.disks):
        f = parsed.files.get(d.file_id)
        if f is None:
            raise OvaPackageError(f"OVF disk {d.disk_id} references unknown file {d.file_id}")
        href = f.href
        bucket_obj = bucket_object_for_href(href) if bucket_object_for_href else None
        disks.append(
            OvaDiskSource(
                index=i,
                label=d.label or f"disk {i}",
                capacity_bytes=d.capacity_bytes,
                is_boot=(i == boot_i),
                vmdk_href=href.split("/")[-1],
                bucket_object=bucket_obj,
                ova_object=ova_object if bucket_obj is None else None,
            )
        )
    return disks


def parse_ova_layout(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
) -> ParsedOvaLayout:
    """Parse an OVA/OVF/VMDK source and return disk layout without copying objects in Object Storage."""
    low = object_name.lower()

    if low.endswith(".ovf"):
        ovf_bytes = _fetch_object_bytes(c, namespace, bucket, object_name)
        try:
            parsed = parse_ovf(ovf_bytes)
        except OvfParseError as exc:
            raise OvaPackageError(str(exc)) from exc
        if len(parsed.disks) > MAX_DISKS:
            raise OvaPackageError(f"OVF has {len(parsed.disks)} disks; maximum is {MAX_DISKS}")
        disks = _disks_from_parsed_ovf(
            parsed,
            bucket_object_for_href=lambda href: _disk_object_for_href(object_name, href),
        )
        return ParsedOvaLayout(ovf_name=object_name, disks=disks)

    if low.endswith(".ova"):
        ovf_bytes = _ovf_from_ova_stream(c, namespace, bucket, object_name)
        try:
            parsed = parse_ovf(ovf_bytes)
        except OvfParseError as exc:
            raise OvaPackageError(str(exc)) from exc
        if len(parsed.disks) > MAX_DISKS:
            raise OvaPackageError(f"OVA has {len(parsed.disks)} disks; maximum is {MAX_DISKS}")
        disks = _disks_from_parsed_ovf(parsed, ova_object=object_name)
        return ParsedOvaLayout(ovf_name=object_name, disks=disks)

    if low.endswith(".vmdk"):
        body = _fetch_object_bytes(c, namespace, bucket, object_name)
        base = object_name.split("/")[-1]
        disks = [
            OvaDiskSource(
                index=0,
                label="disk 0",
                capacity_bytes=len(body),
                is_boot=True,
                vmdk_href=base,
                bucket_object=object_name,
            )
        ]
        return ParsedOvaLayout(ovf_name=object_name, disks=disks)

    raise OvaPackageError(f"{object_name} must end with .ova, .ovf, or .vmdk")


def _extract_ova_tar(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
    prefix: str,
) -> tuple[bytes, dict[str, bytes]]:
    """Stream the OVA tarball; return OVF bytes and VMDK bodies keyed by member basename."""
    try:
        resp = c.object_storage.get_object(namespace, bucket, object_name)
    except Exception as exc:  # noqa: BLE001
        raise OvaPackageError(f"cannot read {bucket}/{object_name}: {exc}") from exc
    stream = open_object_stream(resp.data)
    ovf_bytes: bytes | None = None
    vmdks: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            base = member.name.split("/")[-1]
            f = tar.extractfile(member)
            if f is None:
                continue
            data = f.read()
            low = base.lower()
            if low.endswith(".ovf"):
                ovf_bytes = data
            elif low.endswith(".vmdk"):
                vmdks[base] = data
    if ovf_bytes is None:
        raise OvaPackageError(f"{object_name} contains no .ovf descriptor")
    if not vmdks:
        raise OvaPackageError(f"{object_name} contains no .vmdk disk files")
    return ovf_bytes, vmdks


def _stage_vmdks(
    c: OciClients,
    namespace: str,
    bucket: str,
    prefix: str,
    vmdks: dict[str, bytes],
) -> dict[str, str]:
    staged: dict[str, str] = {}
    for href, body in vmdks.items():
        staged[href] = _put_staged(c, namespace, bucket, prefix, href, body)
    return staged


def parse_and_stage(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
    job_id: str,
) -> ParsedOva:
    """Parse ``object_name`` (``.ova``, ``.ovf``, or ``.vmdk``) and stage VMDKs under ``oci-umt-ova/{job_id}/``."""
    prefix = f"{STAGING_ROOT}/{job_id}"
    low = object_name.lower()

    if low.endswith(".ovf"):
        ovf_bytes = _fetch_object_bytes(c, namespace, bucket, object_name)
        try:
            parsed = parse_ovf(ovf_bytes)
        except OvfParseError as exc:
            raise OvaPackageError(str(exc)) from exc
        if len(parsed.disks) > MAX_DISKS:
            raise OvaPackageError(f"OVF has {len(parsed.disks)} disks; maximum is {MAX_DISKS}")
        boot_i = boot_disk_index(parsed)
        disks: list[StagedDisk] = []
        for i, d in enumerate(parsed.disks):
            f = parsed.files.get(d.file_id)
            if f is None:
                raise OvaPackageError(f"OVF disk {d.disk_id} references unknown file {d.file_id}")
            href = f.href
            disk_key = _disk_object_for_href(object_name, href)
            body = _fetch_object_bytes(c, namespace, bucket, disk_key)
            staged_name = _put_staged(c, namespace, bucket, prefix, href.split("/")[-1], body)
            disks.append(
                StagedDisk(
                    index=i,
                    label=d.label or f"disk {i}",
                    capacity_bytes=d.capacity_bytes,
                    is_boot=(i == boot_i),
                    source_href=href,
                    staged_object_name=staged_name,
                )
            )
        ovf_name = object_name
    elif low.endswith(".ova"):
        ovf_bytes, vmdks = _extract_ova_tar(c, namespace, bucket, object_name, prefix)
        try:
            parsed = parse_ovf(ovf_bytes)
        except OvfParseError as exc:
            raise OvaPackageError(str(exc)) from exc
        if len(parsed.disks) > MAX_DISKS:
            raise OvaPackageError(f"OVA has {len(parsed.disks)} disks; maximum is {MAX_DISKS}")
        staged_map = _stage_vmdks(c, namespace, bucket, prefix, vmdks)
        boot_i = boot_disk_index(parsed)
        disks: list[StagedDisk] = []
        for i, d in enumerate(parsed.disks):
            f = parsed.files.get(d.file_id)
            if f is None:
                raise OvaPackageError(f"OVF disk {d.disk_id} references unknown file {d.file_id}")
            href = f.href
            if href not in staged_map and href not in vmdks:
                raise OvaPackageError(f"OVA is missing disk file {href}")
            staged_name = staged_map.get(href) or _put_staged(c, namespace, bucket, prefix, href, vmdks[href])
            disks.append(
                StagedDisk(
                    index=i,
                    label=d.label or f"disk {i}",
                    capacity_bytes=d.capacity_bytes,
                    is_boot=(i == boot_i),
                    source_href=href,
                    staged_object_name=staged_name,
                )
            )
        ovf_name = object_name
    elif low.endswith(".vmdk"):
        body = _fetch_object_bytes(c, namespace, bucket, object_name)
        staged_name = _put_staged(c, namespace, bucket, prefix, object_name.split("/")[-1], body)
        size = len(body)
        disks = [
            StagedDisk(
                index=0,
                label="disk 0",
                capacity_bytes=size,
                is_boot=True,
                source_href=object_name.split("/")[-1],
                staged_object_name=staged_name,
            )
        ]
        ovf_name = object_name
    else:
        raise OvaPackageError(f"{object_name} must end with .ova, .ovf, or .vmdk")

    return ParsedOva(staging_prefix=prefix, ovf_name=ovf_name, disks=disks)


def staging_namespace(c: OciClients) -> str:
    return object_storage_namespace(c)
