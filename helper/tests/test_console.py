"""Remote console: console connection lifecycle, WebSocket <-> tunnel bridge, conflicts, idle cleanup."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helper_app.console.connection import ConsoleEndpoint, parse_vnc_connection_string
from helper_app.console.tunnel import TunnelError, fingerprint_matches
from helper_app.jobs.store import utcnow
from helper_app.models import Job, JobPhase, OciTarget
from helper_app.vsphere.inventory import vm_spec_from_vm

from .fake_oci import oid
from .fake_vsphere import make_vm
from .test_api import Env, login, target, wait_phase, wait_until

DOC_STRING = ("ssh -o ProxyCommand='ssh -W %h:%p -p 443 ocid1.instanceconsoleconnection.oc1.phx.aaaaaaaa@"
              "instance-console.us-phoenix-1.oci.oraclecloud.com' -N -L localhost:5900:ocid1.instance.oc1.phx.bbbbbbbb:5900 "
              "ocid1.instance.oc1.phx.bbbbbbbb")


BM_STRING = ("ssh -o ProxyCommand='ssh -W %h:%p -p 443 ocid1.instanceconsoleconnection.oc1.eu-frankfurt-1.cccccccc@"
             "instance-console.eu-frankfurt-1.oci.oraclecloud.com' -N -L 5900:localhost:5900 "
             "un7rfqk4ibrpaifa@ocid1.instance.oc1.eu-frankfurt-1.dddddddd")


def test_parse_vnc_connection_string():
    ep = parse_vnc_connection_string(DOC_STRING)
    assert ep == ConsoleEndpoint(proxy_host="instance-console.us-phoenix-1.oci.oraclecloud.com", proxy_port=443,
                                 proxy_user="ocid1.instanceconsoleconnection.oc1.phx.aaaaaaaa",
                                 target_host="ocid1.instance.oc1.phx.bbbbbbbb", target_port=5900,
                                 vnc_host="ocid1.instance.oc1.phx.bbbbbbbb", target_user="")
    with pytest.raises(Exception, match="cannot parse"):
        parse_vnc_connection_string("ssh nowhere")


def test_parse_vnc_connection_string_bare_metal():
    """Bare metal: the -L target is localhost and the hop-2 argument carries a user name; the SSH host of
    the second hop is still the instance OCID (taken from the trailing argument, not from -L)."""
    ep = parse_vnc_connection_string(BM_STRING)
    assert ep.proxy_user == "ocid1.instanceconsoleconnection.oc1.eu-frankfurt-1.cccccccc"
    assert ep.target_host == "ocid1.instance.oc1.eu-frankfurt-1.dddddddd"
    assert (ep.vnc_host, ep.target_port, ep.target_user) == ("localhost", 5900, "un7rfqk4ibrpaifa")


def test_fingerprint_matches():
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    sha = key.get_fingerprint("sha256")
    assert fingerprint_matches(sha, key) is True
    assert fingerprint_matches(sha.split(":", 1)[1], key) is True  # without the SHA256: prefix
    assert fingerprint_matches(key.get_fingerprint("md5").split(":", 1)[1].upper(), key) is True
    assert fingerprint_matches("SHA256:" + "A" * 43, key) is False
    assert fingerprint_matches("", key) is None  # nothing to compare against
    assert fingerprint_matches("something else", key) is None


class FakeTunnel:
    """Echo stream standing in for the SSH channel to the instance's VNC port."""

    opened: list["FakeTunnel"] = []

    def __init__(self, endpoint, key, fingerprint, timeout_s):
        self.endpoint, self.key, self.fingerprint, self.timeout_s = endpoint, key, fingerprint, timeout_s
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        FakeTunnel.opened.append(self)

    async def read(self, n):
        data = await self.queue.get()
        return data

    async def write(self, data):
        await self.queue.put(b"echo:" + data)

    async def close(self):
        self.closed = True
        await self.queue.put(b"")


async def fake_tunnel(endpoint, key, fingerprint, timeout_s):
    return FakeTunnel(endpoint, key, fingerprint, timeout_s)


async def failing_tunnel(endpoint, key, fingerprint, timeout_s):
    raise TunnelError("host key of instance-console rejected")


