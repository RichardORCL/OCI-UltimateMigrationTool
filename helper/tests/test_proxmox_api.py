"""Proxmox VE login (API token and password), inventory and full migrations against a fake Proxmox
API, fake SSH shells with an in-process NBD server, and fake OCI."""

import pytest
from fastapi.testclient import TestClient

from .fake_proxmox import (
    API_URL,
    HOST,
    LOCKED,
    NODE_IPS,
    PASS,
    PASSWORD,
    SSH_KEY,
    SSH_PASSWORD,
    STUCK,
    TOKEN_ID,
    TOKEN_SECRET,
    USER,
    WEB,
    WIN,
)
from .test_api import Env, login, target, wait_phase

TOKEN_LOGIN = {"api_url": API_URL, "auth_mode": "token", "token_id": TOKEN_ID, "token_secret": TOKEN_SECRET,
               "ssh_user": "root", "ssh_password": SSH_PASSWORD}
PASSWORD_LOGIN = {"api_url": API_URL, "auth_mode": "password", "username": USER, "password": PASSWORD}
WINDOWS = {"windows_license_type": "BRING_YOUR_OWN_LICENSE", "operating_system_version": "Windows11"}


@pytest.fixture
def env(tmp_path, fast_retries):
    e = Env(tmp_path)
    e.settings.proxmox_range_chunk_bytes = 64 * 1024  # the test disks are 1-2 MiB with zero grains
    with TestClient(e.app) as client:
        e.client = client
        yield e


