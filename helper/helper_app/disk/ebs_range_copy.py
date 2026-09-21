"""Copy allocated EBS snapshot blocks onto a ``PositionalWriter`` via the EBS Direct APIs."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from helper_app.aws.client import AwsClient, AwsError
from helper_app.disk.writer import PositionalWriter

log = logging.getLogger(__name__)

DEFAULT_RETRIES = 3
_HAS_PWRITE = hasattr(os, "pwrite")


class EbsCopyError(RuntimeError):
    pass


@dataclass
class EbsCopyStats:
    bytes_received: int = 0
    bytes_written: int = 0
    chunks_written: int = 0
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes_received += n
            self.bytes_written += n
            self.chunks_written += 1


def list_blocks(client: AwsClient, snapshot_id: str, disk_bytes: int) -> tuple[int, list[tuple[int, str]]]:
    """Allocated snapshot blocks clipped to ``disk_bytes``."""
    block_size, blocks = client.list_snapshot_blocks(snapshot_id)
    out: list[tuple[int, str]] = []
    for index, token in blocks:
        off = index * block_size
        if off >= disk_bytes:
            continue
        out.append((index, token))
    return block_size, out


def copy_blocks(
    client: AwsClient,
    snapshot_id: str,
    blocks: list[tuple[int, str]],
    block_size: int,
    writer: PositionalWriter,
    disk_bytes: int,
    *,
    workers: int = 4,
    retries: int = DEFAULT_RETRIES,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EbsCopyStats:
    stats = EbsCopyStats()
    if not blocks:
        return stats
    queue = list(reversed(blocks))
    lock = threading.Lock()
    write_lock = threading.Lock() if not _HAS_PWRITE else None
    stop = threading.Event()
    failure: list[BaseException] = []

    def fail(exc: BaseException) -> None:
        with lock:
            if not failure:
                failure.append(exc)
        stop.set()

    def fetch(index: int, token: str) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            if stop.is_set():
                raise EbsCopyError("aborted")
            try:
                data = client.get_snapshot_block(snapshot_id, index, token)
                if not data:
                    raise EbsCopyError(f"block {index} returned empty")
                return data
            except (AwsError, EbsCopyError) as exc:
                last = exc
                if attempt < retries:
                    with stats._lock:
                        stats.retries += 1
                    sleep(min(10.0, 1.0 * attempt))
        raise EbsCopyError(f"{last}") from last

    def worker() -> None:
        try:
            while not stop.is_set():
                with lock:
                    if not queue:
                        return
                    index, token = queue.pop()
                if check_cancel is not None:
                    check_cancel()
                data = fetch(index, token)
                off = index * block_size
                if off >= disk_bytes:
                    continue
                data = data[: disk_bytes - off]
                if write_lock is not None:
                    with write_lock:
                        writer.write_at(off, data)
                else:
                    writer.write_at(off, data)
                stats.add(len(data))
                if on_progress is not None:
                    on_progress(len(data))
        except BaseException as exc:  # noqa: BLE001
            fail(exc)

    n = max(1, min(int(workers), len(blocks)))
    threads = [threading.Thread(target=worker, name=f"ebs-copy-{i}", daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failure:
        raise failure[0]
    return stats
