"""In-memory stand-in for the OCI SDK clients used by the helper."""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Callable, Optional

import oci

from helper_app.oci.clients import HelperIdentity, OciClients

_ids = itertools.count(1)


def oid(kind: str) -> str:
    return f"ocid1.{kind}.oc1..{next(_ids):06d}"


def service_error(status: int, code: str, message: str, operation: str) -> oci.exceptions.ServiceError:
    return oci.exceptions.ServiceError(status, code, {"opc-request-id": "FAKE"}, message, operation_name=operation,
                                       request_endpoint=f"POST https://iaas.fake/{operation}")


def check_volume_size(size_gb, operation: str, what: str = "Boot volume") -> None:
    """Mimic OCI's size validation for boot and block volumes."""
    if size_gb is None or not (50 <= size_gb <= 32768):
        raise service_error(400, "InvalidParameter",
                            f"Requested volume size {size_gb or 0}GB is not in the allowed range. {what} should be "
                            "greater than or equal to 50GB and less than or equal to 32,768GB.", operation)


def check_tags(details, operation: str) -> None:
    """Mimic OCI's freeform tag validation: keys may not contain periods or spaces."""
    for key in (getattr(details, "freeform_tags", None) or {}):
        if "." in key or " " in key or len(key) > 100:
            raise service_error(400, "InvalidParameter", "Invalid tags", operation)


class Resp:
    def __init__(self, data, headers=None):
        self.data = data
        self.has_next_page = False
        self.next_page = None
        self.headers = headers or {}
        self.status = 200
        self.request = None


