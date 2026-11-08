"""Bounded local repository check history, with no secrets or raw exception messages."""

from __future__ import annotations

import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github_backup.models import BackupResult

HISTORY = Path("/cache/backup-history/history.sqlite3")
LIMIT = 250


def record_backup(result: BackupResult) -> None:
    if result.resource.kind != "repository":
        return
    repo = result.resource
    if (
        not re.fullmatch(r"[0-9]{1,30}", repo.identifier)
        or not 1 <= len(repo.owner) <= 100
        or not 1 <= len(repo.name) <= 100
        or result.status not in ("success", "failed", "partial", "skipped")
    ):
        raise ValueError("Invalid repository history record")
    started = datetime.fromisoformat(result.started_at).astimezone(UTC).isoformat()
    finished = datetime.fromisoformat(result.finished_at).astimezone(UTC).isoformat()
    duration = result.duration_seconds if math.isfinite(result.duration_seconds) else 0
    HISTORY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(HISTORY, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    if HISTORY.stat().st_mode & 0o077:
        raise ValueError("History database must be private")
    with closing(sqlite3.connect(HISTORY, timeout=1)) as db, db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute(
            "CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY, "
            "repository_id TEXT, owner TEXT, repository TEXT, status TEXT, started TEXT, "
            "finished TEXT, duration REAL, changed INTEGER)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS results_finished ON results(finished DESC, id DESC)")
        db.execute(
            "INSERT INTO results (repository_id,owner,repository,status,started,finished,"
            "duration,changed) VALUES (?,?,?,?,?,?,?,?)",
            (
                repo.identifier,
                repo.owner,
                repo.name,
                result.status,
                started,
                finished,
                max(0, duration),
                int(result.changed),
            ),
        )
        db.execute(
            "DELETE FROM results WHERE id NOT IN "
            "(SELECT id FROM results ORDER BY finished DESC, id DESC LIMIT ?)",
            (LIMIT,),
        )


def recent_backups() -> list[dict[str, Any]]:
    if not HISTORY.exists():
        return []
    if HISTORY.stat().st_mode & 0o077:
        raise ValueError("History database must be private")
    with closing(sqlite3.connect(f"file:{HISTORY}?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM results ORDER BY finished DESC, id DESC LIMIT ?", (LIMIT,)
            )
        ]
