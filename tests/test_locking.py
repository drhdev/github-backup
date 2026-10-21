from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from github_backup.errors import BackupError
from github_backup.locking import BackupLock


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
