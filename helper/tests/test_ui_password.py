"""UI password hash file: scrypt storage, verify, clear, and unlock rate limit."""

from __future__ import annotations

import os
import time

import pytest

from helper_app.ui_password import MIN_PASSWORD_LENGTH, UiPasswordStore, hash_password, setup_pending, verify_password


def test_hash_round_trip_does_not_store_plaintext(tmp_path):
    path = tmp_path / "ui-password.hash"
    store = UiPasswordStore(str(path))
    assert store.required is False
    store.set_password("s3cret!!")
    text = path.read_text(encoding="utf-8")
    assert "s3cret!!" not in text
    assert text.startswith("scrypt$")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.required is True
    assert store.verify("s3cret!!") is True
    assert store.verify("wrong") is False
    assert store.verify("") is False


def test_empty_hash_file_fails_closed(tmp_path):
    path = tmp_path / "ui-password.hash"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Empty UI password hash"):
        UiPasswordStore(str(path))


def test_reload_from_disk(tmp_path):
    path = tmp_path / "ui-password.hash"
    UiPasswordStore(str(path)).set_password("s3cret!!")
    other = UiPasswordStore(str(path))
    assert other.required is True
    assert other.verify("s3cret!!") is True


def test_clear_removes_file_and_disables_protection(tmp_path):
    path = tmp_path / "ui-password.hash"
    store = UiPasswordStore(str(path))
    store.set_password("s3cret!!")
    store.clear()
    assert path.exists() is False
    assert store.required is False
    assert store.verify("s3cret!!") is False


def test_set_password_rejects_short_secret():
    with pytest.raises(ValueError, match="at least"):
        hash_password("short")
    assert MIN_PASSWORD_LENGTH == 8


def test_verify_password_rejects_truncated_or_foreign_records():
    rec = hash_password("s3cret!!")
    assert verify_password("s3cret!!", rec) is True
    assert verify_password("s3cret!!", "not-a-hash") is False
    assert verify_password("s3cret!!", rec[:-4]) is False


def test_unlock_rate_limit_after_five_failures(tmp_path):
    store = UiPasswordStore(str(tmp_path / "ui-password.hash"))
    store.set_password("s3cret!!")
    ip = "203.0.113.9"
    for _ in range(5):
        assert store.rate_limited(ip) is False
        store.record_failure(ip)
    assert store.rate_limited(ip) is True
    store.record_success(ip)
    assert store.rate_limited(ip) is False


def test_rate_limit_window_expires(tmp_path, monkeypatch):
    store = UiPasswordStore(str(tmp_path / "ui-password.hash"))
    ip = "203.0.113.10"
    now = 1_000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    for _ in range(5):
        store.record_failure(ip)
    assert store.rate_limited(ip) is True
    now = 1_000.0 + 61
    assert store.rate_limited(ip) is False


def test_setup_pending_until_skipped_or_password_set(tmp_path):
    from helper_app.config import Settings
    from helper_app.ui_password import mark_prompt_done

    settings = Settings(runtime_settings_path=str(tmp_path / "runtime-settings.json"),
                        ui_password_hash_path=str(tmp_path / "ui-password.hash"))
    store = UiPasswordStore(settings.ui_password_hash_path)
    assert setup_pending(settings, store) is True
    store.set_password("s3cret!!")
    assert setup_pending(settings, store) is False
    store.clear()
    assert setup_pending(settings, store) is True
    mark_prompt_done(settings)
    assert setup_pending(settings, store) is False