class FakeCompute:
    def __init__(self, fake: "FakeOci"):
        self.f = fake
        self.instances: dict[str, NS] = {}
        self.boot_attachments: dict[str, NS] = {}
        self.launched_vnics: list = []  # CreateVnicDetails of every launch
        self.vnic_attachments: dict[str, NS] = {}
        self.vol_attachments: dict[str, NS] = {}
        self.images: dict[str, NS] = {}
        self.capability_schemas: list = []
        self.launch_details: list = []
        self.attach_errors: list[Exception] = []  # raised (one per call) by attach_volume to a target instance
        self.boot_attach_errors: list[Exception] = []  # raised (one per call) by attach_boot_volume
        self.actions: list[tuple[str, str]] = []
        self.updates: list = []
        self.terminated: list[str] = []
        self.pending_transitions: dict[str, str] = {}
        self.import_polls_left: dict[str, int] = {}  # image id -> get_image calls before the import settles
        self.console_connections: dict[str, NS] = {}
        self.console_deleted: list[str] = []

    # instances -----------------------------------------------------------
    def launch_instance(self, details):
        check_tags(details, "launch_instance")
        boot_size = getattr(details.source_details, "boot_volume_size_in_gbs", None)
        if boot_size is not None:
            check_volume_size(boot_size, "launch_instance", "Boot volume")
        self.launch_details.append(details)
        # OCI: shapeConfig only exists for flexible shapes; bare metal (and fixed VM) shapes refuse it
        fixed = {s.shape for s in self.list_shapes(details.compartment_id).data if not getattr(s, "is_flexible", False)}
        if details.shape in fixed and getattr(details, "shape_config", None) is not None:
            raise service_error(400, "InvalidParameter",
                                f"Shape {details.shape} does not support shapeConfig", "launch_instance")
        boot_volume_id = getattr(details.source_details, "boot_volume_id", None)
        image_id = getattr(details.source_details, "image_id", None)
        if boot_volume_id:
            if boot_volume_id not in self.f.blockstorage.boot_volumes:
                raise service_error(404, "NotAuthorizedOrNotFound", "boot volume not found", "launch_instance")
            image = NS(id="boot-volume-launch", operating_system="Custom", launch_mode="PARAVIRTUALIZED",
                       compatible_shapes=[details.shape])
        else:
            image = self.images[image_id]
        # OCI: the image's shape compatibility list decides which shapes may launch it; an imported image
        # starts with the VM shapes only (see create_image)
        if details.shape not in image.compatible_shapes:
            raise service_error(400, "InvalidParameter",
                                f"Shape {details.shape} is not valid for image {image.id}.", "launch_instance")
        if boot_volume_id:
            lo = details.launch_options
            classes = {lo.boot_volume_type == "PARAVIRTUALIZED", lo.remote_data_volume_type == "PARAVIRTUALIZED",
                       image.launch_mode == "PARAVIRTUALIZED"}
            if len(classes) > 1:
                raise service_error(400, "InvalidParameter",
                                    "Mixing paravirtualized and emulated volumes in the same VM is not supported",
                                    "launch_instance")
        # OCI: one device class per instance.  The boot volume, the data volumes and the image's import
        # launch mode must all be paravirtualized or all emulated.
        lo = details.launch_options
        classes = {lo.boot_volume_type == "PARAVIRTUALIZED", lo.remote_data_volume_type == "PARAVIRTUALIZED",
                   image.launch_mode == "PARAVIRTUALIZED"}
        # ... and OCI resolves the data volume defaults from the image's capability schema too: an emulated
        # boot with a schema pinning Storage.RemoteDataVolumeType to PARAVIRTUALIZED is refused the same way
        schemas = [s for s in self.capability_schemas if s.image_id == image.id]
        if schemas:
            for key in ("Storage.RemoteDataVolumeType", "Storage.LocalDataVolumeType"):
                d = schemas[-1].schema_data.get(key)
                if d is not None:
                    classes.add(d.default_value == "PARAVIRTUALIZED")
        if len(classes) > 1:
            raise service_error(400, "InvalidParameter",
                                "Mixing paravirtualized and emulated volumes in the same VM is not supported",
                                "launch_instance")
        # OCI: consistent volume naming comes from the image schema; a differing launch option is refused
        naming = getattr(lo, "is_consistent_volume_naming_enabled", None)
        if naming is not None:
            schemas = [s for s in self.capability_schemas if s.image_id == image.id]
            image_default = schemas[-1].schema_data["Storage.ConsistentVolumeNaming"].default_value if schemas else True
            if naming != image_default:
                raise service_error(400, "InvalidParameter",
                                    "Overriding ConsistentVolumeNamingEnabled in LaunchOptions is not supported",
                                    "launch_instance")
        pc = getattr(details, "platform_config", None)
        shielded = [getattr(pc, f, False) for f in ("is_secure_boot_enabled", "is_measured_boot_enabled",
                                                   "is_trusted_platform_module_enabled")] if pc else []
        if any(shielded):
            # OCI: shielded launches need UEFI and an image whose capability schema declares Secure Boot
            if details.launch_options.firmware != "UEFI_64":
                raise service_error(400, "InvalidParameter", "Secure Boot requires UEFI_64 firmware", "launch_instance")
            if not boot_volume_id:
                image = self.images[details.source_details.image_id]
                schemas = [s for s in self.capability_schemas if s.image_id == image.id]
                if not schemas or not schemas[-1].schema_data["Compute.SecureBoot"].default_value:
                    raise service_error(400, "InvalidParameter",
                                        "The image does not support Secure Boot (Compute.SecureBoot)", "launch_instance")
            # VM shapes (and Windows anywhere): Secure Boot, Measured Boot and TPM only come as a set
            if not all(shielded) and (details.shape.upper().startswith("VM.") or image.operating_system == "Windows"):
                raise service_error(400, "InvalidParameter",
                                    "Invalid platform configuration for instances secured with Credential Guard. "
                                    "To use Credential Guard, Secure Boot, Measured Boot, and the Trusted Platform "
                                    "Module must be enabled.", "launch_instance")
        iid = oid("instance")
        inst = NS(id=iid, display_name=details.display_name, lifecycle_state="PROVISIONING",
                  availability_domain=details.availability_domain, compartment_id=details.compartment_id,
                  launch_options=details.launch_options, licensing_configs=details.licensing_configs,
                  shape=details.shape, shape_config=details.shape_config, platform_config=pc,
                  operating_system=image.operating_system)
        self.instances[iid] = inst
        vnic = details.create_vnic_details
        label = getattr(vnic, "hostname_label", None)
        if label and label in self.f.network.hostnames_in_subnet(vnic.subnet_id):
            # real OCI accepts the launch and fails it asynchronously via the work request
            self.pending_transitions[iid] = "TERMINATING"
            self.f.work_requests.add("LaunchInstance", details.compartment_id, iid, [
                ("InvalidParameter", "A problem occurred while preparing the instance's VNIC. (Error returned by "
                 f"CreateVnic operation in VcnInternalService service.(400, InvalidParameter, false) Hostname {label} "
                 f"is already used in subnet {vnic.subnet_id}")])
            return Resp(inst)
        self.pending_transitions[iid] = self.f.launch_outcome
        self.f.work_requests.add("LaunchInstance", details.compartment_id, iid, list(self.f.launch_errors))
        self.launched_vnics.append(vnic)
        vnic_id = oid("vnic")
        if label or getattr(vnic, "private_ip", None):
            self.f.network.private_ips.append(NS(hostname_label=label, subnet_id=vnic.subnet_id, vnic_id=vnic_id,
                                                 ip_address=getattr(vnic, "private_ip", None)))
        # the primary VNIC: fixed address when requested, else a DHCP one; public IP only when asked for
        self.f.network.vnics[vnic_id] = NS(
            id=vnic_id, is_primary=True, subnet_id=vnic.subnet_id, lifecycle_state="AVAILABLE",
            private_ip=getattr(vnic, "private_ip", None) or f"10.0.1.{100 + len(self.launched_vnics)}",
            public_ip=f"130.61.0.{len(self.launched_vnics)}" if getattr(vnic, "assign_public_ip", False) else None)
        self.vnic_attachments[oid("vnicattachment")] = NS(id=oid("vnicattachment"), instance_id=iid, vnic_id=vnic_id,
                                                          lifecycle_state="ATTACHED")
        if boot_volume_id:
            bv_id = boot_volume_id
        else:
            bv_id = oid("bootvolume")
            self.f.blockstorage.boot_volumes[bv_id] = NS(id=bv_id, lifecycle_state="AVAILABLE",
                                                         size_in_gbs=details.source_details.boot_volume_size_in_gbs,
                                                         image_id=image_id)
        att_id = oid("bootvolumeattachment")
        self.boot_attachments[att_id] = NS(id=att_id, boot_volume_id=bv_id, instance_id=iid,
                                           lifecycle_state="ATTACHED")
        return Resp(inst)

    def get_instance(self, iid):
        if iid not in self.instances:
            raise service_error(404, "NotAuthorizedOrNotFound", "instance not found", "get_instance")
        inst = self.instances[iid]
        nxt = self.pending_transitions.pop(iid, None)
        if nxt:
            inst.lifecycle_state = nxt
        return Resp(inst)

    def list_instances(self, compartment_id, **kw):
        return Resp([i for i in self.instances.values() if i.compartment_id == compartment_id])

    def add_instance(self, name, compartment_id, shape="VM.Standard.E5.Flex", state="RUNNING",
                     availability_domain=None):
        """An instance that exists in OCI without a migration tool job (Remote Console page tests)."""
        iid = oid("instance")
        self.instances[iid] = NS(id=iid, display_name=name, lifecycle_state=state, compartment_id=compartment_id,
                                 availability_domain=availability_domain or self.f.identity.availability_domain,
                                 shape=shape, time_created=None)
        return iid

    def list_vnic_attachments(self, compartment_id, instance_id=None, **kw):
        return Resp([a for a in self.vnic_attachments.values() if instance_id is None or a.instance_id == instance_id])

    def instance_action(self, iid, action):
        self.actions.append((iid, action))
        inst = self.instances[iid]
        if action in ("STOP", "SOFTSTOP"):
            inst.lifecycle_state = "STOPPING"
            self.pending_transitions[iid] = "STOPPED"
        elif action == "START":
            inst.lifecycle_state = "STARTING"
            self.pending_transitions[iid] = "RUNNING"
        return Resp(inst)

    def update_instance(self, iid, details):
        self.updates.append((iid, details))
        inst = self.instances[iid]
        if details.licensing_configs is not None:
            inst.licensing_configs = details.licensing_configs
        return Resp(inst)

    def terminate_instance(self, iid, preserve_boot_volume=False):
        self.terminated.append(iid)
        self.instances[iid].lifecycle_state = "TERMINATED"
        return Resp(None)

    def list_instance_devices(self, instance_id, is_available=None, **kw):
        used = {a.device for a in self.vol_attachments.values()
                if a.instance_id == instance_id and a.lifecycle_state in ("ATTACHED", "ATTACHING")}
        devs = []
        for i in range(1, 32):
            name = f"{self.f.device_prefix}{_letters(i)}"
            avail = name not in used
            if is_available is None or avail == is_available:
                devs.append(NS(name=name, is_available=avail))
        return Resp(devs)

    def list_shapes(self, compartment_id, availability_domain=None, **kw):
        return Resp([
            NS(shape="VM.Standard.E5.Flex", is_flexible=True, ocpus=1, memory_in_gbs=16,
               ocpu_options=NS(min=1, max=94), memory_options=NS(min_in_g_bs=1, max_in_g_bs=1049)),
            NS(shape="VM.Standard2.1", is_flexible=False, ocpus=1, memory_in_gbs=15, ocpu_options=None,
               memory_options=None),
            NS(shape="VM.Standard.A1.Flex", is_flexible=True, ocpus=1, memory_in_gbs=6,  # Ampere (aarch64)
               ocpu_options=NS(min=1, max=80), memory_options=NS(min_in_g_bs=1, max_in_g_bs=512)),
            NS(shape="BM.Standard.E5.192", is_flexible=False, ocpus=192, memory_in_gbs=2304, ocpu_options=None,
               memory_options=None),
            NS(shape="BM.Standard3.64", is_flexible=False, ocpus=64, memory_in_gbs=1024, ocpu_options=None,
               memory_options=None),
            NS(shape="BM.Standard.A1.160", is_flexible=False, ocpus=160, memory_in_gbs=1024,  # Ampere (aarch64)
               ocpu_options=None, memory_options=None),
        ])

    # console connections -------------------------------------------------
    def create_instance_console_connection(self, details):
        check_tags(details, "create_instance_console_connection")
        if details.instance_id not in self.instances:
            raise service_error(404, "NotAuthorizedOrNotFound", "instance not found",
                                "create_instance_console_connection")
        if not (details.public_key or "").startswith("ssh-rsa "):
            raise service_error(400, "InvalidParameter", "publicKey must be an RSA key in OpenSSH format",
                                "create_instance_console_connection")
        # OCI: one console connection per instance
        for c in self.console_connections.values():
            if c.instance_id == details.instance_id and c.lifecycle_state in ("CREATING", "ACTIVE"):
                raise service_error(409, "Conflict",
                                    f"Instance {details.instance_id} already has a console connection",
                                    "create_instance_console_connection")
        cid = oid("instanceconsoleconnection")
        region = self.f.identity.region
        conn = NS(id=cid, instance_id=details.instance_id,
                  compartment_id=self.instances[details.instance_id].compartment_id,
                  lifecycle_state="CREATING", freeform_tags=dict(details.freeform_tags or {}),
                  fingerprint="SHA256:clientkeyfingerprint", service_host_key_fingerprint=self.f.console_host_fingerprint,
                  connection_string=f"ssh -o ProxyCommand='ssh -W %h:%p -p 443 {cid}@instance-console.{region}.oci."
                                    f"oraclecloud.com' {details.instance_id}",
                  vnc_connection_string=f"ssh -o ProxyCommand='ssh -W %h:%p -p 443 {cid}@instance-console.{region}"
                                        f".oci.oraclecloud.com' -N -L localhost:5900:{details.instance_id}:5900 "
                                        f"{details.instance_id}")
        self.console_connections[cid] = conn
        self.pending_transitions[cid] = "ACTIVE"
        return Resp(conn)

    def get_instance_console_connection(self, cid):
        conn = self.console_connections.get(cid)
        if conn is None:
            raise service_error(404, "NotAuthorizedOrNotFound", "console connection not found",
                                "get_instance_console_connection")
        nxt = self.pending_transitions.pop(cid, None)
        if nxt:
            conn.lifecycle_state = nxt
        return Resp(conn)

    def list_instance_console_connections(self, compartment_id, instance_id=None, **kw):
        for cid in list(self.console_connections):
            self.get_instance_console_connection(cid)  # settle pending transitions like a real listing would
        return Resp([c for c in self.console_connections.values()
                     if (instance_id is None or c.instance_id == instance_id) and c.compartment_id == compartment_id])

    def delete_instance_console_connection(self, cid):
        conn = self.console_connections.get(cid)
        if conn is None or conn.lifecycle_state == "DELETED":
            raise service_error(404, "NotAuthorizedOrNotFound", "console connection not found",
                                "delete_instance_console_connection")
        self.console_deleted.append(cid)
        conn.lifecycle_state = "DELETING"
        self.pending_transitions[cid] = "DELETED"
        return Resp(None)

    # boot volumes --------------------------------------------------------
    def list_boot_volume_attachments(self, availability_domain, compartment_id, instance_id=None, **kw):
        atts = [a for a in self.boot_attachments.values() if instance_id is None or a.instance_id == instance_id]
        return Resp(atts)

    def detach_boot_volume(self, att_id):
        self.boot_attachments[att_id].lifecycle_state = "DETACHED"
        return Resp(None)

    def get_boot_volume_attachment(self, att_id):
        return Resp(self.boot_attachments[att_id])

    def attach_boot_volume(self, details):
        if self.boot_attach_errors:
            raise self.boot_attach_errors.pop(0)
        att_id = oid("bootvolumeattachment")
        att = NS(id=att_id, boot_volume_id=details.boot_volume_id, instance_id=details.instance_id,
                 lifecycle_state="ATTACHED")
        self.boot_attachments[att_id] = att
        return Resp(att)

    # block volume attachments -------------------------------------------
    def attach_volume(self, details):
        att_id = oid("volumeattachment")
        device = getattr(details, "device", None)
        is_boot = details.volume_id in self.f.blockstorage.boot_volumes
        if is_boot and device:
            raise service_error(400, "InvalidParameter",
                                f"The volume cannot be attached to the instance {details.instance_id} because the "
                                f"specified device attribute {device} is invalid.", "attach_volume")
        inst = self.instances.get(details.instance_id)
        if inst is not None and self.attach_errors:
            raise self.attach_errors.pop(0)
        if device and inst is not None and getattr(inst, "operating_system", None) == "Windows":
            raise service_error(400, "InvalidParameter",
                                f"The volume cannot be attached to the instance because the device attribute {device} "
                                "is not supported with Attach Volume Operation for Windows operating system. Remove "
                                "the device attribute value and try again. ", "attach_volume")
        # OCI: data volumes (whatever the attachment type) only attach to a RUNNING instance
        if inst is not None and inst.lifecycle_state != "RUNNING":
            raise service_error(409, "IncorrectState",
                                f"Instance {details.instance_id} is in {inst.lifecycle_state.capitalize()} state, "
                                "when it was expected to be in Running state", "attach_volume")
        # OCI: multi-attach needs read/write shareable on every attachment; only iSCSI and paravirtualized
        # attachments of block (not boot) volumes can be shareable
        shareable = bool(getattr(details, "is_shareable", False))
        if shareable and (is_boot or details.type == "emulated"):
            raise service_error(400, "InvalidParameter",
                                "Shareable attachments are only supported for iSCSI and paravirtualized block "
                                "volumes", "attach_volume")
        others = [a for a in self.vol_attachments.values()
                  if a.volume_id == details.volume_id and a.lifecycle_state in ("ATTACHED", "ATTACHING")]
        if others and not (shareable and all(a.is_shareable for a in others)):
            raise service_error(409, "Conflict",
                                f"Volume {details.volume_id} is already attached to instance {others[0].instance_id}; "
                                "attach it as read/write shareable on all instances to share it", "attach_volume")
        att = NS(id=att_id, volume_id=details.volume_id, instance_id=details.instance_id, device=device,
                 attachment_type=details.type, is_shareable=shareable, lifecycle_state="ATTACHED", fake_disk=None,
                 instance_state_at_attach=inst.lifecycle_state if inst is not None else None)
        self.vol_attachments[att_id] = att
        if details.instance_id == self.f.identity.instance_id:
            # simulate the disk appearing on the helper: at the consistent path, or as the next /dev/sdX
            if device:
                path = Path(device)
            else:
                size = self.f.volume_size_gb(details.volume_id) * 1024**3
                path = Path(self.f.device_prefix).parent / f"sd{_letters(len(self.f.block_devices))}"
                self.f.block_devices[str(path)] = size
                att.fake_disk = str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            body = self.f.blockstorage.volume_bodies.get(details.volume_id)
            if body:
                path.write_bytes(body)
        return Resp(att)

    def list_volume_attachments(self, compartment_id, instance_id=None, **kw):
        return Resp([
            a for a in self.vol_attachments.values()
            if instance_id is None or a.instance_id == instance_id
        ])

    def get_volume_attachment(self, att_id):
        return Resp(self.vol_attachments[att_id])

    def detach_volume(self, att_id):
        att = self.vol_attachments[att_id]
        att.lifecycle_state = "DETACHED"
        if getattr(att, "fake_disk", None):
            self.f.block_devices.pop(att.fake_disk, None)
        return Resp(None)

    # images --------------------------------------------------------------
    def list_images(self, compartment_id, lifecycle_state=None, **kw):
        imgs = [i for i in self.images.values() if i.compartment_id == compartment_id
                and (lifecycle_state is None or i.lifecycle_state == lifecycle_state)]
        return Resp(imgs)

    def create_image(self, details):
        check_tags(details, "create_image")
        if details.launch_mode not in ("NATIVE", "EMULATED", "PARAVIRTUALIZED"):
            # CUSTOM is not importable through the public API (CreateImageDetails has no launchOptions)
            raise service_error(400, "MissingParameter",
                                "Missing launchOptions: launchOptions must be provided when using CUSTOM launchMode",
                                "create_image")
        src = details.image_source_details
        # OCI: CreateImage only knows the server catalog for Windows; the client editions ("Windows10" /
        # "Windows11") are rejected here and can only be set with UpdateImage afterwards
        os_name, os_version = src.operating_system or "Custom", src.operating_system_version or "Custom"
        if os_name == "Windows" and not os_version.startswith("Server "):
            raise service_error(400, "InvalidParameter",
                                f"Invalid operatingSystemVersion: {os_version} (The operating system version is "
                                "not supported.)", "create_image")
        # only the documented types; an ISO is imported as VMDK (OCI recognises the content as boot media)
        if src.source_image_type not in ("QCOW2", "VMDK"):
            raise service_error(400, "InvalidParameter",
                                f"Invalid sourceImageType: {src.source_image_type}", "create_image")
        import_errors, outcome = list(self.f.import_errors), self.f.import_outcome
        if not self.f.object_storage.has_object(src.bucket_name, src.object_name):
            # like a real import of a missing object: accepted, then fails asynchronously
            outcome = "DELETED"
            import_errors.append(("InvalidParameter", f"Object {src.object_name} not found in bucket "
                                                      f"{src.bucket_name}"))
        iid = oid("image")
        img = NS(id=iid, display_name=details.display_name, compartment_id=details.compartment_id,
                 lifecycle_state="IMPORTING", freeform_tags=dict(details.freeform_tags or {}),
                 launch_mode=details.launch_mode, operating_system=os_name,
                 operating_system_version=os_version, source_image_type=src.source_image_type,
                 object_name=src.object_name, bucket_name=src.bucket_name, namespace_name=src.namespace_name,
                 # default compatibility list of an imported image: the VM shapes, no bare metal
                 compatible_shapes={s.shape for s in self.list_shapes(details.compartment_id).data
                                    if s.shape.startswith("VM.")})
        self.images[iid] = img
        self.pending_transitions[iid] = outcome
        # the import stays IMPORTING for `import_polls` get_image calls; the work request percent follows
        self.import_polls_left[iid] = self.f.import_polls
        total = max(1, self.f.import_polls)
        wr_id = self.f.work_requests.add(
            "CreateImage", details.compartment_id, iid, import_errors, self.f.import_logs,
            percent=lambda: 100.0 * (total - self.import_polls_left.get(iid, 0)) / total)
        return Resp(img, headers={"opc-work-request-id": wr_id})

    def get_image(self, iid):
        img = self.images[iid]
        if self.import_polls_left.get(iid, 0) > 0:
            self.import_polls_left[iid] -= 1
            return Resp(img)
        nxt = self.pending_transitions.pop(iid, None)
        if nxt:
            img.lifecycle_state = nxt
        return Resp(img)

    def update_image(self, iid, details):
        img = self.images[iid]
        if img.lifecycle_state != "AVAILABLE":
            raise service_error(409, "IncorrectState", f"Image {iid} is in {img.lifecycle_state} state", "update_image")
        os_name = details.operating_system or img.operating_system
        os_version = details.operating_system_version or img.operating_system_version
        if os_name == "Windows" and not (os_version.startswith("Server ") or os_version in ("Windows10", "Windows11")):
            raise service_error(400, "InvalidParameter",
                                f"Invalid operatingSystemVersion: {os_version} (The operating system version is "
                                "not supported.)", "update_image")
        img.operating_system, img.operating_system_version = os_name, os_version
        if details.display_name:
            img.display_name = details.display_name
        return Resp(img)

    def delete_image(self, iid):
        self.images[iid].lifecycle_state = "DELETED"
        return Resp(None)

    def list_image_shape_compatibility_entries(self, iid, **kw):
        img = self.images[iid]
        return Resp([NS(image_id=iid, shape=s) for s in sorted(img.compatible_shapes)])

    def add_image_shape_compatibility_entry(self, iid, shape_name, **kw):
        # same signature as the SDK: the body only as add_image_shape_compatibility_entry_details=...
        img = self.images[iid]
        if img.lifecycle_state != "AVAILABLE":
            raise service_error(409, "IncorrectState", f"Image {iid} is in {img.lifecycle_state} state",
                                "add_image_shape_compatibility_entry")
        img.compatible_shapes.add(shape_name)
        return Resp(NS(image_id=iid, shape=shape_name))

    def list_compute_global_image_capability_schemas(self, **kw):
        return Resp([NS(id=oid("globalschema"), current_version_name="v1.0")])

    def list_compute_global_image_capability_schema_versions(self, schema_id, **kw):
        return Resp([NS(name="v1.0")])

    def create_compute_image_capability_schema(self, details):
        check_tags(details, "create_compute_image_capability_schema")
        details.id = oid("capschema")
        self.capability_schemas.append(details)
        return Resp(NS(id=details.id, image_id=details.image_id, schema_data=details.schema_data))

    def list_compute_image_capability_schemas(self, compartment_id=None, image_id=None, **kw):
        found = [s for s in self.capability_schemas
                 if (image_id is None or s.image_id == image_id)
                 and (compartment_id is None or s.compartment_id == compartment_id)]
        return Resp([NS(id=s.id, image_id=s.image_id) for s in found])

    def get_compute_image_capability_schema(self, schema_id):
        for s in self.capability_schemas:
            if s.id == schema_id:
                return Resp(NS(id=s.id, image_id=s.image_id, schema_data=dict(s.schema_data)))
        raise service_error(404, "NotAuthorizedOrNotFound", f"schema {schema_id} not found",
                            "get_compute_image_capability_schema")

    def update_compute_image_capability_schema(self, schema_id, details):
        for s in self.capability_schemas:
            if s.id == schema_id:
                s.schema_data = dict(details.schema_data)
                return Resp(NS(id=s.id, image_id=s.image_id, schema_data=s.schema_data))
        raise service_error(404, "NotAuthorizedOrNotFound", f"schema {schema_id} not found",
                            "update_compute_image_capability_schema")


