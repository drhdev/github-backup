from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from github_backup.errors import StorageError
from github_backup.security import prepare_private_directory, private_umask, require_free_space


class SecurityTests(unittest.TestCase):
    def test_private_umask_protects_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory, private_umask():
            path = Path(directory) / "secret"
            path.write_text("private", encoding="utf-8")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_insecure_existing_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup"
            path.mkdir(mode=0o755)
            os.chmod(path, 0o755)
            with self.assertRaisesRegex(StorageError, "permissions are too broad"):
                prepare_private_directory(path, allow_insecure=False)
            prepare_private_directory(path, allow_insecure=True)

    def test_free_space_preflight_fails_for_impossible_requirement(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(StorageError, "insufficient free space"),
        ):
            require_free_space(Path(directory), 10**9)
