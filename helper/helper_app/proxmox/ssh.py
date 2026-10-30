"""Synchronous SSH to a Proxmox node on top of asyncssh.

Job and request threads are synchronous, so the asyncssh connections live on one private event
loop thread and every call is handed over with ``run_coroutine_threadsafe``. A node shell runs
commands (``pvesm path``, ``qemu-img map``, ``qemu-nbd``) and opens forwarded unix-socket
streams to the NBD exports.

Host keys are trusted on first use: the login records the node's key fingerprint and later
connections to that node must present the same key.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import asyncssh

from helper_app.proxmox.client import ProxmoxAuthError, ProxmoxError
from helper_app.proxmox.nbd import ByteStream, NbdError

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 30.0
STREAM_TIMEOUT_S = 300.0


class SshError(ProxmoxError):
    """SSH to the Proxmox node failed, or a command on it failed."""


class SshAuthError(SshError, ProxmoxAuthError):
    """The node rejected the SSH user, password or key."""


@dataclass(frozen=True)
class SshCredentials:
    username: str
    password: str = ""
    private_key: str = ""  # OpenSSH or PEM private key text
    passphrase: str = ""
    port: int = 22

    @property
    def method(self) -> str:
        return "key" if self.private_key else "password"


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class NodeShell(Protocol):
    host: str
    fingerprint: str

    def run(self, command: str, timeout: float = 300.0) -> CommandResult: ...

    def open_unix(self, path: str) -> ByteStream: ...

    def close(self) -> None: ...


ShellFactory = Callable[[str, SshCredentials, str], NodeShell]


class _Loop:
    """One daemon thread running the asyncio loop shared by every SSH connection."""

    _instance: Optional["_Loop"] = None
    _guard = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.loop.run_forever, name="proxmox-ssh", daemon=True)
        thread.start()

    @classmethod
    def get(cls) -> "_Loop":
        with cls._guard:
            if cls._instance is None:
                cls._instance = _Loop()
            return cls._instance

    def call(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise


class _UnixStream:
    def __init__(self, loop: _Loop, reader: Any, writer: Any):
        self._loop = loop
        self._reader = reader
        self._writer = writer

    def write(self, data: bytes) -> None:
        async def send() -> None:
            self._writer.write(data)
            await self._writer.drain()

        try:
            self._loop.call(send(), STREAM_TIMEOUT_S)
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise NbdError(f"NBD stream write failed: {exc}") from exc

    def read_exactly(self, n: int) -> bytes:
        try:
            return self._loop.call(self._reader.readexactly(n), STREAM_TIMEOUT_S)
        except asyncio.IncompleteReadError as exc:
            raise NbdError(f"NBD stream closed after {len(exc.partial)} of {n} bytes") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise NbdError(f"NBD stream read failed: {exc}") from exc

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass


class AsyncSshShell:
    """SSH connection to one node, reconnected on demand. Thread-safe."""

    def __init__(self, host: str, creds: SshCredentials, expected_fingerprint: str = ""):
        self.host = host
        self.creds = creds
        self.fingerprint = expected_fingerprint
        self._loop = _Loop.get()
        self._conn: Any = None
        self._lock = threading.Lock()
        self._connect()

    async def _open(self) -> Any:
        keys: list[Any] = []
        if self.creds.private_key:
            try:
                keys = [asyncssh.import_private_key(self.creds.private_key, self.creds.passphrase or None)]
            except (asyncssh.KeyImportError, ValueError) as exc:
                raise SshAuthError(f"cannot read the SSH private key: {exc}") from exc
        return await asyncssh.connect(
            self.host, self.creds.port, username=self.creds.username,
            password=self.creds.password or None, client_keys=keys, agent_path=None,
            known_hosts=None, connect_timeout=CONNECT_TIMEOUT_S,
            keepalive_interval=30, preferred_auth="publickey,keyboard-interactive,password",
        )

    def _connect(self) -> Any:
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                conn = self._loop.call(self._open(), CONNECT_TIMEOUT_S + 15)
            except asyncssh.PermissionDenied as exc:
                raise SshAuthError(f"SSH login to {self.host} as {self.creds.username} was rejected") from exc
            except (asyncssh.Error, OSError, TimeoutError) as exc:
                raise SshError(f"SSH to {self.host}:{self.creds.port} failed: {exc}") from exc
            key = conn.get_server_host_key()
            fingerprint = key.get_fingerprint("sha256") if key is not None else ""
            if self.fingerprint and fingerprint != self.fingerprint:
                conn.close()
                raise SshError(f"SSH host key of {self.host} changed (expected {self.fingerprint}, got "
                               f"{fingerprint}); check the node before retrying")
            self.fingerprint = fingerprint
            self._conn = conn
            return conn

    def _reset(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def run(self, command: str, timeout: float = 300.0) -> CommandResult:
        for attempt in (1, 2):
            conn = self._connect()
            try:
                result = self._loop.call(conn.run(command, check=False, timeout=timeout), timeout + 15)
            except (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError) as exc:
                self._reset()
                if attempt == 2:
                    raise SshError(f"SSH to {self.host}: {exc}") from exc
                continue
            except (asyncssh.Error, OSError, TimeoutError) as exc:
                raise SshError(f"SSH command on {self.host} failed: {exc}") from exc
            status = result.exit_status if result.exit_status is not None else -1
            return CommandResult(status, str(result.stdout or ""), str(result.stderr or ""))
        raise SshError(f"SSH to {self.host} failed")

    def open_unix(self, path: str) -> ByteStream:
        for attempt in (1, 2):
            conn = self._connect()
            try:
                reader, writer = self._loop.call(conn.open_unix_connection(path, encoding=None),
                                                 CONNECT_TIMEOUT_S)
                return _UnixStream(self._loop, reader, writer)
            except (asyncssh.ConnectionLost, asyncssh.DisconnectError) as exc:
                self._reset()
                if attempt == 2:
                    raise NbdError(f"SSH to {self.host}: {exc}") from exc
            except (asyncssh.Error, OSError, TimeoutError) as exc:
                raise NbdError(f"cannot open {path} on {self.host} over SSH: {exc}") from exc
        raise NbdError(f"cannot open {path} on {self.host}")

    def close(self) -> None:
        self._reset()


def connect_shell(host: str, creds: SshCredentials, expected_fingerprint: str = "") -> NodeShell:
    return AsyncSshShell(host, creds, expected_fingerprint)
