"""REST client for Nutanix Prism: Prism Central (v3 API) or Prism Element (v2 API).

Both APIs are normalized into ``AhvVm`` records so inventory, power and export code does not
care which one answered. Disks leave the cluster as temporary images: a vDisk is cloned into a
``DISK_IMAGE``, its raw contents are downloaded over HTTPS, then the image is deleted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Optional
from urllib.parse import urlsplit

import httpx

from helper_app.redaction import redact

log = logging.getLogger(__name__)

DEFAULT_PORT = 9440
V3 = "/api/nutanix/v3"
V2 = "/PrismGateway/services/rest/v2.0"
V3_PAGE = 250
TASK_POLL_S = 2.0

ApiFlavor = Literal["pc", "pe"]


class NutanixError(RuntimeError):
    """A Prism API call failed (network, HTTP status, or a failed task)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(redact(message))
        self.status = status


class NutanixAuthError(NutanixError):
    """Authentication failure (wrong user or password, or a locked account)."""


@dataclass
class AhvDisk:
    uuid: str
    device_type: str = "DISK"  # DISK | CDROM
    adapter: str = "SCSI"  # SCSI | SATA | IDE | PCI | SPAPR
    index: int = 0
    size_bytes: int = 0
    volume_group: bool = False
    container_uuid: str = ""


@dataclass
class AhvNic:
    mac: str = ""
    subnet: str = ""
    model: str = ""
    ips: list[str] = field(default_factory=list)


@dataclass
class AhvVm:
    uuid: str
    name: str
    cluster: str = ""
    cluster_uuid: str = ""
    power_state: str = ""  # ON | OFF | PAUSED | SUSPENDED | ... (upper case)
    sockets: int = 1
    cores_per_socket: int = 1
    memory_mb: int = 1024
    boot_type: str = "LEGACY"  # LEGACY | UEFI | SECURE_BOOT
    vtpm: bool = False
    ngt_installed: bool = False
    guest_os: str = ""  # NGT guest OS string, when reported
    is_cvm: bool = False
    disks: list[AhvDisk] = field(default_factory=list)
    nics: list[AhvNic] = field(default_factory=list)


@dataclass
class TaskResult:
    status: str
    entity_uuids: list[str] = field(default_factory=list)
    error: str = ""


def parse_prism_url(raw: str) -> tuple[str, str]:
    """``(base_url, display_host)`` from the login form. The Prism port defaults to 9440."""
    text = (raw or "").strip()
    if not text:
        raise NutanixAuthError("Prism address is required")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise NutanixAuthError(f"Prism URL must be https, not {parts.scheme!r}")
    if parts.username or parts.password:
        raise NutanixAuthError("put the user name in the user field, not in the Prism URL")
    if not parts.hostname:
        raise NutanixAuthError(f"Prism URL {raw!r} has no host")
    path = (parts.path or "").rstrip("/")
    for suffix in ("/console", "/PrismGateway", "/api/nutanix/v3"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if path not in ("", "/"):
        raise NutanixAuthError("enter the Prism host (for example https://prism.example.com:9440), not a page path")
    port = parts.port or DEFAULT_PORT
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host else host
    base = f"https://{netloc}:{port}"
    display = host if port == DEFAULT_PORT else f"{host}:{port}"
    return base, display


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "").strip()[:500]
    if not isinstance(body, dict):
        return str(body)[:500]
    msgs = body.get("message_list")
    if isinstance(msgs, list) and msgs:
        parts = [str(m.get("message") or m.get("reason") or "") for m in msgs if isinstance(m, dict)]
        text = "; ".join(p for p in parts if p)
        if text:
            return text[:500]
    return str(body.get("message") or body.get("error_detail") or body.get("reason") or "").strip()[:500]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- v3 shapes
