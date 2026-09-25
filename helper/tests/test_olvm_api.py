"""OLVM login, inventory and a full migration against the fake engine and fake OCI."""

import pytest
from fastapi.testclient import TestClient

from .fake_olvm import CLUSTER, ENGINE, HOSTED, LOCKED, LUN, MOVING, PASSWORD, USER, WEB, WIN
from .test_api import env as _env  # noqa: F401 - fixture
from .test_api import login, target, wait_phase

OLVM = {"engine_url": ENGINE, "username": USER, "password": PASSWORD, "verify_ssl": False}


@pytest.fixture
def env(tmp_path, fast_retries):
    from .test_api import Env

    e = Env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def olvm_login(client, **overrides):
    response = client.post("/api/auth/olvm/login", json={**OLVM, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def test_olvm_login_session_and_errors(env):
    client = env.client
    response = client.post("/api/auth/olvm/login", json={**OLVM, "password": "nope"})
    assert response.status_code == 401 and "invalid user name or password" in response.text
    response = client.post("/api/auth/olvm/login", json={**OLVM, "engine_url": "https://olvm.test/some/page"})
    assert response.status_code == 401 and "engine host" in response.text
    assert client.get("/api/olvm/vms").status_code == 401

    me = olvm_login(client)
    assert me["anonymous"] is False and me["username"] == USER and me["olvm_engine"] == "olvm.test"
    assert me["vcenter_host"] == "" and me["azure_tenant_id"] == ""
    assert client.get("/api/auth/me").json()["olvm_engine"] == "olvm.test"
    assert client.get("/api/olvm/vms").status_code == 200
    assert client.get("/api/vms").status_code == 403
    assert client.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 204
    login(client)
    assert client.get("/api/olvm/vms").status_code == 403
    assert client.post("/api/jobs/olvm", json={"vm_id": WEB, "target": target()}).status_code == 403


def test_olvm_vm_list_and_inspect(env):
    client = env.client
    olvm_login(client)
    vms = client.get("/api/olvm/vms").json()
    assert {vm["name"] for vm in vms} == {"web-01", "win-01", "HostedEngine", "lun-01", "locked-01", "moving-01"}
    web = next(vm for vm in vms if vm["name"] == "web-01")
    assert web["folder"] == CLUSTER and web["power_state"] == "poweredOn" and web["num_disks"] == 2
    listed = len([row for row in env.olvm.requests if row.endswith("/vms")])
    client.get("/api/olvm/vms")
    assert len([row for row in env.olvm.requests if row.endswith("/vms")]) == listed
    client.get("/api/olvm/vms?refresh=true")
    assert len([row for row in env.olvm.requests if row.endswith("/vms")]) == listed + 1

    inspection = client.get("/api/olvm/vm", params={"id": WEB}).json()
    assert inspection["can_export"] is True and inspection["needs_power_off"] is True and inspection["tools_running"] is True
    assert inspection["vm"]["firmware"] == "efi" and inspection["vm"]["guest_id"] == "rhel9_64Guest"
    assert inspection["os"]["operating_system"] == "Red Hat Enterprise Linux"
    assert inspection["os"]["operating_system_version"] == "9"
    win = client.get("/api/olvm/vm", params={"id": WIN}).json()
    assert win["needs_power_off"] is False and win["vm"]["firmware"] == "bios"
    assert win["os"]["operating_system"] == "Windows" and "2022" in win["os"]["operating_system_version"]
    for vm_id, needle in ((HOSTED, "hosted engine"), (LUN, "lun disk"), (LOCKED, "locked"), (MOVING, "migrating")):
        refused = client.get("/api/olvm/vm", params={"id": vm_id}).json()
        assert refused["can_export"] is False and any(needle in problem for problem in refused["problems"])
    assert client.get("/api/olvm/vm", params={"id": "missing"}).status_code == 404


def test_olvm_migration_shuts_down_and_copies(env):
    client = env.client
    olvm_login(client)
    response = client.post("/api/jobs/olvm", json={"vm_id": WEB, "target": target()})
    assert response.status_code == 400 and "shut down" in response.text
    response = client.post("/api/jobs/olvm", json={"vm_id": WEB, "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["kind"] == "olvm" and created["power_off_source"] is True
    assert created["olvm"]["engine_host"] == "olvm.test" and created["olvm"]["cluster"] == CLUSTER
    assert created["olvm"]["disk_ids"] == ["disk-web-os", "disk-web-data"]
    assert client.post("/api/jobs/olvm", json={"vm_id": WEB, "target": target(), "power_off_source": True}).status_code == 409

    job = wait_phase(client, created["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    vm = env.olvm.vms[WEB]
    assert job["power_off_result"] == "guest_shutdown" and vm.status == "down" and vm.ops == ["shutdown"]
    assert [disk["status"] for disk in job["disks"]] == ["COPIED", "COPIED"]
    assert all(disk["bytes_written"] < disk["capacity_bytes"] for disk in job["disks"])
    assert env.olvm.transfers_finished == ["disk-web-os", "disk-web-data"]
    assert env.olvm.transfers_cancelled == []
    assert job["guest_fixup"]["status"] == "done" and job["network_fixup"]["status"] == "done"
    assert job["azure_fixup"] is None
    fake = env.fake
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents and env.raws[1] in contents
    tags = fake.compute.launch_details[-1].freeform_tags
    assert tags["oci-umt-source-olvm"] == f"olvm.test/{CLUSTER}"
    assert "oci-umt-source-esxi-host" not in tags
    assert tags["oci-umt-source-vm"] == "web-01"
    diag = client.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "source OLVM VM: web-01" in diag and "engine=olvm.test" in diag and "kind=olvm" in diag

    response = client.post("/api/jobs/olvm", json={
        "vm_id": WIN, "power_off_source": True,
        "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE"),
    })
    assert response.status_code == 202, response.text
    assert response.json()["power_off_source"] is False
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "already_off" and env.olvm.vms[WIN].ops == []
