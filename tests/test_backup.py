from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from github_backup.backup import BackupService
from github_backup.config import BackupConfig
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner
from github_backup.models import Resource
from github_backup.storage import FilesystemStorage


class FakeGitHub:
    def __init__(self, resource: Resource) -> None:
        self.resource = resource

    def authenticated_user(self) -> str:
        return "octocat"

    def repositories(self, scope: str, organizations: tuple[str, ...]) -> list[Resource]:
        return [self.resource]

    def gists(self, authenticated_user: str) -> list[Resource]:
        return []

    def get_json(self, path: str) -> dict[str, str]:
        return {"path": path}

    def paginate(
        self,
        path: str,
        parameters: object = None,
        *,
        list_key: str | None = None,
    ) -> object:
        del path, parameters, list_key
        return iter([])


class BackupServiceTests(unittest.TestCase):
    def test_run_writes_atomic_manifest_and_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "data.txt").write_text("backup me\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "data.txt"], check=True)
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
                    "data",
                ],
                check=True,
            )
            resource = Resource(
                kind="repository",
                identifier="1",
                name="source",
                owner="octocat",
                clone_url=source.as_uri(),
                html_url="https://example.test/octocat/source",
                private=True,
            )
            config = BackupConfig(destination=destination, include_gists=False).validated()
            service = BackupService(
                config,
                cast(GitHubClient, FakeGitHub(resource)),
                GitRunner("secret", timeout_seconds=30),
                FilesystemStorage(config),
            )
            manifest, location = service.run()

            self.assertEqual(manifest.counts["success"], 1)
            self.assertTrue(Path(location).is_file())
            self.assertTrue((destination / "mirrors/repos/octocat/source.git/HEAD").is_file())
            latest = json.loads((destination / "manifests/latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["counts"]["failed"], 0)

    def test_run_publishes_optional_metadata_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "data.txt").write_text("backup me\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "data.txt"], check=True)
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
                    "data",
                ],
                check=True,
            )
            resource = Resource(
                kind="repository",
                identifier="1",
                name="source",
                owner="octocat",
                clone_url=source.as_uri(),
                html_url="https://example.test/octocat/source",
                private=True,
            )
            config = BackupConfig(
                destination=destination,
                include_gists=False,
                additional_data=("metadata",),
            ).validated()
            service = BackupService(
                config,
                cast(GitHubClient, FakeGitHub(resource)),
                GitRunner("secret", timeout_seconds=30),
                FilesystemStorage(config),
            )

            manifest, _ = service.run()

            self.assertEqual(manifest.counts["success"], 2)
            self.assertEqual(manifest.components[0].component, "metadata")
            self.assertTrue(Path(manifest.components[0].artifact or "").is_file())


if __name__ == "__main__":
    unittest.main()
