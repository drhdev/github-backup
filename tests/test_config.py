from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from github_backup.config import load_config, read_token
from github_backup.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults_match_product_decisions(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(None)
        self.assertEqual(config.storage, "filesystem")
        self.assertEqual(config.scope, "accessible")
        self.assertEqual(config.mode, "incremental")
        self.assertTrue(config.include_gists)

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
