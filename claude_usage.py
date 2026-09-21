#!/usr/bin/env python3
"""Read Claude plan usage from the locally signed-in Claude Code session."""

from __future__ import annotations

import email.utils
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLAUDE_API_BASE = "https://api.anthropic.com"
USAGE_ENDPOINT = "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_REFRESH_SECONDS = 30
# The usage endpoint rate limits easily, so a failure must back off rather than
# retry on the next UI tick.
RETRY_SECONDS = 60
# A 429 means the endpoint wants us to stop knocking, so each further 429 waits
# longer than the last instead of hammering it once a minute.
RATE_LIMIT_BACKOFF_SECONDS = (60, 120, 300, 600)
# Beyond this the cached reading is too old to present as the current number.
STALE_AFTER_SECONDS = 150
REQUEST_TIMEOUT = 20
EXPIRY_SKEW_SECONDS = 30
LOGGER = logging.getLogger("ccusage-monitor")

# Refreshing the expired access token ourselves, guarded by everything below,
# so the first run of the day does not fail just because claude was not opened.
TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# A failed refresh waits this long before trying again, so a broken grant never
# burns attempts once a minute.
REFRESH_RETRY_SECONDS = 300
# Only one process may refresh at a time; a lock older than this is a leftover.
LOCK_STALE_SECONDS = 120
LOCK_WAIT_SECONDS = 5.0
BACKUP_SUFFIX = ".ccusage.bak"
TEMP_SUFFIX = ".ccusage.tmp"
LOCK_SUFFIX = ".ccusage.lock"

OAUTH_SECTION = "claudeAiOauth"
FIVE_HOUR_KEY = "five_hour"
SEVEN_DAY_KEY = "seven_day"


class ClaudeUsageError(RuntimeError):
    pass


class ClaudeRefreshRejected(ClaudeUsageError):
    """The stored refresh token is gone or rejected, so only a login helps."""


