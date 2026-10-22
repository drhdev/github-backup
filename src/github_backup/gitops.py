"""Safe Git mirror and bundle operations."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from github_backup.errors import GitCommandError


class GitRunner:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: int,
        max_retries: int = 3,
        verify_mirrors: bool = True,
        allow_insecure_http: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._verify_mirrors = verify_mirrors
        self._allow_insecure_http = allow_insecure_http
        self._sleep = sleeper

    def ensure_available(self) -> str:
        result = self._run(["git", "--version"], authenticated_url=None)
        return result.stdout.strip()

    def ensure_lfs_available(self) -> str:
        result = self._run(["git", "lfs", "version"], authenticated_url=None)
        return result.stdout.strip()

    def update_mirror(self, clone_url: str, mirror_path: Path) -> tuple[bool, str]:
        before = self.ref_digest(mirror_path) if mirror_path.exists() else None
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        if mirror_path.exists():
            bare = self._run(
                ["git", "-C", str(mirror_path), "rev-parse", "--is-bare-repository"],
                authenticated_url=None,
            ).stdout.strip()
            if bare != "true":
                raise GitCommandError(f"existing backup is not a bare repository: {mirror_path}")
            self._run(
                ["git", "-C", str(mirror_path), "remote", "set-url", "origin", clone_url],
                authenticated_url=None,
            )
            self._run(
                ["git", "-C", str(mirror_path), "remote", "update", "--prune"],
                authenticated_url=self._authenticated_url(clone_url),
                retry=True,
            )
        else:
            self._clone_with_retry(clone_url, mirror_path)
        if self._verify_mirrors:
            self.verify_mirror(mirror_path)
        after = self.ref_digest(mirror_path)
        return before != after, after

    def verify_mirror(self, mirror_path: Path) -> None:
        self._run(
            ["git", "-C", str(mirror_path), "fsck", "--connectivity-only", "--no-dangling"],
            authenticated_url=None,
        )

    def fetch_lfs(self, mirror_path: Path, clone_url: str) -> Path:
        """Fetch every reachable Git LFS object into the bare mirror."""

        self.ensure_lfs_available()
        self._run(
            ["git", "-C", str(mirror_path), "lfs", "fetch", "--all", "origin"],
            authenticated_url=self._authenticated_url(clone_url),
            retry=True,
        )
        objects = mirror_path / "lfs" / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        return objects

    def ref_digest(self, mirror_path: Path) -> str:
        if not mirror_path.exists():
            return ""
        output = self._run(
            [
                "git",
                "-C",
                str(mirror_path),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            ],
            authenticated_url=None,
        ).stdout
        return hashlib.sha256(output.encode("utf-8")).hexdigest()

    def create_bundle(self, mirror_path: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            self._run(
                ["git", "-C", str(mirror_path), "bundle", "create", str(temporary), "--all"],
                authenticated_url=None,
            )
            self._run(
                ["git", "-C", str(mirror_path), "bundle", "verify", str(temporary)],
                authenticated_url=None,
            )
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def verify_bundle(self, mirror_path: Path, bundle_path: Path) -> None:
        self._run(
            ["git", "-C", str(mirror_path), "bundle", "verify", str(bundle_path)],
            authenticated_url=None,
        )

    def has_refs(self, mirror_path: Path) -> bool:
        result = self._run(
            ["git", "-C", str(mirror_path), "for-each-ref", "--count=1", "--format=%(refname)"],
            authenticated_url=None,
        )
        return bool(result.stdout.strip())

    def _run(
        self,
        arguments: list[str],
        *,
        authenticated_url: str | None,
        retry: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "LC_ALL": "C",
            }
        )
        for trace_variable in (
            "GIT_CURL_VERBOSE",
            "GIT_TRACE",
            "GIT_TRACE_CURL",
            "GIT_TRACE_CURL_NO_DATA",
            "GIT_TRACE_REDACT",
        ):
            environment.pop(trace_variable, None)
        encoded_credential: str | None = None
        if authenticated_url is not None:
            parsed = urlsplit(authenticated_url)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and self._allow_insecure_http
            ):
                raise GitCommandError("authenticated Git remotes must use HTTPS")
            if not parsed.hostname:
                raise GitCommandError(f"unsupported authenticated clone URL: {authenticated_url}")
            encoded_credential = base64.b64encode(
                f"x-access-token:{self._token}".encode()
            ).decode()
            hostname = parsed.hostname
            if ":" in hostname:
                hostname = f"[{hostname}]"
            authority = f"{hostname}:{parsed.port}" if parsed.port else hostname
            section_url = f"{parsed.scheme}://{authority}/"
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": f"http.{section_url}.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded_credential}",
                    "GIT_CONFIG_KEY_1": "http.followRedirects",
                    "GIT_CONFIG_VALUE_1": "false",
                }
            )
        for attempt in range(self._max_retries + 1):
            try:
                return subprocess.run(
                    arguments,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except FileNotFoundError as exc:
                raise GitCommandError("Git executable was not found on PATH") from exc
            except subprocess.TimeoutExpired as exc:
                if retry and attempt < self._max_retries:
                    self._sleep(min(2.0**attempt, 30.0))
                    continue
                raise GitCommandError(
                    f"Git command timed out after {self._timeout_seconds}s"
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "unknown Git error").strip()
                detail = detail.replace(self._token, "[REDACTED]")
                if encoded_credential is not None:
                    detail = detail.replace(encoded_credential, "[REDACTED]")
                if retry and attempt < self._max_retries and _retryable_git_error(detail):
                    self._sleep(min(2.0**attempt, 30.0))
                    continue
                raise GitCommandError(f"Git command failed: {detail}") from exc
        raise AssertionError("Git retry loop exhausted unexpectedly")

    def _clone_with_retry(self, clone_url: str, mirror_path: Path) -> None:
        last_error: GitCommandError | None = None
        for attempt in range(self._max_retries + 1):
            temporary = mirror_path.with_name(f".{mirror_path.name}.tmp-{uuid.uuid4().hex}")
            try:
                self._run(
                    ["git", "clone", "--mirror", "--", clone_url, str(temporary)],
                    authenticated_url=self._authenticated_url(clone_url),
                )
                temporary.replace(mirror_path)
                return
            except GitCommandError as exc:
                last_error = exc
                if attempt >= self._max_retries or not _retryable_git_error(str(exc)):
                    raise
                self._sleep(min(2.0**attempt, 30.0))
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        raise last_error or GitCommandError("Git clone failed")

    def _authenticated_url(self, url: str) -> str | None:
        scheme = urlsplit(url).scheme
        if scheme == "https" or (scheme == "http" and self._allow_insecure_http):
            return url
        if scheme == "http":
            raise GitCommandError("authenticated Git remotes must use HTTPS")
        return None


def _retryable_git_error(detail: str) -> bool:
    normalized = detail.casefold()
    permanent_markers = (
        "authentication failed",
        "repository not found",
        "access denied",
        "could not read username",
        "requested url returned error: 401",
        "requested url returned error: 403",
        "must use https",
    )
    return not any(marker in normalized for marker in permanent_markers)


def missing_remote(error: Exception) -> bool:
    normalized = str(error).casefold()
    return "repository not found" in normalized or "requested url returned error: 404" in normalized
