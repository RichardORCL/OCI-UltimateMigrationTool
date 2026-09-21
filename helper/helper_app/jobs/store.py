"""SQLite backed job store (thread safe, JSON documents)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helper_app.models import Job, JobPhase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                   id TEXT PRIMARY KEY,
                   vm_moid TEXT NOT NULL,
                   phase TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   data TEXT NOT NULL
               )"""
        )

    def put(self, job: Job) -> Job:
        job.updated_at = utcnow()
        if job.phase.terminal and job.finished_at is None:
            job.finished_at = job.updated_at
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs(id, vm_moid, phase, created_at, updated_at, data) VALUES (?,?,?,?,?,?)",
                (job.id, job.source_key, job.phase.value, job.created_at.isoformat(), job.updated_at.isoformat(),
                 job.model_dump_json(exclude={"summary"})),  # summary is derived on read
            )
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def delete_finished(self, phases: Optional[set[JobPhase]] = None) -> int:
        """Delete the records of finished jobs (all terminal phases, or only ``phases``); jobs still in
        flight are never removed.  Returns the number of deleted records."""
        wanted = {p for p in (phases or set(JobPhase)) if p.terminal}
        if not wanted:
            return 0
        marks = ",".join("?" * len(wanted))
        with self._lock:
            cur = self._conn.execute(f"DELETE FROM jobs WHERE phase IN ({marks})", [p.value for p in wanted])
        return cur.rowcount

    def list(self, vm_moid: Optional[str] = None, limit: int = 200) -> list[Job]:
        with self._lock:
            if vm_moid:
                rows = self._conn.execute(
                    "SELECT data FROM jobs WHERE vm_moid = ? ORDER BY created_at DESC LIMIT ?", (vm_moid, limit)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def active(self) -> list[Job]:
        return [job for job in self.list(limit=10000) if not job.phase.terminal]

    def active_for_vm(self, vm_moid: str) -> Optional[Job]:
        for job in self.list(vm_moid=vm_moid):
            if not job.phase.terminal:
                return job
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
