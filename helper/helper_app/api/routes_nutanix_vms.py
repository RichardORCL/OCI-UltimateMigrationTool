"""Nutanix AHV inventory for the web UI (needs a Prism login on the session)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.api.inventory_helpers import cached_inventory, guest_mapping
from helper_app.auth import require_nutanix_session
from helper_app.models import VmInspection, VmSummary
from helper_app.nutanix.client import NutanixAuthError, NutanixError
from helper_app.nutanix.inventory import (
    NutanixVmDetails,
    inspect_vm,
    list_vm_summaries,
    preflight,
    warnings,
)
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/nutanix", tags=["nutanix"])


def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    return cached_inventory(session, "nutanix_vms", refresh, lambda: list_vm_summaries(session.nutanix))


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_nutanix_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except NutanixAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except NutanixError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Nutanix inventory failed: {exc}")


def inspect_details(session: UserSession, vm_id: str) -> NutanixVmDetails:
    try:
        return inspect_vm(session.nutanix.client, vm_id)
    except NutanixAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except NutanixError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"AHV virtual machine not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def inspect(session: UserSession, vm_id: str, details: NutanixVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details)
    spec = details.spec
    return VmInspection(
        vm=spec,
        can_export=not problems,
        problems=problems,
        warnings=warnings(details),
        needs_power_off=spec.power_state == "poweredOn",
        tools_running=details.vm.ngt_installed and details.vm.power_state == "ON",
        os=guest_mapping(spec),
    )


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(id: str = Query(description="AHV virtual machine uuid"),
                           session: UserSession = Depends(require_nutanix_session)):
    return await asyncio.to_thread(inspect, session, id)