class ClaudeRateLimitError(ClaudeUsageError):
    """HTTP 429, carrying the server's Retry-After wait when it sent one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Auto refresh can be turned off from config.json. The window sets this at
# startup; the default keeps the daily first run working.
_auto_refresh_enabled = True
# Remembers a failed refresh so the next tick does not repeat it immediately.
_refresh_gate: dict[str, Any] = {"until": 0.0, "error": None, "mtime": None}
_refresh_gate_lock = threading.Lock()


def set_auto_refresh(enabled: bool) -> None:
    """Let config.json turn the built-in token refresh off."""
    global _auto_refresh_enabled
    _auto_refresh_enabled = bool(enabled)


def credentials_path() -> Path:
    """Return the Claude Code credential file, honoring CLAUDE_CONFIG_DIR."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _load_credentials(path: Path) -> dict[str, Any]:
    """Read the whole credential file so a rewrite can keep every other field."""
    if not path.exists():
        raise ClaudeUsageError(
            "No Claude Code credentials found. Run claude and sign in."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeUsageError("Could not read the Claude Code credentials.") from exc
    if not isinstance(payload, dict):
        raise ClaudeUsageError("Could not read the Claude Code credentials.")
    return payload


def _oauth_section(payload: dict[str, Any]) -> dict[str, Any]:
    section = payload.get(OAUTH_SECTION)
    if not isinstance(section, dict):
        raise ClaudeUsageError(
            "Claude Code is not signed in with a Claude subscription."
        )
    return section


def _session_fields(section: dict[str, Any]) -> tuple[str, str | None, Any]:
    token = section.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ClaudeUsageError(
            "Claude Code is not signed in with a Claude subscription."
        )
    subscription = section.get("subscriptionType")
    if not isinstance(subscription, str) or not subscription.strip():
        subscription = None
    return token.strip(), subscription, section.get("expiresAt")


def _is_expired(expires_at: Any) -> bool:
    if not isinstance(expires_at, (int, float)):
        return False
    return expires_at / 1000.0 <= time.time() + EXPIRY_SKEW_SECONDS


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_session() -> tuple[str, str | None]:
    """Read the OAuth token only for the current request. Never store or log it."""
    path = credentials_path()
    payload = _load_credentials(path)
    token, subscription, expires_at = _session_fields(_oauth_section(payload))
    if not _is_expired(expires_at):
        return token, subscription
    if not _auto_refresh_enabled:
        raise ClaudeUsageError(
            "The Claude Code session expired. Run claude to refresh it."
        )
    return _refresh_session(path)


def _check_refresh_gate(path: Path) -> None:
    """Stay off the token endpoint after a failure until the file changes."""
    with _refresh_gate_lock:
        error = _refresh_gate["error"]
        if error is None:
            return
        if _refresh_gate["mtime"] != _file_mtime(path):
            # Claude Code wrote new credentials, so the old failure is moot.
            _refresh_gate.update({"until": 0.0, "error": None, "mtime": None})
            return
        if time.monotonic() < _refresh_gate["until"]:
            raise ClaudeUsageError(str(error))
        _refresh_gate.update({"until": 0.0, "error": None, "mtime": None})


def _close_refresh_gate(path: Path, message: str, permanent: bool = False) -> None:
    """Remember a failed refresh so the next tick does not repeat it."""
    with _refresh_gate_lock:
        _refresh_gate.update(
            {
                "until": float("inf")
                if permanent
                else time.monotonic() + REFRESH_RETRY_SECONDS,
                "error": message,
                "mtime": _file_mtime(path),
            }
        )


def _acquire_refresh_lock(path: Path) -> int:
    """One refresh at a time, across every monitor process on this machine."""
    lock_path = path.with_name(path.name + LOCK_SUFFIX)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - (_file_mtime(lock_path) or 0.0)
            if age > LOCK_STALE_SECONDS:
                # The holder died without cleaning up.
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ClaudeUsageError("A Claude token refresh is already running.")
            time.sleep(0.2)
        except OSError as exc:
            raise ClaudeUsageError("Could not refresh the Claude session.") from exc


def _release_refresh_lock(path: Path, handle: int) -> None:
    lock_path = path.with_name(path.name + LOCK_SUFFIX)
    try:
        os.close(handle)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def _request_refresh(refresh_token: str) -> dict[str, Any]:
    """Trade the refresh token for a new session. The body is never logged."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CODE_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ccusage-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise ClaudeRefreshRejected(
                "Claude refused the saved login. Run claude and sign in again."
            ) from exc
        raise ClaudeUsageError(
            f"Claude token refresh returned HTTP {exc.code}."
        ) from exc
    except urllib.error.URLError as exc:
        raise ClaudeUsageError(
            f"Could not reach the Claude token endpoint: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ClaudeUsageError("Claude token refresh returned invalid JSON.") from exc

    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise ClaudeUsageError("Claude token refresh returned no access token.")
    return payload


def _merged_section(section: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
    """Update only the session fields and keep everything else untouched."""
    merged = dict(section)
    merged["accessToken"] = str(grant["access_token"]).strip()
    refresh_token = grant.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token.strip():
        # The endpoint rotates the refresh token, so the new one must be saved.
        merged["refreshToken"] = refresh_token.strip()
    expires_in = _as_float(grant.get("expires_in"))
    if expires_in is not None:
        merged["expiresAt"] = int((time.time() + expires_in) * 1000)
    scope = grant.get("scope")
    if isinstance(scope, str) and scope.strip():
        merged["scopes"] = scope.split()
    return merged


def _write_credentials(path: Path, payload: dict[str, Any]) -> None:
    """Back up, write atomically, verify, and roll back if anything is off."""
    original = path.read_bytes()
    backup_path = path.with_name(path.name + BACKUP_SUFFIX)
    backup_path.write_bytes(original)

    temp_path = path.with_name(path.name + TEMP_SUFFIX)
    try:
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
        written = json.loads(path.read_text(encoding="utf-8"))
        expected = payload[OAUTH_SECTION].get("accessToken")
        if written.get(OAUTH_SECTION, {}).get("accessToken") != expected:
            raise ClaudeUsageError("Verification of the written credentials failed.")
    except Exception:
        # Never leave the user without a working credential file.
        try:
            path.write_bytes(original)
        except OSError:
            LOGGER.error(
                "Could not restore the Claude credentials. A backup is at %s",
                backup_path,
            )
        raise
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _refresh_session(path: Path) -> tuple[str, str | None]:
    """Renew the expired access token with the stored refresh token."""
    _check_refresh_gate(path)
    handle = _acquire_refresh_lock(path)
    try:
        # Re-read under the lock. Claude Code or another monitor may have just
        # written a usable token, and then there is nothing to refresh.
        payload = _load_credentials(path)
        section = _oauth_section(payload)
        token, subscription, expires_at = _session_fields(section)
        if not _is_expired(expires_at):
            return token, subscription
        _check_refresh_gate(path)

        refresh_token = section.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise ClaudeUsageError(
                "The Claude Code session expired. Run claude to refresh it."
            )
        refresh_expires_at = section.get("refreshTokenExpiresAt")
        if isinstance(refresh_expires_at, (int, float)):
            if refresh_expires_at / 1000.0 <= time.time():
                raise ClaudeRefreshRejected(
                    "The Claude login expired. Run claude and sign in again."
                )

        try:
            grant = _request_refresh(refresh_token.strip())
        except ClaudeRefreshRejected as exc:
            # A dead grant will not heal on its own, so wait for a new file.
            _close_refresh_gate(path, str(exc), permanent=True)
            raise
        except ClaudeUsageError as exc:
            _close_refresh_gate(path, str(exc))
            raise
        finally:
            refresh_token = ""

        payload[OAUTH_SECTION] = _merged_section(section, grant)
        _write_credentials(path, payload)
        LOGGER.info("Refreshed the expired Claude session token.")
        new_token, subscription, _ = _session_fields(payload[OAUTH_SECTION])
        return new_token, subscription
    finally:
        _release_refresh_lock(path, handle)


def format_subscription(subscription: str | None) -> str | None:
    """Turn a raw plan slug such as max_5x into a short label."""
    if not subscription:
        return None
    return subscription.replace("_", " ").strip().title()


def format_claude_title(subscription: str | None) -> str:
    label = format_subscription(subscription)
    return f"Claude ({label})" if label else "Claude"


def _retry_after_seconds(value: str | None) -> float | None:
    """Read a Retry-After header, which is either a delay or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


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
        if exc.code == 429:
            raise ClaudeRateLimitError(
                "Claude usage API returned HTTP 429.",
                _retry_after_seconds(exc.headers.get("Retry-After")),
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
        self.rate_limit_strikes = 0
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

    def _backoff_seconds(self, exc: Exception) -> float:
        """Wait longer after each 429, honoring Retry-After when it is longer."""
        if not isinstance(exc, ClaudeRateLimitError):
            self.rate_limit_strikes = 0
            return self.retry_seconds
        step = min(self.rate_limit_strikes, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
        self.rate_limit_strikes += 1
        wait = float(RATE_LIMIT_BACKOFF_SECONDS[step])
        if exc.retry_after is not None:
            wait = max(wait, exc.retry_after)
        return max(self.refresh_seconds, wait)

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
                self.next_attempt_at = now + self._backoff_seconds(exc)
                self.last_error = str(exc)
                if self.last_usage is not None:
                    return self._cached()
                raise

            self.next_attempt_at = now + self.refresh_seconds
            self.rate_limit_strikes = 0
            self.last_usage = usage
            self.last_success_at = time.time()
            self.last_error = None
            return self._cached()


if __name__ == "__main__":
    print(json.dumps(dump_claude_usage(), indent=2))
