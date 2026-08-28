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
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any

import ccusage


WINDOW_WIDTH = 430
WINDOW_HEIGHT = 170
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


class UsageWindow(tk.Tk):
    """Compact usage dashboard that keeps the UI responsive while polling."""

    def __init__(self, interval: int) -> None:
        super().__init__()
        self.title("Command Code Usage")
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
        self._drag_x = 0
        self._drag_y = 0

        self._configure_style()
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after_id = self.after(100, self.refresh)

    def _configure_style(self) -> None:
        self.configure(bg="#111827")
        style = ttk.Style(self)
        style.theme_use("clam")
        for name, color in (
            ("Blue", "#38bdf8"),
            ("Orange", "#f59e0b"),
            ("Red", "#ef4444"),
        ):
            style.configure(
                f"Usage.{name}.Horizontal.TProgressbar",
                troughcolor="#273449",
                background=color,
                bordercolor="#273449",
                lightcolor=color,
                darkcolor=color,
                thickness=8,
            )

    def _build_widgets(self) -> None:
        titlebar = tk.Frame(self, bg="#1f2937", height=26)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        titlebar.bind("<ButtonPress-1>", self._start_move)
        titlebar.bind("<B1-Motion>", self._move_window)

        title = tk.Label(
            titlebar,
            text="Command Code Usage",
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
        columns.pack(fill="both", expand=True, padx=12)
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)

        codex_column = tk.Frame(columns, bg="#111827")
        codex_column.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.codex_title = self._add_column_title(codex_column, "Codex")
        commandcode_column = tk.Frame(columns, bg="#111827")
        commandcode_column.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.commandcode_title = self._add_column_title(
            commandcode_column, "CommandCode"
        )

        self.rows: dict[str, dict[str, tk.Widget]] = {}
        self._add_usage_row(codex_column, "codexPrimary", "5h")
        self._add_usage_row(codex_column, "codexWeekly", "7d")
        self._add_usage_row(commandcode_column, "fiveHour", "5h")
        self._add_usage_row(commandcode_column, "weekly", "7d")

    def _start_move(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _move_window(self, event: tk.Event) -> None:
        self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _add_column_title(self, parent: tk.Widget, text: str) -> tk.Label:
        title = tk.Label(
            parent,
            text=text,
            bg="#111827",
            fg="#e2e8f0",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        title.pack(fill="x", pady=(0, 2))
        return title

    def _add_usage_row(self, parent: tk.Widget, key: str, label: str) -> None:
        row = tk.Frame(parent, bg="#111827")
        row.pack(fill="x", pady=1)

        name = tk.Label(
            row,
            text=label,
            width=3,
            bg="#111827",
            fg="#cbd5e1",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        name.grid(row=0, column=0, rowspan=2, sticky="w")

        bar = ttk.Progressbar(
            row,
            style="Usage.Blue.Horizontal.TProgressbar",
            maximum=100,
            length=80,
        )
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
        commandcode_data: dict[str, Any] | None = None
        codex_data: dict[str, Any] | None = None
        errors: list[str] = []

        try:
            api_key = ccusage.get_api_key()
            commandcode_data = ccusage.api_get(ccusage.CREDITS_ENDPOINT, api_key)
        except Exception as exc:  # The message is shown in the small status area.
            errors.append(f"CommandCode: {exc}")

        try:
            codex_data = self.codex_client.read_rate_limits()
        except Exception as exc:  # The message is shown in the small status area.
            errors.append(f"Codex: {exc}")

        self.result_queue.put(("data", (commandcode_data, codex_data, errors)))

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
            commandcode_data, codex_data, errors = value
            self._update_usage(commandcode_data, codex_data, errors)
        else:
            self.status.configure(
                text=f"Updated: failed - {value}",
                fg="#fca5a5",
            )

    def _update_usage(
        self,
        data: dict[str, Any] | None,
        codex_data: dict[str, Any] | None,
        errors: list[str],
    ) -> None:
        if data is None:
            self.header.configure(text="Usage unavailable")
        else:
            self._update_commandcode_usage(data)

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

    def _update_commandcode_usage(self, data: dict[str, Any]) -> None:
        credits = data.get("credits") or {}
        limits = data.get("windowLimits") or {}
        plan = ccusage.get_plan_name(credits.get("planId"))
        self.commandcode_title.configure(text=f"CommandCode {plan}")

        for key in ("fiveHour", "weekly"):
            window = limits.get(key)
            if isinstance(window, dict):
                self._update_row(self.rows[key], window)
            else:
                self._clear_row(self.rows[key])

    @staticmethod
    def _update_row(row: dict[str, tk.Widget], window: dict[str, Any]) -> None:
        pct = ccusage.percent(window.get("used"), window.get("cap"))
        bar = row["bar"]
        percent = row["percent"]
        detail = row["detail"]
        assert isinstance(bar, ttk.Progressbar)
        assert isinstance(percent, tk.Label)
        assert isinstance(detail, tk.Label)

        bar.configure(value=max(0, min(100, pct)))
        color = "Red" if pct >= 95 else "Orange" if pct >= 80 else "Blue"
        bar.configure(style=f"Usage.{color}.Horizontal.TProgressbar")
        percent.configure(text=f"{pct:.1f}%", fg="#fca5a5" if pct >= 90 else "#f8fafc")
        detail.configure(
            text=(
                f"{ccusage.format_number(window.get('used'))} / "
                f"{ccusage.format_number(window.get('cap'))}   "
                f"reset {ccusage.format_reset(window.get('resetAt')).split(' (', 1)[0]}"
            ),
            fg="#94a3b8",
        )

    @staticmethod
    def _clear_row(row: dict[str, tk.Widget]) -> None:
        bar = row["bar"]
        assert isinstance(bar, ttk.Progressbar)
        bar.configure(value=0, style="Usage.Blue.Horizontal.TProgressbar")
        row["percent"].configure(text="--.-%", fg="#64748b")
        row["detail"].configure(text="Not available", fg="#64748b")


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
