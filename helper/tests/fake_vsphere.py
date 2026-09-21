"""Fake vCenter: vim.VirtualMachine objects built from real pyVmomi data objects, a connector that
accepts fixed credentials and an NFC export stand-in serving pre-encoded stream-optimized VMDKs."""

from __future__ import annotations

from types import SimpleNamespace as NS

from pyVmomi import vim

from helper_app.models import VmSummary
from helper_app.vsphere.export import DiskUrl, ExportError
from helper_app.vsphere.session import VCenterAuthError, VCenterError

GIB = 1024**3


def make_vm(
    moid="vm-101",
    name="web-01",
    guest_id="oracleLinux8_64Guest",
    guest_full_name="Oracle Linux 8 (64-bit)",
    firmware="efi",
    power_state="poweredOff",
    num_cpu=4,
    memory_mb=8192,
    disks=((40 * GIB, "pvscsi"), (100 * GIB, "pvscsi")),
    nics=("vmxnet3",),
    secure_boot=False,
    snapshot=None,
    folder="DC1/Prod",
    template=False,
    host="esxi-01.test",
    ips=None,  # {nic index: [ip, ...]} as VMware Tools last reported it (guest.net); None = nothing known
    tools_running=True,
    shutdown_polls=1,  # ShutdownGuest: the VM reports poweredOff after this many powerState reads (0 = never)
    encrypted=False,  # VM encryption (config.keyId set)
    vtpm=False,  # a Virtual TPM device (implies encryption on real vSphere; here they are independent knobs)
    encrypted_disks=(),  # indices of disks whose backing has a keyId
):
    d = vim.vm.device
    devices = []
    controllers = {}
    ctrl_classes = {
        "pvscsi": d.ParaVirtualSCSIController,
        "lsilogic": d.VirtualLsiLogicController,
        "lsilogicsas": d.VirtualLsiLogicSASController,
        "buslogic": d.VirtualBusLogicController,
        "ide": d.VirtualIDEController,
        "sata": d.VirtualAHCIController,
        "nvme": d.VirtualNVMEController,
    }
    key = 1000
    for ctype in {c for _, c in disks}:
        ctrl = ctrl_classes[ctype]()
        ctrl.key = key
        ctrl.busNumber = 0
        ctrl.deviceInfo = vim.Description(label=f"{ctype} controller 0", summary="")
        controllers[ctype] = ctrl
        devices.append(ctrl)
        key += 1

    unit = {c: 0 for c in controllers}
    for i, (size, ctype) in enumerate(disks):
        disk = d.VirtualDisk()
        disk.key = 2000 + i
        disk.controllerKey = controllers[ctype].key
        disk.unitNumber = unit[ctype]
        unit[ctype] += 1
        disk.capacityInBytes = size
        disk.capacityInKB = size // 1024
        disk.deviceInfo = vim.Description(label=f"Hard disk {i + 1}", summary="")
        backing = d.VirtualDisk.FlatVer2BackingInfo()
        backing.fileName = f"[ds1] {name}/{name}{'' if i == 0 else '_' + str(i)}.vmdk"
        backing.thinProvisioned = True
        if i in encrypted_disks:
            backing.keyId = vim.encryption.CryptoKeyId(keyId=f"key-{i}", providerId=vim.encryption.KeyProviderId(id="kms"))
        disk.backing = backing
        devices.append(disk)
    if vtpm:
        tpm = d.VirtualTPM()
        tpm.key = 11000
        tpm.deviceInfo = vim.Description(label="Virtual TPM", summary="")
        devices.append(tpm)

    nic_classes = {"vmxnet3": d.VirtualVmxnet3, "e1000": d.VirtualE1000, "e1000e": d.VirtualE1000e}
    for i, ntype in enumerate(nics):
        nic = nic_classes[ntype]()
        nic.key = 4000 + i
        nic.macAddress = f"00:50:56:00:00:{i:02x}"
        nic.deviceInfo = vim.Description(label=f"Network adapter {i + 1}", summary="")
        backing = d.VirtualEthernetCard.NetworkBackingInfo()
        backing.deviceName = "VM Network"
        nic.backing = backing
        devices.append(nic)

    hardware = NS(numCPU=num_cpu, memoryMB=memory_mb, device=devices)
    boot_options = NS(efiSecureBootEnabled=secure_boot)
    config = NS(name=name, instanceUuid="5023-abcd", guestId=guest_id, guestFullName=guest_full_name,
                firmware=firmware, hardware=hardware, bootOptions=boot_options, template=template,
                keyId=(vim.encryption.CryptoKeyId(keyId="vm-key", providerId=vim.encryption.KeyProviderId(id="kms"))
                       if encrypted else None))
    runtime = NS(powerState=power_state, host=NS(name=host) if host else None)
    net = []
    for i, addrs in (ips or {}).items():
        # like GuestNicInfo: the legacy ipAddress list plus the newer ipConfig entries (same addresses)
        net.append(NS(deviceConfigId=4000 + i, macAddress=f"00:50:56:00:00:{i:02x}", ipAddress=list(addrs),
                      ipConfig=NS(ipAddress=[NS(ipAddress=a, prefixLength=24) for a in addrs])))
    guest = NS(net=net, ipAddress=(net[0].ipAddress[0] if net and net[0].ipAddress else None),
               toolsRunningStatus="guestToolsRunning" if tools_running else "guestToolsNotRunning")
    vm = NS(_moId=moid, config=config, runtime=runtime, snapshot=snapshot, name=name, folder_path=folder, guest=guest,
            power_ops=[])

    # power operations like vim.VirtualMachine: ShutdownGuest is asynchronous (the guest stops a little
    # later, or never), PowerOffVM_Task is a task that stops the VM at once
    class _Runtime:
        def __init__(self):
            self._state = power_state
            self._pending = None
            self.host = runtime.host

        @property
        def powerState(self):  # noqa: N802 - vSphere naming
            if self._pending is not None:
                self._pending -= 1
                if self._pending <= 0:
                    self._state, self._pending = "poweredOff", None
            return self._state

        @powerState.setter
        def powerState(self, value):  # noqa: N802
            self._state, self._pending = value, None

    vm.runtime = _Runtime()

    def shutdown_guest():
        vm.power_ops.append("ShutdownGuest")
        if vm.guest.toolsRunningStatus != "guestToolsRunning":
            raise vim.fault.ToolsUnavailable()
        if shutdown_polls:
            vm.runtime._pending = shutdown_polls

    def power_off_task():
        vm.power_ops.append("PowerOffVM_Task")
        vm.runtime.powerState = "poweredOff"
        return NS(info=NS(state="success"))

    vm.ShutdownGuest = shutdown_guest
    vm.PowerOffVM_Task = power_off_task
    return vm


