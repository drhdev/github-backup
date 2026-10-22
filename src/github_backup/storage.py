"""Filesystem/NAS and S3-compatible storage backends."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import uuid
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Any

from github_backup.config import BackupConfig, BackupMode
from github_backup.errors import StorageError
from github_backup.gitops import GitRunner
from github_backup.locking import BackupLock, CombinedLock, S3LeaseLock
from github_backup.models import Resource, RunManifest
from github_backup.security import prepare_private_directory, require_free_space

LOGGER = logging.getLogger(__name__)


class StorageBackend(ABC):
    def __init__(self, config: BackupConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def workspace_root(self) -> Path:
        """Local directory containing incremental Git mirrors."""

    @property
    def lock_path(self) -> Path:
        return self.workspace_root.parent / ".github-backup.lock"

    def mirror_path(self, resource: Resource) -> Path:
        return self.workspace_root / resource.relative_path

    @abstractmethod
    def prepare(self) -> None:
        """Validate and prepare local working storage."""

    @abstractmethod
    def run_lock(self) -> AbstractContextManager[object]:
        """Prevent overlapping runs for this destination."""

    @abstractmethod
    def publish(
        self,
        resource: Resource,
        mirror_path: Path,
        ref_digest: str,
        changed: bool,
        mode: BackupMode,
        run_id: str,
        git: GitRunner,
    ) -> str:
        """Publish a completed mirror and return its artifact location."""

    @abstractmethod
    def write_manifest(self, manifest: RunManifest) -> str:
        """Persist both immutable and latest run manifests."""

    @abstractmethod
    def publish_supplement(
        self,
        repository: Resource,
        component: str,
        archive: Path,
        sha256: str,
        mode: BackupMode,
        run_id: str,
    ) -> str:
        """Publish one self-contained supplementary-data archive."""

    @abstractmethod
    def apply_retention(self, manifest: RunManifest) -> None:
        """Remove snapshots exceeding configured retention after a successful run."""


class FilesystemStorage(StorageBackend):
    @property
    def workspace_root(self) -> Path:
        return self.config.destination / "mirrors"

    def prepare(self) -> None:
        prepare_private_directory(
            self.config.destination,
            allow_insecure=self.config.allow_insecure_permissions,
        )
        require_free_space(self.config.destination, self.config.minimum_free_space_gib)

    def run_lock(self) -> AbstractContextManager[object]:
        return BackupLock(self.lock_path, wait_seconds=self.config.lock_timeout_seconds)

    def publish(
        self,
        resource: Resource,
        mirror_path: Path,
        ref_digest: str,
        changed: bool,
        mode: BackupMode,
        run_id: str,
        git: GitRunner,
    ) -> str:
        metadata_path = self.config.destination / "metadata" / _metadata_path(resource)
        _write_json_atomic(
            metadata_path,
            {"schema_version": 1, "ref_digest": ref_digest, "resource": resource.to_dict()},
        )
        if mode == "incremental":
            return str(mirror_path)
        artifact = _snapshot_path(
            self.config.destination / "snapshots" / run_id, resource, git, mirror_path
        )
        _create_snapshot(git, mirror_path, artifact)
        return str(artifact)

    def write_manifest(self, manifest: RunManifest) -> str:
        manifest_path = self.config.destination / "manifests" / f"{manifest.run_id}.json"
        _write_json_atomic(manifest_path, manifest.to_dict())
        _write_json_atomic(
            self.config.destination / "manifests" / "latest.json", manifest.to_dict()
        )
        return str(manifest_path)

    def publish_supplement(
        self,
        repository: Resource,
        component: str,
        archive: Path,
        sha256: str,
        mode: BackupMode,
        run_id: str,
    ) -> str:
        del sha256
        root = (
            self.config.destination / "snapshots" / run_id
            if mode == "snapshot"
            else self.config.destination / "supplemental" / "latest"
        )
        destination = root / "repos" / repository.owner / repository.name / f"{component}.tar.gz"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            with archive.open("rb") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return str(destination)

    def apply_retention(self, manifest: RunManifest) -> None:
        if not _retention_enabled(self.config, manifest):
            return
        snapshots = self.config.destination / "snapshots"
        if not snapshots.exists():
            return
        runs = sorted(path for path in snapshots.iterdir() if path.is_dir())
        for expired in runs[: -self.config.snapshot_retention]:
            shutil.rmtree(expired)


class S3Storage(StorageBackend):
    def __init__(self, config: BackupConfig) -> None:
        super().__init__(config)
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageError(
                "S3 support is not installed; install with: pip install 'github-backup[s3]'"
            ) from exc
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=config.s3.endpoint_url,
            region_name=config.s3.region,
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 10},
                connect_timeout=30,
                read_timeout=30,
            ),
        )

    @property
    def workspace_root(self) -> Path:
        identity = hashlib.sha256(
            f"{self.config.s3.endpoint_url}|{self.config.s3.bucket}|{self.config.s3.prefix}".encode()
        ).hexdigest()[:16]
        return self.config.cache_dir / identity / "mirrors"

    def prepare(self) -> None:
        local_root = self.workspace_root.parent
        prepare_private_directory(
            local_root,
            allow_insecure=self.config.allow_insecure_permissions,
        )
        require_free_space(local_root, self.config.minimum_free_space_gib)

    def run_lock(self) -> AbstractContextManager[object]:
        local = BackupLock(self.lock_path, wait_seconds=self.config.lock_timeout_seconds)
        remote = S3LeaseLock(
            self._client,
            bucket=self.config.s3.bucket,
            key=self._key("locks", "run.lock"),
            lease_seconds=self.config.s3.lock_lease_seconds,
            wait_seconds=self.config.lock_timeout_seconds,
            put_options=self._lock_options(),
        )
        return CombinedLock(local, remote)

    def publish(
        self,
        resource: Resource,
        mirror_path: Path,
        ref_digest: str,
        changed: bool,
        mode: BackupMode,
        run_id: str,
        git: GitRunner,
    ) -> str:
        metadata_key = self._key("metadata", _metadata_path(resource).as_posix())
        self._put_json(
            metadata_key,
            {"schema_version": 1, "ref_digest": ref_digest, "resource": resource.to_dict()},
        )
        prefix = f"snapshots/{run_id}" if mode == "snapshot" else "latest"
        suffix = ".bundle" if git.has_refs(mirror_path) else ".empty.tar.gz"
        artifact_key = self._key(prefix, f"{resource.relative_path}{suffix}")
        if (
            mode == "incremental"
            and not changed
            and self._remote_digest(artifact_key) == ref_digest
        ):
            return f"s3://{self.config.s3.bucket}/{artifact_key}"

        temporary_dir = self.workspace_root.parent / "artifacts" / run_id
        artifact = temporary_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            _create_snapshot(git, mirror_path, artifact)
            extra_args: dict[str, Any] = self._upload_options()
            extra_args["Metadata"] = {"ref-digest": ref_digest}
            self._client.upload_file(
                str(artifact), self.config.s3.bucket, artifact_key, ExtraArgs=extra_args
            )
        except Exception as exc:
            raise StorageError(
                f"failed to upload s3://{self.config.s3.bucket}/{artifact_key}: {exc}"
            ) from exc
        finally:
            artifact.unlink(missing_ok=True)
        return f"s3://{self.config.s3.bucket}/{artifact_key}"

    def write_manifest(self, manifest: RunManifest) -> str:
        immutable_key = self._key("manifests", f"{manifest.run_id}.json")
        latest_key = self._key("manifests", "latest.json")
        self._put_json(immutable_key, manifest.to_dict())
        self._put_json(latest_key, manifest.to_dict())
        return f"s3://{self.config.s3.bucket}/{immutable_key}"

    def publish_supplement(
        self,
        repository: Resource,
        component: str,
        archive: Path,
        sha256: str,
        mode: BackupMode,
        run_id: str,
    ) -> str:
        prefix = f"snapshots/{run_id}" if mode == "snapshot" else "latest"
        key = self._key(
            prefix,
            "supplemental",
            "repos",
            repository.owner,
            repository.name,
            f"{component}.tar.gz",
        )
        if mode == "incremental" and self._remote_metadata(key, "sha256") == sha256:
            return f"s3://{self.config.s3.bucket}/{key}"
        options: dict[str, Any] = self._upload_options()
        options["Metadata"] = {"sha256": sha256}
        try:
            self._client.upload_file(
                str(archive), self.config.s3.bucket, key, ExtraArgs=options
            )
        except Exception as exc:
            raise StorageError(
                f"failed to upload s3://{self.config.s3.bucket}/{key}: {exc}"
            ) from exc
        return f"s3://{self.config.s3.bucket}/{key}"

    def apply_retention(self, manifest: RunManifest) -> None:
        if not _retention_enabled(self.config, manifest):
            return
        base_prefix = self._key("snapshots") + "/"
        run_ids: set[str] = set()
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.config.s3.bucket,
                "Prefix": base_prefix,
            }
            if continuation:
                request["ContinuationToken"] = continuation
            response = self._client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                key = str(item["Key"])
                remainder = key.removeprefix(base_prefix)
                if "/" in remainder:
                    run_ids.add(remainder.split("/", 1)[0])
            if not response.get("IsTruncated"):
                break
            continuation = str(response["NextContinuationToken"])
        for run_id in sorted(run_ids)[: -self.config.snapshot_retention]:
            self._delete_prefix(f"{base_prefix}{run_id}/")

    def _key(self, *parts: str) -> str:
        return "/".join(part.strip("/") for part in (self.config.s3.prefix, *parts) if part)

    def _put_json(self, key: str, payload: dict[str, Any]) -> None:
        options = self._upload_options()
        options["ContentType"] = "application/json"
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        try:
            self._client.put_object(
                Bucket=self.config.s3.bucket,
                Key=key,
                Body=body,
                **options,
            )
        except Exception as exc:
            raise StorageError(
                f"failed to write s3://{self.config.s3.bucket}/{key}: {exc}"
            ) from exc

    def _remote_digest(self, key: str) -> str | None:
        return self._remote_metadata(key, "ref-digest")

    def _remote_metadata(self, key: str, field: str) -> str | None:
        try:
            response = self._client.head_object(Bucket=self.config.s3.bucket, Key=key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(
                f"failed to inspect s3://{self.config.s3.bucket}/{key}: {exc}"
            ) from exc
        metadata = response.get("Metadata", {})
        value = metadata.get(field)
        return str(value) if value else None

    def _upload_options(self) -> dict[str, str]:
        options: dict[str, str] = {}
        if self.config.s3.storage_class:
            options["StorageClass"] = self.config.s3.storage_class
        if self.config.s3.server_side_encryption:
            options["ServerSideEncryption"] = self.config.s3.server_side_encryption
        return options

    def _lock_options(self) -> dict[str, str]:
        options: dict[str, str] = {}
        if self.config.s3.server_side_encryption:
            options["ServerSideEncryption"] = self.config.s3.server_side_encryption
        return options

    def _delete_prefix(self, prefix: str) -> None:
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.config.s3.bucket,
                "Prefix": prefix,
            }
            if continuation:
                request["ContinuationToken"] = continuation
            response = self._client.list_objects_v2(**request)
            keys = [{"Key": str(item["Key"])} for item in response.get("Contents", [])]
            if keys:
                self._client.delete_objects(
                    Bucket=self.config.s3.bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
            if not response.get("IsTruncated"):
                break
            continuation = str(response["NextContinuationToken"])


def create_storage(config: BackupConfig) -> StorageBackend:
    if config.storage == "filesystem":
        return FilesystemStorage(config)
    return S3Storage(config)


def _metadata_path(resource: Resource) -> Path:
    path = Path(resource.relative_path)
    return path.with_suffix(".json")


def _snapshot_path(root: Path, resource: Resource, git: GitRunner, mirror_path: Path) -> Path:
    suffix = ".bundle" if git.has_refs(mirror_path) else ".empty.tar.gz"
    return root / f"{resource.relative_path}{suffix}"


def _create_snapshot(git: GitRunner, mirror_path: Path, artifact: Path) -> None:
    if git.has_refs(mirror_path):
        git.create_bundle(mirror_path, artifact)
        return
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact.with_name(f".{artifact.name}.tmp-{uuid.uuid4().hex}")
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(mirror_path, arcname=mirror_path.name, recursive=True)
        temporary.replace(artifact)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def cleanup_empty_artifact_directories(root: Path) -> None:
    """Best-effort cleanup for temporary local S3 packaging directories."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            with suppress(OSError):
                path.rmdir()
    with suppress(OSError):
        root.rmdir()


def _retention_enabled(config: BackupConfig, manifest: RunManifest) -> bool:
    return (
        manifest.mode == "snapshot"
        and config.snapshot_retention > 0
        and manifest.fatal_error is None
        and manifest.counts["failed"] == 0
        and manifest.counts["partial"] == 0
    )
