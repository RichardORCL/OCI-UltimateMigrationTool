"""Resource usage of the helper VM for the Setup page: CPU, memory, disk and network.

Everything comes from ``/proc`` and ``shutil.disk_usage`` - no extra dependency.  CPU utilisation and
network throughput are rates, so a background thread takes a sample every few seconds and keeps a short
history; the API returns the current values plus that history, which feeds the live chart in the UI
without the browser having to wait for two samples first.  Off Linux (developer machines) ``available``
is false and only the disk figures are filled in.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class DiskUsage(BaseModel):
    mount: str
    total_bytes: int
    used_bytes: int
    free_bytes: int


class StatPoint(BaseModel):
    t: float = Field(description="Unix time of the sample (seconds)")
    cpu_pct: Optional[float] = None  # 0-100 over all CPUs since the previous sample
    rx_bps: Optional[float] = None  # bytes/s received on all interfaces except lo
    tx_bps: Optional[float] = None


class SystemStats(BaseModel):
    available: bool = Field(description="False when /proc is not readable (not Linux): only disks are filled")
    note: str = ""
    sampled_at: Optional[datetime] = None
    interval_s: float
    history_s: int
    cpu_count: int
    cpu_pct: Optional[float] = None
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None
    mem_total_bytes: Optional[int] = None
    mem_used_bytes: Optional[int] = None  # total - available (what the kernel could not free for us)
    mem_available_bytes: Optional[int] = None
    swap_total_bytes: Optional[int] = None
    swap_used_bytes: Optional[int] = None
    disks: list[DiskUsage] = Field(default_factory=list)
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    net_rx_total_bytes: Optional[int] = None
    net_tx_total_bytes: Optional[int] = None
    uptime_s: Optional[float] = None
    history: list[StatPoint] = Field(default_factory=list)


@dataclass
class RawSample:
    mono: float  # monotonic clock
    wall: float  # unix time
    cpu_busy: int  # jiffies
    cpu_total: int
    rx: int  # bytes
    tx: int


class ProcReader:
    """Reads the few ``/proc`` files we need; every method returns ``None`` when the file is missing or
    unparsable so the sampler degrades instead of failing."""

    def __init__(self, root: str | Path = "/proc"):
        self.root = Path(root)

    def _read(self, name: str) -> Optional[str]:
        try:
            return (self.root / name).read_text()
        except OSError:
            return None

    def cpu(self) -> Optional[tuple[int, int]]:
        """(busy, total) jiffies of the aggregate ``cpu`` line; iowait counts as idle."""
        text = self._read("stat")
        if not text:
            return None
        for line in text.splitlines():
            if line.startswith("cpu "):
                fields = [int(x) for x in line.split()[1:9]]  # user nice system idle iowait irq softirq steal
                if len(fields) < 4:
                    return None
                fields += [0] * (8 - len(fields))
                total = sum(fields)
                idle = fields[3] + fields[4]
                return total - idle, total
        return None

    def meminfo(self) -> Optional[dict[str, int]]:
        """Values in bytes, keyed like the file (MemTotal, MemAvailable, SwapTotal, SwapFree, ...)."""
        text = self._read("meminfo")
        if not text:
            return None
        out: dict[str, int] = {}
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            out[key.strip()] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
        return out or None

    def net(self) -> Optional[tuple[int, int]]:
        """(rx bytes, tx bytes) summed over all interfaces except the loopback."""
        text = self._read("net/dev")
        if not text:
            return None
        rx = tx = 0
        seen = False
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            fields = rest.split()
            if not fields or len(fields) < 9 or name.strip() == "lo":
                continue
            rx += int(fields[0])
            tx += int(fields[8])
            seen = True
        return (rx, tx) if seen else None

    def uptime(self) -> Optional[float]:
        text = self._read("uptime")
        try:
            return float(text.split()[0]) if text else None
        except (ValueError, IndexError):
            return None


def _loadavg() -> Optional[tuple[float, float, float]]:
    try:
        return os.getloadavg()  # type: ignore[attr-defined]  # not on Windows
    except (AttributeError, OSError):
        return None


class StatsSampler:
    """Samples ``/proc`` every ``interval_s`` on a daemon thread and keeps ``history_s`` worth of points."""

    def __init__(
        self,
        reader: Optional[ProcReader] = None,
        disk_paths: tuple[str, ...] = ("/",),
        interval_s: float = 2.0,
        history_s: int = 600,
        disk_usage: Callable[[str], Any] = shutil.disk_usage,  # anything with .total/.used/.free
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.reader = reader or ProcReader()
        self.disk_paths = disk_paths
        self.interval_s = interval_s
        self.history_s = history_s
        self.disk_usage = disk_usage
        self.mono = mono
        self.wall = wall
        self._history: deque[StatPoint] = deque(maxlen=max(2, int(history_s / interval_s)))
        self._last: Optional[RawSample] = None
        self._latest: Optional[StatPoint] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self.sample_once()  # a first point right away so the UI has numbers after one interval
        self._thread = threading.Thread(target=self._loop, name="sysstat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 1)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 - a broken sample must not kill the thread
                log.exception("resource sampling failed")

    # -- sampling
    def _raw(self) -> Optional[RawSample]:
        cpu, net = self.reader.cpu(), self.reader.net()
        if cpu is None and net is None:
            return None
        return RawSample(mono=self.mono(), wall=self.wall(), cpu_busy=cpu[0] if cpu else 0,
                         cpu_total=cpu[1] if cpu else 0, rx=net[0] if net else 0, tx=net[1] if net else 0)

    def sample_once(self) -> Optional[StatPoint]:
        cur = self._raw()
        if cur is None:
            return None
        with self._lock:
            prev, self._last = self._last, cur
            if prev is None:
                return None
            dt = cur.mono - prev.mono
            if dt <= 0:
                return None
            point = StatPoint(t=cur.wall)
            jiffies = cur.cpu_total - prev.cpu_total
            if jiffies > 0:
                point.cpu_pct = round(max(0.0, min(100.0, 100.0 * (cur.cpu_busy - prev.cpu_busy) / jiffies)), 1)
            if cur.rx >= prev.rx and cur.tx >= prev.tx:  # counters reset (interface re-created): skip the rate
                point.rx_bps = round((cur.rx - prev.rx) / dt, 1)
                point.tx_bps = round((cur.tx - prev.tx) / dt, 1)
            self._history.append(point)
            self._latest = point
            return point

    # -- read side
    def disks(self) -> list[DiskUsage]:
        out = []
        for path in self.disk_paths:
            try:
                u = self.disk_usage(path)
            except OSError:
                continue
            out.append(DiskUsage(mount=path, total_bytes=int(u.total), used_bytes=int(u.used), free_bytes=int(u.free)))
        return out

    def current(self) -> SystemStats:
        with self._lock:
            latest, last = self._latest, self._last
            history = list(self._history)
        mem = self.reader.meminfo()
        stats = SystemStats(available=last is not None, interval_s=self.interval_s, history_s=self.history_s,
                            cpu_count=os.cpu_count() or 1, disks=self.disks(), history=history,
                            uptime_s=self.reader.uptime())
        if last is None:
            stats.note = "resource statistics need /proc (Linux); not available on this host"
            return stats
        stats.sampled_at = datetime.fromtimestamp(last.wall, tz=timezone.utc)
        stats.net_rx_total_bytes, stats.net_tx_total_bytes = last.rx, last.tx
        if latest is not None:
            stats.cpu_pct, stats.net_rx_bps, stats.net_tx_bps = latest.cpu_pct, latest.rx_bps, latest.tx_bps
        load = _loadavg()
        if load:
            stats.load_1m, stats.load_5m, stats.load_15m = (round(x, 2) for x in load)
        if mem and "MemTotal" in mem:
            total = mem["MemTotal"]
            available = mem.get("MemAvailable", mem.get("MemFree", 0))
            stats.mem_total_bytes, stats.mem_available_bytes = total, available
            stats.mem_used_bytes = max(0, total - available)
            if "SwapTotal" in mem:
                stats.swap_total_bytes = mem["SwapTotal"]
                stats.swap_used_bytes = max(0, mem["SwapTotal"] - mem.get("SwapFree", 0))
        return stats
