"""Safety properties of the scheduled Spaces deployment."""

from __future__ import annotations

import io
import json
from unittest.mock import Mock

import pytest

from github_backup import scheduler
from github_backup.config import BackupConfig, S3Config
from github_backup.errors import StorageError
from github_backup.models import RunManifest
from github_backup.storage import S3Storage, StorageBackend


def storage(**overrides):
    config = BackupConfig(
        storage="s3",
        mode="snapshot",
        snapshot_retention=2,
        s3=S3Config(bucket="test", prefix="private", **overrides),
    )
    result = object.__new__(S3Storage)
    StorageBackend.__init__(result, config)
    result._client = Mock()
    result._client.get_bucket_versioning.return_value = {}
    result._client.delete_objects.return_value = {}
    return result


def test_budget_reserves_concurrent_uploads_and_counts_pagination():
    backend = storage(max_storage_gib=1)
    backend._client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": "private/a", "Size": 800 * 1024**2}],
            "IsTruncated": True,
            "NextContinuationToken": "page2",
        },
        {"Contents": [{"Key": "private/b", "Size": 100 * 1024**2}]},
    ]
    backend._reserve_upload(50 * 1024**2)
    with pytest.raises(StorageError, match="budget"):
        backend._reserve_upload(100 * 1024**2)
    assert backend._client.list_objects_v2.call_count == 2


def test_versioned_bucket_cannot_claim_bounded_storage():
    backend = storage(max_storage_gib=1)
    backend._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    with pytest.raises(StorageError, match="unversioned"):
        backend._reserve_upload(10)


def test_failed_run_preserves_complete_backups_and_discards_partial():
    backend = storage(manifest_retention=30)
    backend._client.list_objects_v2.side_effect = [
        {"Contents": [{"Key": f"private/snapshots/{run}/repo"} for run in ("01", "02", "03")]},
        {"Contents": [{"Key": "private/snapshots/03/repo"}]},
        {"Contents": []},
    ]

    def manifest(**request):
        success = "03" not in request["Key"]
        return {
            "Body": io.BytesIO(
                json.dumps(
                    {"finished_at": "now", "counts": {"failed": 0 if success else 1, "partial": 0}}
                ).encode()
            )
        }

    backend._client.get_object.side_effect = manifest
    run = RunManifest(
        run_id="03",
        user="test",
        storage="s3",
        scope="accessible",
        mode="snapshot",
        started_at="now",
        fatal_error="failed",
    )
    backend.apply_retention(run)
    assert backend._client.delete_objects.call_count == 1
    assert backend._client.delete_objects.call_args.kwargs["Delete"]["Objects"] == [
        {"Key": "private/snapshots/03/repo"}
    ]


def test_delete_errors_surface():
    backend = storage()
    backend._client.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
    with pytest.raises(StorageError, match="deletion failed"):
        backend._delete_keys([{"Key": "private/old"}])


def test_environment_defaults_and_validation(monkeypatch):
    for key, value in {
        "GITHUB_APP_ID": "123",
        "GITHUB_APP_OWNERS": "alice, team, bob",
        "SPACES_BUCKET": "backups",
        "AWS_ACCESS_KEY_ID": "test-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "SPACES_REGION": "fra1",
    }.items():
        monkeypatch.setenv(key, value)
    config = scheduler.deployment_config()
    assert config.github.app_owners == ("alice", "team", "bob")
    assert config.snapshot_retention == 3
    assert config.version_weekly == 2
    assert config.version_monthly == 2
    assert config.version_keep_oldest
    assert config.version_min_age_days == 0
    assert config.s3.max_storage_gib == 100
    assert config.s3.distributed_lock is False
    assert not config.include_gists
    monkeypatch.setenv("BACKUP_RETENTION", "0")
    with pytest.raises(ValueError, match="positive retention"):
        scheduler.deployment_config()


