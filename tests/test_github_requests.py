from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from github_backup import __version__
from github_backup.config import GitHubConfig
from github_backup.errors import GitHubAPIError
from github_backup.github import GitHubClient, _retry_delay


class FakeResponse:
    def __init__(self, payload: object, headers: Message | None = None) -> None:
        self.payload = io.BytesIO(json.dumps(payload).encode())
        self.headers = headers or Message()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload.read(size)


class GitHubRequestTests(unittest.TestCase):
    def test_authenticated_user_request(self) -> None:
        opener = Mock(return_value=FakeResponse({"login": "owner"}))
        client = GitHubClient(
            GitHubConfig(), "token", opener=opener
        )
        self.assertEqual(client.authenticated_user(), "owner")
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), f"github-backup/{__version__}")

    def test_transient_network_failure_is_retried(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient(
            GitHubConfig(max_retries=1),
            "token",
            sleeper=lambda delay: sleeps.append(delay),
            opener=Mock(
                side_effect=[URLError("temporary"), FakeResponse({"login": "owner"})],
            ),
        )
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
            GitHubConfig(max_retries=1),
            "token",
            sleeper=lambda delay: sleeps.append(delay),
            opener=Mock(side_effect=[error, FakeResponse({"login": "owner"})]),
        )
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
        client = GitHubClient(GitHubConfig(max_retries=0), "token", opener=Mock(side_effect=error))
        with self.assertRaisesRegex(GitHubAPIError, "Bad credentials"):
            client.authenticated_user()

    def test_retry_delay_has_bounded_fallback(self) -> None:
        self.assertEqual(_retry_delay({}, 0), 60.0)
        self.assertEqual(_retry_delay({}, 10), 900.0)

    def test_cross_origin_pagination_is_rejected_before_sending_token(self) -> None:
        request = Mock()
        client = GitHubClient(GitHubConfig(), "token", opener=request)
        with self.assertRaisesRegex(GitHubAPIError, "different API origin"):
            client._request_json("https://attacker.example/repos")
        request.assert_not_called()

    def test_cross_origin_download_is_rejected_before_sending_token(self) -> None:
        client = GitHubClient(GitHubConfig(), "token")
        with self.assertRaisesRegex(GitHubAPIError, "different origin"):
            client.download("https://attacker.example/archive.zip", Path("unused"))

    def test_existing_query_is_preserved_when_paginating(self) -> None:
        client = GitHubClient(GitHubConfig(), "token")
        with patch.object(
            client,
            "_request_json",
            return_value=([{"number": 1}], {}),
        ) as request:
            self.assertEqual(
                list(client.paginate("/repos/o/r/pulls?state=all")),
                [{"number": 1}],
            )
        request.assert_called_once_with("/repos/o/r/pulls?state=all&per_page=100")

    def test_download_redirect_does_not_forward_authorization(self) -> None:
        headers = Message()
        headers["Location"] = "https://objects.example/archive.zip"
        redirect = HTTPError(
            "https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            302,
            "redirect",
            headers,
            io.BytesIO(),
        )
        first = Mock()
        first.open.side_effect = redirect
        second = Mock()
        second.open.return_value = FakeResponse("archive")
        client = GitHubClient(GitHubConfig(), "secret-token")
        with tempfile.TemporaryDirectory() as directory, patch(
            "github_backup.github.build_opener", side_effect=[first, second]
        ):
            destination = Path(directory) / "archive.zip"
            client.download("/repos/o/r/actions/artifacts/1/zip", destination)
            self.assertTrue(destination.read_bytes())
            redirected_request = second.open.call_args.args[0]
            self.assertIsNone(redirected_request.get_header("Authorization"))
