"""GCP inventory for the web UI (needs a GCP login on the session)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.api.inventory_helpers import cached_inventory, guest_mapping
from helper_app.auth import require_gcp_session
from helper_app.gcp.client import GcpAuthError, GcpError
from helper_app.gcp.inventory import GcpVmDetails, inspect_vm, list_vm_summaries
from helper_app.gcp.preflight import preflight, warnings
from helper_app.models import GcpCaptureMode, VmInspection, VmSummary
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/gcp", tags=["gcp"])

def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    return cached_inventory(session, "gcp_vms", refresh, lambda: list_vm_summaries(session.gcp))


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_gcp_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except GcpAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except GcpError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GCP inventory failed: {exc}")


def inspect_details(session: UserSession, vm_id: str) -> GcpVmDetails:
    machine_cache = session.cache.setdefault("gcp_machine_types", {})
    try:
        return inspect_vm(session.gcp, vm_id, machine_cache)
    except GcpAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except GcpError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Compute Engine instance not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def inspect(session: UserSession, vm_id: str, capture_mode: GcpCaptureMode = "stop",
            details: GcpVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details, capture_mode)
    spec = details.spec
    return VmInspection(
        vm=spec,
        can_export=not problems,
        problems=problems,
        warnings=warnings(details, capture_mode),
        needs_power_off=(capture_mode == "stop" and spec.power_state != "poweredOff"),
        tools_running=False,
        os=guest_mapping(spec),
    )


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(
    id: str = Query(description="Compute Engine instance id"),
    capture_mode: GcpCaptureMode = Query(default="stop"),
    session: UserSession = Depends(require_gcp_session),
):
    return await asyncio.to_thread(inspect, session, id, capture_mode)
