from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from github_backup.errors import GitCommandError
from github_backup.gitops import GitRunner


class GitRunnerTests(unittest.TestCase):
    def test_mirror_is_incrementally_updated_and_bundle_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            mirror = root / "backup.git"
            bundle = root / "snapshot.bundle"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "README.md").write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-q",
                    "-m",
                    "first",
                ],
                check=True,
            )
            runner = GitRunner("super-secret-token", timeout_seconds=30)
            changed, first_digest = runner.update_mirror(source.as_uri(), mirror)
            unchanged, second_digest = runner.update_mirror(source.as_uri(), mirror)
            runner.create_bundle(mirror, bundle)

            self.assertTrue(changed)
            self.assertFalse(unchanged)
            self.assertEqual(first_digest, second_digest)
            self.assertTrue(bundle.is_file())
            config = (mirror / "config").read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token", config)

    def test_authenticated_http_remote_is_rejected_without_retry(self) -> None:
        sleeper = Mock()
        runner = GitRunner("secret", timeout_seconds=30, sleeper=sleeper)
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(GitCommandError, "must use HTTPS"),
        ):
            runner.update_mirror("http://github.example/owner/repo.git", Path(directory) / "x")
        sleeper.assert_not_called()

    def test_authenticated_git_disables_inherited_http_tracing(self) -> None:
        completed = subprocess.CompletedProcess(["git", "--version"], 0, "git version test\n", "")
        runner = GitRunner("secret", timeout_seconds=30)
        with (
            patch.dict(os.environ, {"GIT_TRACE_CURL": "1"}),
            patch("github_backup.gitops.subprocess.run", return_value=completed) as run,
        ):
            runner._run(
                ["git", "--version"],
                authenticated_url="https://github.example/owner/repo.git",
            )
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("GIT_TRACE_CURL", environment)
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "false")


if __name__ == "__main__":
    unittest.main()
