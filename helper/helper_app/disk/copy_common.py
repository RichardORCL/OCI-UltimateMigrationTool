"""Thread-safe transfer accounting and bounded worker scheduling."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


@dataclass
class CopyStats:
    bytes_received: int = 0
    bytes_written: int = 0
    chunks_written: int = 0
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, received: int, written: int | None = None) -> None:
        written = received if written is None else written
        with self._lock:
            self.bytes_received += received
            self.bytes_written += written
            if written:
                self.chunks_written += 1


def run_workers(
    items: Iterable[T],
    process: Callable[[T], None],
    *,
    workers: int,
    check_cancel: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Bound in-flight work, stop scheduling after failure, join workers before propagating errors."""
    pending = iter(items)
    lock = threading.Lock()
    stopped = stop if stop is not None else threading.Event()
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            while not stopped.is_set():
                with lock:
                    if stopped.is_set():
                        return
                    try:
                        item = next(pending)
                    except StopIteration:
                        return
                if check_cancel:
                    check_cancel()
                process(item)
        except BaseException as exc:
            with lock:
                if not failures:
                    failures.append(exc)
                stopped.set()

    threads = [threading.Thread(target=worker, name=f"disk-copy-{i}", daemon=True) for i in range(max(1, workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0]
