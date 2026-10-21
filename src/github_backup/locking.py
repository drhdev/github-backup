"""Cross-process lock used to prevent overlapping backup runs."""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import AbstractContextManager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from github_backup.errors import BackupError


class BackupLock(AbstractContextManager["BackupLock"]):
    def __init__(self, path: Path, *, wait_seconds: int = 0) -> None:
        self._path = path
        self._wait_seconds = wait_seconds
        self._acquired = False

    def __enter__(self) -> BackupLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._wait_seconds
        while True:
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise BackupError(
                        f"another backup appears to be running ({self._path})"
                    ) from None
                time.sleep(min(1.0, max(deadline - time.monotonic(), 0.1)))
                continue
            payload = {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "created_at": datetime.now(UTC).isoformat(),
            }
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            self._acquired = True
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False

    def _remove_stale_lock(self) -> bool:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            hostname = str(payload["hostname"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
        if hostname != socket.gethostname() or _pid_exists(pid):
            return False
        with suppress(FileNotFoundError):
            self._path.unlink()
        return True


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
