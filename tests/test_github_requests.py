from __future__ import annotations

import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from github_backup.config import GitHubConfig
from github_backup.errors import GitHubAPIError
from github_backup.github import GitHubClient, _retry_delay


class FakeResponse:
    def __init__(self, payload: object, headers: Message | None = None) -> None:
        self.payload = json.dumps(payload).encode()
        self.headers = headers or Message()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class GitHubRequestTests(unittest.TestCase):
    def test_authenticated_user_request(self) -> None:
        client = GitHubClient(GitHubConfig(), "token")
        with patch("github_backup.github.urlopen", return_value=FakeResponse({"login": "owner"})):
            self.assertEqual(client.authenticated_user(), "owner")

    def test_transient_network_failure_is_retried(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient(
            GitHubConfig(max_retries=1), "token", sleeper=lambda delay: sleeps.append(delay)
        )
        with patch(
            "github_backup.github.urlopen",
            side_effect=[URLError("temporary"), FakeResponse({"login": "owner"})],
        ):
            self.assertEqual(client.authenticated_user(), "owner")
        self.assertEqual(len(sleeps), 1)

    def test_rate_limit_retry_after_is_honored(self) -> None:
        headers = Message()
        headers["Retry-After"] = "3"
        error = HTTPError(
            "https://api.github.com/user",
            429,
            "rate limited",
            headers,
            io.BytesIO(b'{"message":"slow down"}'),
        )
        sleeps: list[float] = []
        client = GitHubClient(
            GitHubConfig(max_retries=1), "token", sleeper=lambda delay: sleeps.append(delay)
        )
        with patch(
            "github_backup.github.urlopen",
            side_effect=[error, FakeResponse({"login": "owner"})],
        ):
            self.assertEqual(client.authenticated_user(), "owner")
        self.assertEqual(sleeps, [3.0])

    def test_non_retryable_error_is_sanitized(self) -> None:
        error = HTTPError(
            "https://api.github.com/user",
            401,
            "unauthorized",
            Message(),
            io.BytesIO(b'{"message":"Bad credentials"}'),
        )
        client = GitHubClient(GitHubConfig(max_retries=0), "token")
        with (
            patch("github_backup.github.urlopen", side_effect=error),
            self.assertRaisesRegex(GitHubAPIError, "Bad credentials"),
        ):
            client.authenticated_user()

    def test_retry_delay_has_bounded_fallback(self) -> None:
        self.assertEqual(_retry_delay({}, 0), 60.0)
        self.assertEqual(_retry_delay({}, 10), 900.0)

    def test_cross_origin_pagination_is_rejected_before_sending_token(self) -> None:
        client = GitHubClient(GitHubConfig(), "token")
        with (
            patch("github_backup.github.urlopen") as request,
            self.assertRaisesRegex(GitHubAPIError, "different API origin"),
        ):
            client._request_json("https://attacker.example/repos")
        request.assert_not_called()