def test_health_detects_failure_staleness_and_dead_scheduler(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(scheduler, "STATE", state)
    monkeypatch.setattr(scheduler.time, "time", lambda: 100000)
    payload = {"heartbeat": 100000, "last_success": 100000, "exit_code": 0, "running": False}
    state.write_text(json.dumps(payload))
    assert scheduler.health() == 0
    for update in ({"exit_code": 2}, {"heartbeat": 0}, {"last_success": 0}):
        state.write_text(json.dumps({**payload, **update}))
        assert scheduler.health() == 1


@pytest.mark.parametrize("code", [0, 2])
def test_scheduler_single_worker_and_outcomes(tmp_path, monkeypatch, code):
    import base64
    from dataclasses import replace

    config = BackupConfig(
        github=replace(BackupConfig().github, app_private_key=str(tmp_path / "app.pem"))
    )
    monkeypatch.setattr(scheduler, "deployment_config", lambda: config)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"private-key").decode())
    monkeypatch.setattr(scheduler, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(scheduler.signal, "signal", Mock())
    event = Mock()
    event.is_set.side_effect = [False, False, True]
    monkeypatch.setattr(scheduler, "STOP", event)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 0)
    process = Mock(returncode=code)
    process.poll.return_value = code
    spawn = Mock(return_value=process)
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    previous_umask = scheduler.os.umask(0o077)
    try:
        scheduler.run()
    finally:
        scheduler.os.umask(previous_umask)
    assert spawn.call_count == 1
    assert spawn.call_args.kwargs["start_new_session"] is True
    result = json.loads((tmp_path / "state.json").read_text())
    assert result["exit_code"] == code
    assert result["running"] is False
    assert (tmp_path / "app.pem").stat().st_mode & 0o077 == 0
    assert "GITHUB_APP_PRIVATE_KEY_BASE64" not in scheduler.os.environ


def test_scheduler_timeout_terminates_process_group(tmp_path, monkeypatch):
    import base64
    from dataclasses import replace

    config = BackupConfig(
        github=replace(BackupConfig().github, app_private_key=str(tmp_path / "key.pem"))
    )
    monkeypatch.setattr(scheduler, "deployment_config", lambda: config)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"key").decode())
    monkeypatch.setenv("BACKUP_TIMEOUT_HOURS", "1")
    monkeypatch.setattr(scheduler, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(scheduler.signal, "signal", Mock())
    event = Mock()
    event.is_set.side_effect = [False, True]
    monkeypatch.setattr(scheduler, "STOP", event)
    clock = iter([0, 0, 0, 0, 0, 4000, 4000])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    process = Mock(returncode=0, pid=1234)
    process.poll.side_effect = [None, 0]
    monkeypatch.setattr(scheduler.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(scheduler.os, "killpg", kill)
    previous_umask = scheduler.os.umask(0o077)
    try:
        scheduler.run()
    finally:
        scheduler.os.umask(previous_umask)
    kill.assert_called_once_with(1234, scheduler.signal.SIGTERM)
    assert json.loads((tmp_path / "state.json").read_text())["exit_code"] == 124


def test_flock_survives_killed_container_worker(tmp_path):
    import os
    import signal
    import subprocess
    import sys

    from github_backup.errors import BackupError
    from github_backup.locking import FlockLock

    lock = tmp_path / "run.flock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; from pathlib import Path; "
            "from github_backup.locking import FlockLock; "
            "lock=FlockLock(Path(sys.argv[1])); lock.__enter__(); "
            'print("locked",flush=True); time.sleep(60)',
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(BackupError), FlockLock(lock):
            pass
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        with FlockLock(lock):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()


def test_budget_only_lists_configured_prefix():
    backend = storage(max_storage_gib=1)

    def scoped_listing(**request):
        assert request["Prefix"] == "private/"
        # Other applications' multi-terabyte objects are deliberately outside this scope.
        return {"Contents": [{"Key": "private/my-artifact", "Size": 100}]}

    backend._client.list_objects_v2.side_effect = scoped_listing
    backend._reserve_upload(100)
    assert backend._reserved_bytes == 200


def test_env_missing_required_value_reports_variable_name(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("SPACES_BUCKET", "backups")
    with pytest.raises(ValueError, match="AWS_ACCESS_KEY_ID"):
        scheduler.deployment_config()
