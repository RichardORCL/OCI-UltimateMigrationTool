"""OCI instance console connections: create with a temporary key, find leftovers, delete.

Synchronous (OCI SDK); the manager calls these through ``asyncio.to_thread``.  The SSH endpoints are
parsed from the ``vncConnectionString`` OCI returns, e.g.::

    ssh -o ProxyCommand='ssh -W %h:%p -p 443 <connection OCID>@instance-console.<region>.oci.oraclecloud.com'
        -N -L localhost:5900:<instance OCID>:5900 <instance OCID>

The forward target (``-L ...:<host>:<port>``) and the SSH host of the second hop (the trailing argument)
are parsed separately: OCI does not always name the instance in both places (bare metal instances get
``-L 5900:localhost:5900 <user>@<instance OCID>``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from helper_app.oci.clients import OciClients, OciError

from helper_app.branding import TAG_CONSOLE, TAG_CONSOLE_VALUE, TAG_JOB

TAG_KEY = TAG_CONSOLE
TAG_VALUE = TAG_CONSOLE_VALUE
TAG_JOB_KEY = TAG_JOB

_PROXY_RE = re.compile(r"(?P<user>ocid1\.instanceconsoleconnection\.[\w.-]+)@(?P<host>[\w.-]+)")
_PROXY_PORT_RE = re.compile(r"-p\s+(?P<port>\d+)")
_FORWARD_RE = re.compile(r"-L\s+(?:[\w.-]+:)?\d+:(?P<target>[\w.-]+):(?P<port>\d+)")
_PROXY_COMMAND_RE = re.compile(r"ProxyCommand=(?P<q>['\"]).*?(?P=q)", re.DOTALL)
# the second hop: the trailing ``[user@]host`` argument of the outer ssh command
_SSH_HOST_RE = re.compile(r"(?:(?P<user>[\w.-]+)@)?(?P<host>ocid1\.instance\.[\w.-]+)\s*$")


class ConsoleConflict(RuntimeError):
    """Another console connection exists for the instance and was not created by the helper."""

    def __init__(self, connection_id: str):
        super().__init__(f"instance console connection {connection_id} exists already")
        self.connection_id = connection_id


@dataclass(frozen=True)
class ConsoleEndpoint:
    """SSH hops of the VNC tunnel: the console service (proxy) and the instance behind it."""

    proxy_host: str
    proxy_port: int
    proxy_user: str  # the console connection OCID
    target_host: str  # SSH host of the second hop: the instance OCID, resolved by the console service
    target_port: int  # VNC port (5900)
    vnc_host: str = ""  # forward target as seen from the second hop (the instance OCID or ``localhost``)
    target_user: str = ""  # user name OCI put in front of the second hop, if any


def parse_vnc_connection_string(text: str) -> ConsoleEndpoint:
    proxy = _PROXY_RE.search(text or "")
    forward = _FORWARD_RE.search(text or "")
    if not proxy or not forward:
        raise OciError(f"cannot parse the VNC connection string OCI returned: {text!r}")
    port = _PROXY_PORT_RE.search(text)
    # the hop-2 host is the trailing argument of the outer command; the -L target names the instance for
    # VM instances and "localhost" for bare metal ones
    outer = _PROXY_COMMAND_RE.sub("ProxyCommand=''", text)
    ssh_host = _SSH_HOST_RE.search(outer)
    vnc_host = forward.group("target")
    target_host = ssh_host.group("host") if ssh_host else vnc_host
    return ConsoleEndpoint(
        proxy_host=proxy.group("host"),
        proxy_port=int(port.group("port")) if port else 443,
        proxy_user=proxy.group("user"),
        target_host=target_host,
        target_port=int(forward.group("port")),
        vnc_host=vnc_host,
        target_user=(ssh_host.group("user") or "") if ssh_host else "",
    )


@dataclass
class ConsoleConnectionInfo:
    id: str
    instance_id: str
    endpoint: ConsoleEndpoint
    service_host_key_fingerprint: str = ""
    vnc_connection_string: str = ""


def is_helper_owned(conn: Any) -> bool:
    return (getattr(conn, "freeform_tags", None) or {}).get(TAG_KEY) == TAG_VALUE


def find_existing(clients: OciClients, compartment_id: str, instance_id: str) -> list[Any]:
    """Console connections of the instance that are not gone yet (OCI allows one per instance)."""
    import oci.pagination

    conns = oci.pagination.list_call_get_all_results(
        clients.compute.list_instance_console_connections, compartment_id, instance_id=instance_id).data or []
    return [c for c in conns if c.lifecycle_state not in ("DELETED", "DELETING", "FAILED")]


def create(clients: OciClients, instance_id: str, compartment_id: str, public_key: str, job_id: str,
           timeout_s: float) -> ConsoleConnectionInfo:
    import oci.core.models as M

    tags = {TAG_KEY: TAG_VALUE}
    if job_id:  # consoles opened from the Remote Console page belong to no job
        tags[TAG_JOB_KEY] = job_id
    details = M.CreateInstanceConsoleConnectionDetails(instance_id=instance_id, public_key=public_key,
                                                        freeform_tags=tags)
    conn = clients.compute.create_instance_console_connection(details).data
    conn = clients.wait_for(lambda: clients.compute.get_instance_console_connection(conn.id), "lifecycle_state",
                            ["ACTIVE"], timeout_s, what="instance console connection")
    return ConsoleConnectionInfo(
        id=conn.id, instance_id=conn.instance_id,
        endpoint=parse_vnc_connection_string(conn.vnc_connection_string),
        service_host_key_fingerprint=conn.service_host_key_fingerprint or "",
        vnc_connection_string=conn.vnc_connection_string or "",
    )


def delete(clients: OciClients, connection_id: str, timeout_s: float, wait: bool = True) -> None:
    """Delete a console connection; a connection that is already gone is not an error."""
    import oci.exceptions

    try:
        clients.compute.delete_instance_console_connection(connection_id)
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            return
        raise
    if not wait:
        return

    def fetch():
        try:
            return clients.compute.get_instance_console_connection(connection_id)
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                return _Gone()
            raise

    clients.wait_for(fetch, "lifecycle_state", ["DELETED"], timeout_s, failure_states=("FAILED",),
                     what="instance console connection deletion")


class _Gone:
    """Stands in for a GET response of a connection OCI no longer knows (404 after deletion)."""

    class _Data:
        lifecycle_state = "DELETED"

    data = _Data()


def ensure_no_other(clients: OciClients, compartment_id: str, instance_id: str, replace: bool,
                    timeout_s: float) -> Optional[str]:
    """Make room for a new console connection.  Helper-created leftovers (a previous helper run whose key
    is gone) are deleted silently; a connection created elsewhere is only deleted with ``replace``.
    Returns the id of the connection that was deleted, if any."""
    deleted = None
    for conn in find_existing(clients, compartment_id, instance_id):
        if not is_helper_owned(conn) and not replace:
            raise ConsoleConflict(conn.id)
        delete(clients, conn.id, timeout_s)
        deleted = conn.id
    return deleted
