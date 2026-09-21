import pytest

from helper_app.models import Firmware
from helper_app.vsphere.export import DiskUrl, ExportError, match_disk_urls, rewrite_lease_url
from helper_app.vsphere.inventory import preflight, vm_spec_from_vm, warnings

from .fake_vsphere import GIB, make_vm


def test_vm_spec_from_vm_basic():
    spec = vm_spec_from_vm(make_vm())
    assert spec.moid == "vm-101" and spec.name == "web-01"
    assert spec.num_cpu == 4 and spec.memory_mb == 8192
    assert spec.firmware == Firmware.EFI
    assert spec.power_state == "poweredOff"
    assert [d.index for d in spec.disks] == [0, 1]
    assert spec.disks[0].capacity_bytes == 40 * GIB
    assert spec.disks[0].controller_type == "pvscsi"
    assert spec.disks[0].controller_class == "ParaVirtualSCSIController"
    assert spec.disks[0].nfc_key_hint == "ParaVirtualSCSIController0:0"
    assert spec.disks[1].nfc_key_hint == "ParaVirtualSCSIController0:1"
    assert spec.disks[0].thin_provisioned and spec.disks[0].backing_file.endswith("web-01.vmdk")
    assert spec.nics[0].adapter_type == "vmxnet3" and spec.nics[0].network == "VM Network"
    assert spec.nics[0].ip_addresses == []  # nothing reported by VMware Tools
    assert spec.host_name == "esxi-01.test"
    assert preflight(spec) == []


def test_vm_spec_guest_ip_addresses():
    from types import SimpleNamespace as NS

    from helper_app.vsphere.inventory import guest_ip_addresses

    vm = make_vm(nics=("vmxnet3", "e1000"),
                 ips={0: ["fe80::1", "2001:db8::10", "10.1.2.3", "169.254.1.1"], 1: ["192.168.0.9"]})
    spec = vm_spec_from_vm(vm)
    # link-local dropped, IPv4 first, per adapter
    assert spec.nics[0].ip_addresses == ["10.1.2.3", "2001:db8::10"]
    assert spec.nics[1].ip_addresses == ["192.168.0.9"]
    # older Tools do not set deviceConfigId: the entry is matched by MAC address instead
    vm.guest.net[0].deviceConfigId = -1
    assert guest_ip_addresses(vm)[4000] == ["10.1.2.3", "2001:db8::10"]
    # no guest info at all (Tools never ran, or the property is not readable) -> unknown, no error
    vm.guest = NS(net=None)
    assert all(n.ip_addresses == [] for n in vm_spec_from_vm(vm).nics)
    del vm.guest
    assert all(n.ip_addresses == [] for n in vm_spec_from_vm(vm).nics)


def test_shut_down_paths():
    from helper_app.vsphere.power import PowerError, shut_down, tools_running

    msgs = []
    # already off: nothing happens
    vm = make_vm()
    assert shut_down(vm, "web-01", timeout_s=5, notify=msgs.append, poll_s=0) == "already_off"
    assert vm.power_ops == []
    # Tools running: guest shutdown, the VM stops after a couple of polls
    vm = make_vm(power_state="poweredOn", shutdown_polls=3)
    assert tools_running(vm)
    assert shut_down(vm, "web-01", timeout_s=5, notify=msgs.append, poll_s=0) == "guest_shutdown"
    assert vm.power_ops == ["ShutdownGuest"] and str(vm.runtime.powerState) == "poweredOff"
    assert any("VMware Tools" in m for m in msgs)
    # Tools running but the guest never stops: hard power-off after the timeout
    vm = make_vm(power_state="poweredOn", shutdown_polls=0)
    assert shut_down(vm, "web-01", timeout_s=0.05, notify=msgs.append, poll_s=0.01) == "powered_off"
    assert vm.power_ops == ["ShutdownGuest", "PowerOffVM_Task"]
    assert any("did not shut down" in m for m in msgs)
    # no Tools: straight to power-off
    vm = make_vm(power_state="poweredOn", tools_running=False)
    assert not tools_running(vm)
    assert shut_down(vm, "web-01", timeout_s=5, notify=msgs.append, poll_s=0) == "powered_off"
    assert vm.power_ops == ["PowerOffVM_Task"]
    # suspended: refused
    with pytest.raises(PowerError, match="suspended"):
        shut_down(make_vm(power_state="suspended"), "web-01", timeout_s=5, notify=msgs.append, poll_s=0)


def test_vm_spec_without_host():
    # a VM that vCenter does not (currently) place on a host still inspects fine
    assert vm_spec_from_vm(make_vm(host=None)).host_name == ""
    assert vm_spec_from_vm(make_vm(host="  ")).host_name == ""


def test_disk_ordering_ide_before_scsi_and_controller_types():
    vm = make_vm(disks=((20 * GIB, "lsilogic"), (10 * GIB, "ide"), (30 * GIB, "sata")), nics=("e1000",),
                 firmware="bios")
    spec = vm_spec_from_vm(vm)
    assert [d.controller_type for d in spec.disks] == ["ide", "lsilogic", "sata"]
    assert spec.disks[0].capacity_bytes == 10 * GIB
    assert spec.disks[1].controller_class == "VirtualLsiLogicController"
    assert spec.firmware == Firmware.BIOS
    assert spec.nics[0].adapter_type == "e1000"


