"""Small, dependency-free client for the GitHub REST API."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from github_backup.config import GitHubConfig, ScopeKind
from github_backup.errors import GitHubAPIError
from github_backup.models import Resource

LOGGER = logging.getLogger(__name__)


class GitHubClient:
    def __init__(
        self,
        config: GitHubConfig,
        token: str,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._token = token
        self._sleep = sleeper

    def authenticated_user(self) -> str:
        payload, _ = self._request_json("/user")
        if not isinstance(payload, dict) or not isinstance(payload.get("login"), str):
            raise GitHubAPIError("GitHub returned an invalid authenticated-user response")
        return cast(str, payload["login"])

    def repositories(self, scope: ScopeKind, organizations: tuple[str, ...]) -> list[Resource]:
        affiliation = {
            "accessible": "owner,collaborator,organization_member",
            "owned": "owner",
            "organizations": "organization_member",
        }[scope]
        params = {
            "affiliation": affiliation,
            "visibility": "all",
            "sort": "full_name",
            "direction": "asc",
        }
        allowed_orgs = {organization.casefold() for organization in organizations}
        resources: list[Resource] = []
        for item in self._paginate("/user/repos", params):
            owner = _nested_string(item, "owner", "login")
            if scope == "organizations" and owner.casefold() not in allowed_orgs:
                continue
            resources.append(
                Resource(
                    kind="repository",
                    identifier=_required_string(item, "id"),
                    name=_required_string(item, "name"),
                    owner=owner,
                    clone_url=_required_string(item, "clone_url"),
                    html_url=_required_string(item, "html_url"),
                    private=bool(item.get("private", False)),
                    updated_at=_optional_string(item.get("updated_at")),
                    archived=bool(item.get("archived", False)),
                    default_branch=_optional_string(item.get("default_branch")),
                )
            )
        return resources

    def gists(self, authenticated_user: str) -> list[Resource]:
        resources: list[Resource] = []
        for item in self._paginate("/gists", {}):
            owner_value = item.get("owner")
            owner = authenticated_user
            if isinstance(owner_value, dict) and isinstance(owner_value.get("login"), str):
                owner = cast(str, owner_value["login"])
            identifier = _required_string(item, "id")
            resources.append(
                Resource(
                    kind="gist",
                    identifier=identifier,
                    name=identifier,
                    owner=owner,
                    clone_url=_required_string(item, "git_pull_url"),
                    html_url=_required_string(item, "html_url"),
                    private=not bool(item.get("public", False)),
                    updated_at=_optional_string(item.get("updated_at")),
                )
            )
        return resources

    def _paginate(self, path: str, parameters: Mapping[str, str]) -> Iterator[dict[str, Any]]:
        query = dict(parameters)
        query["per_page"] = "100"
        next_url: str | None = f"{path}?{urlencode(query)}"
        while next_url:
            payload, headers = self._request_json(next_url)
            if not isinstance(payload, list):
                raise GitHubAPIError(f"GitHub returned a non-list response for {path}")
            for item in payload:
                if not isinstance(item, dict):
                    raise GitHubAPIError(f"GitHub returned an invalid item for {path}")
                yield item
            next_url = _next_link(headers.get("Link", ""))

    def _request_json(self, path_or_url: str) -> tuple[Any, Mapping[str, str]]:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else urljoin(f"{self._config.api_url}/", path_or_url.lstrip("/"))
        )
        if _origin(url) != _origin(self._config.api_url):
            raise GitHubAPIError("GitHub pagination attempted to use a different API origin")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "github-backup/0.1",
            "X-GitHub-Api-Version": self._config.api_version,
        }
        for attempt in range(self._config.max_retries + 1):
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=self._config.timeout_seconds) as response:
                    raw = response.read()
                    return json.loads(raw), dict(response.headers.items())
            except HTTPError as exc:
                message = _http_error_message(exc)
                if not _retryable_http_error(exc, message) or attempt >= self._config.max_retries:
                    raise GitHubAPIError(
                        f"GitHub API request failed ({exc.code}) for {url}: {message}"
                    ) from exc
                delay = _retry_delay(dict(exc.headers.items()), attempt)
                LOGGER.warning(
                    "GitHub API request was rate-limited or unavailable; retrying in %.1fs",
                    delay,
                )
                self._sleep(delay)
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= self._config.max_retries:
                    raise GitHubAPIError(f"GitHub API request failed for {url}: {exc}") from exc
                delay = min(2**attempt + random.random(), 30.0)
                LOGGER.warning("GitHub API request failed; retrying in %.1fs", delay)
                self._sleep(delay)
        raise AssertionError("retry loop exhausted unexpectedly")


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if isinstance(value, (str, int)):
        return str(value)
    raise GitHubAPIError(f"GitHub response is missing required field {key!r}")


def _nested_string(item: Mapping[str, Any], outer: str, inner: str) -> str:
    value = item.get(outer)
    if isinstance(value, dict) and isinstance(value.get(inner), str):
        return cast(str, value[inner])
    raise GitHubAPIError(f"GitHub response is missing required field {outer}.{inner}")


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _next_link(header: str) -> str | None:
    for entry in header.split(","):
        parts = [part.strip() for part in entry.split(";")]
        if len(parts) >= 2 and 'rel="next"' in parts[1:]:
            return parts[0].strip("<>")
    return None


def _retryable_http_error(error: HTTPError, message: str) -> bool:
    if error.code in (429, 500, 502, 503, 504):
        return True
    return error.code == 403 and (
        error.headers.get("X-RateLimit-Remaining") == "0"
        or "secondary rate limit" in message.casefold()
    )


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    if retry_after := headers.get("Retry-After"):
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            return max(float(headers["X-RateLimit-Reset"]) - time.time() + 1.0, 1.0)
        except (KeyError, ValueError):
            pass
    return max(60.0, min(60.0 * 2.0**attempt, 900.0))


def _http_error_message(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return cast(str, payload["message"])
    except (OSError, json.JSONDecodeError):
        pass
    return str(error.reason or "unknown error")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port
