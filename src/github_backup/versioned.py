"""Independent, change-triggered recovery points indexed by GitHub repository ID."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from github_backup.config import BackupMode
from github_backup.errors import StorageError
from github_backup.gitops import GitRunner
from github_backup.models import Resource, RunManifest
from github_backup.storage import S3Storage, _create_snapshot

LOGGER = logging.getLogger(__name__)


class VersionedS3Storage(S3Storage):
    def _identity(self, resource: Resource) -> str:
        if not re.fullmatch(r"[A-Za-z0-9:_-]+", resource.identifier):
            raise StorageError("Invalid repository identifier")
        return f"{resource.kind}/{resource.identifier}"

    def mirror_path(self, resource: Resource) -> Path:
        return self.workspace_root / f"{self._identity(resource)}.git"

    def _catalog_key(self, resource: Resource) -> str:
        return self._key("_system", "catalog", self._identity(resource) + ".json")

    def _load_catalog(self, resource: Resource) -> dict[str, Any] | None:
        current = self._read_catalog(self._catalog_key(resource))
        return (
            current
            if current is not None
            else self._read_catalog(self._key("catalog", self._identity(resource) + ".json"))
        )

    def _repository_prefix(self, resource: Resource) -> str:
        for part in (resource.owner, resource.name):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", ".."):
                raise StorageError("Unsafe repository owner or name")
        return self._key(resource.owner, resource.name) + "/"

    def _valid_artifact(self, resource: Resource, version: dict[str, Any]) -> bool:
        key = version["key"]
        legacy = self._key("versions", self._identity(resource)) + "/"
        if key.startswith(legacy):
            return ".." not in key.split("/")
        historical = Resource(
            **{
                **resource.to_dict(),
                "owner": version.get("owner", ""),
                "name": version.get("repository", ""),
            }
        )
        prefix = self._repository_prefix(historical)
        pattern = (
            re.escape(prefix)
            + r"[0-9]{14}/repository-"
            + re.escape(resource.identifier)
            + r"-[a-f0-9]{32}(?:\.bundle|\.empty\.tar\.gz)"
        )
        return re.fullmatch(pattern, key) is not None

    def _read_catalog(self, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_object(Bucket=self.config.s3.bucket, Key=key)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError("Cannot read repository catalog; refusing to replace it") from exc
        try:
            with response["Body"] as body:
                raw = body.read(8 * 1024**2 + 1)
            if len(raw) > 8 * 1024**2:
                raise ValueError("oversized catalog")
            data = json.loads(raw)
            if data["schema_version"] != 1 or not isinstance(data["versions"], list):
                raise ValueError("invalid schema")
            if len(data["versions"]) > 10000:
                raise ValueError("too many catalog entries")
            resource = Resource(**data["resource"])
            if key not in (
                self._catalog_key(resource),
                self._key("catalog", self._identity(resource) + ".json"),
            ):
                raise ValueError("catalog identity mismatch")
            for version in data["versions"]:
                if not self._valid_artifact(resource, version):
                    raise ValueError("invalid artifact path")
                if not re.fullmatch(r"[a-f0-9]{64}", version["sha256"]):
                    raise ValueError("invalid checksum")
                datetime.fromisoformat(version["created_at"]).astimezone(UTC)
                if int(version["size"]) < 0:
                    raise ValueError("invalid size")
            return dict(data)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError(
                "Invalid repository catalog; refusing destructive operations"
            ) from exc

    def _verify_remote(self, version: dict[str, Any]) -> bool:
        try:
            response = self._client.head_object(Bucket=self.config.s3.bucket, Key=version["key"])
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError("Cannot verify stored artifact") from exc
        return bool(
            int(response.get("ContentLength", -1)) == version["size"]
            and response.get("Metadata", {}).get("sha256") == version["sha256"]
        )

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
        from boto3.s3.transfer import TransferConfig

        catalog_key = self._catalog_key(resource)
        catalog = self._load_catalog(resource)
        versions = catalog["versions"] if catalog else []
        if (
            versions
            and versions[-1]["ref_digest"] == ref_digest
            and self._verify_remote(versions[-1])
        ):
            # Cache loss, restarts, and unchanged polling never duplicate remote code.
            if catalog:
                catalog["resource"] = resource.to_dict()
                self._put_json(catalog_key, catalog)
            return f"s3://{self.config.s3.bucket}/{versions[-1]['key']}"
        suffix = ".bundle" if git.has_refs(mirror_path) else ".empty.tar.gz"

        artifact = self.workspace_root.parent / "artifacts" / f"{uuid.uuid4().hex}{suffix}"
        try:
            _create_snapshot(git, mirror_path, artifact)
            with artifact.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            size = artifact.stat().st_size
            completed = datetime.now(UTC)
            key = (
                self._repository_prefix(resource)
                + completed.strftime("%Y%m%d%H%M%S")
                + f"/repository-{resource.identifier}-{uuid.uuid4().hex}{suffix}"
            )
            self._reserve_upload(size)
            options: dict[str, Any] = self._upload_options()
            options["Metadata"] = {"sha256": sha256, "ref-digest": ref_digest}
            self._client.upload_file(
                str(artifact),
                self.config.s3.bucket,
                key,
                ExtraArgs=options,
                Config=TransferConfig(
                    max_concurrency=1, use_threads=False, multipart_chunksize=8 * 1024**2
                ),
            )
            version = {
                "key": key,
                "ref_digest": ref_digest,
                "sha256": sha256,
                "size": size,
                "created_at": completed.isoformat(),
                "owner": resource.owner,
                "repository": resource.name,
            }
            if not self._verify_remote(version):
                raise StorageError("Uploaded artifact failed size/checksum-metadata verification")
            self._put_json(
                catalog_key,
                {
                    "schema_version": 1,
                    "resource": resource.to_dict(),
                    "versions": [*versions, version],
                },
            )
            return f"s3://{self.config.s3.bucket}/{key}"
        finally:
            artifact.unlink(missing_ok=True)

    def write_manifest(self, manifest: RunManifest) -> str:
        key = self._key("_system", "manifests", f"{manifest.run_id}.json")
        self._put_json(key, manifest.to_dict())
        self._put_json(self._key("_system", "manifests", "latest.json"), manifest.to_dict())
        return f"s3://{self.config.s3.bucket}/{key}"

    def reconcile_inventory(self, resources: list[Resource]) -> list[dict[str, Any]]:
        discovered = {self._identity(resource) for resource in resources}
        missing = []
        identities: set[str] = set()
        for prefix in (self._key("_system", "catalog"), self._key("catalog")):
            for item in self._objects(prefix + "/"):
                key = str(item["Key"])
                if not re.fullmatch(
                    re.escape(prefix + "/") + r"(?:repository|wiki|gist)/[A-Za-z0-9:_-]+\.json", key
                ):
                    continue
                catalog = self._read_catalog(key)
                if not catalog:
                    continue
                resource = Resource(**catalog["resource"])
                identity = self._identity(resource)
                if identity in identities:
                    continue
                identities.add(identity)
                if identity not in discovered:
                    missing.append(resource.to_dict())
                else:
                    self._clean_orphans(resource, catalog)
        return missing

    def _clean_orphans(self, resource: Resource, catalog: dict[str, Any]) -> None:
        referenced = {version["key"] for version in catalog["versions"]}
        cutoff = datetime.now(UTC) - timedelta(hours=48)
        prefixes = {
            self._repository_prefix(resource),
            self._key("versions", self._identity(resource)) + "/",
        }
        for version in catalog["versions"]:
            if "owner" in version:
                historical = Resource(
                    **{
                        **resource.to_dict(),
                        "owner": version["owner"],
                        "name": version["repository"],
                    }
                )
                prefixes.add(self._repository_prefix(historical))
        for item in (item for prefix in prefixes for item in self._objects(prefix)):
            if (
                self._managed_orphan(resource, str(item["Key"]))
                and item["Key"] not in referenced
                and item.get("LastModified", datetime.now(UTC)) < cutoff
            ):
                self._delete_keys([{"Key": str(item["Key"])}])

    def _managed_orphan(self, resource: Resource, key: str) -> bool:
        if key.startswith(self._key("versions", self._identity(resource)) + "/"):
            return True
        return (
            re.fullmatch(
                r"repository-"
                + re.escape(resource.identifier)
                + r"-[a-f0-9]{32}(?:\.bundle|\.empty\.tar\.gz)",
                key.rsplit("/", 1)[-1],
            )
            is not None
        )

    def _retained_versions(self, versions: list[dict[str, Any]], now: datetime) -> set[str]:
        if self.config.snapshot_retention == 0:
            return {version["key"] for version in versions}
        keep = {version["key"] for version in versions[-self.config.snapshot_retention :]}
        if self.config.version_keep_oldest and versions:
            keep.add(versions[0]["key"])
        current_week = now.date() - timedelta(days=now.weekday())
        # One latest changed version in each of the previous completed calendar periods.
        weekly: dict[int, str] = {}
        monthly: dict[int, str] = {}
        for version in versions:
            created = datetime.fromisoformat(version["created_at"]).astimezone(UTC)
            week = created.date() - timedelta(days=created.weekday())
            weeks_ago = (current_week - week).days // 7
            months_ago = (now.year - created.year) * 12 + now.month - created.month
            if 1 <= weeks_ago <= self.config.version_weekly:
                weekly[weeks_ago] = version["key"]
            if 1 <= months_ago <= self.config.version_monthly:
                monthly[months_ago] = version["key"]
            if self.config.version_min_age_days and created > now - timedelta(
                days=self.config.version_min_age_days
            ):
                keep.add(version["key"])
        return keep | set(weekly.values()) | set(monthly.values())

    def apply_retention(self, manifest: RunManifest) -> None:
        # Each repository is independent; another repository's failure cannot discard it.
        now = datetime.now(UTC)
        for result in manifest.results:
            if result.status != "success":
                continue
            catalog_key = self._catalog_key(result.resource)
            catalog = self._load_catalog(result.resource)
            if not catalog or not catalog["versions"]:
                continue
            versions = catalog["versions"]
            if not self._verify_remote(versions[-1]):
                raise StorageError("Latest artifact missing; refusing retention")
            retained = self._retained_versions(versions, now)
            expired = [version for version in versions if version["key"] not in retained]
            # Update index before deletion: interruption leaves extra data, never dangling pointers.
            if expired:
                catalog["versions"] = [version for version in versions if version not in expired]
                self._put_json(catalog_key, catalog)
                for version in expired:
                    self._delete_keys([{"Key": version["key"]}])
        # Bound diagnostic manifests; repository catalogs are independent and never expire.
        retention = self.config.s3.manifest_retention
        if retention:
            keys = sorted(
                str(item["Key"])
                for item in self._objects(self._key("_system", "manifests") + "/")
                if not str(item["Key"]).endswith("/latest.json")
            )
            for key in keys[:-retention]:
                self._delete_keys([{"Key": key}])
