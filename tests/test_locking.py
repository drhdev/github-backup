from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from github_backup.errors import BackupError
from github_backup.locking import BackupLock, S3LeaseLock


class ConditionalFailure(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "PreconditionFailed"}}


class MemoryS3:
    def __init__(self) -> None:
        self.body: bytes | None = None
        self.etag: str | None = None
        self.generation = 0

    def put_object(self, **values: object) -> dict[str, str]:
        if values.get("IfNoneMatch") == "*" and self.body is not None:
            raise ConditionalFailure
        if "IfMatch" in values and values["IfMatch"] != self.etag:
            raise ConditionalFailure
        self.generation += 1
        self.etag = f'"{self.generation}"'
        body = values["Body"]
        self.body = body if isinstance(body, bytes) else str(body).encode()
        return {"ETag": self.etag}

    def get_object(self, **values: object) -> dict[str, object]:
        if self.body is None or self.etag is None:
            raise AssertionError("object does not exist")
        return {"Body": io.BytesIO(self.body), "ETag": self.etag}

    def delete_object(self, **values: object) -> None:
        if values.get("IfMatch") != self.etag:
            raise ConditionalFailure
        self.body = None
        self.etag = None


class BackupLockTests(unittest.TestCase):
    def test_lock_prevents_concurrent_run_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.lock"
            with BackupLock(path):
                self.assertTrue(path.is_file())
                with self.assertRaises(BackupError), BackupLock(path):
                    self.fail("second lock must not be acquired")
            self.assertFalse(path.exists())

    def test_stale_local_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.lock"
            path.write_text(
                json.dumps({"pid": 2**30, "hostname": socket.gethostname()}), encoding="utf-8"
            )
            with BackupLock(path):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())

    def test_malformed_lock_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.lock"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(BackupError), BackupLock(path):
                self.fail("malformed lock should require operator review")

    def test_s3_lease_prevents_overlap_and_releases_conditionally(self) -> None:
        client = MemoryS3()
        first = S3LeaseLock(
            client,
            bucket="bucket",
            key="locks/run.lock",
            lease_seconds=60,
            wait_seconds=0,
        )
        second = S3LeaseLock(
            client,
            bucket="bucket",
            key="locks/run.lock",
            lease_seconds=60,
            wait_seconds=0,
        )
        with first, self.assertRaisesRegex(BackupError, "another backup holds"):
            second.__enter__()
        with second:
            self.assertIsNotNone(client.body)
        self.assertIsNone(client.body)

    def test_expired_s3_lease_is_replaced_without_waiting(self) -> None:
        client = MemoryS3()
        client.put_object(
            Bucket="bucket",
            Key="locks/run.lock",
            Body=b'{"expires_at":0}',
            IfNoneMatch="*",
        )
        lock = S3LeaseLock(
            client,
            bucket="bucket",
            key="locks/run.lock",
            lease_seconds=60,
            wait_seconds=0,
        )
        with lock:
            self.assertIn(b'"token"', client.body or b"")
