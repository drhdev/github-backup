"""Safe Git mirror and bundle operations."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from github_backup.errors import GitCommandError


class GitRunner:
    def __init__(self, token: str, *, timeout_seconds: int) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    def ensure_available(self) -> str:
        result = self._run(["git", "--version"], authenticated_url=None)
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
                authenticated_url=_authenticated_url(clone_url),
            )
        else:
            temporary = mirror_path.with_name(f".{mirror_path.name}.tmp-{uuid.uuid4().hex}")
            try:
                self._run(
                    ["git", "clone", "--mirror", "--", clone_url, str(temporary)],
                    authenticated_url=_authenticated_url(clone_url),
                )
                temporary.replace(mirror_path)
            except BaseException:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
        after = self.ref_digest(mirror_path)
        return before != after, after

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

    def has_refs(self, mirror_path: Path) -> bool:
        result = self._run(
            ["git", "-C", str(mirror_path), "for-each-ref", "--count=1", "--format=%(refname)"],
            authenticated_url=None,
        )
        return bool(result.stdout.strip())

    def _run(
        self, arguments: list[str], *, authenticated_url: str | None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "LC_ALL": "C",
            }
        )
        if authenticated_url is not None:
            parsed = urlsplit(authenticated_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise GitCommandError(f"unsupported authenticated clone URL: {authenticated_url}")
            credential = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            hostname = parsed.hostname
            if ":" in hostname:
                hostname = f"[{hostname}]"
            authority = f"{hostname}:{parsed.port}" if parsed.port else hostname
            section_url = f"{parsed.scheme}://{authority}/"
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.{section_url}.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
                }
            )
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
            raise GitCommandError(f"Git command timed out after {self._timeout_seconds}s") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown Git error").strip()
            detail = detail.replace(self._token, "[REDACTED]")
            raise GitCommandError(f"Git command failed: {detail}") from exc


def _authenticated_url(url: str) -> str | None:
    return url if urlsplit(url).scheme in ("http", "https") else None
