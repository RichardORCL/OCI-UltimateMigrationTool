"""Read Proxmox VM disks over SSH through temporary read-only qemu-nbd exports.

For each disk the node resolves the volume id to a path (``pvesm path``), ``qemu-img map`` lists
the extents that hold data, and ``qemu-nbd`` exports the image (any format, any storage) on a
unix socket. The helper reaches that socket through an SSH stream-local channel and reads the
data extents with NBD. Socket paths are recorded on ``ProxmoxSourceInfo.nbd_exports`` before the
export starts, so a failed or cancelled job can still stop it with a later cleanup.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

from helper_app.branding import PREFIX
from helper_app.models import ProxmoxSourceInfo
from helper_app.proxmox.config_parse import valid_volid
from helper_app.proxmox.nbd import NbdClient, NbdPool
from helper_app.proxmox.session import ProxmoxSession
from helper_app.proxmox.ssh import NodeShell, SshError

log = logging.getLogger(__name__)

RUN_DIR = "/run"
SOCKET_RE = re.compile(rf"^{RUN_DIR}/{PREFIX}-[0-9a-f]{{8}}-disk\d+\.sock$")
FORMATS = ("raw", "qcow2", "vmdk")
Extent = tuple[int, int]


def socket_path(job_id: str, index: int) -> str:
    return f"{RUN_DIR}/{PREFIX}-{re.sub(r'[^0-9a-f]', '0', job_id[:8].lower()):0<8}-disk{int(index)}.sock"


def pid_path(sock: str) -> str:
    return sock[: -len(".sock")] + ".pid"


def _run(shell: NodeShell, command: str, what: str, timeout: float = 300.0) -> str:
    result = shell.run(command, timeout=timeout)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise SshError(f"{what} on {shell.host} failed (exit {result.exit_status}): "
                       f"{detail[-1] if detail else 'no output'}"[:600])
    return result.stdout


def resolve_path(shell: NodeShell, sudo: str, volid: str) -> str:
    if not valid_volid(volid):
        raise SshError(f"refusing unsupported volume id {volid!r}")
    out = _run(shell, f"{sudo}pvesm path {shlex.quote(volid)}", f"pvesm path {volid}", timeout=120)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise SshError(f"pvesm path {volid} printed nothing")
    return lines[-1]


def image_size(shell: NodeShell, sudo: str, path: str, fmt: str) -> int:
    out = _run(shell, f"{sudo}qemu-img info -U --output=json -f {fmt} {shlex.quote(path)}",
               f"qemu-img info {path}", timeout=120)
    try:
        return int(json.loads(out).get("virtual-size") or 0)
    except (ValueError, AttributeError) as exc:
        raise SshError(f"qemu-img info {path} returned unexpected output") from exc


def parse_map(text: str, size: int) -> list[Extent]:
    """Data extents from ``qemu-img map --output=json``, clipped to ``size`` and merged."""
    try:
        rows = json.loads(text or "[]")
    except ValueError as exc:
        raise SshError("qemu-img map returned unexpected output") from exc
    out: list[Extent] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("data") or row.get("present") is False:
            continue
        start, length = int(row.get("start") or 0), int(row.get("length") or 0)
        if start >= size or length <= 0:
            continue
        length = min(length, size - start)
        if out and out[-1][0] + out[-1][1] == start:
            out[-1] = (out[-1][0], out[-1][1] + length)
        else:
            out.append((start, length))
    return out


def data_extents(shell: NodeShell, sudo: str, path: str, fmt: str, size: int) -> list[Extent]:
    out = _run(shell, f"{sudo}qemu-img map -U --output=json -f {fmt} {shlex.quote(path)}",
               f"qemu-img map {path}", timeout=1800)
    return parse_map(out, size)


def stop_command(sudo: str, sock: str) -> str:
    pid = pid_path(sock)
    script = (f'p=$(cat {shlex.quote(pid)} 2>/dev/null); '
              f'if [ -n "$p" ] && grep -q qemu-nbd /proc/"$p"/cmdline 2>/dev/null; then kill "$p"; fi; '
              f'rm -f {shlex.quote(sock)} {shlex.quote(pid)}')
    return f"{sudo}sh -c {shlex.quote(script)}"


def start_command(sudo: str, user: str, sock: str, path: str, fmt: str, clients: int) -> str:
    cmd = (f"{sudo}qemu-nbd --read-only --force-share --persistent --shared={int(clients)} --fork "
           f"--format={fmt} --socket={shlex.quote(sock)} --pid-file={shlex.quote(pid_path(sock))} "
           f"{shlex.quote(path)}")
    if sudo:
        cmd += f" && {sudo}chown {shlex.quote(user)} {shlex.quote(sock)}"
    return cmd


@dataclass
class DiskSource:
    index: int
    volid: str
    fmt: str
    path: str
    size: int
    extents: list[Extent]
    socket: str
    pool: NbdPool = field(repr=False)

    @property
    def data_bytes(self) -> int:
        return sum(length for _, length in self.extents)

    def fetch(self, offset: int, length: int) -> bytes:
        return self.pool.fetch(offset, length)


class ProxmoxDiskExport:
    """Context manager: ``open`` exports one disk; ``finish``/exit stops the exports."""

    def __init__(self, session: ProxmoxSession, job_id: str, info: ProxmoxSourceInfo, *, workers: int,
                 save: Callable[[], None], check_cancel: Optional[Callable[[], None]] = None):
        self.session = session
        self.job_id = job_id
        self.info = info
        self.workers = max(1, workers)
        self._save = save
        self._check = check_cancel or (lambda: None)
        self._open: list[DiskSource] = []
        self.shell = session.shell(info.node)
        info.ssh_host = self.shell.host
        info.host_key_fingerprint = self.shell.fingerprint

    def __enter__(self) -> "ProxmoxDiskExport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self, index: int) -> DiskSource:
        self._check()
        volid = self.info.volids[index]
        fmt = self.info.formats[index] if index < len(self.info.formats) else "raw"
        if fmt not in FORMATS:
            raise SshError(f"disk format {fmt!r} is not supported")
        sudo = self.session.sudo()
        path = resolve_path(self.shell, sudo, volid)
        size = image_size(self.shell, sudo, path, fmt)
        if size <= 0:
            raise SshError(f"qemu-img reports no size for {volid}")
        self._check()
        extents = data_extents(self.shell, sudo, path, fmt, size)
        sock = socket_path(self.job_id, index)
        _run(self.shell, stop_command(sudo, sock), "remove a stale NBD export", timeout=60)
        if sock not in self.info.nbd_exports:
            self.info.nbd_exports.append(sock)
            self._save()
        _run(self.shell, start_command(sudo, self.session.ssh.username, sock, path, fmt, self.workers + 1),
             f"qemu-nbd {volid}", timeout=120)
        pool = NbdPool(lambda: NbdClient(self.shell.open_unix(sock)), self.workers)
        source = DiskSource(index=index, volid=volid, fmt=fmt, path=path, size=size, extents=extents,
                            socket=sock, pool=pool)
        self._open.append(source)
        return source

    def finish(self, source: DiskSource) -> None:
        source.pool.close()
        if source in self._open:
            self._open.remove(source)
        _stop(self.session, self.shell, self.info, source.socket)
        self._save()

    def close(self) -> None:
        for source in list(self._open):
            source.pool.close()
        self._open.clear()
        release_proxmox_resources(self.session, self.info)
        self._save()


def _stop(session: ProxmoxSession, shell: NodeShell, info: ProxmoxSourceInfo, sock: str) -> str:
    if not SOCKET_RE.match(sock):
        info.nbd_exports.remove(sock)
        return f"ignored: unexpected export path {sock!r}"
    try:
        _run(shell, stop_command(session.sudo(), sock), f"stop NBD export {sock}", timeout=60)
    except SshError as exc:
        log.warning("could not stop NBD export %s: %s", sock, exc)
        return f"failed: stop NBD export {sock}: {exc}"
    info.nbd_exports.remove(sock)
    return f"ok: stopped NBD export {sock}"


def release_proxmox_resources(session: ProxmoxSession, info: ProxmoxSourceInfo) -> list[str]:
    """Stop every qemu-nbd export recorded on ``info``. Best effort; leftovers stay listed."""
    if not info.nbd_exports:
        return []
    try:
        shell = session.shell(info.node)
    except SshError as exc:
        return [f"failed: SSH to node {info.node}: {exc}"]
    return [_stop(session, shell, info, sock) for sock in list(info.nbd_exports)]