class FakeBlockstorage:
    def __init__(self):
        self.volumes: dict[str, NS] = {}
        self.boot_volumes: dict[str, NS] = {}
        self.deleted: list[str] = []
        self.volume_bodies: dict[str, bytes] = {}

    def create_volume(self, details):
        check_tags(details, "create_volume")
        check_volume_size(details.size_in_gbs, "create_volume", "Volume")
        vid = oid("volume")
        vol = NS(id=vid, display_name=details.display_name, size_in_gbs=details.size_in_gbs,
                 vpus_per_gb=details.vpus_per_gb if details.vpus_per_gb is not None else 10,
                 lifecycle_state="AVAILABLE", availability_domain=details.availability_domain,
                 compartment_id=details.compartment_id, freeform_tags=details.freeform_tags)
        self.volumes[vid] = vol
        return Resp(vol)

    def create_boot_volume(self, details):
        check_tags(details, "create_boot_volume")
        check_volume_size(details.size_in_gbs, "create_boot_volume", "BootVolume")
        vid = oid("bootvolume")
        vol = NS(id=vid, display_name=details.display_name, size_in_gbs=details.size_in_gbs,
                 vpus_per_gb=details.vpus_per_gb if details.vpus_per_gb is not None else 10,
                 lifecycle_state="AVAILABLE", availability_domain=details.availability_domain,
                 compartment_id=details.compartment_id, freeform_tags=details.freeform_tags)
        self.boot_volumes[vid] = vol
        return Resp(vol)

    def get_volume(self, vid):
        return Resp(self.volumes[vid])

    def get_boot_volume(self, vid):
        return Resp(self.boot_volumes[vid])

    def delete_volume(self, vid):
        self.deleted.append(vid)
        return Resp(None)

    def delete_boot_volume(self, vid):
        self.deleted.append(vid)
        return Resp(None)


