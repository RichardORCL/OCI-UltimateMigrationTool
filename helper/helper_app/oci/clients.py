"""OCI SDK client factory, helper identity discovery and a small polling helper."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx

from helper_app.config import Settings

log = logging.getLogger(__name__)

METADATA_URL = "http://169.254.169.254/opc/v2/instance/"


class OciError(RuntimeError):
    """Raised for provisioning failures with a human readable message."""


def describe_error(exc: BaseException) -> str:
    """Human readable one-liner for an exception; OCI ``ServiceError`` gets operation, status, code,
    message and request id instead of the raw dict dump."""
    try:
        import oci

        service_error = oci.exceptions.ServiceError
    except Exception:  # pragma: no cover
        service_error = ()
    if isinstance(exc, service_error):
        parts = [f"OCI {exc.operation_name or 'request'} failed with HTTP {exc.status} {exc.code}: {exc.message}"]
        if exc.request_endpoint:
            parts.append(f"endpoint {exc.request_endpoint}")
        if exc.request_id:
            parts.append(f"opc-request-id {exc.request_id}")
        return " | ".join(parts)
    vsphere = _describe_vsphere_fault(exc)
    if vsphere:
        return vsphere
    text = str(exc) or exc.__class__.__name__
    return text if text != str(exc.__class__) else exc.__class__.__name__


def _describe_vsphere_fault(exc: BaseException) -> str:
    """pyVmomi ``MethodFault``s print as a multi-line object dump; reduce them to the fault type, ``msg``
    and the localizable detail messages (e.g. 'The operation is not supported on encrypted VM')."""
    if not (hasattr(exc, "faultMessage") and hasattr(exc, "msg") and hasattr(exc, "dynamicType")):
        return ""
    name = type(exc).__name__.rsplit(".", 1)[-1]  # vmodl.fault.NotSupported -> NotSupported
    msg = str(getattr(exc, "msg", "") or "").strip()
    details = []
    for m in getattr(exc, "faultMessage", None) or []:
        text = str(getattr(m, "message", "") or "").strip()
        if text and text != msg and text not in details:
            details.append(text)
    head = f"vSphere {name}: {msg}" if msg else f"vSphere {name}"
    return f"{head} ({'; '.join(details)})" if details else head


@dataclass
class HelperIdentity:
    instance_id: str
    compartment_id: str
    availability_domain: str
    region: str
    tenancy_id: str


def discover_identity(settings: Settings) -> HelperIdentity:
    """Use configured values, falling back to the instance metadata service."""
    fields = (settings.instance_id, settings.compartment_id, settings.availability_domain, settings.region,
              settings.tenancy_id)
    if all(fields):
        return HelperIdentity(*fields)
    try:
        resp = httpx.get(METADATA_URL, headers={"Authorization": "Bearer Oracle"}, timeout=5.0)
        resp.raise_for_status()
        md = resp.json()
    except Exception as exc:  # pragma: no cover - only on a real instance
        raise OciError(
            "The migration tool VM identity is not configured and the instance metadata service is unreachable: "
            f"{exc}. Set HELPER_INSTANCE_ID/COMPARTMENT_ID/AVAILABILITY_DOMAIN/REGION/TENANCY_ID."
        ) from exc
    return HelperIdentity(
        instance_id=settings.instance_id or md["id"],
        compartment_id=settings.compartment_id or md["compartmentId"],
        availability_domain=settings.availability_domain or md["availabilityDomain"],
        region=settings.region or md.get("canonicalRegionName") or md["region"],
        tenancy_id=settings.tenancy_id or md.get("tenantId", ""),
    )


@dataclass
class OciClients:
    compute: Any
    blockstorage: Any
    network: Any
    identity: Any
    object_storage: Any
    identity_info: HelperIdentity
    poll_interval_s: float = 5.0
    work_requests: Any = None  # oci.work_requests.WorkRequestClient; used to explain failed image imports
    search: Any = None  # oci.resource_search.ResourceSearchClient; instance search by name (Remote Console page)

    # ------------------------------------------------------------------ waiting
    def wait_for(
        self,
        fetch: Callable[[], Any],
        attr: str,
        targets: Iterable[str],
        timeout_s: float,
        failure_states: Iterable[str] = ("FAILED", "TERMINATED", "TERMINATING", "DELETED", "FAULTY"),
        what: str = "resource",
    ) -> Any:
        """Poll ``fetch()`` (returns an SDK response) until ``data.<attr>`` is in ``targets``."""
        targets = set(targets)
        failure_states = set(failure_states) - targets
        deadline = time.monotonic() + timeout_s
        last = None
        while True:
            data = fetch().data
            last = getattr(data, attr, None)
            if last in targets:
                return data
            if last in failure_states:
                raise OciError(f"{what} entered state {last} while waiting for {sorted(targets)}")
            if time.monotonic() >= deadline:
                raise OciError(
                    f"timed out after {timeout_s:.0f}s waiting for {what} to reach {sorted(targets)} (last={last})"
                )
            time.sleep(self.poll_interval_s)


    # ------------------------------------------------------------ diagnostics
    def work_request_errors(self, compartment_id: str, resource_id: str) -> str:
        """Errors OCI recorded on the work requests of a resource, e.g. why an instance went TERMINATING
        right after launch.  Never raises: this is only used to enrich an error that already happened."""
        if self.work_requests is None:
            return ""
        try:
            requests = self.work_requests.list_work_requests(compartment_id, resource_id=resource_id).data or []
            parts = []
            for wr in requests:
                for err in self.work_requests.list_work_request_errors(wr.id).data or []:
                    parts.append(f"{wr.operation_type or 'work request'}: {err.message}")
            return "; ".join(parts)
        except Exception as exc:  # noqa: BLE001
            return f"(could not read the work requests of {resource_id}: {describe_error(exc)})"


def build_clients(settings: Settings) -> OciClients:
    import oci

    identity = discover_identity(settings)
    if settings.oci_auth == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {"region": identity.region}
        if not identity.tenancy_id:
            identity.tenancy_id = signer.tenancy_id
        kwargs = {"config": config, "signer": signer}
    elif settings.oci_auth == "config_file":
        config = oci.config.from_file(settings.oci_config_file, settings.oci_profile)
        if identity.region:
            config["region"] = identity.region
        else:
            identity.region = config["region"]
        if not identity.tenancy_id:
            identity.tenancy_id = config["tenancy"]
        kwargs = {"config": config}
    else:
        raise OciError(f"unsupported HELPER_OCI_AUTH={settings.oci_auth}")
    if settings.oci_log_requests:
        config["log_requests"] = True  # SDK dumps every request/response (headers + bodies) at DEBUG

    retry = oci.retry.DEFAULT_RETRY_STRATEGY
    return OciClients(
        compute=oci.core.ComputeClient(retry_strategy=retry, **kwargs),
        blockstorage=oci.core.BlockstorageClient(retry_strategy=retry, **kwargs),
        network=oci.core.VirtualNetworkClient(retry_strategy=retry, **kwargs),
        identity=oci.identity.IdentityClient(retry_strategy=retry, **kwargs),
        object_storage=oci.object_storage.ObjectStorageClient(retry_strategy=retry, **kwargs),
        identity_info=identity,
        work_requests=oci.work_requests.WorkRequestClient(retry_strategy=retry, **kwargs),
        search=oci.resource_search.ResourceSearchClient(retry_strategy=retry, **kwargs),
    )
