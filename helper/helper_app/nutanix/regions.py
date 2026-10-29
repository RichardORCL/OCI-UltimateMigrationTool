"""Find the regions of AHV vDisks that hold data, so the copy can skip unallocated and zeroed space.

The temporary image download is raw and full size. Prism can describe which ranges of a disk
snapshot are ``REGULAR`` (data) and which are ``ZEROED`` or absent:

- Prism Central 2024.3+ (v4 data protection API): a crash-consistent recovery point of the VM,
  then ``compute-changed-regions`` per disk recovery point with no reference, sent to the cluster
  that ``discover-cluster`` names.
- Prism Element (v3 ``data/changed_regions``): only protection-domain snapshot paths are accepted,
  so a temporary protection domain protects the VM, takes one out-of-band snapshot, and is removed.

The VM is powered off before the map is taken, so the map also describes the image clone. Every
recovery point and protection domain is recorded on ``NutanixSourceInfo`` before it is created and
removed afterwards; a crashed job leaves it for ``release_region_resources``. Any failure only means
the disks are read in full.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from helper_app.branding import PREFIX
from helper_app.models import NutanixSourceInfo
from helper_app.nutanix.client import V2, V3, NutanixAuthError, NutanixClient, NutanixError

log = logging.getLogger(__name__)

Extent = tuple[int, int]

DP_VERSIONS = ("v4.0", "v4.1", "v4.2", "v4.3")
V4_TASKS = "/api/prism/v4.0/config/tasks"
BLOCK_BYTES = 256 * 1024  # largest block size compute-changed-regions accepts: fewer, larger regions
POLL_S = 2.0
RECOVERY_POINT_TTL = timedelta(days=1)  # Prism expires a recovery point left behind by a crash
_BUSY = {"SCHEDULED", "RUNNING", "IN_PROGRESS", "PENDING", "QUEUED"}
_NAME = re.compile(r"[^A-Za-z0-9_\-]")


class RegionsUnavailable(NutanixError):
    """Prism did not provide a region map; the caller reads the full disks."""


def region_name(job_id: str) -> str:
    return _NAME.sub("-", f"{PREFIX}-{job_id[:8]}-regions")[:75]


def _request_id() -> dict[str, str]:
    return {"NTNX-Request-Id": str(uuid.uuid4())}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def merge_extents(extents: list[Extent], size: int, gap: int) -> list[Extent]:
    """Sort, clip to ``size`` and join extents that are at most ``gap`` bytes apart."""
    merged: list[list[int]] = []
    for off, length in sorted(extents):
        end = min(off + length, size) if size else off + length
        off = max(0, off)
        if end <= off:
            continue
        if merged and off - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([off, end])
    return [(a, b - a) for a, b in merged]


class RegionMapper:
    """``map(vm_uuid, disk_ids)`` -> data extents per vDisk uuid. Raises ``RegionsUnavailable``."""

    def __init__(self, client: NutanixClient, job_id: str, info: NutanixSourceInfo, *, timeout_s: float,
                 save: Callable[[], None], check_cancel: Optional[Callable[[], None]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.client = client
        self.job_id = job_id
        self.info = info
        self.timeout_s = timeout_s
        self._save = save
        self._check = check_cancel or (lambda: None)
        self._sleep = sleep or client.sleep

    def map(self, vm_uuid: str, disk_ids: list[str]) -> dict[str, list[Extent]]:
        try:
            if self.client.is_central:
                return self._map_recovery_point(vm_uuid, disk_ids)
            return self._map_protection_domain(vm_uuid, disk_ids)
        except (RegionsUnavailable, NutanixAuthError):
            raise
        except NutanixError as exc:
            raise RegionsUnavailable(str(exc), status=exc.status) from exc

    # ----------------------------------------------------------------- Prism Central v4
    def _map_recovery_point(self, vm_uuid: str, disk_ids: list[str]) -> dict[str, list[Extent]]:
        expires = (datetime.now(timezone.utc) + RECOVERY_POINT_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"name": region_name(self.job_id), "recoveryPointType": "CRASH_CONSISTENT",
                "expirationTime": expires, "vmRecoveryPoints": [{"vmExtId": vm_uuid}]}
        try:
            data = self.client.request("POST", "/api/dataprotection/v4.0/config/recovery-points", json=body,
                                       headers=_request_id())
        except NutanixError as exc:
            if exc.status in (404, 405, 501):
                raise RegionsUnavailable(f"this Prism Central has no v4 recovery point API "
                                         f"(needs pc.2024.3 or later): {exc}", status=exc.status) from exc
            raise
        task = str(((data or {}).get("data") or {}).get("extId") or "")
        if not task:
            raise RegionsUnavailable("Prism Central did not return a task for the recovery point")
        affected = self._wait_v4_task(task)
        rp_id = next((str(e.get("extId") or "") for e in affected
                      if str(e.get("rel") or "").endswith(":recovery-point") and e.get("extId")), "")
        if not rp_id:
            rp_id = next((str(e.get("extId") or "") for e in affected
                          if e.get("extId") and str(e.get("extId")) != vm_uuid), "")
        if not rp_id:
            raise RegionsUnavailable("the recovery point task did not name the recovery point")
        self._record(self.info.recovery_point_ids, rp_id)
        try:
            rp = (self.client.request("GET", f"/api/dataprotection/v4.0/config/recovery-points/{rp_id}")
                  or {}).get("data") or {}
            vm_rp = next((v for v in rp.get("vmRecoveryPoints") or []
                          if str(v.get("vmExtId") or "") == vm_uuid), None)
            if vm_rp is None:
                raise RegionsUnavailable(f"recovery point {rp_id} has no recovery point of VM {vm_uuid}")
            by_disk = {str(d.get("diskExtId") or ""): str(d.get("diskRecoveryPointExtId") or "")
                       for d in vm_rp.get("diskRecoveryPoints") or []}
            result: dict[str, list[Extent]] = {}
            for disk_id in disk_ids:
                disk_rp = by_disk.get(disk_id)
                if not disk_rp:
                    log.info("job %s: recovery point %s has no disk recovery point for vDisk %s",
                             self.job_id, rp_id, disk_id)
                    continue
                self._check()
                result[disk_id] = list(self._v4_regions(rp_id, str(vm_rp.get("extId") or ""), disk_rp))
            if not result:
                raise RegionsUnavailable("the recovery point lists none of the VM's disks")
            return result
        finally:
            self._delete_recovery_point(rp_id)

    def _wait_v4_task(self, task: str) -> list[dict]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            self._check()
            data = (self.client.request("GET", f"{V4_TASKS}/{task}") or {}).get("data") or {}
            status = str(data.get("status") or "").upper()
            if status == "SUCCEEDED":
                return [e for e in data.get("entitiesAffected") or [] if isinstance(e, dict)]
            if status in ("FAILED", "CANCELED", "CANCELLED"):
                errors = "; ".join(str(m.get("message") or "") for m in data.get("errorMessages") or []
                                   if isinstance(m, dict))
                raise NutanixError(f"Prism task {task} {status.lower()}: {errors or 'no detail'}")
            if time.monotonic() >= deadline:
                raise NutanixError(f"Prism task {task} did not finish within {int(self.timeout_s)} s")
            self._sleep(POLL_S)

    def _discover(self, rp_id: str, vm_rp: str, disk_rp: str) -> tuple[str, str, str]:
        """``(api version, cluster base URL or "", session token or "")`` for compute-changed-regions."""
        body = {"operation": "COMPUTE_CHANGED_REGIONS", "spec": {
            "$objectType": "dataprotection.v4.content.ComputeChangedRegionsClusterDiscoverSpec",
            "diskRecoveryPoint": {"$objectType": "dataprotection.v4.content.VmDiskRecoveryPointReference",
                                  "recoveryPointExtId": rp_id, "vmRecoveryPointExtId": vm_rp,
                                  "diskRecoveryPointExtId": disk_rp}}}
        for version in DP_VERSIONS:
            try:
                data = self.client.request(
                    "POST", f"/api/dataprotection/{version}/config/recovery-points/{rp_id}/$actions/discover-cluster",
                    json=body, headers=_request_id())
            except NutanixAuthError:
                raise
            except NutanixError as exc:
                if exc.status in (404, 405, 501):
                    continue
                log.info("job %s: discover-cluster failed (%s); asking Prism Central directly", self.job_id, exc)
                return version, "", ""
            found = (data or {}).get("data") or {}
            ip = (((found.get("clusterIp") or {}).get("ipv4") or {}).get("value")
                  or ((found.get("clusterIp") or {}).get("ipv6") or {}).get("value") or "")
            token = str(found.get("jwtToken") or "")
            if ip and token:
                host = f"[{ip}]" if ":" in str(ip) else str(ip)
                return version, f"https://{host}:9440", token
            return version, "", ""
        return DP_VERSIONS[0], "", ""

    def _v4_regions(self, rp_id: str, vm_rp: str, disk_rp: str) -> Iterator[Extent]:
        version, cluster, token = self._discover(rp_id, vm_rp, disk_rp)
        path = (f"/api/dataprotection/{version}/content/recovery-points/{rp_id}/vm-recovery-points/{vm_rp}"
                f"/disk-recovery-points/{disk_rp}/$actions/compute-changed-regions")
        offset = 0
        while True:
            self._check()
            body = {"offset": offset, "blockSizeByte": BLOCK_BYTES}
            if cluster:
                try:
                    data = self.client.request("POST", cluster + path, json=body, basic_auth=False,
                                               headers={**_request_id(), "Cookie": f"NTNX_IGW_SESSION={token}"})
                except NutanixError as exc:
                    log.info("job %s: compute-changed-regions on %s failed (%s); asking Prism Central",
                             self.job_id, cluster, exc)
                    cluster = ""
                    continue
            else:
                data = self.client.request("POST", path, json=body, headers=_request_id())
            regions = [r for r in (data or {}).get("data") or [] if isinstance(r, dict)]
            end = offset
            for region in regions:
                off, length = _int(region.get("offset")), _int(region.get("length"))
                end = max(end, off + length)
                if length > 0 and str(region.get("regionType") or "").upper() == "REGULAR":
                    yield off, length
            if end <= offset:
                return
            offset = end

    def _delete_recovery_point(self, rp_id: str) -> None:
        try:
            delete_recovery_point(self.client, rp_id)
        except NutanixError as exc:
            log.warning("job %s: could not delete recovery point %s: %s", self.job_id, rp_id, exc)
            return
        self._forget(self.info.recovery_point_ids, rp_id)

    # ----------------------------------------------------------------- Prism Element v2/v3
    def _map_protection_domain(self, vm_uuid: str, disk_ids: list[str]) -> dict[str, list[Extent]]:
        cluster = self.client.request("GET", V2 + "/cluster") or {}
        cluster_id = str(cluster.get("id") or "").rpartition("::")[2]
        incarnation = str(cluster.get("cluster_incarnation_id") or "")
        name = region_name(self.job_id)
        release_protection_domain(self.client, name)
        self._record(self.info.protection_domains, name)
        try:
            self.client.request("POST", V2 + "/protection_domains/", json={"value": name})
            protected = self.client.request("POST", f"{V2}/protection_domains/{name}/protect_vms",
                                            json={"uuids": [vm_uuid]})
            if isinstance(protected, list) and not protected:
                raise RegionsUnavailable("the VM could not be added to a protection domain "
                                         "(it may already be protected by another one)")
            self.client.request("POST", f"{V2}/protection_domains/{name}/oob_schedules",
                                json={"app_consistent": False})
            snapshot = self._wait_pd_snapshot(name)
            snapshot_id = str(snapshot.get("snapshot_id") or "").rpartition(":")[2]
            containers: dict[str, str] = {}
            disk_containers = {d: self.info.container_uuids[i] for i, d in enumerate(self.info.disk_ids)
                               if i < len(self.info.container_uuids)}
            result: dict[str, list[Extent]] = {}
            for disk_id in disk_ids:
                self._check()
                container_uuid = disk_containers.get(disk_id, "")
                if container_uuid and container_uuid not in containers:
                    data = self.client.request("GET", f"{V2}/storage_containers/{container_uuid}") or {}
                    containers[container_uuid] = str(data.get("name") or "")
                paths = snapshot_paths(snapshot, disk_id, containers.get(container_uuid, ""),
                                       cluster_id, incarnation, snapshot_id)
                result[disk_id] = self._v3_regions(paths)
            return result
        finally:
            actions = release_protection_domain(self.client, name)
            if not any(a.startswith("failed") for a in actions):
                self._forget(self.info.protection_domains, name)
            else:
                log.warning("job %s: protection domain %s not fully removed: %s", self.job_id, name, actions)

    def _wait_pd_snapshot(self, name: str) -> dict:
        deadline = time.monotonic() + self.timeout_s
        while True:
            self._check()
            data = self.client.request("GET", f"{V2}/protection_domains/{name}/dr_snapshots/") or {}
            snaps = [s for s in data.get("entities") or [] if isinstance(s, dict) and s.get("snapshot_id")]
            ready = [s for s in snaps if str(s.get("state") or "").upper() not in _BUSY]
            if ready:
                return ready[0]
            if time.monotonic() >= deadline:
                raise RegionsUnavailable(f"the snapshot of protection domain {name} did not appear within "
                                         f"{int(self.timeout_s)} s")
            self._sleep(POLL_S)

    def _v3_regions(self, paths: list[str]) -> list[Extent]:
        last: Optional[NutanixError] = None
        for path in paths:
            try:
                return list(self._v3_path_regions(path))
            except NutanixAuthError:
                raise
            except NutanixError as exc:
                if exc.status is None or exc.status >= 500:
                    raise
                last = exc
        raise RegionsUnavailable(f"changed_regions accepted none of the snapshot paths {paths}: {last}")

    def _v3_path_regions(self, path: str) -> Iterator[Extent]:
        offset = 0
        while True:
            self._check()
            data = self.client.request("POST", V3 + "/data/changed_regions", json={
                "snapshot_file_path": path, "reference_snapshot_file_path": "", "start_offset": offset}) or {}
            for region in data.get("region_list") or []:
                off, length = _int(region.get("offset")), _int(region.get("length"))
                if length > 0 and str(region.get("type") or region.get("region_type") or "").upper() == "REGULAR":
                    yield off, length
            nxt, size = _int(data.get("next_offset")), _int(data.get("file_size"))
            if not nxt or nxt <= offset or (size and nxt >= size):
                return
            offset = nxt

    # ----------------------------------------------------------------- bookkeeping
    def _record(self, values: list[str], value: str) -> None:
        if value not in values:
            values.append(value)
            self._save()

    def _forget(self, values: list[str], value: str) -> None:
        if value in values:
            values.remove(value)
            self._save()


def snapshot_paths(snapshot: dict, disk_id: str, container: str, cluster_id: str, incarnation: str,
                   snapshot_id: str) -> list[str]:
    """Candidate ``snapshot_file_path`` values for one vDisk of a protection-domain snapshot.

    A path the snapshot record lists is used first; otherwise the layout backup products use:
    ``/<container>/.snapshot/<id % 100>/<cluster id>-<incarnation>-<id>/.acropolis/vmdisk/<vdisk>``.
    """
    paths: list[str] = []
    suffix = f"/.acropolis/vmdisk/{disk_id}"
    for text in _strings(snapshot):
        if text.endswith(suffix) and "/.snapshot/" in text and text not in paths:
            paths.append(text)
    if container and cluster_id and incarnation and snapshot_id.isdigit():
        tail = f"{cluster_id}-{incarnation}-{snapshot_id}{suffix}"
        for bucket in (f"{int(snapshot_id) % 100:02d}", str(int(snapshot_id) % 100)):
            candidate = f"/{container}/.snapshot/{bucket}/{tail}"
            if candidate not in paths:
                paths.append(candidate)
    if not paths:
        raise RegionsUnavailable(f"no snapshot file path is known for vDisk {disk_id}")
    return paths


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def delete_recovery_point(client: NutanixClient, rp_id: str) -> None:
    """Delete a v4 recovery point; a missing one is not an error."""
    path = f"/api/dataprotection/v4.0/config/recovery-points/{rp_id}"
    try:
        _, headers = client.request_with_headers("GET", path)
    except NutanixError as exc:
        if exc.status == 404:
            return
        raise
    sent = _request_id()
    if headers.get("etag"):
        sent["If-Match"] = headers["etag"]
    try:
        client.request("DELETE", path, headers=sent)
    except NutanixError as exc:
        if exc.status != 404:
            raise


def release_protection_domain(client: NutanixClient, name: str) -> list[str]:
    """Remove a protection domain this tool created: its snapshots, its VMs, then the domain itself."""
    base = f"{V2}/protection_domains/{name}"
    try:
        pd = client.request("GET", base) or {}
    except NutanixError as exc:
        if exc.status == 404:
            return []
        return [f"failed: read protection domain {name}: {exc}"]
    actions: list[str] = []
    try:
        snaps = (client.request("GET", base + "/dr_snapshots/") or {}).get("entities") or []
    except NutanixError as exc:
        snaps = []
        actions.append(f"failed: list snapshots of protection domain {name}: {exc}")
    for snap in snaps:
        sid = str(snap.get("snapshot_id") or "")
        if not sid:
            continue
        try:
            client.request("DELETE", f"{base}/dr_snapshots/{sid}")
            actions.append(f"ok: deleted snapshot {sid} of protection domain {name}")
        except NutanixError as exc:
            if exc.status != 404:
                actions.append(f"failed: delete snapshot {sid} of protection domain {name}: {exc}")
    vms = [str(v.get("vm_name") or "") for v in pd.get("vms") or [] if isinstance(v, dict) and v.get("vm_name")]
    if vms:
        try:
            client.request("POST", base + "/unprotect_vms", json=vms)
        except NutanixError as exc:
            actions.append(f"failed: unprotect {', '.join(vms)} in protection domain {name}: {exc}")
    try:
        client.request("DELETE", base)
        actions.append(f"ok: deleted protection domain {name}")
    except NutanixError as exc:
        if exc.status != 404:
            actions.append(f"failed: delete protection domain {name}: {exc}")
    return actions


def release_region_resources(client: NutanixClient, info: NutanixSourceInfo) -> list[str]:
    """Delete recovery points and protection domains recorded on ``info``. Best effort."""
    actions: list[str] = []
    for rp_id in list(info.recovery_point_ids):
        try:
            delete_recovery_point(client, rp_id)
            info.recovery_point_ids.remove(rp_id)
            actions.append(f"ok: deleted recovery point {rp_id}")
        except NutanixError as exc:
            actions.append(f"failed: delete recovery point {rp_id}: {exc}")
    for name in list(info.protection_domains):
        result = release_protection_domain(client, name)
        actions.extend(result)
        if not any(a.startswith("failed") for a in result):
            info.protection_domains.remove(name)
    return actions
