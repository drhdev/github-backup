"""Local data-protection helpers."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from github_backup.errors import StorageError


@contextmanager
def private_umask() -> Iterator[None]:
    """Ensure files created by Git and Python are private to the service user."""

    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def prepare_private_directory(path: Path, *, allow_insecure: bool) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if allow_insecure:
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise StorageError(
            f"backup directory permissions are too broad ({mode:#05o}): {path}; "
            "use chmod 700 or explicitly set allow_insecure_permissions=true"
        )


def require_free_space(path: Path, minimum_gib: float) -> None:
    if minimum_gib <= 0:
        return
    available = shutil.disk_usage(path).free
    required = int(minimum_gib * 1024**3)
    if available < required:
        raise StorageError(
            f"insufficient free space at {path}: {available / 1024**3:.2f} GiB available, "
            f"{minimum_gib:.2f} GiB required"
        )
