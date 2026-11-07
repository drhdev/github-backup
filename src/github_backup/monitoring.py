"""Optional outbound Uptime Kuma Push monitoring; never logs the secret URL."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOGGER = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class PushMonitor:
    def __init__(
        self,
        url: str = "",
        *,
        allow_http: bool = False,
        timeout: float = 5,
        max_age_hours: float = 3,
    ) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("UPTIME_KUMA_TIMEOUT_SECONDS must be between 0 and 30")
        if not math.isfinite(max_age_hours) or max_age_hours <= 0:
            raise ValueError("Uptime Kuma success-age limit must be positive and finite")
        parts = urlsplit(url)
        if url and (
            parts.scheme not in (("https", "http") if allow_http else ("https",))
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or not re.search(r"/api/push/[A-Za-z0-9_-]+$", parts.path)
        ):
            raise ValueError(
                "UPTIME_KUMA_PUSH_URL must be a valid HTTPS Push monitor URL "
                "(HTTP requires UPTIME_KUMA_ALLOW_HTTP=true)"
            )
        self.url = url
        self.timeout = timeout
        self.max_age_seconds = max_age_hours * 3600
        self._open = build_opener(NoRedirect()).open
        self._next_push = 0.0
        self._previous: str | None = None

    @classmethod
    def from_environment(cls, interval_hours: float) -> PushMonitor:
        url = os.getenv("UPTIME_KUMA_PUSH_URL", "").strip()
        if not url:
            return cls()
        allow = os.getenv("UPTIME_KUMA_ALLOW_HTTP", "false").lower()
        if allow not in ("true", "false"):
            raise ValueError("UPTIME_KUMA_ALLOW_HTTP must be true or false")
        age = float(os.getenv("UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS", "0"))
        if not math.isfinite(age) or age < 0:
            raise ValueError("UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS must be finite and nonnegative")
        return cls(
            url,
            allow_http=allow == "true",
            timeout=float(os.getenv("UPTIME_KUMA_TIMEOUT_SECONDS", "5")),
            max_age_hours=age or 2 * interval_hours + 1,
        )

    def tick(self, state: Mapping[str, Any]) -> None:
        if not self.url:
            return
        now = time.time()
        if state.get("exit_code") is None:
            status, message = "down", "Waiting for first successful backup check"
        elif state["exit_code"] != 0:
            status, message = (
                "down",
                "Backup check failed or repository is unavailable; inspect logs",
            )
        elif now - float(state["last_success"]) > self.max_age_seconds:
            status, message = "down", "Successful backup check is overdue"
        elif now - float(state["heartbeat"]) > 180:
            status, message = "down", "Scheduler heartbeat is stale"
        else:
            status, message = "up", "Recent successful backup check; scheduler running"
        if now < self._next_push and status == self._previous:
            return
        # No tight retries if the monitoring server is down. Backup data has priority.
        self._next_push = now + 60
        self._previous = status
        parts = urlsplit(self.url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key not in ("status", "msg", "ping")
        ]
        query.extend((("status", status), ("msg", message)))
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        try:
            request = Request(url, headers={"User-Agent": "github-backup-monitor"})
            with self._open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(4096))
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Push rejected")
        except Exception as exc:
            # Exception strings often contain full URLs (including push tokens).
            LOGGER.warning("Uptime Kuma delivery failed (%s); backup continues", type(exc).__name__)
