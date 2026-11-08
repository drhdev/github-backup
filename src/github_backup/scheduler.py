"""Single-worker container scheduler with persisted outcome and health checks."""

from __future__ import annotations

import base64
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from github_backup.config import BackupConfig, GitHubConfig
from github_backup.logging_utils import configure_logging, emit_event
from github_backup.monitoring import PushMonitor
from github_backup.onboarding import (
    SetupRequired,
    current_settings,
    stored_github_client,
    stored_github_config,
)
from github_backup.setup_settings import validate_spaces
from github_backup.storage import _write_json_atomic

STATE = Path("/cache/scheduler.json")
STOP = threading.Event()


def deployment_config(settings: dict[str, Any] | None = None) -> BackupConfig:
    settings = current_settings() if settings is None else settings
    spaces = settings.get("spaces")
    if spaces is None:
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SPACES_BUCKET")
        if not any(os.getenv(name, "").strip() for name in names):
            raise SetupRequired("Configure DigitalOcean Spaces in setup")
        for name in names:
            if not os.getenv(name, "").strip():
                raise ValueError(f"Required environment variable is missing or empty: {name}")
        spaces = {
            "access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
            "secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            "bucket": os.environ["SPACES_BUCKET"],
            "region": os.getenv("SPACES_REGION", "fra1"),
            "prefix": os.getenv("SPACES_PREFIX", "github-backup"),
        }
    s3 = validate_spaces(spaces)
    oldest = os.getenv("BACKUP_KEEP_OLDEST", "true").lower()
    if oldest not in ("true", "false"):
        raise ValueError("BACKUP_KEEP_OLDEST must be true or false")
    manual = any(
        os.getenv(name, "").strip()
        for name in ("GITHUB_APP_ID", "GITHUB_APP_OWNERS", "GITHUB_APP_PRIVATE_KEY_BASE64")
    )
    if manual:
        if not os.getenv("GITHUB_APP_ID") or not os.getenv("GITHUB_APP_OWNERS"):
            raise ValueError("Manual authentication requires App ID, owners, and a private key")
        github = GitHubConfig(
            app_id=os.environ["GITHUB_APP_ID"],
            app_private_key="/tmp/app.pem",
            app_owners=tuple(
                v.strip() for v in os.environ["GITHUB_APP_OWNERS"].split(",") if v.strip()
            ),
        )
    else:
        github = stored_github_config(settings)
    config = BackupConfig(
        storage="s3",
        cache_dir=Path("/cache"),
        scope="accessible",
        include_gists=False,
        mode="versioned",
        version_min_age_days=int(os.getenv("BACKUP_MIN_AGE_DAYS", "0")),
        version_weekly=int(os.getenv("BACKUP_KEEP_WEEKLY", "2")),
        version_monthly=int(os.getenv("BACKUP_KEEP_MONTHLY", "2")),
        version_keep_oldest=oldest == "true",
        snapshot_retention=int(os.getenv("BACKUP_RETENTION", "3")),
        workers=int(os.getenv("BACKUP_WORKERS", "1")),
        github=github,
        s3=replace(
            s3,
            manifest_retention=int(os.getenv("BACKUP_MANIFEST_RETENTION", "14")),
            max_storage_gib=float(os.getenv("BACKUP_MAX_STORAGE_GIB", "100")),
        ),
    ).validated()
    if config.snapshot_retention < 1 or config.s3.max_storage_gib < 1:
        raise ValueError(
            "Scheduled deployment requires positive retention and a budget of at least 1 GiB"
        )
    if not config.s3.prefix.strip("/"):
        raise ValueError("SPACES_PREFIX must not be empty")
    return config


def health() -> int:
    try:
        state = json.loads(STATE.read_text())
        interval = float(os.getenv("BACKUP_INTERVAL_HOURS", "1")) * 3600
        if state.get("waiting_for_setup"):
            return int(time.time() - state["heartbeat"] > 180)
        return int(
            state.get("exit_code") not in (None, 0)
            or time.time() - state["heartbeat"] > 180
            or (not state.get("running") and time.time() - state["last_success"] > interval + 3600)
        )
    except (OSError, ValueError, KeyError):
        return 1


def load_monitor(interval_hours: float, previous: PushMonitor | None = None) -> PushMonitor:
    try:
        settings = current_settings()
        monitor = (
            PushMonitor.from_settings(settings["monitoring"], interval_hours)
            if "monitoring" in settings
            else PushMonitor.from_environment(interval_hours)
        )
        if previous is not None and (monitor.url, monitor.timeout, monitor.max_age_seconds) == (
            previous.url,
            previous.timeout,
            previous.max_age_seconds,
        ):
            return previous  # Preserve Push throttling across settings reloads.
        return monitor
    except Exception as exc:
        emit_event("monitoring_config_unavailable", error_type=type(exc).__name__)
        return previous if previous is not None else PushMonitor()


