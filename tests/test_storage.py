from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from github_backup.cli import _verify_local
from github_backup.config import BackupConfig, S3Config
from github_backup.gitops import GitRunner
from github_backup.models import ComponentResult, Resource, RunManifest
from github_backup.storage import (
    FilesystemStorage,
    S3Storage,
    StorageBackend,
    cleanup_empty_artifact_directories,
    create_storage,
)


def _resource(source: Path, name: str = "project") -> Resource:
    return Resource(
        kind="repository",
        identifier="1",
        name=name,
        owner="owner",
        clone_url=source.as_uri(),
        html_url=f"https://example.test/owner/{name}",
        private=True,
    )


class FilesystemStorageTests(unittest.TestCase):
    def test_snapshot_bundle_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "file.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "file.txt"], check=True)
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
                    "content",
                ],
                check=True,
            )
            config = BackupConfig(destination=root / "backup", mode="snapshot").validated()
            storage = FilesystemStorage(config)
            git = GitRunner("secret", timeout_seconds=30)
            resource = _resource(source)
            mirror = storage.mirror_path(resource)
            changed, digest = git.update_mirror(resource.clone_url, mirror)
            artifact = storage.publish(resource, mirror, digest, changed, "snapshot", "run-id", git)

            self.assertTrue(Path(artifact).is_file())
            self.assertTrue(artifact.endswith(".bundle"))
            metadata = json.loads(
                (config.destination / "metadata/repos/owner/project.json").read_text()
            )
            self.assertEqual(metadata["ref_digest"], digest)
            output = io.StringIO()
            os.chmod(config.destination, 0o700)
            with redirect_stdout(output):
                _verify_local(config)
            self.assertIn("Verified 1 mirrors, 1 bundles", output.getvalue())

    def test_empty_repository_snapshot_uses_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty"
            subprocess.run(["git", "init", "-q", "--bare", str(source)], check=True)
            config = BackupConfig(destination=root / "backup", mode="snapshot").validated()
            storage = FilesystemStorage(config)
            git = GitRunner("secret", timeout_seconds=30)
            resource = _resource(source, "empty")
            mirror = storage.mirror_path(resource)
            changed, digest = git.update_mirror(resource.clone_url, mirror)
            artifact = Path(
                storage.publish(resource, mirror, digest, changed, "snapshot", "run-id", git)
            )

            self.assertTrue(artifact.name.endswith(".empty.tar.gz"))
            with tarfile.open(artifact, "r:gz") as archive:
                self.assertTrue(any(name.endswith("/HEAD") for name in archive.getnames()))
            os.chmod(config.destination, 0o700)
            output = io.StringIO()
            with redirect_stdout(output):
                _verify_local(config)
            self.assertIn("1 empty-repository archives", output.getvalue())

    def test_manifest_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BackupConfig(destination=root / "backup").validated()
            storage = create_storage(config)
            manifest = RunManifest(
                run_id="run-id",
                user="owner",
                storage="filesystem",
                scope="accessible",
                mode="incremental",
                started_at=datetime.now(UTC).isoformat(),
            )
            manifest.finish()
            location = Path(storage.write_manifest(manifest))
            self.assertTrue(location.is_file())
            self.assertTrue((config.destination / "manifests/latest.json").is_file())

            artifacts = root / "artifacts/a/b"
            artifacts.mkdir(parents=True)
            cleanup_empty_artifact_directories(root / "artifacts")
            self.assertFalse((root / "artifacts").exists())

    def test_snapshot_retention_removes_only_oldest_successful_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BackupConfig(
                destination=root / "backup", mode="snapshot", snapshot_retention=2
            ).validated()
            storage = FilesystemStorage(config)
            for run_id in ("20260101", "20260102", "20260103"):
                (config.destination / "snapshots" / run_id).mkdir(parents=True)
            manifest = RunManifest(
                run_id="20260103",
                user="owner",
                storage="filesystem",
                scope="accessible",
                mode="snapshot",
                started_at=datetime.now(UTC).isoformat(),
            )
            manifest.finish()
            storage.apply_retention(manifest)
            remaining = sorted(path.name for path in (config.destination / "snapshots").iterdir())
            self.assertEqual(remaining, ["20260102", "20260103"])

    def test_snapshot_retention_does_not_run_after_partial_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BackupConfig(
                destination=root / "backup", mode="snapshot", snapshot_retention=1
            ).validated()
            storage = FilesystemStorage(config)
            for run_id in ("20260101", "20260102"):
                (config.destination / "snapshots" / run_id).mkdir(parents=True)
            now = datetime.now(UTC).isoformat()
            manifest = RunManifest(
                run_id="20260102",
                user="owner",
                storage="filesystem",
                scope="accessible",
                mode="snapshot",
                started_at=now,
                components=[
                    ComponentResult(
                        repository="owner/project",
                        component="metadata",
                        status="partial",
                        started_at=now,
                        finished_at=now,
                        duration_seconds=0.1,
                    )
                ],
            )
            manifest.finish()
            storage.apply_retention(manifest)
            remaining = sorted(path.name for path in (config.destination / "snapshots").iterdir())
            self.assertEqual(remaining, ["20260101", "20260102"])


