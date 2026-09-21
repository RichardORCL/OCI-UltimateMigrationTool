"""AWS source: client, inventory, preflight, export and EBS block copy against fake_aws."""

from __future__ import annotations

import pytest

from helper_app.aws.client import AwsAuthError, AwsError
from helper_app.aws.export import AwsDiskExport, release_aws_resources
from helper_app.aws.inventory import inspect_vm, list_vm_summaries
from helper_app.aws.preflight import preflight, warnings
from helper_app.config import Settings
from helper_app.disk.ebs_range_copy import copy_blocks, list_blocks
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.models import AwsSourceInfo, Firmware
from helper_app.oci.mapping import map_guest_os

from .fake_aws import ACCESS_KEY, ACCOUNT, REGION, SECRET, FakeAws, make_fleet


@pytest.fixture
def fleet():
    return make_fleet()


def login(fake: FakeAws, settings=None):
    return fake.connector(settings or Settings()).login(ACCESS_KEY, SECRET, REGION)


def test_login_and_errors(fleet):
    session = login(fleet)
    assert session.account_id == ACCOUNT and session.region == REGION
    assert session.username == f"aws:{ACCOUNT}"
    session.close()

    conn = fleet.connector(Settings())
    with pytest.raises(AwsAuthError, match="required"):
        conn.login(ACCESS_KEY, "", REGION)
    with pytest.raises(AwsAuthError, match="invalid access key"):
        conn.login("not-a-key", SECRET, REGION)
    with pytest.raises(AwsAuthError, match="invalid region"):
        conn.login(ACCESS_KEY, SECRET, "EU_WEST_1")
    with pytest.raises(AwsAuthError, match="invalid"):
        conn.login("AKIAOTHERKEY12EXAMPLE", SECRET, REGION)


def test_vm_list_and_spec(fleet):
    session = login(fleet)
    rows = list_vm_summaries(session)
    names = [r.name for r in rows]
    assert "lin-01" in names and "win-01" in names
    lin = next(r for r in rows if r.name == "lin-01")
    assert lin.power_state == "poweredOn" and lin.vm_size == "t3.medium"
    assert lin.folder.startswith("eu-west-1/")
    assert "Ubuntu" in lin.guest_full_name

    details = inspect_vm(session, lin.moid)
    spec = details.spec
    assert spec.name == "lin-01" and spec.firmware == Firmware.EFI
    assert spec.num_cpu == 2 and spec.memory_mb == 4096
    assert len(spec.disks) == 2 and spec.disks[0].backing_file == "vol-os"
    os_meta = map_guest_os(spec.guest_id, spec.guest_full_name)
    assert os_meta.operating_system == "Ubuntu"
    win = next(r for r in rows if r.name == "win-01")
    assert inspect_vm(session, win.moid).spec.power_state == "poweredOff"


def test_preflight(fleet):
    session = login(fleet)
    store = next(r for r in list_vm_summaries(session) if r.name == "store-01")
    problems = preflight(inspect_vm(session, store.moid))
    assert any("instance store" in p for p in problems)
    market = next(r for r in list_vm_summaries(session) if r.name == "market-01")
    problems = preflight(inspect_vm(session, market.moid))
    assert any("Marketplace" in p for p in problems)
    lin = next(r for r in list_vm_summaries(session) if r.name == "lin-01")
    details = inspect_vm(session, lin.moid)
    assert preflight(details, "stop") == []
    notes = warnings(details, "snapshot")
    assert any("crash-consistent" in n for n in notes)


def test_export_and_copy(fleet, tmp_path):
    session = login(fleet)
    lin = next(r for r in list_vm_summaries(session) if r.name == "lin-01")
    details = inspect_vm(session, lin.moid)
    info = AwsSourceInfo(account_id=ACCOUNT, region=REGION, instance_id=details.instance_id,
                         capture_mode="snapshot", volume_ids=details.volume_ids)
    saved = []
    with AwsDiskExport(session.client, "job1", info, snapshot_timeout_s=30,
                       save=lambda: saved.append(1)) as export:
        assert len(info.snapshot_ids) == 2
        snap = export.snapshot_id(0)
        dest = tmp_path / "disk0"
        dest.write_bytes(b"\x00")
        writer = BlockDeviceWriter(str(dest), expected_min_size=2 * 512 * 1024, create=True)
        block_size, blocks = list_blocks(session.client, snap, 2 * 512 * 1024)
        assert blocks
        stats = copy_blocks(session.client, snap, blocks, block_size, writer, 2 * 512 * 1024, workers=2)
        writer.close()
        assert stats.bytes_written > 0
        listed = [r for r in fleet.requests if "listSnapshotBlocks" in r or r.endswith(f"/snapshots/{snap}/blocks")]
        assert listed and all("/listSnapshotBlocks" not in r for r in listed)
        assert any(r.endswith(f"/snapshots/{snap}/blocks") for r in listed)
    assert info.snapshot_ids == []
    left = release_aws_resources(session.client, info)
    assert left == []


def test_stop_instance(fleet):
    session = login(fleet)
    client = session.client
    inst = next(i for i in fleet.instances.values() if i.name == "lin-01")
    assert inst.state == "running"
    client.stop_instance(inst.instance_id, timeout_s=30)
    assert inst.state == "stopped" and "stop" in inst.ops
    with pytest.raises(AwsError, match="not found"):
        client.get_instance("i-nope")
