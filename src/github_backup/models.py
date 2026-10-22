"""Domain models shared by discovery, backup, and storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

ResourceKind = Literal["repository", "gist", "wiki"]
ResultStatus = Literal["success", "partial", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class Resource:
    kind: ResourceKind
    identifier: str
    name: str
    owner: str
    clone_url: str
    html_url: str
    private: bool
    updated_at: str | None = None
    archived: bool = False
    default_branch: str | None = None
    has_wiki: bool = False

    @property
    def relative_path(self) -> str:
        if self.kind == "repository":
            return f"repos/{self.owner}/{self.name}.git"
        if self.kind == "wiki":
            return f"wikis/{self.owner}/{self.name}.wiki.git"
        return f"gists/{self.identifier}.git"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackupResult:
    resource: Resource
    status: ResultStatus
    started_at: str
    finished_at: str
    duration_seconds: float
    changed: bool = False
    ref_digest: str | None = None
    artifact: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resource"] = self.resource.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ComponentResult:
    repository: str
    component: str
    status: ResultStatus
    started_at: str
    finished_at: str
    duration_seconds: float
    artifact: str | None = None
    sha256: str | None = None
    files: int = 0
    bytes: int = 0
    unavailable: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunManifest:
    run_id: str
    user: str
    storage: str
    scope: str
    mode: str
    started_at: str
    finished_at: str | None = None
    results: list[BackupResult] = field(default_factory=list)
    components: list[ComponentResult] = field(default_factory=list)
    fatal_error: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(result.status == status for result in self.results)
            + sum(result.status == status for result in self.components)
            for status in ("success", "partial", "failed", "skipped")
        }

    def finish(self) -> None:
        self.finished_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "run_id": self.run_id,
            "user": self.user,
            "storage": self.storage,
            "scope": self.scope,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": self.counts,
            "fatal_error": self.fatal_error,
            "results": [result.to_dict() for result in self.results],
            "components": [result.to_dict() for result in self.components],
        }
