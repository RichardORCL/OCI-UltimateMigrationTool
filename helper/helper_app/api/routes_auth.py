"""Login / logout with source-provider credentials or the shared UI password."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from helper_app.auth import (
    client_ip,
    existing_session,
    require_session,
    require_unlocked_if_protected,
    session_token,
)
from helper_app.aws.client import AwsAuthError, AwsError
from helper_app.azure.client import AzureAuthError, AzureError
from helper_app.gcp.client import GcpAuthError, GcpError
from helper_app.hyperv.client import HypervAuthError, HypervError
from helper_app.models import (
    AwsLoginRequest,
    AzureLoginRequest,
    GcpLoginRequest,
    HypervLoginRequest,
    LoginRequest,
    OlvmLoginRequest,
    SessionInfo,
)
from helper_app.olvm.client import OlvmAuthError, OlvmError
from helper_app.sessions import UserSession
from helper_app.ui_password import MIN_PASSWORD_LENGTH, mark_prompt_done, setup_pending
from helper_app.vsphere.session import VCenterAuthError, VCenterError

router = APIRouter(prefix="/api/auth", tags=["auth"])


class UnlockRequest(BaseModel):
    password: str


class FirstUseRequest(BaseModel):
    password: str = ""  # empty = continue without a UI password


@router.get("/config")
def auth_config(request: Request):
    """Unauthenticated: the defaults for the login page (vCenter, if one is configured, and TLS verification)."""
    settings = request.app.state.settings
    store = request.app.state.ui_password
    return {"vcenter_host": settings.vcenter_host, "vcenter_port": settings.vcenter_port,
            "verify_ssl": settings.vcenter_verify_ssl,
            "ui_password_required": store.required,
            "ui_password_setup_pending": setup_pending(settings, store)}


@router.post("/first-use", response_model=SessionInfo)
def first_use(body: FirstUseRequest, request: Request, response: Response):
    """First browser visit: set a UI password or explicitly continue without one."""
    st = request.app.state
    if st.ui_password.required:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "UI password already configured")
    if not setup_pending(st.settings, st.ui_password):
        existing = existing_session(request)
        if existing is not None:
            return existing.info()
        session = st.sessions.create(None)
        _set_cookie(st, response, session.token)
        return session.info()
    if body.password:
        if len(body.password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        st.ui_password.set_password(body.password)
        st.sessions.logout_others(session_token(request))
    mark_prompt_done(st.settings)
    existing = existing_session(request)
    if existing is not None:
        return existing.info()
    session = st.sessions.create(None)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/unlock", response_model=SessionInfo)
def unlock(body: UnlockRequest, request: Request, response: Response):
    """Create (or reuse) an anonymous session after verifying the optional UI password."""
    store = request.app.state.ui_password
    if not store.required:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no UI password configured")
    ip = client_ip(request)
    if store.rate_limited(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts")
    if not store.verify(body.password):
        store.record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid UI password")
    store.record_success(ip)
    existing = existing_session(request)
    if existing is not None:
        return existing.info()
    session = request.app.state.sessions.create(None)
    _set_cookie(request.app.state, response, session.token)
    return session.info()


@router.post("/login", response_model=SessionInfo)
async def login(body: LoginRequest, request: Request, response: Response):
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        vc = await asyncio.to_thread(st.vcenter.login, body.username, body.password, body.vcenter_host,
                                     body.vcenter_port, body.verify_ssl)
    except VCenterAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except VCenterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    # replace a previous session of this browser, if any
    st.sessions.logout(session_token(request))
    session = st.sessions.create(vc)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/azure/login", response_model=SessionInfo)
async def azure_login(body: AzureLoginRequest, request: Request, response: Response):
    """Log in with an Azure service principal (tenant, application/client ID, client secret).  The
    credentials stay in memory with the session, like a vCenter login."""
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        az = await asyncio.to_thread(st.azure.login, body.tenant_id, body.client_id, body.client_secret)
    except AzureAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except AzureError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, azure=az)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/gcp/login", response_model=SessionInfo)
async def gcp_login(body: GcpLoginRequest, request: Request, response: Response):
    """Log in with a GCP service account JSON key and export bucket."""
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        gcp = await asyncio.to_thread(st.gcp.login, body.service_account_json, body.export_bucket)
    except GcpAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except GcpError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, gcp=gcp)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/aws/login", response_model=SessionInfo)
async def aws_login(body: AwsLoginRequest, request: Request, response: Response):
    """Log in with an IAM access key, secret and region. Credentials stay in memory with the session."""
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        aws = await asyncio.to_thread(st.aws.login, body.access_key_id, body.secret_access_key, body.region)
    except AwsAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except AwsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, aws=aws)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/olvm/login", response_model=SessionInfo)
async def olvm_login(body: OlvmLoginRequest, request: Request, response: Response):
    """Log in to an OLVM engine. The password stays in memory with the session, like a vCenter login."""
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        olvm = await asyncio.to_thread(st.olvm.login, body.engine_url, body.username, body.password, body.verify_ssl)
    except OlvmAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except OlvmError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, olvm=olvm)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/hyperv/login", response_model=SessionInfo)
async def hyperv_login(body: HypervLoginRequest, request: Request, response: Response):
    """Log in to a Hyper-V host. The password stays in memory with the session, like a vCenter login."""
    require_unlocked_if_protected(request)
    st = request.app.state
    try:
        hyperv = await asyncio.to_thread(
            st.hyperv.login, body.host, body.username, body.password,
            use_https=body.use_https, verify_ssl=body.verify_ssl)
    except HypervAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except HypervError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, hyperv=hyperv)
    _set_cookie(st, response, session.token)
    return session.info()


def _set_cookie(st, response: Response, token: str) -> None:
    response.set_cookie(
        st.settings.session_cookie_name,
        token,
        httponly=True,
        secure=st.settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/anonymous", response_model=SessionInfo)
def anonymous(request: Request, response: Response):
    """Session for the ISO flow, which needs no vCenter.  A vCenter login already in this browser is kept
    (it can do everything the anonymous session can); otherwise an anonymous session is created."""
    st = request.app.state
    existing = existing_session(request)
    if existing is not None:
        return existing.info()
    require_unlocked_if_protected(request)
    session = st.sessions.create(None)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    st = request.app.state
    st.sessions.logout(session_token(request))
    response.delete_cookie(st.settings.session_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=SessionInfo)
def me(session: UserSession = Depends(require_session)):
    return session.info()
