from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any

from github_backup.errors import GitHubAPIError
from github_backup.models import Resource
from github_backup.supplemental import SupplementalExporter


def _resource() -> Resource:
    return Resource(
        kind="repository",
        identifier="1",
        name="project",
        owner="owner",
        clone_url="https://github.example/owner/project.git",
        html_url="https://github.example/owner/project",
        private=True,
    )


class FakeGitHub:
    def get_json(self, path: str) -> dict[str, str]:
        return {"full_name": "owner/project", "url": path}

    def paginate(
        self,
        path: str,
        parameters: object = None,
        *,
        list_key: str | None = None,
    ) -> Any:
        del parameters, list_key
        if "codespaces/secrets" in path:
            raise GitHubAPIError("not available", status_code=403)
        if path.endswith("/pulls?state=all"):
            return iter([{"number": 7}])
        if path.endswith("/releases"):
            return iter(
                [
                    {
                        "id": 2,
                        "assets": [
                            {
                                "id": 3,
                                "name": "build/unsafe.zip",
                                "url": "/repos/owner/project/releases/assets/3",
                            }
                        ],
                    }
                ]
            )
        if path.endswith("/actions/artifacts"):
            return iter([{"id": 4, "expired": False}, {"id": 5, "expired": True}])
        if "/actions/runs" in path:
            return iter([{"id": 6}])
        return iter([{"id": 1, "path": path}])

    def download(self, path: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"download:{path}".encode())
        return destination.stat().st_size


class FakeGit:
    def fetch_lfs(self, mirror: Path, clone_url: str) -> Path:
        del mirror, clone_url
        objects = self.root / "objects"
        objects.mkdir(parents=True)
        (objects / "payload").write_bytes(b"lfs")
        return objects


class SupplementalExporterTests(unittest.TestCase):
    def _export(self, component: str) -> tuple[Path, Any, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        git = FakeGit()
        git.root = root
        exporter = SupplementalExporter(FakeGitHub(), git, root / "artifacts")  # type: ignore[arg-type]
        result = exporter.export(_resource(), component, root / "mirror.git")
        return root, result, temporary

    def test_metadata_archive_records_permission_gaps_and_pull_reviews(self) -> None:
        root, result, temporary = self._export("metadata")
        self.addCleanup(temporary.cleanup)
        self.assertIn("codespaces-secrets", result.unavailable)
        del root
        with tarfile.open(result.path, "r:gz") as archive:
            coverage_file = archive.extractfile("metadata/coverage.json")
            assert coverage_file is not None
            coverage = json.load(coverage_file)
            names = archive.getnames()
        self.assertIn("pull-reviews/7", coverage["exported"])
        self.assertIn("metadata/pull-reviews.jsonl", names)

    def test_release_assets_are_downloaded_with_safe_names(self) -> None:
        _root, result, temporary = self._export("releases")
        self.addCleanup(temporary.cleanup)
        with tarfile.open(result.path, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("releases/assets/3-build_unsafe.zip", names)

    def test_actions_export_includes_live_artifacts_and_logs(self) -> None:
        _root, result, temporary = self._export("actions")
        self.addCleanup(temporary.cleanup)
        with tarfile.open(result.path, "r:gz") as archive:
            names = archive.getnames()
            coverage_file = archive.extractfile("actions/coverage.json")
            assert coverage_file is not None
            coverage = json.load(coverage_file)
        self.assertIn("actions/artifacts/4.zip", names)
        self.assertIn("actions/logs/6.zip", names)
        self.assertIn("actions/artifact/5:expired", coverage["skipped"])

    def test_lfs_export_contains_objects(self) -> None:
        _root, result, temporary = self._export("lfs")
        self.addCleanup(temporary.cleanup)
        with tarfile.open(result.path, "r:gz") as archive:
            self.assertIn("lfs/objects/payload", archive.getnames())


if __name__ == "__main__":
    unittest.main()
