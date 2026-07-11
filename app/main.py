import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import redis.asyncio as redis

from app.account_manager import AccountManager
from app.config import load_settings
from app.cooldown_store import MemoryCooldownStore
from app.health_monitor import AppLogHealthMonitor
from app.maintenance import configure_logging, runtime_cache_cleanup_loop, weekly_backup_loop
from app.message_queue import QueuedTelegramSender
from app.storage import Storage
from app.tg_handler import build_tg_app
from app.tg_sender import TelegramSender
from app.time_utils import format_app_datetime

threading.stack_size(524288)

log = logging.getLogger("max2tg")


async def _bootstrap_legacy_account(settings, storage: Storage, manager: AccountManager) -> None:
    max_token = os.environ.get("MAX_TOKEN", "").strip()
    max_device_id = os.environ.get("MAX_DEVICE_ID", "").strip()
    if not (max_token and max_device_id):
        return

    # Определяем tg_user_id: сначала из TG_CHAT_ID (если есть), иначе из TG_ADMIN_ID
    tg_user_id = None
    if settings.tg_chat_id:
        try:
            tg_user_id = int(settings.tg_chat_id)
        except ValueError:
            pass
    if tg_user_id is None:
        tg_user_id = settings.tg_admin_id

    if not tg_user_id:
        log.warning("Legacy bootstrap skipped: no target user ID")
        return

    existing = await storage.list_accounts_for_user(tg_user_id)
    if existing:
        log.info("Legacy bootstrap skipped: user already has accounts")
        return

    # Проверяем, приняты ли условия для этого пользователя
    if not await storage.has_terms_consent(tg_user_id):
        # Если это администратор, автоматически принимаем условия и активируем
        if tg_user_id == settings.tg_admin_id:
            await storage.accept_terms(tg_user_id)
            await manager.activate_user(tg_user_id)  # <-- АКТИВАЦИЯ ДОБАВЛЕНА
            log.info("Automatically accepted terms and activated admin %s", tg_user_id)
        else:
            log.warning(
                "Legacy bootstrap skipped: user %s has not accepted terms yet. "
                "Please open bot in private chat and press 'Принимаю', then restart.",
                tg_user_id
            )
            return
    else:
        # Условия уже приняты, но возможно пользователь не активирован – активируем, если это админ
        if tg_user_id == settings.tg_admin_id:
            user = await storage.get_user(tg_user_id)
            if user and not user.is_active:
                await manager.activate_user(tg_user_id)
                log.info("Activated admin %s", tg_user_id)

    try:
        await manager.add_account(
            tg_user_id=tg_user_id,
            max_token=max_token,
            max_device_id=max_device_id,
            title="legacy-env",
        )
        log.info("Legacy account from .env has been registered automatically for user %s", tg_user_id)
    except Exception as e:
        log.exception("Legacy account bootstrap failed: %s", e)

async def main():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))

    settings = load_settings()

    configure_logging(settings.debug, timezone_name=settings.app_timezone)

    log.info("Debug mode: %s", "ON" if settings.debug else "OFF")
    log.info("Application timezone: %s", settings.app_timezone)

    storage = Storage(
        settings.db_path,
        encryption_key=settings.encryption_key,
        timezone_name=settings.app_timezone,
    )
    await storage.init()
    await storage.cleanup_daily_metrics_if_needed(keep_days=180)

    tg_transport = TelegramSender(settings.tg_bot_token)
    await tg_transport.start()
    health_monitor = AppLogHealthMonitor(
        bot=tg_transport.bot,
        admin_id=settings.tg_admin_id,
        timezone_name=settings.app_timezone,
    )
    health_monitor.install()
    health_stop_event = asyncio.Event()
    health_task = asyncio.create_task(
        health_monitor.daily_check_loop(health_stop_event),
        name="daily-app-log-health-check",
    )

    sender = QueuedTelegramSender(
        sender=tg_transport,
        redis_url=settings.redis_url,
        redis_key_prefix=settings.redis_key_prefix,
        workers=settings.tg_queue_workers,
        min_send_interval_ms=settings.tg_min_send_interval_ms,
        max_attempts=settings.tg_queue_max_attempts,
        job_ttl_sec=settings.tg_queue_job_ttl_sec,
    )
    await sender.start()

    manager = AccountManager(
    storage=storage,
    sender=sender,
    settings=settings,      
    debug=settings.debug,
    reply_enabled=settings.reply_enabled,
    )

    await _bootstrap_legacy_account(settings, storage, manager)
    await manager.start_all()

    # ===== ИЗМЕНЕНИЕ ЗДЕСЬ =====
    tg_app = build_tg_app(
        token=settings.tg_bot_token,
        account_manager=manager,
        admin_id=settings.tg_admin_id,
        storage=storage,          # передаём storage
        settings=settings,        # передаём settings
        app_timezone=settings.app_timezone,
    )
    # ===========================

    tg_app.bot_data["redis_key_prefix"] = settings.redis_key_prefix
    askme_redis = None
    cooldown_store = MemoryCooldownStore()
    if settings.redis_url:
        try:
            askme_redis = redis.from_url(settings.redis_url, decode_responses=True)
            await askme_redis.ping()
            cooldown_store = askme_redis
            log.info("Askme cooldown backend: redis (%s)", settings.redis_url)
        except Exception:
            log.exception(
                "Redis unavailable at %s, fallback to in-memory cooldown store. "
                "To enable Redis manually, start Redis and verify REDIS_URL.",
                settings.redis_url,
            )
            askme_redis = None
    tg_app.bot_data["askme_cooldown"] = cooldown_store
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram polling started")
    try:
        await tg_transport.bot.send_message(
            chat_id=settings.tg_admin_id,
            text=(
                "🤫 я запустилась\n"
                f"Время: {format_app_datetime(timezone_name=settings.app_timezone)} ({settings.app_timezone})"
            ),
        )
    except Exception:
        log.exception("Failed to send startup notification to admin_id=%s", settings.tg_admin_id)

    backup_stop_event = asyncio.Event()
    backup_task = None
    if settings.db_backup_enabled:
        backup_task = asyncio.create_task(
            weekly_backup_loop(
                settings.db_path,
                backup_stop_event,
                timezone_name=settings.app_timezone,
            ),
            name="weekly-db-backup",
        )
    else:
        log.info("Weekly DB backup disabled")

    cache_stop_event = asyncio.Event()
    cache_cleanup_task = asyncio.create_task(
        runtime_cache_cleanup_loop(
            cache_stop_event,
            root_dir=".",
            remove_backups=not settings.db_backup_enabled,
        ),
        name="runtime-cache-cleanup",
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        log.info("Shutting down...")
        health_stop_event.set()
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)
        health_monitor.uninstall()
        backup_stop_event.set()
        if backup_task:
            backup_task.cancel()
            await asyncio.gather(backup_task, return_exceptions=True)
        cache_stop_event.set()
        cache_cleanup_task.cancel()
        await asyncio.gather(cache_cleanup_task, return_exceptions=True)
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await manager.stop_all()
        await sender.stop()
        await tg_transport.stop()
        if askme_redis:
            await askme_redis.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
