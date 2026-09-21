"""Copy a VMDK stored in Object Storage onto a block device attached to the helper."""

from __future__ import annotations

import logging
import tarfile
from typing import Callable, Iterator

from helper_app.disk.vmdk_stream import StreamOptimizedDecoder, VmdkFormatError, VMDK_MAGIC, SECTOR
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.oci.clients import OciClients
from helper_app.oci.object_bytes import open_object_stream

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


def _iter_object(c: OciClients, namespace: str, bucket: str, object_name: str) -> Iterator[bytes]:
    resp = c.object_storage.get_object(namespace, bucket, object_name)
    stream = open_object_stream(resp.data)
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        yield chunk


def vmdk_capacity_bytes(header_sector: bytes) -> int:
    if len(header_sector) < 12:
        return 0
    import struct

    magic = struct.unpack_from("<I", header_sector, 0)[0]
    if magic != VMDK_MAGIC:
        return 0
    capacity_sectors = struct.unpack_from("<Q", header_sector, 16)[0]
    return int(capacity_sectors) * SECTOR


def _decode_vmdk_stream(
    chunks: Iterator[bytes],
    object_label: str,
    device: str,
    capacity_bytes: int,
    skip_zero_grains: bool,
    on_progress: Callable[[int, int], None] | None,
) -> tuple[int, int]:
    writer = BlockDeviceWriter(device, expected_min_size=capacity_bytes)
    writer.ensure_size(capacity_bytes)
    received = 0
    first = True

    def write_at(offset: int, data: bytes) -> None:
        writer.write_at(offset, data)

    decoder = StreamOptimizedDecoder(write_at, expected_capacity_bytes=capacity_bytes, skip_zero_grains=skip_zero_grains)

    for chunk in chunks:
        if first and chunk:
            first = False
            if len(chunk) >= 4:
                import struct

                magic = struct.unpack_from("<I", chunk, 0)[0]
                if magic != VMDK_MAGIC:
                    raise VmdkFormatError(
                        f"{object_label} is not a stream-optimized VMDK; re-export the OVA with stream-optimized disks"
                    )
        decoder.feed(chunk)
        received += len(chunk)
        if on_progress:
            on_progress(received, decoder.stats.bytes_written)

    stats = decoder.finish()
    writer.close()
    return received, stats.bytes_written


def copy_vmdk_object(
    c: OciClients,
    namespace: str,
    bucket: str,
    object_name: str,
    device: str,
    capacity_bytes: int,
    skip_zero_grains: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Stream ``object_name`` onto ``device``. Returns (bytes_received, bytes_written)."""
    return _decode_vmdk_stream(
        _iter_object(c, namespace, bucket, object_name),
        object_name,
        device,
        capacity_bytes,
        skip_zero_grains,
        on_progress,
    )


def _iter_ova_vmdk(
    c: OciClients,
    namespace: str,
    bucket: str,
    ova_object_name: str,
    vmdk_basename: str,
) -> Iterator[bytes]:
    resp = c.object_storage.get_object(namespace, bucket, ova_object_name)
    stream = open_object_stream(resp.data)
    want = vmdk_basename.split("/")[-1].lower()
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            base = member.name.split("/")[-1]
            f = tar.extractfile(member)
            if f is None:
                continue
            if base.lower() != want:
                while f.read(CHUNK):
                    pass
                continue
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                yield chunk
            return
    raise VmdkFormatError(f"{vmdk_basename} not found inside {ova_object_name}")


def copy_vmdk_from_ova(
    c: OciClients,
    namespace: str,
    bucket: str,
    ova_object_name: str,
    vmdk_basename: str,
    device: str,
    capacity_bytes: int,
    skip_zero_grains: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Stream one VMDK member from an ``.ova`` tarball onto ``device``."""
    label = f"{ova_object_name}:{vmdk_basename}"
    return _decode_vmdk_stream(
        _iter_ova_vmdk(c, namespace, bucket, ova_object_name, vmdk_basename),
        label,
        device,
        capacity_bytes,
        skip_zero_grains,
        on_progress,
    )
