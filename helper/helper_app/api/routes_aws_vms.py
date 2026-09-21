"""EC2 inventory for the web UI (needs an AWS login on the session)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.api.inventory_helpers import cached_inventory, guest_mapping
from helper_app.auth import require_aws_session
from helper_app.aws.client import AwsAuthError, AwsError
from helper_app.aws.inventory import AwsVmDetails, inspect_vm, list_vm_summaries
from helper_app.aws.preflight import preflight, warnings
from helper_app.models import AwsCaptureMode, VmInspection, VmSummary
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/aws", tags=["aws"])

def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    return cached_inventory(session, "aws_vms", refresh, lambda: list_vm_summaries(session.aws))


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_aws_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except AwsAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except AwsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AWS inventory failed: {exc}")


def inspect(session: UserSession, vm_id: str, capture_mode: AwsCaptureMode = "stop",
            details: AwsVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details, capture_mode)
    spec = details.spec
    return VmInspection(
        vm=spec, can_export=not problems, problems=problems, warnings=warnings(details, capture_mode),
        needs_power_off=(capture_mode == "stop" and spec.power_state != "poweredOff"),
        tools_running=False,
        os=guest_mapping(spec),
    )


def inspect_details(session: UserSession, vm_id: str) -> AwsVmDetails:
    type_cache = session.cache.setdefault("aws_instance_types", {})
    try:
        return inspect_vm(session.aws, vm_id, type_cache)
    except AwsAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except AwsError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"EC2 instance not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(id: str = Query(description="EC2 instance ARN"),
                           capture_mode: AwsCaptureMode = Query(default="stop"),
                           session: UserSession = Depends(require_aws_session)):
    return await asyncio.to_thread(inspect, session, id, capture_mode)
