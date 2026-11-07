"""Multiple Apps, least-access repository scopes, and browser-managed Spaces."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest
from test_onboarding import credentials as credentials_fixture

# Reuse setup fixtures so every test has an isolated credential volume.
from test_onboarding import request, start_flow
from test_onboarding import ui as ui_fixture

from github_backup import onboarding as setup
from github_backup import scheduler
from github_backup.app_auth import AppClient, MultiAppClient, RepositoryScope
from github_backup.config import GitHubConfig
from github_backup.errors import GitHubAPIError
from github_backup.models import Resource
from github_backup.setup_settings import (
    check_spaces,
    empty_settings,
    normalize_settings,
    validate_spaces,
)

credentials = credentials_fixture
ui = ui_fixture


def repo(identifier, owner="alice"):
    return Resource(
        kind="repository",
        identifier=str(identifier),
        owner=owner,
        name=f"repo-{identifier}",
        clone_url=f"https://github.com/{owner}/repo-{identifier}.git",
        html_url=f"https://github.com/{owner}/repo-{identifier}",
        private=True,
    )


def spaces():
    return {
        "bucket": "shared-files",
        "region": "ams3",
        "prefix": "github-backup",
        "access_key_id": "private-access",
        "secret_access_key": "private-secret",
    }


def test_selected_github_installation_allowed_only_when_explicit():
    data = {"id": 1, "account": {"login": "alice"}, "repository_selection": "selected"}
    for require_all in (True, False):
        client = AppClient(
            GitHubConfig(app_id="123", app_owners=("alice",)), require_all=require_all
        )
        client._app.get_json = Mock(return_value=data)
        if require_all:
            with pytest.raises(GitHubAPIError, match="All repositories"):
                client.authenticated_user()
        else:
            assert client.authenticated_user() == "alice"


def test_multiple_apps_union_scope_and_git_credential_routing():
    first, second, third = Mock(), Mock(), Mock()
    first.repositories.return_value = [repo(1), repo(2)]
    second.repositories.return_value = [repo(2), repo(3)]
    third.repositories.return_value = [repo(4, "org"), repo(5, "org")]
    first.git_token.return_value = "first-token"
    second.git_token.return_value = "second-token"
    multi = MultiAppClient(
        [
            (first, "alice", RepositoryScope("include", frozenset({"1"}))),
            (second, "alice", RepositoryScope("include", frozenset({"2"}))),
            (third, "org", RepositoryScope("exclude", frozenset({"5"}))),
        ]
    )
    assert multi.authenticated_user() == "alice,org"
    assert [r.identifier for r in multi.repositories("accessible", ())] == ["1", "2", "4"]
    assert multi.git_token(repo(1).clone_url) == "first-token"
    assert multi.git_token(repo(2).clone_url) == "second-token"
    with pytest.raises(GitHubAPIError, match="selection"):
        multi.git_token(repo(3).clone_url)
    assert not multi.backup_enabled(repo(3))
    assert not multi.backup_enabled(repo(5, "org"))
    assert multi.backup_enabled(repo(1))  # Still selected but revoked/deleted must alert.
    assert multi.backup_enabled(repo(99, "unknown-owner"))


def test_pause_does_not_require_a_working_installation_and_preserves_archives():
    paused = Mock()
    multi = MultiAppClient([(paused, "alice", RepositoryScope("none"))])
    multi.authenticated_user()
    assert multi.repositories("accessible", ()) == []
    assert not multi.backup_enabled(repo(1))
    paused.authenticated_user.assert_not_called()
    paused.repositories.assert_not_called()


def test_cross_account_installation_response_fails_closed():
    client = Mock()
    client.repositories.return_value = [repo(1, "stranger")]
    multi = MultiAppClient([(client, "alice", RepositoryScope())])
    assert multi.repositories("accessible", ()) == []
    assert len(multi.discovery_errors) == 1


def test_migrate_single_app_and_reject_corrupt_scopes(credentials):
    result = normalize_settings({**credentials, "owners": ["alice"]})
    assert result["version"] == 2
    assert result["apps"][0]["owners"] == ["alice"]
    assert result["spaces"] is None
    result["apps"][0]["scopes"] = {"alice": {"mode": "include", "repositories": ["not-an-id"]}}
    with pytest.raises(ValueError, match="selection"):
        normalize_settings(result)


def test_add_app_preserves_other_apps_and_spaces(ui, credentials, monkeypatch):
    setup.write_credentials({"version": 2, "apps": [credentials], "spaces": spaces()})
    cookie, _, state = start_flow(ui)
    second = {**credentials, "id": "456", "slug": "second-app"}
    monkeypatch.setattr(setup, "exchange_manifest", Mock(return_value=second))
    status, _, _ = request(
        ui, "/callback", cookie=cookie, QUERY_STRING=f"state={state}&code=second-app-code"
    )
    assert status == "303 See Other"
    saved = setup.read_credentials()
    assert [app["id"] for app in saved["apps"]] == ["123", "456"]
    assert saved["spaces"] == spaces()


def test_scope_form_and_id_validation(ui, credentials, monkeypatch):
    cookie, token, _ = start_flow(ui)
    setup.write_credentials({**credentials, "owners": ["alice"]})
    client = Mock()
    client.repositories.return_value = [repo(1), repo(2)]
    monkeypatch.setattr(setup, "AppClient", Mock(return_value=client))
    status, _, body = request(
        ui, "/repositories", cookie=cookie, QUERY_STRING="app_id=123&owner=alice"
    )
    assert status == "200 OK"
    assert 'name="repositories" value="1"' in body
    form = {
        "csrf": token,
        "app_id": "123",
        "owner": "alice",
        "mode": "include",
        "repositories": "1",
    }
    assert request(ui, "/scope", "POST", form, cookie)[0] == "303 See Other"
    policy = setup.read_credentials()["apps"][0]["scopes"]["alice"]
    assert policy == {"mode": "include", "repositories": ["1"]}
    assert (
        request(ui, "/scope", "POST", {**form, "repositories": "999"}, cookie)[0]
        == "400 Bad Request"
    )
    assert setup.read_credentials()["apps"][0]["scopes"]["alice"] == policy
    client.repositories.return_value = []
    assert (
        "Unavailable repository ID 1"
        in request(ui, "/repositories", cookie=cookie, QUERY_STRING="app_id=123&owner=alice")[2]
    )
    assert request(ui, "/scope", "POST", form, cookie)[0] == "303 See Other"
    assert request(ui, "/scope", "POST", {**form, "mode": "none"}, cookie)[0] == "303 See Other"


def test_setup_saves_tested_spaces_without_echoing_secrets(ui, monkeypatch):
    cookie, token, _ = start_flow(ui)
    check = Mock()
    monkeypatch.setattr(setup, "check_spaces", check)
    candidate = spaces()
    assert (
        request(ui, "/spaces", "POST", {**candidate, "csrf": token}, cookie)[0] == "303 See Other"
    )
    check.assert_called_once_with(candidate)
    body = request(ui, cookie=cookie)[2]
    assert "private-access" not in body and "private-secret" not in body
    replacement = {
        **candidate,
        "bucket": "other-bucket",
        "csrf": token,
        "access_key_id": "",
        "secret_access_key": "",
    }
    assert request(ui, "/spaces", "POST", replacement, cookie)[0] == "400 Bad Request"
    assert setup.read_credentials()["spaces"] == candidate
    assert (
        request(ui, "/spaces", "POST", {**replacement, "confirm_destination": "yes"}, cookie)[0]
        == "303 See Other"
    )
    assert setup.read_credentials()["spaces"]["access_key_id"] == candidate["access_key_id"]
    before = setup.CREDENTIALS.read_bytes()
    check.side_effect = RuntimeError("private-secret")
    status, _, body = request(
        ui, "/spaces", "POST", {**candidate, "csrf": token, "confirm_destination": "yes"}, cookie
    )
    assert status == "400 Bad Request" and "private-secret" not in body
    assert setup.CREDENTIALS.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("region", "evil.example/path"),
        ("bucket", "http://evil"),
        ("prefix", ""),
        ("prefix", "../other-app"),
        ("secret_access_key", ""),
    ],
)
def test_spaces_validation_constrains_destination(field, value):
    with pytest.raises(ValueError):
        validate_spaces({**spaces(), field: value})


def test_spaces_probe_is_private_prefix_scoped_and_cleans_up(monkeypatch):
    client = Mock()
    client.get_bucket_versioning.return_value = {}
    client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(client.put_object.call_args.kwargs["Body"])
    }
    factory = Mock(return_value=client)
    monkeypatch.setattr("boto3.client", factory)
    check_spaces(spaces())
    assert factory.call_args.kwargs["endpoint_url"] == "https://ams3.digitaloceanspaces.com"
    put = client.put_object.call_args.kwargs
    assert put["ACL"] == "private"
    assert put["Key"].startswith("github-backup/_system/setup-check/")
    client.delete_object.assert_called_once_with(Bucket="shared-files", Key=put["Key"])
    client.close.assert_called_once()
    client.reset_mock()
    client.get_object.side_effect = RuntimeError("read failed")
    with pytest.raises(RuntimeError):
        check_spaces(spaces())
    client.delete_object.assert_called_once()
    client.get_bucket_versioning.return_value = {"Status": "Suspended"}
    client.reset_mock()
    with pytest.raises(ValueError, match="unversioned"):
        check_spaces(spaces())
    client.put_object.assert_not_called()


def test_saved_spaces_and_multiple_apps_reload_atomically(ui, credentials, tmp_path, monkeypatch):
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SPACES_BUCKET",
        "GITHUB_APP_ID",
        "GITHUB_APP_OWNERS",
        "GITHUB_APP_PRIVATE_KEY_BASE64",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = empty_settings()
    settings["spaces"] = spaces()
    settings["apps"] = [
        {
            **credentials,
            "owners": ["alice"],
            "scopes": {"alice": {"mode": "include", "repositories": ["1"]}},
        },
        {**credentials, "id": "456", "slug": "org-app", "owners": ["org"], "scopes": {}},
    ]
    setup.write_credentials(settings)
    real_path = setup.Path
    monkeypatch.setattr(setup, "Path", lambda name: tmp_path / real_path(name).name)
    config = scheduler.deployment_config()
    assert config.s3.bucket == "shared-files" and config.s3.region == "ams3"
    assert config.s3.secret_access_key == "private-secret"
    assert "private-secret" not in repr(config)
    multi = setup.stored_github_client()
    assert [
        (owner, client._config.app_id, policy.mode) for client, owner, policy in multi.connections
    ] == [("alice", "123", "include"), ("org", "456", "all")]
    assert len(set(client._config.app_private_key for client, _, _ in multi.connections)) == 2


def test_scope_changes_separate_excluded_from_missing_without_pruning(tmp_path):
    from contextlib import nullcontext

    from github_backup.backup import BackupService
    from github_backup.config import BackupConfig

    client = Mock()
    client.repositories.return_value = []
    github = MultiAppClient(
        [
            (client, "alice", RepositoryScope("include", frozenset({"1"}))),
        ]
    )
    storage = Mock()
    storage.workspace_root = tmp_path / "mirrors"
    storage.run_lock.return_value = nullcontext()
    storage.reconcile_inventory.return_value = [repo(1).to_dict(), repo(2).to_dict()]
    storage.write_manifest.return_value = "s3://test/manifest"
    git = Mock()
    manifest, _ = BackupService(
        BackupConfig(include_gists=False),
        github,
        git,
        storage,
    ).run()
    assert [r["identifier"] for r in manifest.missing_resources] == ["1"]
    assert [r["identifier"] for r in manifest.excluded_resources] == ["2"]
    assert manifest.to_dict()["excluded_resources"] == [repo(2).to_dict()]
    assert manifest.results == []
    assert storage.apply_retention.call_args.args[0].results == []
    git.update_mirror.assert_not_called()


def test_one_broken_connection_does_not_block_other_accounts():
    broken, working = Mock(), Mock()
    broken._config = GitHubConfig(app_id="123")
    broken.repositories.side_effect = GitHubAPIError("private-API-data")
    working.repositories.return_value = [repo(2, "org")]
    multi = MultiAppClient(
        [
            (broken, "alice", RepositoryScope()),
            (working, "org", RepositoryScope()),
        ]
    )
    assert [r.identifier for r in multi.repositories("accessible", ())] == ["2"]
    assert multi.discovery_errors == [
        {
            "app_id": "123",
            "owner": "alice",
            "error": "Installation discovery failed (GitHubAPIError)",
        }
    ]
    assert "private-API-data" not in str(multi.discovery_errors)
    broken.repositories.side_effect = None
    broken.repositories.return_value = [repo(1)]
    assert [r.identifier for r in multi.repositories("accessible", ())] == ["1", "2"]
    assert multi.discovery_errors == []


def test_connection_failure_marks_scheduled_run_failed(monkeypatch):
    from github_backup.config import BackupConfig
    from github_backup.models import RunManifest

    manifest = RunManifest(
        run_id="test",
        user="alice",
        storage="s3",
        scope="accessible",
        mode="versioned",
        started_at="now",
        discovery_errors=[{"owner": "alice", "error": "unavailable"}],
    )
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.setattr(scheduler, "current_settings", lambda: {})
    monkeypatch.setattr(scheduler, "deployment_config", lambda data: BackupConfig())
    monkeypatch.setattr(scheduler, "stored_github_client", lambda data: Mock())
    monkeypatch.setattr("github_backup.storage.create_storage", Mock())
    service = Mock()
    service.run.return_value = (manifest, "s3://test/manifest")
    monkeypatch.setattr("github_backup.backup.BackupService", Mock(return_value=service))
    with pytest.raises(SystemExit) as caught:
        scheduler.once()
    assert caught.value.code == 2
