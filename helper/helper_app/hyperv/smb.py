"""Read-only SMB access to a VHD/VHDX on a Hyper-V host.

A drive path ``C:\\Hyper-V\\disk.vhdx`` becomes ``\\\\host\\C$\\Hyper-V\\disk.vhdx``. A path that is
already a UNC path is opened as given, with the same credentials.
"""

from __future__ import annotations

import re
import threading

from helper_app.hyperv.client import HypervAuthError, HypervError

_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.DOTALL)


def to_unc(host: str, path: str) -> str:
    """Map a Hyper-V disk path to the UNC the migration tool opens."""
    text = (path or "").strip()
    if text.startswith("\\\\") or text.startswith("//"):
        return text.replace("/", "\\")
    match = _DRIVE.match(text)
    if not match or not host:
        raise HypervError(f"disk path {path!r} is not a drive path or a UNC path")
    rest = match.group(2).replace("/", "\\")
    return f"\\\\{host}\\{match.group(1).upper()}$\\{rest}"


class SmbFile:
    """Random-access read of one file. ``read_at`` is safe to call from the copy workers."""

    def __init__(self, read_at, size: int, close):
        self._read_at = read_at
        self.size = size
        self._close = close
        self._closed = False

    def read_at(self, offset: int, length: int) -> bytes:
        return self._read_at(offset, length)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close()


def open_smb_file(username: str, password: str, unc: str, *, port: int = 445) -> SmbFile:
    """Open ``unc`` read-only over SMB (NTLM). The server is the host in the UNC path."""
    import smbclient

    normalized = unc.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    if len(parts) < 2:
        raise HypervError(f"not a UNC path: {unc}")
    server = parts[0]
    try:
        smbclient.register_session(server, username=username, password=password, port=port,
                                   auth_protocol="ntlm")
        handle = smbclient.open_file(normalized, mode="rb")
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if any(word in text for word in ("logon", "credential", "authentication", "access is denied", "status_logon")):
            raise HypervAuthError(f"SMB login to {server} failed: {exc}") from exc
        raise HypervError(f"cannot open {normalized}: {exc}") from exc
    try:
        handle.seek(0, 2)
        size = handle.tell()
    except Exception as exc:  # noqa: BLE001
        handle.close()
        raise HypervError(f"cannot size {normalized}: {exc}") from exc
    lock = threading.Lock()

    def read_at(offset: int, length: int) -> bytes:
        with lock:
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise HypervError(f"short read of {normalized} at {offset} ({len(data)} of {length} bytes)")
        return data

    def close() -> None:
        with lock:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    return SmbFile(read_at, size, close)
