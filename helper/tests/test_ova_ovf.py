import io
import tarfile

import pytest

from helper_app.disk import vmdk_stream as vs
from helper_app.oci.mapping import map_ovf_description, normalize_os_version_for_oci
from helper_app.oci.object_bytes import open_object_stream, read_object_bytes
from helper_app.ova.ovf import OvfParseError, boot_disk_index, inspect_ovf, parse_ovf, prepare_ovf_bytes
from helper_app.ova.package import OvaPackageError, inspect_ova_object, parse_and_stage, read_ovf_from_object


def _minimal_ovf(capacity: int = 1073741824, disk_id: str = "disk1", file_id: str = "file1") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">
  <References>
    <File ovf:id="{file_id}" ovf:href="disk1.vmdk"/>
  </References>
  <DiskSection>
    <Disk ovf:diskId="{disk_id}" ovf:fileRef="{file_id}" ovf:capacity="{capacity}"
          ovf:capacityAllocationUnits="byte"/>
  </DiskSection>
</Envelope>
""".encode()


def _make_ova(vmdk: bytes, ovf: bytes | None = None) -> bytes:
    ovf = ovf or _minimal_ovf(len(vmdk))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("test.ovf")
        info.size = len(ovf)
        tar.addfile(info, io.BytesIO(ovf))
        info = tarfile.TarInfo("disk1.vmdk")
        info.size = len(vmdk)
        tar.addfile(info, io.BytesIO(vmdk))
    return buf.getvalue()


def test_open_object_stream_callable():
    from types import SimpleNamespace

    payload = b"vmdk-chunk"
    data = SimpleNamespace(content=None, raw=SimpleNamespace(stream=lambda: io.BytesIO(payload)))
    assert open_object_stream(data).read() == payload


def test_read_object_bytes_oci_stream_only():
    import io
    from types import SimpleNamespace

    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    payload = _minimal_ovf()
    fake = FakeOci(get_settings().device_prefix)
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects["OVA"] = {"stream.ovf": payload}

    stream = io.BytesIO(payload)

    class ClientWrap:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def object_storage(self):
            os = self._inner.object_storage

            class OSWrap:
                def get_object(self, namespace, bucket, name, **kw):
                    resp = os.get_object(namespace, bucket, name, **kw)
                    return SimpleNamespace(
                        data=SimpleNamespace(content=None, raw=SimpleNamespace(stream=io.BytesIO(payload)))
                    )

            return OSWrap()

    got = read_object_bytes(ClientWrap(fake.clients()), fake.object_storage.NAMESPACE, "OVA", "stream.ovf")
    assert got == payload


def test_parse_ovf_utf16():
    text = _minimal_ovf().decode()
    utf16 = text.encode("utf-16")
    parsed = parse_ovf(utf16)
    assert len(parsed.disks) == 1


def test_parse_ovf_single_disk():
    parsed = parse_ovf(_minimal_ovf())
    assert len(parsed.disks) == 1
    assert parsed.disks[0].capacity_bytes == 1073741824
    assert boot_disk_index(parsed) == 0


def test_parse_ovf_missing_disk():
    with pytest.raises(OvfParseError):
        parse_ovf(b"<Envelope xmlns='http://schemas.dmtf.org/ovf/envelope/1'></Envelope>")


def _rich_ovf() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
 xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData">
  <References>
    <File ovf:id="file1" ovf:href="disk1.vmdk"/>
    <File ovf:id="file2" ovf:href="disk2.vmdk"/>
  </References>
  <DiskSection>
    <Disk ovf:diskId="disk1" ovf:fileRef="file1" ovf:capacity="8589934592" ovf:capacityAllocationUnits="byte"/>
    <Disk ovf:diskId="disk2" ovf:fileRef="file2" ovf:capacity="1073741824" ovf:capacityAllocationUnits="byte"/>
  </DiskSection>
  <OperatingSystemSection ovf:id="96">
    <Info>Guest OS</Info>
    <Description>Ubuntu Linux (64-bit)</Description>
  </OperatingSystemSection>
  <ProductSection>
    <Product>Ubuntu</Product>
    <Version>22.04</Version>
  </ProductSection>
  <VirtualHardwareSection>
    <Item>
      <rasd:ResourceType>3</rasd:ResourceType>
      <rasd:VirtualQuantity>4</rasd:VirtualQuantity>
    </Item>
    <Item>
      <rasd:ResourceType>4</rasd:ResourceType>
      <rasd:VirtualQuantity>16384</rasd:VirtualQuantity>
      <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
    </Item>
  </VirtualHardwareSection>
</Envelope>
"""


def test_inspect_ovf_hardware_and_os():
    meta = inspect_ovf(_rich_ovf())
    assert meta.num_vcpu == 4
    assert meta.memory_mb == 16384
    assert meta.disk_count == 2
    assert meta.boot_disk_bytes == 8589934592
    assert "Ubuntu" in meta.os_description


