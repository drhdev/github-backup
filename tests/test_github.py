from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from github_backup.config import GitHubConfig
from github_backup.github import GitHubClient, _next_link


class StubGitHubClient(GitHubClient):
    def __init__(self, responses: dict[str, tuple[Any, Mapping[str, str]]]) -> None:
        super().__init__(GitHubConfig(), "not-a-real-token")
        self.responses = responses

    def _request_json(self, path_or_url: str) -> tuple[Any, Mapping[str, str]]:
        key = path_or_url.split("?", 1)[0]
        return self.responses[key]


class GitHubClientTests(unittest.TestCase):
    def test_next_link_parser(self) -> None:
        header = '<https://api.github.com/user/repos?page=2>; rel="next", <x>; rel="last"'
        self.assertEqual(_next_link(header), "https://api.github.com/user/repos?page=2")

    def test_repository_scopes_and_mapping(self) -> None:
        client = StubGitHubClient(
            {
                "/user/repos": (
                    [
                        {
                            "id": 1,
                            "name": "project",
                            "owner": {"login": "wanted-org"},
                            "clone_url": "https://github.com/wanted-org/project.git",
                            "html_url": "https://github.com/wanted-org/project",
                            "private": True,
                            "archived": False,
                            "default_branch": "main",
                            "updated_at": "2026-01-01T00:00:00Z",
                        },
                        {
                            "id": 2,
                            "name": "other",
                            "owner": {"login": "other-org"},
                            "clone_url": "https://github.com/other-org/other.git",
                            "html_url": "https://github.com/other-org/other",
                            "private": False,
                        },
                    ],
                    {},
                )
            }
        )
        resources = client.repositories("organizations", ("Wanted-Org",))
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].relative_path, "repos/wanted-org/project.git")
        self.assertTrue(resources[0].private)

    def test_secret_gist_is_private(self) -> None:
        client = StubGitHubClient(
            {
                "/gists": (
                    [
                        {
                            "id": "abc123",
                            "owner": {"login": "octocat"},
                            "git_pull_url": "https://gist.github.com/abc123.git",
                            "html_url": "https://gist.github.com/abc123",
                            "public": False,
                        }
                    ],
                    {},
                )
            }
        )
        resources = client.gists("octocat")
        self.assertEqual(resources[0].relative_path, "gists/abc123.git")
        self.assertTrue(resources[0].private)
