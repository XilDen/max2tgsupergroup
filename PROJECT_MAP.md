# PROJECT_MAP.md

Fast orientation map for `max2tg`.

## What This Bot Does

The service forwards incoming messages from one or more Max accounts to Telegram private chats. Telegram users can register Max credentials, admins can manage users and bindings, and optional replies from Telegram can be sent back into Max.

Core paths:

```text
Incoming Max message
  app.max_client.MaxClient
  -> app.max_listener.create_max_client handlers
  -> app.message_queue.QueuedTelegramSender
  -> app.tg_sender.TelegramSender
  -> Telegram

Telegram command/reply
  app.tg_handler.build_tg_app
  -> app.tg_commands handlers
  -> app.account_manager.AccountManager
  -> app.storage.Storage / app.max_client.MaxClient
```

## File Map

### Entry And Configuration

- `app/main.py`
  - Runtime entry point.
  - Loads settings, configures logging, initializes storage, starts Telegram transport/queue, starts Max account runtimes, starts polling and backup/health loops.
  - Legacy bootstrap from `MAX_TOKEN` + `MAX_DEVICE_ID` + `TG_CHAT_ID` lives here.

- `app/config.py`
  - Reads `.env` through `python-dotenv`.
  - Required env: `TG_BOT_TOKEN`, `TG_ADMIN_ID`, `ENCRYPTION_KEY`.
  - Important optional env: `APP_TIMEZONE`, `DB_PATH`, `REDIS_URL`, `REDIS_KEY_PREFIX`, queue settings, `DEBUG`, `REPLY_ENABLED`.

- `app/time_utils.py`
  - Global app timezone helpers.
  - Default timezone: `Europe/Moscow`.
  - Use this for reports, logs, backup names, admin-visible timestamps and local-day windows.

### Max Side

- `app/max_client.py`
  - Low-level Max WebSocket client and protocol opcodes.
  - Handles handshake/auth, reconnects, heartbeats, RPC commands, file downloads and message parsing.
  - `validate_max_credentials()` returns `True`, `False`, or `None`.
  - `account_id` is included in logs because multiple clients run concurrently.
  - Dispatch handler tasks are observed to avoid unhandled `asyncio` task exceptions.

- `app/max_listener.py`
  - Converts parsed Max messages into Telegram messages/media.
  - Formats headers, handles attachments, albums, forwards/replies, media fallback.
  - Updates daily report metrics via callback.
  - Builds reply keyboards only when replies are enabled and the source chat is not a channel.
  - Must not log raw names/text/URLs/payloads.

- `app/resolver.py`
  - Caches and resolves Max user/chat names and chat types.
  - Loads metadata from auth snapshots and best-effort RPC calls.
  - `is_dm()` and `is_channel()` drive message formatting and reply-button behavior.
  - Contact/chat fetch failures should not block forwarding.

### Telegram Side

- `app/tg_sender.py`
  - Direct Telegram Bot API wrapper.
  - Splits long text/captions, retries rate limits/timeouts, sends media and media groups.
  - `send_video()` returns bool so callers can decide whether to fallback.

- `app/message_queue.py`
  - Async queued wrapper around `TelegramSender`.
  - Local memory queue by default, optional Redis backend.
  - Preserves tenant isolation by checking queued `chat_id` against `tenant_tg_user_id`.
  - Must mirror direct sender methods used by `max_listener`, including `send_media_group()`.

- `app/tg_handler.py`
  - Builds the python-telegram-bot `Application`.
  - Stores shared objects in `bot_data`: `account_manager`, `admin_id`, `app_timezone`, Redis prefix/cooldowns.
  - Registers handlers through `tg_commands.register_handlers()`.

- `app/tg_commands.py`
  - Telegram command and callback handlers.
  - User commands: `/start`, `/help`, `/register`, `/accounts`, `/remove`, `/askme`, `/cancel`.
  - Admin commands: `/bind`, `/activate`, `/deactivate`, `/users`, `/reports`.
  - Terms acceptance, user/admin guards, cooldowns, daily mutation limits and reply routing live here.
  - Uses app timezone for daily windows and admin-visible timestamps.

### Accounts, Storage, Security

- `app/account_manager.py`
  - Owns active Max runtimes keyed by account id.
  - Starts/stops clients, validates credentials, enforces duplicate/limit rules, sends replies back to Max.
  - Passes `QueuedTelegramSender` into Max listeners.

