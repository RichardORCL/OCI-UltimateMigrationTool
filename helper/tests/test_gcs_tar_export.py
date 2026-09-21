"""GCS export tarball (disk.raw in .tar.gz) streaming copy."""

from __future__ import annotations

from helper_app.disk.gcs_range_copy import copy_from_gcs_export_tarball
from helper_app.gcp.client import parse_service_account_json
from tests.fake_gcp import BUCKET, SA_JSON, FakeGcp, _FakeGcpClient


class _BufWriter:
    def __init__(self, size: int):
        self._buf = bytearray(size)
        self.writes: list[tuple[int, int]] = []

    def write_at(self, offset: int, data: bytes) -> None:
        self._buf[offset: offset + len(data)] = data
        self.writes.append((offset, len(data)))

    @property
    def data(self) -> bytes:
        return bytes(self._buf)


def _put_tar_gz(fake: FakeGcp, object_name: str, raw: bytes) -> None:
    fake.objects[object_name] = FakeGcp._tar_gz_disk_raw(raw)


def test_copy_from_gcs_export_tarball():
    raw = b"\x01" * 4096 + b"\x00" * 4096
    fake = FakeGcp([])
    obj = "oci-umt/job/0-disk.tar.gz"
    _put_tar_gz(fake, obj, raw)
    sa = parse_service_account_json(SA_JSON)
    client = _FakeGcpClient(sa["project_id"], sa["client_email"], sa["private_key"],
                            http=fake.http(), sleep=lambda s: None)
    writer = _BufWriter(size=len(raw) * 2)
    stats = copy_from_gcs_export_tarball(
        client, BUCKET, obj, writer, expected_bytes=len(raw), chunk_bytes=4096,
    )
    assert stats.bytes_received == len(raw)
    assert stats.bytes_written == 4096
    assert writer.data[:4096] == b"\x01" * 4096
    assert writer.data[4096:8192] == b"\x00" * 4096
    assert writer.writes == [(0, 4096)]


def test_copy_from_gcs_export_tarball_skips_zero_regions():
    raw = b"\x00" * 8192 + b"\xab" * 1024 + b"\x00" * 4096
    fake = FakeGcp([])
    obj = "oci-umt/job/1-disk.tar.gz"
    _put_tar_gz(fake, obj, raw)
    sa = parse_service_account_json(SA_JSON)
    client = _FakeGcpClient(sa["project_id"], sa["client_email"], sa["private_key"],
                            http=fake.http(), sleep=lambda s: None)
    writer = _BufWriter(size=len(raw))
    stats = copy_from_gcs_export_tarball(
        client, BUCKET, obj, writer, expected_bytes=len(raw), chunk_bytes=1024,
    )
    assert stats.bytes_received == len(raw)
    assert stats.bytes_written == 1024
    assert writer.writes == [(8192, 1024)]
    assert writer.data[8192:9216] == b"\xab" * 1024
