"""WinRM client for one Hyper-V host.

Inventory, guest identity and power changes are one PowerShell script each (NTLM). The password
stays on this object and is not logged. ``runner`` replaces WinRM in tests.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_INVENTORY = r"""# hyperv-action: inventory
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V
$OnlyId = 'ONLY_ID'
if ($OnlyId) {
  $vms = @(Get-VM -Id $OnlyId -ErrorAction SilentlyContinue)
} else {
  $vms = @(Get-VM)
}
function Get-DiskChain([string]$Path) {
  $chain = New-Object System.Collections.Generic.List[string]
  $guard = @{}
  $current = $Path
  while ($current -and -not $guard.ContainsKey($current)) {
    $guard[$current] = $true
    [void]$chain.Add($current)
    try { $vhd = Get-VHD -Path $current } catch { break }
    $current = [string]$vhd.ParentPath
  }
  return @($chain)
}
foreach ($vm in $vms) {
  $osName = ''
  $osVersion = ''
  try {
    $filter = "SystemName='{0}'" -f $vm.Id
    $kvp = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_KvpExchangeComponent -Filter $filter
    if ($kvp -and $kvp.GuestIntrinsicExchangeItems) {
      foreach ($item in @($kvp.GuestIntrinsicExchangeItems)) {
        $xml = [xml]$item
        $pairs = @{}
        foreach ($prop in $xml.INSTANCE.PROPERTY) { $pairs[$prop.NAME] = [string]$prop.VALUE }
        if ($pairs['Name'] -eq 'OSName') { $osName = $pairs['Data'] }
        if ($pairs['Name'] -eq 'OSVersion') { $osVersion = $pairs['Data'] }
      }
    }
  } catch {}
  $secure = $false
  $bootPath = ''
  if ($vm.Generation -eq 2) {
    try {
      $fw = Get-VMFirmware -VM $vm
      $secure = [bool]$fw.SecureBoot
      foreach ($entry in @($fw.BootOrder)) {
        $dev = $entry.Device
        if ($dev -and $dev.Path) { $bootPath = [string]$dev.Path; break }
      }
    } catch {}
  }
  $shielded = $false
  try { $shielded = [bool]$vm.IsShielded } catch {}
  $disks = @()
  foreach ($drive in @(Get-VMHardDiskDrive -VM $vm)) {
    $path = [string]$drive.Path
    $passthrough = [string]::IsNullOrEmpty($path)
    $size = [int64]0
    $vhdType = ''
    $shared = $false
    $chain = @()
    if (-not $passthrough) {
      $vhd = Get-VHD -Path $path
      $size = [int64]$vhd.Size
      $vhdType = [string]$vhd.VhdType
      try { $shared = [bool]$vhd.Shared } catch {}
      $chain = @(Get-DiskChain $path)
    }
    $disks += [ordered]@{
      path = $path
      controller = [string]$drive.ControllerType
      controller_number = [int]$drive.ControllerNumber
      controller_location = [int]$drive.ControllerLocation
      size_bytes = $size
      vhd_type = $vhdType
      shared = $shared
      passthrough = $passthrough
      boot = [bool]($path -and $path -eq $bootPath)
      chain = @($chain)
    }
  }
  $nics = @()
  foreach ($nic in @(Get-VMNetworkAdapter -VM $vm)) {
    $nics += [ordered]@{
      name = [string]$nic.Name
      mac = [string]$nic.MacAddress
      switch = [string]$nic.SwitchName
    }
  }
  $obj = [ordered]@{
    id = [string]$vm.Id
    name = [string]$vm.Name
    state = [string]$vm.State
    generation = [int]$vm.Generation
    cpu = [int]$vm.ProcessorCount
    memory_mb = [int]($vm.MemoryStartup / 1MB)
    shielded = $shielded
    secure_boot = $secure
    os_name = $osName
    os_version = $osVersion
    nics = @($nics)
    disks = @($disks)
  }
  ConvertTo-Json -InputObject $obj -Depth 6 -Compress
}
"""


class HypervError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class HypervAuthError(HypervError):
    pass


def parse_host(raw: str, use_https: bool) -> tuple[str, int, str]:
    """Return ``(hostname, winrm port, display)`` from the login form.

    ``https://hv.example.com``, ``hv.example.com`` and ``hv.example.com:5986`` are accepted.
    The port defaults to 5986 for HTTPS and 5985 for HTTP.
    """
    text = (raw or "").strip()
    if not text:
        raise HypervAuthError("Hyper-V host is required")
    if "://" in text:
        parts = urlsplit(text if "://" in text else f"https://{text}")
        if parts.scheme not in ("http", "https"):
            raise HypervAuthError(f"Hyper-V host must be a host name, not {parts.scheme!r}")
        if parts.username or parts.password:
            raise HypervAuthError("put the user name in the user field, not in the host")
        text = parts.netloc
        if parts.path not in ("", "/"):
            raise HypervAuthError("enter the Hyper-V host name, not a page path")
    if "@" in text:
        raise HypervAuthError("put the user name in the user field, not in the host")
    host = text
    port: Optional[int] = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise HypervAuthError(f"Hyper-V host {raw!r} is not a valid address")
        host = text[1:end]
        if len(text) > end + 1 and text[end + 1] == ":":
            port = _port(text[end + 2:])
    elif text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
        port = _port(port_text)
    host = host.strip()
    if not host:
        raise HypervAuthError("Hyper-V host is required")
    if port is None:
        port = 5986 if use_https else 5985
    display = host if port in (5985, 5986) else f"{host}:{port}"
    return host, port, display


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise HypervAuthError(f"WinRM port {text!r} is not a number") from exc
    if not 1 <= port <= 65535:
        raise HypervAuthError(f"WinRM port {port} is out of range")
    return port


def _decode(payload: bytes | str) -> str:
    if isinstance(payload, str):
        return payload
    return payload.decode("utf-8", errors="replace")


class HypervClient:
    """One WinRM login. ``runner(script) -> stdout`` is set by tests instead of a real host."""

    def __init__(self, host: str, username: str, password: str, *, use_https: bool = True, port: int = 5986,
                 verify_ssl: bool = False, runner: Optional[Callable[[str], str]] = None):
        self.host = host
        self.username = username
        self.password = password
        self.use_https = use_https
        self.port = port
        self.verify_ssl = verify_ssl
        self._runner = runner

    def close(self) -> None:
        return None

    def run(self, script: str) -> str:
        if self._runner is not None:
            return self._runner(script)
        return self._winrm(script)

    def probe(self) -> str:
        """Confirm the login and that the Hyper-V module answers. Returns the host name."""
        return self.run("# hyperv-action: probe\n(Get-VMHost).Name\n").strip()

    def list_vms(self) -> list[dict]:
        return self._inventory("")

    def get_vm(self, vm_id: str) -> dict:
        self._check_id(vm_id)
        rows = self._inventory(vm_id)
        if not rows:
            raise HypervError(f"virtual machine {vm_id} was not found", status=404)
        return rows[0]

    def state(self, vm_id: str) -> str:
        self._check_id(vm_id)
        return self.run(f"# hyperv-action: state\n(Get-VM -Id '{vm_id}').State.ToString()\n").strip()

    def shutdown(self, vm_id: str) -> None:
        self._check_id(vm_id)
        self.run(f"# hyperv-action: shutdown\nStop-VM -Id '{vm_id}'\n")

    def turn_off(self, vm_id: str) -> None:
        self._check_id(vm_id)
        self.run(f"# hyperv-action: turnoff\nStop-VM -Id '{vm_id}' -TurnOff\n")

    def _inventory(self, vm_id: str) -> list[dict]:
        text = self.run(_INVENTORY.replace("ONLY_ID", vm_id))
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise HypervError(f"Hyper-V inventory was not JSON: {exc}") from exc
        return rows

    def _check_id(self, vm_id: str) -> None:
        if not _GUID.match(vm_id or ""):
            raise HypervError(f"virtual machine id {vm_id!r} is not a GUID", status=404)

    def _winrm(self, script: str) -> str:
        import winrm

        scheme = "https" if self.use_https else "http"
        endpoint = f"{scheme}://{self.host}:{self.port}/wsman"
        validation = "validate" if self.verify_ssl else "ignore"
        session = winrm.Session(endpoint, auth=(self.username, self.password), transport="ntlm",
                                server_cert_validation=validation)
        try:
            result = session.run_ps(script)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if any(word in text for word in ("401", "cred", "unauthor", "logon", "forbidden")):
                raise HypervAuthError(f"Hyper-V login failed: {exc}") from exc
            raise HypervError(f"WinRM call to {self.host} failed: {exc}") from exc
        if result.status_code != 0:
            err = (_decode(result.std_err) or _decode(result.std_out)).strip()
            low = err.lower()
            if any(word in low for word in ("access is denied", "logon failure", "authentication")):
                raise HypervAuthError(err or "invalid user name or password")
            raise HypervError(err or f"PowerShell exited {result.status_code}")
        return _decode(result.std_out)