- `app/storage.py`
  - SQLite persistence.
  - Tables: `tg_users`, `max_accounts`, `daily_report_stats`.
  - Encrypts Max token/device id using `SecretBox`.
  - Daily reports and cleanup use app-local dates.

- `app/crypto_box.py`
  - AES-GCM encryption wrapper for secrets.
  - Derives key from `ENCRYPTION_KEY` plus built-in pepper.
  - Includes legacy decrypt fallback.

- `app/privacy.py`
  - Log masking helpers.
  - Current policy: keep first 2 characters and replace the rest with an ellipsis marker.

### Operations

- `app/maintenance.py`
  - Logging setup, log rotation, app timezone formatter.
  - Splits app logs and DB logs with filters.
  - Suppresses transient Telegram polling network tracebacks.
  - Weekly SQLite backup loop.

- `app/health_monitor.py`
  - Captures app-level ERROR records.
  - Sends admin alert only for systematic/non-transient errors over a 24h window.

- `app/cooldown_store.py`
  - In-memory async-ish Redis-compatible store for cooldown/limit fallback.

### Deployment And Docs

- `.env.example`
  - Template environment config.

- `requirements.txt`
  - Python dependencies. Includes `tzdata` for IANA timezone support in slim/Windows environments.

- `Dockerfile`
  - Minimal Python image for the app.

- `docker-compose.yml`
  - Runs app + Redis. Mounts `./data` to persist SQLite.

- `README.md`
  - Human setup and usage instructions in Russian and English.

- `AGENTS.md`
  - Rules for AI/vibecoding agents.

## Data Flow Details

### Incoming Max Message

1. `MaxClient.run()` connects and authenticates to Max WS.
2. `MaxClient._handle()` receives `DISPATCH`, parses it into `MaxMessage`.
3. `max_listener.handle_message()` ignores self messages.
4. Resolver updates chat metadata, resolves sender name best-effort.
5. Listener decides metric: `forward_dm`, `forward_group`, or `forward_channel`.
6. Listener builds Telegram header and optional reply keyboard.
7. Attachments are sent as text/media/document/media group through `QueuedTelegramSender`.

### Telegram Reply To Max

1. User taps inline reply button.
2. `tg_commands._on_reply_button()` stores pending account/chat in `context.user_data`.
3. Next text message goes through `_on_text_reply()`.
4. `AccountManager.send_message()` validates ownership and active runtime.
5. `MaxClient.send_message()` sends opcode `SEND_MESSAGE`.
6. On success, storage increments `reply_dm` or `reply_group`.

### Register/Bind Account

1. `/register` or `/bind` parses `device_id`, `token`, title.
2. Commands validate field shape and rate limits.
3. `AccountManager.validate_credentials()` calls Max validation.
4. `True`: account is stored encrypted and runtime starts.
5. `False`: user is told credentials were rejected.
6. `None`: user is told validation is temporarily unavailable.

## Log Level Guide

Use `INFO` for:

- service startup/shutdown
- selected timezone
- Telegram queue backend
- Max connect/auth/reconnect
- runtime start/stop summaries
- successful high-level forwarding summary
- backup success/failure

Use `DEBUG` for:

- heartbeat
- raw WS event summaries
- contact/chat lookup
- resolved contact names, masked only
- known resolver caches, masked only
- attachment processing details
- file download success

Use `WARNING` for:

- recoverable external failures
- Max API denied sends
- download failures
- Redis fallback
- auth failures

Use `ERROR`/`exception` for:

- dropped queue jobs after retries
- unexpected message handler failures
- DB/report write failures
- backup failures

## Important Invariants

- Do not expose PII in logs.
- Do not expose tokens/device IDs.
- Do not add reply buttons for channels.
- Do not let resolver lookup failures stop message forwarding.
- Keep queue API parity with direct Telegram sender.
- Keep daily reports and admin timestamps in `APP_TIMEZONE`.
- Preserve encrypted storage for Max credentials.
- Keep Telegram queue tenant isolation check.

## Quick Verification Checklist

```powershell
python -m compileall app
git diff --check
```

If dependency imports are needed on Windows:

```powershell
.\.venv\Scripts\python.exe -m compileall app
```

Targeted smoke ideas:

- Instantiate `QueuedTelegramSender` with a fake sender and verify `send_media_group()` enqueues and dispatches.
- Check `mask_text("Анна Чернобурова")` returns only the first two characters plus mask.
- Check `format_app_datetime(timezone_name="Europe/Moscow")` includes `+03:00`.
- Check `/register` handling distinguishes `False` credentials from `None` validation outage.
