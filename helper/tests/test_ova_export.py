"""OVA/OVF export: OVF writer, multipart upload, block reader, and API end-to-end."""

from __future__ import annotations

import hashlib
import io
import threading
from types import SimpleNamespace as NS

from helper_app.disk.block_reader import iter_device_grains
from helper_app.disk.vmdk_stream import DEFAULT_GRAIN_SECTORS, SECTOR, StreamOptimizedDecoder, encode_stream_optimized
from helper_app.oci.object_upload import MultipartObjectWriter, put_small_object
from helper_app.ova.ovf import inspect_ovf, parse_ovf
from helper_app.ova.ovf_writer import build_manifest, build_ovf, map_os_to_ovf
from tests.fake_oci import oid
from tests.test_api import anonymous, login, wait_phase, wait_until
from tests.test_vmdk_stream import MemSink, make_raw

pytest_plugins = ("tests.test_api",)


def decode_vmdk(data: bytes, capacity: int) -> bytes:
    sink = MemSink(capacity)
    dec = StreamOptimizedDecoder(sink.write_at, expected_capacity_bytes=capacity)
    dec.feed(data)
    dec.finish()
    return bytes(sink.buf)


def test_ovf_round_trip():
    disks = [
        {"file_id": "file1", "href": "web-disk1.vmdk", "size_bytes": 1000, "capacity_bytes": 50 * 1024**3,
         "label": "boot"},
        {"file_id": "file2", "href": "web-disk2.vmdk", "size_bytes": 2000, "capacity_bytes": 100 * 1024**3,
         "label": "data"},
    ]
    xml = build_ovf("web", ("Oracle Linux", "9"), num_vcpu=4, memory_mb=8192,
                    firmware="UEFI_64", secure_boot=True, disks=disks)
    parsed = parse_ovf(xml)
    assert len(parsed.disks) == 2
    assert parsed.files["file1"].href == "web-disk1.vmdk"
    assert parsed.disks[0].capacity_bytes == 50 * 1024**3
    info = inspect_ovf(xml)
    assert info.disk_count == 2
    assert info.num_vcpu == 4
    assert info.memory_mb == 8192
    assert info.firmware == "UEFI_64"
    assert info.secure_boot is True
    assert "Oracle Linux" in info.os_description
    guest, os_id, _ = map_os_to_ovf("Windows", "Server 2022 Standard")
    assert "windows" in guest.lower()
    assert os_id
    mf = build_manifest([("web.ovf", "abc"), ("web-disk1.vmdk", "def")])
    assert b"SHA256(web.ovf)= abc" in mf


