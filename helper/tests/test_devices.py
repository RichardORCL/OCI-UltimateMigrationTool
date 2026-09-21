import pytest

from helper_app.disk import devices as dv

GIB = 1024**3


def test_scan_block_devices_reads_sys_block(tmp_path):
    for name, sectors in [("sda", 50 * GIB // 512), ("sdb", 100 * GIB // 512), ("sda1", 1000), ("loop0", 8),
                          ("dm-0", 8), ("nvme0n1", 20)]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "size").write_text(f"{sectors}\n")
    (tmp_path / "sda1" / "partition").write_text("1\n")  # sysfs marks partitions like this
    (tmp_path / "sdc").mkdir()  # no size file -> ignored
    assert dv.scan_block_devices(tmp_path) == {"/dev/sda": 50 * GIB, "/dev/sdb": 100 * GIB, "/dev/nvme0n1": 20 * 512}
    assert dv.scan_block_devices(tmp_path / "missing") == {}


def test_wait_for_new_device_returns_the_disk_that_appeared():
    before = {"/dev/sda": 50 * GIB}
    scans = iter([
        {"/dev/sda": 50 * GIB},                        # nothing yet
        {"/dev/sda": 50 * GIB, "/dev/sdb": 0},         # udev still settling: size not reported
        {"/dev/sda": 50 * GIB, "/dev/sdb": 50 * GIB},  # there it is
    ])
    slept = []
    path = dv.wait_for_new_device(before, 50 * GIB, timeout_s=60, scan=lambda: next(scans), poll_s=0.5,
                                  sleep=slept.append)
    assert path == "/dev/sdb"
    assert slept == [0.5, 0.5]


def test_wait_for_new_device_accepts_oci_size_slack():
    # Marketplace / size_in_gbs=47 vs a 50 GiB kernel disk — the export hang.
    before = {"/dev/sda": 50 * GIB}
    scan = lambda: {**before, "/dev/sdb": 50 * GIB}  # noqa: E731
    assert dv.wait_for_new_device(before, 47 * GIB, timeout_s=10, scan=scan, sleep=lambda s: None) == "/dev/sdb"
    assert dv.device_size_matches(47 * 1000**3, 47 * GIB)
    assert not dv.device_size_matches(100 * GIB, 50 * GIB)


def test_wait_for_new_device_ignores_other_sizes_and_times_out(monkeypatch):
    clock = iter([0, 0, 1, 2, 3, 100, 100, 100])
    monkeypatch.setattr(dv.time, "monotonic", lambda: next(clock))
    scan = lambda: {"/dev/sda": 50 * GIB, "/dev/sdb": 100 * GIB}  # noqa: E731 - a data volume of another size
    with pytest.raises(RuntimeError, match=r"no new disk of 53687091200 bytes.*sdb \(107374182400 bytes\)"):
        dv.wait_for_new_device({"/dev/sda": 50 * GIB}, 50 * GIB, timeout_s=10, scan=scan, sleep=lambda s: None)


def test_wait_for_new_device_refuses_ambiguity():
    scan = lambda: {"/dev/sda": 50 * GIB, "/dev/sdb": 50 * GIB, "/dev/sdc": 50 * GIB}  # noqa: E731
    with pytest.raises(RuntimeError, match="2 new disks"):
        dv.wait_for_new_device({"/dev/sda": 50 * GIB}, 50 * GIB, timeout_s=10, scan=scan, sleep=lambda s: None)


def test_wait_for_new_device_honours_cancel():
    before = {"/dev/sda": 50 * GIB}
    scan = lambda: before  # noqa: E731

    def boom():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        dv.wait_for_new_device(before, 50 * GIB, timeout_s=10, scan=scan, sleep=lambda s: None,
                               check_cancel=boom)


def test_rescan_scsi_hosts_writes_scan(tmp_path):
    host = tmp_path / "host0"
    host.mkdir()
    (host / "scan").write_text("")
    dv.rescan_scsi_hosts(tmp_path)
    assert (host / "scan").read_text() == "- - -\n"
    dv.rescan_scsi_hosts(tmp_path / "missing")
