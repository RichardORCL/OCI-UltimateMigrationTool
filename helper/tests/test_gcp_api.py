"""GCP login, inventory and migrations through the web API against fake GCP + fake OCI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .fake_gcp import BUCKET, CLIENT_EMAIL, PROJECT, SA_JSON, make_fleet
from .test_api import Env, target, wait_phase

GCP_LOGIN = {"service_account_json": SA_JSON, "export_bucket": BUCKET}


@pytest.fixture
def env(tmp_path, fast_retries, monkeypatch):
    sizes = {0: 2 * 1024**2, 1: 1024**2}
    from .test_vmdk_stream import make_raw

    raws = {i: make_raw(s, seed=10 + i) for i, s in sizes.items()}
    gcp = make_fleet(raws)
    from helper_app.gcp import inventory

    # Scale the API's GiB unit to MiB in this test, preserving exact logical-size validation.
    monkeypatch.setattr(inventory, "GIB", 1024**2)
    for disk in gcp.disks.values():
        disk.size_gb = len(disk.data) // 1024**2
    e = Env(tmp_path, gcp=gcp)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def gcp_login(client, **overrides):
    r = client.post("/api/auth/gcp/login", json={**GCP_LOGIN, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def vm_id(env: Env, name: str) -> str:
    return f"projects/{PROJECT}/zones/europe-west3-a/instances/{name}"


def test_gcp_login_and_inventory(env):
    c = env.client
    assert c.get("/api/gcp/vms").status_code == 401
    me = gcp_login(c)
    assert me["gcp_client_email"] == CLIENT_EMAIL and me["gcp_export_bucket"] == BUCKET
    assert c.get("/api/vms").status_code == 403
    vms = c.get("/api/gcp/vms").json()
    assert {v["name"] for v in vms} == {"lin-01", "win-01"}
    lin = next(v for v in vms if v["name"] == "lin-01")
    insp = c.get("/api/gcp/vm", params={"id": lin["moid"]}).json()
    assert insp["can_export"] is True and insp["needs_power_off"] is True
    assert len(insp["vm"]["disks"]) == 2


def test_gcp_migration_stop_mode(env):
    c = env.client
    gcp_login(c)
    vid = vm_id(env, "lin-01")
    r = c.post("/api/jobs/gcp", json={"vm_id": vid, "target": target(), "power_off_source": True})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["kind"] == "gcp" and job["gcp"]["project_id"] == PROJECT
    job = wait_phase(c, job["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert [d["status"] for d in job["disks"]] == ["COPIED", "COPIED"]
    assert job["gcp_fixup"]["status"] == "done"
    assert env.fixups[-1][1:] == (True, True, False, True, False)
    tags = env.fake.compute.launch_details[-1].freeform_tags
    assert tags["oci-umt-source-gcp"] == f"{PROJECT}/europe-west3-a"
