"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from github_backup import __version__
from github_backup.backup import BackupService
from github_backup.config import (
    BackupConfig,
    BackupMode,
    ScopeKind,
    StorageKind,
    load_config,
    read_token,
)
from github_backup.errors import BackupError
from github_backup.github import GitHubClient
from github_backup.gitops import GitRunner
from github_backup.logging_utils import configure_logging
from github_backup.storage import create_storage

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-backup",
        description="Back up every GitHub repository and gist accessible to a token.",
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
    list_command.add_argument("--json", action="store_true", help="emit JSON")

    check = subparsers.add_parser("check", help="validate Git, credentials, and resource discovery")
    check.add_argument(
        "--scope", choices=("accessible", "owned", "organizations"), help="repository scope"
    )
    check.add_argument(
        "--organization", action="append", dest="organizations", help="organization to include"
    )
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
    parser.add_argument("--workers", type=int, help="parallel Git transfers (default: 4)")
    parser.add_argument("--s3-bucket", help="S3 bucket")
    parser.add_argument("--s3-prefix", help="key prefix")
    parser.add_argument("--s3-endpoint-url", help="S3-compatible endpoint URL")
    parser.add_argument("--s3-region", help="S3 region")


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
        token = read_token(config.github)
        github = GitHubClient(config.github, token)
        git = GitRunner(token, timeout_seconds=config.git_timeout_seconds)

        if arguments.command == "list":
            _list_resources(config, github, arguments.json)
            return
        if arguments.command == "check":
            git_version = git.ensure_available()
            user = github.authenticated_user()
            repos = github.repositories(config.scope, config.organizations)
            gists = github.gists(user) if config.include_gists else []
            print(
                f"OK: {git_version}; authenticated as {user}; "
                f"{len(repos)} repos; {len(gists)} gists"
            )
            return

        storage = create_storage(config)
        service = BackupService(config, github, git, storage)
        manifest, manifest_location = service.run()
        counts = manifest.counts
        print(
            f"Backup finished: {counts['success']} succeeded, {counts['failed']} failed, "
            f"{counts['skipped']} skipped. Manifest: {manifest_location}"
        )
        if counts["failed"]:
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
    organization_values = cast(list[str] | None, getattr(arguments, "organizations", None))
    organizations = (
        tuple(organization_values) if organization_values is not None else config.organizations
    )
    gist_value = cast(bool | None, getattr(arguments, "gists", None))
    include_gists = gist_value if gist_value is not None else config.include_gists

    s3_config = config.s3
    s3_updates: dict[str, str] = {}
    for argument_name, field_name in (
        ("s3_bucket", "bucket"),
        ("s3_prefix", "prefix"),
        ("s3_endpoint_url", "endpoint_url"),
        ("s3_region", "region"),
    ):
        value = getattr(arguments, argument_name, None)
        if value is not None:
            s3_updates[field_name] = value
    if s3_updates:
        s3_config = replace(config.s3, **s3_updates)
    return replace(
        config,
        storage=storage,
        destination=destination,
        cache_dir=cache_dir,
        scope=scope,
        organizations=organizations,
        mode=mode,
        include_gists=include_gists,
        workers=workers,
        s3=s3_config,
    ).validated()
