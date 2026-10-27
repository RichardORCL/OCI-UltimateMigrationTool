"""Copy a VMDK stored in Object Storage onto a block device attached to the helper."""

from __future__ import annotations

import logging
import tarfile
from contextlib import closing
from typing import Callable, Iterator

from helper_app.disk.vmdk_stream import StreamOptimizedDecoder, VmdkFormatError
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.oci.clients import OciClients
from helper_app.oci.object_bytes import open_object_stream

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


def _iter_object(c: OciClients, namespace: str, bucket: str, object_name: str) -> Iterator[bytes]:
    resp = c.object_storage.get_object(namespace, bucket, object_name)
    stream = open_object_stream(resp.data)
    with closing(stream):
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            yield chunk


def _decode_vmdk_stream(
    chunks: Iterator[bytes],
    object_label: str,
    device: str,
    capacity_bytes: int,
    skip_zero_grains: bool,
    on_progress: Callable[[int, int], None] | None,
    check_cancel: Callable[[], None] | None = None,
) -> tuple[int, int]:
    # Both public callers supply generators with close(), so an early exit also closes HTTP streams.
    with closing(chunks), BlockDeviceWriter(device, expected_min_size=capacity_bytes) as writer:
        writer.ensure_size(capacity_bytes)
        received = 0
        decoder = StreamOptimizedDecoder(
            writer.write_at, expected_capacity_bytes=capacity_bytes, skip_zero_grains=skip_zero_grains,
        )
        for chunk in chunks:
            if check_cancel:
                check_cancel()
            decoder.feed(chunk)
            received += len(chunk)
            if on_progress:
                on_progress(received, decoder.stats.bytes_written)
        if check_cancel:
            check_cancel()
        stats = decoder.finish()
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
    check_cancel: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Stream ``object_name`` onto ``device``. Returns (bytes_received, bytes_written)."""
    return _decode_vmdk_stream(
        _iter_object(c, namespace, bucket, object_name),
        object_name,
        device,
        capacity_bytes,
        skip_zero_grains,
        on_progress,
        check_cancel,
    )


def _iter_object_range(
    c: OciClients, namespace: str, bucket: str, object_name: str, start: int, length: int, label: str,
) -> Iterator[bytes]:
    """Stream ``length`` bytes of an object from ``start`` with a single ranged ``GetObject``."""
    if length <= 0:
        return
    resp = c.object_storage.get_object(namespace, bucket, object_name, range=f"bytes={start}-{start + length - 1}")
    stream = open_object_stream(resp.data)
    got = 0
    with closing(stream):
        while got < length:
            chunk = stream.read(min(CHUNK, length - got))
            if not chunk:
                break
            got += len(chunk)
            yield chunk
    if got != length:
        raise VmdkFormatError(
            f"{label}: Object Storage returned {got} of {length} bytes (bytes {start}-{start + length - 1} of "
            f"{object_name}); the object may be shorter than its tar header claims"
        )


def _iter_ova_vmdk(
    c: OciClients,
    namespace: str,
    bucket: str,
    ova_object_name: str,
    vmdk_basename: str,
    check_cancel: Callable[[], None] | None = None,
) -> Iterator[bytes]:
    """Sequential fallback: walk the tarball until the wanted member (used when the archive is compressed)."""
    resp = c.object_storage.get_object(namespace, bucket, ova_object_name)
    stream = open_object_stream(resp.data)
    want = vmdk_basename.replace("\\", "/").split("/")[-1].lower()
    seen: list[str] = []
    with closing(stream), tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if check_cancel:
                check_cancel()
            if not member.isfile():
                continue
            base = member.name.replace("\\", "/").split("/")[-1]
            seen.append(base)
            f = tar.extractfile(member)
            if f is None:
                continue
            if base.lower() != want:
                while f.read(CHUNK):
                    if check_cancel:
                        check_cancel()
                continue
            got = 0
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                yield chunk
            if got != member.size:
                raise VmdkFormatError(
                    f"{base} in {ova_object_name} ended after {got} of {member.size} bytes (archive truncated?)"
                )
            return
    raise VmdkFormatError(
        f"{vmdk_basename} not found inside {ova_object_name}; the archive ended after "
        f"{', '.join(seen) or 'no file entries'}"
    )


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
    check_cancel: Callable[[], None] | None = None,
    member_offset: int | None = None,
    member_size: int | None = None,
) -> tuple[int, int]:
    """Stream one VMDK member from an ``.ova`` tarball onto ``device``.

    With ``member_offset``/``member_size`` (from the OVA index) the member is read with one ranged
    request; otherwise the whole archive is walked sequentially.
    """
    label = f"{ova_object_name}:{vmdk_basename}"
    if member_offset is not None and member_size is not None:
        chunks = _iter_object_range(c, namespace, bucket, ova_object_name, member_offset, member_size, label)
    else:
        chunks = _iter_ova_vmdk(c, namespace, bucket, ova_object_name, vmdk_basename, check_cancel)
    return _decode_vmdk_stream(
        chunks,
        label,
        device,
        capacity_bytes,
        skip_zero_grains,
        on_progress,
        check_cancel,
    )
