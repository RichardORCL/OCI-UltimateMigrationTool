"""Hyper-V inventory for the web UI (needs a Hyper-V host login on the session)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.api.inventory_helpers import cached_inventory, guest_mapping
from helper_app.auth import require_hyperv_session
from helper_app.hyperv.client import HypervAuthError, HypervError
from helper_app.hyperv.inventory import (
    HypervVmDetails,
    guest_services_running,
    inspect_vm,
    list_vm_summaries,
    preflight,
    warnings,
)
from helper_app.models import VmInspection, VmSummary
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/hyperv", tags=["hyperv"])


def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    return cached_inventory(session, "hyperv_vms", refresh, lambda: list_vm_summaries(session.hyperv))


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_hyperv_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except HypervAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except HypervError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Hyper-V inventory failed: {exc}")


def inspect_details(session: UserSession, vm_id: str) -> HypervVmDetails:
    try:
        return inspect_vm(session.hyperv.client, vm_id, session.hyperv.hostname)
    except HypervAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except HypervError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Hyper-V virtual machine not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def inspect(session: UserSession, vm_id: str, details: HypervVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details)
    spec = details.spec
    return VmInspection(
        vm=spec,
        can_export=not problems,
        problems=problems,
        warnings=warnings(details),
        needs_power_off=spec.power_state == "poweredOn",
        tools_running=guest_services_running(details.vm),
        os=guest_mapping(spec),
    )


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(id: str = Query(description="Hyper-V virtual machine id"),
                           session: UserSession = Depends(require_hyperv_session)):
    return await asyncio.to_thread(inspect, session, id)
