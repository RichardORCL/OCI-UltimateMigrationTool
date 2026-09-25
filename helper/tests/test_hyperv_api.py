"""Hyper-V login, inventory and a full migration against the fake host and fake OCI."""

import pytest
from fastapi.testclient import TestClient

from .fake_hyperv import HOST, ORPHAN, PASS, PASSWORD, SAVED, SHIELD, USER, WEB, WIN
from .test_api import env as _env  # noqa: F401 - fixture
from .test_api import login, target, wait_phase

HYPERV = {"host": HOST, "username": USER, "password": PASSWORD, "use_https": True, "verify_ssl": False}


@pytest.fixture
def env(tmp_path, fast_retries):
    from .test_api import Env

    e = Env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def hyperv_login(client, **overrides):
    response = client.post("/api/auth/hyperv/login", json={**HYPERV, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def test_hyperv_login_session_and_errors(env):
    client = env.client
    response = client.post("/api/auth/hyperv/login", json={**HYPERV, "password": "nope"})
    assert response.status_code == 401 and "invalid user name or password" in response.text
    response = client.post("/api/auth/hyperv/login", json={**HYPERV, "host": "https://hv.test/some/page"})
    assert response.status_code == 401 and "host name" in response.text
    assert client.get("/api/hyperv/vms").status_code == 401

    me = hyperv_login(client)
    assert me["anonymous"] is False and me["username"] == USER and me["hyperv_host"] == HOST
    assert me["vcenter_host"] == "" and me["olvm_engine"] == ""
    assert client.get("/api/auth/me").json()["hyperv_host"] == HOST
    assert client.get("/api/hyperv/vms").status_code == 200
    assert client.get("/api/vms").status_code == 403
    assert client.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 204
    login(client)
    assert client.get("/api/hyperv/vms").status_code == 403
    assert client.post("/api/jobs/hyperv", json={"vm_id": WEB, "target": target()}).status_code == 403


def test_hyperv_vm_list_and_inspect(env):
    client = env.client
    hyperv_login(client)
    vms = client.get("/api/hyperv/vms").json()
    assert {vm["name"] for vm in vms} == {"web-01", "win-01", "saved-01", "pass-01", "shield-01", "orphan-01"}
    web = next(vm for vm in vms if vm["name"] == "web-01")
    assert web["folder"] == HOST and web["power_state"] == "poweredOn" and web["num_disks"] == 1
    inspection = client.get("/api/hyperv/vm", params={"id": WEB}).json()
    assert inspection["can_export"] is True and inspection["needs_power_off"] is True
    assert inspection["vm"]["firmware"] == "efi" and inspection["vm"]["secure_boot"] is True
    assert inspection["os"]["operating_system"] == "Ubuntu" and inspection["os"]["operating_system_version"] == "24.04"
    win = client.get("/api/hyperv/vm", params={"id": WIN}).json()
    assert win["needs_power_off"] is False and win["vm"]["firmware"] == "bios"
    assert win["os"]["operating_system"] == "Windows" and "2022" in win["os"]["operating_system_version"]
    for vm_id, needle in ((SAVED, "saved"), (PASS, "pass-through"), (SHIELD, "shielded"), (ORPHAN, "parent")):
        refused = client.get("/api/hyperv/vm", params={"id": vm_id}).json()
        assert refused["can_export"] is False and any(needle in problem for problem in refused["problems"])
    assert client.get("/api/hyperv/vm", params={"id": "missing"}).status_code == 404


def test_hyperv_migration_shuts_down_and_copies(env):
    client = env.client
    hyperv_login(client)
    response = client.post("/api/jobs/hyperv", json={"vm_id": WEB, "target": target()})
    assert response.status_code == 400 and "shut down" in response.text
    response = client.post("/api/jobs/hyperv", json={"vm_id": WEB, "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["kind"] == "hyperv" and created["power_off_source"] is True
    assert created["hyperv"]["host"] == HOST
    assert created["hyperv"]["disks"] == [[r"C:\Hyper-V\web.vhdx"]]
    assert client.post("/api/jobs/hyperv", json={"vm_id": WEB, "target": target(), "power_off_source": True}).status_code == 409

    job = wait_phase(client, created["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    vm = env.hyperv.vms[WEB]
    assert job["power_off_result"] == "guest_shutdown" and vm.state == "Off" and vm.ops == ["shutdown"]
    assert [disk["status"] for disk in job["disks"]] == ["COPIED"]
    assert job["disks"][0]["bytes_written"] < job["disks"][0]["capacity_bytes"]
    assert job["nfc_host"] == HOST
    assert job["guest_fixup"]["status"] == "done" and job["network_fixup"]["status"] == "done"
    assert job["azure_fixup"] is None
