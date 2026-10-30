"""Minimal NBD client (fixed newstyle handshake, simple replies) for reading a qemu-nbd export.

Only what a read-only copy needs: ``NBD_OPT_GO`` for the default export, ``NBD_CMD_READ`` and
``NBD_CMD_DISC``. It runs over any byte stream with ``write``/``read_exactly``/``close``, here an
SSH-forwarded unix socket on the Proxmox node.
"""

from __future__ import annotations

import queue
import struct
import threading
from typing import Callable, Protocol

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
REPLY_MAGIC = 0x0003E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1
FLAG_NO_ZEROES = 2
OPT_GO = 7
REP_ACK = 1
REP_INFO = 3
INFO_EXPORT = 0
REP_ERROR_BIT = 1 << 31
CMD_READ = 0
CMD_DISC = 2

MAX_READ = 32 * 1024 * 1024  # qemu-nbd rejects larger requests
MAX_OPTION_REPLY = 64 * 1024


class NbdError(OSError):
    """The NBD server misbehaved, refused a request, or the stream broke."""


class ByteStream(Protocol):
    def write(self, data: bytes) -> None: ...

    def read_exactly(self, n: int) -> bytes: ...

    def close(self) -> None: ...


class NbdClient:
    """One NBD connection. Not thread-safe; ``NbdPool`` hands one client to each worker."""

    def __init__(self, stream: ByteStream, export_name: str = ""):
        self._s = stream
        self._handle = 0
        self.size = 0
        self.flags = 0
        try:
            self._handshake(export_name.encode())
        except BaseException:
            stream.close()
            raise

    def _read(self, n: int) -> bytes:
        data = self._s.read_exactly(n)
        if len(data) != n:
            raise NbdError(f"NBD stream ended after {len(data)} of {n} bytes")
        return data

    def _handshake(self, name: bytes) -> None:
        magic, opt_magic, server_flags = struct.unpack(">8sQH", self._read(18))
        if magic != NBDMAGIC or opt_magic != IHAVEOPT:
            raise NbdError("not an NBD server (bad greeting)")
        if not server_flags & FLAG_FIXED_NEWSTYLE:
            raise NbdError("NBD server does not support the fixed newstyle handshake")
        self._s.write(struct.pack(">I", FLAG_FIXED_NEWSTYLE | (server_flags & FLAG_NO_ZEROES)))
        payload = struct.pack(">I", len(name)) + name + struct.pack(">H", 0)
        self._s.write(struct.pack(">QII", IHAVEOPT, OPT_GO, len(payload)) + payload)
        while True:
            magic, option, rtype, length = struct.unpack(">QIII", self._read(20))
            if magic != REPLY_MAGIC or option != OPT_GO:
                raise NbdError("unexpected NBD option reply")
            if length > MAX_OPTION_REPLY:
                raise NbdError(f"NBD option reply too large ({length} bytes)")
            data = self._read(length) if length else b""
            if rtype & REP_ERROR_BIT:
                raise NbdError(f"NBD server refused the export (reply 0x{rtype:x}): "
                               f"{data.decode(errors='replace')}".rstrip(": "))
            if rtype == REP_INFO and len(data) >= 12 and struct.unpack(">H", data[:2])[0] == INFO_EXPORT:
                self.size, self.flags = struct.unpack(">QH", data[2:12])
            elif rtype == REP_ACK:
                break
        if not self.size:
            raise NbdError("NBD server did not report the export size")

    def _request(self, cmd: int, offset: int, length: int) -> int:
        self._handle += 1
        self._s.write(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, cmd, self._handle, offset, length))
        return self._handle

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise NbdError(f"read {offset}+{length} is outside the {self.size}-byte export")
        parts = []
        while length > 0:
            n = min(length, MAX_READ)
            handle = self._request(CMD_READ, offset, n)
            magic, error, got = struct.unpack(">IIQ", self._read(16))
            if magic != SIMPLE_REPLY_MAGIC or got != handle:
                raise NbdError("unexpected NBD reply")
            if error:
                raise NbdError(f"NBD read {offset}+{n} failed with error {error}")
            parts.append(self._read(n))
            offset += n
            length -= n
        return b"".join(parts)

    def close(self) -> None:
        try:
            self._request(CMD_DISC, 0, 0)
        except (OSError, NbdError):
            pass
        finally:
            self._s.close()


class NbdPool:
    """Thread-safe ``fetch(offset, length)`` over up to ``size`` NBD connections.

    A connection that fails is closed and dropped, so the retry of that chunk opens a fresh one.
    """

    def __init__(self, connect: Callable[[], NbdClient], size: int):
        self._connect = connect
        self._size = max(1, size)
        self._idle: queue.SimpleQueue[NbdClient] = queue.SimpleQueue()
        self._open = 0
        self._all: list[NbdClient] = []
        self._lock = threading.Lock()
        self.export_size = 0

    def _take(self) -> NbdClient:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            create = self._open < self._size
            if create:
                self._open += 1
        if not create:
            return self._idle.get()
        try:
            client = self._connect()
        except BaseException:
            with self._lock:
                self._open -= 1
            raise
        with self._lock:
            self._all.append(client)
            self.export_size = client.size
        return client

    def _drop(self, client: NbdClient) -> None:
        with self._lock:
            self._open -= 1
            if client in self._all:
                self._all.remove(client)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    def fetch(self, offset: int, length: int) -> bytes:
        client = self._take()
        try:
            data = client.read(offset, length)
        except BaseException:
            self._drop(client)
            raise
        self._idle.put(client)
        return data

    def close(self) -> None:
        with self._lock:
            clients = list(self._all)
            self._all.clear()
            self._open = 0
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