class FakeObjectStorage:
    NAMESPACE = "testnamespace"

    def __init__(self, bucket_exists=False, compartment_id: str = "ocid1.compartment.oc1..helper"):
        self.buckets: set[str] = {"oci-umt-seed"} if bucket_exists else set()
        self.bucket_compartments: dict[str, str] = {}  # bucket -> compartment (default: the helper's)
        self.default_compartment = compartment_id
        self.objects: dict[str, bytes] = {}  # seed placeholders (seed bucket): name -> body
        self.bucket_objects: dict[str, dict[str, bytes]] = {}  # bucket -> name -> body
        self.iso_objects: dict[str, dict[str, NS]] = {}  # bucket -> name -> ObjectSummary-like
        self.deleted: list[str] = []
        self.list_calls: list[dict] = []  # kwargs of every list_buckets / list_objects call
        self.pars: list[dict] = []
        self.multiparts: dict[str, dict] = {}
        self.aborted_uploads: list[str] = []

    def add_bucket(self, name: str, compartment_id: Optional[str] = None) -> None:
        self.buckets.add(name)
        self.bucket_compartments[name] = compartment_id or self.default_compartment

    def add_object(self, bucket: str, name: str, size: int = 4 * 1024**3, etag: str = "") -> NS:
        """Put an object (an ISO, typically) into a bucket the way a user upload would."""
        if bucket not in self.buckets:
            self.add_bucket(bucket)
        obj = NS(name=name, size=size, etag=etag or f"etag-{name}", time_modified=None, md5=None,
                 time_created=None, storage_tier="Standard", archival_state=None)
        self.iso_objects.setdefault(bucket, {})[name] = obj
        return obj

    def has_object(self, bucket: str, name: str) -> bool:
        return (
            name in self.iso_objects.get(bucket, {})
            or name in self.bucket_objects.get(bucket, {})
            or (bucket in self.buckets and name in self.objects)
        )

    def get_namespace(self, **kw):
        return Resp(self.NAMESPACE)

    def get_bucket(self, namespace, bucket, **kw):
        if bucket not in self.buckets:
            raise oci.exceptions.ServiceError(404, "BucketNotFound", {}, "bucket not found")
        return Resp(NS(name=bucket))

    def create_bucket(self, namespace, details, **kw):
        self.buckets.add(details.name)
        self.bucket_compartments[details.name] = details.compartment_id
        return Resp(NS(name=details.name))

    def list_buckets(self, namespace_name, compartment_id, **kw):
        self.list_calls.append({"op": "list_buckets", "namespace_name": namespace_name,
                                "compartment_id": compartment_id, **kw})
        if namespace_name != self.NAMESPACE:
            raise service_error(404, "NamespaceNotFound", f"namespace {namespace_name} not found", "ListBuckets")
        return Resp([NS(name=b, namespace=namespace_name, compartment_id=compartment_id, time_created=None)
                     for b in sorted(self.buckets)
                     if self.bucket_compartments.get(b, self.default_compartment) == compartment_id])

    def list_objects(self, namespace_name, bucket_name, prefix=None, fields=None, start=None, **kw):
        """Like the SDK: a ``ListObjects`` wrapper (``objects`` + ``next_start_with``), which
        ``oci.pagination`` aggregates into another ``ListObjects`` rather than a plain list."""
        from oci.object_storage.models import ListObjects, ObjectSummary

        self.list_calls.append({"op": "list_objects", "namespace_name": namespace_name, "bucket_name": bucket_name,
                                "prefix": prefix, "fields": fields, "start": start, **kw})
        if bucket_name not in self.buckets:
            raise service_error(404, "BucketNotFound", f"bucket {bucket_name} not found", "ListObjects")
        names = set(self.iso_objects.get(bucket_name, {})) | set(self.bucket_objects.get(bucket_name, {}))
        if bucket_name == "oci-umt-seed":
            names |= set(self.objects)
        summaries = []
        for name in sorted(names):
            if prefix and not name.startswith(prefix):
                continue
            src = self.iso_objects.get(bucket_name, {}).get(name)
            body = self.bucket_objects.get(bucket_name, {}).get(name) or self.objects.get(name)
            s = ObjectSummary()
            s.name = name
            s.size = src.size if src else (len(body) if body else 0)
            s.etag = src.etag if src else f"etag-{name}"
            s.time_modified = src.time_modified if src else None
            summaries.append(s)
        return Resp(ListObjects(objects=summaries, prefixes=[], next_start_with=None))

    def get_object(self, namespace, bucket_name, object_name, **kw):
        if bucket_name not in self.buckets:
            raise service_error(404, "BucketNotFound", f"bucket {bucket_name} not found", "GetObject")
        body = self.bucket_objects.get(bucket_name, {}).get(object_name)
        if body is None and bucket_name == "oci-umt-seed":
            body = self.objects.get(object_name)
        if body is None:
            raise service_error(404, "ObjectNotFound", f"object {object_name} not found", "GetObject")
        import io

        stream = io.BytesIO(body)
        return Resp(NS(content=stream, raw=NS(stream=stream)))

    def put_object(self, namespace, bucket, name, body, **kw):
        assert bucket in self.buckets
        data = bytes(body)
        self.bucket_objects.setdefault(bucket, {})[name] = data
        if bucket == "oci-umt-seed":
            self.objects[name] = data
        return Resp(None)

    def create_preauthenticated_request(self, namespace_name, bucket_name, create_preauthenticated_request_details, **kw):
        self.pars.append({"bucket": bucket_name, "object": create_preauthenticated_request_details.object_name})
        uri = f"/p/fake-upload/n/{namespace_name}/b/{bucket_name}/o/{create_preauthenticated_request_details.object_name}"
        return Resp(NS(access_uri=uri))

    def delete_object(self, namespace, bucket, name, **kw):
        self.objects.pop(name, None)
        self.deleted.append(name)
        return Resp(None)

    def create_multipart_upload(self, namespace_name, bucket_name, create_multipart_upload_details, **kw):
        if bucket_name not in self.buckets:
            raise service_error(404, "BucketNotFound", f"bucket {bucket_name} not found", "CreateMultipartUpload")
        upload_id = f"upload-{next(_ids):06d}"
        object_name = getattr(create_multipart_upload_details, "object", None) or getattr(
            create_multipart_upload_details, "object_name", None
        )
        self.multiparts[upload_id] = {"bucket": bucket_name, "object": object_name, "parts": {}, "aborted": False}
        return Resp(NS(upload_id=upload_id, bucket=bucket_name, object=object_name))

    def upload_part(self, namespace_name, bucket_name, object_name, upload_id, upload_part_num, upload_part_body, **kw):
        mp = self.multiparts[upload_id]
        if mp["aborted"]:
            raise service_error(409, "NoSuchUpload", "upload aborted", "UploadPart")
        mp["parts"][int(upload_part_num)] = bytes(upload_part_body)
        return Resp(NS(etag=f"etag-{upload_part_num}"))

    def commit_multipart_upload(self, namespace_name, bucket_name, object_name, upload_id,
                                commit_multipart_upload_details, **kw):
        mp = self.multiparts[upload_id]
        parts = []
        listed = getattr(commit_multipart_upload_details, "parts_to_commit", None) or []
        if listed:
            for p in listed:
                parts.append(mp["parts"][int(p.part_num)])
        else:
            parts = [mp["parts"][n] for n in sorted(mp["parts"])]
        data = b"".join(parts)
        self.bucket_objects.setdefault(bucket_name, {})[object_name] = data
        mp["committed"] = True
        return Resp(None)

    def abort_multipart_upload(self, namespace_name, bucket_name, object_name, upload_id, **kw):
        mp = self.multiparts.get(upload_id)
        if mp:
            mp["aborted"] = True
            self.aborted_uploads.append(upload_id)
        return Resp(None)


