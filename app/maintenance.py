from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from shutil import copy2

from app.time_utils import DEFAULT_APP_TIMEZONE, app_now, load_timezone

log = logging.getLogger(__name__)

LOG_DIR = "logs"
APP_LOG_FILE = "app.log"
DB_LOG_FILE = "db.log"
LOG_MAX_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 3

BACKUP_INTERVAL_SEC = 7 * 24 * 60 * 60
BACKUP_KEEP_COUNT = 4


class _DbOnlyFilter(logging.Filter):
    DB_PREFIXES = ("app.storage", "app.maintenance", "aiosqlite", "sqlite3")

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.DB_PREFIXES)


class _AppOnlyFilter(logging.Filter):
    DB_PREFIXES = ("app.storage", "app.maintenance", "aiosqlite", "sqlite3")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self.DB_PREFIXES)


class _TransientTelegramPollingFilter(logging.Filter):
    _POLLING_MARKERS = (
        "polling for updates",
        "get_updates",
    )
    _TRANSIENT_MARKERS = (
        "NetworkError",
        "TimedOut",
        "ReadError",
        "ReadTimeout",
        "ConnectError",
        "ConnectTimeout",
        "RemoteProtocolError",
        "PoolTimeout",
        "httpx.",
        "httpcore.",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("telegram.ext"):
            return True
        message = record.getMessage().lower()
        if not any(marker in message for marker in self._POLLING_MARKERS):
            return True
        exc_text = self._exception_chain_text(record.exc_info[1] if record.exc_info else None)
        if not exc_text:
            return True
        return not any(marker in exc_text for marker in self._TRANSIENT_MARKERS)

    def _exception_chain_text(self, exc: BaseException | None) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            parts.append(f"{exc.__class__.__module__}.{exc.__class__.__name__}: {exc}")
            exc = exc.__cause__ or exc.__context__
        return " | ".join(parts)


class AppTimezoneFormatter(logging.Formatter):
    def __init__(self, fmt: str, timezone_name: str):
        super().__init__(fmt)
        self._tz = load_timezone(timezone_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(sep=" ", timespec="seconds")


def configure_logging(debug: bool, timezone_name: str = DEFAULT_APP_TIMEZONE) -> None:
    level = logging.DEBUG if debug else logging.INFO
    os.makedirs(LOG_DIR, exist_ok=True)
    app_log_path = os.path.join(LOG_DIR, APP_LOG_FILE)
    db_log_path = os.path.join(LOG_DIR, DB_LOG_FILE)

    formatter = AppTimezoneFormatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        timezone_name=timezone_name,
    )

    app_file_handler = RotatingFileHandler(
        app_log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_file_handler.setLevel(level)
    app_file_handler.setFormatter(formatter)
    app_file_handler.addFilter(_AppOnlyFilter())
    app_file_handler.addFilter(_TransientTelegramPollingFilter())

    db_file_handler = RotatingFileHandler(
        db_log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    db_file_handler.setLevel(level)
    db_file_handler.setFormatter(formatter)
    db_file_handler.addFilter(_DbOnlyFilter())
    db_file_handler.addFilter(_TransientTelegramPollingFilter())

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_TransientTelegramPollingFilter())

    logging.basicConfig(
        level=level,
        handlers=[stream_handler, app_file_handler, db_file_handler],
        force=True,
    )

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Never log Telegram request payloads (can include message text/captions), even in DEBUG mode.
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)


def _backup_db_sync(db_path: str, backup_path: str) -> None:
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error:
        # Fallback copy if sqlite backup API fails
        copy2(db_path, backup_path)


async def backup_db_once(
    db_path: str,
    backups_dir: str = "data/backups",
    timezone_name: str = DEFAULT_APP_TIMEZONE,
) -> str | None:
    if not os.path.exists(db_path):
        return None

    ts = app_now(timezone_name).strftime("%Y%m%d_%H%M%S")
    backup_name = f"max2tg_{ts}.sqlite3"
    backup_path = os.path.join(backups_dir, backup_name)
    await asyncio.to_thread(_backup_db_sync, db_path, backup_path)

    files = sorted(Path(backups_dir).glob("max2tg_*.sqlite3"))
    excess = max(0, len(files) - BACKUP_KEEP_COUNT)
    for file_path in files[:excess]:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            log.exception("Failed to remove old backup: %s", file_path)

    return backup_path


async def weekly_backup_loop(
    db_path: str,
    stop_event: asyncio.Event,
    timezone_name: str = DEFAULT_APP_TIMEZONE,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=BACKUP_INTERVAL_SEC)
            if stop_event.is_set():
                break
        except asyncio.TimeoutError:
            pass

        try:
            backup_path = await backup_db_once(db_path, timezone_name=timezone_name)
            if backup_path:
                log.info("Weekly DB backup created: %s", backup_path)
        except Exception:
            log.exception("Weekly DB backup failed")
