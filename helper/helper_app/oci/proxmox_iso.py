"""Rewrite a Proxmox installer ISO so its initrd can see the media OCI presents.

The stock installer only inspects a whole disk that is removable, detected as ISO9660, or smaller than
about 32 GB, and it never looks at virtio disks (``vd*``) or partitions.  OCI boots an imported ISO as a
large non-removable virtio disk, so the installer prints ``no device with valid ISO found`` and stops.
This module, running on the migration tool VM, unpacks ``/boot/initrd.img``, widens that scan, and
uploads a new object next to the original.  The ``.pve-cd-id.txt`` copies (one in the initrd, one on the
ISO) are left as they are; only the ``init`` script changes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from helper_app.models import IsoSpec
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.object_bytes import open_object_stream
from helper_app.oci.object_upload import MultipartObjectWriter

log = logging.getLogger(__name__)

# Dropped into the rewritten init script.  A second pass sees it and leaves the ISO alone.
MARKER = "oci-umt-proxmox-media"
_SEARCH = "searching for block device containing the ISO"
# The device scan.  ``do`` stays on this line in the Proxmox initrd.
_DEVICE_FOR = re.compile(
    r"^(?P<indent>[ \t]*)for i in (?P<globs>(?:/sys/block/\S+[ \t]*)+);\s*do[ \t]*$",
    re.M,
)
_CD_INFO_KEY = re.compile(r"^([A-Z]+)=['\"]?([^'\"\n]+)", re.M)

Progress = Callable[[int, str], None]
Cancel = Callable[[], None]


class ProxmoxIsoError(Exception):
    """The ISO is not a Proxmox installer this tool can prepare, or a local tool failed."""


@dataclass(frozen=True)
class InitScan:
    """What the installer init script will do with block devices."""

    state: str  # not_proxmox | already_fixed | needs_fix | unrecognised
    detail: str


def fixed_object_name(object_name: str) -> str:
    """Object name of the rewritten ISO, beside the original."""
    if object_name.lower().endswith(".oci-umt.iso"):
        return object_name
    if object_name.lower().endswith(".iso"):
        return object_name[:-4] + ".oci-umt.iso"
    return object_name + ".oci-umt.iso"


def inspect_init_script(text: str) -> InitScan:
    """Classify an unpacked ``/init`` from a Proxmox installer initrd."""
    if MARKER in text:
        return InitScan("already_fixed", "the installer already scans virtio disks and partitions")
    if _SEARCH not in text:
        return InitScan("not_proxmox", "the ISO is not a Proxmox installer (its initrd does not search for an ISO volume)")
    loop = _device_loop_at(text)
    if loop is None:
        return InitScan(
            "unrecognised",
            "this is a Proxmox installer, but its device scan is not the version this tool can rewrite",
        )
    return InitScan("needs_fix", "the installer skips large, non-removable and virtio disks")


def patch_init_script(text: str) -> str:
    """Return ``text`` with the device scan widened.  Already-fixed scripts are returned unchanged."""
    scan = inspect_init_script(text)
    if scan.state == "already_fixed":
        return text.replace("\r\n", "\n")
    if scan.state != "needs_fix":
        raise ProxmoxIsoError(scan.detail)
    normalised = text.replace("\r\n", "\n")
    found = _device_loop_at(normalised)
    assert found is not None  # needs_fix means the loop is there
    start, end, indent = found
    return normalised[:start] + _replacement_loop(indent) + normalised[end:]


def read_cd_info(text: str) -> dict[str, str]:
    """``PRODUCT=...`` assignments from the initrd's ``.cd-info``."""
    return {m.group(1): m.group(2).strip().strip("'\"") for m in _CD_INFO_KEY.finditer(text)}


def product_label(info: dict[str, str]) -> str:
    name = info.get("PRODUCTLONG") or "Proxmox"
    release = info.get("RELEASE") or ""
    iso_release = info.get("ISORELEASE") or ""
    if release and iso_release:
        return f"{name} {release}-{iso_release}"
    if release:
        return f"{name} {release}"
    return name


