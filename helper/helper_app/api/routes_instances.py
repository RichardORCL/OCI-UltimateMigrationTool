"""OCI Remote Console page: compute instances by compartment or name search, and a console session for any
of them (independent of migration jobs)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status

from helper_app.api.routes_console import bridge_vnc, foreign_connection_error
from helper_app.auth import require_session
from helper_app.console.connection import ConsoleConflict
from helper_app.models import OciCompartment, OciInstance
from helper_app.oci.clients import describe_error
from helper_app.oci.options import list_compartments, list_instances, search_instances
from helper_app.sessions import UserSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["instances"])

_GONE = ("TERMINATED", "TERMINATING")


def _paths(request: Request) -> dict[str, str]:
    return {c.id: c.path or c.name for c in list_compartments(request.app.state.clients)}


def _jobs_by_instance(request: Request) -> dict[str, str]:
    """instance OCID -> id of the migration tool job that launched it (link on the page)."""
    return {j.instance_id: j.id for j in request.app.state.store.list() if j.instance_id}


@router.get("/oci/compartments", response_model=list[OciCompartment], dependencies=[Depends(require_session)])
async def compartments(request: Request):
    try:
        return await asyncio.to_thread(list_compartments, request.app.state.clients)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"cannot list compartments: {describe_error(exc)}")


@router.get("/instances", response_model=list[OciInstance], dependencies=[Depends(require_session)])
async def instances(request: Request, compartment_id: Optional[str] = None):
    """Instances of a compartment (the migration tool VM's when omitted)."""
    st = request.app.state
    comp = compartment_id or st.clients.identity_info.compartment_id

    def work():
        return list_instances(st.clients, comp, _paths(request), _jobs_by_instance(request))

    try:
        return await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"cannot list instances: {describe_error(exc)}")


@router.get("/instances/search", response_model=list[OciInstance], dependencies=[Depends(require_session)])
async def instances_search(request: Request, q: str):
    """Instances whose name contains ``q`` (OCI Resource Search, every compartment the tool may see)."""
    st = request.app.state
    if not q.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "enter (part of) an instance name to search for")
    if st.clients.search is None:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "instance search is not available (no search client)")

    def work():
        return search_instances(st.clients, q, _paths(request), _jobs_by_instance(request))

    try:
        return await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"instance search failed: {describe_error(exc)}")


def _instance(request: Request, instance_id: str):
    """The instance from OCI (its compartment is where the console connection is created)."""
    try:
        inst = request.app.state.clients.compute.get_instance(instance_id).data
    except Exception as exc:  # noqa: BLE001
        code = status.HTTP_404_NOT_FOUND if getattr(exc, "status", None) == 404 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, f"instance {instance_id}: {describe_error(exc)}")
    if inst.lifecycle_state in _GONE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"instance {inst.display_name} is {inst.lifecycle_state}")
    return inst


@router.get("/instances/{instance_id}", response_model=OciInstance, dependencies=[Depends(require_session)])
async def instance_detail(instance_id: str, request: Request):
    inst = await asyncio.to_thread(_instance, request, instance_id)
    paths = await asyncio.to_thread(_paths, request)
    return OciInstance(id=inst.id, name=inst.display_name or inst.id, compartment_id=inst.compartment_id,
                       compartment_path=paths.get(inst.compartment_id, ""), lifecycle_state=inst.lifecycle_state,
                       shape=getattr(inst, "shape", None),
                       availability_domain=getattr(inst, "availability_domain", None),
                       time_created=getattr(inst, "time_created", None),
                       job_id=_jobs_by_instance(request).get(inst.id))


@router.post("/instances/{instance_id}/console", status_code=status.HTTP_202_ACCEPTED)
async def open_console(instance_id: str, request: Request, replace: bool = False,
                       session: UserSession = Depends(require_session)):
    """Create (or reuse) the console connection of an instance; see the job variant for ``replace``."""
    inst = await asyncio.to_thread(_instance, request, instance_id)
    try:
        return await request.app.state.consoles.open_instance(inst.id, inst.compartment_id, session.username,
                                                              replace=replace)
    except ConsoleConflict as exc:
        raise foreign_connection_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, describe_error(exc))


def _none(instance_id: str) -> dict:
    return {"job_id": None, "instance_id": instance_id, "state": "NONE", "connection_id": None, "viewers": 0,
            "error": None, "created_by": None, "idle_s": 0}


@router.get("/instances/{instance_id}/console", dependencies=[Depends(require_session)])
def console_status(instance_id: str, request: Request):
    consoles = request.app.state.consoles
    key_id = consoles.find_for_instance(instance_id) or instance_id
    return consoles.status(key_id) or _none(instance_id)


@router.delete("/instances/{instance_id}/console", dependencies=[Depends(require_session)])
async def close_console(instance_id: str, request: Request):
    consoles = request.app.state.consoles
    key_id = consoles.find_for_instance(instance_id) or instance_id
    closed = await consoles.close(key_id)
    return closed or {**_none(instance_id), "state": "NONE"}


@router.websocket("/instances/{instance_id}/console/vnc")
async def console_vnc(ws: WebSocket, instance_id: str):
    consoles = ws.app.state.consoles
    key_id = consoles.find_for_instance(instance_id) or instance_id
    await bridge_vnc(ws, key_id, f"instance {instance_id}")