def test_block_reader_skips_zero_grains(tmp_path):
    grain = DEFAULT_GRAIN_SECTORS * SECTOR
    raw = bytearray(grain * 4)
    raw[0:16] = b"BOOTBOOTBOOTBOOT"
    raw[2 * grain : 2 * grain + 8] = b"DATA1234"
    path = tmp_path / "disk.raw"
    path.write_bytes(raw)
    grains = list(iter_device_grains(str(path), len(raw)))
    assert [lba for lba, _ in grains] == [0, 2 * (grain // SECTOR)]
    assert grains[0][1].startswith(b"BOOTBOOTBOOTBOOT")
    out = io.BytesIO()
    encode_stream_optimized(out, len(raw), grains, extent_name="disk.vmdk")
    decoded = decode_vmdk(out.getvalue(), len(raw))
    assert decoded == bytes(raw)


def test_multipart_object_writer_parts_and_small_put(env):
    fake = env.fake
    fake.object_storage.add_bucket("exports")
    clients = fake.clients()
    small = MultipartObjectWriter(clients, "testnamespace", "exports", "tiny.txt", part_size=64)
    small.write(b"hello")
    small.close()
    assert fake.object_storage.bucket_objects["exports"]["tiny.txt"] == b"hello"
    assert small.sha256_hex == hashlib.sha256(b"hello").hexdigest()

    writer = MultipartObjectWriter(clients, "testnamespace", "exports", "big.bin", part_size=8)
    writer.write(b"abcdefghijklmnop")  # 16 bytes -> two parts flushed, rest on close
    writer.close()
    assert fake.object_storage.bucket_objects["exports"]["big.bin"] == b"abcdefghijklmnop"
    assert writer.upload_id
    assert fake.object_storage.multiparts[writer.upload_id].get("committed")

    aborted = MultipartObjectWriter(clients, "testnamespace", "exports", "gone.bin", part_size=4)
    aborted.write(b"12345678")
    aborted.abort()
    assert aborted.upload_id is None or fake.object_storage.multiparts[aborted.upload_id]["aborted"]

    put_small_object(clients, "testnamespace", "exports", "note.ovf", b"<ovf/>")
    assert fake.object_storage.bucket_objects["exports"]["note.ovf"] == b"<ovf/>"


def _add_export_instance(fake, name="web-01", state="RUNNING"):
    image_id = oid("image")
    fake.compute.images[image_id] = NS(
        id=image_id, operating_system="Oracle Linux", operating_system_version="9",
        launch_mode="PARAVIRTUALIZED", compatible_shapes=["VM.Standard.E5.Flex"],
        compartment_id=fake.identity.compartment_id, lifecycle_state="AVAILABLE",
    )
    iid = fake.compute.add_instance(name, fake.identity.compartment_id, state=state)
    inst = fake.compute.instances[iid]
    inst.image_id = image_id
    inst.shape_config = NS(ocpus=2, memory_in_gbs=16)
    inst.launch_options = NS(firmware="UEFI_64")
    inst.platform_config = NS(is_secure_boot_enabled=False)
    boot_raw = make_raw(2 * 1024 * 1024, seed=21)
    data_raw = make_raw(1024 * 1024, seed=22)
    bv_id = oid("bootvolume")
    fake.blockstorage.boot_volumes[bv_id] = NS(
        id=bv_id, size_in_gbs=1, display_name=f"{name}-boot", lifecycle_state="AVAILABLE", image_id=image_id,
    )
    fake.blockstorage.volume_bodies[bv_id] = boot_raw
    boot_att_id = oid("bootvolumeattachment")
    fake.compute.boot_attachments[boot_att_id] = NS(
        id=boot_att_id, boot_volume_id=bv_id, instance_id=iid, lifecycle_state="ATTACHED",
    )
    vol_id = oid("volume")
    fake.blockstorage.volumes[vol_id] = NS(
        id=vol_id, size_in_gbs=1, display_name=f"{name}-data", lifecycle_state="AVAILABLE",
    )
    fake.blockstorage.volume_bodies[vol_id] = data_raw
    vol_att_id = oid("volumeattachment")
    fake.compute.vol_attachments[vol_att_id] = NS(
        id=vol_att_id, volume_id=vol_id, instance_id=iid,
        device=f"{fake.device_prefix}c", attachment_type="paravirtualized",
        is_shareable=False, is_read_only=False, lifecycle_state="ATTACHED",
    )
    vnic_id = oid("vnic")
    fake.network.vnics[vnic_id] = NS(
        id=vnic_id, is_primary=True, subnet_id="ocid1.subnet.oc1..1",
        lifecycle_state="AVAILABLE", private_ip="10.0.1.50", public_ip=None,
    )
    vnic_att_id = oid("vnicattachment")
    fake.compute.vnic_attachments[vnic_att_id] = NS(
        id=vnic_att_id, instance_id=iid, vnic_id=vnic_id, lifecycle_state="ATTACHED",
    )
    fake.object_storage.add_bucket("exports")
    return iid, bv_id, vol_id, boot_raw, data_raw


def test_ova_export_job(env):
    c, fake = env.client, env.fake
    iid, bv_id, vol_id, boot_raw, data_raw = _add_export_instance(fake)
    anonymous(c)
    r = c.post("/api/jobs/ova-export", json={"instance_id": iid, "bucket": "exports", "include_data_volumes": True})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED", timeout=60)
    assert job["phase"] == "COMPLETED", job
    assert job["kind"] == "ovaexport"
    spec = job["ova_export"]
    assert spec["ovf_object"] and spec["manifest_object"]
    assert len(spec["objects"]) == 4  # 2 vmdk + ovf + mf
    inst = fake.compute.instances[iid]
    assert inst.lifecycle_state == "STOPPED"
    boot_atts = [a for a in fake.compute.boot_attachments.values()
                 if a.instance_id == iid and a.lifecycle_state == "ATTACHED"]
    assert boot_atts and boot_atts[0].boot_volume_id == bv_id
    data_atts = [a for a in fake.compute.vol_attachments.values()
                 if a.instance_id == iid and a.lifecycle_state == "ATTACHED" and a.volume_id == vol_id]
    assert data_atts and data_atts[0].is_shareable
    helper_atts = [a for a in fake.compute.vol_attachments.values()
                   if a.instance_id == fake.identity.instance_id and a.lifecycle_state == "ATTACHED"]
    assert not helper_atts
    assert "START" not in [action for _, action in fake.compute.actions]

    boot_vmdk = fake.object_storage.bucket_objects["exports"][spec["objects"][0]]
    data_vmdk = fake.object_storage.bucket_objects["exports"][spec["objects"][1]]
    cap = 1024**3
    assert decode_vmdk(boot_vmdk, cap)[: len(boot_raw)] == boot_raw
    assert decode_vmdk(data_vmdk, cap)[: len(data_raw)] == data_raw
    ovf = fake.object_storage.bucket_objects["exports"][spec["ovf_object"]]
    info = inspect_ovf(ovf)
    assert info.disk_count == 2
    assert info.firmware == "UEFI_64"
    diag = c.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "ovaexport" in diag and iid in diag and "exports" in diag


def test_ova_export_rejects_helper_and_busy_bucket(env):
    c, fake = env.client, env.fake
    anonymous(c)
    r = c.post("/api/jobs/ova-export", json={"instance_id": fake.identity.instance_id, "bucket": "exports"})
    assert r.status_code == 400
    iid, *_ = _add_export_instance(fake)
    r = c.post("/api/jobs/ova-export", json={"instance_id": iid, "bucket": "missing"})
    assert r.status_code == 404
    inst = fake.compute.instances[iid]
    inst.lifecycle_state = "STOPPING"
    fake.object_storage.add_bucket("exports")
    r = c.post("/api/jobs/ova-export", json={"instance_id": iid, "bucket": "exports"})
    assert r.status_code == 409


def test_ova_export_cancel_restores_source(env, monkeypatch):
    c, fake = env.client, env.fake
    iid, bv_id, vol_id, *_ = _add_export_instance(fake)
    gate = threading.Event()
    from helper_app.oci import ova_export as mod

    real = mod.iter_device_grains

    def blocked(*args, **kwargs):
        gate.wait(timeout=15)
        check = kwargs.get("check_cancel")
        if check:
            check()
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "iter_device_grains", blocked)
    login(c)
    r = c.post("/api/jobs/ova-export", json={"instance_id": iid, "bucket": "exports"})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["phase"] == "EXPORTING", what="exporting")
    assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 202
    gate.set()
    job = wait_phase(c, job_id, "CANCELLED", "COMPLETED", "FAILED", timeout=30)
    assert job["phase"] == "CANCELLED", job
    inst = fake.compute.instances[iid]
    assert inst.lifecycle_state == "STOPPED"
    assert any(a.boot_volume_id == bv_id and a.lifecycle_state == "ATTACHED"
               for a in fake.compute.boot_attachments.values() if a.instance_id == iid)
    assert any(a.volume_id == vol_id and a.lifecycle_state == "ATTACHED"
               for a in fake.compute.vol_attachments.values() if a.instance_id == iid)