@pytest.fixture
def cenv(tmp_path, request):
    FakeTunnel.opened.clear()
    factory = getattr(request, "param", fake_tunnel)
    e = Env(tmp_path, fail_once=frozenset(), tunnel_factory=factory)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def completed_job(env) -> dict:
    login(env.client)
    r = env.client.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert r.status_code == 202, r.text
    job = wait_phase(env.client, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    return job


def wait_console(client, job_id, *states):
    wait_until(lambda: client.get(f"/api/jobs/{job_id}/console").json()["state"] in states, what=f"console {states}")
    return client.get(f"/api/jobs/{job_id}/console").json()


# --------------------------------------------------------------------------- flow
def test_console_open_bridge_close(cenv):
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid, iid = job["id"], job["instance_id"]
    assert c.get(f"/api/jobs/{jid}/console").json()["state"] == "NONE"

    r = c.post(f"/api/jobs/{jid}/console")
    assert r.status_code == 202, r.text
    assert r.json()["state"] in ("CREATING", "ACTIVE")
    st = wait_console(c, jid, "ACTIVE", "FAILED")
    assert st["state"] == "ACTIVE", st
    assert st["created_by"] == "admin@vsphere.local" and st["viewers"] == 0
    conns = [x for x in fake.compute.console_connections.values() if x.instance_id == iid]
    assert len(conns) == 1 and conns[0].id == st["connection_id"] and conns[0].lifecycle_state == "ACTIVE"
    assert conns[0].freeform_tags == {"oci-umt": "console", "oci-umt-job": jid}
    # a second open is idempotent: no second connection, no 409 from OCI
    assert c.post(f"/api/jobs/{jid}/console").json()["connection_id"] == st["connection_id"]

    with c.websocket_connect(f"/api/jobs/{jid}/console/vnc", subprotocols=["binary"]) as ws:
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"echo:RFB 003.008\n"
        assert c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 1
        t = FakeTunnel.opened[-1]
        # the tunnel got the parsed endpoint, the in-memory key and the service fingerprint from OCI
        assert t.endpoint.proxy_user == st["connection_id"] and t.endpoint.target_host == iid
        assert t.endpoint.proxy_host == "instance-console.eu-frankfurt-1.oci.oraclecloud.com"
        assert t.key is not None and t.fingerprint == fake.console_host_fingerprint
    wait_until(lambda: c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 0, what="viewer released")
    assert FakeTunnel.opened[-1].closed

    r = c.delete(f"/api/jobs/{jid}/console")
    assert r.status_code == 200 and r.json()["state"] == "CLOSED"
    assert fake.compute.console_deleted == [st["connection_id"]]
    assert c.get(f"/api/jobs/{jid}/console").json()["state"] == "NONE"
    # the private key was discarded with the session
    assert cenv.app.state.consoles.sessions == {}


def test_console_refused_before_completion(cenv):
    c = cenv.client
    login(c)
    vm = vm_spec_from_vm(make_vm(moid="vm-x", name="x"))
    now = utcnow()
    for phase, instance_id in ((JobPhase.FAILED, "ocid1.instance.oc1..x"), (JobPhase.COMPLETED, None),
                               (JobPhase.EXPORTING, "ocid1.instance.oc1..x")):
        job = Job(id=f"job-{phase.value}", vm=vm, target=OciTarget(**target()), phase=phase, instance_id=instance_id,
                  created_at=now, updated_at=now)
        cenv.store.put(job)
        r = c.post(f"/api/jobs/{job.id}/console")
        assert r.status_code == 409, (phase, r.text)
    assert c.post("/api/jobs/nope/console").status_code == 404
    assert c.get("/api/jobs/nope/console").status_code == 404


def test_console_available_while_iso_installation_runs(cenv):
    """ISO jobs: the console is the way to run the installer, so it opens in INSTALLING (from the anonymous
    session), stays after Installation finished, and is refused for an ISO job that never launched."""
    c, fake = cenv.client, cenv.fake
    fake.object_storage.add_object("isos", "ubuntu.iso", etag="e1")
    assert c.post("/api/auth/anonymous").status_code == 200
    body = {"iso": {"namespace": "testnamespace", "bucket": "isos", "object_name": "ubuntu.iso", "etag": "e1",
                    "operating_system": "Ubuntu", "operating_system_version": "24.04"},
            "target": target(display_name="ubuntu-iso", shape="VM.Standard.E5.Flex", ocpus=1, memory_gb=8)}
    r = c.post("/api/jobs/iso", json=body)
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "INSTALLING", "FAILED")
    assert job["phase"] == "INSTALLING", job
    jid, iid = job["id"], job["instance_id"]

    r = c.post(f"/api/jobs/{jid}/console")
    assert r.status_code == 202, r.text
    st = wait_console(c, jid, "ACTIVE", "FAILED")
    assert st["state"] == "ACTIVE" and st["created_by"] == "anonymous"
    conns = [x for x in fake.compute.console_connections.values() if x.instance_id == iid]
    assert len(conns) == 1 and conns[0].freeform_tags["oci-umt-job"] == jid
    with c.websocket_connect(f"/api/jobs/{jid}/console/vnc", subprotocols=["binary"]) as ws:
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"echo:RFB 003.008\n"
        assert FakeTunnel.opened[-1].endpoint.target_host == iid
    # finishing the installation keeps the console usable (the instance now boots from its boot volume)
    assert c.post(f"/api/jobs/{jid}/finish").json()["phase"] == "COMPLETED"
    assert c.get(f"/api/jobs/{jid}/console").json()["state"] == "ACTIVE"
    assert c.post(f"/api/jobs/{jid}/console").json()["connection_id"] == st["connection_id"]

    # an ISO job without an instance (failed import) has nothing to connect to
    failed = Job.model_validate({**job, "id": "iso-failed", "phase": JobPhase.FAILED.value, "instance_id": None})
    cenv.store.put(failed)
    assert c.post("/api/jobs/iso-failed/console").status_code == 409