def apply_proxmox_media_fix(
    clients: OciClients,
    iso: IsoSpec,
    on_progress: Progress | None = None,
    check_cancel: Cancel | None = None,
) -> IsoSpec:
    """Download ``iso`` onto the migration tool VM, rewrite it when needed, upload the result.

    A Proxmox ISO that is already fixed is returned unchanged, with ``proxmox_fix_note`` set.
    Anything else raises ``OciError`` and leaves the bucket as it was.
    """
    def progress(pct: int, message: str) -> None:
        log.info("proxmox iso: %s", message)
        if on_progress:
            on_progress(pct, message)

    def cancel() -> None:
        if check_cancel:
            check_cancel()

    root = "/var/tmp" if os.path.isdir("/var/tmp") else tempfile.gettempdir()
    need = max(iso.size_bytes or 2 * 1024**3, 1) * 3
    free = shutil.disk_usage(root).free
    if free < need:
        raise OciError(
            f"the migration tool VM has {free // (1024**3)} GB free in {root}; preparing this ISO needs "
            f"about {need // (1024**3)} GB (the download, the rewritten ISO, and the unpacked initrd)"
        )
    try:
        with tempfile.TemporaryDirectory(prefix="vc-oci-iso-", dir=root) as tmp:
            work = Path(tmp)
            src = work / "source.iso"
            cancel()
            progress(1, f"Downloading {iso.object_name} to the migration tool VM")
            _download(clients, iso, src, progress, cancel)
            cancel()
            progress(40, "Inspecting the installer initrd")
            info, scan, dst = _prepare_local(src, work / "fixed.iso", work / "build")
            label = product_label(info)
            if scan.state == "already_fixed":
                progress(100, f"{label}: {scan.detail}")
                return iso.model_copy(update={"proxmox_fix_note": f"{label}: {scan.detail}; importing the original"})
            if scan.state != "needs_fix" or dst is None:
                raise ProxmoxIsoError(f"{label}: {scan.detail}" if info else scan.detail)
            name = fixed_object_name(iso.object_name)
            cancel()
            progress(70, f"Uploading {name}")
            _upload(clients, iso.namespace, iso.bucket, name, dst, progress, cancel)
            etag, size = _head(clients, iso.namespace, iso.bucket, name)
            note = (
                f"{label}: rewrote the initrd so the installer also looks on virtio disks and partitions; "
                f"uploaded {name}"
            )
            progress(100, note)
            return iso.model_copy(update={
                "source_object_name": iso.object_name,
                "object_name": name,
                "etag": etag or iso.etag,
                "size_bytes": size or dst.stat().st_size,
                "proxmox_fix_note": note,
            })
    except ProxmoxIsoError as exc:
        raise OciError(str(exc)) from exc


# --------------------------------------------------------------------------- init script
def _device_loop_at(text: str) -> tuple[int, int, str] | None:
    """Start, end and indent of the ``for i in /sys/block/...`` loop after the ISO search."""
    origin = text.find(_SEARCH)
    if origin < 0:
        return None
    match = _DEVICE_FOR.search(text, origin)
    if match is None:
        return None
    globs = match.group("globs")
    if "/sys/block/sd*" not in globs and "/sys/block/hd*" not in globs:
        return None
    end = _matching_done(text, match.end())
    if end is None:
        return None
    return match.start(), end, match.group("indent")


def _matching_done(text: str, pos: int) -> int | None:
    """Offset just past the ``done`` that closes a loop whose body starts at ``pos``."""
    depth = 1
    for line in text[pos:].splitlines(keepends=True):
        if re.match(r"[ \t]*(for|while|until)\b", line):
            depth += 1
        elif re.match(r"[ \t]*done\b", line):
            depth -= 1
            pos += len(line)
            if depth == 0:
                return pos
            continue
        pos += len(line)
    return None


def _replacement_loop(indent: str) -> str:
    # OCI bare metal stores the ISO at the start of a much larger NVMe disk. Proxmox builds that
    # ISO with a 16-sector partition offset, so `mount -t auto /dev/nvme0n1` never sees it.
    body = r"""# MARKER_PLACEHOLDER: mount the ISO9660 session on a large NVMe or virtio disk.
oci_umt_try() {
	oci_dev=$1
	[ -b "$oci_dev" ] || return 1
	echo "testing device '$oci_dev' for ISO"
	oci_mounted=
	if mount -t iso9660 -o ro "$oci_dev" /mnt >/dev/null 2>&1; then
		oci_mounted=1
	elif mount -t iso9660 -o ro,loop,offset=32768 "$oci_dev" /mnt >/dev/null 2>&1; then
		oci_mounted=1
	elif mount -t auto -o ro "$oci_dev" /mnt >/dev/null 2>&1; then
		oci_mounted=1
	fi
	if [ -n "$oci_mounted" ]; then
		if [ -r "/mnt/$CDID_FN" ] && [ "X$(cat "/mnt/$CDID_FN")" = "X$reqid" ]; then
			echo "found $PRODUCTLONG ISO"
			cdrom=$oci_dev
			return 0
		fi
		umount /mnt >/dev/null 2>&1 || true
	fi
	return 1
}
for i in /sys/block/hd* /sys/block/sr* /sys/block/scd* /sys/block/sd* /sys/block/nvme* /sys/block/vd* /sys/block/xvd* /sys/block/mmcblk*; do
	if [ -d "$i" ]; then
		basedev="${i##*/}"
		oci_umt_try "/dev/$basedev" && break
		for part in "$i"/${basedev}*; do
			[ -d "$part" ] || continue
			oci_umt_try "/dev/${part##*/}" && break
		done
		[ -n "$cdrom" ] && break
	fi
done
"""
    body = body.replace("MARKER_PLACEHOLDER", MARKER)
    lines = [(indent + line) if line else line for line in body.splitlines()]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- ISO file on the helper VM