def normalize_v3_vm(entity: dict) -> AhvVm:
    meta = entity.get("metadata") or {}
    status = entity.get("status") or {}
    spec = entity.get("spec") or {}
    res = status.get("resources") or spec.get("resources") or {}
    cluster = status.get("cluster_reference") or spec.get("cluster_reference") or {}
    boot = res.get("boot_config") or {}
    boot_type = str(boot.get("boot_type") or "LEGACY").upper()
    ngt = ((res.get("guest_tools") or {}).get("nutanix_guest_tools") or {})
    vtpm = res.get("vtpm_config") or {}
    disks = []
    for disk in res.get("disk_list") or []:
        if not isinstance(disk, dict):
            continue
        props = disk.get("device_properties") or {}
        addr = props.get("disk_address") or {}
        size = _int(disk.get("disk_size_bytes")) or _int(disk.get("disk_size_mib")) * 1024 * 1024
        disks.append(AhvDisk(
            uuid=str(disk.get("uuid") or ""),
            device_type=str(props.get("device_type") or "DISK").upper(),
            adapter=str(addr.get("adapter_type") or "SCSI").upper(),
            index=_int(addr.get("device_index")),
            size_bytes=size,
            volume_group=bool(disk.get("volume_group_reference")),
            container_uuid=str(((disk.get("storage_config") or {}).get("storage_container_reference") or {})
                               .get("uuid") or ""),
        ))
    nics = []
    for nic in res.get("nic_list") or []:
        if not isinstance(nic, dict):
            continue
        ips = [str(ep.get("ip")) for ep in nic.get("ip_endpoint_list") or [] if isinstance(ep, dict) and ep.get("ip")]
        nics.append(AhvNic(
            mac=str(nic.get("mac_address") or ""),
            subnet=str((nic.get("subnet_reference") or {}).get("name") or ""),
            model=str(nic.get("model") or nic.get("nic_type") or "virtio"),
            ips=ips,
        ))
    return AhvVm(
        uuid=str(meta.get("uuid") or ""),
        name=str(status.get("name") or spec.get("name") or ""),
        cluster=str(cluster.get("name") or ""),
        cluster_uuid=str(cluster.get("uuid") or ""),
        power_state=str(res.get("power_state") or "").upper(),
        sockets=max(1, _int(res.get("num_sockets"), 1)),
        cores_per_socket=max(1, _int(res.get("num_vcpus_per_socket"), 1)),
        memory_mb=max(1, _int(res.get("memory_size_mib"), 1024)),
        boot_type=boot_type if boot_type in ("LEGACY", "UEFI", "SECURE_BOOT") else "LEGACY",
        vtpm=bool(vtpm.get("vtpm_enabled")) if isinstance(vtpm, dict) else False,
        ngt_installed=str(ngt.get("ngt_state") or "").upper() == "INSTALLED",
        guest_os=str(ngt.get("guest_os_version") or ""),
        is_cvm=bool(res.get("is_controller_vm")) or str(meta.get("kind") or "") == "cvm",
        disks=disks,
        nics=nics,
    )


