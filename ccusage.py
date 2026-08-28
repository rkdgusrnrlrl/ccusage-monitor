#!/usr/bin/env python3
"""
CommandCode usage monitor.

Usage:
    python ccusage.py
    python ccusage.py --watch
    python ccusage.py --watch --interval 5
    python ccusage.py --watch --no-clear
    python ccusage.py --raw

API key lookup order:
    1. COMMANDCODE_API_KEY
    2. COMMAND_CODE_API_KEY
    3. ~/.commandcode/auth.json

Note:
    This uses CommandCode's undocumented /alpha/billing/credits endpoint,
    so it may break if CommandCode changes its internal API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


API_BASE = "https://api.commandcode.ai"
CREDITS_ENDPOINT = "/alpha/billing/credits"


class CommandCodeError(RuntimeError):
    pass


def get_local_account_id() -> str | None:
    """Return the authenticated CommandCode user ID when it is available."""
    auth_path = Path.home() / ".commandcode" / "auth.json"
    if not auth_path.exists():
        return None

    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    user_id = auth.get("userId")
    return user_id.strip() if isinstance(user_id, str) and user_id.strip() else None


def get_api_key() -> str:
    """Find a CommandCode API key from env vars or ~/.commandcode/auth.json."""
    for name in ("COMMANDCODE_API_KEY", "COMMAND_CODE_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()

    auth_path = Path.home() / ".commandcode" / "auth.json"
    if not auth_path.exists():
        raise CommandCodeError(
            f"CommandCode auth file not found: {auth_path}\n"
            "Run 'cmdc login' first, or set COMMANDCODE_API_KEY."
        )

    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CommandCodeError(
            f"Could not parse {auth_path}: {exc}"
        ) from exc

    # Common / legacy direct shapes.
    for key in ("apiKey", "api_key", "token", "accessToken"):
        value = auth.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Possible nested shapes.
    for section_name in ("commandcode", "command-code"):
        section = auth.get(section_name)

        if isinstance(section, str) and section.strip():
            return section.strip()

        if isinstance(section, dict):
            for key in ("key", "access", "apiKey", "api_key", "token"):
                value = section.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    raise CommandCodeError(
        f"No usable CommandCode API key found in {auth_path}.\n"
        "Run 'cmdc login' again, or set COMMANDCODE_API_KEY."
    )


def api_get(path: str, api_key: str) -> Any:
    """GET a CommandCode API endpoint and parse JSON."""
    url = f"{API_BASE}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "ccusage-python/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CommandCodeError(
                "CommandCode rejected the API key (HTTP 401). "
                "Run 'cmdc login' again."
            ) from exc

        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""

        detail = f": {error_body}" if error_body else ""
        raise CommandCodeError(
            f"CommandCode API returned HTTP {exc.code} for {path}{detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CommandCodeError(
            f"Could not reach CommandCode API: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CommandCodeError(
            "CommandCode API returned invalid JSON."
        ) from exc


def percent(used: Any, cap: Any) -> float:
    try:
        used_f = float(used)
        cap_f = float(cap)
    except (TypeError, ValueError):
        return 0.0

    if cap_f <= 0:
        return 0.0

    return used_f / cap_f * 100.0


def format_number(value: Any) -> str:
    if value is None:
        return "-"

    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def normalize_timestamp(value: Any) -> float | None:
    """
    Convert an epoch timestamp to seconds.

    Handles both:
      - milliseconds: 1760000000000
      - seconds:      1760000000
    """
    if value is None:
        return None

    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None

    # Anything this large is almost certainly milliseconds.
    if ts > 10_000_000_000:
        ts /= 1000.0

    return ts


def format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts: list[str] = []

    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    else:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def format_reset(reset_at: Any) -> str:
    ts = normalize_timestamp(reset_at)
    if ts is None or ts <= 0:
        return "unknown"

    try:
        reset = datetime.fromtimestamp(ts).astimezone()
    except (OverflowError, OSError, ValueError):
        return "unknown"

    now = datetime.now().astimezone()
    remaining = reset - now

    if remaining.total_seconds() <= 0:
        relative = "now"
    else:
        relative = format_duration(remaining)

    return f"{relative} ({reset:%Y-%m-%d %H:%M:%S})"


def progress_bar(value: float, width: int = 24) -> str:
    clamped = max(0.0, min(100.0, value))
    filled = round((clamped / 100.0) * width)
    filled = max(0, min(width, filled))

    return "[" + "#" * filled + "-" * (width - filled) + "]"


def get_plan_name(plan_id: Any) -> str:
    if not isinstance(plan_id, str) or not plan_id.strip():
        return "Command Code"

    raw = plan_id.strip()
    normalized = raw.lower().replace("_", "-")

    mappings = (
        ("individual-goat", "GOAT"),
        ("individual-go", "Go"),
        ("individual-pro", "Pro"),
        ("individual-provider", "Provider"),
        ("individual-max", "Max"),
        ("individual-ultra", "Ultra"),
        ("teams-pro", "Teams Pro"),
    )

    for prefix, label in mappings:
        if normalized.startswith(prefix):
            return label

    return raw


def format_window(label: str, window: dict[str, Any]) -> list[str]:
    used = window.get("used")
    cap = window.get("cap")
    pct = percent(used, cap)

    exceeded = "  EXCEEDED" if window.get("exceeded") is True else ""

    return [
        (
            f"{label:<8}: "
            f"{progress_bar(pct)} "
            f"{pct:6.1f}%  "
            f"{format_number(used)} / {format_number(cap)}"
            f"{exceeded}"
        ),
        f"{'':10}reset in {format_reset(window.get('resetAt'))}",
    ]


def render_usage(data: dict[str, Any], watch: bool, interval: int) -> str:
    credits = data.get("credits") or {}
    limits = data.get("windowLimits") or {}

    five_hour = limits.get("fiveHour")
    weekly = limits.get("weekly")

    if not isinstance(five_hour, dict) and not isinstance(weekly, dict):
        raise CommandCodeError(
            "The billing endpoint responded, but windowLimits were missing. "
            "The undocumented API shape may have changed. "
            "Try: python ccusage.py --raw"
        )

    plan_name = get_plan_name(credits.get("planId"))

    lines = [
        "",
        f"Command Code {plan_name} usage",
        f"Updated : {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
    ]

    if isinstance(five_hour, dict):
        lines.extend(format_window("5 hour", five_hour))

    if isinstance(weekly, dict):
        lines.extend(format_window("Weekly", weekly))

    if credits:
        lines.extend(
            [
                "",
                (
                    "Credits : "
                    f"monthly={format_number(credits.get('monthlyCredits'))}  "
                    f"purchased={format_number(credits.get('purchasedCredits'))}  "
                    f"free={format_number(credits.get('freeCredits'))}"
                ),
            ]
        )

    if watch:
        lines.extend(["", f"Refreshing every {interval}s. Press Ctrl+C to stop."])

    return "\n".join(lines)


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def supports_ansi() -> bool:
    """Return whether stdout can safely receive ANSI cursor commands."""
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return False

    if os.name != "nt":
        return True

    # Enable virtual-terminal processing for the current Windows console when
    # possible. Older consoles simply use the clear-and-redraw fallback.
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False

        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enable_vt:
            return True

        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except (AttributeError, OSError):
        return False


class TerminalDisplay:
    """Update a watch display in place when the terminal supports ANSI codes."""

    def __init__(self) -> None:
        self.first_render = True
        self.in_place = supports_ansi()

    def show(self, output: str) -> None:
        if self.first_render:
            print(output)
            self.first_render = False
            return

        if not self.in_place:
            clear_screen()
            print(output)
            return

        # Clear each old line as it is replaced instead of blanking the whole
        # screen first. This prevents the visible flash caused by cls/clear.
        lines = output.splitlines()
        sys.stdout.write("\033[H")
        sys.stdout.write("".join(f"\033[2K{line}\n" for line in lines))
        sys.stdout.write("\033[J")
        sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show CommandCode account usage without opening the TUI."
    )

    parser.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="Refresh usage continuously.",
    )

    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=10,
        help="Refresh interval in seconds when --watch is enabled. Default: 10",
    )

    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw JSON response.",
    )

    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear the terminal between watch refreshes.",
    )

    return parser.parse_args()


def fetch_and_print(
    api_key: str,
    *,
    raw: bool,
    watch: bool,
    interval: int,
) -> str:
    data = api_get(CREDITS_ENDPOINT, api_key)

    if raw:
        return json.dumps(data, indent=2, ensure_ascii=False)
    else:
        return render_usage(data, watch=watch, interval=interval)


def main() -> int:
    args = parse_args()

    if args.interval < 2:
        print("error: --interval must be at least 2 seconds.", file=sys.stderr)
        return 2

    try:
        api_key = get_api_key()

        if not args.watch:
            print(fetch_and_print(
                api_key,
                raw=args.raw,
                watch=False,
                interval=args.interval,
            ))
            return 0

        display = TerminalDisplay()
        while True:
            try:
                output = fetch_and_print(
                    api_key,
                    raw=args.raw,
                    watch=True,
                    interval=args.interval,
                )
                if args.no_clear:
                    print(output)
                else:
                    display.show(output)
            except CommandCodeError as exc:
                print(f"ccusage error: {exc}", file=sys.stderr)
                print(f"Retrying in {args.interval}s...")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except CommandCodeError as exc:
        print(f"ccusage error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
