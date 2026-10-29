"""Nutanix login, inventory and full migrations (Prism Central and Prism Element) against fake Prism and fake OCI."""

import pytest
from fastapi.testclient import TestClient

from .fake_nutanix import CLUSTER, PASSWORD, PRISM, PRISM_URL, STUCK, USER, VG, WEB, WIN, make_fleet
from .test_api import login, target, wait_phase

NUTANIX = {"prism_url": PRISM_URL, "username": USER, "password": PASSWORD, "verify_ssl": False}


def make_env(tmp_path, **fleet_kw):
    from .test_api import Env

    env = Env(tmp_path)
    if fleet_kw:
        env = Env(tmp_path, nutanix=make_fleet(env.raws, **fleet_kw))
    env.settings.nutanix_chunk_bytes = 64 * 1024  # the test disks are 1-2 MiB with zero grains
    return env


@pytest.fixture
def env(tmp_path, fast_retries):
    e = make_env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        yield e


@pytest.fixture
def pe_env(tmp_path, fast_retries):
    e = make_env(tmp_path, flavor="pe", ranges=False)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def nutanix_login(client, **overrides):
    response = client.post("/api/auth/nutanix/login", json={**NUTANIX, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def test_nutanix_login_session_and_errors(env):
    client = env.client
    response = client.post("/api/auth/nutanix/login", json={**NUTANIX, "password": "nope"})
    assert response.status_code == 401
    response = client.post("/api/auth/nutanix/login", json={**NUTANIX, "prism_url": "http://prism.test"})
    assert response.status_code == 401 and "https" in response.text
    assert client.get("/api/nutanix/vms").status_code == 401

    me = nutanix_login(client)
    assert me["username"] == USER and me["nutanix_prism"] == PRISM and me["nutanix_api"] == "pc"
    assert me["vcenter_host"] == "" and me["olvm_engine"] == ""
    assert client.get("/api/nutanix/vms").status_code == 200
    assert client.get("/api/vms").status_code == 403
    assert client.post("/api/auth/logout").status_code == 204
    login(client)
    assert client.get("/api/nutanix/vms").status_code == 403
    assert client.post("/api/jobs/nutanix", json={"vm_id": WEB, "target": target()}).status_code == 403


def test_nutanix_vm_list_and_inspect(env):
    client = env.client
    nutanix_login(client)
    vms = client.get("/api/nutanix/vms").json()
    assert {vm["name"] for vm in vms} == {"web-01", "win-01", "vg-01", "cd-01", "stuck-01"}
    web = next(vm for vm in vms if vm["name"] == "web-01")
    assert web["folder"] == CLUSTER and web["power_state"] == "poweredOn" and web["num_disks"] == 2

    inspection = client.get("/api/nutanix/vm", params={"id": WEB}).json()
    assert inspection["can_export"] is True and inspection["needs_power_off"] is True
    assert inspection["tools_running"] is True and inspection["vm"]["firmware"] == "efi"
    assert inspection["os"]["operating_system"] == "Red Hat Enterprise Linux"
    refused = client.get("/api/nutanix/vm", params={"id": VG}).json()
    assert refused["can_export"] is False and any("volume group" in p for p in refused["problems"])
    assert client.get("/api/nutanix/vm", params={"id": "missing"}).status_code == 404


def test_nutanix_migration_on_prism_central(env):
    client = env.client
    nutanix_login(client)
    response = client.post("/api/jobs/nutanix", json={"vm_id": WEB, "target": target()})
    assert response.status_code == 400 and "shut down" in response.text
    response = client.post("/api/jobs/nutanix", json={"vm_id": WEB, "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["kind"] == "nutanix" and created["nutanix"]["api"] == "pc"
    assert created["nutanix"]["disk_ids"] == ["vd-web-os", "vd-web-data"]
    assert created["nutanix"]["cluster"] == CLUSTER

    job = wait_phase(client, created["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    fleet = env.nutanix
    assert job["power_off_result"] == "guest_shutdown" and fleet.vms[WEB].ops == ["OFF:ACPI"]
    assert [d["status"] for d in job["disks"]] == ["COPIED", "COPIED"]
    assert all(d["bytes_written"] < d["capacity_bytes"] for d in job["disks"])
    assert len(fleet.images_created) == 2 and fleet.images == {} and job["nutanix"]["image_uuids"] == []
    assert job["nfc_host"] == PRISM
    helper_atts = [a for a in env.fake.compute.vol_attachments.values()
                   if a.instance_id == env.fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents and env.raws[1] in contents
    tags = env.fake.compute.launch_details[-1].freeform_tags
    assert tags["oci-umt-source-nutanix"] == f"{PRISM}/{CLUSTER}"
    assert "Nutanix cluster" in tags["oci-umt-source-vm-details"]
    diag = client.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "source Nutanix VM: web-01" in diag and "api=pc" in diag


def test_nutanix_migration_on_prism_element_streams(pe_env):
    env = pe_env
    client = env.client
    me = nutanix_login(client)
    assert me["nutanix_api"] == "pe"
    response = client.post("/api/jobs/nutanix", json={
        "vm_id": WIN, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "already_off" and env.nutanix.vms[WIN].ops == []
    assert job["nutanix"]["api"] == "pe" and env.nutanix.images == {}
    assert any(r.startswith("POST /PrismGateway/services/rest/v2.0/images") for r in env.nutanix.requests)


def test_nutanix_hard_power_off(env):
    client = env.client
    env.settings.nutanix_shutdown_timeout_s = 0
    nutanix_login(client)
    response = client.post("/api/jobs/nutanix", json={"vm_id": STUCK, "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "powered_off" and env.nutanix.vms[STUCK].ops == ["OFF:ACPI", "OFF:HARD"]


def test_nutanix_failed_download_deletes_images(env):
    client = env.client
    env.nutanix.fail_download = True
    nutanix_login(client)
    response = client.post("/api/jobs/nutanix", json={
        "vm_id": WIN, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "FAILED" and "download" in (job["error"] or "")
    assert env.nutanix.images_created and env.nutanix.images == {}
    assert job["nutanix"]["image_uuids"] == []