class FakeWorkRequests:
    def __init__(self):
        self.requests: dict[str, tuple[list, list]] = {}  # id -> (errors, log entries)
        self.by_resource: dict[str, list[NS]] = {}  # resource id -> work request summaries
        self.error: Optional[Exception] = None  # raised on every lookup when set (e.g. missing policy)

    def add(self, operation: str, compartment_id: str, resource_id: str, errors: list, logs: list = (),
            percent: Optional[Callable[[], float]] = None) -> str:
        wr_id = oid("coreservicesworkrequest")
        self.requests[wr_id] = (list(errors), list(logs))
        self.by_resource.setdefault(resource_id, []).append(
            NS(id=wr_id, operation_type=operation, compartment_id=compartment_id,
               status="FAILED" if errors else "SUCCEEDED", percent=percent))
        return wr_id

    def get_work_request(self, work_request_id, **kw):
        """Like OCI: ``percent_complete`` grows while the operation runs, ``status`` ends SUCCEEDED/FAILED."""
        if self.error:
            raise self.error
        wr = next(w for ws in self.by_resource.values() for w in ws if w.id == work_request_id)
        pct = wr.percent() if wr.percent else 100.0
        status = wr.status if pct >= 100 else "IN_PROGRESS"
        return Resp(NS(id=wr.id, operation_type=wr.operation_type, compartment_id=wr.compartment_id,
                       status=status, percent_complete=pct))

    def list_work_requests(self, compartment_id, resource_id=None, **kw):
        if self.error:
            raise self.error
        if resource_id:
            return Resp([w for w in self.by_resource.get(resource_id, []) if w.compartment_id == compartment_id])
        return Resp([w for ws in self.by_resource.values() for w in ws if w.compartment_id == compartment_id])

    def list_work_request_errors(self, work_request_id, **kw):
        if self.error:
            raise self.error
        return Resp([NS(code=c, message=m) for c, m in self.requests[work_request_id][0]])

    def list_work_request_logs(self, work_request_id, **kw):
        return Resp([NS(message=m) for m in self.requests[work_request_id][1]])


