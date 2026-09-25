"""OLVM inventory for the web UI (needs an OLVM login on the session)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.api.inventory_helpers import cached_inventory, guest_mapping
from helper_app.auth import require_olvm_session
from helper_app.models import VmInspection, VmSummary
from helper_app.olvm.client import OlvmAuthError, OlvmError
from helper_app.olvm.inventory import (
    OlvmVmDetails,
    guest_agent_running,
    inspect_vm,
    list_vm_summaries,
    preflight,
    warnings,
)
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/olvm", tags=["olvm"])


def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    return cached_inventory(session, "olvm_vms", refresh, lambda: list_vm_summaries(session.olvm))


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_olvm_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except OlvmAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except OlvmError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"OLVM inventory failed: {exc}")


def inspect_details(session: UserSession, vm_id: str) -> OlvmVmDetails:
    try:
        return inspect_vm(session.olvm.client, vm_id)
    except OlvmAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except OlvmError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"OLVM virtual machine not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def inspect(session: UserSession, vm_id: str, details: OlvmVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details)
    spec = details.spec
    return VmInspection(
        vm=spec,
        can_export=not problems,
        problems=problems,
        warnings=warnings(details),
        needs_power_off=spec.power_state == "poweredOn",
        tools_running=guest_agent_running(details.vm),
        os=guest_mapping(spec),
    )


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(id: str = Query(description="OLVM virtual machine id"),
                           session: UserSession = Depends(require_olvm_session)):
    return await asyncio.to_thread(inspect, session, id)
