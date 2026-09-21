"""GCP preflight rules."""

from __future__ import annotations

from helper_app.gcp.inventory import GcpVmDetails
from helper_app.gcp.preflight import confidential_technology, preflight, warnings
from helper_app.models import DiskSpec, VmSpec


def _details(instance: dict) -> GcpVmDetails:
    disk = DiskSpec(
        index=0,
        label="boot",
        device_key=0,
        capacity_bytes=10 * 1024**3,
        controller_type="nvme",
        backing_file="projects/p/zones/z/disks/boot",
        thin_provisioned=True,
    )
    spec = VmSpec(
        moid="projects/p/zones/z/instances/vm",
        name="vm",
        num_cpu=2,
        memory_mb=4096,
        guest_id="ubuntu64Guest",
        power_state="poweredOn",
        disks=[disk],
    )
    return GcpVmDetails(instance, {}, spec, {})


def test_confidential_vm_is_allowed_with_warning():
    inst = {
        "status": "RUNNING",
        "disks": [{"type": "PERSISTENT", "boot": True, "source": "https://x/disks/boot"}],
        "confidentialInstanceConfig": {"confidentialInstanceType": "SEV_SNP"},
    }
    details = _details(inst)
    assert preflight(details) == []
    assert any("Confidential VM" in w for w in warnings(details, "stop"))


def test_confidential_technology_legacy_flag():
    inst = {"confidentialInstanceConfig": {"enableConfidentialCompute": True}}
    assert confidential_technology(inst) == "SEV"


def test_standard_vm_no_confidential_warning():
    inst = {"status": "TERMINATED", "disks": [{"type": "PERSISTENT", "boot": True, "source": "https://x/disks/boot"}]}
    details = _details(inst)
    assert confidential_technology(inst) is None
    assert not any("Confidential" in w for w in warnings(details, "stop"))
