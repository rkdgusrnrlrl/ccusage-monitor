#!/usr/bin/env python3
"""Small always-on-top Windows monitor for Command Code usage."""

from __future__ import annotations

import queue
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any

import ccusage
import cursor_usage


WINDOW_WIDTH = 870
WINDOW_HEIGHT = 220
USAGE_SLOT_HEIGHT = 40
SPACED_GAP_HEIGHT = 20
SPACED_TITLE_PADY = 10
GAUGE_HEIGHT = 12
GAUGE_TROUGH = "#273449"
GAUGE_MARKER = "#f8fafc"
GAUGE_MARKER_WIDTH = 2
PACE_COLORS = {
    "under": "#94a3b8",
    "on": "#94a3b8",
    "over": "#f59e0b",
    "severe": "#ef4444",
}
GAUGE_COLORS = {
    "Blue": "#38bdf8",
    "Orange": "#f59e0b",
    "Red": "#ef4444",
}
CURSOR_REFRESH_SECONDS = 30
CURSOR_GROK_REFRESH_SECONDS = 1
CODEX_TIMEOUT = 15
LOG_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "ccusage-monitor"
    / "ccusage.log"
)

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("ccusage-monitor")


def application_directory() -> Path:
    """Return the folder containing this script or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = application_directory() / "config.json"
PARENT_CONFIG_PATH = application_directory().parent / "config.json"


def hidden_subprocess_options() -> dict[str, Any]:
    """Prevent helper console windows from appearing in the GUI application."""
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def find_codex_command() -> str:
    codex_command = shutil.which("codex")
    if codex_command is None:
        fallback = (
            Path.home()
            / "AppData"
            / "Local"
            / "Programs"
            / "OpenAI"
            / "Codex"
            / "bin"
            / "codex.exe"
        )
        codex_command = str(fallback) if fallback.exists() else None
    if codex_command is None:
        raise RuntimeError("Codex CLI not found")
    return codex_command


def load_app_config() -> dict[str, Any] | None:
    """Load the local app config beside the executable or project folder."""
    config_path = next(
        (
            path
            for path in (CONFIG_PATH, PARENT_CONFIG_PATH)
            if path.exists()
        ),
        None,
    )
    if config_path is None:
        return None

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ccusage.CommandCodeError(
            f"Could not read {config_path.name}: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise ccusage.CommandCodeError(
            f"{config_path.name} must contain a JSON object."
        )
    return config


def is_cursor_enabled() -> bool:
    """Cursor is on by default unless config explicitly disables it."""
    try:
        config = load_app_config()
    except ccusage.CommandCodeError:
        return True
    if config is None:
        return True
    cursor = config.get("cursor")
    if cursor is False:
        return False
    if isinstance(cursor, dict):
        return cursor.get("enabled", True) is not False
    return True


def get_commandcode_accounts() -> list[dict[str, str]]:
    """Load up to two CommandCode accounts without persisting their secrets."""
    configured_accounts = load_config_accounts()
    if configured_accounts is not None:
        return configured_accounts

    configured_accounts = (
        ("COMMANDCODE_API_KEY_PERSONAL", "COMMANDCODE_USER_ID_PERSONAL"),
        ("COMMANDCODE_API_KEY_WORK", "COMMANDCODE_USER_ID_WORK"),
    )
    has_named_account = any(os.environ.get(key) for key, _ in configured_accounts)

    if has_named_account:
        accounts: list[dict[str, str]] = []
        for key_name, id_name in configured_accounts:
            api_key = os.environ.get(key_name, "").strip()
            if api_key:
                accounts.append(
                    {
                        "api_key": api_key,
                        "account_id": os.environ.get(id_name, "").strip() or "unknown",
                    }
                )
        return accounts

    return [
        {
            "api_key": ccusage.get_api_key(),
            "account_id": ccusage.get_local_account_id() or "unknown",
        }
    ]


def load_config_accounts() -> list[dict[str, str]] | None:
    """Load account keys from config beside the app or its project folder."""
    config = load_app_config()
    if config is None:
        return None

    accounts = config.get("commandcode_accounts")
    if accounts is None:
        return None
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 2:
        raise ccusage.CommandCodeError(
            "config.json must contain one or two commandcode_accounts."
        )

    result: list[dict[str, str]] = []
    for index, account in enumerate(accounts, start=1):
        if not isinstance(account, dict):
            raise ccusage.CommandCodeError(
                f"commandcode_accounts[{index}] must be an object."
            )
        api_key = account.get("api_key")
        account_id = account.get("id", "unknown")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ccusage.CommandCodeError(
                f"commandcode_accounts[{index}].api_key is required."
            )
        if not isinstance(account_id, str) or not account_id.strip():
            raise ccusage.CommandCodeError(
                f"commandcode_accounts[{index}].id must be a non-empty string."
            )
        result.append(
            {"api_key": api_key.strip(), "account_id": account_id.strip()}
        )
    return result


def format_commandcode_title(account_id: str) -> str:
    """Keep a UUID-like account ID distinguishable within a compact column."""
    if len(account_id) > 16:
        account_id = f"{account_id[:8]}...{account_id[-4:]}"
    return f"CommandCode ({account_id})"


class CodexRateLimitClient:
    """Keep one app-server alive and restart it only after a real failure."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.next_request_id = 1

    def _start(self) -> None:
        codex_command = find_codex_command()
        LOGGER.info("Starting Codex app-server: %s", codex_command)
        self.process = subprocess.Popen(
            [codex_command, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            **hidden_subprocess_options(),
        )
        self.output_queue = queue.Queue()

        def read_output() -> None:
            assert self.process is not None
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put(line)

        threading.Thread(target=read_output, daemon=True).start()
        self._send(
            {
                "id": self._request_id(),
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "ccusage-window",
                        "version": "1.0.0",
                    }
                },
            }
        )
        self._wait_for(self.next_request_id - 1)
        self._send({"method": "initialized"})

    def _request_id(self) -> int:
        request_id = self.next_request_id
        self.next_request_id += 1
        return request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex app-server stdin unavailable")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _wait_for(self, response_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + CODEX_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Codex usage request timed out")
            try:
                line = self.output_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError("Codex usage request timed out") from exc
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == response_id:
                if "error" in response:
                    raise RuntimeError(str(response["error"]))
                return response

    def read_rate_limits(self) -> dict[str, Any]:
        with self.lock:
            try:
                if self.process is None or self.process.poll() is not None:
                    self._close_process()
                    self._start()

                request_id = self._request_id()
                self._send({"id": request_id, "method": "account/rateLimits/read"})
                response = self._wait_for(request_id)
                rate_limits = response.get("result", {}).get("rateLimits") or {}
                primary = rate_limits.get("primary")
                secondary = rate_limits.get("secondary")
                if not isinstance(primary, dict) or not isinstance(secondary, dict):
                    raise RuntimeError("Codex rate limit windows unavailable")
                return {
                    "primary": normalize_codex_window(primary),
                    "secondary": normalize_codex_window(secondary),
                }
            except Exception:
                LOGGER.exception("Codex app-server request failed")
                self._close_process()
                raise

    def _close_process(self) -> None:
        if self.process is None:
            return
        process_id = self.process.pid
        if self.process.poll() is None:
            if os.name == "nt":
                # Codex may create a child process for app-server. Terminating
                # only the Popen parent leaves that child orphaned on Windows.
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    **hidden_subprocess_options(),
                )
            else:
                self.process.kill()
        self.process.wait()
        self.process = None

    def close(self) -> None:
        with self.lock:
            LOGGER.info("Stopping Codex app-server")
            self._close_process()