def summary_of(vm) -> VmSummary:
    disks = [dev for dev in vm.config.hardware.device if type(dev).__name__.endswith("VirtualDisk")]
    return VmSummary(
        moid=vm._moId, name=vm.config.name, folder=vm.folder_path, power_state=str(vm.runtime.powerState),
        guest_full_name=vm.config.guestFullName, guest_id=vm.config.guestId, num_cpu=vm.config.hardware.numCPU,
        memory_mb=vm.config.hardware.memoryMB, num_disks=len(disks),
        disk_capacity_bytes=sum(d.capacityInBytes for d in disks), is_template=bool(vm.config.template),
        encrypted=vm.config.keyId is not None
        or any(type(dev).__name__.endswith("VirtualTPM") for dev in vm.config.hardware.device)
        or any(getattr(d.backing, "keyId", None) is not None for d in disks),
    )


class FakeVCenterSession:
    def __init__(self, connector: "FakeVCenterConnector", username: str, host: str = "vc.test", port: int = 443,
                 verify_ssl: bool = False):
        self.c = connector
        self.username = username
        self.host = host
        self.port = port
        self.verify_ssl = verify_ssl
        self.closed = False
        self.keepalives = 0

    @property
    def version(self) -> str:
        return "Fake vCenter 8.0"

    def vm(self, moid):
        if moid not in self.c.vms:
            raise VCenterError(f"virtual machine {moid} not found")
        return self.c.vms[moid]

    def list_vms(self):
        self.c.list_calls += 1
        return [summary_of(vm) for vm in self.c.vms.values()]

    def keepalive(self) -> bool:
        self.keepalives += 1
        return not self.closed

    def close(self):
        self.closed = True
        self.c.closed.append(self)


class FakeVCenterConnector:
    def __init__(self, vms: dict[str, object], users: dict[str, str] | None = None):
        self.vms = vms
        self.users = users or {"admin@vsphere.local": "secret"}
        self.sessions: list[FakeVCenterSession] = []
        self.closed: list[FakeVCenterSession] = []
        self.list_calls = 0

    @property
    def host(self) -> str:
        return "vc.test"

    def login(self, username, password, host="", port=None, verify_ssl=None):
        from helper_app.vsphere.session import parse_vcenter_address

        host, port = parse_vcenter_address(host, self.host, port or 443)
        if self.users.get(username) != password:
            raise VCenterAuthError("invalid vCenter user name or password")
        session = FakeVCenterSession(self, username, host, port, verify_ssl=bool(verify_ssl))
        self.sessions.append(session)
        return session


class FakeExport:
    """Stands in for NfcExport: serves pre-encoded stream-optimized VMDKs, optionally failing once."""

    instances: list["FakeExport"] = []

    def __init__(self, vm, payloads: dict[int, bytes], fail_once: set[int] = frozenset(), chunk=64 * 1024,
                 block_event=None):
        self.vm = vm
        self.payloads = payloads
        self.fail_once = set(fail_once)
        self.chunk = chunk
        self.block_event = block_event  # when set, iter_disk waits for it before each chunk
        self.completed = False
        self.aborted = False
        self.failed_reason = None
        self.sent = 0
        FakeExport.instances.append(self)

    @property
    def percent(self) -> int:
        """Like NfcExport: sent bytes over the lease's total stream size (here: the payload sizes)."""
        total = sum(len(p) for p in self.payloads.values())
        return max(0, min(99, int(self.sent * 100 / total))) if total else 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.failed_reason is None:
            self.completed = True
        else:
            self.aborted = True

    def disk_urls(self):
        n_disks = sum(1 for d in self.vm.config.hardware.device if type(d).__name__.endswith("VirtualDisk"))
        return [DiskUrl(key=f"/{self.vm._moId}/ParaVirtualSCSIController0:{i}", target_id=f"disk-{i}.vmdk",
                        url=f"nfc://disk/{i}", file_size=len(self.payloads[i]))
                for i in sorted(self.payloads) if i < n_disks]

    def iter_disk(self, url, on_progress=None):
        idx = int(url.rsplit("/", 1)[1])
        data = self.payloads[idx]
        half = len(data) // 2
        for pos in range(0, len(data), self.chunk):
            if self.block_event is not None:
                self.block_event.wait(timeout=10)
            chunk = data[pos : pos + self.chunk]
            if idx in self.fail_once and pos >= half:
                self.fail_once.discard(idx)
                raise ExportError("simulated NFC read error")
            self.sent += len(chunk)
            if on_progress:
                on_progress(len(chunk))
            yield chunk

    def mark_failed(self, reason):
        self.failed_reason = reason
