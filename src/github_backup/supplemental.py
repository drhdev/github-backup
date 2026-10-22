"""Optional backups for GitHub data that does not live in normal Git refs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from github_backup.errors import GitHubAPIError
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner
from github_backup.models import Resource

_EXPECTED_UNAVAILABLE = {403, 404, 409, 410, 422}


@dataclass(slots=True)
class ExportedArchive:
    path: Path
    sha256: str
    files: int
    bytes: int
    unavailable: tuple[str, ...] = ()


@dataclass(slots=True)
class _Coverage:
    exported: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "exported": sorted(self.exported),
            "unavailable": sorted(self.unavailable),
            "skipped": sorted(self.skipped),
        }


class SupplementalExporter:
    def __init__(
        self,
        github: GitHubClient,
        git: GitRunner,
        artifact_root: Path,
    ) -> None:
        self.github = github
        self.git = git
        self.artifact_root = artifact_root

    def export(
        self,
        repository: Resource,
        component: str,
        mirror_path: Path,
    ) -> ExportedArchive:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="component-", dir=self.artifact_root))
        archive = self.artifact_root / f"{uuid.uuid4().hex}.tar.gz"
        coverage = _Coverage()
        try:
            payload_root = work / component
            payload_root.mkdir()
            external_tree: Path | None = None
            if component == "metadata":
                self._export_metadata(repository, payload_root, coverage)
            elif component == "releases":
                self._export_releases(repository, payload_root, coverage)
            elif component == "actions":
                self._export_actions(repository, payload_root, coverage)
            elif component == "lfs":
                external_tree = self._export_lfs(repository, mirror_path, coverage)
            else:
                raise ValueError(f"unsupported supplementary component: {component}")
            _write_json(payload_root / "coverage.json", coverage.payload())
            files = sum(path.is_file() for path in payload_root.rglob("*"))
            if external_tree is not None:
                files += sum(path.is_file() for path in external_tree.rglob("*"))
            with tarfile.open(archive, "w:gz") as output:
                output.add(payload_root, arcname=component, recursive=True)
                if external_tree is not None:
                    output.add(
                        external_tree,
                        arcname=f"{component}/objects",
                        recursive=True,
                    )
            return ExportedArchive(
                path=archive,
                sha256=_sha256(archive),
                files=files,
                bytes=archive.stat().st_size,
                unavailable=tuple(sorted(coverage.unavailable)),
            )
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _export_metadata(
        self, repository: Resource, root: Path, coverage: _Coverage
    ) -> None:
        base = _repo_api(repository)
        self._write_single(base, root / "repository.json", coverage, required=True)
        endpoints: tuple[tuple[str, str, str | None], ...] = (
            ("issues", f"{base}/issues?state=all", None),
            ("issue-comments", f"{base}/issues/comments", None),
            ("issue-events", f"{base}/issues/events", None),
            ("pull-requests", f"{base}/pulls?state=all", None),
            ("pull-review-comments", f"{base}/pulls/comments", None),
            ("commit-comments", f"{base}/comments", None),
            ("labels", f"{base}/labels", None),
            ("milestones", f"{base}/milestones?state=all", None),
            ("branches", f"{base}/branches", None),
            ("rulesets", f"{base}/rulesets", None),
            ("collaborators", f"{base}/collaborators?affiliation=all", None),
            ("teams", f"{base}/teams", None),
            ("deploy-keys", f"{base}/keys", None),
            ("webhooks", f"{base}/hooks", None),
            ("deployments", f"{base}/deployments", None),
            ("environments", f"{base}/environments", "environments"),
            ("actions-secrets", f"{base}/actions/secrets", "secrets"),
            ("dependabot-secrets", f"{base}/dependabot/secrets", "secrets"),
            ("codespaces-secrets", f"{base}/codespaces/secrets", "secrets"),
            ("code-scanning-alerts-open", f"{base}/code-scanning/alerts?state=open", None),
            ("code-scanning-alerts-closed", f"{base}/code-scanning/alerts?state=closed", None),
            ("secret-scanning-alerts-open", f"{base}/secret-scanning/alerts?state=open", None),
            (
                "secret-scanning-alerts-resolved",
                f"{base}/secret-scanning/alerts?state=resolved",
                None,
            ),
            ("dependabot-alerts-open", f"{base}/dependabot/alerts?state=open", None),
            ("dependabot-alerts-dismissed", f"{base}/dependabot/alerts?state=dismissed", None),
            ("dependabot-alerts-fixed", f"{base}/dependabot/alerts?state=fixed", None),
            (
                "dependabot-alerts-auto-dismissed",
                f"{base}/dependabot/alerts?state=auto_dismissed",
                None,
            ),
            ("repository-security-advisories", f"{base}/security-advisories", None),
        )
        pull_numbers: list[int] = []
        for name, endpoint, key in endpoints:
            identifiers = self._write_collection(
                name,
                endpoint,
                root / f"{name}.jsonl",
                coverage,
                list_key=key,
                collect=name == "pull-requests",
            )
            if name == "pull-requests":
                pull_numbers = [
                    number
                    for item in identifiers
                    if isinstance((number := item.get("number")), int)
                ]
        reviews = root / "pull-reviews.jsonl"
        details = root / "pull-details.jsonl"
        with (
            reviews.open("w", encoding="utf-8") as review_output,
            details.open("w", encoding="utf-8") as detail_output,
        ):
            for number in pull_numbers:
                detail_name = f"pull-details/{number}"
                try:
                    detail = self.github.get_json(f"{base}/pulls/{number}")
                    detail_output.write(json.dumps(detail, sort_keys=True) + "\n")
                    coverage.exported.append(detail_name)
                except GitHubAPIError as exc:
                    if exc.status_code not in _EXPECTED_UNAVAILABLE:
                        raise
                    coverage.unavailable.append(detail_name)
                name = f"pull-reviews/{number}"
                try:
                    for item in self.github.paginate(f"{base}/pulls/{number}/reviews"):
                        review_output.write(json.dumps(item, sort_keys=True) + "\n")
                    coverage.exported.append(name)
                except GitHubAPIError as exc:
                    if exc.status_code not in _EXPECTED_UNAVAILABLE:
                        raise
                    coverage.unavailable.append(name)

    def _export_releases(
        self, repository: Resource, root: Path, coverage: _Coverage
    ) -> None:
        endpoint = f"{_repo_api(repository)}/releases"
        releases = self._write_collection(
            "releases", endpoint, root / "releases.jsonl", coverage, collect=True
        )
        for release in releases:
            for asset in release.get("assets", []):
                if not isinstance(asset, dict):
                    continue
                asset_id = asset.get("id")
                asset_url = asset.get("url")
                if not isinstance(asset_id, int) or not isinstance(asset_url, str):
                    continue
                name = _safe_name(str(asset.get("name") or "asset"))
                target = root / "assets" / f"{asset_id}-{name}"
                self._download(f"release-asset/{asset_id}", asset_url, target, coverage)

    def _export_actions(
        self, repository: Resource, root: Path, coverage: _Coverage
    ) -> None:
        base = _repo_api(repository)
        artifacts = self._write_collection(
            "actions/artifacts",
            f"{base}/actions/artifacts",
            root / "artifacts.jsonl",
            coverage,
            list_key="artifacts",
            collect=True,
        )
        for artifact in artifacts:
            artifact_id = artifact.get("id")
            if not isinstance(artifact_id, int):
                continue
            if artifact.get("expired") is True:
                coverage.skipped.append(f"actions/artifact/{artifact_id}:expired")
                continue
            target = root / "artifacts" / f"{artifact_id}.zip"
            self._download(
                f"actions/artifact/{artifact_id}",
                f"{base}/actions/artifacts/{artifact_id}/zip",
                target,
                coverage,
            )
        runs = self._write_collection(
            "actions/runs",
            f"{base}/actions/runs?status=completed",
            root / "runs.jsonl",
            coverage,
            list_key="workflow_runs",
            collect=True,
        )
        for run in runs:
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            self._download(
                f"actions/run-log/{run_id}",
                f"{base}/actions/runs/{run_id}/logs",
                root / "logs" / f"{run_id}.zip",
                coverage,
            )

    def _export_lfs(
        self,
        repository: Resource,
        mirror_path: Path,
        coverage: _Coverage,
    ) -> Path:
        objects = self.git.fetch_lfs(mirror_path, repository.clone_url)
        coverage.exported.append("lfs/objects")
        return objects

    def _write_single(
        self,
        name: str,
        destination: Path,
        coverage: _Coverage,
        *,
        required: bool = False,
    ) -> None:
        try:
            payload = self.github.get_json(name)
            _write_json(destination, payload)
            coverage.exported.append(str(name))
        except GitHubAPIError as exc:
            if required or exc.status_code not in _EXPECTED_UNAVAILABLE:
                raise
            coverage.unavailable.append(str(name))

    def _write_collection(
        self,
        name: str,
        endpoint: str,
        destination: Path,
        coverage: _Coverage,
        *,
        list_key: str | None = None,
        collect: bool = False,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8") as output:
                for item in self.github.paginate(endpoint, list_key=list_key):
                    output.write(json.dumps(item, sort_keys=True) + "\n")
                    if collect:
                        items.append(item)
            coverage.exported.append(name)
        except GitHubAPIError as exc:
            destination.unlink(missing_ok=True)
            if exc.status_code not in _EXPECTED_UNAVAILABLE:
                raise
            coverage.unavailable.append(name)
        return items

    def _download(
        self,
        name: str,
        endpoint: str,
        destination: Path,
        coverage: _Coverage,
    ) -> None:
        try:
            self.github.download(endpoint, destination)
            coverage.exported.append(name)
        except GitHubAPIError as exc:
            destination.unlink(missing_ok=True)
            if exc.status_code not in _EXPECTED_UNAVAILABLE:
                raise
            coverage.unavailable.append(name)


def _repo_api(repository: Resource) -> str:
    owner = quote(repository.owner, safe="")
    name = quote(repository.name, safe="")
    return f"/repos/{owner}/{name}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2, sort_keys=True)
        output.write("\n")


def _safe_name(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in value
    )
    return safe[:180] or "asset"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
