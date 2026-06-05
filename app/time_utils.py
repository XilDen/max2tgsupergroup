from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_APP_TIMEZONE = "Europe/Moscow"


def normalize_timezone_name(timezone_name: str | None) -> str:
    value = (timezone_name or DEFAULT_APP_TIMEZONE).strip()
    return value or DEFAULT_APP_TIMEZONE


def load_timezone(timezone_name: str | None = None) -> tzinfo:
    name = normalize_timezone_name(timezone_name)
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name == DEFAULT_APP_TIMEZONE:
            return timezone(timedelta(hours=3), DEFAULT_APP_TIMEZONE)
        raise ValueError(f"Unknown APP_TIMEZONE: {name}") from exc


def app_now(timezone_name: str | None = None) -> datetime:
    return datetime.now(load_timezone(timezone_name))


def app_today(timezone_name: str | None = None) -> date:
    return app_now(timezone_name).date()


def format_app_datetime(dt: datetime | None = None, timezone_name: str | None = None) -> str:
    tz = load_timezone(timezone_name)
    value = dt or datetime.now(tz)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).isoformat(sep=" ", timespec="seconds")


def seconds_until_next_app_day(timezone_name: str | None = None) -> int:
    tz = load_timezone(timezone_name)
    now = datetime.now(tz)
    next_day = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=tz)
    return max(1, int((next_day - now).total_seconds()))
