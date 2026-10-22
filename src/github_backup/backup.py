"""Backup orchestration."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import UTC, datetime

from github_backup.config import BackupConfig
from github_backup.errors import GitCommandError
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner, missing_remote
from github_backup.models import (
    BackupResult,
    ComponentResult,
    Resource,
    ResultStatus,
    RunManifest,
)
from github_backup.security import private_umask
from github_backup.storage import StorageBackend, cleanup_empty_artifact_directories
from github_backup.supplemental import SupplementalExporter

LOGGER = logging.getLogger(__name__)


class BackupService:
    def __init__(
        self,
        config: BackupConfig,
        github: GitHubClient,
        git: GitRunner,
        storage: StorageBackend,
    ) -> None:
        self.config = config
        self.github = github
        self.git = git
        self.storage = storage

    def discover(self) -> tuple[str, list[Resource]]:
        user = self.github.authenticated_user()
        repositories = self.github.repositories(self.config.scope, self.config.organizations)
        gists = self.github.gists(user) if self.config.include_gists else []
        wikis = (
            [_wiki_resource(repository) for repository in repositories if repository.has_wiki]
            if "wikis" in self.config.additional_data
            else []
        )
        resources = sorted(
            (*repositories, *wikis, *gists), key=lambda item: item.relative_path.casefold()
        )
        LOGGER.info(
            "Discovered %d repositories, %d wikis, and %d gists for %s",
            len(repositories),
            len(wikis),
            len(gists),
            user,
        )
        return user, resources

    def run(self) -> tuple[RunManifest, str]:
        with private_umask():
            self.git.ensure_available()
            self.storage.prepare()
            run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            manifest = RunManifest(
                run_id=run_id,
                user="unknown",
                storage=self.config.storage,
                scope=self.config.scope,
                mode=self.config.mode,
                started_at=datetime.now(UTC).isoformat(),
            )
            lock_entered = False
            try:
                with self.storage.run_lock():
                    lock_entered = True
                    try:
                        user, resources = self.discover()
                        manifest.user = user
                        with ThreadPoolExecutor(
                            max_workers=self.config.workers, thread_name_prefix="github-backup"
                        ) as executor:
                            futures = {
                                executor.submit(self._backup_one, resource, run_id): resource
                                for resource in resources
                            }
                            for future in as_completed(futures):
                                result = future.result()
                                manifest.results.append(result)
                                if result.status == "failed":
                                    LOGGER.error(
                                        "Failed %s: %s",
                                        result.resource.relative_path,
                                        result.error,
                                    )
                                elif result.status == "skipped":
                                    LOGGER.warning(
                                        "Skipped %s: %s",
                                        result.resource.relative_path,
                                        result.error,
                                    )
                                else:
                                    LOGGER.info(
                                        "Backed up %s (%s)",
                                        result.resource.relative_path,
                                        "changed" if result.changed else "unchanged",
                                    )
                        self._backup_supplementary_data(resources, run_id, manifest)
                    except BaseException as exc:
                        manifest.fatal_error = f"{type(exc).__name__}: {exc}"
                        raise
                    finally:
                        manifest.results.sort(
                            key=lambda item: item.resource.relative_path.casefold()
                        )
                        manifest.components.sort(
                            key=lambda item: (item.repository.casefold(), item.component)
                        )
                        manifest.finish()
                        manifest_location = self.storage.write_manifest(manifest)
                        try:
                            self.storage.apply_retention(manifest)
                        except Exception as exc:
                            manifest.fatal_error = (
                                f"retention failed: {type(exc).__name__}: {exc}"
                            )
                            manifest_location = self.storage.write_manifest(manifest)
                            raise
                        cleanup_empty_artifact_directories(
                            self.storage.workspace_root.parent / "artifacts"
                        )
            except BaseException as exc:
                if lock_entered and manifest.fatal_error is None:
                    manifest.fatal_error = f"{type(exc).__name__}: {exc}"
                    manifest.finish()
                    with suppress(Exception):
                        self.storage.write_manifest(manifest)
                raise
        return manifest, manifest_location

    def _backup_one(self, resource: Resource, run_id: str) -> BackupResult:
        started = datetime.now(UTC)
        monotonic_started = time.monotonic()
        try:
            mirror_path = self.storage.mirror_path(resource)
            changed, ref_digest = self.git.update_mirror(resource.clone_url, mirror_path)
            artifact = self.storage.publish(
                resource,
                mirror_path,
                ref_digest,
                changed,
                self.config.mode,
                run_id,
                self.git,
            )
            status: ResultStatus = "success"
            error = None
        except Exception as exc:
            changed = False
            ref_digest = None
            artifact = None
            status = (
                "skipped"
                if resource.kind == "wiki"
                and isinstance(exc, GitCommandError)
                and missing_remote(exc)
                else "failed"
            )
            error = f"{type(exc).__name__}: {exc}"
        finished = datetime.now(UTC)
        return BackupResult(
            resource=resource,
            status=status,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_seconds=round(time.monotonic() - monotonic_started, 3),
            changed=changed,
            ref_digest=ref_digest,
            artifact=artifact,
            error=error,
        )

    def _backup_supplementary_data(
        self,
        resources: list[Resource],
        run_id: str,
        manifest: RunManifest,
    ) -> None:
        components = tuple(
            component for component in self.config.additional_data if component != "wikis"
        )
        if not components:
            return
        repositories = [resource for resource in resources if resource.kind == "repository"]
        successful = {
            result.resource.identifier
            for result in manifest.results
            if result.resource.kind == "repository" and result.status == "success"
        }
        artifact_root = self.storage.workspace_root.parent / "artifacts" / run_id
        exporter = SupplementalExporter(self.github, self.git, artifact_root)
        with ThreadPoolExecutor(
            max_workers=self.config.workers,
            thread_name_prefix="github-backup-data",
        ) as executor:
            futures = {}
            for repository in repositories:
                for component in components:
                    if repository.identifier not in successful:
                        manifest.components.append(
                            _skipped_component(repository, component, "repository backup failed")
                        )
                        continue
                    future = executor.submit(
                        self._backup_component,
                        exporter,
                        repository,
                        component,
                        run_id,
                    )
                    futures[future] = (repository, component)
            for future in as_completed(futures):
                result = future.result()
                manifest.components.append(result)
                if result.status == "failed":
                    LOGGER.error(
                        "Failed %s component for %s: %s",
                        result.component,
                        result.repository,
                        result.error,
                    )
                else:
                    LOGGER.info(
                        "Backed up %s component for %s (%s)",
                        result.component,
                        result.repository,
                        result.status,
                    )

    def _backup_component(
        self,
        exporter: SupplementalExporter,
        repository: Resource,
        component: str,
        run_id: str,
    ) -> ComponentResult:
        started = datetime.now(UTC)
        monotonic_started = time.monotonic()
        archive = None
        try:
            archive = exporter.export(
                repository,
                component,
                self.storage.mirror_path(repository),
            )
            artifact = self.storage.publish_supplement(
                repository,
                component,
                archive.path,
                archive.sha256,
                self.config.mode,
                run_id,
            )
            status: ResultStatus = "partial" if archive.unavailable else "success"
            return ComponentResult(
                repository=f"{repository.owner}/{repository.name}",
                component=component,
                status=status,
                started_at=started.isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
                duration_seconds=round(time.monotonic() - monotonic_started, 3),
                artifact=artifact,
                sha256=archive.sha256,
                files=archive.files,
                bytes=archive.bytes,
                unavailable=archive.unavailable,
            )
        except Exception as exc:
            return ComponentResult(
                repository=f"{repository.owner}/{repository.name}",
                component=component,
                status="failed",
                started_at=started.isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
                duration_seconds=round(time.monotonic() - monotonic_started, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if archive is not None:
                archive.path.unlink(missing_ok=True)


def _wiki_resource(repository: Resource) -> Resource:
    base = (
        repository.clone_url[:-4]
        if repository.clone_url.endswith(".git")
        else repository.clone_url
    )
    return Resource(
        kind="wiki",
        identifier=f"{repository.identifier}:wiki",
        name=repository.name,
        owner=repository.owner,
        clone_url=f"{base}.wiki.git",
        html_url=f"{repository.html_url.rstrip('/')}/wiki",
        private=repository.private,
        updated_at=repository.updated_at,
    )


def _skipped_component(
    repository: Resource, component: str, reason: str
) -> ComponentResult:
    timestamp = datetime.now(UTC).isoformat()
    return ComponentResult(
        repository=f"{repository.owner}/{repository.name}",
        component=component,
        status="skipped",
        started_at=timestamp,
        finished_at=timestamp,
        duration_seconds=0.0,
        error=reason,
    )
