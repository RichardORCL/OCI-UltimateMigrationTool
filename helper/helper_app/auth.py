"""Cookie based authentication for the web UI / API."""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status

from helper_app.sessions import UserSession


def session_token(request: Request | WebSocket) -> str | None:
    return request.cookies.get(request.app.state.settings.session_cookie_name)


def session_from_websocket(ws: WebSocket) -> UserSession | None:
    """The logged-in session behind a WebSocket handshake (browsers send the cookie with it)."""
    return ws.app.state.sessions.get(session_token(ws))


def existing_session(request: Request) -> UserSession | None:
    return request.app.state.sessions.get(session_token(request))


def require_unlocked_if_protected(request: Request) -> UserSession | None:
    """When a UI password is configured, minting or replacing a session needs an already-unlocked cookie."""
    if not request.app.state.ui_password.required:
        return existing_session(request)
    session = existing_session(request)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UI password required")
    return session


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def require_session(request: Request) -> UserSession:
    """Any UI session: a vCenter login or the anonymous session of the ISO flow."""
    session = request.app.state.sessions.get(session_token(request))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    return session


async def require_vcenter_session(request: Request) -> UserSession:
    """A session with a vCenter connection behind it (VM inventory, VMware migrations)."""
    session = await require_session(request)
    if session.vc is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs a vCenter login")
    return session


async def require_azure_session(request: Request) -> UserSession:
    """A session with an Azure service principal behind it (Azure VM inventory and migrations)."""
    session = await require_session(request)
    if session.azure is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs an Azure login")
    return session


async def require_gcp_session(request: Request) -> UserSession:
    """A session with a GCP service account behind it (GCP VM inventory and migrations)."""
    session = await require_session(request)
    if session.gcp is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs a Google Cloud login")
    return session


async def require_aws_session(request: Request) -> UserSession:
    """A session with an AWS IAM login behind it (EC2 inventory and migrations)."""
    session = await require_session(request)
    if session.aws is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs an AWS login")
    return session
