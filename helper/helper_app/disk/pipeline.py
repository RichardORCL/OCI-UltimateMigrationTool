"""Run the VMDK decoder (inflate + ``pwrite``) on its own thread behind a bounded queue.

Without this the export loop is strictly serial: while ``zlib`` inflates a grain and the kernel
writes it, nothing reads from the NFC socket, and while the socket is being read nothing is
decoded.  With the pipeline the download thread only hands chunks over, so the socket is drained
while the previous chunks are still being decoded and written.  The queue is bounded so a slow
volume back-pressures the download instead of buffering the whole disk in memory.
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

from helper_app.disk.vmdk_stream import DecodeStats, StreamOptimizedDecoder

_EOF = None


class PipelinedDecoder:
    """Drop-in for ``StreamOptimizedDecoder`` (``feed`` / ``finish`` / ``stats``) with the work
    done on a worker thread.  Errors raised by the decoder or the writer surface from ``feed`` or
    ``finish`` on the calling thread with their original type, so retry logic stays unchanged.
    Always call ``abort()`` (idempotent) when giving up, before closing the underlying writer.
    """

    def __init__(self, decoder: StreamOptimizedDecoder, depth: int = 8, name: str = "vmdk-decode"):
        if depth < 1:
            raise ValueError("pipeline depth must be at least 1")
        self._decoder = decoder
        self._q: queue.Queue = queue.Queue(maxsize=depth)
        self._error: Optional[BaseException] = None
        self._failed = threading.Event()
        self._stop = threading.Event()
        self._done = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ decoder API
    @property
    def stats(self) -> DecodeStats:
        return self._decoder.stats

    @property
    def depth(self) -> int:
        return self._q.maxsize

    @property
    def queued(self) -> int:
        return self._q.qsize()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._raise_if_failed()
        while True:
            try:
                self._q.put(chunk, timeout=0.5)
                return
            except queue.Full:
                self._raise_if_failed()

    def finish(self) -> DecodeStats:
        """Flush the queue, stop the worker and finalise the decoder on the calling thread."""
        if self._done:
            return self._decoder.stats
        self._raise_if_failed()
        self._q.put(_EOF)
        self._thread.join()
        self._done = True
        self._raise_if_failed()
        return self._decoder.finish()

    def abort(self) -> None:
        """Stop the worker without finalising; safe to call any time, including after ``finish``."""
        if self._done:
            return
        self._stop.set()
        if self._error is None:
            self._error = RuntimeError("decode pipeline aborted")  # makes a blocked/late feed() raise
            self._failed.set()
        # unblock a worker waiting on an empty queue and discard whatever is still buffered
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        try:
            self._q.put_nowait(_EOF)
        except queue.Full:
            pass
        self._thread.join(timeout=30)
        self._done = True

    # ---------------------------------------------------------------- worker
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                chunk = self._q.get()
                if chunk is _EOF:
                    return
                self._decoder.feed(chunk)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            self._error = exc
            self._failed.set()
            self._stop.set()
            # let a producer blocked on a full queue notice the failure
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break

    def _raise_if_failed(self) -> None:
        if self._failed.is_set() and self._error is not None:
            raise self._error
