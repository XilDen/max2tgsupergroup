# AGENTS.md

Guidance for Codex, Cursor, Claude Code and other AI/vibecoding agents working on this repo.

## Project Snapshot

`max2tg` is an unofficial Python 3.12 service that forwards messages from Max (web.max.ru) to Telegram and can optionally send Telegram replies back to Max. It supports multiple Max accounts, SQLite storage, optional Redis-backed Telegram send queue, admin commands, daily reports, log rotation and optional DB backups.

Runtime flow:

```text
Max WebSocket -> MaxClient -> max_listener -> QueuedTelegramSender -> Telegram Bot
                                                ^
Telegram commands/replies -> tg_commands -> AccountManager -> MaxClient
```

## Prime Directives

1. Protect secrets and personal data.
2. Keep logs useful but anonymized.
3. Preserve multi-account isolation.
4. Do not block message forwarding on best-effort metadata lookup.
5. Keep calendar/report times on the configured app timezone.

## Privacy And Logging Rules

- Never log full names, chat titles, group names, channel names, message text, file URLs, tokens, device IDs or raw Max payloads.
- Use `app.privacy.mask_text()` / `mask_mapping_values()` when a human-readable label must appear in logs. Current policy: keep the first 2 characters only, then mask the rest.
- No raw attachment dicts in logs. Log attachment type and key names only.
- No raw download URLs in logs. Log status/bytes/account only.
- No Telegram payload logging. `telegram` and `telegram.ext` are intentionally kept at `WARNING`.
- Repetitive WebSocket noise belongs in `DEBUG`: heartbeat, `<<< EVENT`, contact/chat lookup, resolved contacts, attachment processing, downloaded file.
- Important lifecycle events can stay `INFO`: startup, account runtime start, Max connection/auth, reconnect, queue backend, forwarded message summary.
- Keep transient Telegram polling network tracebacks suppressed via `maintenance._TransientTelegramPollingFilter`; do not suppress real handler errors.
- Media/message payloads must not be persisted to disk. Keep relay bytes in memory/Redis only for delivery and TTL.
- Enforce hard media download caps: 5MB for images/stickers/previews, 20MB for videos/documents/audio. Oversized user-facing text is `[вырезано так как файл слишком большой]`.

## Timezone Rules

- `APP_TIMEZONE` is the global display/report timezone. Default is `Europe/Moscow`.
- Use `app.time_utils` for app-visible dates/times:
  - `format_app_datetime()`
  - `app_now()`
  - `app_today()`
  - `seconds_until_next_app_day()`
- Do not use `datetime.utcnow()` or bare `date.today()` for logs, reports, backup names, admin-visible timestamps or daily limits.
- Durations/TTL/reconnect intervals can continue using monotonic time or epoch seconds when they are not user-visible calendar boundaries.

## Max Integration Rules

- `MaxClient` is a best-effort WebSocket client for the reverse-engineered Max web protocol.
- `validate_max_credentials()` returns:
  - `True` when credentials are accepted.
  - `False` when Max explicitly rejects credentials.
  - `None` when validation is temporarily unavailable due to network/WS/timeout issues.
- Contact/chat metadata fetches (`CONTACT_GET`, `CHAT_GET`) are best-effort. Timeouts should not fail message forwarding.
- WebSocket `DISPATCH` handler tasks must be observed. Keep `MaxClient._log_message_task_result()` or equivalent so bugs do not become `Task exception was never retrieved`.
- Include `account_id` in Max logs where possible. Multiple Max runtimes run at once and sequence numbers overlap.
- Channels generally cannot be replied to. If `resolver.is_channel(chat_id)` is true, do not render the Telegram reply button.

## Telegram Sending Rules

- `AccountManager` passes a `QueuedTelegramSender` into Max listeners in normal runtime.
- Keep `QueuedTelegramSender` API compatible with `TelegramSender` for methods used by listeners:
  - `send`
  - `send_photo`
  - `send_document`
  - `send_video`
  - `send_voice`
  - `send_sticker`
  - `send_media_group`
- If a direct sender method returns a meaningful bool, the queued sender should mirror the caller contract as closely as possible. For queued sends, returning `True` means "accepted into queue", not "Telegram delivery confirmed".
- Queue jobs enforce tenant isolation: `tenant_tg_user_id` must match `chat_id`.
- Redis queue payloads use a JSON schema with base64 for bytes. Do not reintroduce pickle or other executable deserialization.
- Redis queue storage is intended to be non-persistent. Docker Compose disables snapshots/AOF and does not mount a Redis data volume.

## Storage And Reports

- SQLite is managed by `app.storage.Storage`.
- Account secrets are encrypted through `SecretBox`; never store plaintext tokens/device IDs.
- Daily metrics are calendar-day metrics in `APP_TIMEZONE`.
- `/reports` is admin-facing and must keep using local app dates.
- Existing DB defaults may contain `CURRENT_TIMESTAMP` for legacy fallback, but new code should pass timezone-aware timestamps explicitly.
- Weekly DB backups are disabled by default (`DB_BACKUP_ENABLED=false`). Do not enable persistent backup copies unless the user explicitly accepts that encrypted account data will be copied to disk.
- Runtime cache cleanup removes Python/test cache directories and stale `data/backups` when backups are disabled.

## Command And Admin UX Rules

- `/register` and `/bind` validate Max credentials before creating bindings.
- When validation returns `None`, tell the user validation is temporarily unavailable; do not claim credentials are invalid.
- Admin-visible event messages should include `APP_TIMEZONE` timestamps when time matters.
- Keep user-facing Telegram text concise and in Russian unless the surrounding command is clearly English.

## Common Commands

Use these from repo root:

```powershell
python -m compileall app
git diff --check
```

When dependencies from `requirements.txt` are needed locally on Windows, prefer:

```powershell
.\.venv\Scripts\python.exe -m compileall app
```

There is no dedicated test suite in the repo right now. For risky changes, add a small smoke test with `python -c` or a temporary local script and mention exactly what was verified.

## Editing Discipline

- Make narrow changes. This repo is small; avoid framework-style abstractions unless they remove real risk.
- Prefer existing module boundaries over new global utilities, except for cross-cutting concerns like privacy/timezone.
- Do not rewrite README or docs wholesale when a focused update is enough.
- Be careful in dirty worktrees. Preserve user changes in files like `app/max_listener.py` and `app/tg_sender.py`.
- Use `apply_patch` for manual edits.

## High-Risk Areas

- Max protocol assumptions in `max_client.py`, `resolver.py`, `max_listener.py`.
- Queue method parity between `TelegramSender` and `QueuedTelegramSender`.
- Privacy regressions in logs.
- Calendar-day boundaries around midnight Moscow time.
- Multi-account runtime cleanup and reconnect loops.
