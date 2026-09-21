"""Regression tests for credential handling, disk integrity and interruption safety."""

import http.client
import io
import logging
import tarfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from helper_app import __version__, logging_config
from helper_app.azure.client import AzureClient, AzureError
from helper_app.config import Settings
from helper_app.disk import object_vmdk_copy
from helper_app.disk.copy_common import run_workers
from helper_app.disk.gcs_range_copy import GcsCopyError, copy_from_gcs_export_tarball, copy_parallel_ranges
from helper_app.disk.vmdk_stream import VmdkFormatError
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.ova.ovf import OvfParseError, boot_disk_index, parse_ovf
from helper_app.redaction import redact
from helper_app.ui_password import UiPasswordStore


def test_credentials_redacted_in_requests_errors_and_tracebacks(caplog):
    logging_config.apply(Settings(log_level="DEBUG", oci_log_requests=True))
    assert http.client.HTTPConnection.debuglevel == 0
    url = "https://example.invalid/disk?sig=FAKE_REVIEW_SIGNATURE&sv=1"
    # Force HTTP request logs back on to verify redaction as well as normal suppression.
    with caplog.at_level(logging.INFO, logger="httpx"):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            AzureClient("test", "test", "test", http=client).blob_get(url)
        try:
            raise ValueError(url)
        except ValueError:
            logging.getLogger(__name__).exception("failed for %s", url)
    assert "FAKE_REVIEW_SIGNATURE" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "FAKE_REVIEW_SIGNATURE" not in str(AzureError(url))


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer FAKE_CREDENTIAL",
        '{"client_secret": "FAKE_CREDENTIAL"}',
        "https://user:FAKE_CREDENTIAL@example.invalid/path",
        "-----BEGIN PRIVATE KEY-----\nFAKE_CREDENTIAL\n-----END PRIVATE KEY-----",
    ],
)
def test_redaction_of_diagnostics_patterns(text):
    assert "FAKE_CREDENTIAL" not in redact(text)


def test_unreadable_password_fails_closed(tmp_path, monkeypatch):
    store = UiPasswordStore(str(tmp_path / "password"))
    store.set_password("example-password")

    def unreadable(*args, **kwargs):
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(RuntimeError, match="locked"):
        store.reload()
    assert store.required and store.verify("example-password")


def test_failed_password_replace_preserves_previous_password(tmp_path, monkeypatch):
    import helper_app.ui_password as module

    path = tmp_path / "password"
    store = UiPasswordStore(str(path))
    store.set_password("previous-password")
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated interruption")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.set_password("replacement-password")
    assert path.read_bytes() == before
    assert store.verify("previous-password")
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("size", [512, 2048])
def test_wrong_gcs_logical_disk_size_rejected_before_writing(size):
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w:gz") as tar:
        member = tarfile.TarInfo("disk.raw")
        member.size = size
        tar.addfile(member, io.BytesIO(b"x" * size))
    writes = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body.getvalue()))) as http:
        client = SimpleNamespace(http=http, object_media_url=lambda *a: "https://example.invalid/disk", _headers=dict)
        with pytest.raises(GcsCopyError, match="logical size"):
            copy_from_gcs_export_tarball(
                client,
                "test",
                "disk.tar.gz",
                SimpleNamespace(write_at=lambda *a: writes.append(a)),
                expected_bytes=1024,
            )
    assert not writes


def test_short_gcs_range_is_never_written():
    writes = []
    with pytest.raises(GcsCopyError, match="returned 1 bytes"):
        copy_parallel_ranges(
            SimpleNamespace(object_get_range=lambda *a: b"x"),
            "test",
            "disk.raw",
            SimpleNamespace(write_at=lambda *a: writes.append(a)),
            total_bytes=1024,
        )
    assert writes == []


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_vmdk_closes_device_and_http_stream(tmp_path, monkeypatch, cancel):
    writers = []

    def writer_factory(*args, **kwargs):
        writer = BlockDeviceWriter(*args, **kwargs)
        writers.append(writer)
        return writer

    monkeypatch.setattr(object_vmdk_copy, "BlockDeviceWriter", writer_factory)
    target = tmp_path / "disk"
    target.touch()
    body = io.BytesIO(b"INVALID!" * 128)
    clients = SimpleNamespace(
        object_storage=SimpleNamespace(get_object=lambda *a: SimpleNamespace(data=SimpleNamespace(content=body)))
    )

    def check_cancel():
        raise InterruptedError("cancel requested")

    with pytest.raises(InterruptedError if cancel else VmdkFormatError):
        object_vmdk_copy.copy_vmdk_object(
            clients, "ns", "bucket", "disk.vmdk", str(target), 1024, check_cancel=check_cancel if cancel else None
        )
    assert body.closed
    assert writers[0]._closed


def test_worker_failure_stops_scheduling():
    seen = []

    def process(item):
        seen.append(item)
        raise ValueError("copy failed")

    with pytest.raises(ValueError, match="copy failed"):
        run_workers(range(100), process, workers=1)
    assert seen == [0]


def ovf_with_boot(boots):
    return (
        """<Envelope><References><File id="f1" href="one.vmdk"/><File id="f2" href="two.vmdk"/>
    </References><DiskSection><Disk diskId="d1" fileRef="f1" capacity="1024"/>
    <Disk diskId="d2" fileRef="f2" capacity="1024"/></DiskSection>"""
        + boots
        + "</Envelope>"
    ).encode()


def test_ovf_boot_order_is_numeric_and_missing_priority_is_last():
    parsed = parse_ovf(
        ovf_with_boot('<Boot deviceRef="d1"/><Boot order="2" deviceRef="d1"/><Boot order="1" deviceRef="d2"/>')
    )
    assert boot_disk_index(parsed) == 1


@pytest.mark.parametrize("order", ["wrong", "-1"])
def test_invalid_ovf_boot_priority_fails(order):
    with pytest.raises(OvfParseError, match="boot order"):
        parse_ovf(ovf_with_boot(f'<Boot order="{order}" deviceRef="d1"/>'))


def test_package_and_application_versions_agree():
    assert __version__ == version("vc-oci-helper")
