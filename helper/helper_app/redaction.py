"""Remove credentials from log messages, errors and exported diagnostics."""

from __future__ import annotations

import logging
import re
import traceback

_URL_QUERY = re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]+", re.IGNORECASE)
_URL_USER = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL)
_SECRET = re.compile(
    r"(?i)([\"']?(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"client_secret|secret_access_key|access_token|refresh_token|password|private_key)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,}]+)"
)


def redact(text: str) -> str:
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _URL_USER.sub(r"\1[REDACTED]@", text)
    text = _URL_QUERY.sub(r"\1?[REDACTED]", text)
    return _SECRET.sub(r"\1[REDACTED]", text)


def configure_logging_redaction() -> None:
    """Sanitize records before any handler sees them, including exception tracebacks."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_helper_redacts", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
        return record

    factory._helper_redacts = True
    logging.setLogRecordFactory(factory)