def test_console_requires_login(cenv):
    c = cenv.client
    job = completed_job(cenv)
    c.post("/api/auth/logout")
    assert c.post(f"/api/jobs/{job['id']}/console").status_code == 401
    assert c.get(f"/api/jobs/{job['id']}/console").status_code == 401
    with pytest.raises(WebSocketDisconnect):  # handshake denied
        with c.websocket_connect(f"/api/jobs/{job['id']}/console/vnc"):
            pass


def test_console_ws_without_connection_and_cross_origin(cenv):
    c = cenv.client
    job = completed_job(cenv)
    jid = job["id"]
    # no console connection opened yet: accepted, then closed with the application code
    with c.websocket_connect(f"/api/jobs/{jid}/console/vnc") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 4409
    # another site's page cannot ride on the cookie
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/api/jobs/{jid}/console/vnc", headers={"Origin": "https://evil.example"}):
            pass


@pytest.mark.parametrize("cenv", [failing_tunnel], indirect=True)
def test_console_ws_tunnel_failure_is_reported(cenv):
    c = cenv.client
    job = completed_job(cenv)
    jid = job["id"]
    c.post(f"/api/jobs/{jid}/console")
    wait_console(c, jid, "ACTIVE")
    with c.websocket_connect(f"/api/jobs/{jid}/console/vnc") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 4502
        assert "host key" in msg.get("reason", "")
    assert c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 0


# --------------------------------------------------------------------------- existing connections
def preexisting_connection(fake, instance_id, tags):
    cid = oid("instanceconsoleconnection")
    fake.compute.console_connections[cid] = NS(
        id=cid, instance_id=instance_id, compartment_id=fake.compute.instances[instance_id].compartment_id,
        lifecycle_state="ACTIVE", freeform_tags=tags, fingerprint="", service_host_key_fingerprint="",
        connection_string="", vnc_connection_string="")
    return cid


def test_console_replaces_helper_leftover_silently(cenv):
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid, iid = job["id"], job["instance_id"]
    # a previous helper run created one; its key is gone with the process
    old = preexisting_connection(fake, iid, {"oci-umt": "console", "oci-umt-job": "older-run"})
    r = c.post(f"/api/jobs/{jid}/console")
    assert r.status_code == 202, r.text
    st = wait_console(c, jid, "ACTIVE", "FAILED")
    assert st["state"] == "ACTIVE" and st["connection_id"] != old
    assert fake.compute.console_deleted == [old]


