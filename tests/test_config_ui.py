"""Config credentials, bounded activity, and hostile form/output boundaries."""

from __future__ import annotations

import io
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_setup_auth import PASSWORD, csrf, http, login, register
from test_setup_auth import fresh as fresh_fixture

from github_backup import config_ui, history, onboarding
from github_backup.backup import BackupService
from github_backup.config import BackupConfig
from github_backup.models import BackupResult, Resource, RunManifest
from github_backup.setup_settings import validate_spaces

fresh = fresh_fixture


def result(number=1, status="success"):
    return BackupResult(
        resource=Resource(
            kind="repository",
            identifier=str(number),
            owner="owner",
            name=f"repo-{number}",
            clone_url="https://github.com/owner/repo.git",
            html_url="https://github.com/owner/repo",
            private=True,
        ),
        status=status,
        started_at="2026-11-08T00:00:00+00:00",
        finished_at="2026-11-08T00:00:05+00:00",
        duration_seconds=5,
        changed=True,
        error="private-credentials-and-exception-payload",
    )


def test_change_username_and_password_requires_password_and_revokes_all_sessions(fresh):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    other = {}
    login(fresh, other)
    status, _, body = http(fresh, jar, "/account")
    assert status == "200 OK" and "All browsers will be signed out" in body
    form = {
        "csrf": csrf(body),
        "username": "new-owner",
        "current_password": "wrong",
        "password": "Another-unique-passphrase-84!",
        "confirmation": "Another-unique-passphrase-84!",
    }
    assert http(fresh, jar, "/account", form)[0] == "400 Bad Request"
    assert fresh.auth.username() == "backup-owner"
    assert (
        http(fresh, jar, "/account", {**form, "current_password": PASSWORD})[0] == "303 See Other"
    )
    assert "current-password" in http(fresh, other)[2]
    assert fresh.auth.login("new-owner", form["password"])
    assert not fresh.auth.login("backup-owner", PASSWORD)
    assert http(fresh, {}, "/register")[0] == "404 Not Found"


def test_username_only_change_and_reject_xss_and_weak_password(fresh):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    _, _, body = http(fresh, jar, "/account")
    form = {
        "csrf": csrf(body),
        "username": '<script>alert("x")</script>',
        "current_password": PASSWORD,
        "password": "",
        "confirmation": "",
    }
    assert http(fresh, jar, "/account", form)[0] == "400 Bad Request"
    assert fresh.auth.username() == "backup-owner"
    assert (
        http(
            fresh,
            jar,
            "/account",
            {**form, "username": "safe-owner", "password": "password", "confirmation": "password"},
        )[0]
        == "400 Bad Request"
    )
    assert http(fresh, jar, "/account", {**form, "username": "safe-owner"})[0] == "303 See Other"
    assert fresh.auth.login("safe-owner", PASSWORD)


