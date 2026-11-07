"""GitHub App authentication for explicitly configured installation owners."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from github_backup.config import GitHubConfig, ScopeKind
from github_backup.errors import ConfigurationError, GitHubAPIError
from github_backup.github import GitHubClient
from github_backup.models import Resource


class InstallationClient(GitHubClient):
    def authenticated_user(self) -> str:
        return "installation"

    def repositories(self, scope: ScopeKind, organizations: tuple[str, ...]) -> list[Resource]:
        # Reuse the normal resource parser with installation-specific pagination.
        return super().repositories(scope, organizations)

    def _paginate(
        self, path: str, parameters: Mapping[str, str], *, list_key: str | None = None
    ) -> Any:
        if path == "/user/repos":
            path, parameters, list_key = "/installation/repositories", {}, "repositories"
        return super()._paginate(path, parameters, list_key=list_key)


class AppClient(GitHubClient):
    def __init__(self, config: GitHubConfig) -> None:
        super().__init__(config, self._jwt)
        self._app = GitHubClient(config, self._jwt)
        self._lock = threading.RLock()
        self._installations: dict[str, int] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._clients: dict[str, InstallationClient] = {}
        for owner in config.app_owners:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
                raise ConfigurationError(f"Invalid GitHub installation owner: {owner}")

    def _jwt(self) -> str:
        def encode(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

        now = int(time.time())
        header = encode(b'{"alg":"RS256","typ":"JWT"}')
        payload = encode(
            json.dumps({"iat": now - 60, "exp": now + 540, "iss": self._config.app_id}).encode()
        )
        signing_input = f"{header}.{payload}"
        key = Path(self._config.app_private_key).expanduser()
        try:
            if key.stat().st_mode & 0o077:
                raise ConfigurationError("App private key must have permissions 0600 or 0400")
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", str(key)],
                input=signing_input.encode(),
                capture_output=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError("Cannot sign App JWT; check private key and openssl") from exc
        return f"{signing_input}.{encode(result.stdout)}"

    def _client(self, owner: str) -> InstallationClient:
        owner = owner.casefold()
        with self._lock:
            if owner not in self._clients:
                try:
                    installation = self._app.get_json(f"/users/{quote(owner)}/installation")
                except GitHubAPIError as exc:
                    if exc.status_code != 404:
                        raise
                    installation = self._app.get_json(f"/orgs/{quote(owner)}/installation")
                if not isinstance(installation, dict) or "id" not in installation:
                    raise GitHubAPIError(f"Invalid installation response for {owner}")
                account = installation.get("account", {})
                if str(account.get("login", "")).casefold() != owner:
                    raise GitHubAPIError("Installation account does not match configured owner")
                if installation.get("repository_selection") != "all":
                    raise GitHubAPIError(f"Install the App on All repositories for {owner}")
                if installation.get("suspended_at"):
                    raise GitHubAPIError(f"App installation suspended for {owner}")
                self._installations[owner] = int(installation["id"])
                self._clients[owner] = InstallationClient(
                    self._config, lambda: self._installation_token(owner)
                )
            return self._clients[owner]

    def _installation_token(self, owner: str) -> str:
        with self._lock:
            cached = self._tokens.get(owner)
            if cached and cached[1] > time.time() + 300:
                return cached[0]
            url = self._absolute_api_url(
                f"/app/installations/{self._installations[owner]}/access_tokens"
            )
            try:
                minter = GitHubClient(
                    self._config, self._token, opener=self._open, sleeper=self._sleep
                )
                payload, _ = minter._request_json(url, method="POST", data=b"{}")
                token = payload["token"]
                expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
                if (
                    not isinstance(token, str)
                    or not token
                    or expires.timestamp() <= time.time() + 300
                ):
                    raise ValueError("invalid token")
            except Exception as exc:
                raise GitHubAPIError(f"Cannot obtain installation token for {owner}") from exc
            self._tokens[owner] = (token, expires.timestamp())
            return token

    def authenticated_user(self) -> str:
        for owner in self._config.app_owners:
            self._client(owner)
        return ",".join(self._config.app_owners)

    def repositories(self, scope: ScopeKind, organizations: tuple[str, ...]) -> list[Resource]:
        repos: dict[str, Resource] = {}
        for owner in self._config.app_owners:
            for repo in self._client(owner).repositories(scope, organizations):
                repos[repo.identifier] = repo
        return sorted(repos.values(), key=lambda repo: repo.relative_path.casefold())

    def _owner_client(self, path: str) -> InstallationClient:
        parsed = urlsplit(self._absolute_api_url(path))
        api = urlsplit(self._config.api_url)
        if (parsed.scheme, parsed.netloc) != (api.scheme, api.netloc):
            raise GitHubAPIError("App request attempted to use a different API origin")
        relative = parsed.path.removeprefix(api.path.rstrip("/"))
        parts = relative.strip("/").split("/")
        if len(parts) < 2 or parts[0] not in ("repos", "orgs", "users"):
            raise GitHubAPIError("Cannot route App request to an installation")
        owner = parts[1].casefold()
        if owner not in {value.casefold() for value in self._config.app_owners}:
            raise GitHubAPIError("Request owner is outside configured installations")
        return self._client(owner)

    def _request_json(
        self, path_or_url: str, *, method: str = "GET", data: bytes | None = None
    ) -> tuple[Any, Mapping[str, str]]:
        return self._owner_client(path_or_url)._request_json(path_or_url, method=method, data=data)

    def download(self, path_or_url: str, destination: Path) -> int:
        return self._owner_client(path_or_url).download(path_or_url, destination)

    def git_token(self, url: str) -> str:
        parsed = urlsplit(url)
        api_host = urlsplit(self._config.api_url).hostname
        git_host = "github.com" if api_host == "api.github.com" else api_host
        if parsed.scheme != "https" or parsed.hostname != git_host:
            raise GitHubAPIError("Git remote is outside the configured GitHub host")
        owner = parsed.path.strip("/").split("/")[0].casefold()
        if owner not in {value.casefold() for value in self._config.app_owners}:
            raise GitHubAPIError("Git owner is outside configured installations")
        self._client(owner)
        return self._installation_token(owner)