def proxmox_login(client, body=None, **overrides):
    response = client.post("/api/auth/proxmox/login", json={**(body or TOKEN_LOGIN), **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def volume_contents(env) -> set[bytes]:
    atts = [a for a in env.fake.compute.vol_attachments.values() if a.instance_id == env.fake.identity.instance_id]
    return {open(a.device or a.fake_disk, "rb").read() for a in atts}


def test_proxmox_login_modes_and_errors(env):
    client, fleet = env.client, env.proxmox
    assert client.post("/api/auth/proxmox/login", json={**TOKEN_LOGIN, "token_secret": "nope"}).status_code == 401
    assert client.post("/api/auth/proxmox/login", json={**TOKEN_LOGIN, "ssh_password": "nope"}).status_code == 401
    assert client.post("/api/auth/proxmox/login", json={**TOKEN_LOGIN, "ssh_password": ""}).status_code == 401
    assert client.post("/api/auth/proxmox/login", json={**PASSWORD_LOGIN, "password": "nope"}).status_code == 401
    response = client.post("/api/auth/proxmox/login", json={**TOKEN_LOGIN, "api_url": "http://pve.test"})
    assert response.status_code == 401 and "https" in response.text
    fleet.missing_tools = ["qemu-nbd"]
    response = client.post("/api/auth/proxmox/login", json=TOKEN_LOGIN)
    assert response.status_code == 502 and "missing qemu-nbd" in response.text
    fleet.missing_tools = []
    assert client.get("/api/proxmox/vms").status_code == 401

    me = proxmox_login(client)
    assert me["username"] == "root@pam" and me["proxmox_host"] == HOST and me["proxmox_version"] == "8.2.4"
    assert me["vcenter_host"] == "" and me["nutanix_prism"] == ""
    assert fleet.ssh_logins[-1] == (HOST, "root", "password")
    assert client.get("/api/vms").status_code == 403
    assert client.post("/api/auth/logout").status_code == 204

    me = proxmox_login(client, PASSWORD_LOGIN)  # root@pam: the same password works for SSH
    assert me["username"] == USER and fleet.tickets_issued == 1
    proxmox_login(client, ssh_private_key=SSH_KEY, ssh_password="")
    assert fleet.ssh_logins[-1] == (HOST, "root", "key")
    response = client.post("/api/auth/proxmox/login", json={**PASSWORD_LOGIN, "username": "ops@pve"})
    assert response.status_code == 401
    login(client)
    assert client.get("/api/proxmox/vms").status_code == 403
    assert client.post("/api/jobs/proxmox", json={"vm_id": str(WEB), "target": target()}).status_code == 403


def test_proxmox_vm_list_and_inspect(env):
    client = env.client
    proxmox_login(client)
    vms = client.get("/api/proxmox/vms").json()
    assert {vm["name"] for vm in vms} == {"web-01", "win-01", "pass-01", "stuck-01", "locked-01"}  # no containers
    web = next(vm for vm in vms if vm["name"] == "web-01")
    assert web["folder"] == "pve1" and web["power_state"] == "poweredOn" and web["num_disks"] == 2
    assert web["num_cpu"] == 2 and web["memory_mb"] == 4096

    inspection = client.get("/api/proxmox/vm", params={"id": WEB}).json()
    vm = inspection["vm"]
    assert inspection["can_export"] is True and inspection["needs_power_off"] is True
    assert inspection["tools_running"] is True and vm["firmware"] == "efi"
    assert [d["label"] for d in vm["disks"]] == ["scsi0", "scsi1"]
    assert vm["nics"][0]["ip_addresses"] == ["192.168.20.15"] and "VLAN 20" in vm["nics"][0]["network"]
    assert inspection["os"]["operating_system"] == "Ubuntu" and inspection["os"]["operating_system_version"] == "22.04"
    assert any("CD-ROM" in w for w in inspection["warnings"]) and any("snapshot" in w for w in inspection["warnings"])

    win = client.get("/api/proxmox/vm", params={"id": WIN}).json()
    assert win["os"]["operating_system"] == "Windows" and win["os"]["version_detected"] is False
    assert win["vm"]["has_vtpm"] is True and any("confirm the OS" in w for w in win["warnings"])
    refused = client.get("/api/proxmox/vm", params={"id": PASS}).json()
    assert refused["can_export"] is False and any("passed-through" in p for p in refused["problems"])
    locked = client.get("/api/proxmox/vm", params={"id": LOCKED}).json()
    assert locked["can_export"] is False and any("locked (backup)" in p for p in locked["problems"])
    assert client.get("/api/proxmox/vm", params={"id": "999"}).status_code == 404
    assert client.get("/api/proxmox/vm", params={"id": "../etc"}).status_code == 404


def test_proxmox_migration_of_a_running_vm(env):
    client, fleet = env.client, env.proxmox
    proxmox_login(client)
    response = client.post("/api/jobs/proxmox", json={"vm_id": str(WEB), "target": target()})
    assert response.status_code == 400 and "shut down" in response.text
    response = client.post("/api/jobs/proxmox", json={"vm_id": str(WEB), "target": target(), "power_off_source": True})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["kind"] == "proxmox" and created["proxmox"]["node"] == "pve1"
    assert created["proxmox"]["volids"] == ["local:101/vm-101-disk-0.qcow2", "local-lvm:vm-101-disk-1"]
    assert created["proxmox"]["formats"] == ["qcow2", "raw"]

    job = wait_phase(client, created["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "guest_shutdown" and fleet.vms[WEB].ops == ["shutdown"]
    assert [d["status"] for d in job["disks"]] == ["COPIED", "COPIED"]
    total = len(env.raws[0]) + len(env.raws[1])
    assert fleet.bytes_served < total * 0.8  # zero grains were never read
    assert all(d["stream_bytes"] < d["capacity_bytes"] for d in job["disks"])
    contents = volume_contents(env)
    assert env.raws[0] in contents and env.raws[1] in contents
    px = job["proxmox"]
    assert px["nbd_exports"] == [] and fleet.exports == {} and len(fleet.started) == 2
    assert px["ssh_host"] == NODE_IPS["pve1"] and px["host_key_fingerprint"] == f"SHA256:fake-{NODE_IPS['pve1']}"
    assert "scsi0:" in px["extents"] and "scsi1:" in px["extents"]
    assert job["nfc_host"] == NODE_IPS["pve1"]
    started = [c for h, c in fleet.commands if c.startswith("qemu-nbd")]
    assert all("--read-only" in c and "--shared=5" in c for c in started)
    tags = env.fake.compute.launch_details[-1].freeform_tags
    assert tags["oci-umt-source-proxmox"] == f"{HOST}/pve1/{WEB}"
    assert "Proxmox node pve1" in tags["oci-umt-source-vm-details"]
    diag = client.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "source Proxmox VM: web-01" in diag and "local:101/vm-101-disk-0.qcow2[qcow2]" in diag


def test_proxmox_password_login_migrates_a_stopped_windows_vm(env):
    client, fleet = env.client, env.proxmox
    proxmox_login(client, PASSWORD_LOGIN)
    response = client.post("/api/jobs/proxmox", json={"vm_id": str(WIN), "target": target(**WINDOWS)})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "already_off" and fleet.vms[WIN].ops == []
    # a zvol reports no holes: everything is read, the zero chunks are not written
    assert fleet.bytes_served == len(env.raws[0])
    assert job["disks"][0]["bytes_written"] < job["disks"][0]["bytes_received"]
    assert env.raws[0] in volume_contents(env)
    assert (NODE_IPS["pve2"], "root", "password") in fleet.ssh_logins
    assert any(r.startswith("POST /access/ticket") for r in fleet.requests)


def test_proxmox_hard_stop(env):
    client, fleet = env.client, env.proxmox
    env.settings.proxmox_shutdown_timeout_s = 0
    proxmox_login(client)
    response = client.post("/api/jobs/proxmox", json={"vm_id": str(STUCK), "target": target(),
                                                      "power_off_source": True})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "powered_off" and fleet.vms[STUCK].ops == ["shutdown", "stop"]


def test_proxmox_failed_reads_stop_the_exports(env):
    client, fleet = env.client, env.proxmox
    fleet.fail_reads = True
    proxmox_login(client)
    response = client.post("/api/jobs/proxmox", json={"vm_id": str(WIN), "target": target(**WINDOWS)})
    assert response.status_code == 202, response.text
    job = wait_phase(client, response.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "FAILED" and "NBD read" in (job["error"] or ""), job
    assert fleet.started and fleet.exports == {} and job["proxmox"]["nbd_exports"] == []
