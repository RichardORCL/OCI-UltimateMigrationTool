"""Console sessions per job: temporary key, OCI console connection, tunnel hand-out, idle cleanup.

The private key never leaves this process (it is not written to disk); it is discarded with the session.
After a helper restart the key is gone, so a helper-tagged connection found on the instance is deleted and
recreated on the next open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from helper_app.config import Settings
from helper_app.console import connection as cc
from helper_app.console.tunnel import TunnelFactory, TunnelStream, open_vnc_stream
from helper_app.models import Job
from helper_app.oci.clients import OciClients, describe_error

log = logging.getLogger(__name__)


@dataclass
class ConsoleSession:
    """One console connection.  Sessions are keyed by the job id (console of a migration / ISO job) or by
    the instance OCID (OCI Remote Console page, any instance)."""

    key_id: str
    instance_id: str
    compartment_id: str
    created_by: str
    job_id: Optional[str] = None
    state: str = "CREATING"  # CREATING | ACTIVE | FAILED
    error: Optional[str] = None
    connection: Optional[cc.ConsoleConnectionInfo] = None
    key: object = None  # asyncssh private key, in memory only
    viewers: int = 0
    last_used: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = None

    def status(self) -> dict:
        return {
            "job_id": self.job_id,
            "instance_id": self.instance_id,
            "state": self.state,
            "connection_id": self.connection.id if self.connection else None,
            "viewers": self.viewers,
            "error": self.error,
            "created_by": self.created_by,
            "idle_s": 0 if self.viewers else int(time.monotonic() - self.last_used),
        }


class ConsoleNotReady(RuntimeError):
    """No ACTIVE console session for the job."""


class ConsoleManager:
    def __init__(self, clients: OciClients, settings: Settings, tunnel_factory: TunnelFactory = open_vnc_stream):
        self.c = clients
        self.s = settings
        self.tunnel_factory = tunnel_factory
        self.sessions: dict[str, ConsoleSession] = {}
        self._reaper: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def close_all(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        for job_id in list(self.sessions):
            try:
                await self.close(job_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("closing console of job %s: %s", job_id, exc)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(min(30, max(1, self.s.console_idle_timeout_s / 4)))
            try:
                await self.reap_idle()
            except Exception as exc:  # noqa: BLE001  # pragma: no cover
                log.warning("console reaper: %s", exc)

    async def reap_idle(self, now: Optional[float] = None) -> list[str]:
        """Delete console connections nobody has looked at for ``console_idle_timeout_s``."""
        now = time.monotonic() if now is None else now
        reaped = []
        for job_id, sess in list(self.sessions.items()):
            if sess.state == "CREATING" or sess.viewers:
                continue
            if now - sess.last_used >= self.s.console_idle_timeout_s:
                log.info("console of job %s idle for %.0fs; deleting the console connection", job_id,
                         now - sess.last_used)
                await self.close(job_id)
                reaped.append(job_id)
        return reaped

    # ------------------------------------------------------------------- sessions
    def status(self, job_id: str) -> Optional[dict]:
        sess = self.sessions.get(job_id)
        return sess.status() if sess else None

    async def open(self, job: Job, user: str, replace: bool = False) -> dict:
        """Create the console connection for the job's instance (idempotent while one is being created or
        active).  Raises ``ConsoleConflict`` when a foreign connection is in the way and ``replace`` is off."""
        assert job.instance_id
        return await self.open_target(job.id, job.instance_id, job.target.compartment_id, user, replace,
                                      job_id=job.id)

    async def open_instance(self, instance_id: str, compartment_id: str, user: str, replace: bool = False) -> dict:
        """Console connection for any instance (OCI Remote Console page), keyed by the instance OCID.  When
        a job console for the same instance is already active it is reused."""
        existing = self.find_for_instance(instance_id)
        if existing is not None:
            sess = self.sessions[existing]
            sess.last_used = time.monotonic()
            return sess.status()
        return await self.open_target(instance_id, instance_id, compartment_id, user, replace)

    async def open_target(self, key_id: str, instance_id: str, compartment_id: str, user: str, replace: bool,
                          job_id: Optional[str] = None) -> dict:
        async with self._lock:
            sess = self.sessions.get(key_id)
            if sess and sess.state in ("CREATING", "ACTIVE"):
                sess.last_used = time.monotonic()
                return sess.status()
            # the console connection lives in the instance's compartment
            sess = ConsoleSession(key_id=key_id, instance_id=instance_id, compartment_id=compartment_id,
                                  created_by=user, job_id=job_id)
            # room first, synchronously: a foreign connection must be reported to the caller right away
            await asyncio.to_thread(cc.ensure_no_other, self.c, sess.compartment_id, sess.instance_id, replace,
                                    self.s.console_connect_timeout_s)
            self.sessions[key_id] = sess
            sess.task = asyncio.get_running_loop().create_task(self._create(sess))
            return sess.status()

    def find_for_instance(self, instance_id: str) -> Optional[str]:
        """Key of the live session (job or instance keyed) that serves ``instance_id``."""
        for key_id, sess in self.sessions.items():
            if sess.instance_id == instance_id and sess.state in ("CREATING", "ACTIVE"):
                return key_id
        return None

    async def _create(self, sess: ConsoleSession) -> None:
        import asyncssh

        try:
            key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
            public_key = key.export_public_key("openssh").decode().strip()
            info = await asyncio.to_thread(cc.create, self.c, sess.instance_id, sess.compartment_id, public_key,
                                           sess.job_id or "", self.s.console_connect_timeout_s)
            sess.key = key
            sess.connection = info
            sess.state = "ACTIVE"
            sess.last_used = time.monotonic()
            log.info("console connection %s for %s (instance %s) is active; OCI connection string: %s",
                     info.id, sess.key_id, sess.instance_id, info.vnc_connection_string)
        except Exception as exc:  # noqa: BLE001
            sess.state = "FAILED"
            sess.error = describe_error(exc)
            log.warning("console connection for %s failed: %s", sess.key_id, sess.error)

    async def close(self, job_id: str) -> Optional[dict]:
        sess = self.sessions.pop(job_id, None)
        if sess is None:
            return None
        if sess.task and not sess.task.done():
            sess.task.cancel()
        if sess.connection:
            try:
                await asyncio.to_thread(cc.delete, self.c, sess.connection.id, self.s.console_connect_timeout_s,
                                        False)
                log.info("console connection %s of job %s deleted", sess.connection.id, job_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("deleting console connection %s: %s", sess.connection.id, describe_error(exc))
        sess.key = None
        sess.state = "CLOSED"
        return sess.status()

    # --------------------------------------------------------------------- tunnel
    async def connect(self, job_id: str) -> TunnelStream:
        sess = self.sessions.get(job_id)
        if sess is None or sess.state != "ACTIVE" or sess.connection is None:
            raise ConsoleNotReady(f"no active console connection for job {job_id}")
        sess.viewers += 1
        sess.last_used = time.monotonic()
        try:
            return await self.tunnel_factory(sess.connection.endpoint, sess.key,
                                             sess.connection.service_host_key_fingerprint,
                                             self.s.console_connect_timeout_s)
        except BaseException:
            self.release(job_id)
            raise

    def release(self, job_id: str) -> None:
        sess = self.sessions.get(job_id)
        if sess is not None:
            sess.viewers = max(0, sess.viewers - 1)
            sess.last_used = time.monotonic()
