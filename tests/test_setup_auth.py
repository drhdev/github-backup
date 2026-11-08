"""First-owner claims, immutable registration, session boundaries, and live monitoring."""

from __future__ import annotations

import base64
import io
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from unittest.mock import Mock
from urllib.parse import urlencode, urlsplit

import pytest

from github_backup import onboarding, scheduler, setup_auth
from github_backup.monitoring import PushMonitor
from github_backup.setup_auth import AuthLockedError, AuthStore

PASSWORD = "A-unique-safe-passphrase-42!"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_auth, "AUTH_DIRECTORY", tmp_path / "setup-auth")
    monkeypatch.setattr(onboarding, "CREDENTIALS", tmp_path / "settings" / "credentials.json")
    monkeypatch.setenv("GITHUB_SETUP_URL", "https://backup.example.com")
    return onboarding.SetupApplication()


def http(app, jar, path="/", form=None, method=None, authorization=""):
    data = urlencode(form or {}, doseq=True).encode()
    parsed = urlsplit(path)
    env = {
        "HTTP_AUTHORIZATION": authorization,
        "REQUEST_METHOD": method or ("POST" if form is not None else "GET"),
        "PATH_INFO": parsed.path,
        "QUERY_STRING": parsed.query,
        "HTTP_HOST": "backup.example.com",
        "wsgi.input": io.BytesIO(data),
        "CONTENT_LENGTH": str(len(data)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "HTTP_COOKIE": "; ".join(f"{key}={value}" for key, value in jar.items()),
    }
    start = Mock()
    body = b"".join(app(env, start)).decode()
    status, headers = start.call_args.args
    for name, value in headers:
        if name == "Set-Cookie":
            cookies = SimpleCookie()
            cookies.load(value)
            for key, item in cookies.items():
                if item["max-age"] == "0":
                    jar.pop(key, None)
                else:
                    jar[key] = item.value
                assert item["secure"] and item["httponly"] and item["samesite"] == "Lax"
    return status, dict(headers), body


def csrf(body):
    return re.search(r'name="csrf" value="([a-f0-9]{64})"', body)[1]


def register(app, jar):
    status, _, body = http(app, jar)
    assert status == "200 OK"
    return http(
        app,
        jar,
        "/register",
        {
            "csrf": csrf(body),
            "username": "backup-owner",
            "password": PASSWORD,
            "confirmation": PASSWORD,
            "code": app.auth.registration_code(),
        },
    )


def login(app, jar):
    _, _, body = http(app, jar, "/login")
    return http(
        app, jar, "/login", {"csrf": csrf(body), "username": "backup-owner", "password": PASSWORD}
    )


def test_first_registration_then_login_and_logout(fresh):
    jar = {}
    status, _, page = http(fresh, jar)
    assert status == "200 OK" and "Create your config account" in page
    code = fresh.auth.registration_code()
    assert code not in page
    assert register(fresh, jar)[0] == "303 See Other"
    assert not (fresh.auth.directory / "registration-code").exists()
    assert fresh.auth.directory.stat().st_mode & 0o077 == 0
    assert (fresh.auth.directory / "auth.sqlite3").stat().st_mode & 0o077 == 0
    assert PASSWORD.encode() not in (fresh.auth.directory / "auth.sqlite3").read_bytes()
    assert http(fresh, jar, "/register")[0] == "404 Not Found"
    before = dict(jar)
    assert login(fresh, jar)[0] == "303 See Other"
    assert before["__Host-github-backup-csrf"] != jar["__Host-github-backup-csrf"]
    assert setup_auth.SESSION_COOKIE in jar
    status, _, page = http(fresh, jar)
    assert status == "200 OK" and "DigitalOcean Spaces" in page
    assert "Create your config account" not in page
    token = jar[setup_auth.SESSION_COOKIE]
    _, _, body = http(fresh, jar)
    assert http(fresh, jar, "/logout", {"csrf": csrf(body)})[0] == "303 See Other"
    assert setup_auth.SESSION_COOKIE not in jar
    assert not fresh.auth.authenticated(token, before["__Host-github-backup-csrf"])
    assert "current-password" in http(fresh, jar)[2]
    assert "Spaces access key ID" not in http(fresh, jar)[2]


def test_claim_requires_code_and_secure_matching_password(fresh):
    jar = {}
    _, _, body = http(fresh, jar)
    data = {
        "csrf": csrf(body),
        "username": "owner",
        "password": PASSWORD,
        "confirmation": PASSWORD,
        "code": "wrong-code",
    }
    assert http(fresh, jar, "/register", data)[0] == "400 Bad Request"
    code = fresh.auth.registration_code()
    assert (
        http(
            fresh,
            jar,
            "/register",
            {**data, "code": code, "password": "password", "confirmation": "password"},
        )[0]
        == "400 Bad Request"
    )
    assert (
        http(fresh, jar, "/register", {**data, "code": code, "confirmation": "does-not-match"})[0]
        == "400 Bad Request"
    )
    assert not fresh.auth.registered()
    assert http(fresh, jar, "/register", {**data, "code": code})[0] == "303 See Other"
    assert not fresh.auth.register("second", PASSWORD, PASSWORD, code)


def test_second_registration_and_basic_auth_cannot_bypass_login(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    other = {}
    _, _, body = http(fresh, other)
    assert "current-password" in body and 'action="/register"' not in body
    assert (
        http(
            fresh,
            other,
            "/register",
            {
                "csrf": csrf(body),
                "username": "attacker",
                "password": PASSWORD,
                "confirmation": PASSWORD,
                "code": "irrelevant",
            },
        )[0]
        == "404 Not Found"
    )
    assert (
        http(fresh, other, "/spaces", {"csrf": csrf(body), "bucket": "stolen"})[0]
        == "303 See Other"
    )
    monkeypatch.setenv("GITHUB_SETUP_PASSWORD", PASSWORD)
    old_basic = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    assert (
        http(
            fresh,
            other,
            "/spaces",
            {"csrf": csrf(body), "bucket": "stolen"},
            authorization=old_basic,
        )[0]
        == "303 See Other"
    )
    assert not onboarding.CREDENTIALS.exists()


def test_sessions_survive_worker_restart_but_expire_and_bind_to_browser(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    restarted = onboarding.SetupApplication()
    assert "DigitalOcean Spaces" in http(restarted, jar)[2]
    copied = {setup_auth.SESSION_COOKIE: jar[setup_auth.SESSION_COOKIE]}
    assert "current-password" in http(restarted, copied)[2]
    # A copied session without its browser binding is revoked, not silently accepted.
    assert "current-password" in http(restarted, jar)[2]
    login(restarted, jar)
    now = setup_auth.time.time()
    monkeypatch.setattr(setup_auth.time, "time", lambda: now + setup_auth.SESSION_IDLE + 1)
    assert "current-password" in http(restarted, jar)[2]


def test_failed_logins_are_rate_limited_persistently_without_permanent_lockout(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    _, _, body = http(fresh, jar)
    data = {"csrf": csrf(body), "username": "backup-owner", "password": "incorrect"}
    for _ in range(5):
        assert http(fresh, jar, "/login", data)[0] == "401 Unauthorized"
    assert http(onboarding.SetupApplication(), jar, "/login", data)[0] == "429 Too Many Requests"
    now = setup_auth.time.time()
    monkeypatch.setattr(setup_auth.time, "time", lambda: now + 61)
    assert http(fresh, jar, "/login", {**data, "password": PASSWORD})[0] == "303 See Other"


@pytest.mark.parametrize("damage", ["database", "marker", "directory", "account-row", "corrupt"])
def test_lost_authentication_state_never_reopens_registration(fresh, damage):
    register(fresh, {})
    root = fresh.auth.directory
    if damage == "database":
        (root / "auth.sqlite3").unlink()
    elif damage == "marker":
        (root.parent / ".setup-owner-deployment").unlink()
    elif damage == "directory":
        shutil.rmtree(root)
    elif damage == "account-row":
        with fresh.auth.database() as db:
            db.execute("DELETE FROM account")
    else:
        (root / "auth.sqlite3").write_bytes(b"damaged")
    status, _, page = http(onboarding.SetupApplication(), {})
    assert status == "503 Service Unavailable"
    assert "Registration cannot be reopened" in page
    assert 'action="/register"' not in page


def test_simultaneous_registrations_only_one_owner_wins(fresh):
    code = fresh.auth.registration_code()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda name: AuthStore().register(name, PASSWORD, PASSWORD, code),
                ["first-owner", "second-owner"],
            )
        )
    assert sorted(results) == [False, True]
    with pytest.raises(AuthLockedError):
        fresh.auth.registration_code()


def test_registration_and_login_require_csrf(fresh):
    code = fresh.auth.registration_code()
    assert (
        http(
            fresh,
            {},
            "/register",
            {
                "csrf": "forged",
                "username": "owner",
                "password": PASSWORD,
                "confirmation": PASSWORD,
                "code": code,
            },
        )[0]
        == "400 Bad Request"
    )
    jar = {}
    register(fresh, jar)
    assert (
        http(
            fresh,
            jar,
            "/login",
            {"csrf": "forged", "username": "backup-owner", "password": PASSWORD},
        )[0]
        == "400 Bad Request"
    )


def test_save_monitoring_and_disable_without_exposing_push_secret(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    _, _, body = http(fresh, jar)
    url = "https://kuma.example.com/api/push/private-push-token"
    data = {"csrf": csrf(body), "enabled": "yes", "url": url}
    assert http(fresh, jar, "/monitoring", data)[0] == "303 See Other"
    saved = onboarding.read_credentials()["monitoring"]
    assert saved["url"] == url and saved["enabled"]
    assert "private-push-token" not in http(fresh, jar)[2]
    monitor = scheduler.load_monitor(1)
    assert monitor.url == url
    monitor._next_push = 9999999999
    assert scheduler.load_monitor(1, monitor) is monitor
    assert (
        http(fresh, jar, "/monitoring", {**data, "url": "http://kuma/api/push/token"})[0]
        == "400 Bad Request"
    )
    assert onboarding.read_credentials()["monitoring"] == saved
    assert http(fresh, jar, "/monitoring", {"csrf": data["csrf"], "url": ""})[0] == "303 See Other"
    monkeypatch.setenv("UPTIME_KUMA_PUSH_URL", url)
    assert scheduler.load_monitor(1).url == ""  # Explicit saved disable overrides old env values.


def test_invalid_monitoring_reload_keeps_previous_without_interrupting_backups(monkeypatch):
    previous = PushMonitor("https://kuma.example.com/api/push/private-token")
    monkeypatch.setattr(
        scheduler,
        "current_settings",
        lambda: {"monitoring": {"enabled": True, "url": "not-a-push-url", "allow_http": False}},
    )
    assert scheduler.load_monitor(1, previous) is previous
    assert scheduler.load_monitor(1).url == ""


def test_absolute_session_expiry_and_bounded_sessions(fresh, monkeypatch):
    register(fresh, {})
    browser = "a" * 64
    token = fresh.auth.create_session(browser)
    now = setup_auth.time.time()
    for hour in range(1, 9):
        monkeypatch.setattr(setup_auth.time, "time", lambda hour=hour: now + hour * 3500)
        # Simulate frequent activity without allowing an absolute lifetime extension.
        with fresh.auth.database() as db:
            db.execute("UPDATE sessions SET seen=?", (setup_auth.time.time(),))
    monkeypatch.setattr(setup_auth.time, "time", lambda: now + setup_auth.SESSION_LIFETIME + 1)
    assert not fresh.auth.authenticated(token, browser)
    for _ in range(35):
        fresh.auth.create_session(browser)
    with fresh.auth.database() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 32


def test_monitoring_form_validates_advanced_options_and_private_http(fresh):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    _, _, body = http(fresh, jar)
    form = {
        "csrf": csrf(body),
        "enabled": "yes",
        "allow_http": "yes",
        "url": "http://private-kuma/api/push/private-token",
        "timeout_seconds": "3",
        "max_success_age_hours": "27",
    }
    assert http(fresh, jar, "/monitoring", form)[0] == "303 See Other"
    monitor = scheduler.load_monitor(24)
    assert monitor.timeout == 3 and monitor.max_age_seconds == 27 * 3600
    before = onboarding.CREDENTIALS.read_bytes()
    for bad in ("nan", "inf", "31", "-1"):
        assert (
            http(fresh, jar, "/monitoring", {**form, "timeout_seconds": bad})[0]
            == "400 Bad Request"
        )
        assert onboarding.CREDENTIALS.read_bytes() == before
