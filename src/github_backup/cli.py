"""Command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import tarfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from github_backup import __version__
from github_backup.backup import BackupService, _wiki_resource
from github_backup.config import (
    ALL_ADDITIONAL_DATA,
    AdditionalData,
    BackupConfig,
    BackupMode,
    ScopeKind,
    StorageKind,
    load_config,
    read_token,
)
from github_backup.errors import BackupError, ConfigurationError
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner
from github_backup.logging_utils import configure_logging
from github_backup.storage import create_storage

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-backup",
        description="Back up GitHub Git data and optional service data accessible to a token.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML configuration file (default: ./github-backup.toml when present)",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--json-logs", action="store_true", help="emit structured JSON logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="discover and back up resources")
    _add_runtime_options(run)

    list_command = subparsers.add_parser("list", help="list resources without backing them up")
    list_command.add_argument(
        "--scope", choices=("accessible", "owned", "organizations"), help="repository scope"
    )
    list_command.add_argument(
        "--organization", action="append", dest="organizations", help="organization to include"
    )
    list_command.add_argument(
        "--gists", action=argparse.BooleanOptionalAction, default=None, help="include gists"
    )
    _add_additional_data_option(list_command)
    list_command.add_argument("--json", action="store_true", help="emit JSON")

    check = subparsers.add_parser("check", help="validate Git, credentials, and resource discovery")
    check.add_argument(
        "--scope", choices=("accessible", "owned", "organizations"), help="repository scope"
    )
    check.add_argument(
        "--organization", action="append", dest="organizations", help="organization to include"
    )
    _add_additional_data_option(check)

    verify = subparsers.add_parser("verify", help="verify local mirrors and snapshot artifacts")
    verify.add_argument("--destination", type=Path, help="local/NAS backup directory")
    return parser


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--storage", choices=("filesystem", "s3"), help="storage backend (default: filesystem)"
    )
    parser.add_argument(
        "--destination", type=Path, help="local/NAS backup directory (default: ./backups)"
    )
    parser.add_argument("--cache-dir", type=Path, help="local mirror cache used by S3 storage")
    parser.add_argument(
        "--scope",
        choices=("accessible", "owned", "organizations"),
        help="repositories to include (default: accessible)",
    )
    parser.add_argument(
        "--organization",
        action="append",
        dest="organizations",
        help="organization to include; repeat as needed",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "snapshot"),
        help="backup mode (default: incremental)",
    )
    parser.add_argument(
        "--gists", action=argparse.BooleanOptionalAction, default=None, help="include user gists"
    )
    _add_additional_data_option(parser)
    parser.add_argument("--workers", type=int, help="parallel Git transfers (default: 4)")
    parser.add_argument("--git-retries", type=int, help="retries for transient Git failures")
    parser.add_argument(
        "--verify-mirrors",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run Git connectivity checks after each update",
    )
    parser.add_argument(
        "--minimum-free-space-gib", type=float, help="required free local working space"
    )
    parser.add_argument(
        "--snapshot-retention",
        type=int,
        help="number of successful snapshots to keep; 0 keeps all",
    )
    parser.add_argument(
        "--allow-insecure-permissions",
        action="store_true",
        default=None,
        help="allow group/other access to the destination (not recommended)",
    )
    parser.add_argument("--s3-bucket", help="S3 bucket")
    parser.add_argument("--s3-prefix", help="key prefix")
    parser.add_argument("--s3-endpoint-url", help="S3-compatible endpoint URL")
    parser.add_argument("--s3-region", help="S3 region")
    parser.add_argument(
        "--s3-server-side-encryption",
        help="S3 encryption algorithm (default: AES256; use 'none' to disable)",
    )
    parser.add_argument(
        "--allow-insecure-s3-endpoint",
        action="store_true",
        default=None,
        help="allow an HTTP S3 endpoint (isolated testing only)",
    )


def _add_additional_data_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include",
        action="append",
        choices=(*ALL_ADDITIONAL_DATA, "all"),
        dest="additional_data",
        help=(
            "optional data component; repeat as needed or use 'all' "
            "(wikis, metadata, releases, actions, lfs)"
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    configure_logging(verbose=arguments.verbose, json_logs=arguments.json_logs)
    try:
        config_path = arguments.config
        if config_path is None:
            default_path = Path("github-backup.toml")
            config_path = default_path if default_path.is_file() else None
        config = _apply_cli_overrides(load_config(config_path), arguments)
        if arguments.command == "verify":
            _verify_local(config)
            return
        token = read_token(config.github)
        github = GitHubClient(config.github, token)
        git = GitRunner(
            token,
            timeout_seconds=config.git_timeout_seconds,
            max_retries=config.git_max_retries,
            verify_mirrors=config.verify_mirrors,
            allow_insecure_http=config.github.allow_insecure_http,
        )

        if arguments.command == "list":
            _list_resources(config, github, arguments.json)
            return
        if arguments.command == "check":
            git_version = git.ensure_available()
            if "lfs" in config.additional_data:
                git.ensure_lfs_available()
            user = github.authenticated_user()
            repos = github.repositories(config.scope, config.organizations)
            gists = github.gists(user) if config.include_gists else []
            storage = create_storage(config)
            storage.prepare()
            print(
                f"OK: {git_version}; authenticated as {user}; "
                f"{len(repos)} repos; {len(gists)} gists; "
                f"additional={','.join(config.additional_data) or 'none'}"
            )
            return

        storage = create_storage(config)
        service = BackupService(config, github, git, storage)
        manifest, manifest_location = service.run()
        counts = manifest.counts
        print(
            f"Backup finished: {counts['success']} succeeded, {counts['partial']} partial, "
            f"{counts['failed']} failed, {counts['skipped']} skipped. "
            f"Manifest: {manifest_location}"
        )
        if counts["failed"] or counts["partial"]:
            raise SystemExit(2)
    except BackupError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt as exc:
        LOGGER.error("Interrupted")
        raise SystemExit(130) from exc


def _list_resources(config: BackupConfig, github: GitHubClient, as_json: bool) -> None:
    user = github.authenticated_user()
    resources = github.repositories(config.scope, config.organizations)
    repositories = list(resources)
    if "wikis" in config.additional_data:
        resources.extend(
            _wiki_resource(repository) for repository in repositories if repository.has_wiki
        )
    if config.include_gists:
        resources.extend(github.gists(user))
    resources.sort(key=lambda resource: resource.relative_path.casefold())
    if as_json:
        print(json.dumps([resource.to_dict() for resource in resources], indent=2, sort_keys=True))
        return
    for resource in resources:
        privacy = "private" if resource.private else "public"
        print(f"{resource.kind:10} {privacy:7} {resource.owner}/{resource.name}")
    print(f"{len(resources)} resources accessible to {user}")


def _apply_cli_overrides(config: BackupConfig, arguments: argparse.Namespace) -> BackupConfig:
    storage = cast(StorageKind, getattr(arguments, "storage", None) or config.storage)
    destination = cast(Path | None, getattr(arguments, "destination", None)) or config.destination
    cache_dir = cast(Path | None, getattr(arguments, "cache_dir", None)) or config.cache_dir
    scope = cast(ScopeKind, getattr(arguments, "scope", None) or config.scope)
    mode = cast(BackupMode, getattr(arguments, "mode", None) or config.mode)
    worker_value = cast(int | None, getattr(arguments, "workers", None))
    workers = worker_value if worker_value is not None else config.workers
    git_retries_value = cast(int | None, getattr(arguments, "git_retries", None))
    git_max_retries = (
        git_retries_value if git_retries_value is not None else config.git_max_retries
    )
    verify_value = cast(bool | None, getattr(arguments, "verify_mirrors", None))
    verify_mirrors = verify_value if verify_value is not None else config.verify_mirrors
    free_space_value = cast(float | None, getattr(arguments, "minimum_free_space_gib", None))
    minimum_free_space_gib = (
        free_space_value if free_space_value is not None else config.minimum_free_space_gib
    )
    retention_value = cast(int | None, getattr(arguments, "snapshot_retention", None))
    snapshot_retention = (
        retention_value if retention_value is not None else config.snapshot_retention
    )
    insecure_permissions_value = cast(
        bool | None, getattr(arguments, "allow_insecure_permissions", None)
    )
    allow_insecure_permissions = (
        insecure_permissions_value
        if insecure_permissions_value is not None
        else config.allow_insecure_permissions
    )
    organization_values = cast(list[str] | None, getattr(arguments, "organizations", None))
    organizations = (
        tuple(organization_values) if organization_values is not None else config.organizations
    )
    gist_value = cast(bool | None, getattr(arguments, "gists", None))
    include_gists = gist_value if gist_value is not None else config.include_gists
    additional_values = cast(list[str] | None, getattr(arguments, "additional_data", None))
    if additional_values is None:
        additional_data = config.additional_data
    elif "all" in additional_values:
        additional_data = ALL_ADDITIONAL_DATA
    else:
        additional_data = cast(tuple[AdditionalData, ...], tuple(additional_values))

    s3_bucket = cast(str | None, getattr(arguments, "s3_bucket", None)) or config.s3.bucket
    s3_prefix = cast(str | None, getattr(arguments, "s3_prefix", None)) or config.s3.prefix
    s3_endpoint_url = (
        cast(str | None, getattr(arguments, "s3_endpoint_url", None))
        or config.s3.endpoint_url
    )
    s3_region = cast(str | None, getattr(arguments, "s3_region", None)) or config.s3.region
    encryption_value = cast(
        str | None, getattr(arguments, "s3_server_side_encryption", None)
    )
    s3_encryption = (
        None
        if encryption_value == "none"
        else encryption_value or config.s3.server_side_encryption
    )
    insecure_s3_value = cast(
        bool | None, getattr(arguments, "allow_insecure_s3_endpoint", None)
    )
    allow_insecure_s3_endpoint = (
        insecure_s3_value
        if insecure_s3_value is not None
        else config.s3.allow_insecure_endpoint
    )
    s3_config = replace(
        config.s3,
        bucket=s3_bucket,
        prefix=s3_prefix,
        endpoint_url=s3_endpoint_url,
        region=s3_region,
        server_side_encryption=s3_encryption,
        allow_insecure_endpoint=allow_insecure_s3_endpoint,
    )
    return replace(
        config,
        storage=storage,
        destination=destination,
        cache_dir=cache_dir,
        scope=scope,
        organizations=organizations,
        mode=mode,
        include_gists=include_gists,
        additional_data=additional_data,
        workers=workers,
        git_max_retries=git_max_retries,
        verify_mirrors=verify_mirrors,
        minimum_free_space_gib=minimum_free_space_gib,
        snapshot_retention=snapshot_retention,
        allow_insecure_permissions=allow_insecure_permissions,
        s3=s3_config,
    ).validated()


def _verify_local(config: BackupConfig) -> None:
    if config.storage != "filesystem":
        raise ConfigurationError("verify currently requires filesystem storage")
    storage = create_storage(config)
    storage.prepare()
    git = GitRunner("", timeout_seconds=config.git_timeout_seconds, max_retries=0)
    mirrors = sorted(path for path in storage.workspace_root.rglob("*.git") if path.is_dir())
    for mirror in mirrors:
        git.verify_mirror(mirror)
    bundles = sorted(config.destination.glob("snapshots/**/*.bundle"))
    for bundle in bundles:
        relative = bundle.relative_to(config.destination / "snapshots")
        mirror_relative = Path(*relative.parts[1:]).with_suffix("")
        mirror = storage.workspace_root / mirror_relative
        if not mirror.is_dir():
            raise ConfigurationError(f"cannot verify bundle without mirror: {bundle}")
        git.verify_bundle(mirror, bundle)
    archives = sorted(config.destination.glob("snapshots/**/*.empty.tar.gz"))
    for archive in archives:
        if not tarfile.is_tarfile(archive):
            raise ConfigurationError(f"invalid empty-repository archive: {archive}")
    supplements = sorted(config.destination.glob("supplemental/**/*.tar.gz"))
    supplements.extend(
        archive
        for archive in config.destination.glob("snapshots/**/*.tar.gz")
        if not archive.name.endswith(".empty.tar.gz")
    )
    for archive in supplements:
        if not tarfile.is_tarfile(archive):
            raise ConfigurationError(f"invalid supplementary archive: {archive}")
    digests_verified = _verify_component_digests(config.destination)
    print(
        f"Verified {len(mirrors)} mirrors, {len(bundles)} bundles, "
        f"{len(archives)} empty-repository archives, and "
        f"{len(supplements)} supplementary archives "
        f"({digests_verified} SHA-256 digests)"
    )


def _verify_component_digests(destination: Path) -> int:
    latest = destination / "manifests" / "latest.json"
    if not latest.is_file():
        return 0
    try:
        manifest = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read latest manifest: {exc}") from exc
    components = manifest.get("components", [])
    if not isinstance(components, list):
        raise ConfigurationError("latest manifest has an invalid components field")
    verified = 0
    for component in components:
        if not isinstance(component, dict):
            raise ConfigurationError("latest manifest contains an invalid component")
        artifact = component.get("artifact")
        expected = component.get("sha256")
        if not isinstance(artifact, str) or not isinstance(expected, str):
            continue
        path = Path(artifact)
        if not path.is_file():
            raise ConfigurationError(f"supplementary artifact is missing: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ConfigurationError(f"SHA-256 mismatch for supplementary artifact: {path}")
        verified += 1
    return verified