def test_console_foreign_connection_needs_replace(cenv):
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid, iid = job["id"], job["instance_id"]
    foreign = preexisting_connection(fake, iid, {})
    r = c.post(f"/api/jobs/{jid}/console")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "foreign_connection" and detail["connection_id"] == foreign
    assert fake.compute.console_deleted == [] and c.get(f"/api/jobs/{jid}/console").json()["state"] == "NONE"

    r = c.post(f"/api/jobs/{jid}/console?replace=true")
    assert r.status_code == 202, r.text
    st = wait_console(c, jid, "ACTIVE", "FAILED")
    assert st["state"] == "ACTIVE" and fake.compute.console_deleted == [foreign]


def test_console_create_failure_is_reported(cenv):
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid = job["id"]
    # the instance disappears (terminated in the OCI console) before the connection is created
    del fake.compute.instances[job["instance_id"]]
    r = c.post(f"/api/jobs/{jid}/console")
    assert r.status_code == 202
    st = wait_console(c, jid, "ACTIVE", "FAILED")
    assert st["state"] == "FAILED" and "NotAuthorizedOrNotFound" in st["error"]
    # a new attempt is allowed after a failure
    assert c.post(f"/api/jobs/{jid}/console").status_code == 202


# --------------------------------------------------------------------------- idle cleanup / shutdown
def test_console_idle_reaper(cenv):
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid = job["id"]
    c.post(f"/api/jobs/{jid}/console")
    st = wait_console(c, jid, "ACTIVE")
    manager = cenv.app.state.consoles
    idle = cenv.settings.console_idle_timeout_s
    # not idle long enough: kept
    assert c.portal.call(manager.reap_idle, time.monotonic() + idle - 5) == []
    # a viewer keeps it alive however long the session is
    with c.websocket_connect(f"/api/jobs/{jid}/console/vnc"):
        wait_until(lambda: c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 1, what="viewer")
        assert c.portal.call(manager.reap_idle, time.monotonic() + 10 * idle) == []
    wait_until(lambda: c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 0, what="viewer released")
    assert c.portal.call(manager.reap_idle, time.monotonic() + idle + 1) == [jid]
    assert fake.compute.console_deleted == [st["connection_id"]]
    assert c.get(f"/api/jobs/{jid}/console").json()["state"] == "NONE"


# --------------------------------------------------------------------------- OCI Remote Console page
def anonymous(client):
    r = client.post("/api/auth/anonymous")
    assert r.status_code == 200, r.text


def test_instances_listing_and_search(cenv):
    """Instances by compartment (terminated ones dropped, job of migrated instances linked) and by name
    through Resource Search, all without a vCenter login."""
    c, fake = cenv.client, cenv.fake
    prod, migr = "ocid1.compartment.oc1..prod", "ocid1.compartment.oc1..migr"
    web = fake.compute.add_instance("web-frontend-01", prod)
    db = fake.compute.add_instance("db-primary", prod, shape="BM.Standard.E5.192", state="STOPPED")
    fake.compute.add_instance("web-old", prod, state="TERMINATED")
    other = fake.compute.add_instance("web-frontend-02", migr)
    job = completed_job(cenv)  # a migrated VM in the migration compartment
    c.post("/api/auth/logout")
    anonymous(c)

    r = c.get("/api/oci/compartments")
    assert r.status_code == 200 and {x["id"] for x in r.json()} >= {prod, migr}

    r = c.get("/api/instances", params={"compartment_id": prod})
    assert r.status_code == 200, r.text
    rows = {x["id"]: x for x in r.json()}
    assert list(rows) == [db, web]  # sorted by name, TERMINATED left out
    assert rows[db]["shape"] == "BM.Standard.E5.192" and rows[db]["lifecycle_state"] == "STOPPED"
    assert rows[web]["compartment_path"] == "prod" and rows[web]["job_id"] is None

    r = c.get("/api/instances", params={"compartment_id": migr})
    rows = {x["id"]: x for x in r.json()}
    assert rows[job["instance_id"]]["job_id"] == job["id"] and other in rows

    # search: case-insensitive substring across compartments, quotes escaped in the query
    r = c.get("/api/instances/search", params={"q": "WEB-front"})
    assert r.status_code == 200, r.text
    assert [(x["name"], x["compartment_path"]) for x in r.json()] == [("web-frontend-01", "prod"),
                                                                       ("web-frontend-02", "prod/migrations")]
    assert fake.search.queries[-1] == "query instance resources where displayName =~ 'WEB-front'"
    r = c.get("/api/instances/search", params={"q": "o'brien"})
    assert r.status_code == 200 and r.json() == []
    assert fake.search.queries[-1] == "query instance resources where displayName =~ 'o\\'brien'"
    assert c.get("/api/instances/search", params={"q": "  "}).status_code == 400

    r = c.get(f"/api/instances/{web}")
    assert r.status_code == 200 and r.json()["name"] == "web-frontend-01" and r.json()["compartment_path"] == "prod"
    assert c.get("/api/instances/ocid1.instance.oc1..nope").status_code == 404


