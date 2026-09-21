"""Setup page: helper information and self-update."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from helper_app import __version__, logging_config, runtime_settings
from helper_app.auth import require_session, session_token
from helper_app.logging_config import LoggingSettings, LoggingStatus
from helper_app.models import JobPhase
from helper_app.runtime_settings import OperationSettings, OperationStatus
from helper_app.sysstat import SystemStats
from helper_app.ui_password import MIN_PASSWORD_LENGTH, mark_prompt_done
from helper_app.updater import SoftwareStatus, UpdateError

log = logging.getLogger(__name__)
# any UI session: the Setup page (logging, concurrency, software update, image cleanup) needs no vCenter
router = APIRouter(prefix="/api/setup", tags=["setup"], dependencies=[Depends(require_session)])


class UpdateRequest(BaseModel):
    force: bool = False  # update even though migrations are running (they will fail)


def _active_jobs(request: Request) -> int:
    return len(request.app.state.store.active())


@router.get("/info")
def setup_info(request: Request):
    st = request.app.state
    ident = st.clients.identity_info
    s = st.settings
    return {
        "version": __version__,
        "commit": st.commit,
        "instance_id": ident.instance_id,
        "compartment_id": ident.compartment_id,
        "region": ident.region,
        "availability_domain": ident.availability_domain,
        "tenancy_id": ident.tenancy_id,
        "default_vcenter": f"{s.vcenter_host}:{s.vcenter_port}" if s.vcenter_host else "",
        "seed_bucket": s.seed_bucket,
        "default_shape": s.default_shape,
        "max_concurrent_jobs": s.max_concurrent_jobs,
        "session_ttl_s": s.session_ttl_s,
        "sessions": len(st.sessions),
        "active_jobs": _active_jobs(request),
    }


@router.get("/stats", response_model=SystemStats)
def setup_stats(request: Request):
    """Resource usage of the helper VM (CPU, memory, disk, network) with a short history for the chart."""
    return request.app.state.stats.current()


class JobsPurgeResult(BaseModel):
    scope: Literal["failed", "all"]
    deleted: int  # job records removed
    kept_active: int  # jobs still running/queued (never deleted)


@router.delete("/jobs", response_model=JobsPurgeResult)
def purge_jobs(request: Request, scope: Literal["failed", "all"] = "failed"):
    """Remove job records from the helper: ``failed`` = FAILED and CANCELLED jobs, ``all`` = every finished
    job (COMPLETED too).  Running or queued jobs are never touched.  Only the records disappear; OCI
    resources a failed job left behind (instance, volumes) are not cleaned up here - cancel the job first
    for that."""
    st = request.app.state
    phases = {JobPhase.FAILED, JobPhase.CANCELLED} if scope == "failed" else None
    deleted = st.store.delete_finished(phases)
    log.info("deleted %d %s job record(s) on request of the Setup page", deleted, scope)
    return JobsPurgeResult(scope=scope, deleted=deleted, kept_active=_active_jobs(request))


@router.get("/logging", response_model=LoggingStatus)
def get_logging(request: Request):
    return logging_config.current(request.app.state.settings)


@router.put("/logging", response_model=LoggingStatus)
def put_logging(body: LoggingSettings, request: Request):
    return logging_config.update(request.app.state.settings, body)


@router.get("/operation", response_model=OperationStatus)
def get_operation(request: Request):
    return runtime_settings.current(request.app.state.settings)


@router.put("/operation", response_model=OperationStatus)
def put_operation(body: OperationSettings, request: Request):
    """Concurrency and session idle timeout: applied at once (queued jobs start as slots open, existing
    logins get the new timeout) and persisted for the next start."""
    st = request.app.state
    st.runner.set_max_concurrent(body.max_concurrent_jobs)
    st.settings.session_ttl_s = body.session_ttl_s
    st.sessions.set_ttl(body.session_ttl_s)
    log.warning("operation settings changed: max_concurrent_jobs=%s session_ttl_s=%s",
                body.max_concurrent_jobs, body.session_ttl_s)
    status_ = runtime_settings.current(st.settings)
    warning = runtime_settings.write(st.settings, body.model_dump())
    status_.persisted = warning is None
    status_.warning = warning or ""
    return status_


class UiPasswordChange(BaseModel):
    current_password: str = ""
    new_password: str = ""  # empty clears the password when current_password matches


class UiPasswordStatus(BaseModel):
    required: bool


@router.get("/ui-password", response_model=UiPasswordStatus)
def get_ui_password(request: Request):
    return UiPasswordStatus(required=request.app.state.ui_password.required)


@router.put("/ui-password", response_model=UiPasswordStatus)
def put_ui_password(body: UiPasswordChange, request: Request):
    """Set, change or remove the optional web UI password.  The hash is the only thing persisted."""
    store = request.app.state.ui_password
    if store.required and not store.verify(body.current_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current UI password is incorrect")
    if body.new_password:
        if len(body.new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        store.set_password(body.new_password)
    elif store.required:
        store.clear()
    mark_prompt_done(request.app.state.settings)
    request.app.state.sessions.logout_others(session_token(request))
    log.warning("UI password %s", "changed" if store.required else "removed")
    return UiPasswordStatus(required=store.required)


@router.get("/software", response_model=SoftwareStatus)
async def software_status(request: Request, check: bool = True):
    updater = request.app.state.updater
    return await asyncio.to_thread(updater.status, _active_jobs(request), check)


@router.post("/software/update", response_model=SoftwareStatus, status_code=status.HTTP_202_ACCEPTED)
async def software_update(request: Request, body: UpdateRequest = UpdateRequest()):
    updater = request.app.state.updater
    try:
        await asyncio.to_thread(updater.start, _active_jobs(request), body.force)
    except UpdateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return await asyncio.to_thread(updater.status, _active_jobs(request), False)
