"""Remote (VNC) console of a migrated instance: console connection lifecycle + WebSocket <-> SSH bridge."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status

from helper_app.api.routes_jobs import _get_job
from helper_app.auth import require_session, session_from_websocket
from helper_app.console.connection import ConsoleConflict
from helper_app.console.manager import ConsoleNotReady
from helper_app.models import JobPhase
from helper_app.oci.clients import describe_error
from helper_app.sessions import UserSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["console"])

# WebSocket close codes handed to the browser (4000-4999 are application defined)
WS_NOT_READY = 4409
WS_TUNNEL_FAILED = 4502
READ_CHUNK = 64 * 1024


def _console_job(request: Request, job_id: str):
    job = _get_job(request, job_id)
    # completed migrations, and ISO installations while the installer runs (that is what the console is for)
    if job.phase not in (JobPhase.COMPLETED, JobPhase.INSTALLING) or not job.instance_id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "the remote console is available for completed migrations and running installations "
                            "with an OCI instance")
    return job


@router.post("/{job_id}/console", status_code=status.HTTP_202_ACCEPTED)
async def open_console(job_id: str, request: Request, replace: bool = False,
                       session: UserSession = Depends(require_session)):
    """Create (or reuse) the instance console connection for the job's instance.  ``replace`` deletes a
    console connection that was created outside the helper (OCI allows one per instance)."""
    job = _console_job(request, job_id)
    try:
        return await request.app.state.consoles.open(job, session.username, replace=replace)
    except ConsoleConflict as exc:
        raise foreign_connection_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, describe_error(exc))


@router.get("/{job_id}/console", dependencies=[Depends(require_session)])
def console_status(job_id: str, request: Request):
    _get_job(request, job_id)
    return request.app.state.consoles.status(job_id) or {"job_id": job_id, "state": "NONE", "connection_id": None,
                                                          "viewers": 0, "error": None, "created_by": None,
                                                          "idle_s": 0}


@router.delete("/{job_id}/console", dependencies=[Depends(require_session)])
async def close_console(job_id: str, request: Request):
    _get_job(request, job_id)
    closed = await request.app.state.consoles.close(job_id)
    return closed or {"job_id": job_id, "state": "NONE"}


def _same_origin(ws: WebSocket) -> bool:
    """Cross-site WebSocket hijacking guard: a browser's Origin must match the Host it connected to."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client (tests, curl); the cookie still has to be valid
    return urlsplit(origin).netloc.lower() == (ws.headers.get("host") or "").lower()


@router.websocket("/{job_id}/console/vnc")
async def console_vnc(ws: WebSocket, job_id: str):
    """RFB bytes between the browser (noVNC) and the instance's VNC port through the SSH tunnel."""
    if ws.app.state.store.get(job_id) is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await bridge_vnc(ws, job_id, f"job {job_id}")


def foreign_connection_error(exc: ConsoleConflict) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {
        "code": "foreign_connection",
        "connection_id": exc.connection_id,
        "message": "An instance console connection that was not created by the migration tool exists for this "
                   "instance (OCI allows one per instance). Replace it, or delete it in the OCI console first.",
    })


def ws_allowed(ws: WebSocket) -> bool:
    """Cookie session present and same origin; the caller closes the socket (403 handshake) otherwise."""
    return session_from_websocket(ws) is not None and _same_origin(ws)


async def bridge_vnc(ws: WebSocket, key_id: str, what: str) -> None:
    """Accept the WebSocket and pump RFB bytes between noVNC and the console session ``key_id`` (a job id or
    an instance OCID) until either side closes."""
    st = ws.app.state
    if not ws_allowed(ws):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)  # before accept: the handshake is denied (403)
        return
    # noVNC (older releases) asks for the "binary" subprotocol; echo whatever the client offered
    offered = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    subprotocol = "binary" if "binary" in offered else (offered[0] if offered else None)
    await ws.accept(subprotocol=subprotocol)
    try:
        stream = await st.consoles.connect(key_id)
    except ConsoleNotReady as exc:
        await ws.close(code=WS_NOT_READY, reason=str(exc)[:120])
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("console tunnel for %s: %s", what, exc)
        await ws.close(code=WS_TUNNEL_FAILED, reason=str(exc)[:120])
        return

    async def browser_to_instance():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is None and msg.get("text"):
                data = msg["text"].encode()
            if data:
                await stream.write(data)

    async def instance_to_browser():
        while True:
            data = await stream.read(READ_CHUNK)
            if not data:
                return
            await ws.send_bytes(data)

    tasks = [asyncio.ensure_future(browser_to_instance()), asyncio.ensure_future(instance_to_browser())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                log.info("console bridge of %s ended: %s", what, exc)
    finally:
        st.consoles.release(key_id)
        await stream.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001  # already closed by the browser
            pass
