"""SSH tunnel to the VNC port of an instance through the OCI console connection service.

Mirrors the ``vncConnectionString`` OCI hands out, but in-process with asyncssh instead of two ``ssh``
processes and a local listening port:

1. hop 1: SSH to the console service (``instance-console.<region>...:443``) as the connection OCID; its host
   key is checked against ``serviceHostKeyFingerprint`` from the API;
2. hop 2: SSH to ``<instance OCID>:22`` tunnelled through hop 1 (what ``-W %h:%p`` does);
3. a direct-tcpip channel from hop 2 to the ``-L`` target (``<instance OCID>:5900`` for VM instances,
   ``localhost:5900`` for bare metal ones) carries the RFB (VNC) bytes.

Both hops authenticate with the temporary key the console connection was created with.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional, Protocol

from helper_app.console.connection import ConsoleEndpoint

log = logging.getLogger(__name__)

# user names tried for the second hop, after the one OCI put into the connection string (if any): OCI's
# documented command lets ssh pick the local user name, so the service does not seem to care; the instance
# OCID is what the console's Windows instructions use
HOP2_USERNAMES = ("{instance}", "{connection}")


class TunnelStream(Protocol):
    """Byte stream to the VNC server; what the WebSocket bridge pumps."""

    async def read(self, n: int) -> bytes:  # b"" at EOF
        ...

    async def write(self, data: bytes) -> None:
        ...

    async def close(self) -> None:
        ...


TunnelFactory = Callable[[ConsoleEndpoint, object, str, float], Awaitable[TunnelStream]]


class TunnelError(RuntimeError):
    """The SSH tunnel could not be established (host key mismatch, authentication, timeout...)."""


def fingerprint_matches(expected: str, key) -> Optional[bool]:
    """Compare OCI's ``serviceHostKeyFingerprint`` with an asyncssh key.  ``None`` when there is nothing to
    compare against (empty or unrecognised fingerprint format)."""
    fp = (expected or "").strip()
    if not fp:
        return None
    if re.fullmatch(r"(MD5:)?([0-9a-fA-F]{2}:){15}[0-9a-fA-F]{2}", fp):
        actual = key.get_fingerprint("md5")
        return _norm_md5(actual) == _norm_md5(fp)
    if re.fullmatch(r"(SHA256:)?[A-Za-z0-9+/=]{43,44}", fp):
        actual = key.get_fingerprint("sha256")
        return _norm_sha(actual) == _norm_sha(fp)
    return None


def _norm_md5(s: str) -> str:
    return s.split(":", 1)[1].lower() if s.upper().startswith("MD5:") else s.lower()


def _norm_sha(s: str) -> str:
    s = s.split(":", 1)[1] if s.upper().startswith("SHA256:") else s
    return s.rstrip("=")


class _SshStream:
    def __init__(self, reader, writer, connections) -> None:
        self._reader = reader
        self._writer = writer
        self._connections = connections  # closed innermost first

    async def read(self, n: int) -> bytes:
        return await self._reader.read(n)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        for conn in self._connections:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        for conn in self._connections:
            try:
                await conn.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def open_vnc_stream(endpoint: ConsoleEndpoint, key, service_fingerprint: str,
                          timeout_s: float) -> TunnelStream:
    """Open the two SSH hops and the VNC channel; ``key`` is the asyncssh private key of the connection."""
    import asyncssh

    class ServiceHostClient(asyncssh.SSHClient):
        def validate_host_public_key(self, host, addr, port, host_key) -> bool:
            ok = fingerprint_matches(service_fingerprint, host_key)
            if ok is None:
                log.warning("console service %s:%s presented host key %s; OCI gave no comparable fingerprint (%r)",
                            host, port, host_key.get_fingerprint("sha256"), service_fingerprint)
                return True
            if not ok:
                log.error("console service %s:%s host key %s does not match OCI's fingerprint %s", host, port,
                          host_key.get_fingerprint("sha256"), service_fingerprint)
            return ok

    hop1_desc = f"{endpoint.proxy_host}:{endpoint.proxy_port}"
    hop2_desc = f"{endpoint.target_host}:22 through the console service"

    async def _open() -> TunnelStream:
        connections = []
        try:
            try:
                hop1 = await asyncssh.connect(
                    endpoint.proxy_host, endpoint.proxy_port, username=endpoint.proxy_user, client_keys=[key],
                    known_hosts=([], [], []), client_factory=ServiceHostClient, connect_timeout=timeout_s,
                )
            except asyncssh.HostKeyNotVerifiable as exc:
                raise TunnelError(f"host key of {endpoint.proxy_host} rejected: {exc}") from exc
            except (asyncssh.Error, OSError) as exc:
                raise TunnelError(f"SSH to the console service {hop1_desc} failed: {exc}") from exc
            connections.insert(0, hop1)
            hop2 = None
            errors = []
            users = [u for u in (endpoint.target_user,) if u]
            users += [t.format(instance=endpoint.target_host, connection=endpoint.proxy_user) for t in HOP2_USERNAMES]
            for user in dict.fromkeys(users):
                try:
                    hop2 = await asyncssh.connect(endpoint.target_host, 22, tunnel=hop1, username=user,
                                                  client_keys=[key], known_hosts=None, connect_timeout=timeout_s)
                    break
                except asyncssh.PermissionDenied as exc:
                    errors.append(f"{user}: {exc}")
                except (asyncssh.Error, OSError) as exc:
                    raise TunnelError(f"second SSH hop to {hop2_desc} failed: {exc}") from exc
            if hop2 is None:
                raise TunnelError("the console service refused the second SSH hop: " + "; ".join(errors))
            connections.insert(0, hop2)
            # the VNC channel: the -L target OCI named first (localhost on bare metal), the instance OCID as
            # the fallback (and the other way round), since the string has not always been consistent
            targets = list(dict.fromkeys(h for h in (endpoint.vnc_host, endpoint.target_host, "localhost") if h))
            last: Exception | None = None
            for host in targets:
                try:
                    reader, writer = await hop2.open_connection(host, endpoint.target_port)
                    if host != targets[0]:
                        log.info("console VNC channel to %s:%s opened after %s was refused", host, endpoint.target_port,
                                 targets[0])
                    return _SshStream(reader, writer, connections)
                except asyncssh.ChannelOpenError as exc:
                    last = exc
                    log.debug("console VNC channel to %s:%s refused: %s", host, endpoint.target_port, exc)
            raise TunnelError(f"VNC channel to port {endpoint.target_port} refused by the console service "
                              f"(tried {', '.join(targets)}): {last}")
        except BaseException:
            for conn in connections:
                conn.close()
            raise

    try:
        return await asyncio.wait_for(_open(), timeout_s)
    except TunnelError:
        raise
    except asyncio.TimeoutError:
        raise TunnelError(f"timed out after {timeout_s:.0f}s connecting to {hop1_desc}")
    except (asyncssh.Error, OSError) as exc:
        raise TunnelError(f"SSH tunnel through {hop1_desc} failed: {exc}") from exc
