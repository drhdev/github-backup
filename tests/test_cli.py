from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from github_backup.cli import (
    _apply_cli_overrides,
    _verify_component_digests,
    build_parser,
    main,
)
from github_backup.config import BackupConfig
from github_backup.errors import ConfigurationError
from github_backup.models import BackupResult, Resource, RunManifest


def _resource() -> Resource:
    return Resource(
        kind="repository",
        identifier="1",
        name="project",
        owner="owner",
        clone_url="https://github.com/owner/project.git",
        html_url="https://github.com/owner/project",
        private=True,
    )


class CliTests(unittest.TestCase):
    def test_help_command_does_not_require_configuration_or_token(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "github_backup.cli.load_config",
                side_effect=AssertionError("configuration must not be loaded"),
            ),
            patch(
                "github_backup.cli.read_token",
                side_effect=AssertionError("token must not be read"),
            ),
            redirect_stdout(output),
        ):
            main(["help"])
        self.assertIn("usage: github-backup", output.getvalue())
        self.assertIn("help", output.getvalue())

    def test_help_command_prints_command_specific_options(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main(["help", "run"])
        self.assertIn("usage: github-backup run", output.getvalue())
        self.assertIn("--include", output.getvalue())
        self.assertIn("--snapshot-retention", output.getvalue())

    def test_runtime_flags_override_configuration(self) -> None:
        parser = build_parser()
        arguments = parser.parse_args(
            [
                "run",
                "--storage",
                "s3",
                "--s3-bucket",
                "bucket",
                "--scope",
                "organizations",
                "--organization",
                "example",
                "--mode",
                "snapshot",
                "--no-gists",
                "--workers",
                "2",
                "--include",
                "all",
            ]
        )
        config = _apply_cli_overrides(BackupConfig(), arguments)
        self.assertEqual(config.storage, "s3")
        self.assertEqual(config.s3.bucket, "bucket")
        self.assertEqual(config.organizations, ("example",))
        self.assertEqual(config.mode, "snapshot")
        self.assertFalse(config.include_gists)
        self.assertEqual(config.workers, 2)
        self.assertEqual(
            config.additional_data,
            ("wikis", "metadata", "releases", "actions", "lfs"),
        )

    def test_invalid_zero_workers_is_not_silently_ignored(self) -> None:
        arguments = build_parser().parse_args(["run", "--workers", "0"])
        with self.assertRaisesRegex(ConfigurationError, "workers must be between"):
            _apply_cli_overrides(BackupConfig(), arguments)

    def test_list_command_prints_resources(self) -> None:
        github = Mock()
        github.authenticated_user.return_value = "owner"
        github.repositories.return_value = [_resource()]
        github.gists.return_value = []
        output = io.StringIO()
        with (
            patch("github_backup.cli.load_config", return_value=BackupConfig()),
            patch("github_backup.cli.read_token", return_value="token"),
            patch("github_backup.cli.GitHubClient", return_value=github),
            redirect_stdout(output),
        ):
            main(["list"])
        self.assertIn("repository private owner/project", output.getvalue())
        self.assertIn("1 resources accessible to owner", output.getvalue())

    def test_check_command_prints_dependency_and_discovery_status(self) -> None:
        github = Mock()
        github.authenticated_user.return_value = "owner"
        github.repositories.return_value = [_resource()]
        github.gists.return_value = []
        git = Mock()
        git.ensure_available.return_value = "git version test"
        storage = Mock()
        output = io.StringIO()
        with (
            patch("github_backup.cli.load_config", return_value=BackupConfig()),
            patch("github_backup.cli.read_token", return_value="token"),
            patch("github_backup.cli.GitHubClient", return_value=github),
            patch("github_backup.cli.GitRunner", return_value=git),
            patch("github_backup.cli.create_storage", return_value=storage),
            redirect_stdout(output),
        ):
            main(["check"])
        self.assertIn("authenticated as owner; 1 repos; 0 gists", output.getvalue())
        storage.prepare.assert_called_once_with()

    def test_run_returns_two_when_a_resource_failed(self) -> None:
        resource = _resource()
        now = datetime.now(UTC).isoformat()
        manifest = RunManifest(
            run_id="run-id",
            user="owner",
            storage="filesystem",
            scope="accessible",
            mode="incremental",
            started_at=now,
            finished_at=now,
            results=[
                BackupResult(
                    resource=resource,
                    status="failed",
                    started_at=now,
                    finished_at=now,
                    duration_seconds=0.1,
                    error="failure",
                )
            ],
        )
        service = Mock()
        service.run.return_value = (manifest, "/backup/manifest.json")
        with (
            patch("github_backup.cli.load_config", return_value=BackupConfig()),
            patch("github_backup.cli.read_token", return_value="token"),
            patch("github_backup.cli.GitHubClient"),
            patch("github_backup.cli.GitRunner"),
            patch("github_backup.cli.create_storage"),
            patch("github_backup.cli.BackupService", return_value=service),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            main(["run"])

    def test_expected_error_returns_one(self) -> None:
        with (
            patch(
                "github_backup.cli.load_config",
                side_effect=ConfigurationError("bad configuration"),
            ),
            self.assertRaisesRegex(SystemExit, "1"),
        ):
            main(["run"])

    def test_default_config_file_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "github-backup.toml").write_text("", encoding="utf-8")
            github = Mock()
            github.authenticated_user.return_value = "owner"
            github.repositories.return_value = []
            github.gists.return_value = []
            with (
                patch("github_backup.cli.Path.is_file", return_value=True),
                patch("github_backup.cli.load_config", return_value=BackupConfig()) as loader,
                patch("github_backup.cli.read_token", return_value="token"),
                patch("github_backup.cli.GitHubClient", return_value=github),
                redirect_stdout(io.StringIO()),
            ):
                main(["list", "--json"])
            loader.assert_called_once_with(Path("github-backup.toml"))

    def test_verify_does_not_require_a_github_token(self) -> None:
        with (
            patch("github_backup.cli.load_config", return_value=BackupConfig()),
            patch("github_backup.cli._verify_local") as verify,
            patch(
                "github_backup.cli.read_token",
                side_effect=AssertionError("token must not be read"),
            ),
        ):
            main(["verify"])
        verify.assert_called_once()

    def test_component_digest_verification_detects_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "supplemental/latest/repos/o/r/metadata.tar.gz"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"archive")
            digest = hashlib.sha256(b"archive").hexdigest()
            manifest = root / "manifests/latest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "components": [
                            {"artifact": str(artifact), "sha256": digest}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_verify_component_digests(root), 1)
            artifact.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ConfigurationError, "SHA-256 mismatch"):
                _verify_component_digests(root)
