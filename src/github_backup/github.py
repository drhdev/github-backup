"""Small, dependency-free client for the GitHub REST API."""

from __future__ import annotations

import json
import logging
import random
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from github_backup import __version__
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
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._token = token
        self._sleep = sleeper
        self._open = opener or build_opener(SameOriginRedirectHandler()).open

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
                    has_wiki=bool(item.get("has_wiki", False)),
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

    def paginate(
        self,
        path: str,
        parameters: Mapping[str, str] | None = None,
        *,
        list_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield all objects from a list or wrapped-list REST endpoint."""

        return self._paginate(path, parameters or {}, list_key=list_key)

    def get_json(self, path: str) -> Any:
        payload, _ = self._request_json(path)
        return payload

    def download(self, path_or_url: str, destination: Path) -> int:
        """Stream a download and never forward the token to an asset host."""

        url = self._absolute_api_url(path_or_url)
        if _origin(url) != _origin(self._config.api_url):
            raise GitHubAPIError("refusing to send GitHub credentials to a different origin")
        request = Request(url, headers=self._headers("application/octet-stream"))
        response, location = self._open_api_download(request, url)
        if location is not None:
            redirected = urljoin(url, location)
            _validate_download_url(
                redirected, allow_insecure=self._config.allow_insecure_http
            )
            response = self._open_redirected_download(redirected, url)
        if response is None:
            raise AssertionError("download response missing")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with response, destination.open("wb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        return destination.stat().st_size

    def _open_api_download(self, request: Request, url: str) -> tuple[Any | None, str | None]:
        opener = build_opener(_NoRedirectHandler()).open
        for attempt in range(self._config.max_retries + 1):
            try:
                return opener(request, timeout=self._config.timeout_seconds), None
            except HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location")
                    if not location:
                        raise GitHubAPIError(
                            f"GitHub download redirect had no Location header for {url}",
                            status_code=exc.code,
                        ) from exc
                    return None, location
                message = _http_error_message(exc)
                if _retryable_http_error(exc, message) and attempt < self._config.max_retries:
                    self._sleep(_retry_delay(dict(exc.headers.items()), attempt))
                    continue
                raise GitHubAPIError(
                    f"GitHub download failed ({exc.code}) for {url}: {message}",
                    status_code=exc.code,
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt >= self._config.max_retries:
                    raise GitHubAPIError(f"GitHub download failed for {url}: {exc}") from exc
                self._sleep(min(2**attempt + random.random(), 30.0))
        raise AssertionError("download retry loop exhausted unexpectedly")

    def _open_redirected_download(self, redirected: str, source_url: str) -> Any:
        opener = build_opener(
            _HTTPSOnlyRedirectHandler(self._config.allow_insecure_http)
        ).open
        request = Request(
            redirected,
            headers={"User-Agent": f"github-backup/{__version__}"},
        )
        for attempt in range(self._config.max_retries + 1):
            try:
                return opener(request, timeout=self._config.timeout_seconds)
            except (HTTPError, URLError, TimeoutError) as exc:
                retryable = not isinstance(exc, HTTPError) or exc.code in (
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                if retryable and attempt < self._config.max_retries:
                    self._sleep(min(2**attempt + random.random(), 30.0))
                    continue
                status = exc.code if isinstance(exc, HTTPError) else None
                raise GitHubAPIError(
                    f"redirected GitHub download failed for {source_url}: {exc}",
                    status_code=status,
                ) from exc
        raise AssertionError("redirected download retry loop exhausted unexpectedly")

    def _paginate(
        self,
        path: str,
        parameters: Mapping[str, str],
        *,
        list_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        query = dict(parameters)
        query["per_page"] = "100"
        separator = "&" if "?" in path else "?"
        next_url: str | None = f"{path}{separator}{urlencode(query)}"
        while next_url:
            payload, headers = self._request_json(next_url)
            page = payload.get(list_key) if list_key and isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise GitHubAPIError(f"GitHub returned a non-list response for {path}")
            for item in page:
                if not isinstance(item, dict):
                    raise GitHubAPIError(f"GitHub returned an invalid item for {path}")
                yield item
            next_url = _next_link(headers.get("Link", ""))

    def _request_json(self, path_or_url: str) -> tuple[Any, Mapping[str, str]]:
        url = self._absolute_api_url(path_or_url)
        if _origin(url) != _origin(self._config.api_url):
            raise GitHubAPIError("GitHub pagination attempted to use a different API origin")
        headers = self._headers("application/vnd.github+json")
        for attempt in range(self._config.max_retries + 1):
            request = Request(url, headers=headers)
            try:
                with self._open(request, timeout=self._config.timeout_seconds) as response:
                    raw = response.read()
                    return json.loads(raw), dict(response.headers.items())
            except HTTPError as exc:
                message = _http_error_message(exc)
                if not _retryable_http_error(exc, message) or attempt >= self._config.max_retries:
                    raise GitHubAPIError(
                        f"GitHub API request failed ({exc.code}) for {url}: {message}",
                        status_code=exc.code,
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

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "User-Agent": f"github-backup/{__version__}",
            "X-GitHub-Api-Version": self._config.api_version,
        }

    def _absolute_api_url(self, path_or_url: str) -> str:
        return (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else urljoin(f"{self._config.api_url}/", path_or_url.lstrip("/"))
        )


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


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Permit API redirects only when credentials stay on the same origin."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        if _origin(request.full_url) != _origin(new_url):
            raise HTTPError(
                request.full_url,
                code,
                "refusing cross-origin redirect for authenticated GitHub request",
                headers,
                file_pointer,
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allow_insecure: bool) -> None:
        super().__init__()
        self._allow_insecure = allow_insecure

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        _validate_download_url(new_url, allow_insecure=self._allow_insecure)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def _validate_download_url(url: str, *, allow_insecure: bool) -> None:
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise GitHubAPIError("GitHub download redirected to an invalid URL")
    if parsed.scheme != "https" and not (allow_insecure and parsed.scheme == "http"):
        raise GitHubAPIError("GitHub download redirected to a non-HTTPS URL")
