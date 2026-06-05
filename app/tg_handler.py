from telegram.ext import Application

from app.account_manager import AccountManager
from app.tg_commands import register_handlers
from app.time_utils import DEFAULT_APP_TIMEZONE


def build_tg_app(
    token: str,
    account_manager: AccountManager,
    admin_id: int,
    app_timezone: str = DEFAULT_APP_TIMEZONE,
) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["account_manager"] = account_manager
    app.bot_data["admin_id"] = int(admin_id)
    app.bot_data["app_timezone"] = app_timezone
    register_handlers(app)
    return app