# --------------------------------------------------------------------------- v2 shapes
def normalize_v2_vm(entity: dict, cluster: str = "", cluster_uuid: str = "",
                    networks: Optional[dict[str, str]] = None) -> AhvVm:
    networks = networks or {}
    boot = entity.get("boot") or {}
    if boot.get("secure_boot"):
        boot_type = "SECURE_BOOT"
    elif boot.get("uefi_boot"):
        boot_type = "UEFI"
    else:
        boot_type = "LEGACY"
    disks = []
    for disk in entity.get("vm_disk_info") or []:
        if not isinstance(disk, dict):
            continue
        addr = disk.get("disk_address") or {}
        disks.append(AhvDisk(
            uuid=str(addr.get("vmdisk_uuid") or ""),
            device_type="CDROM" if disk.get("is_cdrom") else "DISK",
            adapter=str(addr.get("device_bus") or "scsi").upper(),
            index=_int(addr.get("device_index")),
            size_bytes=_int(disk.get("size")),
            volume_group=bool(addr.get("volume_group_uuid") or disk.get("volume_group_uuid")),
            container_uuid=str(disk.get("storage_container_uuid") or ""),
        ))
    nics = []
    for nic in entity.get("vm_nics") or []:
        if not isinstance(nic, dict):
            continue
        ips = [str(ip) for ip in nic.get("ip_addresses") or [] if ip]
        if not ips and nic.get("ip_address"):
            ips = [str(nic["ip_address"])]
        nics.append(AhvNic(
            mac=str(nic.get("mac_address") or ""),
            subnet=networks.get(str(nic.get("network_uuid") or ""), ""),
            model=str(nic.get("model") or "virtio"),
            ips=ips,
        ))
    tools = entity.get("vm_features") or {}
    ngt = entity.get("nutanix_guest_tools") or {}
    return AhvVm(
        uuid=str(entity.get("uuid") or ""),
        name=str(entity.get("name") or ""),
        cluster=cluster,
        cluster_uuid=cluster_uuid,
        power_state=str(entity.get("power_state") or "").upper(),
        sockets=max(1, _int(entity.get("num_vcpus"), 1)),
        cores_per_socket=max(1, _int(entity.get("num_cores_per_vcpu"), 1)),
        memory_mb=max(1, _int(entity.get("memory_mb"), 1024)),
        boot_type=boot_type,
        vtpm=bool((entity.get("vtpm_config") or {}).get("is_enabled")) or bool(tools.get("VTPM")),
        ngt_installed=bool(ngt.get("installed_version")) or bool(ngt.get("enabled")),
        guest_os=str(entity.get("guest_os") or ngt.get("guest_os_version") or ""),
        is_cvm=bool(entity.get("is_controller_vm")),
        disks=disks,
        nics=nics,
    )