class FakeIdentity:
    def __init__(self, tenancy_id: str):
        self.tenancy_id = tenancy_id

    def get_compartment(self, cid):
        return Resp(NS(id=cid, name="root", compartment_id=None))

    def list_compartments(self, compartment_id, **kw):
        return Resp([
            NS(id="ocid1.compartment.oc1..prod", name="prod", compartment_id=compartment_id),
            NS(id="ocid1.compartment.oc1..migr", name="migrations", compartment_id="ocid1.compartment.oc1..prod"),
        ])

    def list_availability_domains(self, compartment_id, **kw):
        return Resp([NS(name="Uocm:EU-FRANKFURT-1-AD-1"), NS(name="Uocm:EU-FRANKFURT-1-AD-2")])


class FakeNetwork:
    vcns = {
        "ocid1.vcn.oc1..1": NS(id="ocid1.vcn.oc1..1", display_name="vcn-main", cidr_blocks=["10.0.0.0/16"]),
        "ocid1.vcn.oc1..2": NS(id="ocid1.vcn.oc1..2", display_name="vcn-dmz", cidr_blocks=["192.168.0.0/24"]),
        "ocid1.vcn.oc1..shared": NS(id="ocid1.vcn.oc1..shared", display_name="vcn-shared", cidr_blocks=["172.16.0.0/16"]),
    }

    def __init__(self):
        # private IPs (with DNS labels) already present in the subnets; launches add to this
        self.private_ips: list[NS] = []
        self.vnics: dict[str, NS] = {}  # VNICs created by launches (id -> Vnic)
        self.listed_compartments: list[str] = []  # compartment_id of each list_vcns / list_subnets call

    def hostnames_in_subnet(self, subnet_id: str) -> set[str]:
        return {ip.hostname_label for ip in self.private_ips if ip.subnet_id == subnet_id and ip.hostname_label}

    def get_vnic(self, vnic_id):
        if vnic_id not in self.vnics:
            raise service_error(404, "NotAuthorizedOrNotFound", f"vnic {vnic_id} not found", "GetVnic")
        return Resp(self.vnics[vnic_id])

    def list_private_ips(self, subnet_id=None, ip_address=None, **kw):
        return Resp([ip for ip in self.private_ips if (subnet_id is None or ip.subnet_id == subnet_id)
                     and (ip_address is None or getattr(ip, "ip_address", None) == ip_address)])

    def get_subnet(self, subnet_id):
        for s in self.list_subnets("any").data:
            if s.id == subnet_id:
                return Resp(s)
        raise service_error(404, "NotAuthorizedOrNotFound", f"subnet {subnet_id} not found", "GetSubnet")

    def list_vcns(self, compartment_id, **kw):
        self.listed_compartments.append(compartment_id)
        # vcn-shared lives in another compartment; only its subnet is visible here
        return Resp([v for k, v in self.vcns.items() if k != "ocid1.vcn.oc1..shared"])

    def get_vcn(self, vcn_id):
        return Resp(self.vcns[vcn_id])

    def list_subnets(self, compartment_id, **kw):
        self.listed_compartments.append(compartment_id)
        return Resp([
            NS(id="ocid1.subnet.oc1..1", display_name="private", vcn_id="ocid1.vcn.oc1..1",
               cidr_block="10.0.1.0/24", availability_domain=None, prohibit_public_ip_on_vnic=True),
            NS(id="ocid1.subnet.oc1..2", display_name="public", vcn_id="ocid1.vcn.oc1..2",
               cidr_block="192.168.0.0/25", availability_domain=None, prohibit_public_ip_on_vnic=False),
            NS(id="ocid1.subnet.oc1..3", display_name="app", vcn_id="ocid1.vcn.oc1..shared",
               cidr_block="172.16.1.0/24", availability_domain=None, prohibit_public_ip_on_vnic=True),
        ])