class FakeS3Client:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.puts: list[dict[str, object]] = []
        self.uploads: list[tuple[str, str, str, dict[str, object]]] = []
        self.keys: set[str] = set()
        self.deleted: list[str] = []

    def put_object(self, **values: object) -> None:
        self.puts.append(values)

    def head_object(self, **values: object) -> dict[str, object]:
        return {"Metadata": {"ref-digest": self.digest}}

    def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: dict[str, object]) -> None:
        self.uploads.append((path, bucket, key, ExtraArgs))

    def list_objects_v2(self, **values: object) -> dict[str, object]:
        prefix = str(values["Prefix"])
        return {
            "Contents": [{"Key": key} for key in sorted(self.keys) if key.startswith(prefix)],
            "IsTruncated": False,
        }

    def delete_objects(self, **values: object) -> None:
        delete = values["Delete"]
        if not isinstance(delete, dict):
            raise AssertionError("Delete must be a mapping")
        for item in delete["Objects"]:
            key = str(item["Key"])
            self.deleted.append(key)
            self.keys.discard(key)


class S3StorageTests(unittest.TestCase):
    def test_unchanged_incremental_artifact_is_not_uploaded_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            (source / "file.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "file.txt"], check=True)
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
                    "content",
                ],
                check=True,
            )
            config = BackupConfig(
                storage="s3",
                cache_dir=root / "cache",
                s3=S3Config(bucket="bucket", prefix="prefix"),
            ).validated()
            storage = object.__new__(S3Storage)
            StorageBackend.__init__(storage, config)
            git = GitRunner("secret", timeout_seconds=30)
            resource = _resource(source)
            mirror = storage.mirror_path(resource)
            _, digest = git.update_mirror(resource.clone_url, mirror)
            client = FakeS3Client(digest)
            storage._client = client

            artifact = storage.publish(
                resource, mirror, digest, False, "incremental", "run-id", git
            )
            self.assertEqual(artifact, "s3://bucket/prefix/latest/repos/owner/project.git.bundle")
            self.assertEqual(client.uploads, [])
            self.assertEqual(len(client.puts), 1)

    def test_snapshot_retention_deletes_old_s3_run_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = BackupConfig(
                storage="s3",
                mode="snapshot",
                snapshot_retention=1,
                cache_dir=Path(directory),
                s3=S3Config(bucket="bucket", prefix="prefix"),
            ).validated()
            storage = object.__new__(S3Storage)
            StorageBackend.__init__(storage, config)
            client = FakeS3Client("digest")
            client.keys = {
                "prefix/snapshots/20260101/repos/o/a.git.bundle",
                "prefix/snapshots/20260102/repos/o/a.git.bundle",
            }
            storage._client = client
            manifest = RunManifest(
                run_id="20260102",
                user="owner",
                storage="s3",
                scope="accessible",
                mode="snapshot",
                started_at=datetime.now(UTC).isoformat(),
            )
            manifest.finish()
            storage.apply_retention(manifest)
            self.assertEqual(
                client.deleted,
                ["prefix/snapshots/20260101/repos/o/a.git.bundle"],
            )