class NutanixClient:
    """Basic-auth client for one Prism endpoint. ``http`` and ``sleep`` are injectable for tests."""

    def __init__(self, base_url: str, username: str, password: str, *, verify_ssl: bool = True,
                 http: Optional[httpx.Client] = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.verify_ssl = verify_ssl
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), verify=verify_ssl,
                                         follow_redirects=True)
        self._auth = httpx.BasicAuth(username, password)
        self.sleep = sleep
        self.flavor: ApiFlavor = "pc"
        self._cluster: tuple[str, str] = ("", "")
        self._networks: Optional[dict[str, str]] = None

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    # ------------------------------------------------------------- plumbing
    def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                headers: Optional[dict[str, str]] = None, basic_auth: bool = True) -> Any:
        return self.request_with_headers(method, path, json=json, params=params, headers=headers,
                                         basic_auth=basic_auth)[0]

    def request_with_headers(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                             headers: Optional[dict[str, str]] = None,
                             basic_auth: bool = True) -> tuple[Any, httpx.Headers]:
        """``(json body, response headers)``. ``basic_auth=False`` sends only the given headers (session cookie)."""
        url = path if path.startswith("http") else self.base_url + path
        path = urlsplit(url).path
        sent = {"Accept": "application/json", **(headers or {})}
        if json is not None:
            sent["Content-Type"] = "application/json"
        try:
            resp = self.http.request(method, url, json=json, params=params, headers=sent,
                                     auth=self._auth if basic_auth else None)
        except httpx.HTTPError as exc:
            raise NutanixError(f"{method} {path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise NutanixAuthError(f"{method} {path}: HTTP {resp.status_code}: "
                                   f"{_error_text(resp) or 'invalid user name or password'}",
                                   status=resp.status_code)
        if resp.status_code >= 400:
            raise NutanixError(f"{method} {path}: HTTP {resp.status_code}: {_error_text(resp)}",
                               status=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return {}, resp.headers
        try:
            return resp.json(), resp.headers
        except Exception:  # noqa: BLE001
            return {}, resp.headers

    # ------------------------------------------------------------- login
    def detect(self) -> ApiFlavor:
        """Decide between Prism Central (v3) and Prism Element (v2). Also checks the credentials."""
        v3_error: Optional[NutanixError] = None
        try:
            data = self.request("POST", V3 + "/clusters/list", json={"kind": "cluster", "length": 500})
            for entity in (data or {}).get("entities") or []:
                services = (((entity.get("status") or {}).get("resources") or {}).get("config") or {}) \
                    .get("service_list") or []
                if "PRISM_CENTRAL" in [str(s).upper() for s in services]:
                    self.flavor = "pc"
                    return self.flavor
        except NutanixAuthError:
            raise
        except NutanixError as exc:
            v3_error = exc
        try:
            cluster = self.request("GET", V2 + "/cluster")
        except NutanixAuthError:
            raise
        except NutanixError:
            if v3_error is not None:
                raise v3_error
            self.flavor = "pc"
            return self.flavor
        self.flavor = "pe"
        self._cluster = (str(cluster.get("name") or ""), str(cluster.get("uuid") or ""))
        return self.flavor

    @property
    def is_central(self) -> bool:
        return self.flavor == "pc"

    # ------------------------------------------------------------- inventory
    def list_vms(self) -> list[AhvVm]:
        if self.is_central:
            vms: list[AhvVm] = []
            offset = 0
            while True:
                data = self.request("POST", V3 + "/vms/list",
                                    json={"kind": "vm", "length": V3_PAGE, "offset": offset})
                entities = (data or {}).get("entities") or []
                vms.extend(normalize_v3_vm(e) for e in entities if isinstance(e, dict))
                total = _int(((data or {}).get("metadata") or {}).get("total_matches"), len(vms))
                offset += len(entities)
                if not entities or offset >= total:
                    return vms
        data = self.request("GET", V2 + "/vms/", params={"include_vm_disk_config": "true",
                                                          "include_vm_nic_config": "true"})
        cluster, cluster_uuid = self._pe_cluster()
        networks = self._pe_networks()
        return [normalize_v2_vm(e, cluster, cluster_uuid, networks)
                for e in (data or {}).get("entities") or [] if isinstance(e, dict)]

    def get_vm(self, vm_uuid: str) -> AhvVm:
        if self.is_central:
            return normalize_v3_vm(self.request("GET", f"{V3}/vms/{vm_uuid}"))
        data = self.request("GET", f"{V2}/vms/{vm_uuid}", params={"include_vm_disk_config": "true",
                                                                   "include_vm_nic_config": "true"})
        cluster, cluster_uuid = self._pe_cluster()
        return normalize_v2_vm(data or {}, cluster, cluster_uuid, self._pe_networks())

    def _pe_cluster(self) -> tuple[str, str]:
        if not self._cluster[0]:
            try:
                data = self.request("GET", V2 + "/cluster")
                self._cluster = (str(data.get("name") or ""), str(data.get("uuid") or ""))
            except NutanixError as exc:
                log.info("Prism Element cluster name unavailable: %s", exc)
        return self._cluster

    def _pe_networks(self) -> dict[str, str]:
        if self._networks is None:
            try:
                data = self.request("GET", V2 + "/networks/")
                self._networks = {str(n.get("uuid") or ""): str(n.get("name") or "")
                                  for n in (data or {}).get("entities") or [] if isinstance(n, dict)}
            except NutanixError as exc:
                log.info("Prism Element network names unavailable: %s", exc)
                self._networks = {}
        return self._networks

    # ------------------------------------------------------------- power
    def set_power(self, vm_uuid: str, transition: Literal["ACPI_SHUTDOWN", "OFF"]) -> str:
        """Start a power transition; returns the task uuid (may be empty)."""
        if not self.is_central:
            data = self.request("POST", f"{V2}/vms/{vm_uuid}/set_power_state", json={"transition": transition})
            return str((data or {}).get("task_uuid") or "")
        entity = self.request("GET", f"{V3}/vms/{vm_uuid}")
        spec = dict(entity.get("spec") or {})
        resources = dict(spec.get("resources") or {})
        resources["power_state"] = "OFF"
        resources["power_state_mechanism"] = {"mechanism": "ACPI" if transition == "ACPI_SHUTDOWN" else "HARD"}
        spec["resources"] = resources
        body = {"spec": spec, "metadata": entity.get("metadata") or {},
                "api_version": entity.get("api_version") or "3.1"}
        data = self.request("PUT", f"{V3}/vms/{vm_uuid}", json=body)
        return str((((data or {}).get("status") or {}).get("execution_context") or {}).get("task_uuid") or "")

    # ------------------------------------------------------------- tasks
    def get_task(self, task_uuid: str) -> TaskResult:
        if self.is_central:
            data = self.request("GET", f"{V3}/tasks/{task_uuid}")
            entities = [str(e.get("uuid") or "") for e in data.get("entity_reference_list") or []
                        if isinstance(e, dict)]
            return TaskResult(status=str(data.get("status") or "").upper(), entity_uuids=entities,
                              error=str(data.get("error_detail") or ""))
        data = self.request("GET", f"{V2}/tasks/{task_uuid}")
        status = str(data.get("progress_status") or "").upper()
        entities = [str(e.get("entity_id") or "") for e in data.get("entity_list") or [] if isinstance(e, dict)]
        error = str((data.get("meta_response") or {}).get("error_detail") or "")
        return TaskResult(status=status, entity_uuids=entities, error=error)

    def wait_task(self, task_uuid: str, timeout_s: float, *,
                  check_cancel: Optional[Callable[[], None]] = None) -> TaskResult:
        deadline = time.monotonic() + timeout_s
        while True:
            if check_cancel is not None:
                check_cancel()
            task = self.get_task(task_uuid)
            if task.status in ("SUCCEEDED", "SUCCESS"):
                return task
            if task.status in ("FAILED", "ABORTED", "CANCELLED"):
                raise NutanixError(f"Prism task {task_uuid} {task.status.lower()}: {task.error or 'no detail'}")
            if time.monotonic() >= deadline:
                raise NutanixError(f"Prism task {task_uuid} did not finish within {int(timeout_s)} s "
                                   f"(status {task.status or 'unknown'})")
            self.sleep(TASK_POLL_S)

    # ------------------------------------------------------------- images
    def create_disk_image(self, name: str, disk: AhvDisk) -> tuple[str, str]:
        """Clone a vDisk into a temporary image: ``(image uuid or "", task uuid)``."""
        if self.is_central:
            body = {
                "spec": {
                    "name": name,
                    "description": "Temporary image created by the OCI migration tool",
                    "resources": {"image_type": "DISK_IMAGE",
                                  "data_source_reference": {"kind": "vm_disk", "uuid": disk.uuid}},
                },
                "metadata": {"kind": "image"},
                "api_version": "3.1.0",
            }
            data = self.request("POST", V3 + "/images", json=body)
            image_uuid = str(((data or {}).get("metadata") or {}).get("uuid") or "")
            task = str((((data or {}).get("status") or {}).get("execution_context") or {}).get("task_uuid") or "")
            return image_uuid, task
        clone: dict[str, Any] = {"disk_address": {"vmdisk_uuid": disk.uuid}}
        if disk.container_uuid:
            clone["storage_container_uuid"] = disk.container_uuid
        data = self.request("POST", V2 + "/images", json={
            "name": name,
            "annotation": "Temporary image created by the OCI migration tool",
            "image_type": "DISK_IMAGE",
            "vm_disk_clone_spec": clone,
        })
        return "", str((data or {}).get("task_uuid") or "")

    def find_image(self, name: str) -> str:
        if self.is_central:
            data = self.request("POST", V3 + "/images/list",
                                json={"kind": "image", "length": 50, "filter": f"name=={name}"})
            for entity in (data or {}).get("entities") or []:
                if str(((entity.get("spec") or {}).get("name")) or "") == name:
                    return str((entity.get("metadata") or {}).get("uuid") or "")
            return ""
        data = self.request("GET", V2 + "/images/")
        for entity in (data or {}).get("entities") or []:
            if str(entity.get("name") or "") == name:
                return str(entity.get("uuid") or "")
        return ""

    def image_size(self, image_uuid: str) -> int:
        if self.is_central:
            data = self.request("GET", f"{V3}/images/{image_uuid}")
            res = ((data or {}).get("status") or {}).get("resources") or {}
            return _int(res.get("size_bytes"))
        data = self.request("GET", f"{V2}/images/{image_uuid}", params={"include_vm_disk_sizes": "true"})
        return _int((data or {}).get("vm_disk_size"))

    def delete_image(self, image_uuid: str) -> str:
        """Delete an image; returns the task uuid. A missing image is not an error."""
        path = f"{V3}/images/{image_uuid}" if self.is_central else f"{V2}/images/{image_uuid}"
        try:
            data = self.request("DELETE", path)
        except NutanixError as exc:
            if exc.status == 404:
                return ""
            raise
        if self.is_central:
            return str((((data or {}).get("status") or {}).get("execution_context") or {}).get("task_uuid") or "")
        return str((data or {}).get("task_uuid") or "")

    def image_file_urls(self, image_uuid: str) -> list[str]:
        """Download URLs to try, best first. Prism Element also serves the v3 image file path."""
        urls = [f"{self.base_url}{V3}/images/{image_uuid}/file"]
        if not self.is_central:
            urls.append(f"{self.base_url}/api/nutanix/v0.8/images/{image_uuid}/file")
        return urls

    def probe_file(self, url: str) -> tuple[int, bool, str]:
        """``(size or 0, ranges supported, what the server answered)`` for an image download URL,
        without reading the body."""
        headers = {"Range": "bytes=0-511", "Accept": "application/octet-stream"}
        try:
            with self.http.stream("GET", url, headers=headers, auth=self._auth) as resp:
                if resp.status_code in (401, 403):
                    raise NutanixAuthError(f"image download: HTTP {resp.status_code}", status=resp.status_code)
                if resp.status_code >= 400:
                    resp.read()
                    raise NutanixError(f"image download {url}: HTTP {resp.status_code}: {_error_text(resp)}",
                                       status=resp.status_code)
                note = (f"HTTP {resp.status_code}, accept-ranges={resp.headers.get('accept-ranges') or '-'}, "
                        f"content-range={resp.headers.get('content-range') or '-'}")
                if resp.status_code == 206:
                    total = str(resp.headers.get("content-range") or "").rpartition("/")[2]
                    return _int(total), True, note
                return _int(resp.headers.get("content-length")), False, note
        except httpx.HTTPError as exc:
            raise NutanixError(f"image download {url}: {exc}") from exc

    def read_range(self, url: str, start: int, length: int) -> bytes:
        end = start + length - 1
        headers = {"Range": f"bytes={start}-{end}", "Accept": "application/octet-stream"}
        try:
            resp = self.http.get(url, headers=headers, auth=self._auth)
        except httpx.HTTPError as exc:
            raise NutanixError(f"image read {start}-{end}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise NutanixAuthError(f"image read {start}-{end}: HTTP {resp.status_code}", status=resp.status_code)
        if resp.status_code != 206:
            raise NutanixError(f"image read {start}-{end}: HTTP {resp.status_code}: {_error_text(resp)}",
                               status=resp.status_code)
        return resp.content

    def stream_file(self, url: str, start: int = 0, chunk_bytes: int = 1024 * 1024) -> Iterator[bytes]:
        """Yield the image bytes from ``start`` (a Range request when ``start`` > 0)."""
        headers = {"Accept": "application/octet-stream"}
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            with self.http.stream("GET", url, headers=headers, auth=self._auth) as resp:
                if resp.status_code in (401, 403):
                    raise NutanixAuthError(f"image download: HTTP {resp.status_code}", status=resp.status_code)
                if resp.status_code >= 400 or (start and resp.status_code != 206):
                    resp.read()
                    raise NutanixError(f"image download from {start}: HTTP {resp.status_code}: "
                                       f"{_error_text(resp)}", status=resp.status_code)
                yield from resp.iter_bytes(chunk_bytes)
        except httpx.HTTPError as exc:
            raise NutanixError(f"image download from {start}: {exc}") from exc
