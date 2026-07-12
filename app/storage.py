from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiosqlite
from cryptography.exceptions import InvalidTag

from app.crypto_box import SecretBox
from app.time_utils import DEFAULT_APP_TIMEZONE, app_today, format_app_datetime


class DuplicateActiveAccountBindingError(Exception):
    pass


class MaxActiveAccountLimitError(Exception):
    pass


@dataclass(frozen=True)
class MaxAccountRecord:
    id: int
    tg_user_id: int
    max_token: str
    max_device_id: str
    title: str
    is_active: bool


@dataclass(frozen=True)
class TgUserRecord:
    tg_user_id: int
    is_active: bool
    created_at: str
    terms_accepted_at: str | None
    activated_at: str | None
    accounts_count: int = 0
    supergroup_id: str | None = None  # НОВОЕ ПОЛЕ


@dataclass(frozen=True)
class DailyReportRow:
    day: str
    forward_dm: int
    forward_group: int
    forward_channel: int
    reply_dm: int
    reply_group: int


class Storage:
    def __init__(
        self,
        db_path: str,
        encryption_key: str,
        timezone_name: str = DEFAULT_APP_TIMEZONE,
    ):
        self._db_path = db_path
        self._timezone_name = timezone_name
        self._last_stats_cleanup_day: str | None = None
        self._box = SecretBox(encryption_key)

    def _local_timestamp(self) -> str:
        return format_app_datetime(timezone_name=self._timezone_name)

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_users (
                    tg_user_id INTEGER PRIMARY KEY,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    terms_accepted_at TEXT,
                    activated_at TEXT,
                    supergroup_id TEXT    -- НОВАЯ КОЛОНКА
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS max_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER NOT NULL,
                    max_token TEXT NOT NULL,
                    max_device_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_report_stats (
                    day TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    cnt INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, metric)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_report_stats_day ON daily_report_stats(day)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_mappings (
                    max_chat_id TEXT PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    topic_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_message_time TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_topic_mappings_topic_id ON topic_mappings(topic_id)"
            )

            # Убедимся, что колонка supergroup_id есть (для миграции старых БД)
            await self._ensure_column(db, "tg_users", "supergroup_id", "TEXT")
            await self._ensure_column(db, "tg_users", "terms_accepted_at", "TEXT")

            # Migrate legacy consents table into tg_users.terms_accepted_at when present.
            consent_exists_cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_user_consents' LIMIT 1"
            )
            consent_exists = await consent_exists_cur.fetchone()
            if consent_exists:
                await db.execute(
                    """
                    UPDATE tg_users
                    SET terms_accepted_at = COALESCE(
                        terms_accepted_at,
                        (SELECT c.accepted_at FROM tg_user_consents c WHERE c.tg_user_id = tg_users.tg_user_id)
                    )
                    """
                )
            await self._migrate_encrypt_account_secrets(db)
            await db.commit()

    async def _ensure_column(self, db: aiosqlite.Connection, table: str, column: str, column_sql: str) -> None:
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        cols = {str(row[1]) for row in rows}
        if column not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")

    async def has_terms_consent(self, tg_user_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT 1 FROM tg_users WHERE tg_user_id = ? AND terms_accepted_at IS NOT NULL LIMIT 1",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            return row is not None

    async def accept_terms(self, tg_user_id: int) -> None:
        ts = self._local_timestamp()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO tg_users (tg_user_id, is_active, created_at, terms_accepted_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    terms_accepted_at = COALESCE(tg_users.terms_accepted_at, ?)
                """,
                (tg_user_id, ts, ts, ts),
            )
            await db.commit()

    def _row_to_account(self, row: Any) -> MaxAccountRecord:
        raw_token = str(row["max_token"])
        raw_device_id = str(row["max_device_id"])
        try:
            max_token = self._box.decrypt(raw_token)
            max_device_id = self._box.decrypt(raw_device_id)
        except (InvalidTag, ValueError, TypeError) as exc:
            raise ValueError("Failed to decrypt account secrets. Check ENCRYPTION_KEY.") from exc
        return MaxAccountRecord(
            id=int(row["id"]),
            tg_user_id=int(row["tg_user_id"]),
            max_token=max_token,
            max_device_id=max_device_id,
            title=str(row["title"] or ""),
            is_active=bool(row["is_active"]),
        )

    @staticmethod
    def _row_to_user(row: Any) -> TgUserRecord:
        return TgUserRecord(
            tg_user_id=int(row["tg_user_id"]),
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
            terms_accepted_at=str(row["terms_accepted_at"]) if row["terms_accepted_at"] else None,
            activated_at=str(row["activated_at"]) if row["activated_at"] else None,
            accounts_count=int(row["accounts_count"]) if "accounts_count" in row.keys() else 0,
            supergroup_id=str(row["supergroup_id"]) if row.get("supergroup_id") else None,  # НОВОЕ
        )

    async def ensure_user(self, tg_user_id: int) -> TgUserRecord:
        ts = self._local_timestamp()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO tg_users (tg_user_id, is_active, created_at, terms_accepted_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(tg_user_id) DO NOTHING
                """,
                (tg_user_id, ts, ts),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM tg_users WHERE tg_user_id = ?",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            return self._row_to_user(row)

    async def get_user(self, tg_user_id: int) -> TgUserRecord | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tg_users WHERE tg_user_id = ?",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            return self._row_to_user(row) if row else None

    async def set_user_supergroup(self, tg_user_id: int, supergroup_id: str | None) -> None:
        """Установить supergroup_id для пользователя."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tg_users SET supergroup_id = ? WHERE tg_user_id = ?",
                (supergroup_id, tg_user_id)
            )
            await db.commit()

    async def get_user_supergroup(self, tg_user_id: int) -> str | None:
        """Получить supergroup_id пользователя."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT supergroup_id FROM tg_users WHERE tg_user_id = ?",
                (tg_user_id,)
            )
            row = await cur.fetchone()
            return str(row["supergroup_id"]) if row and row["supergroup_id"] else None

    # Остальные методы без изменений (activate_user, deactivate_user, list_users_page, add_account, ...)
    # Я их не переписываю, чтобы не раздувать ответ, они остаются как были.
    # Убедитесь, что в вашем файле они есть. В этом ответе я привожу только изменения.