def test_instance_console_open_bridge_close(cenv):
    """A console for an instance without a job: created in the instance's compartment, keyed by the OCID,
    served through the same bridge, no job tag on the connection."""
    c, fake = cenv.client, cenv.fake
    iid = fake.compute.add_instance("debug-me", "ocid1.compartment.oc1..prod")
    anonymous(c)
    assert c.get(f"/api/instances/{iid}/console").json()["state"] == "NONE"
    r = c.post(f"/api/instances/{iid}/console")
    assert r.status_code == 202, r.text
    wait_until(lambda: c.get(f"/api/instances/{iid}/console").json()["state"] in ("ACTIVE", "FAILED"), what="console")
    st = c.get(f"/api/instances/{iid}/console").json()
    assert st["state"] == "ACTIVE", st
    assert st["instance_id"] == iid and st["job_id"] is None and st["created_by"] == "anonymous"
    conn = fake.compute.console_connections[st["connection_id"]]
    assert conn.instance_id == iid and conn.freeform_tags == {"oci-umt": "console"}
    assert cenv.app.state.consoles.sessions[iid].compartment_id == "ocid1.compartment.oc1..prod"
    assert c.post(f"/api/instances/{iid}/console").json()["connection_id"] == st["connection_id"]  # idempotent

    with c.websocket_connect(f"/api/instances/{iid}/console/vnc", subprotocols=["binary"]) as ws:
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"echo:RFB 003.008\n"
        assert FakeTunnel.opened[-1].endpoint.target_host == iid
    wait_until(lambda: c.get(f"/api/instances/{iid}/console").json()["viewers"] == 0, what="viewer released")
    r = c.delete(f"/api/instances/{iid}/console")
    assert r.status_code == 200 and r.json()["state"] == "CLOSED"
    assert fake.compute.console_deleted == [st["connection_id"]]
    assert cenv.app.state.consoles.sessions == {}

    # a terminated or unknown instance gets no console
    gone = fake.compute.add_instance("gone", "ocid1.compartment.oc1..prod", state="TERMINATED")
    assert c.post(f"/api/instances/{gone}/console").status_code == 409
    assert c.post("/api/instances/ocid1.instance.oc1..nope/console").status_code == 404


def test_instance_console_reuses_job_console(cenv):
    """The instance endpoints find a console opened through the job (and vice versa): one connection."""
    c, fake = cenv.client, cenv.fake
    job = completed_job(cenv)
    jid, iid = job["id"], job["instance_id"]
    c.post(f"/api/jobs/{jid}/console")
    st = wait_console(c, jid, "ACTIVE")
    r = c.post(f"/api/instances/{iid}/console")
    assert r.status_code == 202 and r.json()["connection_id"] == st["connection_id"]
    assert c.get(f"/api/instances/{iid}/console").json()["job_id"] == jid
    assert len([x for x in fake.compute.console_connections.values() if x.instance_id == iid]) == 1
    with c.websocket_connect(f"/api/instances/{iid}/console/vnc", subprotocols=["binary"]) as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"echo:x"
    wait_until(lambda: c.get(f"/api/jobs/{jid}/console").json()["viewers"] == 0, what="viewer released")
    assert c.delete(f"/api/instances/{iid}/console").json()["state"] == "CLOSED"
    assert c.get(f"/api/jobs/{jid}/console").json()["state"] == "NONE"


def test_console_connections_deleted_on_shutdown(tmp_path):
    env = Env(tmp_path, fail_once=frozenset(), tunnel_factory=fake_tunnel)
    with TestClient(env.app) as c:
        env.client = c
        job = completed_job(env)
        c.post(f"/api/jobs/{job['id']}/console")
        st = wait_console(c, job["id"], "ACTIVE")
    assert env.fake.compute.console_deleted == [st["connection_id"]]
