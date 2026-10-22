"""Configuration loading, validation, and environment overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from github_backup.errors import ConfigurationError

StorageKind = Literal["filesystem", "s3"]
ScopeKind = Literal["accessible", "owned", "organizations"]
BackupMode = Literal["incremental", "snapshot"]
AdditionalData = Literal["wikis", "metadata", "releases", "actions", "lfs"]
ALL_ADDITIONAL_DATA: tuple[AdditionalData, ...] = (
    "wikis",
    "metadata",
    "releases",
    "actions",
    "lfs",
)


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    api_url: str = "https://api.github.com"
    token_env: str = "GITHUB_TOKEN"
    api_version: str = "2022-11-28"
    timeout_seconds: float = 30.0
    max_retries: int = 5
    allow_insecure_http: bool = False


@dataclass(frozen=True, slots=True)
class S3Config:
    bucket: str = ""
    prefix: str = "github-backup"
    endpoint_url: str | None = None
    region: str | None = None
    storage_class: str | None = None
    server_side_encryption: str | None = "AES256"
    lock_lease_seconds: int = 900
    allow_insecure_endpoint: bool = False


@dataclass(frozen=True, slots=True)
class BackupConfig:
    storage: StorageKind = "filesystem"
    destination: Path = Path("./backups")
    cache_dir: Path = Path("~/.cache/github-backup")
    scope: ScopeKind = "accessible"
    organizations: tuple[str, ...] = ()
    mode: BackupMode = "incremental"
    include_gists: bool = True
    additional_data: tuple[AdditionalData, ...] = ()
    workers: int = 4
    git_timeout_seconds: int = 3600
    git_max_retries: int = 3
    verify_mirrors: bool = True
    lock_timeout_seconds: int = 0
    minimum_free_space_gib: float = 1.0
    allow_insecure_permissions: bool = False
    snapshot_retention: int = 0
    github: GitHubConfig = field(default_factory=GitHubConfig)
    s3: S3Config = field(default_factory=S3Config)

    def validated(self) -> BackupConfig:
        if self.storage not in ("filesystem", "s3"):
            raise ConfigurationError("storage must be 'filesystem' or 's3'")
        if self.scope not in ("accessible", "owned", "organizations"):
            raise ConfigurationError("scope must be accessible, owned, or organizations")
        if self.mode not in ("incremental", "snapshot"):
            raise ConfigurationError("mode must be incremental or snapshot")
        if self.scope == "organizations" and not self.organizations:
            raise ConfigurationError("scope 'organizations' requires at least one organization")
        if self.storage == "s3" and not self.s3.bucket:
            raise ConfigurationError("S3 storage requires s3.bucket or --s3-bucket")
        if not 1 <= self.workers <= 32:
            raise ConfigurationError("workers must be between 1 and 32")
        if self.github.max_retries < 0:
            raise ConfigurationError("github.max_retries cannot be negative")
        if self.git_max_retries < 0:
            raise ConfigurationError("git_max_retries cannot be negative")
        if self.github.timeout_seconds <= 0 or self.git_timeout_seconds <= 0:
            raise ConfigurationError("timeouts must be positive")
        if self.lock_timeout_seconds < 0:
            raise ConfigurationError("lock_timeout_seconds cannot be negative")
        if self.minimum_free_space_gib < 0:
            raise ConfigurationError("minimum_free_space_gib cannot be negative")
        if self.snapshot_retention < 0:
            raise ConfigurationError("snapshot_retention cannot be negative")
        unknown_data = set(self.additional_data) - set(ALL_ADDITIONAL_DATA)
        if unknown_data:
            raise ConfigurationError(
                "additional_data contains unsupported values: "
                + ", ".join(sorted(unknown_data))
            )
        if self.s3.lock_lease_seconds < 60:
            raise ConfigurationError("s3.lock_lease_seconds must be at least 60")
        _validate_service_url(
            self.github.api_url,
            key="github.api_url",
            allow_insecure=self.github.allow_insecure_http,
            override="github.allow_insecure_http=true",
        )
        if self.s3.endpoint_url is not None:
            _validate_service_url(
                self.s3.endpoint_url,
                key="s3.endpoint_url",
                allow_insecure=self.s3.allow_insecure_endpoint,
                override="s3.allow_insecure_endpoint=true",
            )
        return replace(
            self,
            destination=self.destination.expanduser().resolve(),
            cache_dir=self.cache_dir.expanduser().resolve(),
            organizations=tuple(
                dict.fromkeys(org.strip() for org in self.organizations if org.strip())
            ),
            additional_data=tuple(dict.fromkeys(self.additional_data)),
            github=replace(self.github, api_url=self.github.api_url.rstrip("/")),
            s3=replace(self.s3, prefix=self.s3.prefix.strip("/")),
        )


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"[{key}] must be a TOML table")
    return value


def _string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings")
    return tuple(value)


def load_config(path: Path | None) -> BackupConfig:
    data: Mapping[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as stream:
                parsed = tomllib.load(stream)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"configuration file does not exist: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc
        data = parsed

    backup = _section(data, "backup")
    github = _section(data, "github")
    s3 = _section(data, "s3")

    try:
        config = BackupConfig(
            storage=cast(StorageKind, backup.get("storage", "filesystem")),
            destination=Path(backup.get("destination", "./backups")),
            cache_dir=Path(backup.get("cache_dir", "~/.cache/github-backup")),
            scope=cast(ScopeKind, backup.get("scope", "accessible")),
            organizations=_string_tuple(backup.get("organizations"), "backup.organizations"),
            mode=cast(BackupMode, backup.get("mode", "incremental")),
            include_gists=_boolean(backup.get("include_gists", True), "backup.include_gists"),
            additional_data=cast(
                tuple[AdditionalData, ...],
                _string_tuple(backup.get("additional_data"), "backup.additional_data"),
            ),
            workers=int(backup.get("workers", 4)),
            git_timeout_seconds=int(backup.get("git_timeout_seconds", 3600)),
            git_max_retries=int(backup.get("git_max_retries", 3)),
            verify_mirrors=_boolean(
                backup.get("verify_mirrors", True), "backup.verify_mirrors"
            ),
            lock_timeout_seconds=int(backup.get("lock_timeout_seconds", 0)),
            minimum_free_space_gib=float(backup.get("minimum_free_space_gib", 1.0)),
            allow_insecure_permissions=_boolean(
                backup.get("allow_insecure_permissions", False),
                "backup.allow_insecure_permissions",
            ),
            snapshot_retention=int(backup.get("snapshot_retention", 0)),
            github=GitHubConfig(
                api_url=str(github.get("api_url", "https://api.github.com")),
                token_env=str(github.get("token_env", "GITHUB_TOKEN")),
                api_version=str(github.get("api_version", "2022-11-28")),
                timeout_seconds=float(github.get("timeout_seconds", 30.0)),
                max_retries=int(github.get("max_retries", 5)),
                allow_insecure_http=_boolean(
                    github.get("allow_insecure_http", False), "github.allow_insecure_http"
                ),
            ),
            s3=S3Config(
                bucket=str(s3.get("bucket", "")),
                prefix=str(s3.get("prefix", "github-backup")),
                endpoint_url=_optional_string(s3.get("endpoint_url")),
                region=_optional_string(s3.get("region")),
                storage_class=_optional_string(s3.get("storage_class")),
                server_side_encryption=_optional_string(
                    s3.get("server_side_encryption", "AES256")
                ),
                lock_lease_seconds=int(s3.get("lock_lease_seconds", 900)),
                allow_insecure_endpoint=_boolean(
                    s3.get("allow_insecure_endpoint", False),
                    "s3.allow_insecure_endpoint",
                ),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid configuration value: {exc}") from exc
    return apply_environment(config).validated()


def _optional_string(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be true or false")
    return value


def _validate_service_url(
    value: str, *, key: str, allow_insecure: bool, override: str
) -> None:
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (scheme != "https" and not (allow_insecure and scheme == "http"))
    ):
        raise ConfigurationError(
            f"{key} must use HTTPS and include a valid hostname; "
            f"set {override} only for isolated testing"
        )


def apply_environment(config: BackupConfig) -> BackupConfig:
    """Apply non-secret operational overrides from the environment."""

    updates: dict[str, Any] = {}
    if value := os.getenv("GITHUB_BACKUP_DESTINATION"):
        updates["destination"] = Path(value)
    if value := os.getenv("GITHUB_BACKUP_STORAGE"):
        updates["storage"] = value
    if value := os.getenv("GITHUB_BACKUP_SCOPE"):
        updates["scope"] = value
    if value := os.getenv("GITHUB_BACKUP_MODE"):
        updates["mode"] = value
    if value := os.getenv("GITHUB_BACKUP_CACHE_DIR"):
        updates["cache_dir"] = Path(value)
    return replace(config, **updates)


def read_token(config: GitHubConfig) -> str:
    token = os.getenv(config.token_env, "").strip()
    if not token:
        raise ConfigurationError(
            f"GitHub token is missing; set the {config.token_env} environment variable"
        )
    if any(character.isspace() for character in token):
        raise ConfigurationError(f"{config.token_env} contains whitespace")
    return token
