"""End-to-end: Azure login, inventory and migrations through the web API against fake Azure + fake OCI."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from .fake_azure import CLIENT_ID, SECRET, SUB, TENANT, FakeDisk, FakeVm
from .test_api import MIB, Env, anonymous, login, target, wait_phase, wait_until

AZ = {"tenant_id": TENANT, "client_id": CLIENT_ID, "client_secret": SECRET}


@pytest.fixture
def env(tmp_path, fast_retries):
    e = Env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def azure_login(client, **overrides):
    r = client.post("/api/auth/azure/login", json={**AZ, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def vm_id(env: Env, name: str) -> str:
    return next(vm.id for vm in env.azure.vms.values() if vm.name == name)


def fake_vm(env: Env, name: str) -> FakeVm:
    return next(vm for vm in env.azure.vms.values() if vm.name == name)


# --------------------------------------------------------------------------- auth
def test_azure_login_session_and_errors(env):
    c = env.client
    r = c.post("/api/auth/azure/login", json={**AZ, "client_secret": "nope"})
    assert r.status_code == 401 and "invalid client secret" in r.text
    r = c.post("/api/auth/azure/login", json={**AZ, "tenant_id": "22222222-2222-3333-4444-555555555555"})
    assert r.status_code == 401 and "tenant not found" in r.text
    assert c.get("/api/azure/vms").status_code == 401

    me = azure_login(c)
    assert me["anonymous"] is False and me["username"] == f"azure:{CLIENT_ID}"
    assert me["azure_tenant_id"] == TENANT and me["azure_client_id"] == CLIENT_ID and me["vcenter_host"] == ""
    assert [s["id"] for s in me["azure_subscriptions"]] == [SUB]
    assert c.get("/api/auth/me").json()["azure_tenant_id"] == TENANT
    # an Azure session covers the OCI/ISO functions but not the vCenter ones (and vice versa)
    for path in ("/api/jobs", "/api/oci/options", "/api/setup/info", "/api/azure/vms"):
        assert c.get(path).status_code == 200, path
    assert c.get("/api/vms").status_code == 403
    assert c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).status_code == 403
    assert anonymous(c)["azure_tenant_id"] == TENANT  # the anonymous entry point keeps the Azure login
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/azure/vms").status_code == 401
    login(c)
    assert c.get("/api/azure/vms").status_code == 403
    assert c.post("/api/jobs/azure", json={"vm_id": vm_id(env, "lin-01"), "target": target()}).status_code == 403


# --------------------------------------------------------------------------- inventory
def test_azure_vm_list_and_inspect(env):
    c = env.client
    azure_login(c)
    vms = c.get("/api/azure/vms").json()
    assert {v["name"] for v in vms} == {"lin-01", "win-01", "enc-01", "conf-01", "ol-01"}
    lin = next(v for v in vms if v["name"] == "lin-01")
    assert lin["folder"] == "Prod Subscription/rg-prod" and lin["power_state"] == "poweredOn"
    assert lin["vm_size"] == "Standard_D2s_v3" and lin["num_disks"] == 2
    listed = len([r for r in env.azure.requests if r.endswith("/virtualMachines")])
    c.get("/api/azure/vms")  # cached
    assert len([r for r in env.azure.requests if r.endswith("/virtualMachines")]) == listed
    c.get("/api/azure/vms?refresh=true")
    assert len([r for r in env.azure.requests if r.endswith("/virtualMachines")]) == listed + 1

    insp = c.get("/api/azure/vm", params={"id": lin["moid"]}).json()
    assert insp["can_export"] is True and insp["needs_power_off"] is True and insp["tools_running"] is False
    assert insp["vm"]["num_cpu"] == 2 and insp["vm"]["firmware"] == "efi" and insp["vm"]["guest_id"] == "ubuntu64Guest"
    assert insp["os"] == {"operating_system": "Ubuntu", "operating_system_version": "22.04", "version_detected": True,
                          "version_choices": ["18.04", "20.04", "22.04", "24.04", "26.04"]}
    assert any("waagent" in w for w in insp["warnings"])
    # snapshot mode: no deallocation needed
    insp = c.get("/api/azure/vm", params={"id": lin["moid"], "capture_mode": "snapshot"}).json()
    assert insp["needs_power_off"] is False and any("crash-consistent" in w for w in insp["warnings"])
    enc = c.get("/api/azure/vm", params={"id": vm_id(env, "enc-01")}).json()
    assert enc["can_export"] is False and "Azure Disk Encryption" in enc["problems"][0]
    r = c.get("/api/azure/vm", params={"id": vm_id(env, "lin-01").replace("lin-01", "nope")})
    assert r.status_code == 404
    assert c.get("/api/azure/vm", params={"id": "vm-101"}).status_code == 502


def test_azure_revoke_stale_export_sas(env):
    c = env.client
    azure_login(c)
    vm = fake_vm(env, "win-01")
    vm.os_disk.sas_token = "stale"
    vid = vm_id(env, "win-01")
    insp = c.get("/api/azure/vm", params={"id": vid}).json()
    assert insp["can_export"] is False and len(insp["azure_revoke_export_disks"]) == 1
    assert insp["azure_revoke_export_disks"][0]["name"] == "win-01_OsDisk"
    r = c.post("/api/azure/revoke-export-access", json={"disk_ids": [insp["azure_revoke_export_disks"][0]["disk_id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is True and vm.os_disk.sas_token is None
    assert vm.os_disk.id in env.azure.sas_revoked
    insp = c.get("/api/azure/vm", params={"id": vid}).json()
    assert insp["can_export"] is True and insp["azure_revoke_export_disks"] == []
    r = c.post("/api/azure/revoke-export-access", json={"vm_id": vid})
    assert r.status_code == 200 and r.json()["results"] == []
    assert c.post("/api/azure/revoke-export-access", json={}).status_code == 422


# --------------------------------------------------------------------------- migrations
def test_azure_migration_deallocate_mode(env):
    c = env.client
    azure_login(c)
    vid = vm_id(env, "lin-01")
    vm = fake_vm(env, "lin-01")
    # a running VM needs the confirmation that it may be deallocated
    r = c.post("/api/jobs/azure", json={"vm_id": vid, "target": target()})
    assert r.status_code == 400 and "deallocated" in r.text and "snapshot mode" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": vid, "target": target(), "power_off_source": True})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["kind"] == "azure" and job["power_off_source"] is True and "deallocated" in job["message"]
    assert job["azure"]["subscription_id"] == SUB and job["azure"]["resource_group"] == "rg-prod"
    assert job["azure"]["capture_mode"] == "deallocate" and len(job["azure"]["disk_ids"]) == 2
    assert job["created_by"] == f"azure:{CLIENT_ID}" and [d["label"] for d in job["disks"]] == ["lin-01_OsDisk", "lin-01-data0"]
    # a second job for the same VM is refused while the first is active
    assert c.post("/api/jobs/azure", json={"vm_id": vid.upper(), "target": target(), "power_off_source": True}).status_code == 409

    job = wait_phase(c, job["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "deallocated" and vm.ops == ["deallocate"] and vm.power == "deallocated"
    assert [d["status"] for d in job["disks"]] == ["COPIED", "COPIED"]
    assert all(d["attempts"] == 1 and d["percent"] == 100 for d in job["disks"])
    # progress: exact, based on the allocated pages (sparse disks -> fewer bytes than the capacity)
    assert all(0 < d["stream_bytes"] < d["capacity_bytes"] for d in job["disks"])
    assert all(d["bytes_received"] == d["bytes_written"] == d["stream_bytes"] for d in job["disks"])
    tr = job["transfer"]
    assert tr["percent"] == 100 and tr["bytes_received"] == sum(d["stream_bytes"] for d in job["disks"])
    assert tr["started_at"] and tr["finished_at"] and job["summary"]["bytes_received"] == tr["bytes_received"]
    # SAS revoked, no snapshots, nothing left on the record
    assert job["azure"]["sas_granted"] == [] and job["azure"]["snapshot_ids"] == [] and job["azure"]["sas_expires_at"] is None
    assert sorted(env.azure.sas_granted) == sorted(d.id for d in vm.disks)
    assert sorted(env.azure.sas_revoked) == sorted(d.id for d in vm.disks)
    assert env.azure.snapshots_created == [] and all(d.sas_token is None for d in vm.disks)
    # the bytes on the OCI volumes equal the Azure disks; the guest fix-ups ran on the boot volume
    fake = env.fake
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents and env.raws[1] in contents
    assert job["guest_fixup"]["status"] == "done" and job["network_fixup"]["status"] == "done"
    assert job["azure_fixup"]["status"] == "done"
    assert env.fixups and env.fixups[-1][1:] == (True, True, True, False, False)
    # OCI side: Ubuntu 22.04 UEFI image, instance tagged with the Azure origin
    img = fake.compute.images[job["seed_image_id"]]
    assert (img.operating_system, img.operating_system_version) == ("Ubuntu", "22.04")
    assert job["launch_options"]["firmware"] == "UEFI_64"
    tags = fake.compute.launch_details[-1].freeform_tags
    assert tags["oci-umt-source-azure"] == f"{SUB}/rg-prod" and "oci-umt-source-vcenter" not in tags
    assert tags["oci-umt-source-vm"] == "lin-01" and tags["oci-umt-source-moid"] == vid.lower()
    assert tags["oci-umt-source-vm-details"].startswith("Azure shape Standard_D2s_v3, 2 vCPU, 8 GB RAM, 2 disk(s)")
    assert fake.compute.instances[job["instance_id"]].lifecycle_state == "RUNNING"
    # job list by source, diagnostics
    assert [j["id"] for j in c.get("/api/jobs", params={"vm_moid": vid.lower()}).json()] == [job["id"]]
    diag = c.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "source Azure VM: lin-01" in diag and f"subscription={SUB}" in diag and "capture_mode=deallocate" in diag
    assert "power_off_result=deallocated" in diag and "kind=azure" in diag

    # an already deallocated VM confirmed "just in case" is left alone
    vm2 = fake_vm(env, "win-01")
    r = c.post("/api/jobs/azure", json={"vm_id": vm2.id, "power_off_source": True,
                                        "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
    assert r.status_code == 202, r.text
    assert r.json()["power_off_source"] is False
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["power_off_result"] is None and vm2.ops == []
    assert job["launch_options"]["firmware"] == "BIOS" and job["guest_fixup"]["status"] == "skipped"
    ld = [d for d in fake.compute.launch_details if d.display_name == "win-01"][0]
    assert ld.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"


def test_azure_migration_snapshot_mode_keeps_vm_running(env):
    c = env.client
    azure_login(c)
    vm = fake_vm(env, "lin-01")
    r = c.post("/api/jobs/azure", json={"vm_id": vm.id, "target": target(), "capture_mode": "snapshot"})
    assert r.status_code == 202, r.text
    assert r.json()["power_off_source"] is False and "snapshotted" in r.json()["message"]
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert vm.power == "running" and vm.ops == [] and job["power_off_result"] == "snapshotted"
    assert len(env.azure.snapshots_created) == 2 and len(env.azure.snapshots_deleted) == 2 and env.azure.snapshots == {}
    assert env.azure.sas_granted == env.azure.snapshots_created  # SAS on the snapshots, never on the disks
    assert job["azure"]["snapshot_ids"] == [] and job["azure"]["sas_granted"] == []
    fake = env.fake
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents and env.raws[1] in contents
    assert "capture_mode=snapshot" in c.get(f"/api/jobs/{job['id']}/diagnostics").text
    # a stopped-but-allocated VM is deallocated in deallocate mode (Azure exports deallocated disks only)
    ol = fake_vm(env, "ol-01")
    assert ol.power == "stopped"
    r = c.post("/api/jobs/azure", json={"vm_id": ol.id, "target": target(), "power_off_source": True})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and ol.ops == ["deallocate"] and job["power_off_result"] == "deallocated"


def test_azure_cleanup_disabled_skips_fixup(env):
    c = env.client
    azure_login(c)
    vm = fake_vm(env, "lin-01")
    r = c.post("/api/jobs/azure", json={"vm_id": vm.id, "target": target(azure_cleanup=False),
                                        "capture_mode": "snapshot"})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["azure_fixup"]["status"] == "skipped" and "disabled" in job["azure_fixup"]["detail"]
    assert env.fixups and env.fixups[-1][1:] == (True, True, False, False, False)
    assert "azure fixup=skipped (enabled=False)" in c.get(f"/api/jobs/{job['id']}/diagnostics").text


def test_azure_job_validation(env):
    c = env.client
    azure_login(c)
    win = vm_id(env, "win-01")
    r = c.post("/api/jobs/azure", json={"vm_id": win, "target": target()})
    assert r.status_code == 400 and "license" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": vm_id(env, "enc-01"), "target": target()})
    assert r.status_code == 400 and "Azure Disk Encryption" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": vm_id(env, "conf-01"), "target": target()})
    assert r.status_code == 400 and "Confidential" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": win, "target": target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2",
                                                                       windows_license_type="BRING_YOUR_OWN_LICENSE")})
    assert r.status_code == 400 and "availability domain" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": win, "capture_mode": "clone", "target": target()})
    assert r.status_code == 422
    assert c.post("/api/jobs/azure", json={"vm_id": win.replace("win-01", "nope"), "target": target()}).status_code == 404
    # a custom image without OS metadata: the release must be picked
    env.azure.add_vm(FakeVm("custom-01", FakeDisk("custom-os", env.raws[0], os_type="Linux"), power="deallocated",
                            os_name="ubuntu"))
    cid = vm_id(env, "custom-01")
    insp = c.get("/api/azure/vm", params={"id": cid}).json()
    assert insp["os"]["operating_system"] == "Ubuntu" and insp["os"]["version_detected"] is False
    r = c.post("/api/jobs/azure", json={"vm_id": cid, "target": target()})
    assert r.status_code == 400 and "Azure does not report" in r.text
    r = c.post("/api/jobs/azure", json={"vm_id": cid, "target": target(operating_system_version="24.04")})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED"
    assert env.fake.compute.images[job["seed_image_id"]].operating_system_version == "24.04"
    assert not env.azure.snapshots


def test_azure_copy_retries_disk_after_range_failures(env):
    """A range that keeps failing fails the disk attempt; the next attempt starts over and succeeds."""
    c = env.client
    azure_login(c)
    vm = fake_vm(env, "win-01")
    # the first three range GETs fail (= the 3 tries of the first chunk): the disk attempt fails, the
    # second attempt copies everything
    calls = {"n": 0}
    orig_handle = env.azure._blob

    def flaky_blob(request, path, query):
        if request.headers.get("x-ms-range") and calls["n"] < 3:
            calls["n"] += 1
            import httpx

            return httpx.Response(500, text="<Error><Code>InternalError</Code></Error>")
        return orig_handle(request, path, query)

    env.azure._blob = flaky_blob
    env.settings.azure_range_workers = 1
    try:
        r = c.post("/api/jobs/azure", json={"vm_id": vm.id, "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
        assert r.status_code == 202, r.text
        job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    finally:
        env.azure._blob = orig_handle
    assert job["phase"] == "COMPLETED", job
    assert job["disks"][0]["attempts"] == 2 and job["disks"][0]["status"] == "COPIED"
    assert job["transfer"]["bytes_received"] >= job["disks"][0]["stream_bytes"]
    fake = env.fake
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents
    assert vm.os_disk.sas_token is None and "attempt 1 failed" not in job["message"]


def test_azure_cancel_during_copy_releases_everything(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset())
    env.azure.block_event = gate
    with TestClient(env.app) as c:
        azure_login(c)
        vm = fake_vm(env, "lin-01")
        r = c.post("/api/jobs/azure", json={"vm_id": vm.id, "target": target(), "capture_mode": "snapshot"})
        job_id = r.json()["id"]
        wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["disks"][0]["status"] == "COPYING", what="copying")
        snap_ids = c.get(f"/api/jobs/{job_id}").json()["azure"]["snapshot_ids"]
        assert len(snap_ids) == 2 and len(env.azure.snapshots) == 2
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 202
        gate.set()
        job = wait_phase(c, job_id, "CANCELLED", "COMPLETED", "FAILED")
        assert job["phase"] == "CANCELLED", job
        assert job["instance_id"] in env.fake.compute.terminated
        assert set(env.fake.blockstorage.deleted) == {d["volume_id"] for d in job["disks"]}
        assert env.azure.snapshots == {} and sorted(env.azure.snapshots_deleted) == sorted(snap_ids)
        assert job["azure"]["snapshot_ids"] == [] and job["azure"]["sas_granted"] == []
        assert vm.power == "running"
        assert not [t for t in threading.enumerate() if t.name.startswith("vhd-copy")]


def test_azure_failed_job_cleanup_releases_sas_with_a_later_login(tmp_path, fast_retries):
    """The helper restarts mid-copy: the job fails; cancelling it later from an Azure session revokes the
    SAS and deletes the snapshots the job recorded; without an Azure login the record says what is left."""
    from helper_app.azure.inventory import inspect_vm
    from helper_app.azure.session import AzureSession
    from helper_app.jobs.store import JobStore, utcnow
    from helper_app.models import AzureSourceInfo, DiskState, Job, JobPhase

    from .fake_azure import make_fleet
    from .test_vmdk_stream import make_raw

    raws = {0: make_raw(2 * MIB, seed=10), 1: make_raw(MIB, seed=11)}
    fake_az = make_fleet(raws)
    vm = next(v for v in fake_az.vms.values() if v.name == "lin-01")
    client = fake_az.client_factory(TENANT, CLIENT_ID, SECRET)
    vm.power = "deallocated"
    client.begin_get_access(vm.os_disk.id, 3600)
    assert vm.os_disk.sas_token
    spec = inspect_vm(AzureSession(client, []), vm.id).spec
    store = JobStore(str(tmp_path / "jobs.db"))
    now = utcnow()
    store.put(Job(id="stale-az", kind="azure", phase=JobPhase.EXPORTING, vm=spec, target=target(),
                  azure=AzureSourceInfo(tenant_id=TENANT, subscription_id=SUB, resource_group="rg-prod",
                                        disk_ids=[d.id for d in vm.disks], sas_granted=[vm.os_disk.id]),
                  disks=[DiskState(index=0, label="os", capacity_bytes=2 * MIB, is_boot=True)],
                  created_at=now, updated_at=now))
    env = Env(tmp_path, store=store, azure=fake_az)
    with TestClient(env.app) as c:
        # anonymous session: cleanup of the OCI side, Azure resources are reported as left behind
        anonymous(c)
        assert c.get("/api/jobs/stale-az").json()["phase"] == "FAILED"
        assert c.post("/api/jobs/stale-az/cancel").status_code == 202
        wait_phase(c, "stale-az", "CANCELLED")
        # the Azure part of the message is appended once the OCI cleanup has finished
        wait_until(lambda: "left behind" in c.get("/api/jobs/stale-az").json()["message"], what="Azure note")
        job = c.get("/api/jobs/stale-az").json()
        assert "lin-01_OsDisk" in job["message"]
        assert job["azure"]["sas_granted"] == [vm.os_disk.id] and vm.os_disk.sas_token
        # the same from an Azure session releases them
        job_rec = store.get("stale-az")
        job_rec.phase = JobPhase.FAILED
        store.put(job_rec)
        azure_login(c)
        assert c.post("/api/jobs/stale-az/cancel").status_code == 202
        wait_phase(c, "stale-az", "CANCELLED")
        wait_until(lambda: "revoked export access" in c.get("/api/jobs/stale-az").json()["message"], what="revoke")
        job = c.get("/api/jobs/stale-az").json()
        assert job["azure"]["sas_granted"] == [] and vm.os_disk.sas_token is None
