"""Optional shared UI password: scrypt hash on disk, never plaintext.

The hash file (``HELPER_UI_PASSWORD_HASH_PATH``) is the only persistence.  A missing or empty file
means the web UI is open (today's anonymous session).  Format::

    scrypt$n$r$p$<urlsafe-b64-salt>$<urlsafe-b64-dk>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from helper_app import runtime_settings

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
UNLOCK_MAX_FAILURES = 5
UNLOCK_WINDOW_S = 60.0


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    """Encode ``password`` as a self-contained scrypt record.  Raises ``ValueError`` if too short."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, record: str) -> bool:
    """Constant-time check of ``password`` against a ``hash_password`` record.  False on any malformation."""
    try:
        kind, n_s, r_s, p_s, salt_b64, dk_b64 = record.strip().split("$")
        if kind != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if n != SCRYPT_N or r != SCRYPT_R or p != SCRYPT_P:
            return False
        salt, expected = _unb64(salt_b64), _unb64(dk_b64)
        if len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN)
    except (ValueError, TypeError, OverflowError):
        return False
    return hmac.compare_digest(dk, expected)


class UiPasswordStore:
    """Hash file + in-memory copy, plus a per-IP failure window for ``POST /api/auth/unlock``."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._record: str | None = None
        self._fail: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.reload()

    @property
    def required(self) -> bool:
        return bool(self._record)

    def reload(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self._record = None
            return
        except OSError as exc:
            log.warning("ignoring unreadable UI password hash %s: %s", self.path, exc)
            self._record = None
            return
        self._record = text or None

    def verify(self, password: str) -> bool:
        if not self._record or not password:
            return False
        return verify_password(password, self._record)

    def set_password(self, password: str) -> None:
        record = hash_password(password)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(record + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        self._record = record
        log.warning("UI password hash written to %s", self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._record = None
        log.warning("UI password hash removed from %s", self.path)

    def rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            times = [t for t in self._fail.get(ip, []) if now - t < UNLOCK_WINDOW_S]
            self._fail[ip] = times
            return len(times) >= UNLOCK_MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            times = [t for t in self._fail.get(ip, []) if now - t < UNLOCK_WINDOW_S]
            times.append(now)
            self._fail[ip] = times

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fail.pop(ip, None)


PROMPT_DONE_KEY = "ui_password_prompt_done"


def setup_pending(settings, store: UiPasswordStore) -> bool:
    """True until the operator sets a password or explicitly continues without one."""
    if store.required:
        return False
    return not bool(runtime_settings.read(settings).get(PROMPT_DONE_KEY))


def mark_prompt_done(settings) -> None:
    runtime_settings.write(settings, {PROMPT_DONE_KEY: True})
