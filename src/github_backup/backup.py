"""Backup orchestration."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from github_backup.config import BackupConfig
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner
from github_backup.locking import BackupLock
from github_backup.models import BackupResult, Resource, ResultStatus, RunManifest
from github_backup.storage import StorageBackend, cleanup_empty_artifact_directories

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
        resources = sorted((*repositories, *gists), key=lambda item: item.relative_path.casefold())
        LOGGER.info(
            "Discovered %d repositories and %d gists for %s",
            len(repositories),
            len(gists),
            user,
        )
        return user, resources

    def run(self) -> tuple[RunManifest, str]:
        self.git.ensure_available()
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        manifest = RunManifest(
            run_id=run_id,
            user="unknown",
            storage=self.config.storage,
            scope=self.config.scope,
            mode=self.config.mode,
            started_at=datetime.now(UTC).isoformat(),
        )
        with BackupLock(self.storage.lock_path, wait_seconds=self.config.lock_timeout_seconds):
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
                                "Failed %s: %s", result.resource.relative_path, result.error
                            )
                        else:
                            LOGGER.info(
                                "Backed up %s (%s)",
                                result.resource.relative_path,
                                "changed" if result.changed else "unchanged",
                            )
            except BaseException as exc:
                manifest.fatal_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                manifest.results.sort(key=lambda item: item.resource.relative_path.casefold())
                manifest.finish()
                manifest_location = self.storage.write_manifest(manifest)
                cleanup_empty_artifact_directories(self.storage.workspace_root.parent / "artifacts")
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
            status = "failed"
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
