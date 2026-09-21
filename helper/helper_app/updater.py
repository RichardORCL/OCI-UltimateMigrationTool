"""Self-update of a source-installed helper.

The Terraform/cloud-init deployment clones the git repository to ``/opt/vc-oci/src`` and installs the
``helper`` package into ``/opt/vc-oci/venv``.  Updating means: fetch the branch that is checked out,
reset to its remote head, ``pip install`` it again and restart the systemd service.

The update runs in a *transient systemd unit* (``systemd-run``) so that restarting the helper service
does not kill the update itself.  Progress is appended to a log file that the Setup page shows.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

import httpx
from pydantic import BaseModel

from helper_app import __version__
from helper_app.config import Settings

log = logging.getLogger(__name__)

UPDATE_UNIT = "vc-oci-helper-update"
Runner = Callable[[list[str], float], tuple[int, str]]


class UpdateError(RuntimeError):
    pass


class SoftwareStatus(BaseModel):
    version: str
    install_method: str  # source | none
    source_dir: str = ""
    repo_url: str = ""  # browsable repository URL, if the origin is on GitHub
    branch: str = ""
    commit: str = ""
    commit_date: str = ""
    commit_subject: str = ""
    latest_commit: str = ""
    latest_date: str = ""
    latest_subject: str = ""
    update_available: Optional[bool] = None  # None: could not check
    check_error: str = ""
    update_running: bool = False
    active_jobs: int = 0
    can_update: bool = False
    can_force: bool = False  # source install, idle updater, but jobs are running
    reason: str = ""
    log: str = ""


def _default_runner(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(args)}: timed out"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def github_repo(remote_url: str) -> Optional[tuple[str, str]]:
    """``(owner, repo)`` for https:// or ssh GitHub remotes, else None."""
    m = re.match(r"^(?:https?://|git@|ssh://git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url.strip())
    return (m.group(1), m.group(2)) if m else None


class Updater:
    def __init__(self, settings: Settings, runner: Runner = _default_runner, http_get=None):
        self.src = Path(settings.update_source_dir)
        self.venv = Path(settings.update_venv_dir)
        self.service = settings.update_service
        self.log_path = Path(settings.update_log_path)
        self._run = runner
        self._http_get = http_get or (lambda url, headers: httpx.get(url, headers=headers, timeout=10.0,
                                                                     follow_redirects=True))
        self._background: Optional[subprocess.Popen] = None  # fallback when systemd-run is unavailable

    # ------------------------------------------------------------------ local
    @property
    def install_method(self) -> str:
        return "source" if (self.src / ".git").exists() else "none"

    def _git(self, *args: str, timeout: float = 30.0) -> Optional[str]:
        rc, out = self._run(["git", "-C", str(self.src), *args], timeout)
        return out.strip() if rc == 0 else None

    def local_state(self) -> dict:
        if self.install_method != "source":
            return {}
        branch = self._git("symbolic-ref", "--short", "-q", "HEAD") or ""
        remote = self._git("remote", "get-url", "origin") or ""
        show = self._git("log", "-1", "--format=%H%x00%cI%x00%s") or ""
        commit, date, subject = (show.split("\x00") + ["", "", ""])[:3]
        gh = github_repo(remote)
        return {
            "source_dir": str(self.src),
            "branch": branch,
            "remote": remote,
            "repo_url": f"https://github.com/{gh[0]}/{gh[1]}" if gh else "",
            "commit": commit,
            "commit_date": date,
            "commit_subject": subject,
        }

    # ----------------------------------------------------------------- remote
    def remote_state(self, local: dict) -> dict:
        """Head of the tracked branch: GitHub API when possible (gives date/subject), else ``git ls-remote``."""
        branch = local.get("branch") or ""
        remote = local.get("remote") or ""
        if not branch:
            return {"check_error": "the checkout is not on a branch (fixed tag or commit); nothing to track"}
        gh = github_repo(remote)
        if gh:
            try:
                resp = self._http_get(f"https://api.github.com/repos/{gh[0]}/{gh[1]}/commits/{branch}",
                                      {"Accept": "application/vnd.github+json", "User-Agent": "vc-oci-helper"})
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "latest_commit": data["sha"],
                        "latest_date": data["commit"]["committer"]["date"],
                        "latest_subject": data["commit"]["message"].splitlines()[0],
                    }
                log.info("GitHub API returned %s; falling back to git ls-remote", resp.status_code)
            except Exception as exc:  # noqa: BLE001
                log.info("GitHub API unreachable (%s); falling back to git ls-remote", exc)
        out = self._git("ls-remote", "origin", f"refs/heads/{branch}", timeout=60.0)
        if not out:
            return {"check_error": f"cannot reach {remote or 'the git remote'}"}
        return {"latest_commit": out.split()[0]}

    # ----------------------------------------------------------------- status
    def running(self) -> bool:
        if self._background is not None:
            if self._background.poll() is None:
                return True
            self._background = None
        rc, out = self._run(["systemctl", "is-active", UPDATE_UNIT], 10.0)
        return rc == 0 or out.strip() in ("active", "activating")

    def log_tail(self, lines: int = 80) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def status(self, active_jobs: int = 0, check_remote: bool = True) -> SoftwareStatus:
        method = self.install_method
        st = SoftwareStatus(version=__version__, install_method=method, active_jobs=active_jobs)
        if method == "source":
            local = self.local_state()
            st.source_dir, st.repo_url, st.branch = local["source_dir"], local["repo_url"], local["branch"]
            st.commit, st.commit_date = local["commit"], local["commit_date"]
            st.commit_subject = local["commit_subject"]
            if check_remote:
                remote = self.remote_state(local)
                st.check_error = remote.get("check_error", "")
                st.latest_commit = remote.get("latest_commit", "")
                st.latest_date = remote.get("latest_date", "")
                st.latest_subject = remote.get("latest_subject", "")
                if st.latest_commit and st.commit:
                    st.update_available = st.latest_commit != st.commit
        st.update_running = self.running()
        st.log = self.log_tail()
        if method != "source":
            st.reason = f"No git checkout at {self.src}; the migration tool was not installed from source."
        elif st.update_running:
            st.reason = "An update is running."
        elif active_jobs:
            st.reason = f"{active_jobs} migration(s) running; the restart would abort them."
            st.can_force = True
        else:
            st.can_update = True
        return st

    # ----------------------------------------------------------------- update
    def build_script(self, branch: str) -> str:
        src, venv, logf = shlex.quote(str(self.src)), shlex.quote(str(self.venv)), shlex.quote(str(self.log_path))
        steps = [f"git -C {src} fetch --tags --force origin"]
        if branch:
            b = shlex.quote(branch)
            steps += [f"git -C {src} checkout --force {b}", f"git -C {src} reset --hard origin/{b}"]
        steps += [
            f"{venv}/bin/pip install --quiet --upgrade {src}/helper",
            f'echo "== $(date -Is) restarting {self.service}"',
            f"systemctl restart {shlex.quote(self.service)}",
            'echo "== $(date -Is) update finished"',
        ]
        # steps are chained with && (set -e is suppressed inside a { } group used as an || operand)
        body = " &&\n".join(steps)
        return (
            f"mkdir -p $(dirname {logf})\n"
            "{\n"
            f'echo "== $(date -Is) update started ({branch or "fixed ref"})" &&\n'
            f"{body}\n"
            f'}} >>{logf} 2>&1 || {{ rc=$?; echo "== $(date -Is) UPDATE FAILED (exit $rc)" >>{logf}; }}\n'
        )

    def start(self, active_jobs: int = 0, force: bool = False) -> None:
        st = self.status(active_jobs=active_jobs, check_remote=False)
        if not st.can_update and not (force and st.install_method == "source" and not st.update_running):
            raise UpdateError(st.reason or "update not possible")
        script = self.build_script(st.branch)
        log.warning("starting self-update of %s (%s @ %s)", self.service, st.branch or "fixed ref", st.commit[:12])
        rc, out = self._run(
            ["systemd-run", "--unit", UPDATE_UNIT, "--description", "vc-oci-helper self-update", "--collect",
             "--quiet", "/bin/bash", "-c", script],
            30.0,
        )
        if rc == 0:
            return
        if rc == 127:  # no systemd (development): detach a plain process instead
            log.warning("systemd-run unavailable; running the update as a detached process")
            self._background = subprocess.Popen(["/bin/bash", "-c", script], start_new_session=True,
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
            return
        if "already exists" in out or "already loaded" in out:
            raise UpdateError("an update is already running")
        raise UpdateError(f"cannot start the update: {out.strip() or rc}")