class FakeOci:
    def __init__(self, device_prefix: str, identity: Optional[HelperIdentity] = None, bucket_exists=False):
        self.device_prefix = device_prefix
        self.identity = identity or HelperIdentity(
            instance_id="ocid1.instance.oc1..helper",
            compartment_id="ocid1.compartment.oc1..helper",
            availability_domain="Uocm:EU-FRANKFURT-1-AD-1",
            region="eu-frankfurt-1",
            tenancy_id="ocid1.tenancy.oc1..test",
        )
        # image import outcome: state the image reaches, plus what OCI records on the work request
        self.import_outcome = "AVAILABLE"
        self.import_errors: list[tuple[str, str]] = []
        self.import_logs: list[str] = []
        self.import_polls = 0  # >0: the image stays IMPORTING that many polls, work request percent grows
        # instance launch outcome (state reached after PROVISIONING) and work request errors
        self.launch_outcome = "RUNNING"
        self.launch_errors: list[tuple[str, str]] = []
        # host key fingerprint of the (fake) console connection service
        self.console_host_fingerprint = "SHA256:servicehostkeyfingerprintAAAAAAAAAAAAAAAAAAA"
        # whole disks visible on the helper (what /sys/block would list): its own boot disk to begin with
        self.block_devices: dict[str, int] = {"/dev/sda": 50 * 1024**3}
        self.work_requests = FakeWorkRequests()
        self.blockstorage = FakeBlockstorage()
        self.compute = FakeCompute(self)
        self.object_storage = FakeObjectStorage(bucket_exists, compartment_id=self.identity.compartment_id)
        self.identity_client = FakeIdentity(self.identity.tenancy_id)
        self.network = FakeNetwork()
        self.search = FakeSearch(self)

    def scan_devices(self) -> dict[str, int]:
        return dict(self.block_devices)

    def volume_size_gb(self, volume_id: str) -> int:
        vol = self.blockstorage.boot_volumes.get(volume_id) or self.blockstorage.volumes[volume_id]
        return vol.size_in_gbs

    def clients(self) -> OciClients:
        return OciClients(
            compute=self.compute,
            blockstorage=self.blockstorage,
            network=self.network,
            identity=self.identity_client,
            object_storage=self.object_storage,
            identity_info=self.identity,
            poll_interval_s=0.0,
            work_requests=self.work_requests,
            search=self.search,
        )


