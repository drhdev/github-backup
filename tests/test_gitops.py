from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