def test_preflight_and_warnings():
    spec = vm_spec_from_vm(make_vm(power_state="poweredOn", snapshot=object(), secure_boot=True, num_cpu=3,
                                   disks=((10 * GIB, "pvscsi"),)))
    # powered on is fine (the migration shuts the VM down), suspended is not
    assert preflight(spec) == []
    suspended = vm_spec_from_vm(make_vm(power_state="suspended"))
    assert any("suspended" in p for p in preflight(suspended))
    notes = warnings(spec)
    assert any("snapshots" in n for n in notes)
    assert any("Secure Boot" in n for n in notes)
    assert any("3 vCPUs" in n for n in notes)
    assert any("50 GB" in n for n in notes)
    assert spec.is_windows is False
    assert vm_spec_from_vm(make_vm(guest_id="windows2019srv_64Guest")).is_windows
    assert spec.encrypted is False and spec.has_vtpm is False and spec.encrypted_disks == []


def test_encrypted_vms_are_refused_before_anything_is_created():
    """vSphere rejects ExportVm on encrypted VMs (opUnsupportedOnEncryptedVm) - this used to surface only
    after the seed image and instance had been created.  A Windows 11 VM with a vTPM is the common case."""
    from tests.fake_vsphere import summary_of

    win11 = vm_spec_from_vm(make_vm(guest_id="windows11_64Guest", encrypted=True, vtpm=True))
    assert win11.encrypted and win11.has_vtpm
    (problem,) = preflight(win11)
    assert "encrypted" in problem and "Virtual TPM" in problem and "BitLocker" in problem and "Encrypt VM" in problem
    assert summary_of(make_vm(encrypted=True)).encrypted and summary_of(make_vm(vtpm=True)).encrypted
    assert summary_of(make_vm(encrypted_disks=(0,))).encrypted
    assert summary_of(make_vm()).encrypted is False

    # encrypted VM home without vTPM
    (problem,) = preflight(vm_spec_from_vm(make_vm(encrypted=True)))
    assert "encrypted" in problem and "Virtual TPM" not in problem

    # only a disk is encrypted (storage policy with encryption)
    spec = vm_spec_from_vm(make_vm(encrypted_disks=(1,)))
    assert spec.encrypted is False and spec.encrypted_disks == ["Hard disk 2"]
    (problem,) = preflight(spec)
    assert "Hard disk 2" in problem and "storage policy" in problem


def test_describe_vsphere_fault():
    from pyVmomi import vmodl

    from helper_app.oci.clients import describe_error

    fault = vmodl.fault.NotSupported(msg="The operation is not supported on the object.")
    fault.faultMessage = [vmodl.LocalizableMessage(key="com.vmware.vim.vpxd.encryption.opUnsupportedOnEncryptedVm",
                                                   message="The operation is not supported on encrypted VM")]
    assert describe_error(fault) == ("vSphere NotSupported: The operation is not supported on the object. "
                                     "(The operation is not supported on encrypted VM)")
    assert describe_error(vmodl.fault.InvalidArgument(msg="bad")) == "vSphere InvalidArgument: bad"
    assert describe_error(RuntimeError("plain")) == "plain"


def test_rewrite_lease_url():
    assert rewrite_lease_url("https://*/ha-nfc/52a1.vmdk?x=1", "vc.example.com") == \
        "https://vc.example.com/ha-nfc/52a1.vmdk?x=1"
    assert rewrite_lease_url("https://*:443/nfc/disk-0.vmdk", "esx1") == "https://esx1:443/nfc/disk-0.vmdk"
    assert rewrite_lease_url("https://esx1/nfc/disk.vmdk", "vc") == "https://esx1/nfc/disk.vmdk"


def test_match_disk_urls_by_key():
    spec = vm_spec_from_vm(make_vm())
    urls = [
        DiskUrl(key="/vm-101/ParaVirtualSCSIController0:1", target_id="disk-1.vmdk", url="u1"),
        DiskUrl(key="/vm-101/ParaVirtualSCSIController0:0", target_id="disk-0.vmdk", url="u0"),
    ]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "u0" and m[1].url == "u1"


def test_match_disk_urls_fallback_target_id_and_positional():
    spec = vm_spec_from_vm(make_vm())
    urls = [DiskUrl(key="", target_id="disk-1.vmdk", url="u1"), DiskUrl(key="", target_id="disk-0.vmdk", url="u0")]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "u0" and m[1].url == "u1"
    urls = [DiskUrl(key="a", target_id="x.vmdk", url="p0"), DiskUrl(key="b", target_id="y.vmdk", url="p1")]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "p0" and m[1].url == "p1"
    with pytest.raises(ExportError):
        match_disk_urls(spec.disks, [DiskUrl(key="", target_id="", url="only-one")])
