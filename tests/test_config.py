from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from github_backup.config import BackupConfig, S3Config, load_config, read_token
from github_backup.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults_match_product_decisions(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(None)
        self.assertEqual(config.storage, "filesystem")
        self.assertEqual(config.scope, "accessible")
        self.assertEqual(config.mode, "incremental")
        self.assertTrue(config.include_gists)
        self.assertEqual(config.additional_data, ())

    def test_toml_is_loaded_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[backup]
storage = "filesystem"
destination = "./archive"
scope = "organizations"
organizations = ["example", "example"]
mode = "snapshot"
workers = 2
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)
        self.assertEqual(config.organizations, ("example",))
        self.assertEqual(config.mode, "snapshot")
        self.assertTrue(config.destination.is_absolute())

    def test_organization_scope_requires_organization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[backup]\nscope = "organizations"\n', encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(ConfigurationError),
            ):
                load_config(path)

    def test_token_is_read_only_from_named_environment_variable(self) -> None:
        config = load_config(None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret-value"}, clear=True):
            self.assertEqual(read_token(config.github), "secret-value")

    def test_boolean_values_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[backup]\ninclude_gists = "false"\n', encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(ConfigurationError, "must be true or false"),
            ):
                load_config(path)

    def test_http_api_requires_explicit_insecure_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[github]\napi_url = "http://github.test"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "must use HTTPS"):
                load_config(path)
            path.write_text(
                '[github]\napi_url = "http://github.test"\nallow_insecure_http = true\n',
                encoding="utf-8",
            )
            self.assertTrue(load_config(path).github.allow_insecure_http)

    def test_production_hardening_defaults_are_enabled(self) -> None:
        config = load_config(None)
        self.assertTrue(config.verify_mirrors)
        self.assertEqual(config.git_max_retries, 3)
        self.assertFalse(config.allow_insecure_permissions)
        self.assertEqual(config.s3.server_side_encryption, "AES256")

    def test_s3_http_endpoint_requires_explicit_insecure_opt_in(self) -> None:
        config = BackupConfig(
            storage="s3",
            s3=S3Config(bucket="bucket", endpoint_url="http://minio.test:9000"),
        )
        with self.assertRaisesRegex(ConfigurationError, "s3.endpoint_url.*HTTPS"):
            config.validated()
        allowed = replace(
            config,
            s3=replace(config.s3, allow_insecure_endpoint=True),
        ).validated()
        self.assertTrue(allowed.s3.allow_insecure_endpoint)

    def test_additional_data_is_validated(self) -> None:
        config = BackupConfig(additional_data=("metadata", "wikis"))  # type: ignore[arg-type]
        self.assertEqual(config.validated().additional_data, ("metadata", "wikis"))
        invalid = BackupConfig(additional_data=("unknown",))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ConfigurationError, "unsupported values: unknown"):
            invalid.validated()
