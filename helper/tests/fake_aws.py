"""Fake AWS: STS, EC2 Query and EBS Direct APIs served through ``httpx.MockTransport``."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import httpx

from helper_app.aws.client import AwsClient
from helper_app.aws.session import AwsConnector

ACCESS_KEY = "AKIATESTKEY12EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
REGION = "eu-west-1"
ACCOUNT = "123456789012"
BLOCK = 512 * 1024


def _xml(tag: str, inner: str) -> str:
    return f'<?xml version="1.0"?>\n<{tag} xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">{inner}</{tag}>'


def _item(fields: dict[str, str]) -> str:
    parts = []
    for k, v in fields.items():
        parts.append(f"<{k}>{escape(str(v))}</{k}>")
    return "<item>" + "".join(parts) + "</item>"


@dataclass
class FakeVolume:
    volume_id: str
    data: bytes
    encrypted: bool = False
    size_gb: int = 1


@dataclass
class FakeSnapshot:
    snapshot_id: str
    volume_id: str
    data: bytes
    status: str = "completed"


@dataclass
class FakeImage:
    image_id: str
    name: str
    description: str = ""
    platform: str = ""


@dataclass
class FakeInstance:
    instance_id: str
    name: str
    instance_type: str = "t3.medium"
    state: str = "running"
    root_device_type: str = "ebs"
    root_device_name: str = "/dev/sda1"
    image_id: str = "ami-ubuntu"
    platform: str = ""
    platform_details: str = "Linux/UNIX"
    boot_mode: str = "uefi"
    vpc_id: str = "vpc-aaa"
    az: str = "eu-west-1a"
    volumes: list[FakeVolume] = field(default_factory=list)
    product_codes: list[str] = field(default_factory=list)
    ops: list[str] = field(default_factory=list)

    def arn(self) -> str:
        return f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{self.instance_id}"


class FakeAws:
    def __init__(self, instances: list[FakeInstance], images: Optional[dict[str, FakeImage]] = None):
        self.instances = {i.instance_id: i for i in instances}
        self.volumes = {v.volume_id: v for i in instances for v in i.volumes}
        self.snapshots: dict[str, FakeSnapshot] = {}
        self.images = images or {}
        self.snap_seq = 0
        self.requests: list[str] = []
        self.valid_key = ACCESS_KEY
        self.valid_secret = SECRET
        self.account = ACCOUNT

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=30.0)

    def client_factory(self, key: str, secret: str, region: str) -> AwsClient:
        return AwsClient(key, secret, region, http=self.http())

    def connector(self, settings) -> AwsConnector:
        return AwsConnector(settings, client_factory=self.client_factory)

    def _auth_key(self, request: httpx.Request) -> str:
        auth = request.headers.get("authorization") or ""
        if "Credential=" not in auth:
            return ""
        cred = auth.split("Credential=", 1)[1].split(",", 1)[0]
        return cred.split("/", 1)[0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        path = request.url.path or "/"
        self.requests.append(f"{request.method} {host}{path}")
        key = self._auth_key(request)
        if key and key != self.valid_key:
            return httpx.Response(403, text='<Error><Code>InvalidClientTokenId</Code>'
                                            '<Message>The security token included in the request is invalid.</Message></Error>')
        if host.startswith("sts."):
            return self._sts(request)
        if host.startswith("ebs."):
            return self._ebs(request, path)
        if host.startswith("ec2."):
            return self._ec2(request)
        return httpx.Response(404, text="unknown host")

    def _form(self, request: httpx.Request) -> dict[str, list[str]]:
        return parse_qs(request.content.decode("utf-8") if request.content else "")

    def _sts(self, request: httpx.Request) -> httpx.Response:
        form = self._form(request)
        action = (form.get("Action") or [""])[0]
        if action != "GetCallerIdentity":
            return httpx.Response(400, text=f"<Error><Code>Unknown</Code><Message>{action}</Message></Error>")
        body = (
            "<GetCallerIdentityResponse>"
            "<GetCallerIdentityResult>"
            f"<Account>{self.account}</Account>"
            f"<Arn>arn:aws:iam::{self.account}:user/oci-umt</Arn>"
            "<UserId>AIDAEXAMPLE</UserId>"
            "</GetCallerIdentityResult>"
            "</GetCallerIdentityResponse>"
        )
        return httpx.Response(200, text=body)

    def _ec2(self, request: httpx.Request) -> httpx.Response:
        form = self._form(request)
        action = (form.get("Action") or [""])[0]
        if action == "DescribeInstances":
            ids = [v for k, vs in form.items() if k.startswith("InstanceId") for v in vs]
            items = []
            for inst in self.instances.values():
                if ids and inst.instance_id not in ids:
                    continue
                items.append(self._instance_xml(inst))
            inner = "<reservationSet>" + "".join(
                f"<item><instancesSet>{it}</instancesSet></item>" for it in items) + "</reservationSet>"
            return httpx.Response(200, text=_xml("DescribeInstancesResponse", inner))
        if action == "DescribeVolumes":
            ids = [v for k, vs in form.items() if k.startswith("VolumeId") for v in vs]
            items = []
            for vid in ids or list(self.volumes):
                vol = self.volumes.get(vid)
                if not vol:
                    continue
                items.append(_item({"volumeId": vol.volume_id, "size": str(vol.size_gb),
                                    "encrypted": "true" if vol.encrypted else "false"}))
            return httpx.Response(200, text=_xml("DescribeVolumesResponse",
                                                 "<volumeSet>" + "".join(items) + "</volumeSet>"))
        if action == "DescribeInstanceTypes":
            types = [v for k, vs in form.items() if k.startswith("InstanceType") for v in vs]
            items = []
            catalog = {
                "t3.medium": (2, 4096),
                "t3.large": (2, 8192),
                "m5.xlarge": (4, 16384),
            }
            for t in types or catalog:
                vcpu, mem = catalog.get(t, (2, 4096))
                items.append(
                    f"<item><instanceType>{t}</instanceType>"
                    f"<vCpuInfo><defaultVCpus>{vcpu}</defaultVCpus></vCpuInfo>"
                    f"<memoryInfo><sizeInMiB>{mem}</sizeInMiB></memoryInfo></item>"
                )
            return httpx.Response(200, text=_xml("DescribeInstanceTypesResponse",
                                                 "<instanceTypeSet>" + "".join(items) + "</instanceTypeSet>"))
        if action == "DescribeImages":
            ids = [v for k, vs in form.items() if k.startswith("ImageId") for v in vs]
            items = []
            for iid in ids:
                img = self.images.get(iid)
                if not img:
                    continue
                items.append(_item({"imageId": img.image_id, "name": img.name, "description": img.description,
                                    "platform": img.platform}))
            return httpx.Response(200, text=_xml("DescribeImagesResponse",
                                                 "<imagesSet>" + "".join(items) + "</imagesSet>"))
        if action == "StopInstances":
            iid = (form.get("InstanceId.1") or [""])[0]
            inst = self.instances.get(iid)
            if not inst:
                return httpx.Response(400, text="<Error><Code>InvalidInstanceID.NotFound</Code><Message>nope</Message></Error>")
            inst.state = "stopped"
            inst.ops.append("stop")
            return httpx.Response(200, text=_xml("StopInstancesResponse",
                                                 f"<instancesSet>{self._instance_xml(inst)}</instancesSet>"))
        if action == "CreateSnapshot":
            vid = (form.get("VolumeId") or [""])[0]
            vol = self.volumes.get(vid)
            if not vol:
                return httpx.Response(400, text="<Error><Code>InvalidVolume.NotFound</Code><Message>nope</Message></Error>")
            self.snap_seq += 1
            sid = f"snap-{self.snap_seq:08x}"
            self.snapshots[sid] = FakeSnapshot(sid, vid, bytes(vol.data))
            return httpx.Response(200, text=_xml("CreateSnapshotResponse",
                                                 f"<snapshotId>{sid}</snapshotId><status>completed</status>"))
        if action == "DescribeSnapshots":
            ids = [v for k, vs in form.items() if k.startswith("SnapshotId") for v in vs]
            items = []
            for sid in ids:
                snap = self.snapshots.get(sid)
                if snap:
                    items.append(_item({"snapshotId": snap.snapshot_id, "status": snap.status,
                                        "volumeId": snap.volume_id}))
            return httpx.Response(200, text=_xml("DescribeSnapshotsResponse",
                                                 "<snapshotSet>" + "".join(items) + "</snapshotSet>"))
        if action == "DeleteSnapshot":
            sid = (form.get("SnapshotId") or [""])[0]
            self.snapshots.pop(sid, None)
            return httpx.Response(200, text=_xml("DeleteSnapshotResponse", "<return>true</return>"))
        return httpx.Response(400, text=f"<Error><Code>Unknown</Code><Message>{action}</Message></Error>")

    def _instance_xml(self, inst: FakeInstance) -> str:
        mappings = []
        for i, vol in enumerate(inst.volumes):
            dev = inst.root_device_name if i == 0 else f"/dev/sdf{i}"
            mappings.append(
                f"<item><deviceName>{dev}</deviceName><ebs><volumeId>{vol.volume_id}</volumeId>"
                f"<status>attached</status></ebs></item>"
            )
        codes = "".join(f"<item><productCode>{c}</productCode></item>" for c in inst.product_codes)
        return (
            "<item>"
            f"<instanceId>{inst.instance_id}</instanceId>"
            f"<instanceType>{inst.instance_type}</instanceType>"
            f"<imageId>{inst.image_id}</imageId>"
            f"<platform>{inst.platform}</platform>"
            f"<platformDetails>{inst.platform_details}</platformDetails>"
            f"<rootDeviceType>{inst.root_device_type}</rootDeviceType>"
            f"<rootDeviceName>{inst.root_device_name}</rootDeviceName>"
            f"<bootMode>{inst.boot_mode}</bootMode>"
            f"<currentInstanceBootMode>{inst.boot_mode}</currentInstanceBootMode>"
            f"<vpcId>{inst.vpc_id}</vpcId>"
            f"<instanceState><name>{inst.state}</name></instanceState>"
            f"<placement><availabilityZone>{inst.az}</availabilityZone></placement>"
            f"<tagSet><item><key>Name</key><value>{escape(inst.name)}</value></item></tagSet>"
            f"<blockDeviceMapping>{''.join(mappings)}</blockDeviceMapping>"
            f"<productCodes>{codes}</productCodes>"
            f"<networkInterfaceSet><item><networkInterfaceId>eni-{inst.instance_id[-4:]}</networkInterfaceId></item></networkInterfaceSet>"
            "</item>"
        )

    def _ebs(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = [p for p in path.split("/") if p]
        # Official EBS Direct: GET /snapshots/{id}/blocks  and  GET /snapshots/{id}/blocks/{index}
        if len(parts) < 3 or parts[0] != "snapshots":
            return httpx.Response(404, text="<UnknownOperationException/>")
        sid = parts[1]
        snap = self.snapshots.get(sid)
        if snap is None:
            return httpx.Response(404, json={"Code": "ResourceNotFoundException", "Message": sid})
        if parts[2] == "blocks" and len(parts) == 3:
            blocks = []
            data = snap.data
            for i in range(0, max(len(data), 1), BLOCK):
                chunk = data[i:i + BLOCK]
                if any(chunk):
                    token = hashlib.sha256(chunk).hexdigest()[:24]
                    blocks.append({"BlockIndex": i // BLOCK, "BlockToken": token})
            return httpx.Response(200, json={"Blocks": blocks, "BlockSize": BLOCK, "VolumeSize": 1})
        if parts[2] == "blocks" and len(parts) >= 4:
            index = int(parts[3])
            qs = parse_qs(urlsplit(str(request.url)).query)
            token = unquote((qs.get("blockToken") or [""])[0])
            off = index * BLOCK
            chunk = snap.data[off:off + BLOCK]
            if hashlib.sha256(chunk).hexdigest()[:24] != token:
                return httpx.Response(400, json={"Message": "bad token"})
            return httpx.Response(200, content=chunk)
        return httpx.Response(404, text="<UnknownOperationException/>")


def make_fleet(raws: Optional[dict[int, bytes]] = None) -> FakeAws:
    raws = raws or {0: b"\x01" * 4096 + bytes(BLOCK), 1: b"\x02" * 2048 + bytes(BLOCK)}
    os_vol = FakeVolume("vol-os", raws.get(0, b"\x01" * BLOCK), size_gb=1)
    data_vol = FakeVolume("vol-data", raws.get(1, b"\x02" * BLOCK), size_gb=1)
    win_vol = FakeVolume("vol-win", raws.get(0, b"\x01" * BLOCK), size_gb=1)
    store_vol = FakeVolume("vol-store", raws.get(0, b"\x01" * BLOCK), size_gb=1)
    market_vol = FakeVolume("vol-mkt", raws.get(0, b"\x01" * BLOCK), size_gb=1)
    images = {
        "ami-ubuntu": FakeImage("ami-ubuntu", "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server"),
        "ami-win": FakeImage("ami-win", "Windows_Server-2019-English-Full-Base", platform="windows"),
        "ami-al": FakeImage("ami-al", "amzn2-ami-hvm-2.0"),
    }
    instances = [
        FakeInstance("i-0aa11bb22cc33dd", "lin-01", volumes=[os_vol, data_vol], image_id="ami-ubuntu"),
        FakeInstance("i-0aa11bb22cc33ee", "win-01", state="stopped", platform="windows",
                     platform_details="Windows", image_id="ami-win", volumes=[win_vol]),
        FakeInstance("i-0aa11bb22cc33ff", "store-01", root_device_type="instance-store", volumes=[store_vol],
                     image_id="ami-al"),
        FakeInstance("i-0aa11bb22cc3301", "market-01", product_codes=["abc123"], volumes=[market_vol],
                     image_id="ami-ubuntu"),
    ]
    return FakeAws(instances, images)
