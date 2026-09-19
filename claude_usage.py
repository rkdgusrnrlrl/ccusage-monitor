#!/usr/bin/env python3
"""Read Claude plan usage from the locally signed-in Claude Code session."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CLAUDE_API_BASE = "https://api.anthropic.com"
USAGE_ENDPOINT = "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_REFRESH_SECONDS = 30
# The usage endpoint rate limits easily, so a failure must back off rather than
# retry on the next UI tick.
RETRY_SECONDS = 60
# Beyond this the cached reading is too old to present as the current number.
STALE_AFTER_SECONDS = 150
REQUEST_TIMEOUT = 20
EXPIRY_SKEW_SECONDS = 30
LOGGER = logging.getLogger("ccusage-monitor")

OAUTH_SECTION = "claudeAiOauth"
FIVE_HOUR_KEY = "five_hour"
SEVEN_DAY_KEY = "seven_day"


class ClaudeUsageError(RuntimeError):
    pass


def credentials_path() -> Path:
    """Return the Claude Code credential file, honoring CLAUDE_CONFIG_DIR."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _read_session() -> tuple[str, str | None]:
    """Read the OAuth token only for the current request. Never store or log it."""
    path = credentials_path()
    if not path.exists():
        raise ClaudeUsageError(
            "No Claude Code credentials found. Run claude and sign in."
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeUsageError("Could not read the Claude Code credentials.") from exc

    section = payload.get(OAUTH_SECTION) if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        raise ClaudeUsageError(
            "Claude Code is not signed in with a Claude subscription."
        )

    token = section.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ClaudeUsageError(
            "Claude Code is not signed in with a Claude subscription."
        )

    expires_at = section.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        if expires_at / 1000.0 <= time.time() + EXPIRY_SKEW_SECONDS:
            raise ClaudeUsageError(
                "The Claude Code session expired. Run claude to refresh it."
            )

    subscription = section.get("subscriptionType")
    if not isinstance(subscription, str) or not subscription.strip():
        subscription = None
    return token.strip(), subscription


def format_subscription(subscription: str | None) -> str | None:
    """Turn a raw plan slug such as max_5x into a short label."""
    if not subscription:
        return None
    return subscription.replace("_", " ").strip().title()


def format_claude_title(subscription: str | None) -> str:
    label = format_subscription(subscription)
    return f"Claude ({label})" if label else "Claude"


def _get_json(token: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{CLAUDE_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": "ccusage-monitor/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ClaudeUsageError(
                "Claude rejected the local session. Run claude and sign in again."
            ) from exc
        raise ClaudeUsageError(f"Claude usage API returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ClaudeUsageError(
            f"Could not reach the Claude usage API: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ClaudeUsageError("Claude usage API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ClaudeUsageError("Claude usage API returned an unexpected payload.")
    return payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utilization_window(window: Any) -> dict[str, Any] | None:
    """Map one rate-limit window onto the shared used/cap/resetAt shape."""
    if not isinstance(window, dict):
        return None
    used = _as_float(window.get("utilization"))
    if used is None:
        return None
    return {
        "used": used,
        "cap": 100,
        "resetAt": window.get("resets_at"),
    }


def normalize_claude_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the 5 hour and 7 day plan windows the monitor displays."""
    return {
        "fiveHour": _utilization_window(payload.get(FIVE_HOUR_KEY)),
        "sevenDay": _utilization_window(payload.get(SEVEN_DAY_KEY)),
    }


def fetch_claude_usage() -> dict[str, Any]:
    """Read the local Claude Code session and request plan usage."""
    token, subscription = _read_session()
    try:
        payload = _get_json(token, USAGE_ENDPOINT)
    finally:
        token = ""
    usage = normalize_claude_usage(payload)
    usage["subscription"] = subscription
    return usage


def _dump_window(label: str, window: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(window, dict):
        return {"label": label, "available": False}
    return {
        "label": label,
        "available": True,
        "usedPercent": window.get("used"),
        "cap": window.get("cap"),
        "resetAt": window.get("resetAt"),
    }


def dump_claude_usage() -> dict[str, Any]:
    """Return a token-free snapshot for comparing with the /usage command."""
    usage = fetch_claude_usage()
    return {
        "subscription": usage.get("subscription"),
        "fiveHour": _dump_window("Claude (5 hour)", usage.get("fiveHour")),
        "sevenDay": _dump_window("Claude (7 day)", usage.get("sevenDay")),
    }


class ClaudeUsageClient:
    """Cache plan usage so the 1 second UI loop does not hit the API every tick."""

    def __init__(
        self,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        retry_seconds: int = RETRY_SECONDS,
    ) -> None:
        self.refresh_seconds = max(1, refresh_seconds)
        self.retry_seconds = max(self.refresh_seconds, retry_seconds)
        self.lock = threading.Lock()
        self.next_attempt_at = 0.0
        self.last_usage: dict[str, Any] | None = None
        self.last_success_at: float | None = None
        self.last_error: str | None = None

    def _cached(self) -> dict[str, Any]:
        """Hand back the cached reading, saying how old it is."""
        assert self.last_usage is not None
        age = 0.0 if self.last_success_at is None else time.time() - self.last_success_at
        return dict(
            self.last_usage,
            ageSeconds=age,
            stale=age > STALE_AFTER_SECONDS,
            error=self.last_error,
        )

    def read_usage(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            if now < self.next_attempt_at:
                if self.last_usage is not None:
                    return self._cached()
                raise ClaudeUsageError(self.last_error or "Claude usage unavailable.")

            try:
                usage = fetch_claude_usage()
            except Exception as exc:
                # The messages raised here never carry the token.
                LOGGER.info("Claude usage request failed: %s", exc)
                # Hold off either way. Without this a failure repeats every tick.
                self.next_attempt_at = now + self.retry_seconds
                self.last_error = str(exc)
                if self.last_usage is not None:
                    return self._cached()
                raise

            self.next_attempt_at = now + self.refresh_seconds
            self.last_usage = usage
            self.last_success_at = time.time()
            self.last_error = None
            return self._cached()


if __name__ == "__main__":
    print(json.dumps(dump_claude_usage(), indent=2))
