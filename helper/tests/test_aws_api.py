"""End-to-end: AWS login, inventory and migrations through the web API against fake AWS + fake OCI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .fake_aws import ACCESS_KEY, ACCOUNT, REGION, SECRET, make_fleet
from .test_api import Env, anonymous, login, target, wait_phase

AWS = {"access_key_id": ACCESS_KEY, "secret_access_key": SECRET, "region": REGION}


@pytest.fixture
def env(tmp_path, fast_retries):
    e = Env(tmp_path, aws=make_fleet())
    with TestClient(e.app) as client:
        e.client = client
        yield e


def aws_login(client, **overrides):
    r = client.post("/api/auth/aws/login", json={**AWS, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def test_aws_login_session_and_errors(env):
    c = env.client
    r = c.post("/api/auth/aws/login", json={**AWS, "access_key_id": "AKIAOTHERKEY12EXAMPLE"})
    assert r.status_code == 401
    assert c.get("/api/aws/vms").status_code == 401

    me = aws_login(c)
    assert me["anonymous"] is False and me["username"] == f"aws:{ACCOUNT}"
    assert me["aws_account_id"] == ACCOUNT and me["aws_region"] == REGION and me["vcenter_host"] == ""
    assert c.get("/api/auth/me").json()["aws_account_id"] == ACCOUNT
    for path in ("/api/jobs", "/api/oci/options", "/api/setup/info", "/api/aws/vms"):
        assert c.get(path).status_code == 200, path
    assert c.get("/api/vms").status_code == 403
    assert c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).status_code == 403
    assert anonymous(c)["aws_account_id"] == ACCOUNT
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/aws/vms").status_code == 401
    login(c)
    assert c.get("/api/aws/vms").status_code == 403


def test_aws_vm_list_and_inspect(env):
    c = env.client
    aws_login(c)
    vms = c.get("/api/aws/vms").json()
    assert {v["name"] for v in vms} >= {"lin-01", "win-01", "store-01", "market-01"}
    lin = next(v for v in vms if v["name"] == "lin-01")
    assert lin["power_state"] == "poweredOn" and lin["vm_size"] == "t3.medium"
    listed = len([r for r in env.aws.requests if "DescribeInstances" in r or r.endswith("/")])
    c.get("/api/aws/vms")
    c.get("/api/aws/vms?refresh=true")

    insp = c.get("/api/aws/vm", params={"id": lin["moid"]}).json()
    assert insp["can_export"] is True
    assert insp["needs_power_off"] is True
    store = next(v for v in vms if v["name"] == "store-01")
    insp = c.get("/api/aws/vm", params={"id": store["moid"]}).json()
    assert insp["can_export"] is False
    assert any("instance store" in p for p in insp["problems"])


def test_aws_migration_stop_mode(env):
    c = env.client
    aws_login(c)
    lin = next(v for v in c.get("/api/aws/vms").json() if v["name"] == "lin-01")
    r = c.post("/api/jobs/aws", json={"vm_id": lin["moid"], "target": target(), "capture_mode": "stop",
                                      "power_off_source": True})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED")
    assert job["kind"] == "aws"
    assert job["power_off_result"] == "stopped"
    inst = next(i for i in env.aws.instances.values() if i.name == "lin-01")
    assert inst.state == "stopped"
    assert env.fixups and env.fixups[-1][1:] == (True, True, False, False, True)
    assert not env.aws.snapshots


def test_aws_migration_snapshot_mode(env):
    c = env.client
    aws_login(c)
    lin = next(v for v in c.get("/api/aws/vms").json() if v["name"] == "lin-01")
    r = c.post("/api/jobs/aws", json={"vm_id": lin["moid"], "target": target(), "capture_mode": "snapshot"})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED")
    assert job["power_off_result"] == "snapshotted"
    inst = next(i for i in env.aws.instances.values() if i.name == "lin-01")
    assert inst.state == "running"
    assert not env.aws.snapshots