def fetch_codex_rate_limits() -> dict[str, Any]:
    """Compatibility helper for one-off callers."""
    client = CodexRateLimitClient()
    try:
        return client.read_rate_limits()
    finally:
        client.close()


def normalize_codex_window(window: dict[str, Any]) -> dict[str, Any]:
    """Adapt Codex's percentage-based window to the common display format."""
    return {
        "used": window.get("usedPercent"),
        "cap": 100,
        "resetAt": window.get("resetsAt"),
    }


class GaugeBar(tk.Canvas):
    """Fixed-height usage bar. ttk progress bars ignore thickness on Windows."""

    def __init__(self, parent: tk.Widget, height: int, length: int = 80) -> None:
        super().__init__(
            parent,
            height=height,
            width=length,
            bg=GAUGE_TROUGH,
            highlightthickness=0,
            bd=0,
        )
        self._value = 0.0
        self._color = GAUGE_COLORS["Blue"]
        self._marker_percent: float | None = None
        self.bind("<Configure>", lambda _event: self._redraw())

    def set_usage(
        self,
        percent: float,
        color: str,
        marker_percent: float | None = None,
    ) -> None:
        self._value = max(0.0, min(100.0, percent))
        self._color = GAUGE_COLORS.get(color, GAUGE_COLORS["Blue"])
        if marker_percent is None:
            self._marker_percent = None
        else:
            self._marker_percent = max(0.0, min(100.0, marker_percent))
        self._redraw()

    def _redraw(self) -> None:
        self.delete("fill")
        self.delete("marker")
        width = self.winfo_width()
        height = self.winfo_height()
        fill_width = width * self._value / 100.0
        if fill_width > 0:
            self.create_rectangle(
                0,
                0,
                fill_width,
                height,
                fill=self._color,
                outline="",
                tags="fill",
            )
        if self._marker_percent is None or width <= 1:
            return
        marker_x = width * self._marker_percent / 100.0
        x0 = max(0, min(width - GAUGE_MARKER_WIDTH, round(marker_x) - 1))
        self.create_rectangle(
            x0,
            0,
            x0 + GAUGE_MARKER_WIDTH,
            height,
            fill=GAUGE_MARKER,
            outline="",
            tags="marker",
        )


