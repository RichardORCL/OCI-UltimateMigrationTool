"""Web UI sessions: opaque cookies with optional VMware, OLVM, Azure, GCP or AWS credentials.

A session that started a migration is *pinned* by that job: logging out or idling past the
TTL marks the session dead, but the underlying vCenter connection is only closed once the last
pinned job has finished, so a running export never loses its lease.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from helper_app.aws.session import AwsSession
from helper_app.azure.session import AzureSession
from helper_app.gcp.session import GcpSession
from helper_app.models import SessionInfo
from helper_app.olvm.session import OlvmSession
from helper_app.vsphere.session import VCenterSession

log = logging.getLogger(__name__)


ANONYMOUS_USER = "anonymous"


class UserSession:
    """A web UI session.  ``vc`` is the vCenter connection of a logged-in user, ``azure`` the service
    principal of an Azure login; GCP/AWS have their own backends. All are ``None`` for anonymous sessions."""

    def __init__(self, token: str, vc: Optional[VCenterSession], ttl_s: float,
                 azure: Optional[AzureSession] = None, gcp: Optional[GcpSession] = None,
                 aws: Optional[AwsSession] = None, olvm: Optional[OlvmSession] = None):
        self.token = token
        self.vc = vc
        self.azure = azure
        self.gcp = gcp
        self.aws = aws
        self.olvm = olvm
        if vc is not None:
            self.username = vc.username
        elif azure is not None:
            self.username = azure.username
        elif gcp is not None:
            self.username = gcp.username
        elif aws is not None:
            self.username = aws.username
        elif olvm is not None:
            self.username = olvm.username
        else:
            self.username = ANONYMOUS_USER
        self.created_at = datetime.now(timezone.utc)
        self.ttl_s = ttl_s
        self._last_used = time.monotonic()
        self._pins: set[str] = set()
        self._dead = False
        self._lock = threading.Lock()
        self.cache: dict[str, object] = {}  # per-session scratch space (e.g. the VM list)

    def cloud_backend(self, kind: str):
        """Return credentials for a supported cloud job, never an arbitrary session attribute."""
        return {"azure": self.azure, "gcp": self.gcp, "aws": self.aws, "olvm": self.olvm}.get(kind)

    # ------------------------------------------------------------- lifetime
    def touch(self) -> None:
        with self._lock:
            self._last_used = time.monotonic()

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self._last_used

    @property
    def expired(self) -> bool:
        return self.idle_s > self.ttl_s

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def expires_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=max(0.0, self.ttl_s - self.idle_s))

    @property
    def anonymous(self) -> bool:
        return (self.vc is None and self.azure is None and self.gcp is None and self.aws is None
                and self.olvm is None)

    def info(self) -> SessionInfo:
        if self.anonymous:
            return SessionInfo(username=self.username, anonymous=True, created_at=self.created_at,
                               expires_at=self.expires_at)
        if self.aws is not None:
            return SessionInfo(
                username=self.username,
                aws_access_key_id=self.aws.access_key_id,
                aws_region=self.aws.region,
                aws_account_id=self.aws.account_id,
                created_at=self.created_at,
                expires_at=self.expires_at,
            )
        if self.gcp is not None:
            return SessionInfo(
                username=self.username,
                gcp_client_email=self.gcp.client.client_email,
                gcp_project_id=self.gcp.project_id,
                gcp_export_bucket=self.gcp.export_bucket,
                gcp_projects=self.gcp.projects,
                created_at=self.created_at,
                expires_at=self.expires_at,
            )
        if self.vc is None and self.azure is not None:
            return SessionInfo(username=self.username, azure_tenant_id=self.azure.tenant_id,
                               azure_client_id=self.azure.client_id, azure_subscriptions=self.azure.subscriptions,
                               created_at=self.created_at, expires_at=self.expires_at)
        if self.olvm is not None:
            return SessionInfo(username=self.username, olvm_engine=self.olvm.engine_host,
                               created_at=self.created_at, expires_at=self.expires_at)
        return SessionInfo(username=self.username, vcenter_host=self.vc.host,
                           vcenter_port=getattr(self.vc, "port", 443), vcenter_version=self.vc.version,
                           verify_ssl=bool(getattr(self.vc, "verify_ssl", False)),
                           created_at=self.created_at, expires_at=self.expires_at)

    # ------------------------------------------------------------- pinning
    def pin(self, job_id: str) -> None:
        with self._lock:
            self._pins.add(job_id)

    def unpin(self, job_id: str) -> None:
        with self._lock:
            self._pins.discard(job_id)
            close = self._dead and not self._pins
        if close:
            self._close_backends()

    @property
    def pinned_jobs(self) -> set[str]:
        with self._lock:
            return set(self._pins)

    def kill(self) -> None:
        """Mark the session unusable for the UI; disconnect from vCenter / Azure unless a job still needs it."""
        with self._lock:
            self._dead = True
            close = not self._pins
        if close:
            self._close_backends()

    def _close_backends(self) -> None:
        if self.vc is not None:
            self.vc.close()
        if self.azure is not None:
            self.azure.close()
        if self.gcp is not None:
            self.gcp.close()
        if self.aws is not None:
            self.aws.close()
        if self.olvm is not None:
            self.olvm.close()


class SessionStore:
    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        vc: Optional[VCenterSession],
        azure: Optional[AzureSession] = None,
        gcp: Optional[GcpSession] = None,
        aws: Optional[AwsSession] = None,
        olvm: Optional[OlvmSession] = None,
    ) -> UserSession:
        """New session for a vCenter login, a cloud or OLVM login, or an anonymous one for the ISO flow."""
        token = secrets.token_urlsafe(32)
        session = UserSession(token, vc, self.ttl_s, azure=azure, gcp=gcp, aws=aws, olvm=olvm)
        with self._lock:
            self._sessions[token] = session
        log.info("session created for %s", session.username)
        return session

    def set_ttl(self, ttl_s: float) -> None:
        """Change the idle timeout for new *and* existing sessions (Setup page)."""
        with self._lock:
            self.ttl_s = ttl_s
            for s in self._sessions.values():
                s.ttl_s = ttl_s

    def get(self, token: Optional[str]) -> Optional[UserSession]:
        if not token:
            return None
        self.sweep()
        with self._lock:
            session = self._sessions.get(token)
        if session is None or session.dead:
            return None
        session.touch()
        return session

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            session = self._sessions.pop(token, None)
        if session is not None:
            log.info("session of %s logged out (pinned jobs: %d)", session.username, len(session.pinned_jobs))
            session.kill()

    def logout_others(self, keep_token: Optional[str]) -> None:
        """Kill every session except ``keep_token`` (used when the UI password changes)."""
        with self._lock:
            dropped = [(t, s) for t, s in self._sessions.items() if t != keep_token]
            for t, _ in dropped:
                del self._sessions[t]
        for _, session in dropped:
            log.info("session of %s ended (UI password changed)", session.username)
            session.kill()

    def sweep(self) -> None:
        with self._lock:
            expired = [t for t, s in self._sessions.items() if s.expired]
            sessions = [self._sessions.pop(t) for t in expired]
        for s in sessions:
            log.info("session of %s expired", s.username)
            s.kill()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s._close_backends()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