def test_history_has_latest_250_only_and_never_copies_exception_details(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY", tmp_path / "history" / "history.sqlite3")
    for number in range(260):
        history.record_backup(result(number, "failed" if number % 2 else "success"))
    rows = history.recent_backups()
    assert len(rows) == 250
    assert rows[0]["repository"] == "repo-259" and rows[-1]["repository"] == "repo-10"
    assert "private-credentials" not in str(rows)
    assert history.HISTORY.stat().st_mode & 0o077 == 0
    assert {r["status"] for r in rows} == {"success", "failed"}
    page = config_ui.history_page(rows, 1, "")
    assert "Page 1 of 10" in page and "Success" in page and "Failed" in page
    assert "08 Nov 2026" in page and "data-local" in page
    assert "repo-234<" not in page
    assert "repo-234" in config_ui.history_page(rows, 2, "")


def test_history_is_private_and_does_not_block_real_backups(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(history, "HISTORY", tmp_path / "history.sqlite3")
    assert history.recent_backups() == []
    callback = Mock(side_effect=OSError("private-credentials-and-exception-payload"))
    service = BackupService(BackupConfig(), Mock(), Mock(), Mock(), result_callback=callback)
    manifest = RunManifest(
        run_id="test",
        user="owner",
        storage="s3",
        scope="accessible",
        mode="versioned",
        started_at="now",
    )
    service._record_result(manifest, result())
    assert manifest.counts["success"] == 1
    assert "private-credentials-and-exception-payload" not in caplog.text
    assert "backup continues" in caplog.text
    gist = replace(result(), resource=replace(result().resource, kind="gist"))
    history.record_backup(gist)
    assert not history.HISTORY.exists()


def test_activity_route_requires_login_and_only_has_safe_fields(fresh, tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY", tmp_path / "history.sqlite3")
    history.record_backup(result())
    register(fresh, {})
    assert http(fresh, {}, "/history")[0] == "303 See Other"
    jar = {}
    login(fresh, jar)
    status, headers, body = http(fresh, jar, "/history")
    assert status == "200 OK" and "owner/repo-1" in body
    assert "private-credentials" not in body
    assert headers["Cache-Control"] == "no-store"
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_external_names_search_and_form_values_are_html_encoded():
    attack = '"><img src=x onerror=alert(1)>'
    app = {"id": "123", "slug": "app", "owners": ["owner"]}
    repo = replace(result().resource, name=attack)
    page = config_ui.repository_page(app, "owner", "a" * 64, [repo])
    assert attack not in page and "&lt;img" in page
    page = config_ui.history_page([{**result_row(), "repository": attack}], 1, "")
    assert attack not in page and "&lt;img" in page
    page = config_ui.history_page([], 1, attack)
    assert attack not in page and "&lt;img" in page
    assert 'value="&quot;&gt;&lt;img' in config_ui.field("q", "Search", attack)


def result_row():
    return {
        "owner": "owner",
        "repository": "repo",
        "status": "success",
        "finished": "2026-11-08T00:00:05+00:00",
        "duration": 1,
    }


def test_duplicate_and_unknown_form_parameters_are_rejected_before_actions(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    _, _, body = http(fresh, jar)
    nonce = csrf(body)
    writer = Mock()
    monkeypatch.setattr(onboarding, "write_credentials", writer)
    for form in ({"csrf": nonce, "unexpected": "<script>"},):
        assert http(fresh, jar, "/monitoring", form)[0] == "400 Bad Request"
    payload = f"csrf={nonce}&csrf=other&enabled=yes".encode()
    env = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/monitoring",
        "HTTP_HOST": "backup.example.com",
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
        "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in jar.items()),
    }
    response = Mock()
    b"".join(fresh(env, response))
    assert response.call_args.args[0] == "400 Bad Request"
    writer.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("prefix", "backups/<script>"),
        ("prefix", 'root/" onclick="alert(1)'),
        ("access_key_id", "key\r\nHeader: injected"),
        ("secret_access_key", "secret<script>"),
        ("region", "fra1.evil.example"),
    ],
)
def test_storage_rejects_control_code_and_destination_injection(field, value):
    data = {
        "bucket": "backups",
        "region": "fra1",
        "prefix": "github-backup",
        "access_key_id": "test-key",
        "secret_access_key": "test-secret",
    }
    with pytest.raises(ValueError):
        validate_spaces({**data, field: value})


def test_static_assets_are_local_and_do_not_accept_paths_or_embed_secrets(fresh):
    status, headers, body = http(fresh, {}, "/assets/config.js")
    assert status == "200 OK" and headers["Content-Type"] == "application/javascript"
    assert "innerHTML" not in body and "eval(" not in body and "fetch(" not in body
    status, headers, css = http(fresh, {}, "/assets/config.css")
    assert status == "200 OK" and "prefers-color-scheme:dark" in css
    assert "max-width:640px" in css and "font-size:16px" in css
    assert http(fresh, {}, "/assets/../../github-app/credentials.json")[0] == "303 See Other"


def test_saved_storage_cannot_be_changed_without_current_password(fresh, monkeypatch):
    jar = {}
    register(fresh, jar)
    login(fresh, jar)
    saved = {
        "bucket": "backups",
        "region": "fra1",
        "prefix": "github-backup",
        "access_key_id": "test-key",
        "secret_access_key": "test-secret",
    }
    onboarding.write_credentials({"version": 2, "apps": [], "spaces": saved})
    _, _, body = http(fresh, jar)
    checker = Mock()
    monkeypatch.setattr(onboarding, "check_spaces", checker)
    form = {
        **saved,
        "csrf": csrf(body),
        "prefix": "different-root",
        "confirm_destination": "yes",
        "current_password": "incorrect",
    }
    assert http(fresh, jar, "/spaces", form)[0] == "400 Bad Request"
    checker.assert_not_called()
    assert onboarding.read_credentials()["spaces"] == saved
    assert http(fresh, jar, "/spaces", {**form, "current_password": PASSWORD})[0] == "303 See Other"
    assert onboarding.read_credentials()["spaces"]["prefix"] == "different-root"
    assert "current_password" not in onboarding.read_credentials()["spaces"]