class UsageWindow(tk.Tk):
    """Compact usage dashboard that keeps the UI responsive while polling."""

    def __init__(self, interval: int) -> None:
        super().__init__()
        self.title("AI Agent Usage")
        self.overrideredirect(True)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.maxsize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.resizable(False, False)
        self.attributes("-topmost", True)

        self.result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.refresh_pending = False
        self.after_id: str | None = None
        self.interval = interval
        self.refresh_ms = interval * 1000
        self.codex_client = CodexRateLimitClient()
        self.cursor_client = cursor_usage.CursorUsageClient(
            refresh_seconds=CURSOR_REFRESH_SECONDS,
            grok_refresh_seconds=CURSOR_GROK_REFRESH_SECONDS,
        )
        self._drag_x = 0
        self._drag_y = 0

        self._configure_style()
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after_id = self.after(100, self.refresh)

    def _configure_style(self) -> None:
        self.configure(bg="#111827")

    def _build_widgets(self) -> None:
        titlebar = tk.Frame(self, bg="#1f2937", height=26)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        titlebar.bind("<ButtonPress-1>", self._start_move)
        titlebar.bind("<B1-Motion>", self._move_window)

        title = tk.Label(
            titlebar,
            text="AI Agent Usage",
            bg="#1f2937",
            fg="#cbd5e1",
            font=("Segoe UI", 9),
            anchor="w",
        )
        title.pack(side="left", fill="both", expand=True, padx=(10, 0))
        title.bind("<ButtonPress-1>", self._start_move)
        title.bind("<B1-Motion>", self._move_window)

        close_button = tk.Button(
            titlebar,
            text="×",
            command=self._close,
            bg="#1f2937",
            fg="#cbd5e1",
            activebackground="#dc2626",
            activeforeground="#ffffff",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 12),
            width=3,
            cursor="hand2",
        )
        close_button.pack(side="right", fill="y")

        header_row = tk.Frame(self, bg="#111827")
        header_row.pack(fill="x", padx=12, pady=(8, 4))

        self.header = tk.Label(
            header_row,
            text="Usage",
            bg="#111827",
            fg="#f9fafb",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        )
        self.header.pack(side="left")

        self.status = tk.Label(
            header_row,
            text="Updated: connecting...",
            bg="#111827",
            fg="#94a3b8",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.status.pack(side="right")

        columns = tk.Frame(self, bg="#111827")
        columns.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for index in range(4):
            columns.columnconfigure(index, weight=1, uniform="col")
        columns.rowconfigure(0, weight=1)
        padx_by_col = ((0, 8), (8, 8), (8, 8), (8, 0))

        self.rows: dict[str, dict[str, tk.Widget]] = {}
        self.codex_title, codex_body = self._add_provider_column(
            columns, 0, "Codex", padx_by_col[0], title_pady=SPACED_TITLE_PADY
        )
        self._configure_row_slots(codex_body, gap=SPACED_GAP_HEIGHT)
        self._add_usage_row(self._row_slot(codex_body, 0), "codexPrimary", "5h")
        self._row_slot(codex_body, 1)
        self._add_usage_row(self._row_slot(codex_body, 2), "codexWeekly", "7d")

        self.cursor_title, cursor_body = self._add_provider_column(
            columns, 1, "Cursor", padx_by_col[1]
        )
        self._configure_row_slots(cursor_body)
        self._add_usage_row(self._row_slot(cursor_body, 0), "cursorModels", "cur")
        self._add_usage_row(self._row_slot(cursor_body, 1), "cursorOtherModels", "api")
        self._add_usage_row(self._row_slot(cursor_body, 2), "cursorGrokBot", "bot")

        self.commandcode_titles = []
        for index in range(2):
            account_number = index + 1
            title, body = self._add_provider_column(
                columns,
                index + 2,
                "CommandCode",
                padx_by_col[index + 2],
                title_pady=SPACED_TITLE_PADY,
            )
            self.commandcode_titles.append(title)
            self._configure_row_slots(body, gap=SPACED_GAP_HEIGHT)
            self._add_usage_row(
                self._row_slot(body, 0), f"commandcode{account_number}FiveHour", "5h"
            )
            self._row_slot(body, 1)
            self._add_usage_row(
                self._row_slot(body, 2), f"commandcode{account_number}Weekly", "7d"
            )

    def _start_move(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _move_window(self, event: tk.Event) -> None:
        self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _add_provider_column(
        self,
        parent: tk.Widget,
        column: int,
        text: str,
        padx: tuple[int, int],
        *,
        title_pady: int = 2,
    ) -> tuple[tk.Label, tk.Frame]:
        column_frame = tk.Frame(parent, bg="#111827")
        column_frame.grid(row=0, column=column, sticky="nsew", padx=padx)
        title = tk.Label(
            column_frame,
            text=text,
            bg="#111827",
            fg="#e2e8f0",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        title.pack(fill="x", pady=(0, title_pady))
        body = tk.Frame(column_frame, bg="#111827")
        body.pack(fill="both", expand=True)
        return title, body

    def _configure_row_slots(
        self,
        body: tk.Frame,
        *,
        gap: int | None = None,
    ) -> None:
        body.columnconfigure(0, weight=1)
        heights = (
            USAGE_SLOT_HEIGHT,
            USAGE_SLOT_HEIGHT if gap is None else gap,
            USAGE_SLOT_HEIGHT,
        )
        for index, height in enumerate(heights):
            body.rowconfigure(index, weight=0, minsize=height)

    def _row_slot(self, body: tk.Frame, index: int) -> tk.Frame:
        cell = tk.Frame(body, bg="#111827")
        cell.grid(row=index, column=0, sticky="nsew")
        return cell

    def _add_usage_row(
        self,
        parent: tk.Widget,
        key: str,
        label: str,
        *,
        bar_length: int = 80,
    ) -> None:
        row = tk.Frame(parent, bg="#111827")
        row.pack(fill="both", expand=True, pady=0)

        name = tk.Label(
            row,
            text=label,
            width=3,
            bg="#111827",
            fg="#cbd5e1",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        name.grid(row=0, column=0, rowspan=2, sticky="nsw")

        bar = GaugeBar(row, height=GAUGE_HEIGHT, length=bar_length)
        bar.grid(row=0, column=1, sticky="ew", padx=(0, 4))

        percent = tk.Label(
            row,
            text="--.-%",
            width=7,
            bg="#111827",
            fg="#f8fafc",
            font=("Segoe UI", 9, "bold"),
            anchor="e",
        )
        percent.grid(row=0, column=2, sticky="e")

        detail = tk.Label(
            row,
            text="Waiting for data",
            bg="#111827",
            fg="#64748b",
            font=("Segoe UI", 8),
            anchor="w",
        )
        detail.grid(row=1, column=1, columnspan=2, sticky="w")
        row.columnconfigure(1, weight=1)
        self.rows[key] = {"bar": bar, "percent": percent, "detail": detail}

    def refresh(self) -> None:
        self.after_id = self.after(self.refresh_ms, self.refresh)
        if self.refresh_pending:
            return

        self.refresh_pending = True
        threading.Thread(target=self._fetch_usage, daemon=True).start()
        self.after(50, self._consume_results)

    def _fetch_usage(self) -> None:
        commandcode_accounts: list[tuple[dict[str, str], dict[str, Any] | None]] = []
        cursor_data: dict[str, Any] | None = None
        codex_data: dict[str, Any] | None = None
        errors: list[str] = []

        try:
            for account in get_commandcode_accounts():
                try:
                    data = ccusage.api_get(
                        ccusage.CREDITS_ENDPOINT,
                        account["api_key"],
                    )
                    commandcode_accounts.append((account, data))
                except Exception as exc:
                    commandcode_accounts.append((account, None))
                    errors.append(f"CommandCode: {exc}")
        except Exception as exc:  # The message is shown in the small status area.
            errors.append(f"CommandCode: {exc}")

        if is_cursor_enabled():
            try:
                cursor_data = self.cursor_client.read_usage()
            except Exception as exc:  # The message is shown in the small status area.
                errors.append(f"Cursor: {exc}")

        try:
            codex_data = self.codex_client.read_rate_limits()
        except Exception as exc:  # The message is shown in the small status area.
            errors.append(f"Codex: {exc}")

        self.result_queue.put(
            ("data", (commandcode_accounts, cursor_data, codex_data, errors))
        )

    def _close(self) -> None:
        if self.after_id is not None:
            self.after_cancel(self.after_id)
        self.codex_client.close()
        self.destroy()

    def _consume_results(self) -> None:
        try:
            kind, value = self.result_queue.get_nowait()
        except queue.Empty:
            if self.refresh_pending:
                self.after(50, self._consume_results)
            return

        self.refresh_pending = False
        if kind == "data":
            commandcode_accounts, cursor_data, codex_data, errors = value
            self._update_usage(commandcode_accounts, cursor_data, codex_data, errors)
        else:
            self.status.configure(
                text=f"Updated: failed - {value}",
                fg="#fca5a5",
            )

    def _update_usage(
        self,
        commandcode_accounts: list[tuple[dict[str, str], dict[str, Any] | None]],
        cursor_data: dict[str, Any] | None,
        codex_data: dict[str, Any] | None,
        errors: list[str],
    ) -> None:
        if commandcode_accounts or cursor_data or codex_data:
            self.header.configure(text="Usage")
        else:
            self.header.configure(text="Usage unavailable")
        self._update_commandcode_usage(commandcode_accounts)
        self._update_cursor_usage(cursor_data)

        if codex_data is None:
            self._clear_row(self.rows["codexPrimary"])
            self._clear_row(self.rows["codexWeekly"])
        else:
            self._update_row(self.rows["codexPrimary"], codex_data["primary"])
            self._update_row(self.rows["codexWeekly"], codex_data["secondary"])

        status = f"Updated: {datetime.now():%H:%M:%S}"
        if errors:
            status += "  •  " + errors[0].split(": ", 1)[0] + " unavailable"
            self.status.configure(text=status, fg="#fca5a5")
        else:
            self.status.configure(text=status, fg="#86efac")

    def _update_commandcode_usage(
        self,
        accounts: list[tuple[dict[str, str], dict[str, Any] | None]],
    ) -> None:
        for index in range(2):
            account_number = index + 1
            five_hour_row = self.rows[f"commandcode{account_number}FiveHour"]
            weekly_row = self.rows[f"commandcode{account_number}Weekly"]

            if index >= len(accounts):
                self.commandcode_titles[index].configure(text="CommandCode (not configured)")
                self._clear_row(five_hour_row)
                self._clear_row(weekly_row)
                continue

            account, data = accounts[index]
            self.commandcode_titles[index].configure(
                text=format_commandcode_title(account["account_id"])
            )
            limits = (data or {}).get("windowLimits") or {}
            for row, key in ((five_hour_row, "fiveHour"), (weekly_row, "weekly")):
                window = limits.get(key)
                if isinstance(window, dict):
                    self._update_row(row, window)
                else:
                    self._clear_row(row)

    def _update_cursor_usage(self, cursor_data: dict[str, Any] | None) -> None:
        if cursor_data is None:
            self.cursor_title.configure(
                text="Cursor" if is_cursor_enabled() else "Cursor (off)"
            )
            self._clear_row(self.rows["cursorModels"])
            self._clear_row(self.rows["cursorOtherModels"])
            self._clear_row(self.rows["cursorGrokBot"])
            return

        self.cursor_title.configure(text="Cursor")
        monthly_pools = (
            ("cursorModels", "cursorModels"),
            ("otherModels", "cursorOtherModels"),
        )
        for pool_key, row_key in monthly_pools:
            pool = cursor_data.get(pool_key)
            if isinstance(pool, dict):
                self._update_row(self.rows[row_key], pool, pace=_cursor_monthly_pace(pool))
            else:
                self._clear_row(self.rows[row_key])

        grok_bot = cursor_data.get("grokBot")
        if isinstance(grok_bot, dict):
            self._update_row(self.rows["cursorGrokBot"], grok_bot)
        else:
            self._clear_row(self.rows["cursorGrokBot"])

    @staticmethod
    def _update_row(
        row: dict[str, tk.Widget],
        window: dict[str, Any],
        *,
        compact: bool = False,
        pace: dict[str, Any] | None = None,
    ) -> None:
        pct = ccusage.percent(window.get("used"), window.get("cap"))
        bar = row["bar"]
        percent = row["percent"]
        detail = row.get("detail")
        assert isinstance(bar, GaugeBar)
        assert isinstance(percent, tk.Label)

        color = "Red" if pct >= 95 else "Orange" if pct >= 80 else "Blue"
        marker_percent = pace.get("expectedPercent") if pace else None
        bar.set_usage(max(0, min(100, pct)), color, marker_percent=marker_percent)
        percent.configure(
            text=f"{pct:.0f}%" if compact else f"{pct:.1f}%",
            fg="#fca5a5" if pct >= 90 else "#f8fafc",
        )
        if not isinstance(detail, tk.Label):
            return
        prefix = window.get("prefix", "")
        if not isinstance(prefix, str):
            prefix = ""
        if compact:
            detail.configure(
                text=(
                    f"{prefix}{ccusage.format_number(window.get('used'))}/"
                    f"{prefix}{ccusage.format_number(window.get('cap'))}"
                ),
                fg="#94a3b8",
            )
            return
        reset_text = ccusage.format_reset(window.get("resetAt")).split(" (", 1)[0]
        if pace is not None:
            delta = round(float(pace.get("deltaPoints", 0)))
            status = pace.get("status")
            detail.configure(
                text=f"pace {delta:+d}p · reset {reset_text}",
                fg=PACE_COLORS.get(status, PACE_COLORS["on"]),
            )
            return
        detail.configure(
            text=(
                f"{prefix}{ccusage.format_number(window.get('used'))} / "
                f"{prefix}{ccusage.format_number(window.get('cap'))}   "
                f"reset {reset_text}"
            ),
            fg="#94a3b8",
        )

    @staticmethod
    def _clear_row(row: dict[str, tk.Widget], detail: str = "Not available") -> None:
        bar = row["bar"]
        assert isinstance(bar, GaugeBar)
        thin = "detail" not in row
        bar.set_usage(0, "Blue")
        row["percent"].configure(text="--%" if thin else "--.-%", fg="#64748b")
        detail_label = row.get("detail")
        if isinstance(detail_label, tk.Label):
            detail_label.configure(text=detail, fg="#64748b")


def _cursor_monthly_pace(window: dict[str, Any]) -> dict[str, Any] | None:
    pct = ccusage.percent(window.get("used"), window.get("cap"))
    return ccusage.monthly_pace(
        pct,
        window.get("periodStart"),
        window.get("periodEnd"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Command Code usage monitor window.")
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=1,
        help="Refresh interval in seconds. Default: 1",
    )
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    return args


if __name__ == "__main__":
    args = parse_args()
    app = UsageWindow(interval=args.interval)
    app.mainloop()
