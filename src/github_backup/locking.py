"""Cross-process lock used to prevent overlapping backup runs."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import AbstractContextManager, ExitStack, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from github_backup.errors import BackupError, StorageError


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


class CombinedLock(AbstractContextManager["CombinedLock"]):
    def __init__(self, *locks: AbstractContextManager[object]) -> None:
        self._locks = locks
        self._stack: ExitStack | None = None

    def __enter__(self) -> CombinedLock:
        stack = ExitStack()
        try:
            for lock in self._locks:
                stack.enter_context(lock)
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._stack is None:
            return None
        return self._stack.__exit__(exc_type, exc_value, traceback)


class S3LeaseLock(AbstractContextManager["S3LeaseLock"]):
    """Renewable S3 lease using conditional writes and ETag compare-and-swap."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        key: str,
        lease_seconds: int,
        wait_seconds: int,
        put_options: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self._lease_seconds = lease_seconds
        self._wait_seconds = wait_seconds
        self._put_options = put_options or {}
        self._token = uuid.uuid4().hex
        self._etag: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_error: Exception | None = None
        self._state_lock = threading.Lock()

    def __enter__(self) -> S3LeaseLock:
        deadline = time.monotonic() + self._wait_seconds
        while True:
            if self._acquire_once():
                self._thread = threading.Thread(
                    target=self._heartbeat_loop,
                    name="github-backup-s3-lock",
                    daemon=True,
                )
                self._thread.start()
                return self
            if time.monotonic() >= deadline:
                raise BackupError(
                    f"another backup holds s3://{self._bucket}/{self._key}"
                ) from None
            time.sleep(min(1.0, max(deadline - time.monotonic(), 0.1)))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        release_error: Exception | None = self._heartbeat_error
        with self._state_lock:
            etag = self._etag
        if etag is not None:
            try:
                self._client.delete_object(
                    Bucket=self._bucket,
                    Key=self._key,
                    IfMatch=etag,
                )
            except Exception as error:
                release_error = error
        if release_error is not None and exc_type is None:
            raise StorageError(f"lost or could not release S3 backup lease: {release_error}")

    def _acquire_once(self) -> bool:
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=self._body(),
                ContentType="application/json",
                IfNoneMatch="*",
                **self._put_options,
            )
        except Exception as error:
            if _s3_error_code(error) not in ("409", "412", "PreconditionFailed"):
                raise StorageError(f"could not acquire S3 backup lease: {error}") from error
            return self._acquire_once() if self._remove_expired_lease() else False
        self._etag = str(response["ETag"])
        return True

    def _remove_expired_lease(self) -> bool:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key)
            payload = json.loads(response["Body"].read())
            expires_at = float(payload["expires_at"])
            etag = str(response["ETag"])
        except Exception as error:
            raise StorageError(f"could not inspect existing S3 backup lease: {error}") from error
        if expires_at >= time.time():
            return False
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._key, IfMatch=etag)
        except Exception as error:
            if _s3_error_code(error) not in ("409", "412", "PreconditionFailed"):
                raise StorageError(f"could not clear expired S3 backup lease: {error}") from error
            return False
        return True

    def _heartbeat_loop(self) -> None:
        interval = max(self._lease_seconds / 3, 20.0)
        while not self._stop.wait(interval):
            with self._state_lock:
                etag = self._etag
            if etag is None:
                return
            try:
                response = self._client.put_object(
                    Bucket=self._bucket,
                    Key=self._key,
                    Body=self._body(),
                    ContentType="application/json",
                    IfMatch=etag,
                    **self._put_options,
                )
                with self._state_lock:
                    self._etag = str(response["ETag"])
            except Exception as error:
                self._heartbeat_error = error
                return

    def _body(self) -> bytes:
        return json.dumps(
            {
                "token": self._token,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "expires_at": time.time() + self._lease_seconds,
            },
            sort_keys=True,
        ).encode("utf-8")


def _s3_error_code(error: Exception) -> str:
    response = getattr(error, "response", {})
    return str(response.get("Error", {}).get("Code", ""))