def test_inspect_ovf_vmware_config_firmware():
    ovf = b"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
 xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:id="file1" ovf:href="disk1.vmdk"/>
  </References>
  <DiskSection>
    <Disk ovf:diskId="disk1" ovf:fileRef="file1" ovf:capacity="1073741824" ovf:capacityAllocationUnits="byte"/>
  </DiskSection>
  <VirtualSystem>
    <VirtualHardwareSection>
      <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/>
      <vmw:Config ovf:required="false" vmw:key="bootOptions.efiSecureBootEnabled" vmw:value="true"/>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
"""
    meta = inspect_ovf(ovf)
    assert meta.firmware == "UEFI_64"
    assert meta.secure_boot is True


def test_map_ovf_description_ubuntu():
    os_meta = map_ovf_description("Ubuntu Linux (64-bit)", "Ubuntu", "22.04")
    assert os_meta.operating_system == "Ubuntu"
    assert os_meta.operating_system_version == "22.04"
    assert os_meta.version_detected


def test_normalize_os_version_windows_and_linux():
    assert normalize_os_version_for_oci("Windows", "2022") == "Server 2022 Standard"
    assert normalize_os_version_for_oci("Windows", "Server 2022") == "Server 2022 Standard"
    assert normalize_os_version_for_oci("Windows", "Windows11") == "Windows11"
    assert normalize_os_version_for_oci("Oracle Linux", "9.8") == "9"
    assert normalize_os_version_for_oci("Ubuntu", "22.04.1") == "22.04"


def test_read_standalone_ovf_object():
    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    fake = FakeOci(get_settings().device_prefix)
    ovf = _rich_ovf()
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects.setdefault("OVA", {})["guest.ovf"] = ovf
    fake.object_storage.add_object("OVA", "guest.ovf", size=len(ovf))
    ovf_only = read_ovf_from_object(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "guest.ovf")
    assert b"Ubuntu" in ovf_only
    info = inspect_ova_object(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "guest.ovf")
    assert info.operating_system == "Ubuntu"
    assert any("Standalone OVF" in n for n in info.notes)


def test_parse_and_stage_standalone_ovf():
    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    fake = FakeOci(get_settings().device_prefix)
    raw = make_raw_small()
    vmdk = vs.encode_raw_bytes(raw)
    ovf = _rich_ovf()
    fake.object_storage.add_bucket("OVA")
    objs = fake.object_storage.bucket_objects.setdefault("OVA", {})
    objs["guest.ovf"] = ovf
    objs["disk1.vmdk"] = vmdk
    objs["disk2.vmdk"] = vmdk
    fake.object_storage.add_object("OVA", "guest.ovf", size=len(ovf))
    fake.object_storage.add_object("OVA", "disk1.vmdk", size=len(vmdk))
    fake.object_storage.add_object("OVA", "disk2.vmdk", size=len(vmdk))
    parsed = parse_and_stage(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "guest.ovf", "jobovf")
    assert len(parsed.disks) == 2
    assert parsed.disks[0].is_boot


def test_read_ovf_and_inspect_ova_object():
    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    fake = FakeOci(get_settings().device_prefix)
    raw = make_raw_small()
    vmdk = vs.encode_raw_bytes(raw)
    ovf = _rich_ovf()
    ova_bytes = _make_ova(vmdk, ovf)
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects.setdefault("OVA", {})["guest.ova"] = ova_bytes
    fake.object_storage.add_object("OVA", "guest.ova", size=len(ova_bytes))
    ovf_only = read_ovf_from_object(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "guest.ova")
    assert b"Ubuntu" in ovf_only
    info = inspect_ova_object(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "guest.ova")
    assert info.suggested_ocpus == 2
    assert info.suggested_memory_gb == 16
    assert info.operating_system == "Ubuntu"


def test_parse_and_stage_ova():
    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    fake = FakeOci(get_settings().device_prefix)
    raw = make_raw_small()
    vmdk = vs.encode_raw_bytes(raw)
    ova_bytes = _make_ova(vmdk)
    fake.object_storage.add_bucket("OVA")
    fake.object_storage.bucket_objects.setdefault("OVA", {})["test.ova"] = ova_bytes
    fake.object_storage.add_object("OVA", "test.ova", size=len(ova_bytes))
    parsed = parse_and_stage(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "test.ova", "job001")
    assert len(parsed.disks) == 1
    assert parsed.disks[0].is_boot
    assert parsed.staging_prefix == "oci-umt-ova/job001"
    assert fake.object_storage.has_object("OVA", f"{parsed.staging_prefix}/disk1.vmdk")


def make_raw_small() -> bytes:
    import random

    size = 4 * 1024 * 1024
    rnd = random.Random(2)
    raw = bytearray(size)
    grain = vs.DEFAULT_GRAIN_SECTORS * vs.SECTOR
    for g in range(size // grain):
        if g % 3 == 0:
            raw[g * grain : (g + 1) * grain] = rnd.randbytes(grain)
    return bytes(raw)


def test_parse_and_stage_missing_ova():
    from helper_app.config import get_settings
    from .fake_oci import FakeOci

    fake = FakeOci(get_settings().device_prefix)
    fake.object_storage.add_bucket("OVA")
    with pytest.raises(OvaPackageError):
        parse_and_stage(fake.clients(), fake.object_storage.NAMESPACE, "OVA", "missing.ova", "j1")