def _ensure_tools(extra: tuple[str, ...] = ()) -> None:
    needed = [name for name in ("xorriso", "cpio", "gzip", *extra) if shutil.which(name) is None]
    if not needed:
        return
    if shutil.which("dnf") is None:
        raise ProxmoxIsoError(
            "the migration tool VM is missing " + ", ".join(needed) + " and has no dnf to install them"
        )
    log.info("installing %s", " ".join(needed))
    proc = subprocess.run(["dnf", "install", "-y", *needed], capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise ProxmoxIsoError(f"installing {' '.join(needed)} on the migration tool VM failed: {tail}")
    still = [name for name in needed if shutil.which(name) is None]
    if still:
        raise ProxmoxIsoError("still missing after install: " + ", ".join(still))


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise ProxmoxIsoError(f"{cmd[0]} failed: {tail}")
    return proc


def _decompress_initrd(blob: Path, dest_cpio: Path) -> None:
    magic = blob.read_bytes()[:6]
    if magic.startswith(b"\x1f\x8b"):
        cmd = ["gzip", "-dc", str(blob)]
    elif magic.startswith(b"\x28\xb5\x2f\xfd"):
        _ensure_tools(("zstd",))
        cmd = ["zstd", "-dc", str(blob)]
    elif magic.startswith(b"\xfd7zXZ\x00"):
        cmd = ["xz", "-dc", str(blob)]
    elif magic.startswith(b"070701") or magic.startswith(b"070702"):
        shutil.copyfile(blob, dest_cpio)
        return
    else:
        raise ProxmoxIsoError(f"unrecognised installer initrd (magic {magic[:4].hex()})")
    _ensure_tools()
    with dest_cpio.open("wb") as out:
        proc = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
        raise ProxmoxIsoError(f"{cmd[0]} failed: {err.strip()[-600:]}")


def _unpack_initrd(blob: Path, tree: Path) -> None:
    _ensure_tools()
    cpio_path = tree.parent / "initrd.cpio"
    _decompress_initrd(blob, cpio_path)
    tree.mkdir(parents=True, exist_ok=True)
    with cpio_path.open("rb") as fh:
        proc = subprocess.run(["cpio", "-idm"], cwd=tree, stdin=fh, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProxmoxIsoError(f"cpio failed: {(proc.stderr or '').strip()[-600:]}")
    if not (tree / "init").is_file():
        raise ProxmoxIsoError("the installer initrd has no /init")


def _pack_initrd(tree: Path, dest: Path) -> None:
    _ensure_tools()
    with dest.open("wb") as out:
        find = subprocess.Popen(["find", ".", "-print0"], cwd=tree, stdout=subprocess.PIPE)
        cpio = subprocess.Popen(
            ["cpio", "--null", "-o", "-H", "newc"], cwd=tree, stdin=find.stdout, stdout=subprocess.PIPE,
        )
        assert find.stdout is not None
        find.stdout.close()  # let cpio see EOF when find exits
        gzip = subprocess.Popen(["gzip", "-9"], stdin=cpio.stdout, stdout=out, stderr=subprocess.PIPE)
        assert cpio.stdout is not None
        cpio.stdout.close()
        gzip_err = gzip.stderr.read().decode(errors="replace") if gzip.stderr else ""
        codes = (gzip.wait(), cpio.wait(), find.wait())
    if any(code != 0 for code in codes):
        raise ProxmoxIsoError(f"repacking the initrd failed: {gzip_err.strip()[-600:]}")


def _extract_initrd(iso: Path, dest: Path) -> None:
    _ensure_tools()
    _run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", "/boot/initrd.img", str(dest)])
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ProxmoxIsoError("the ISO has no /boot/initrd.img")


def parse_volume_date_uuid(report: str) -> str:
    """The ``-volume_date uuid`` value from ``xorriso -report_system_area cmd``.

    Proxmox's own ISO prep keeps this timestamp. Rebuilding the boot records instead
    (``-boot_image any replay``) makes GPT partitions 1 and 2 overlap on a PVE 9 image.
    """
    for line in report.splitlines():
        if line.startswith("-volume_date uuid"):
            uuid = line.split()[-1].strip().strip("'\"")
            if uuid:
                return uuid
    raise ProxmoxIsoError("the ISO has no volume date UUID, so its boot partitions cannot be kept")


def _iso_volume_uuid(iso: Path) -> str:
    proc = _run(["xorriso", "-indev", str(iso), "-report_system_area", "cmd"])
    return parse_volume_date_uuid(proc.stdout)


def _write_fixed_iso(src: Path, dst: Path, initrd: Path) -> None:
    """Copy ``src`` and replace ``/boot/initrd.img`` without rebuilding the GPT.

    Same approach as ``proxmox-auto-install-assistant``: ``-boot_image any keep`` plus the
    original volume-date UUID, so GRUB still finds its partition.
    """
    uuid = _iso_volume_uuid(src)
    shutil.copyfile(src, dst)
    _run([
        "xorriso",
        "-boot_image", "any", "keep",
        "-volume_date", "uuid", uuid,
        "-dev", str(dst),
        "-map", str(initrd), "/boot/initrd.img",
    ])


def _prepare_local(src: Path, dst: Path, work: Path) -> tuple[dict[str, str], InitScan, Path | None]:
    """Inspect ``src`` and, when the init script needs it, write a rewritten ISO to ``dst``.

    The third value is ``dst`` only when a new ISO was written.
    """
    work.mkdir(parents=True, exist_ok=True)
    blob = work / "initrd.img"
    _extract_initrd(src, blob)
    tree = work / "tree"
    _unpack_initrd(blob, tree)
    init_path = tree / "init"
    text = init_path.read_text(errors="replace")
    info: dict[str, str] = {}
    cd_info = tree / ".cd-info"
    if cd_info.is_file():
        info = read_cd_info(cd_info.read_text(errors="replace"))
    scan = inspect_init_script(text)
    if scan.state != "needs_fix":
        return info, scan, None
    init_path.write_text(patch_init_script(text))
    packed = work / "initrd-new.img"
    _pack_initrd(tree, packed)
    if dst.exists():
        dst.unlink()
    _write_fixed_iso(src, dst, packed)
    return info, scan, dst


def _download(clients: OciClients, iso: IsoSpec, dest: Path, progress: Progress, cancel: Cancel) -> None:
    resp = clients.object_storage.get_object(iso.namespace, iso.bucket, iso.object_name)
    stream = open_object_stream(resp.data)
    got = 0
    total = iso.size_bytes or 0
    with dest.open("wb") as out, closing(stream):
        while True:
            cancel()
            block = stream.read(1024 * 1024)
            if not block:
                break
            if not isinstance(block, (bytes, bytearray)):
                block = bytes(block)
            out.write(block)
            got += len(block)
            if total:
                progress(min(35, got * 35 // total), f"Downloading {iso.object_name}")
    if got == 0:
        raise ProxmoxIsoError(f"{iso.object_name} is empty")


def _upload(
    clients: OciClients, namespace: str, bucket: str, name: str, src: Path, progress: Progress, cancel: Cancel,
) -> None:
    size = src.stat().st_size
    writer = MultipartObjectWriter(clients, namespace, bucket, name)
    sent = 0
    try:
        with src.open("rb") as fh:
            while True:
                cancel()
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                sent += len(chunk)
                if size:
                    progress(70 + min(25, sent * 25 // size), f"Uploading {name}")
        writer.close()
    except Exception:
        writer.abort()
        raise


def _head(clients: OciClients, namespace: str, bucket: str, name: str) -> tuple[str, int]:
    resp = clients.object_storage.head_object(namespace, bucket, name)
    headers = {str(k).lower(): v for k, v in (getattr(resp, "headers", None) or {}).items()}
    etag = str(headers.get("etag", "")).strip('"')
    try:
        size = int(headers.get("content-length") or 0)
    except (TypeError, ValueError):
        size = 0
    return etag, size
