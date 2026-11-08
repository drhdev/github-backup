"""Protected GitHub manifest onboarding; no repository code or secrets in HTTP logs."""

from __future__ import annotations

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
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from github_backup.app_auth import AppClient, MultiAppClient, RepositoryScope
from github_backup.config import GitHubConfig
from github_backup.config_ui import (
    account_page,
    credentials_page,
    document,
    history_page,
    home_page,
)
from github_backup.config_ui import (
    repository_page as render_repository_page,
)
from github_backup.history import recent_backups
from github_backup.setup_auth import (
    SESSION_COOKIE,
    AuthLockedError,
    AuthRateLimited,
    AuthStore,
)
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
        raise SetupRequired("Complete GitHub configuration on the config page") from exc


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
        raise SetupRequired("Connect an App installation on the config page")
    return MultiAppClient(connections)


def stored_github_config(data: dict[str, Any] | None = None) -> GitHubConfig:
    return stored_github_client(data).connections[0][0]._config


def repository_page(app: dict[str, Any], owner: str, browser: str) -> str:
    if owner not in app["owners"]:
        raise ValueError("Unknown installation")
    with app_client(app, owner) as client:
        repos = client.repositories("accessible", ())
    if any(repo.owner.casefold() != owner.casefold() for repo in repos):
        raise ValueError("Repository owner mismatch")
    if any(not re.fullmatch(r"[0-9]{1,30}", repo.identifier) for repo in repos):
        raise ValueError("Invalid repository identity")
    return render_repository_page(app, owner, browser, repos)


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
        self.auth = AuthStore()

    @staticmethod
    def browser_cookie(browser: str) -> tuple[str, str]:
        return (
            "Set-Cookie",
            f"__Host-github-backup-csrf={browser}; Secure; HttpOnly; SameSite=Lax; Path=/",
        )

    def account_response(
        self,
        method: str,
        path: str,
        browser: str,
        session: str,
        form: dict[str, list[str]],
        headers: list[tuple[str, str]],
    ) -> tuple[str, str] | None:
        registered = self.auth.registered()
        if path == "/register" and registered:
            return "404 Not Found", "Registration is permanently closed."
        if not registered:
            if method == "POST" and path == "/register":
                if self.auth.register(
                    form.get("username", [""])[0].strip(),
                    form.get("password", [""])[0],
                    form.get("confirmation", [""])[0],
                    form.get("code", [""])[0].strip(),
                ):
                    headers.extend(
                        [
                            ("Location", "/login?notice=registered"),
                            self.browser_cookie(secrets.token_hex(32)),
                        ]
                    )
                    return "303 See Other", "Account created. Log in with your saved credentials."
                return "400 Bad Request", account_page(
                    browser,
                    register=True,
                    error="Check the registration code, username, password and confirmation.",
                )
            if method == "GET" and path in ("/", "/register", "/login"):
                return "200 OK", account_page(browser, register=True)
            headers.append(("Location", "/register"))
            return "303 See Other", "Register the deployment owner first."
        authenticated = self.auth.authenticated(session, browser)
        if method == "POST" and path == "/login" and not authenticated:
            if self.auth.login(
                form.get("username", [""])[0].strip(), form.get("password", [""])[0]
            ):
                browser = secrets.token_hex(32)
                token = self.auth.create_session(browser)
                headers.extend(
                    [
                        self.browser_cookie(browser),
                        (
                            "Set-Cookie",
                            f"{SESSION_COOKIE}={token}; Secure; HttpOnly; SameSite=Lax; Path=/",
                        ),
                        ("Location", "/"),
                    ]
                )
                return "303 See Other", "Logged in."
            return "401 Unauthorized", account_page(
                browser, register=False, error="Invalid username or password."
            )
        if not authenticated:
            if method == "GET" and path in ("/", "/login"):
                return "200 OK", account_page(browser, register=False)
            headers.append(("Location", "/login"))
            return "303 See Other", "Log in to continue."
        if method == "POST" and path == "/logout":
            self.auth.logout(session)
            headers.extend(
                [
                    (
                        "Set-Cookie",
                        f"{SESSION_COOKIE}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
                    ),
                    self.browser_cookie(secrets.token_hex(32)),
                    ("Location", "/login"),
                ]
            )
            return "303 See Other", "Logged out."
        if path == "/login":
            headers.append(("Location", "/"))
            return "303 See Other", "Already logged in."
        return None

    def __call__(self, env: dict[str, Any], start: Callable[..., Any]) -> Iterable[bytes]:
        status = "200 OK"
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Strict-Transport-Security", "max-age=31536000"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
            (
                "Content-Security-Policy",
                "default-src 'none'; form-action 'self' "
                "https://github.com; frame-ancestors 'none'; base-uri 'none'; "
                "script-src 'self'; style-src 'self'",
            ),
        ]
        if env.get("PATH_INFO") == "/health" and env.get("REQUEST_METHOD") == "GET":
            start(status, headers)
            return [b"OK"]
        asset = {
            "/assets/config.css": ("config.css", "text/css"),
            "/assets/config.js": ("config.js", "application/javascript"),
        }.get(env.get("PATH_INFO", ""))
        if asset and env.get("REQUEST_METHOD") == "GET":
            start(
                "200 OK",
                [
                    ("Content-Type", asset[1]),
                    ("Cache-Control", "public, max-age=300"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
            return [files("github_backup").joinpath("static", asset[0]).read_bytes()]
        authenticated = False
        browser = ""
        try:
            origin = (os.getenv("GITHUB_CONFIG_URL") or os.environ["GITHUB_SETUP_URL"]).rstrip("/")
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Config requires an HTTPS origin")
            # Never trust a request Host or forwarded header to construct callback URLs.
            if env.get("HTTP_HOST", "").casefold() != parsed.netloc.casefold():
                start("400 Bad Request", headers)
                return [b"Unexpected host"]
            cookies = SimpleCookie()
            cookies.load(env.get("HTTP_COOKIE", ""))
            cookie = cookies.get("__Host-github-backup-csrf")
            browser = cookie.value if cookie else ""
            if not re.fullmatch(r"[a-f0-9]{64}", browser):
                browser = secrets.token_hex(32)
                headers.append(
                    (
                        "Set-Cookie",
                        f"__Host-github-backup-csrf={browser}; "
                        "Secure; HttpOnly; SameSite=Lax; Path=/",
                    )
                )
            method, path = str(env.get("REQUEST_METHOD", "")), str(env.get("PATH_INFO", ""))
            form = {}
            if method == "POST":
                size = int(env.get("CONTENT_LENGTH") or "0")
                limit = 4096 if path in ("/register", "/login", "/logout", "/account") else 262144
                if not 0 < size <= limit:
                    raise ValueError("Invalid request size")
                if env.get("CONTENT_TYPE", "").split(";")[0] != "application/x-www-form-urlencoded":
                    raise ValueError("Invalid form encoding")
                raw = env["wsgi.input"].read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete form body")
                form = parse_qs(
                    raw.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    errors="strict",
                    max_num_fields=10000,
                )
                allowed = {
                    "/register": {"csrf", "username", "password", "confirmation", "code"},
                    "/login": {"csrf", "username", "password"},
                    "/logout": {"csrf"},
                    "/account": {
                        "csrf",
                        "username",
                        "password",
                        "confirmation",
                        "current_password",
                    },
                    "/connect": {"csrf", "registration_owner"},
                    "/owners": {"csrf", "owner", "app_id"},
                    "/scope": {"csrf", "owner", "app_id", "mode", "repositories"},
                    "/spaces": {
                        "csrf",
                        "bucket",
                        "region",
                        "prefix",
                        "access_key_id",
                        "secret_access_key",
                        "confirm_destination",
                        "current_password",
                    },
                    "/monitoring": {
                        "csrf",
                        "url",
                        "enabled",
                        "allow_http",
                        "timeout_seconds",
                        "max_success_age_hours",
                    },
                }.get(path, {"csrf"})
                if set(form) - allowed or any(
                    len(values) != 1 for key, values in form.items() if key != "repositories"
                ):
                    raise ValueError("Unexpected or repeated form field")
                if not secrets.compare_digest(form.get("csrf", [""])[0], browser):
                    raise ValueError("Invalid form token; reload config")
            session_cookie = cookies.get(SESSION_COOKIE)
            session = session_cookie.value if session_cookie else ""
            response = self.account_response(method, path, browser, session, form, headers)
            authenticated = response is None
            if response is not None:
                status, body = response
                notices = {
                    "notice=account-updated": "Account updated. Log in with your new credentials.",
                    "notice=registered": "Account created. Log in to configure backups.",
                }
                if path == "/login" and method == "GET" and env.get("QUERY_STRING") in notices:
                    body = (
                        '<div class="notice" role="status">'
                        + notices[env["QUERY_STRING"]]
                        + "</div>"
                        + body
                    )
            elif path == "/account":
                if method == "POST":
                    account_changed = self.auth.change_account(
                        session,
                        browser,
                        form.get("current_password", [""])[0],
                        form.get("username", [""])[0],
                        form.get("password", [""])[0],
                        form.get("confirmation", [""])[0],
                    )
                    if account_changed:
                        headers.extend(
                            [
                                ("Location", "/login?notice=account-updated"),
                                (
                                    "Set-Cookie",
                                    f"{SESSION_COOKIE}=; Secure; HttpOnly; "
                                    "SameSite=Lax; Path=/; Max-Age=0",
                                ),
                                self.browser_cookie(secrets.token_hex(32)),
                            ]
                        )
                        authenticated = False
                        status, body = "303 See Other", "Account updated. Sign in again."
                    else:
                        status, body = (
                            "400 Bad Request",
                            credentials_page(
                                self.auth.username(),
                                browser,
                                "Confirm your current password and enter a valid username "
                                "or a different strong password.",
                            ),
                        )
                elif method == "GET":
                    body = credentials_page(self.auth.username(), browser)
                else:
                    status, body = "405 Method Not Allowed", "Use the account form."
            elif method == "GET" and path == "/history":
                query = parse_qs(
                    env.get("QUERY_STRING", ""), keep_blank_values=True, max_num_fields=3
                )
                if set(query) - {"q", "page"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("Invalid activity query")
                page = int(query.get("page", ["1"])[0])
                term = query.get("q", [""])[0]
                if not 1 <= page <= 10 or len(term) > 100 or any(ord(c) < 32 for c in term):
                    raise ValueError("Invalid activity filter")
                try:
                    body = history_page(recent_backups(), page, term)
                except Exception:
                    status, body = (
                        "503 Service Unavailable",
                        (
                            "<h1>Activity is temporarily unavailable.</h1>"
                            "<p>Backups continue independently. "
                            "Check the worker logs for local history errors.</p>"
                            '<a href="/">Back to config</a>'
                        ),
                    )
            elif method == "GET" and path == "/callback":
                query = parse_qs(env.get("QUERY_STRING", ""), errors="strict", max_num_fields=10)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("Repeated query parameter")
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
                headers.append(("Location", origin + "/?notice=app-created#github"))
            elif method == "POST" and path in (
                "/connect",
                "/owners",
                "/scope",
                "/spaces",
                "/monitoring",
            ):
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
                        '<div class="intro"><p class="eyebrow">GitHub connection</p>'
                        '<h1>Continue on GitHub.</h1><p class="muted">Approve the read-only App, '
                        "then return to config to install it and connect an account.</p></div>"
                        '<form method="post" '
                        f'action="{target}?state={state}">'
                        f'<input type="hidden" name="manifest" value="{encoded_manifest}">'
                        '<div class="actions"><button>Create App on GitHub</button>'
                        '<a class="button secondary" href="/#github">Back to config</a>'
                        "</div></form>"
                    )
                else:
                    with LOCK:
                        data = current_settings()
                        if path == "/monitoring":
                            from github_backup.monitoring import PushMonitor

                            old = data.get("monitoring") or {}
                            candidate_monitor = {
                                "enabled": form.get("enabled") == ["yes"],
                                "url": form.get("url", [""])[0].strip() or old.get("url", ""),
                                "allow_http": form.get("allow_http") == ["yes"],
                                "timeout_seconds": float(
                                    form.get(
                                        "timeout_seconds", [str(old.get("timeout_seconds", 5))]
                                    )[0]
                                ),
                                "max_success_age_hours": float(
                                    form.get(
                                        "max_success_age_hours",
                                        [str(old.get("max_success_age_hours", 0))],
                                    )[0]
                                ),
                            }
                            PushMonitor.from_settings(candidate_monitor, 1)
                            data["monitoring"] = candidate_monitor
                            write_credentials(data)
                        elif path == "/scope":
                            save_scope(data, form)
                        elif path == "/spaces":
                            from github_backup.setup_settings import validate_spaces

                            previous = data.get("spaces") or {}
                            if previous and not self.auth.reauthenticate(
                                session, browser, form.get("current_password", [""])[0]
                            ):
                                raise ValueError(
                                    "Confirm the current password before editing storage"
                                )
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
                    headers.append(
                        (
                            "Location",
                            origin
                            + "/?notice=saved#"
                            + {
                                "/spaces": "storage",
                                "/monitoring": "monitoring",
                                "/owners": "scope",
                                "/scope": "scope",
                            }[path],
                        )
                    )
            elif method == "GET" and path == "/":
                body = home_page(current_settings(), browser)
                notices = {
                    "notice=saved": "Saved. Your configuration has been updated.",
                    "notice=app-created": (
                        "App created. Install it on GitHub, then connect its account below."
                    ),
                }
                if env.get("QUERY_STRING") in notices:
                    body = (
                        '<div class="notice" role="status">'
                        + notices[env["QUERY_STRING"]]
                        + "</div>"
                        + body
                    )
            elif method == "GET" and path == "/repositories":
                query = parse_qs(env.get("QUERY_STRING", ""), errors="strict", max_num_fields=10)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("Repeated query parameter")
                app = find_app(read_credentials(), query.get("app_id", [""])[0])
                body = repository_page(app, query.get("owner", [""])[0], browser)
            else:
                status, body = "404 Not Found", "Not found"
        except AuthLockedError:
            LOGGER.error("Config authentication unavailable; registration remains closed")
            status, body = (
                "503 Service Unavailable",
                (
                    "Authentication state is unavailable. Registration cannot be reopened. "
                    "Restore the complete authentication state or redeploy from scratch."
                ),
            )
        except AuthRateLimited:
            headers.append(("Retry-After", "60"))
            status, body = (
                "429 Too Many Requests",
                "Too many login attempts. Try again in one minute.",
            )
        except Exception:
            # Exceptions may contain the conversion code, API response, or other secrets.
            LOGGER.warning(
                "Config request failed; no stored credentials were intentionally removed"
            )
            status, body = (
                "400 Bad Request",
                (
                    "Config could not be saved. Check the config URL, "
                    "retry with a fresh flow, "
                    "or verify the App installation and repository permissions. "
                    "All/All except require "
                    "All repositories on GitHub. For Spaces check the region, bucket, "
                    "key permissions "
                    "and destination-change confirmation. "
                    '<a href="/">Return to config</a>'
                ),
            )
        start(status, headers)
        return [
            document(
                body,
                browser,
                authenticated=authenticated,
                current_path=str(env.get("PATH_INFO", "/")),
            ).encode()
        ]


application = SetupApplication()
