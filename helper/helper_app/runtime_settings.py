"""Settings adjustable from the Setup page without a restart.

They are persisted to a small JSON file (``HELPER_RUNTIME_SETTINGS_PATH``) that overrides the environment
on the next start.  The file is one flat object shared by the logging settings (``logging_config``) and
the operational limits below; writers merge their own keys into it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from helper_app.config import Settings

log = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = 16  # matches the runner's thread pool
MIN_SESSION_TTL_S = 5 * 60
MAX_SESSION_TTL_S = 7 * 24 * 3600


class OperationSettings(BaseModel):
    max_concurrent_jobs: int = Field(ge=1, le=MAX_CONCURRENT_JOBS,
                                     description="Migrations copying disks at the same time")
    session_ttl_s: int = Field(ge=MIN_SESSION_TTL_S, le=MAX_SESSION_TTL_S,
                               description="Idle timeout of a web/vCenter login, seconds")


class OperationStatus(OperationSettings):
    max_concurrent_jobs_limit: int = MAX_CONCURRENT_JOBS
    session_ttl_min_s: int = MIN_SESSION_TTL_S
    session_ttl_max_s: int = MAX_SESSION_TTL_S
    persisted: bool = True
    warning: str = ""


def read(settings: Settings) -> dict:
    """The persisted overrides, or ``{}`` when there are none / the file is unreadable."""
    path = Path(settings.runtime_settings_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable runtime settings %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def write(settings: Settings, patch: dict) -> Optional[str]:
    """Merge ``patch`` into the persisted overrides.  Returns a warning text when saving failed (the change
    is applied to the running process regardless)."""
    path = Path(settings.runtime_settings_path)
    data = read(settings)
    data.update(patch)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        warning = f"applied, but could not save to {path} ({exc}); the change is lost on restart"
        log.warning(warning)
        return warning
    return None


def load_operation_overrides(settings: Settings) -> None:
    """Apply persisted concurrency / session timeout overrides on top of the environment (at startup,
    before the session store and the runner are built)."""
    data = {k: v for k, v in read(settings).items() if k in OperationSettings.model_fields}
    if not data:
        return
    try:
        override = OperationSettings.model_validate({
            "max_concurrent_jobs": data.get("max_concurrent_jobs", settings.max_concurrent_jobs),
            "session_ttl_s": data.get("session_ttl_s", settings.session_ttl_s),
        })
    except ValueError as exc:
        log.warning("ignoring invalid runtime settings %s: %s", settings.runtime_settings_path, exc)
        return
    settings.max_concurrent_jobs = override.max_concurrent_jobs
    settings.session_ttl_s = override.session_ttl_s
    log.info("runtime settings loaded: %s", override.model_dump())


def current(settings: Settings) -> OperationStatus:
    # model_construct: values set through the environment may lie outside the Setup page's bounds
    return OperationStatus.model_construct(
        max_concurrent_jobs=int(settings.max_concurrent_jobs), session_ttl_s=int(settings.session_ttl_s),
        persisted=Path(settings.runtime_settings_path).exists(),
    )
