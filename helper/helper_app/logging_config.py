"""Runtime-adjustable logging: helper log level and OCI SDK request/response dumps.

Both can be changed from the Setup page without a restart and are persisted to a small JSON file
(``HELPER_RUNTIME_SETTINGS_PATH``) that overrides the environment on the next start.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from helper_app import runtime_settings
from helper_app.config import Settings

log = logging.getLogger(__name__)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LEVELS: list[str] = ["DEBUG", "INFO", "WARNING", "ERROR"]
OCI_CLIENT_LOGGER_PREFIX = "oci.base_client."


class LoggingSettings(BaseModel):
    log_level: LogLevel = "INFO"
    oci_log_requests: bool = False


class LoggingStatus(LoggingSettings):
    levels: list[str] = LEVELS
    persisted: bool = True
    warning: str = ""


def configure_stdout() -> None:
    """Line-buffer stdout so http.client debug output reaches the journal immediately."""
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - not a TTY/pipe stdout
        pass


def _set_oci_request_logging(enabled: bool) -> None:
    """The SDK creates one logger per client (``oci.base_client.<id>``), disabled unless the client was
    built with ``log_requests``; bodies are printed through ``http.client`` debug output."""
    import http.client

    http.client.HTTPConnection.debuglevel = 1 if enabled else 0
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and name.startswith(OCI_CLIENT_LOGGER_PREFIX):
            logger.disabled = not enabled
            logger.setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("oci").setLevel(logging.DEBUG if enabled else logging.WARNING)


def apply(settings: Settings) -> None:
    """Make the process match ``settings.log_level`` / ``settings.oci_log_requests``."""
    level = settings.log_level.upper()
    if level not in LEVELS:
        log.warning("unknown log level %r; using INFO", settings.log_level)
        level = settings.log_level = "INFO"
    logging.getLogger().setLevel(getattr(logging, level))
    # keep the chatty libraries at INFO+ even when the helper itself logs DEBUG
    for noisy in ("httpx", "httpcore", "urllib3", "pyVmomi"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, getattr(logging, level)))
    _set_oci_request_logging(settings.oci_log_requests)


def load_overrides(settings: Settings) -> None:
    """Apply persisted overrides (from a previous Setup page change) on top of the environment."""
    data = {k: v for k, v in runtime_settings.read(settings).items() if k in LoggingSettings.model_fields}
    if not data:
        return
    try:
        override = LoggingSettings.model_validate({
            "log_level": data.get("log_level", settings.log_level.upper()),
            "oci_log_requests": data.get("oci_log_requests", settings.oci_log_requests),
        })
    except ValueError as exc:
        log.warning("ignoring invalid runtime settings %s: %s", settings.runtime_settings_path, exc)
        return
    settings.log_level = override.log_level
    settings.oci_log_requests = override.oci_log_requests
    log.info("runtime logging settings loaded from %s: %s", settings.runtime_settings_path, override.model_dump())


def current(settings: Settings) -> LoggingStatus:
    return LoggingStatus(log_level=settings.log_level.upper(), oci_log_requests=settings.oci_log_requests,
                         persisted=Path(settings.runtime_settings_path).exists())


def update(settings: Settings, new: LoggingSettings) -> LoggingStatus:
    """Apply immediately and persist; a failure to persist is reported, not fatal."""
    settings.log_level = new.log_level
    settings.oci_log_requests = new.oci_log_requests
    apply(settings)
    log.warning("logging changed: level=%s oci_log_requests=%s", new.log_level, new.oci_log_requests)
    status = current(settings)
    warning = runtime_settings.write(settings, new.model_dump())
    status.persisted = warning is None
    status.warning = warning or ""
    return status
