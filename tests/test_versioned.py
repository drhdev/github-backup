"""Real Git recovery against a small in-memory S3 transport."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from github_backup.config import BackupConfig, S3Config
from github_backup.errors import StorageError
from github_backup.gitops import GitRunner
from github_backup.models import BackupResult, Resource, RunManifest
from github_backup.storage import StorageBackend
from github_backup.versioned import VersionedS3Storage


class NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NoSuchKey"}}


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.metadata = {}
        self.uploads = 0
        self.fail_catalog = False
        self.bad_head = False
        self.deleted = []

    def get_object(self, *, Bucket, Key):
        if Key not in self.data:
            raise NotFound()
        return {"Body": io.BytesIO(self.data[Key])}

    def put_object(self, *, Bucket, Key, Body, **options):
        if self.fail_catalog and "/catalog/" in Key:
            raise OSError("interrupted catalog write")
        self.data[Key] = Body

    def upload_file(self, path, bucket, key, ExtraArgs, Config):
        assert Config.max_concurrency == 1 and Config.use_threads is False
        self.data[key] = Path(path).read_bytes()
        self.metadata[key] = ExtraArgs["Metadata"]
        self.uploads += 1

    def head_object(self, *, Bucket, Key):
        if Key not in self.data:
            raise NotFound()
        return {
            "ContentLength": 0 if self.bad_head else len(self.data[Key]),
            "Metadata": self.metadata[Key],
        }

    def list_objects_v2(self, *, Bucket, Prefix, **options):
        return {
            "Contents": [
                {"Key": k, "Size": len(v), "LastModified": datetime.now(UTC)}
                for k, v in sorted(self.data.items())
                if k.startswith(Prefix)
            ]
        }

    def delete_objects(self, *, Bucket, Delete):
        for entry in Delete["Objects"]:
            self.deleted.append(entry["Key"])
            self.data.pop(entry["Key"], None)
        return {}


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    git(source, "config", "user.name", "Backup Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "file").write_text("original")
    git(source, "add", ".")
    git(source, "commit", "-qm", "original")
    config = BackupConfig(
        storage="s3",
        mode="versioned",
        cache_dir=tmp_path / "cache",
        snapshot_retention=2,
        s3=S3Config(bucket="bucket", prefix="backup"),
    )
    backend = object.__new__(VersionedS3Storage)
    StorageBackend.__init__(backend, config)
    backend._client = MemoryStore()
    resource = Resource(
        kind="repository",
        identifier="123",
        owner="alice",
        name="repo",
        clone_url=str(source),
        html_url="https://github.com/alice/repo",
        private=True,
    )
    return backend, resource, GitRunner("secret", timeout_seconds=30), source


def git(source, *args):
    return subprocess.run(
        ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def publish(backend, resource, runner, run="run"):
    mirror = backend.mirror_path(resource)
    changed, digest = runner.update_mirror(resource.clone_url, mirror)
    return backend.publish(resource, mirror, digest, changed, "versioned", run, runner)


def catalog(backend, resource):
    return json.loads(backend._client.data[backend._catalog_key(resource)])


def test_no_duplicate_after_poll_or_cache_loss_and_restores(setup, tmp_path):
    import shutil

    backend, resource, runner, _ = setup
    first = publish(backend, resource, runner)
    assert publish(backend, resource, runner, "poll") == first
    shutil.rmtree(backend.workspace_root)
    assert publish(backend, resource, runner, "cache-loss") == first
    assert backend._client.uploads == 1
    version = catalog(backend, resource)["versions"][-1]
    bundle = tmp_path / "restore.bundle"
    bundle.write_bytes(backend._client.data[version["key"]])
    subprocess.run(["git", "clone", "-q", str(bundle), str(tmp_path / "restored")], check=True)
    assert (tmp_path / "restored/file").read_text() == "original"
    assert backend.reconcile_inventory([])[0]["identifier"] == "123"
    backend.apply_retention(
        RunManifest(
            run_id="empty",
            user="alice",
            storage="s3",
            scope="accessible",
            mode="versioned",
            started_at="now",
        )
    )
    assert version["key"] in backend._client.data


def test_force_push_retries_failed_upload_and_retains_old_code(setup):
    backend, resource, runner, source = setup
    publish(backend, resource, runner)
    old = catalog(backend, resource)["versions"][0]["key"]
    git(source, "checkout", "--orphan", "replacement")
    (source / "file").write_text("malicious")
    git(source, "add", ".")
    git(source, "commit", "-qm", "replacement")
    git(source, "branch", "-D", "main")
    backend._client.fail_catalog = True
    with pytest.raises(StorageError):
        publish(backend, resource, runner, "failed")
    assert catalog(backend, resource)["versions"][0]["key"] == old
    backend._client.fail_catalog = False
    publish(backend, resource, runner, "retry-unchanged-local")
    assert len(catalog(backend, resource)["versions"]) == 2
    assert old in backend._client.data


def test_rename_reuses_id_but_recreated_repo_does_not(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    renamed = replace(resource, name="new-name")
    publish(backend, renamed, runner)
    assert backend._client.uploads == 1
    assert catalog(backend, renamed)["resource"]["name"] == "new-name"
    recreated = replace(resource, identifier="456")
    publish(backend, recreated, runner)
    assert backend._client.uploads == 2
    assert backend.reconcile_inventory([recreated])[0]["identifier"] == "123"


def test_missing_remote_artifact_is_repaired_without_a_push(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    backend._client.data.pop(catalog(backend, resource)["versions"][-1]["key"])
    publish(backend, resource, runner, "repair")
    assert backend._client.uploads == 2


def test_failed_verification_cannot_publish_catalog(setup):
    backend, resource, runner, _ = setup
    backend._client.bad_head = True
    with pytest.raises(StorageError, match="verification"):
        publish(backend, resource, runner)
    assert backend._catalog_key(resource) not in backend._client.data


def test_age_floor_and_count_retention(setup):
    backend, resource, runner, source = setup
    for index in range(4):
        (source / "file").write_text(str(index))
        git(source, "add", ".")
        git(source, "commit", "-qm", str(index))
        publish(backend, resource, runner, str(index))
    run = RunManifest(
        run_id="last",
        user="alice",
        storage="s3",
        scope="accessible",
        mode="versioned",
        started_at="now",
        results=[
            BackupResult(
                resource=resource,
                status="success",
                started_at="now",
                finished_at="now",
                duration_seconds=1,
            )
        ],
    )
    backend.apply_retention(run)
    assert len(catalog(backend, resource)["versions"]) == 4
    data = catalog(backend, resource)
    for version in data["versions"]:
        version["created_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    backend._client.data[backend._catalog_key(resource)] = json.dumps(data).encode()
    backend.apply_retention(run)
    assert len(catalog(backend, resource)["versions"]) == 2
    assert len(backend._client.deleted) == 2


def test_corrupt_catalog_fails_closed(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    backend._client.data[backend._catalog_key(resource)] = b"bad json"
    with pytest.raises(StorageError, match="Invalid repository catalog"):
        publish(backend, resource, runner)
    assert backend._client.uploads == 1
    assert not backend._client.deleted


def test_restore_old_bundle_after_force_push(setup, tmp_path):
    backend, resource, runner, source = setup
    publish(backend, resource, runner)
    old = catalog(backend, resource)["versions"][0]["key"]
    git(source, "checkout", "--orphan", "rewritten")
    (source / "file").write_text("rewritten")
    git(source, "add", ".")
    git(source, "commit", "-qm", "rewritten")
    git(source, "branch", "-D", "main")
    publish(backend, resource, runner, "force-push")
    bundle = tmp_path / "old.bundle"
    bundle.write_bytes(backend._client.data[old])
    subprocess.run(["git", "clone", "-q", str(bundle), str(tmp_path / "old-code")], check=True)
    assert (tmp_path / "old-code/file").read_text() == "original"


def test_access_denied_catalog_never_overwritten(setup):
    from unittest.mock import Mock

    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    backend._client.get_object = Mock(side_effect=PermissionError("access denied"))
    with pytest.raises(StorageError, match="refusing to replace"):
        publish(backend, resource, runner)
    assert backend._client.uploads == 1


def test_failed_worker_does_not_discard_another_repositories_version(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    run = RunManifest(
        run_id="failure",
        user="alice",
        storage="s3",
        scope="accessible",
        mode="versioned",
        started_at="now",
        fatal_error="another repository failed",
    )
    backend.apply_retention(run)
    assert len(catalog(backend, resource)["versions"]) == 1
    assert not backend._client.deleted


def test_deleting_all_refs_keeps_previous_nonempty_version(setup):
    backend, resource, runner, source = setup
    publish(backend, resource, runner)
    original = catalog(backend, resource)["versions"][0]["key"]
    git(source, "update-ref", "-d", "refs/heads/main")
    publish(backend, resource, runner, "deleted-refs")
    versions = catalog(backend, resource)["versions"]
    assert len(versions) == 2
    assert versions[-1]["key"].endswith(".empty.tar.gz")
    assert original in backend._client.data


def test_orphan_cleanup_keeps_referenced_versions(setup):
    from unittest.mock import patch

    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    existing = catalog(backend, resource)
    referenced = existing["versions"][0]["key"]
    orphan = backend._key("versions", backend._identity(resource), "interrupted.bundle")
    backend._client.data[orphan] = b"orphan"
    objects = [
        {"Key": key, "LastModified": datetime.now(UTC) - timedelta(days=3)}
        for key in (referenced, orphan)
    ]
    with patch.object(backend, "_objects", return_value=objects):
        backend._clean_orphans(resource, existing)
    assert referenced in backend._client.data
    assert orphan not in backend._client.data


def test_invalid_catalog_artifact_path_never_deleted(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    data = catalog(backend, resource)
    data["versions"][0]["key"] = "backup/versions/repository/OTHER/important.bundle"
    backend._client.data[backend._catalog_key(resource)] = json.dumps(data).encode()
    with pytest.raises(StorageError, match="Invalid repository catalog"):
        backend.reconcile_inventory([resource])
    assert not backend._client.deleted


def test_human_readable_layout_and_same_second_collision(setup):
    import re

    backend, resource, runner, source = setup
    publish(backend, resource, runner)
    (source / "file").write_text("changed")
    git(source, "add", ".")
    git(source, "commit", "-qm", "changed")
    publish(backend, resource, runner)
    versions = catalog(backend, resource)["versions"]
    assert versions[0]["key"] != versions[1]["key"]
    for version in versions:
        assert re.fullmatch(
            r"backup/alice/repo/[0-9]{14}/repository-123-[a-f0-9]{32}\.bundle", version["key"]
        )
    assert backend._catalog_key(resource) == "backup/_system/catalog/repository/123.json"


def test_tiered_retention_is_bounded_and_keeps_older_checkpoints(setup):
    backend, _, _, _ = setup
    backend.config = replace(
        backend.config,
        snapshot_retention=3,
        version_min_age_days=0,
        version_weekly=2,
        version_monthly=2,
        version_keep_oldest=True,
    )
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    dates = [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 9, 30, tzinfo=UTC),
        datetime(2026, 10, 31, tzinfo=UTC),
        datetime(2026, 11, 8, tzinfo=UTC),
        datetime(2026, 11, 15, tzinfo=UTC),
    ]
    dates += [now - timedelta(minutes=100 - i) for i in range(100)]
    versions = [{"key": str(i), "created_at": date.isoformat()} for i, date in enumerate(dates)]
    retained = backend._retained_versions(versions, now)
    assert len(retained) == 8
    assert retained == {"0", "1", "2", "3", "4", "102", "103", "104"}
    # Same policy still keeps a truly old version when every push occurs today.
    rapid = [versions[0], *versions[5:]]
    assert len(backend._retained_versions(rapid, now)) == 4


def test_shared_bucket_does_not_count_or_delete_other_app_data(setup):
    from unittest.mock import patch

    backend, resource, runner, _ = setup
    backend._client.data["other-app/photo.jpg"] = b"not-a-backup"
    backend._client.data["backup-other/photo.jpg"] = b"also-not-a-backup"
    backend._client.data["backup/alice/repo/notes.txt"] = b"not-managed"
    publish(backend, resource, runner)
    # Old unrelated files even under the repository folder are not managed artifacts.
    listing = backend._client.list_objects_v2(Bucket="bucket", Prefix="backup/alice/repo/")[
        "Contents"
    ]
    for item in listing:
        item["LastModified"] = datetime.now(UTC) - timedelta(days=3)
    with patch.object(backend, "_objects", return_value=listing):
        backend._clean_orphans(resource, catalog(backend, resource))
    assert backend._client.data["other-app/photo.jpg"] == b"not-a-backup"
    assert backend._client.data["backup-other/photo.jpg"] == b"also-not-a-backup"
    assert backend._client.data["backup/alice/repo/notes.txt"] == b"not-managed"
    with pytest.raises(StorageError, match="outside"):
        backend._delete_keys([{"Key": "other-app/photo.jpg"}])
    with pytest.raises(StorageError, match="outside"):
        backend._delete_keys([{"Key": "backup-other/photo.jpg"}])
    assert not backend._client.deleted


def test_legacy_catalog_remains_readable_and_migrates_lazily(setup):
    backend, resource, runner, _ = setup
    publish(backend, resource, runner)
    data = catalog(backend, resource)
    old_artifact = "backup/versions/repository/123/old.bundle"
    original_key = data["versions"][0]["key"]
    backend._client.data[old_artifact] = backend._client.data.pop(original_key)
    backend._client.metadata[old_artifact] = backend._client.metadata.pop(original_key)
    data["versions"][0]["key"] = old_artifact
    legacy_key = "backup/catalog/repository/123.json"
    backend._client.data[legacy_key] = json.dumps(data).encode()
    backend._client.data.pop(backend._catalog_key(resource))
    publish(backend, resource, runner)
    assert backend._client.uploads == 1
    assert catalog(backend, resource)["versions"][0]["key"] == old_artifact
    assert backend.reconcile_inventory([resource]) == []


def test_custom_nested_prefix_and_manifest_scope(setup):
    backend, resource, runner, _ = setup
    backend.config = replace(backend.config, s3=replace(backend.config.s3, prefix="backups/github"))
    publish(backend, resource, runner)
    key = catalog(backend, resource)["versions"][0]["key"]
    assert key.startswith("backups/github/alice/repo/")
    run = RunManifest(
        run_id="run",
        user="alice",
        storage="s3",
        scope="accessible",
        mode="versioned",
        started_at="now",
    )
    backend.write_manifest(run)
    assert "backups/github/_system/manifests/latest.json" in backend._client.data
    assert all(key.startswith("backups/github/") for key in backend._client.data)


@pytest.mark.parametrize(
    "prefix", ["", "/", "/backups", "backups/", "backups//github", "../github"]
)
def test_unsafe_prefixes_are_rejected(setup, prefix):
    from github_backup.errors import ConfigurationError

    backend, _, _, _ = setup
    config = replace(backend.config, s3=replace(backend.config.s3, prefix=prefix))
    with pytest.raises(ConfigurationError, match="prefix"):
        config.validated()


def test_catalog_named_github_owner_is_not_confused_with_legacy_indexes(setup):
    backend, resource, runner, _ = setup
    resource = replace(resource, owner="catalog", name="repository")
    publish(backend, resource, runner)
    assert backend.reconcile_inventory([resource]) == []
