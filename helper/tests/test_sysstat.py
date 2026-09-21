"""Helper resource usage (Setup page): /proc parsing, rate computation and the degraded (non-Linux) case."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

from helper_app.sysstat import ProcReader, StatsSampler


def write_proc(root: Path, *, cpu=(100, 10, 50, 800, 20, 0, 5, 0), rx=1_000_000, tx=500_000, mem_avail_kb=6_000_000):
    root.mkdir(exist_ok=True)
    (root / "stat").write_text("cpu  " + " ".join(str(x) for x in cpu) + " 0 0\n"
                               "cpu0 " + " ".join(str(x) for x in cpu) + " 0 0\n"
                               "intr 12345\nctxt 999\n")
    (root / "meminfo").write_text(f"MemTotal:        8000000 kB\nMemFree:         1000000 kB\n"
                                  f"MemAvailable:    {mem_avail_kb} kB\nBuffers:          200000 kB\n"
                                  "SwapTotal:       2000000 kB\nSwapFree:        1500000 kB\n"
                                  "HugePages_Total:       0\n")
    (root / "net").mkdir(exist_ok=True)
    (root / "net" / "dev").write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo: 555555    1000    0    0    0     0          0         0   555555    1000    0    0    0     0       0          0\n"
        f"  ens3: {rx}   20000    0    0    0     0          0         0   {tx}   15000    0    0    0     0       0          0\n"
        f"  ens4: {rx // 10}   2000    0    0    0     0          0         0   {tx // 10}   1500    0    0    0     0       0          0\n")
    (root / "uptime").write_text("12345.67 45000.00\n")


def test_proc_reader(tmp_path):
    write_proc(tmp_path / "proc")
    r = ProcReader(tmp_path / "proc")
    busy, total = r.cpu()
    assert total == 100 + 10 + 50 + 800 + 20 + 0 + 5 + 0 and busy == total - 800 - 20
    mem = r.meminfo()
    assert mem["MemTotal"] == 8_000_000 * 1024 and mem["HugePages_Total"] == 0
    assert r.net() == (1_100_000, 550_000)  # lo excluded, ens3 + ens4 summed
    assert r.uptime() == 12345.67
    # missing files degrade to None
    empty = ProcReader(tmp_path / "nowhere")
    assert empty.cpu() is None and empty.meminfo() is None and empty.net() is None and empty.uptime() is None


def test_sampler_rates_and_history(tmp_path):
    proc = tmp_path / "proc"
    write_proc(proc)
    clock = {"mono": 100.0, "wall": 1_700_000_000.0}
    du = lambda path: NS(total=100 * 2**30, used=40 * 2**30, free=60 * 2**30)  # noqa: E731
    s = StatsSampler(ProcReader(proc), disk_paths=("/",), interval_s=2.0, history_s=20, disk_usage=du,
                     mono=lambda: clock["mono"], wall=lambda: clock["wall"])
    assert s.sample_once() is None  # first sample: no rate yet
    st = s.current()
    assert st.available and st.cpu_pct is None and st.history == []
    assert st.mem_total_bytes == 8_000_000 * 1024 and st.mem_used_bytes == 2_000_000 * 1024
    assert st.swap_total_bytes == 2_000_000 * 1024 and st.swap_used_bytes == 500_000 * 1024
    assert st.disks[0].mount == "/" and st.disks[0].free_bytes == 60 * 2**30
    assert st.uptime_s == 12345.67 and st.net_rx_total_bytes == 1_100_000

    # 2 s later: 100 more jiffies of which 25 busy; 4.4 MB received and 2.2 MB sent over ens3 + ens4
    write_proc(proc, cpu=(120, 10, 55, 875, 20, 0, 5, 0), rx=5_000_000, tx=2_500_000)
    clock["mono"] += 2.0
    clock["wall"] += 2.0
    p = s.sample_once()
    assert p.cpu_pct == 25.0
    assert p.rx_bps == 2_200_000.0 and p.tx_bps == 1_100_000.0
    st = s.current()
    assert st.cpu_pct == 25.0 and st.net_rx_bps == 2_200_000.0 and len(st.history) == 1
    assert st.sampled_at.timestamp() == clock["wall"]

    # counters going backwards (interface re-created): CPU still computed, no network rate for that point
    write_proc(proc, cpu=(220, 10, 55, 875, 20, 0, 5, 0), rx=10, tx=10)
    clock["mono"] += 2.0
    p = s.sample_once()
    assert p.cpu_pct == 100.0 and p.rx_bps is None and p.tx_bps is None

    # the history is bounded to history_s / interval_s points
    for _ in range(30):
        clock["mono"] += 2.0
        s.sample_once()
    assert len(s.current().history) == 10


def test_sampler_without_proc(tmp_path):
    du = lambda path: NS(total=10, used=4, free=6)  # noqa: E731
    s = StatsSampler(ProcReader(tmp_path / "missing"), disk_usage=du)
    assert s.sample_once() is None
    st = s.current()
    assert st.available is False and "/proc" in st.note
    assert st.cpu_pct is None and st.mem_total_bytes is None and st.history == []
    assert [d.free_bytes for d in st.disks] == [6]


def test_sampler_thread_start_stop(tmp_path):
    write_proc(tmp_path / "proc")
    s = StatsSampler(ProcReader(tmp_path / "proc"), interval_s=0.05, history_s=1)
    s.start()
    s.start()  # idempotent
    import time

    time.sleep(0.3)
    s.stop()
    assert s.current().available
    assert s._thread is None
