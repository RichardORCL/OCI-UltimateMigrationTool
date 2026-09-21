"""Copy allocated EBS snapshot blocks onto a ``PositionalWriter`` via the EBS Direct APIs."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from helper_app.aws.client import AwsClient, AwsError
from helper_app.disk.copy_common import CopyStats as EbsCopyStats
from helper_app.disk.copy_common import run_workers
from helper_app.disk.writer import PositionalWriter

log = logging.getLogger(__name__)

DEFAULT_RETRIES = 3


class EbsCopyError(RuntimeError):
    pass


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
    stop = threading.Event()
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

    def process(block: tuple[int, str]) -> None:
        index, token = block
        data = fetch(index, token)
        off = index * block_size
        if off >= disk_bytes:
            return
        data = data[:disk_bytes - off]
        writer.write_at(off, data)
        stats.add(len(data))
        if on_progress:
            on_progress(len(data))

    run_workers(blocks, process, workers=min(workers, len(blocks)), check_cancel=check_cancel, stop=stop)
    return stats
