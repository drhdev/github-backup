"""Installation discovery and renewable credentials without live credentials."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest

from github_backup.app_auth import AppClient
from github_backup.config import BackupConfig, GitHubConfig, load_config
from github_backup.errors import ConfigurationError, GitHubAPIError
from github_backup.github import GitHubClient


def client():
    return AppClient(
        GitHubConfig(app_id="123", app_private_key="key.pem", app_owners=("alice", "team", "bob"))
    )


def installation(owner, selection="all"):
    return {"id": 42, "account": {"login": owner}, "repository_selection": selection}


def test_config(tmp_path):
    config = tmp_path / "app.toml"
    config.write_text(
        '[backup]\ninclude_gists=false\n[github]\napp_id="123"\n'
        'app_private_key="key.pem"\napp_owners=["alice","team","bob"]\n'
    )
    assert load_config(config).github.app_owners == ("alice", "team", "bob")
    with pytest.raises(ConfigurationError, match="gists"):
        replace(load_config(config), include_gists=True).validated()
    with pytest.raises(ConfigurationError, match="app_private_key"):
        BackupConfig(github=GitHubConfig(app_id="123")).validated()


def test_all_owners_and_selected_rejection():
    app = client()
    app._app.get_json = Mock(side_effect=lambda path: installation(path.split("/")[2]))
    assert app.authenticated_user() == "alice,team,bob"
    assert len(app._clients) == 3
    other = client()
    other._app.get_json = Mock(return_value=installation("alice", "selected"))
    with pytest.raises(GitHubAPIError, match="All repositories"):
        other.authenticated_user()


def test_cached_and_expiring_tokens():
    app = client()
    app._installations["alice"] = 42
    response = Mock()
    response.read.return_value = json.dumps(
        {"token": "new-secret", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    ).encode()
    app._open = MagicMock()
    app._open.return_value.__enter__.return_value = response
    with patch.object(app, "_token", return_value="signed-jwt"):
        assert app._installation_token("alice") == "new-secret"
        assert app._installation_token("alice") == "new-secret"
        assert app._open.call_count == 1
        app._tokens["alice"] = ("old", 0)
        assert app._installation_token("alice") == "new-secret"
        assert app._open.call_count == 2
    request = app._open.call_args.args[0]
    assert request.method == "POST"
    assert request.data == b"{}"


def test_routes_and_rejects_unconfigured_owner():
    app = client()
    app._app.get_json = Mock(return_value=installation("team"))
    assert app._owner_client("/repos/team/repo/releases") is app._client("team")
    with pytest.raises(GitHubAPIError, match="outside"):
        app.git_token("https://github.com/stranger/repo.git")
    with pytest.raises(GitHubAPIError, match="origin"):
        app.get_json("https://evil.example/repos/team/repo")


def test_installation_pagination():
    app = client()
    app._app.get_json = Mock(return_value=installation("alice"))
    api = app._client("alice")
    repo = {
        "id": 1,
        "name": "repo",
        "owner": {"login": "alice"},
        "clone_url": "https://github.com/alice/repo.git",
        "html_url": "https://github.com/alice/repo",
    }
    with patch.object(
        GitHubClient,
        "_request_json",
        side_effect=[
            (
                {"repositories": [repo]},
                {"Link": '<https://api.github.com/installation/repositories?page=2>; rel="next"'},
            ),
            ({"repositories": []}, {}),
        ],
    ) as request:
        assert len(api.repositories("accessible", ())) == 1
        assert request.call_count == 2
        assert request.call_args_list[0].args[0].startswith("/installation/repositories?")


def test_organization_fallback_and_git_host():
    app = client()
    app._app.get_json = Mock(
        side_effect=[GitHubAPIError("not a user", status_code=404), installation("team")]
    )
    app._client("team")
    assert app._app.get_json.call_args.args[0] == "/orgs/team/installation"
    with pytest.raises(GitHubAPIError, match="host"):
        app.git_token("https://evil.example/team/repo.git")
    app._tokens["team"] = ("team-token", 99999999999)
    assert app.git_token("https://github.com/team/repo.git") == "team-token"


def test_jwt_signing(tmp_path):
    import base64
    import subprocess

    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    key.chmod(0o600)
    app = AppClient(GitHubConfig(app_id="123", app_private_key=str(key), app_owners=("alice",)))
    header, payload, signature = app._jwt().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] == 600
    public = tmp_path / "public.pem"
    public.write_bytes(
        subprocess.run(
            ["openssl", "rsa", "-in", str(key), "-pubout"], check=True, capture_output=True
        ).stdout
    )
    sig = tmp_path / "signature"
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
        input=f"{header}.{payload}".encode(),
        check=True,
        capture_output=True,
    )
    key.chmod(0o644)
    with pytest.raises(ConfigurationError, match="permissions"):
        app._jwt()


def test_git_provider_refreshes_and_redacts():
    import base64
    import subprocess

    from github_backup.errors import GitCommandError
    from github_backup.gitops import GitRunner

    provider = Mock(side_effect=["initial", "fresh"])
    runner = GitRunner(provider, timeout_seconds=30)
    error = subprocess.CalledProcessError(1, ["git"], stderr="failed with fresh")
    with (
        patch("github_backup.gitops.subprocess.run", side_effect=error) as run,
        pytest.raises(GitCommandError) as caught,
    ):
        runner._run(["git", "fetch"], authenticated_url="https://github.com/alice/repo.git")
    assert "fresh" not in str(caught.value)
    auth = run.call_args.kwargs["env"]["GIT_CONFIG_VALUE_0"]
    assert base64.b64decode(auth.removeprefix("Authorization: Basic ")) == b"x-access-token:fresh"


def test_cli_selects_app_without_personal_token(tmp_path):
    from github_backup.cli import main

    config = tmp_path / "app.toml"
    config.write_text(
        '[backup]\ninclude_gists=false\n[github]\napp_id="123"\n'
        'app_private_key="key.pem"\napp_owners=["alice","team","bob"]\n'
    )
    with (
        patch("github_backup.app_auth.AppClient") as app,
        patch("github_backup.cli.read_token", side_effect=AssertionError("PAT read")),
    ):
        app.return_value.authenticated_user.return_value = "alice,team,bob"
        app.return_value.repositories.return_value = []
        main(["--config", str(config), "list"])
        app.assert_called_once()
        app.return_value.repositories.assert_called_once()
