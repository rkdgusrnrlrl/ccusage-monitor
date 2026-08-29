# ccusage-monitor contributor guide

## Product scope

- This is a compact, always-on-top Windows GUI for Codex and CommandCode usage.
- Keep the UI horizontally arranged: Codex, CommandCode account 1, CommandCode account 2.
- Each provider shows its short and weekly windows as `5h` and `7d` rows.
- Keep warning colors consistent: below 80% blue, 80-94% orange, 95% or more red.
- The native Windows title bar is intentionally hidden. Preserve the custom draggable title bar and the in-app `X` close button.

## Credentials and configuration

- Never commit API keys, Codex tokens, `config.json`, local auth files, logs, or screenshots containing sensitive values.
- `config.json` is the preferred local configuration. It is searched beside the executable first, then in its parent project directory. `config.example.json` is the tracked template.
- `config.json` supports one or two entries in `commandcode_accounts`; each entry needs `id` and `api_key`.
- `id` is a display label only. It is rendered as `CommandCode(<id>)`; do not treat it as authentication data.
- If no `config.json` exists, preserve environment-variable fallback support:
  - `COMMANDCODE_API_KEY` or `COMMAND_CODE_API_KEY` for the legacy single account.
  - `COMMANDCODE_API_KEY_PERSONAL` / `COMMANDCODE_USER_ID_PERSONAL`.
  - `COMMANDCODE_API_KEY_WORK` / `COMMANDCODE_USER_ID_WORK`.
- `ccusage.py` may read the current local CommandCode `userId` only as a fallback display label. Do not print it in diagnostics.

## Codex usage integration

- Codex usage is read through the locally installed, signed-in Codex CLI app-server. Do not copy, parse, log, or persist its authentication token.
- Keep one `CodexRateLimitClient` app-server process alive across refreshes. Restart only after an actual request/process failure.
- On Windows, terminate the whole process tree only during failure recovery or normal app shutdown. Do not return to per-refresh `codex` spawning.
- All helper processes, including `codex app-server` and `taskkill`, must use the hidden Windows subprocess options so no console window flashes.
- Codex backend 503/timeouts are expected transient failures. Preserve the distinction between a provider failure and a UI failure, and keep error details in `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`.

## Build and verification

- The only supported distribution filename is `dist\ccusage-monitor.exe`.
- Build with:

  ```powershell
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name ccusage-monitor .\ccusage_window.pyw
  ```

- Before rebuilding, verify that `ccusage-monitor.exe` is not running; an active process locks the output file.
- `dist/` and `build/` are intentionally ignored by Git. Do not add executable artifacts or one-off build specs to Git.
- Run at least a syntax compile check after Python changes:

  ```powershell
  python -m py_compile .\ccusage.py .\ccusage_window.pyw
  ```

- When testing account parsing, use placeholders or mocks only. Never print real API keys or auth-file contents.

## Git workflow

- Work on a feature/fix branch, not `main`.
- Commit only task-related source, documentation, and safe templates.
- Push the branch and update/create a PR. Do not merge a PR without explicit user approval.
