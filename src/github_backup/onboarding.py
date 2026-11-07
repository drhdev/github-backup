"""Protected GitHub manifest onboarding; no repository code or secrets in HTTP logs."""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from github_backup.app_auth import AppClient, MultiAppClient, RepositoryScope
from github_backup.config import GitHubConfig
from github_backup.setup_settings import (
    LOGIN,
    check_spaces,
    empty_settings,
    normalize_settings,
)

LOGGER = logging.getLogger(__name__)
LOCK = threading.RLock()
CREDENTIALS = Path("/cache/github-app/credentials.json")


class SetupRequired(ValueError):
    """No completed automatic setup is available yet."""


def read_credentials() -> dict[str, Any]:
    try:
        if CREDENTIALS.stat().st_mode & 0o077:
            raise ValueError("Stored GitHub credentials must have permissions 0600 or 0400")
        data = json.loads(CREDENTIALS.read_text())
        return normalize_settings(data)
    except FileNotFoundError as exc:
        raise SetupRequired("Complete GitHub setup in the protected setup page") from exc


def write_credentials(data: dict[str, Any]) -> None:
    CREDENTIALS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=CREDENTIALS.parent, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, CREDENTIALS)
        directory = os.open(CREDENTIALS.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def current_settings() -> dict[str, Any]:
    try:
        return read_credentials()
    except SetupRequired:
        return empty_settings()


def find_app(data: dict[str, Any], app_id: str) -> dict[str, Any]:
    for app in data["apps"]:
        if app["id"] == app_id:
            return dict(app)
    raise ValueError("Unknown App")


@contextmanager
def app_client(
    app: dict[str, Any], owner: str, *, require_all: bool = False
) -> Iterator[AppClient]:
    with tempfile.NamedTemporaryFile() as key:
        key.write(app["pem"].encode())
        key.flush()
        yield AppClient(
            GitHubConfig(
                app_id=app["id"],
                app_private_key=key.name,
                app_owners=(owner,),
                max_retries=0,
                timeout_seconds=15,
            ),
            require_all=require_all,
        )


def stored_github_client(data: dict[str, Any] | None = None) -> MultiAppClient:
    data = read_credentials() if data is None else data
    connections = []
    for app in data["apps"]:
        if not app["owners"]:
            continue
        key = Path(f"/tmp/github-backup-app-{app['id']}.pem")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(app["pem"])
        key.chmod(0o600)
        for owner in app["owners"]:
            scope = app.get("scopes", {}).get(owner, {"mode": "all", "repositories": []})
            policy = RepositoryScope(scope["mode"], frozenset(scope["repositories"]))
            client = AppClient(
                GitHubConfig(
                    app_id=app["id"],
                    app_private_key=str(key),
                    app_owners=(owner,),
                ),
                require_all=policy.mode in ("all", "exclude"),
            )
            connections.append((client, owner, policy))
    if not connections:
        raise SetupRequired("Connect an App installation in setup")
    return MultiAppClient(connections)


def stored_github_config(data: dict[str, Any] | None = None) -> GitHubConfig:
    return stored_github_client(data).connections[0][0]._config


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'


def home_page(data: dict[str, Any], browser: str) -> str:
    csrf = hidden("csrf", browser)
    spaces = data.get("spaces") or {}
    body = "<h1>GitHub backup setup</h1><h2>DigitalOcean Spaces</h2>"
    body += "<p>Credentials saved.</p>" if spaces else "<p>Storage is not configured yet.</p>"
    body += '<form method="post" action="/spaces">' + csrf
    for name, label, default in (
        ("bucket", "Bucket name", ""),
        ("region", "Region", "fra1"),
        ("prefix", "Backup root folder", "github-backup"),
    ):
        value = html.escape(spaces.get(name, default), quote=True)
        body += f'<p><label>{label} <input name="{name}" value="{value}" required></label></p>'
    body += (
        '<p><label>Spaces access key ID <input name="access_key_id" type="password" '
        'autocomplete="off"></label></p>'
        '<p><label>Spaces secret key <input name="secret_access_key" type="password" '
        'autocomplete="new-password"></label></p>'
        "<p>Use DigitalOcean Spaces → Access Keys. "
        "Leave both key fields blank to keep saved keys.</p>"
        '<p><label><input type="checkbox" name="confirm_destination" value="yes"> '
        "Confirm a change of bucket, region or root folder; "
        "existing backups stay in the old location."
        "</label></p><button>Test and save Spaces</button></form><h2>GitHub Apps</h2>"
    )
    for app in data["apps"]:
        app_id = app["id"]
        body += (
            f"<h3>{html.escape(app['slug'])}</h3>"
            f'<p><a href="https://github.com/apps/{app["slug"]}/installations/new">'
            "Install App on an account or organization</a></p>"
        )
        for owner in app["owners"]:
            scope = app.get("scopes", {}).get(owner, {"mode": "all"})
            body += (
                f"<p>{html.escape(owner)}: {scope['mode']} — "
                f'<a href="/repositories?app_id={app_id}&amp;owner={owner}">'
                "Choose repositories / pause backups</a></p>"
            )
        body += (
            '<form method="post" action="/owners">'
            + csrf
            + hidden("app_id", app_id)
            + "<label>Installed account or organization login "
            '<input name="owner" required maxlength="100"></label> '
            "<button>Verify and connect account</button></form>"
        )
    body += (
        "<p>New connections start paused. Choose a repository scope to enable backups.</p>"
        '<form method="post" action="/connect">'
        + csrf
        + "<label>Create the App in an organization (optional login) "
        '<input name="registration_owner" maxlength="100"></label> '
        "<button>Connect GitHub / create another App</button></form>"
    )
    return body


def repository_page(app: dict[str, Any], owner: str, browser: str) -> str:
    if owner not in app["owners"]:
        raise ValueError("Unknown installation")
    with app_client(app, owner) as client:
        repos = client.repositories("accessible", ())
    if any(repo.owner.casefold() != owner.casefold() for repo in repos):
        raise ValueError("Repository owner mismatch")
    scope = app.get("scopes", {}).get(owner, {"mode": "all", "repositories": []})
    selected = set(scope["repositories"])
    body = (
        f"<h1>Repositories for {html.escape(owner)}</h1>"
        "<p>GitHub limits which repositories this App can access. "
        "All and All except require All repositories in the GitHub installation settings. "
        "Selected works with either All repositories or Only select repositories on GitHub.</p>"
        '<form method="post" action="/scope">'
        + hidden("csrf", browser)
        + hidden("app_id", app["id"])
        + hidden("owner", owner)
    )
    for mode, label in (
        ("all", "All repositories (including future ones)"),
        ("include", "Only selected repositories"),
        ("exclude", "All except selected repositories (including future ones)"),
        ("none", "Pause this account"),
    ):
        checked = " checked" if scope["mode"] == mode else ""
        body += (
            f'<p><label><input type="radio" name="mode" value="{mode}"{checked}> '
            f"{label}</label></p>"
        )
    visible = set()
    for repo in repos:
        visible.add(repo.identifier)
        checked = " checked" if repo.identifier in selected else ""
        body += (
            f'<p><label><input type="checkbox" name="repositories" '
            f'value="{repo.identifier}"{checked}>{html.escape(repo.name)}</label></p>'
        )
    for identifier in sorted(selected - visible):
        body += (
            f'<p><label><input type="checkbox" name="repositories" '
            f'value="{identifier}" checked>Unavailable repository ID {identifier}</label></p>'
        )
    return body + (
        "<button>Save backup scope</button></form><p>Excluded or paused repositories "
        "keep existing backups without pruning. Selected repositories that become "
        "inaccessible still raise an alert. Uncheck an unavailable ID only to exclude it "
        'deliberately.</p><a href="/">Back to setup</a>'
    )


def save_scope(data: dict[str, Any], form: dict[str, list[str]]) -> None:
    app = find_app(data, form.get("app_id", [""])[0])
    owner, mode = form.get("owner", [""])[0], form.get("mode", [""])[0]
    if owner not in app["owners"] or mode not in ("all", "include", "exclude", "none"):
        raise ValueError("Invalid backup scope")
    chosen = set(form.get("repositories", []))
    previous = app.get("scopes", {}).get(owner, {"repositories": []})
    if mode != "none":
        with app_client(app, owner, require_all=mode in ("all", "exclude")) as client:
            repos = client.repositories("accessible", ())
        available = {r.identifier for r in repos if r.owner.casefold() == owner.casefold()}
        if chosen - available - set(previous["repositories"]):
            raise ValueError("Repository selection is outside this installation")
    if mode == "all":
        chosen = set()
    elif mode == "none":
        chosen = set(previous["repositories"])
    if any(not identifier.isdigit() for identifier in chosen):
        raise ValueError("Invalid repository identity")
    app.setdefault("scopes", {})[owner] = {"mode": mode, "repositories": sorted(chosen)}
    data["apps"] = [app if item["id"] == app["id"] else item for item in data["apps"]]
    write_credentials(data)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def exchange_manifest(code: str) -> dict[str, Any]:
    request = Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-backup",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    # Single-use codes must not be retried automatically or logged on errors.
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise ValueError("Oversized GitHub response")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("id"), int)
        or not LOGIN.fullmatch(str(data.get("slug", "")))
        or not isinstance(data.get("pem"), str)
    ):
        raise ValueError("Invalid GitHub response")
    result = {"id": str(data["id"]), "slug": data["slug"], "pem": data["pem"], "owners": []}
    # Validate the key before publishing the credential file.
    with tempfile.NamedTemporaryFile() as key:
        key.write(result["pem"].encode())
        key.flush()
        AppClient(GitHubConfig(app_id=result["id"], app_private_key=key.name))._jwt()
    return result