class FakeSearch:
    """Resource Search: structured queries of the form
    ``query instance resources where displayName =~ '<text>'`` against the fake compute inventory."""

    _QUERY_RE = re.compile(r"^query instance resources where displayName =~ '(?P<text>(?:[^'\\]|\\.)*)'$")

    def __init__(self, fake: "FakeOci"):
        self.f = fake
        self.queries: list[str] = []

    def search_resources(self, details, limit=None, **kw):
        self.queries.append(details.query)
        m = self._QUERY_RE.match(details.query)
        if details.type != "Structured" or not m:
            raise service_error(400, "InvalidParameter", f"unsupported query: {details.query}", "search_resources")
        needle = m.group("text").replace("\\'", "'").replace("\\\\", "\\").lower()
        items = [
            NS(identifier=i.id, display_name=i.display_name, compartment_id=i.compartment_id,
               lifecycle_state=i.lifecycle_state, availability_domain=getattr(i, "availability_domain", None),
               time_created=getattr(i, "time_created", None), resource_type="Instance")
            for i in self.f.compute.instances.values() if needle in (i.display_name or "").lower()
        ]
        return Resp(NS(items=items[: limit or len(items)]))


def _letters(i: int) -> str:
    # i=1 -> b, ..., z, aa, ab, ...
    s = ""
    while True:
        s = chr(ord("a") + i % 26) + s
        i = i // 26 - 1
        if i < 0:
            return s
