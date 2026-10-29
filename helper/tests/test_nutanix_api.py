"""Nutanix login, inventory and full migrations (Prism Central and Prism Element) against fake Prism and fake OCI."""

import pytest
from fastapi.testclient import TestClient

from . import fake_nutanix
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


def volume_contents(env) -> set[bytes]:
    atts = [a for a in env.fake.compute.vol_attachments.values() if a.instance_id == env.fake.identity.instance_id]
    return {open(a.device or a.fake_disk, "rb").read() for a in atts}


@pytest.fixture
def no_merge(monkeypatch):
    from helper_app.jobs import runner

    monkeypatch.setattr(runner, "REGION_MERGE_GAP", 0)  # the test disks alternate 64 KiB data and zero grains


def test_nutanix_sparse_copy_on_prism_central(env, no_merge):
    client = env.client
    nutanix_login(client)
    response = client.post("/api/jobs/nutanix", json={"vm_id": WEB, "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    fleet = env.nutanix
    total = len(env.raws[0]) + len(env.raws[1])
    assert job["nutanix"]["regions"].startswith("changed regions from a recovery point"), job["nutanix"]
    assert fleet.bytes_served < total * 0.8  # zero grains and the zero tail were never downloaded
    assert all(d["stream_bytes"] < d["capacity_bytes"] for d in job["disks"])
    assert env.raws[0] in volume_contents(env) and env.raws[1] in volume_contents(env)
    assert fleet.recovery_points == {} and len(fleet.recovery_points_deleted) == 1
    assert job["nutanix"]["recovery_point_ids"] == [] and fleet.images == {}
    # discover-cluster is only on v4.3 here, and compute-changed-regions only answers on the cluster
    assert any("/v4.3/" in r and r.endswith("discover-cluster") for r in fleet.requests)
    assert any(r.endswith("compute-changed-regions") and "/v4.3/" in r for r in fleet.requests)
    assert "nutanix regions: sparse_copy=True changed regions" in client.get(
        f"/api/jobs/{job['id']}/diagnostics").text


def test_nutanix_sparse_copy_on_prism_element(tmp_path, fast_retries, no_merge):
    env = make_env(tmp_path, flavor="pe", ranges=True)
    with TestClient(env.app) as client:
        nutanix_login(client)
        response = client.post("/api/jobs/nutanix", json={
            "vm_id": WIN, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
        assert response.status_code == 202, response.text
        job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    fleet = env.nutanix
    assert job["nutanix"]["regions"].startswith("changed regions from a temporary protection domain"), job
    assert fleet.bytes_served < len(env.raws[0]) * 0.8
    assert env.raws[0] in volume_contents(env)
    assert fleet.protection_domains == {} and len(fleet.protection_domains_deleted) == 1
    assert job["nutanix"]["protection_domains"] == []
    assert sum(r == f"POST {fake_nutanix.V3}/data/changed_regions" for r in fleet.requests) > 1  # paged


def test_nutanix_prism_element_prefers_a_download_path_with_ranges(tmp_path, fast_retries, no_merge):
    env = make_env(tmp_path, flavor="pe", ranges=False)
    env.nutanix.v08_ranges = True
    with TestClient(env.app) as client:
        nutanix_login(client)
        response = client.post("/api/jobs/nutanix", json={
            "vm_id": WIN, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
        job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert env.nutanix.bytes_served < len(env.raws[0]) * 0.8
    assert any(r.startswith("GET /api/nutanix/v0.8/images/") for r in env.nutanix.requests)
    assert env.raws[0] in volume_contents(env)


def test_nutanix_without_region_map_reads_full_disks(tmp_path, fast_retries, no_merge):
    env = make_env(tmp_path, regions=False)
    with TestClient(env.app) as client:
        nutanix_login(client)
        response = client.post("/api/jobs/nutanix", json={
            "vm_id": WIN, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
        job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["nutanix"]["regions"].startswith("full read:") and "pc.2024.3" in job["nutanix"]["regions"]
    assert env.nutanix.bytes_served >= len(env.raws[0])
    assert env.raws[0] in volume_contents(env)


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
