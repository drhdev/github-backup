"""Push monitoring semantics, bounded failures, and Docker logging streams."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from github_backup.logging_utils import configure_logging, emit_event
from github_backup.monitoring import NoRedirect, PushMonitor


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr("github_backup.monitoring.time.time", lambda: 10000)
    result = PushMonitor("https://kuma.example/api/push/SECRET?status=up&msg=OK&ping=")
    result._open = MagicMock()
    result._open.return_value.__enter__.return_value = io.BytesIO(b'{"ok":true}')
    return result


def state(**overrides):
    return {
        "heartbeat": 10000,
        "last_success": 10000,
        "exit_code": 0,
        "running": False,
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, "up"),
        ({"exit_code": None}, "down"),
        ({"exit_code": 2}, "down"),
        ({"exit_code": 124}, "down"),
        ({"heartbeat": 0}, "down"),
        ({"last_success": 0, "running": True}, "down"),
    ],
)
def test_status_reports_real_success_and_overdue_running_jobs(monitor, overrides, expected):
    monitor.max_age_seconds = 3600
    monitor.tick(state(**overrides))
    request = monitor._open.call_args.args[0]
    query = parse_qs(urlsplit(request.full_url).query)
    assert query["status"] == [expected]
    assert "ping" not in query
    assert monitor._open.call_args.kwargs["timeout"] == 5


def test_disabled_makes_no_network_requests():
    monitor = PushMonitor()
    monitor._open = MagicMock()
    monitor.tick(state())
    monitor._open.assert_not_called()


def test_network_failure_is_isolated_and_url_is_not_logged(monitor, caplog):
    monitor._open.side_effect = OSError("network failed at https://kuma.example/api/push/SECRET")
    with caplog.at_level(logging.WARNING):
        monitor.tick(state())
        monitor.tick(state())
    assert monitor._open.call_count == 1
    assert "backup continues" in caplog.text
    assert "SECRET" not in caplog.text
    assert "kuma.example" not in caplog.text


def test_status_changes_are_sent_immediately_and_repeated_status_is_throttled(monitor):
    monitor._open.return_value.__enter__.side_effect = lambda: io.BytesIO(b'{"ok":true}')
    monitor.tick(state())
    monitor.tick(state())
    assert monitor._open.call_count == 1
    monitor.tick(state(exit_code=2))
    assert monitor._open.call_count == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://kuma.example/api/push/token",
        "https://user:pass@kuma.example/api/push/token",
        "https://kuma.example/other",
        "https://kuma.example/api/push/token#fragment",
    ],
)
def test_invalid_or_insecure_push_urls_fail_without_printing_them(url):
    with pytest.raises(ValueError) as caught:
        PushMonitor(url)
    assert url not in str(caught.value)


def test_private_http_requires_explicit_opt_in():
    monitor = PushMonitor("http://kuma:3001/api/push/token", allow_http=True)
    assert monitor.url.startswith("http://")
    assert NoRedirect().redirect_request(None) is None


def test_environment_defaults_and_auto_success_age(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_PUSH_URL", "https://kuma.example/api/push/token")
    monitor = PushMonitor.from_environment(1)
    assert monitor.max_age_seconds == 3 * 3600
    monkeypatch.setenv("UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS", "26")
    assert PushMonitor.from_environment(24).max_age_seconds == 26 * 3600


def test_standard_stdout_event_and_stderr_application_logs(capsys):
    root = logging.getLogger()
    previous = list(root.handlers)
    level = root.level
    try:
        configure_logging(verbose=False, json_logs=True)
        emit_event("backup_finished", exit_code=0)
        logging.getLogger("github_backup.test").warning("example warning")
        output = capsys.readouterr()
        assert json.loads(output.out)["event"] == "backup_finished"
        assert json.loads(output.err)["level"] == "warning"
        assert "timestamp" in json.loads(output.out)
    finally:
        root.handlers = previous
        root.setLevel(level)
