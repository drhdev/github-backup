"""Manifest security boundaries, durable credentials, and multi-owner onboarding."""

from __future__ import annotations

import base64
import html
import io
import json
import re
import subprocess
from unittest.mock import MagicMock, Mock
from urllib.error import URLError
from urllib.parse import urlencode

import pytest

from github_backup import onboarding as setup
from github_backup import scheduler
from github_backup.errors import GitHubAPIError


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "auth" / "credentials.json"
    monkeypatch.setattr(setup, "CREDENTIALS", path)
    return {
        "id": "123",
        "slug": "backup-test",
        "pem": "-----BEGIN PRIVATE KEY-----",
        "owners": [],
        "scopes": {},
    }


def read_app():
    return setup.read_credentials()["apps"][0]


@pytest.fixture
def ui(credentials, monkeypatch):
    monkeypatch.setenv("GITHUB_SETUP_URL", "https://backup.example.com")
    monkeypatch.setenv("GITHUB_SETUP_PASSWORD", "a" * 32)
    return setup.SetupApplication()


def request(ui, path="/", method="GET", form=None, cookie="", auth=True, **overrides):
    body = urlencode(form or {}).encode()
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "HTTP_HOST": "backup.example.com",
        "HTTP_COOKIE": cookie,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "wsgi.input": io.BytesIO(body),
    }
    if auth:
        env["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(b"admin:" + b"a" * 32).decode()
    env.update(overrides)
    response = Mock()
    output = b"".join(ui(env, response)).decode()
    status, headers = response.call_args.args
    return status, dict(headers), output


def start_flow(ui):
    status, headers, body = request(ui)
    assert status == "200 OK"
    token = re.search(r'name="csrf" value="([a-f0-9]+)"', body)[1]
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "Secure; HttpOnly; SameSite=Lax" in headers["Set-Cookie"]
    status, _, body = request(ui, "/connect", "POST", {"csrf": token}, cookie)
    assert status == "200 OK"
    state = re.search(r"new\?state=([a-f0-9]+)", body)[1]
    manifest = json.loads(html.unescape(re.search(r'name="manifest" value="([^"]+)"', body)[1]))
    assert manifest["default_permissions"] == {"contents": "read"}
    assert manifest["public"] is True
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["redirect_url"] == "https://backup.example.com/callback"
    return cookie, token, state


def test_health_and_authentication(ui):
    assert request(ui, "/health", auth=False)[0] == "200 OK"
    status, headers, body = request(ui, auth=False)
    assert status == "401 Unauthorized"
    assert "WWW-Authenticate" in headers
    assert "csrf" not in body
    assert request(ui, HTTP_HOST="evil.example")[0] == "400 Bad Request"


@pytest.mark.parametrize(
    "variable,value",
    [
        ("GITHUB_SETUP_URL", "http://backup.example.com"),
        ("GITHUB_SETUP_URL", "https://user@backup.example.com"),
        ("GITHUB_SETUP_URL", "https://backup.example.com/path"),
        ("GITHUB_SETUP_PASSWORD", "short"),
    ],
)
def test_invalid_server_configuration(ui, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    assert request(ui)[0] == "400 Bad Request"


def test_callback_persists_once_and_preserves_existing_app(ui, credentials, monkeypatch):
    cookie, token, state = start_flow(ui)
    exchange = Mock(return_value=credentials)
    monkeypatch.setattr(setup, "exchange_manifest", exchange)
    query = urlencode({"code": "secret-conversion-code", "state": state})
    result = request(ui, "/callback", cookie=cookie, QUERY_STRING=query)
    assert result[0] == "303 See Other"
    assert "pem" not in result[2]
    assert read_app() == credentials
    assert setup.CREDENTIALS.stat().st_mode & 0o077 == 0
    assert request(ui, "/callback", cookie=cookie, QUERY_STRING=query)[0] == "400 Bad Request"
    assert exchange.call_count == 1
    assert request(ui, "/connect", "POST", {"csrf": token}, cookie)[0] == "200 OK"
    assert "Install App" in request(ui, cookie=cookie)[2]
    assert not list(setup.CREDENTIALS.parent.glob(".credentials-*"))


@pytest.mark.parametrize("kind", ["wrong-browser", "expired", "wrong-state", "invalid-code"])
def test_callback_rejects_unbound_or_expired_codes(ui, monkeypatch, kind):
    cookie, _, state = start_flow(ui)
    code = "secret-conversion-code"
    if kind == "wrong-browser":
        cookie = "__Host-github-backup-setup=" + "b" * 64
    elif kind == "expired":
        browser, started = ui.states[state]
        ui.states[state] = (browser, started - 3601)
    elif kind == "wrong-state":
        state = "c" * 64
    else:
        code = "../../evil"
    exchange = Mock()
    monkeypatch.setattr(setup, "exchange_manifest", exchange)
    status, _, _ = request(
        ui, "/callback", cookie=cookie, QUERY_STRING=urlencode({"state": state, "code": code})
    )
    assert status == "400 Bad Request"
    exchange.assert_not_called()
    assert not setup.CREDENTIALS.exists()


def test_csrf_size_content_type_and_unknown_routes(ui):
    cookie, token, _ = start_flow(ui)
    assert request(ui, "/connect", "POST", {"csrf": "wrong"}, cookie)[0] == "400 Bad Request"
    assert (
        request(ui, "/connect", "POST", {"csrf": token}, cookie, CONTENT_LENGTH="999999")[0]
        == "400 Bad Request"
    )
    assert (
        request(ui, "/connect", "POST", {"csrf": token}, cookie, CONTENT_TYPE="application/json")[0]
        == "400 Bad Request"
    )
    assert request(ui, "/unknown")[0] == "404 Not Found"


def test_owner_additions_require_verified_all_repository_installations(
    ui, credentials, monkeypatch
):
    cookie, token, _ = start_flow(ui)
    setup.write_credentials({**credentials, "owners": ["alice"]})
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(setup, "AppClient", factory)
    status, _, _ = request(
        ui, "/owners", "POST", {"csrf": token, "app_id": "123", "owner": "Team"}, cookie
    )
    assert status == "303 See Other"
    assert factory.call_args.args[0].app_owners == ("team",)
    client.authenticated_user.assert_called_once()
    assert read_app()["owners"] == ["alice", "team"]
    client.authenticated_user.side_effect = GitHubAPIError("Select All repositories")
    assert (
        request(ui, "/owners", "POST", {"csrf": token, "app_id": "123", "owner": "bob"}, cookie)[0]
        == "400 Bad Request"
    )
    assert read_app()["owners"] == ["alice", "team"]
    assert (
        request(
            ui, "/owners", "POST", {"csrf": token, "app_id": "123", "owner": "../evil"}, cookie
        )[0]
        == "400 Bad Request"
    )


def test_conversion_failure_is_redacted_and_does_not_publish_credentials(ui, monkeypatch, caplog):
    cookie, _, state = start_flow(ui)
    monkeypatch.setattr(setup, "exchange_manifest", Mock(side_effect=URLError("secret-code")))
    status, _, body = request(
        ui,
        "/callback",
        cookie=cookie,
        QUERY_STRING=urlencode({"code": "secret-code", "state": state}),
    )
    assert status == "400 Bad Request"
    assert "secret-code" not in body + caplog.text
    assert not setup.CREDENTIALS.exists()


def test_credentials_missing_corrupt_permissions_and_atomic_replacement(credentials, monkeypatch):
    with pytest.raises(setup.SetupRequired):
        read_app()
    setup.write_credentials(credentials)
    setup.CREDENTIALS.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        read_app()
    setup.CREDENTIALS.chmod(0o600)
    setup.CREDENTIALS.write_text("{}")
    with pytest.raises(ValueError):
        read_app()
    setup.write_credentials(credentials)
    with pytest.raises(setup.SetupRequired):
        setup.stored_github_config()
    original = setup.CREDENTIALS.read_bytes()
    monkeypatch.setattr(setup.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        setup.write_credentials({**credentials, "owners": ["alice"]})
    assert setup.CREDENTIALS.read_bytes() == original
    assert not list(setup.CREDENTIALS.parent.glob(".credentials-*"))


def test_convert_real_private_key_and_ignore_unneeded_secrets(credentials, tmp_path, monkeypatch):
    key = tmp_path / "test-key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    payload = {
        "id": 123,
        "slug": "backup-test",
        "pem": key.read_text(),
        "client_secret": "unneeded",
        "webhook_secret": "unneeded",
    }
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.read.return_value = json.dumps(payload).encode()
    monkeypatch.setattr(setup, "build_opener", Mock(return_value=opener))
    result = setup.exchange_manifest("secret-code")
    assert set(result) == {"id", "slug", "pem", "owners"}
    assert result["owners"] == []
    request_arg = opener.open.call_args.args[0]
    assert request_arg.method == "POST"
    assert request_arg.full_url == "https://api.github.com/app-manifests/secret-code/conversions"
    response.read.return_value = b"x" * 131073
    with pytest.raises(ValueError, match="Oversized"):
        setup.exchange_manifest("another-code")
    response.read.return_value = b"{}"
    with pytest.raises(ValueError, match="Invalid"):
        setup.exchange_manifest("another-code")


def test_deployment_reads_saved_credentials_and_reloads_owners(credentials, tmp_path, monkeypatch):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_OWNERS", "GITHUB_APP_PRIVATE_KEY_BASE64"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SPACES_BUCKET"):
        monkeypatch.setenv(name, "test")
    setup.write_credentials({**credentials, "owners": ["alice"]})
    # Avoid sharing a fixed tmp key with other tests or local deployments.
    real_path = setup.Path
    monkeypatch.setattr(
        setup,
        "Path",
        lambda path: (
            tmp_path / "app.pem"
            if str(path).startswith("/tmp/github-backup-app-")
            else real_path(path)
        ),
    )
    config = scheduler.deployment_config()
    assert config.github.app_id == "123"
    assert config.github.app_owners == ("alice",)
    assert (tmp_path / "app.pem").stat().st_mode & 0o077 == 0
    setup.write_credentials({**credentials, "owners": ["alice", "team"]})
    assert [owner for _, owner, _ in setup.stored_github_client().connections] == ["alice", "team"]


def test_waiting_worker_heartbeat_is_healthy_without_claiming_backup_success(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE", tmp_path / "scheduler.json")
    monkeypatch.setattr(scheduler, "deployment_config", Mock(side_effect=setup.SetupRequired()))
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_BASE64", raising=False)
    monkeypatch.setattr(scheduler.signal, "signal", Mock())
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    monkeypatch.setattr(scheduler, "STOP", stop)
    spawn = Mock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    previous = scheduler.os.umask(0o077)
    try:
        scheduler.run()
    finally:
        scheduler.os.umask(previous)
    spawn.assert_not_called()
    state = json.loads(scheduler.STATE.read_text())
    assert state["waiting_for_setup"]
    assert state["last_success"] == 0
    assert state["exit_code"] == 1
    assert scheduler.health() == 0


def test_worker_starts_after_setup_without_restart(tmp_path, monkeypatch):
    from github_backup.config import BackupConfig

    monkeypatch.setattr(scheduler, "STATE", tmp_path / "scheduler.json")
    config = BackupConfig()
    monkeypatch.setattr(
        scheduler,
        "deployment_config",
        Mock(side_effect=[setup.SetupRequired(), setup.SetupRequired(), config]),
    )
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_BASE64", raising=False)
    monkeypatch.setattr(scheduler.signal, "signal", Mock())
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]
    monkeypatch.setattr(scheduler, "STOP", stop)
    process = Mock(returncode=0)
    process.poll.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    previous = scheduler.os.umask(0o077)
    try:
        scheduler.run()
    finally:
        scheduler.os.umask(previous)
    spawn.assert_called_once()
    state = json.loads(scheduler.STATE.read_text())
    assert not state["waiting_for_setup"]
    assert state["last_success"] > 0
    assert state["exit_code"] == 0


def test_another_callback_cannot_replace_an_existing_app(ui, credentials, monkeypatch):
    cookie, _, state = start_flow(ui)
    setup.write_credentials(credentials)
    exchange = Mock(return_value=credentials)
    monkeypatch.setattr(setup, "exchange_manifest", exchange)
    status, _, _ = request(
        ui,
        "/callback",
        cookie=cookie,
        QUERY_STRING=urlencode({"state": state, "code": "secret-code"}),
    )
    assert status == "400 Bad Request"
    exchange.assert_called_once()
    assert read_app() == credentials