def run() -> None:
    os.umask(0o077)
    configure_logging(verbose=False, json_logs=True)
    try:
        config = deployment_config()
    except SetupRequired:
        config = None
    interval = float(os.getenv("BACKUP_INTERVAL_HOURS", "1")) * 3600
    timeout = float(os.getenv("BACKUP_TIMEOUT_HOURS", "12")) * 3600
    if not math.isfinite(timeout) or timeout < 60:
        raise ValueError("BACKUP_TIMEOUT_HOURS must be finite and at least one minute")
    if not math.isfinite(interval) or interval < 3600:
        raise ValueError("BACKUP_INTERVAL_HOURS must be at least 1")
    monitor = load_monitor(interval / 3600)
    encoded = os.environ.pop("GITHUB_APP_PRIVATE_KEY_BASE64", "")
    if encoded:
        key = base64.b64decode(encoded, validate=True)
        key_path = Path(config.github.app_private_key if config is not None else "/tmp/app.pem")
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    elif config is not None and os.getenv("GITHUB_APP_ID"):
        raise ValueError("Manual authentication requires GITHUB_APP_PRIVATE_KEY_BASE64")
    state: dict[str, object] = {
        "heartbeat": time.time(),
        "last_success": time.time() if config is not None else 0,
        "exit_code": None,
        "running": False,
    }
    try:
        previous = json.loads(STATE.read_text())
        state["last_success"] = previous["last_success"]
        state["exit_code"] = previous["exit_code"]
    except (OSError, ValueError, KeyError):
        pass
    process: subprocess.Popen[bytes] | None = None
    timed_out = False

    def stop(signum: int, frame: object) -> None:
        STOP.set()
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    due = time.monotonic()  # First backup immediately; subsequent runs on a fixed interval.
    emit_event(
        "scheduler_started", interval_hours=interval / 3600, monitoring_enabled=bool(monitor.url)
    )
    waiting_logged = False
    while not STOP.is_set():
        monitor = load_monitor(interval / 3600, monitor)
        try:
            if config is None:
                config = deployment_config()
            state["waiting_for_setup"] = False
        except SetupRequired:
            state["waiting_for_setup"] = True
            state["exit_code"] = 1
            if not waiting_logged:
                emit_event("waiting_for_setup")
                waiting_logged = True
            state["heartbeat"] = time.time()
            _write_json_atomic(STATE, state)
            monitor.tick(state)
            STOP.wait(30)
            continue
        if time.monotonic() >= due and process is None:
            process = subprocess.Popen(
                [sys.executable, "-m", "github_backup.scheduler", "once"], start_new_session=True
            )
            started = time.monotonic()
            timed_out = False
            state["running"] = True
            due = time.monotonic() + interval
        if process is not None and process.poll() is None and time.monotonic() - started > timeout:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if process is not None and process.poll() is not None:
            code = 124 if timed_out else process.returncode
            state.update(exit_code=code, running=False)
            if code == 0:
                state["last_success"] = time.time()
            emit_event("backup_finished", exit_code=code)
            process = None
            # Never immediately repeat a backup that took longer than its interval.
            due = max(due, time.monotonic() + 60)
        state["heartbeat"] = time.time()
        _write_json_atomic(STATE, state)
        monitor.tick(state)
        STOP.wait(30)
    if process is not None:
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def once() -> None:
    from github_backup.app_auth import AppClient
    from github_backup.backup import BackupService
    from github_backup.gitops import GitRunner
    from github_backup.history import record_backup
    from github_backup.storage import create_storage

    configure_logging(verbose=False, json_logs=True)
    settings = current_settings()
    config = deployment_config(settings)
    github = (
        AppClient(config.github) if os.getenv("GITHUB_APP_ID") else stored_github_client(settings)
    )
    git = GitRunner(
        github.git_token,
        timeout_seconds=config.git_timeout_seconds,
        max_retries=config.git_max_retries,
        verify_mirrors=True,
    )
    manifest, location = BackupService(
        config, github, git, create_storage(config), result_callback=record_backup
    ).run()
    emit_event(
        "manifest",
        location=location,
        counts=manifest.counts,
        missing_repositories=len(manifest.missing_resources),
        excluded_repositories=len(manifest.excluded_resources),
        discovery_errors=len(manifest.discovery_errors),
    )
    acknowledged = {
        value.strip()
        for value in os.getenv("ACKNOWLEDGED_MISSING_REPOSITORIES", "").split(",")
        if value.strip()
    }
    missing = [
        item for item in manifest.missing_resources if item["identifier"] not in acknowledged
    ]
    if (
        manifest.fatal_error
        or manifest.counts["failed"]
        or manifest.counts["partial"]
        or manifest.discovery_errors
        or missing
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    if sys.argv[1:] == ["health"]:
        raise SystemExit(health())
    if sys.argv[1:] == ["once"]:
        once()
    else:
        run()
