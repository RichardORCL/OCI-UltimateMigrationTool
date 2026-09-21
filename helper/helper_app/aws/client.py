"""IAM-signed AWS client for STS, EC2 and EBS Direct APIs (httpx, no boto3)."""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx

from helper_app.aws.sigv4 import sign_headers

log = logging.getLogger(__name__)

EC2_API_VERSION = "2016-11-15"
STS_API_VERSION = "2011-06-15"


class AwsError(Exception):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class AwsAuthError(AwsError):
    """Invalid access key, secret, or signature (login-time failures)."""


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_to_value(elem: ET.Element) -> Any:
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    if all(_local_tag(c.tag) == "item" for c in children):
        return [xml_to_value(c) for c in children]
    out: dict[str, Any] = {}
    for child in children:
        key = _local_tag(child.tag)
        val = xml_to_value(child)
        if key in out:
            existing = out[key]
            out[key] = existing + [val] if isinstance(existing, list) else [existing, val]
        else:
            out[key] = val
    return out


def parse_aws_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    body = xml_to_value(root)
    return body if isinstance(body, dict) else {_local_tag(root.tag): body}


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def flatten_form(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """EC2 Query list encoding: ``Filter.1.Name``, ``VolumeId.1``, ..."""
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.extend(flatten_form(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value, 1):
                if isinstance(item, dict):
                    items.extend(flatten_form(item, f"{name}.{i}"))
                else:
                    items.append((f"{name}.{i}", str(item)))
        elif value is not None:
            items.append((name, str(value)))
    return items


class AwsClient:
    """SigV4 client for one IAM access key in one region."""

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        *,
        http: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.access_key_id = access_key_id.strip()
        self._secret = secret_access_key
        self.region = region.strip()
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._own_http:
            self.http.close()

    # ----------------------------------------------------------------- HTTP
    def _signed_request(self, method: str, url: str, *, service: str, body: bytes = b"",
                        headers: Optional[dict[str, str]] = None,
                        extra: Optional[dict[str, str]] = None) -> httpx.Response:
        hdrs = sign_headers(method, url, body, access_key_id=self.access_key_id,
                            secret_access_key=self._secret, region=self.region, service=service,
                            extra_headers=extra)
        if headers:
            hdrs.update(headers)
        try:
            return self.http.request(method, url, content=body or None, headers=hdrs)
        except httpx.HTTPError as exc:
            raise AwsError(f"cannot reach {url}: {exc}") from exc

    def _raise_xml(self, resp: httpx.Response, service: str) -> None:
        text = resp.text[:2000]
        code = ""
        message = text
        try:
            parsed = parse_aws_xml(resp.text)
            err = parsed.get("Error") or parsed.get("Errors") or parsed
            if isinstance(err, list):
                err = err[0] if err else {}
            if isinstance(err, dict):
                inner = err.get("Error") if isinstance(err.get("Error"), dict) else err
                code = str(inner.get("Code") or "")
                message = str(inner.get("Message") or inner.get("message") or text)
        except ET.ParseError:
            pass
        authish = resp.status_code in (401, 403) or code in (
            "AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch", "UnauthorizedOperation",
            "AccessDenied", "ExpiredToken",
        )
        cls = AwsAuthError if authish else AwsError
        raise cls(f"{service} {code or resp.status_code}: {message}", status=resp.status_code, code=code)

    def _query(self, host: str, service: str, action: str, version: str,
               params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload = dict(params or {})
        payload["Action"] = action
        payload["Version"] = version
        body = urlencode(flatten_form(payload)).encode("utf-8")
        url = f"https://{host}/"
        resp = self._signed_request(
            "POST", url, service=service, body=body,
            extra={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        if resp.status_code != 200:
            self._raise_xml(resp, service)
        return parse_aws_xml(resp.text)

    def _ec2(self, action: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._query(f"ec2.{self.region}.amazonaws.com", "ec2", action, EC2_API_VERSION, params)

    def _sts(self, action: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._query(f"sts.{self.region}.amazonaws.com", "sts", action, STS_API_VERSION, params)

    # ----------------------------------------------------------------- STS / identity
    def get_caller_identity(self) -> dict[str, str]:
        body = self._sts("GetCallerIdentity")
        result = body.get("GetCallerIdentityResult") or body
        account = str(result.get("Account") or "")
        if not account:
            raise AwsAuthError("STS GetCallerIdentity returned no account id")
        return {
            "account": account,
            "arn": str(result.get("Arn") or ""),
            "user_id": str(result.get("UserId") or ""),
        }

    # ----------------------------------------------------------------- EC2 inventory
    def describe_instances(self, instance_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if instance_ids:
            params["InstanceId"] = instance_ids
        rows: list[dict[str, Any]] = []
        token = None
        while True:
            if token:
                params["NextToken"] = token
            body = self._ec2("DescribeInstances", params)
            reservations = _as_list((body.get("reservationSet") or {}).get("item") if isinstance(
                body.get("reservationSet"), dict) else body.get("reservationSet"))
            # parser already expands item lists; reservationSet may be a list or a dict with item
            if isinstance(body.get("reservationSet"), list):
                reservations = body["reservationSet"]
            elif isinstance(body.get("reservationSet"), dict):
                reservations = _as_list(body["reservationSet"].get("item"))
            for res in reservations:
                if not isinstance(res, dict):
                    continue
                inst_set = res.get("instancesSet")
                instances = inst_set if isinstance(inst_set, list) else _as_list(
                    (inst_set or {}).get("item") if isinstance(inst_set, dict) else inst_set)
                for inst in instances:
                    if isinstance(inst, dict) and inst.get("instanceId"):
                        rows.append(inst)
            token = body.get("nextToken") or ""
            if not token:
                break
            params = dict(params)
        return rows

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        found = self.describe_instances([instance_id])
        if not found:
            raise AwsError(f"EC2 instance not found: {instance_id}", status=404, code="InvalidInstanceID.NotFound")
        return found[0]

    def describe_volumes(self, volume_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not volume_ids:
            return {}
        body = self._ec2("DescribeVolumes", {"VolumeId": volume_ids})
        items = body.get("volumeSet")
        vols = items if isinstance(items, list) else _as_list(
            (items or {}).get("item") if isinstance(items, dict) else items)
        return {str(v.get("volumeId")): v for v in vols if isinstance(v, dict) and v.get("volumeId")}

    def describe_instance_types(self, types: list[str]) -> dict[str, dict[str, Any]]:
        if not types:
            return {}
        try:
            body = self._ec2("DescribeInstanceTypes", {"InstanceType": types})
        except AwsError as exc:
            log.warning("DescribeInstanceTypes failed: %s", exc)
            return {}
        items = body.get("instanceTypeSet") or body.get("instanceTypeInfo")
        rows = items if isinstance(items, list) else _as_list(
            (items or {}).get("item") if isinstance(items, dict) else items)
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("instanceType"):
                out[str(row["instanceType"])] = row
        return out

    def describe_images(self, image_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [i for i in image_ids if i]
        if not ids:
            return {}
        try:
            body = self._ec2("DescribeImages", {"ImageId": ids})
        except AwsError as exc:
            log.warning("DescribeImages failed: %s", exc)
            return {}
        items = body.get("imagesSet")
        rows = items if isinstance(items, list) else _as_list(
            (items or {}).get("item") if isinstance(items, dict) else items)
        return {str(r.get("imageId")): r for r in rows if isinstance(r, dict) and r.get("imageId")}

    # ----------------------------------------------------------------- power / snapshot
    def stop_instance(self, instance_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]] = None) -> None:
        self._ec2("StopInstances", {"InstanceId": [instance_id]})
        self._wait_instance(instance_id, "stopped", timeout_s, on_wait)

    def _wait_instance(self, instance_id: str, want: str, timeout_s: float,
                       on_wait: Optional[Callable[[], None]] = None) -> None:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            if on_wait:
                on_wait()
            inst = self.get_instance(instance_id)
            name = _instance_state(inst)
            if name == want:
                return
            if name == "terminated":
                raise AwsError(f"instance {instance_id} was terminated while waiting to be {want}")
            self._sleep(2)
        raise AwsError(f"instance {instance_id} did not reach {want} within {int(timeout_s)} s")

    def create_snapshot(self, volume_id: str, description: str, timeout_s: float,
                        tags: Optional[dict[str, str]] = None,
                        on_wait: Optional[Callable[[], None]] = None) -> str:
        params: dict[str, Any] = {"VolumeId": volume_id, "Description": description}
        if tags:
            params["TagSpecification"] = [{
                "ResourceType": "snapshot",
                "Tag": [{"Key": k, "Value": v} for k, v in tags.items()],
            }]
        body = self._ec2("CreateSnapshot", params)
        snap_id = str(body.get("snapshotId") or "")
        if not snap_id:
            raise AwsError(f"CreateSnapshot of {volume_id} returned no snapshot id")
        self._wait_snapshot(snap_id, timeout_s, on_wait)
        return snap_id

    def _wait_snapshot(self, snapshot_id: str, timeout_s: float,
                       on_wait: Optional[Callable[[], None]] = None) -> None:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            if on_wait:
                on_wait()
            body = self._ec2("DescribeSnapshots", {"SnapshotId": [snapshot_id]})
            items = body.get("snapshotSet")
            rows = items if isinstance(items, list) else _as_list(
                (items or {}).get("item") if isinstance(items, dict) else items)
            status = str((rows[0] if rows else {}).get("status") or "")
            if status == "completed":
                return
            if status == "error":
                raise AwsError(f"snapshot {snapshot_id} entered error state")
            self._sleep(2)
        raise AwsError(f"snapshot {snapshot_id} did not finish within {int(timeout_s)} s")

    def delete_snapshot(self, snapshot_id: str) -> None:
        try:
            self._ec2("DeleteSnapshot", {"SnapshotId": snapshot_id})
        except AwsError as exc:
            if exc.status == 404 or exc.code in ("InvalidSnapshot.NotFound",):
                return
            raise

    # ----------------------------------------------------------------- EBS Direct
    def list_snapshot_blocks(self, snapshot_id: str) -> tuple[int, list[tuple[int, str]]]:
        """Return ``(block_size, [(block_index, block_token), ...])`` for allocated snapshot blocks."""
        blocks: list[tuple[int, str]] = []
        block_size = 512 * 1024
        token = None
        while True:
            qs: dict[str, str] = {"maxResults": "10000"}
            if token:
                qs["pageToken"] = token
            url = (f"https://ebs.{self.region}.amazonaws.com/snapshots/{snapshot_id}/blocks"
                   f"?{urlencode(qs)}")
            resp = self._signed_request("GET", url, service="ebs")
            if resp.status_code != 200:
                self._raise_json(resp, "ebs")
            body = resp.json()
            block_size = int(body.get("BlockSize") or block_size)
            for b in body.get("Blocks") or []:
                blocks.append((int(b["BlockIndex"]), str(b["BlockToken"])))
            token = body.get("NextToken")
            if not token:
                break
        return block_size, blocks

    def get_snapshot_block(self, snapshot_id: str, block_index: int, block_token: str) -> bytes:
        url = (f"https://ebs.{self.region}.amazonaws.com/snapshots/{snapshot_id}/blocks/{block_index}"
               f"?blockToken={block_token}")
        resp = self._signed_request("GET", url, service="ebs")
        if resp.status_code != 200:
            self._raise_json(resp, "ebs")
        return resp.content

    def _raise_json(self, resp: httpx.Response, service: str) -> None:
        code = ""
        message = resp.text[:500]
        try:
            body = resp.json()
            code = str(body.get("Code") or body.get("code") or body.get("__type") or "")
            message = str(body.get("Message") or body.get("message") or message)
        except Exception:  # noqa: BLE001
            pass
        cls = AwsAuthError if resp.status_code in (401, 403) else AwsError
        raise cls(f"{service} {code or resp.status_code}: {message}", status=resp.status_code, code=code)


def _instance_state(inst: dict) -> str:
    state = inst.get("instanceState") or inst.get("state") or {}
    if isinstance(state, dict):
        return str(state.get("name") or "").lower()
    return str(state).lower()
