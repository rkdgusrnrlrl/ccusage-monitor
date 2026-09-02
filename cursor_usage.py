#!/usr/bin/env python3
"""Read Cursor plan usage from the locally signed-in IDE session."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CURSOR_API_BASE = "https://api2.cursor.sh"
USAGE_ENDPOINT = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
SAND_USAGE_ENDPOINT = "/aiserver.v1.DashboardService/GetSandUsageStatus"
DEFAULT_REFRESH_SECONDS = 30
GROK_BOT_REFRESH_SECONDS = 1
REQUEST_TIMEOUT = 20
LOGGER = logging.getLogger("ccusage-monitor")
GROK_BOT_STATUS_KEYS = (
    "usagePercent",
    "hasNonZeroIncludedLimit",
    "includedLimitZero",
    "usesPooledEnterpriseAllowance",
    "hasAvailableUsage",
    "nextResetTimestampUtc",
    "currentPeriodStart",
    "grokPlanLabel",
)

ACCESS_TOKEN_KEY = "cursorAuth/accessToken"
MEMBERSHIP_KEY = "cursorAuth/stripeMembershipType"
PERCENT_IN_MESSAGE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class CursorUsageError(RuntimeError):
    pass


def state_db_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _decode_state_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith('"') and text.endswith('"'):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
    return text


def _read_state_values(keys: tuple[str, ...]) -> dict[str, str]:
    db_path = state_db_path()
    if not db_path.exists():
        raise CursorUsageError(
            "Cursor is not signed in on this machine. Open Cursor and sign in first."
        )

    placeholders = ", ".join("?" for _ in keys)
    query = f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})"

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(query, keys).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        with tempfile.TemporaryDirectory(prefix="ccusage-cursor-") as temp_dir:
            copied = Path(temp_dir) / "state.vscdb"
            copied.write_bytes(db_path.read_bytes())
            connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
            try:
                rows = connection.execute(query, keys).fetchall()
            finally:
                connection.close()

    values: dict[str, str] = {}
    for key, raw in rows:
        decoded = _decode_state_value(raw)
        if decoded:
            values[key] = decoded
    return values


def _read_session() -> tuple[str, str | None]:
    values = _read_state_values((ACCESS_TOKEN_KEY, MEMBERSHIP_KEY))
    token = values.get(ACCESS_TOKEN_KEY)
    if not token:
        raise CursorUsageError(
            "Cursor session was not found. Open Cursor and sign in first."
        )
    return token, values.get(MEMBERSHIP_KEY)


def format_membership(membership: str | None) -> str | None:
    if not membership or not membership.strip():
        return None
    label = membership.strip().replace("_", " ")
    return " ".join(part.capitalize() for part in label.split())


def format_cursor_title(membership: str | None) -> str:
    label = format_membership(membership)
    return f"Cursor ({label})" if label else "Cursor"


def _raise_payload_error(payload: dict[str, Any]) -> None:
    if "error" not in payload:
        return
    error = payload["error"]
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "request failed"
    else:
        message = "request failed"
    raise CursorUsageError(f"Cursor usage API error: {message}")


def _unwrap_usage_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CursorUsageError("Cursor usage API returned an unexpected payload.")
    _raise_payload_error(payload)
    result = payload.get("result")
    if isinstance(result, dict) and (
        "planUsage" in result or "billingCycleEnd" in result
    ):
        return result
    return payload


def _unwrap_sand_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CursorUsageError("Cursor Grok Bot usage API returned an unexpected payload.")
    _raise_payload_error(payload)
    result = payload.get("result")
    if isinstance(result, dict) and any(
        key in result
        for key in (
            "usagePercent",
            "hasNonZeroIncludedLimit",
            "includedLimitZero",
            "nextResetTimestampUtc",
            "currentPeriodStart",
        )
    ):
        return result
    return payload


def _connect_post(token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{CURSOR_API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Origin": "https://cursor.com",
            "User-Agent": "ccusage-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise CursorUsageError(
                "Cursor rejected the local session. Open Cursor and sign in again."
            ) from exc
        raise CursorUsageError(f"Cursor usage API returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise CursorUsageError(f"Could not reach Cursor usage API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise CursorUsageError("Cursor usage API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise CursorUsageError("Cursor usage API returned an unexpected payload.")
    _raise_payload_error(payload)
    return payload


def _post_json(token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _unwrap_usage_payload(_connect_post(token, path, body))


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent_from_message(message: Any) -> float | None:
    if not isinstance(message, str):
        return None
    match = PERCENT_IN_MESSAGE.search(message)
    if not match:
        return None
    return float(match.group(1))


def _percent_window(
    percent: float | None,
    reset_at: Any,
    period_start: Any = None,
    period_end: Any = None,
) -> dict[str, Any] | None:
    if percent is None:
        return None
    window: dict[str, Any] = {
        "used": percent,
        "cap": 100,
        "resetAt": reset_at,
    }
    if period_start is not None:
        window["periodStart"] = period_start
    if period_end is not None:
        window["periodEnd"] = period_end
    return window


def normalize_cursor_usage(payload: dict[str, Any]) -> dict[str, Any]:
    data = _unwrap_usage_payload(payload)
    plan = data.get("planUsage")
    if not isinstance(plan, dict):
        plan = {}

    period_start = data.get("billingCycleStart")
    period_end = data.get("billingCycleEnd")
    reset_at = period_end
    auto = _as_float(plan.get("autoPercentUsed"))
    api = _as_float(plan.get("apiPercentUsed"))
    if auto is None:
        auto = _percent_from_message(data.get("autoModelSelectedDisplayMessage"))
    if api is None:
        api = _percent_from_message(data.get("namedModelSelectedDisplayMessage"))

    cursor_models = _percent_window(auto, reset_at, period_start, period_end)
    other_models = _percent_window(api, reset_at, period_start, period_end)
    if cursor_models is None and other_models is None:
        raise CursorUsageError("Cursor usage response did not include monthly pool usage.")

    return {
        "cursorModels": cursor_models,
        "otherModels": other_models,
        "grokBot": None,
        "membership": format_membership(data.get("membershipType")),
    }


def _sand_status_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in GROK_BOT_STATUS_KEYS if key in payload}


def normalize_grok_bot_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = _unwrap_sand_payload(payload)
    if data.get("usesPooledEnterpriseAllowance") is True:
        return None
    if data.get("hasNonZeroIncludedLimit") is False:
        return None
    if data.get("includedLimitZero") is True:
        return None

    percent = _as_float(data.get("usagePercent"))
    if percent is None or percent < 0:
        return None

    reset_at = data.get("nextResetTimestampUtc")
    return _percent_window(percent, reset_at)


def _fetch_grok_bot_usage(
    token: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
    payload = _unwrap_sand_payload(_connect_post(token, SAND_USAGE_ENDPOINT, {}))
    keys = sorted(str(key) for key in payload.keys())
    return normalize_grok_bot_usage(payload), _sand_status_fields(payload), keys


def fetch_cursor_usage() -> dict[str, Any]:
    """Read the local Cursor session and request monthly plus Grok Bot usage."""
    token, membership = _read_session()
    period = _post_json(token, USAGE_ENDPOINT, {})
    usage = normalize_cursor_usage(period)
    grok_bot_error: str | None = None
    grok_bot_keys: list[str] = []
    try:
        grok_bot, grok_bot_status, grok_bot_keys = _fetch_grok_bot_usage(token)
    except Exception as exc:
        LOGGER.info("Cursor Grok Bot usage request failed")
        grok_bot = None
        grok_bot_status = {}
        grok_bot_error = str(exc)
    token = ""
    usage["grokBot"] = grok_bot
    usage["grokBotStatus"] = grok_bot_status
    usage["grokBotKeys"] = grok_bot_keys
    usage["grokBotError"] = grok_bot_error
    if usage.get("membership") is None:
        usage["membership"] = format_membership(membership)
    return usage


def _dump_window(label: str, window: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(window, dict):
        return {"label": label, "available": False}
    snapshot = {
        "label": label,
        "available": True,
        "usedPercent": window.get("used"),
        "cap": window.get("cap"),
        "resetAt": window.get("resetAt"),
    }
    if "periodStart" in window:
        snapshot["periodStart"] = window["periodStart"]
    if "periodEnd" in window:
        snapshot["periodEnd"] = window["periodEnd"]
    return snapshot


def dump_cursor_usage() -> dict[str, Any]:
    """Return a token-free snapshot for comparing with the Cursor dashboard."""
    usage = fetch_cursor_usage()
    snapshot: dict[str, Any] = {
        "membership": usage.get("membership"),
        "cursorModels": _dump_window("Cursor Models (monthly)", usage.get("cursorModels")),
        "otherModels": _dump_window("Other Models (monthly)", usage.get("otherModels")),
        "grokBot": _dump_window("Grok Bot (weekly)", usage.get("grokBot")),
        "grokBotStatus": usage.get("grokBotStatus") or {},
        "grokBotKeys": usage.get("grokBotKeys") or [],
    }
    if usage.get("grokBotError"):
        snapshot["grokBotError"] = usage["grokBotError"]
    return snapshot


class CursorUsageClient:
    """Cache monthly Cursor usage longer than Grok Bot usage."""

    def __init__(
        self,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        grok_refresh_seconds: int = GROK_BOT_REFRESH_SECONDS,
    ) -> None:
        self.refresh_seconds = max(1, refresh_seconds)
        self.grok_refresh_seconds = max(1, grok_refresh_seconds)
        self.lock = threading.Lock()
        self.last_monthly_fetch_at = 0.0
        self.last_grok_fetch_at = 0.0
        self.last_usage: dict[str, Any] | None = None
        self.last_error: str | None = None

    def read_usage(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            need_monthly = (
                self.last_usage is None
                or now - self.last_monthly_fetch_at >= self.refresh_seconds
            )
            need_grok = (
                self.last_usage is None
                or now - self.last_grok_fetch_at >= self.grok_refresh_seconds
            )
            if not need_monthly and not need_grok:
                return self.last_usage

            try:
                token, membership = _read_session()
            except Exception as exc:
                LOGGER.info("Cursor usage request failed")
                if self.last_usage is not None:
                    return self.last_usage
                self.last_error = str(exc)
                raise

            usage = dict(self.last_usage) if self.last_usage is not None else {}
            monthly_error: Exception | None = None

            if need_monthly:
                try:
                    monthly = normalize_cursor_usage(_post_json(token, USAGE_ENDPOINT, {}))
                    usage["cursorModels"] = monthly.get("cursorModels")
                    usage["otherModels"] = monthly.get("otherModels")
                    if monthly.get("membership") is not None:
                        usage["membership"] = monthly["membership"]
                    self.last_error = None
                except Exception as exc:
                    LOGGER.info("Cursor usage request failed")
                    monthly_error = exc
                self.last_monthly_fetch_at = now

            if need_grok:
                try:
                    grok_bot, grok_bot_status, grok_bot_keys = _fetch_grok_bot_usage(
                        token
                    )
                    usage["grokBot"] = grok_bot
                    usage["grokBotStatus"] = grok_bot_status
                    usage["grokBotKeys"] = grok_bot_keys
                    usage.pop("grokBotError", None)
                except Exception as exc:
                    LOGGER.info("Cursor Grok Bot usage request failed")
                    usage["grokBotError"] = str(exc)
                    if "grokBot" not in usage:
                        usage["grokBot"] = None
                        usage["grokBotStatus"] = {}
                        usage["grokBotKeys"] = []
                self.last_grok_fetch_at = now

            token = ""
            if usage.get("membership") is None:
                usage["membership"] = format_membership(membership)

            has_monthly = isinstance(usage.get("cursorModels"), dict) or isinstance(
                usage.get("otherModels"), dict
            )
            if not has_monthly and self.last_usage is None:
                if monthly_error is not None:
                    self.last_error = str(monthly_error)
                    raise monthly_error
                self.last_error = "Cursor usage is unavailable."
                raise CursorUsageError(self.last_error)

            self.last_usage = usage
            return usage


if __name__ == "__main__":
    print(json.dumps(dump_cursor_usage(), indent=2, ensure_ascii=False))