class SetupApplication:
    """Small WSGI UI, served by one Gunicorn worker behind Coolify's HTTPS proxy."""

    def __init__(self) -> None:
        self.states: dict[str, tuple[str, float]] = {}

    def __call__(self, env: dict[str, Any], start: Callable[..., Any]) -> Iterable[bytes]:
        status = "200 OK"
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            (
                "Content-Security-Policy",
                "default-src 'none'; form-action 'self' "
                "https://github.com; frame-ancestors 'none'; base-uri 'none'",
            ),
        ]
        if env.get("PATH_INFO") == "/health" and env.get("REQUEST_METHOD") == "GET":
            start(status, headers)
            return [b"OK"]
        try:
            origin = os.environ["GITHUB_SETUP_URL"].rstrip("/")
            parsed = urlsplit(origin)
            password = os.environ["GITHUB_SETUP_PASSWORD"]
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or len(password) < 24
            ):
                raise ValueError("Setup requires an HTTPS origin and a password of >=24 characters")
            expected = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
            if not secrets.compare_digest(str(env.get("HTTP_AUTHORIZATION", "")), expected):
                start(
                    "401 Unauthorized",
                    [*headers, ("WWW-Authenticate", 'Basic realm="GitHub setup"')],
                )
                return [b"Authentication required"]
            # Never trust a request Host or forwarded header to construct callback URLs.
            if env.get("HTTP_HOST", "").casefold() != parsed.netloc.casefold():
                start("400 Bad Request", headers)
                return [b"Unexpected host"]
            cookies = SimpleCookie()
            cookies.load(env.get("HTTP_COOKIE", ""))
            cookie = cookies.get("__Host-github-backup-setup")
            browser = cookie.value if cookie else ""
            if not re.fullmatch(r"[a-f0-9]{64}", browser):
                browser = secrets.token_hex(32)
                headers.append(
                    (
                        "Set-Cookie",
                        f"__Host-github-backup-setup={browser}; "
                        "Secure; HttpOnly; SameSite=Lax; Path=/",
                    )
                )
            method, path = env.get("REQUEST_METHOD"), env.get("PATH_INFO")
            if method == "GET" and path == "/callback":
                query = parse_qs(env.get("QUERY_STRING", ""))
                state = query.get("state", [""])[0]
                code = query.get("code", [""])[0]
                with LOCK:
                    pending = self.states.pop(state, None)
                    if (
                        not pending
                        or not secrets.compare_digest(pending[0], browser)
                        or time.monotonic() - pending[1] > 3600
                        or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", code)
                    ):
                        raise ValueError("Invalid or expired setup callback; start again")
                    data = current_settings()
                    app = exchange_manifest(code)
                    if any(item["id"] == app["id"] for item in data["apps"]):
                        raise ValueError("This App is already connected")
                    data["apps"].append(app)
                    write_credentials(data)
                status, body = "303 See Other", "App created. Continue with installation."
                headers.append(("Location", origin + "/"))
            elif method == "POST" and path in ("/connect", "/owners", "/scope", "/spaces"):
                size = int(env.get("CONTENT_LENGTH") or "0")
                if not 0 < size <= 262144:
                    raise ValueError("Invalid request size")
                if env.get("CONTENT_TYPE", "").split(";")[0] != "application/x-www-form-urlencoded":
                    raise ValueError("Invalid form encoding")
                form = parse_qs(env["wsgi.input"].read(size).decode())
                if not secrets.compare_digest(form.get("csrf", [""])[0], browser):
                    raise ValueError("Invalid form token; reload setup")
                if path == "/connect":
                    with LOCK:
                        now = time.monotonic()
                        self.states = {s: v for s, v in self.states.items() if now - v[1] < 3600}
                        if len(self.states) >= 32:
                            raise ValueError("Too many pending setup attempts; try later")
                        state = secrets.token_hex(32)
                        self.states[state] = (browser, now)
                    manifest = {
                        "name": "github-backup-" + secrets.token_hex(4),
                        "url": origin,
                        "redirect_url": origin + "/callback",
                        "setup_url": origin + "/",
                        "hook_attributes": {"active": False, "url": origin + "/events"},
                        "public": True,
                        "default_permissions": {"contents": "read"},
                        "default_events": [],
                        "request_oauth_on_install": False,
                    }
                    registration = form.get("registration_owner", [""])[0].strip()
                    if registration and not LOGIN.fullmatch(registration):
                        raise ValueError("Invalid organization login")
                    target = (
                        f"https://github.com/organizations/{registration}/settings/apps/new"
                        if registration
                        else "https://github.com/settings/apps/new"
                    )
                    encoded_manifest = html.escape(json.dumps(manifest), quote=True)
                    body = (
                        f'<h1>Create your backup App</h1><form method="post" '
                        f'action="{target}?state={state}">'
                        f'<input type="hidden" name="manifest" value="{encoded_manifest}">'
                        "<button>Create App on GitHub</button></form>"
                    )
                else:
                    with LOCK:
                        data = current_settings()
                        if path == "/scope":
                            save_scope(data, form)
                        elif path == "/spaces":
                            from github_backup.setup_settings import validate_spaces

                            previous = data.get("spaces") or {}
                            candidate = {
                                name: form.get(name, [""])[0].strip()
                                for name in (
                                    "bucket",
                                    "region",
                                    "prefix",
                                    "access_key_id",
                                    "secret_access_key",
                                )
                            }
                            for name in ("access_key_id", "secret_access_key"):
                                candidate[name] = candidate[name] or previous.get(name, "")
                            validate_spaces(candidate)
                            changed = previous and any(
                                candidate[n] != previous[n] for n in ("bucket", "region", "prefix")
                            )
                            if changed and form.get("confirm_destination") != ["yes"]:
                                raise ValueError("Confirm the change of storage destination")
                            check_spaces(candidate)
                            data["spaces"] = candidate
                            write_credentials(data)
                        else:
                            owner = form.get("owner", [""])[0].strip().casefold()
                            if not LOGIN.fullmatch(owner):
                                raise ValueError("Invalid account login")
                            app = find_app(data, form.get("app_id", [""])[0])
                            with app_client(app, owner) as client:
                                client.authenticated_user()
                            app["owners"] = sorted({*app["owners"], owner})
                            app.setdefault("scopes", {}).setdefault(
                                owner, {"mode": "none", "repositories": []}
                            )
                            data["apps"] = [
                                app if a["id"] == app["id"] else a for a in data["apps"]
                            ]
                            write_credentials(data)
                    status, body = "303 See Other", "Settings saved for the next backup run."
                    headers.append(("Location", origin + "/"))
            elif method == "GET" and path == "/":
                body = home_page(current_settings(), browser)
            elif method == "GET" and path == "/repositories":
                query = parse_qs(env.get("QUERY_STRING", ""))
                app = find_app(read_credentials(), query.get("app_id", [""])[0])
                body = repository_page(app, query.get("owner", [""])[0], browser)
            else:
                status, body = "404 Not Found", "Not found"
        except Exception:
            # Exceptions may contain the conversion code, API response, or other secrets.
            LOGGER.warning("Setup request failed; no stored credentials were intentionally removed")
            status, body = (
                "400 Bad Request",
                (
                    "Setup could not complete. Check the setup URL/password, "
                    "retry with a fresh flow, "
                    "or verify the App installation and repository permissions. "
                    "All/All except require "
                    "All repositories on GitHub. For Spaces check the region, bucket, "
                    "key permissions "
                    "and destination-change confirmation. "
                    '<a href="/">Return to setup</a>'
                ),
            )
        start(status, headers)
        return [
            (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                "<title>GitHub backup setup</title><body>" + body + "</body></html>"
            ).encode()
        ]


application = SetupApplication()
