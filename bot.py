"""Mutah LMS Monitor — slim Telegram bot entrypoint on a shared asyncio loop with FastAPI."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers

from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application

from config import OWNER_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
import database as db
from handlers import register_handlers
from handlers.auth import (
    _send_app_pairing,
    cancel_conv,
    cmd_app,
    cmd_logout,
    cmd_pair,
    cmd_start,
    cmd_unlink,
    handle_confirm_logout,
    handle_logout_prompt,
    handle_text,
    login_got_pass,
    login_got_user,
    login_start,
)
from handlers.callbacks import _dispatch_callback, fallback_callback, handle_callback
from handlers.common import (
    _BACKGROUND_TASKS,
    HTML_PARSE_MODE,
    MAX_CHECK_INTERVAL,
    MIN_CHECK_INTERVAL,
    STATE_ADD_COURSE,
    STATE_LOGIN_PASS,
    STATE_LOGIN_USER,
    _back_kb,
    _ensure_user_menu_button,
    _format_interval,
    _is_harmless_callback_error,
    _is_owner,
    _main_kb,
    _make_answer_once,
    _on_telegram_forbidden,
    _reject_unauthorized,
    _run_blocking,
    _send,
    _send_sections,
    _track_background_task,
    error_handler,
    get_engine,
    get_reminder_engine,
    get_webapp_url,
    owner_guard,
    set_engine,
    set_reminder_engine,
    validate_check_interval,
)
from handlers.courses import (
    _fetch_show_course,
    add_course_got_id,
    add_course_start,
    cmd_courses,
)
from handlers.views import (
    _build_course_sections,
    _render_announcements_view,
    _render_attendance_view,
    _render_course_detail,
    _render_courses,
    _render_courses_view,
    _render_deadlines_view,
    _render_grades_view,
    _render_help,
    _render_main_menu,
    _render_settings,
    _render_stats_view,
    _show_main,
    cmd_help,
    status_cmd,
)
from lms_scraper import LMSScraper
from monitor import MonitorEngine
from reminder import ReminderEngine
from services.observability import configure_logging
from webapp_server import (
    create_pairing_code,
    init_webapp_engines,
    start_webapp_server_async,
    stop_webapp_server_async,
)

_BOT_LOG_HANDLER = logging.handlers.RotatingFileHandler(
    "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
)
_obs_file_handler = configure_logging(log_file="bot.log")
_BOT_LOG_HANDLER.setFormatter(_obs_file_handler.formatter)
_root_logger = logging.getLogger()
_root_logger.removeHandler(_obs_file_handler)
_obs_file_handler.close()
_root_logger.addHandler(_BOT_LOG_HANDLER)
logger = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def post_init(app: Application) -> None:
    """Initialize bot commands, MonitorEngine, ReminderEngine, and FastAPI on the shared asyncio loop."""
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 القائمة الرئيسية"),
        BotCommand("courses", "📚 موادي المراقَبة"),
        BotCommand("status", "🩺 حالة النظام والمراقبة"),
        BotCommand("app", "📲 ربط تطبيق الهاتف"),
        BotCommand("pair", "📲 ربط تطبيق الهاتف"),
        BotCommand("unlink", "🔌 فصل أجهزة التطبيق"),
        BotCommand("logout", "🚪 تسجيل الخروج من الحساب"),
        BotCommand("help", "ℹ️ المساعدة"),
    ])

    rem_engine = ReminderEngine(app.bot)
    app.bot_data["reminder_engine"] = rem_engine
    set_reminder_engine(rem_engine)

    engine = MonitorEngine(app.bot, rem_engine)
    if hasattr(engine, "attach_loop"):
        engine.attach_loop(asyncio.get_running_loop())
    app.bot_data["engine"] = engine
    set_engine(engine)

    await engine.start()
    await rem_engine.start()

    init_webapp_engines(engine, rem_engine)
    app.bot_data["webapp_task"] = await start_webapp_server_async()

    webapp_url = get_webapp_url()
    if webapp_url:
        try:
            await app.bot.set_chat_menu_button(
                chat_id=OWNER_TELEGRAM_ID,
                menu_button=MenuButtonWebApp(text="📱 لوحة التحكم", web_app=WebAppInfo(url=webapp_url)),
            )
            logger.info("Telegram MenuButtonWebApp configured for owner: %s", webapp_url)
        except Exception as exc:
            logger.warning("Could not configure chat menu button: %s", exc)

    logger.info("Bot ready — shared asyncio loop with FastAPI!")


async def post_shutdown(app: Application) -> None:
    """Gracefully drain background tasks, FastAPI server, and engines before closing the database."""
    pending_tasks = [t for t in list(_BACKGROUND_TASKS) if not t.done()]
    if pending_tasks:
        _, still_pending = await asyncio.wait(pending_tasks, timeout=5.0)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    try:
        try:
            await stop_webapp_server_async()
        except Exception as exc:
            logger.warning("Failed to stop FastAPI server cleanly on shutdown: %s", exc)

        engine = app.bot_data.get("engine") or get_engine()
        if engine:
            await engine.stop()
        set_engine(None)
    finally:
        try:
            rem_engine = app.bot_data.get("reminder_engine") or get_reminder_engine()
            if rem_engine:
                await rem_engine.stop()
            set_reminder_engine(None)
        finally:
            if hasattr(db, "close"):
                try:
                    await _run_blocking(db.close)
                except Exception as exc:
                    logger.warning("Failed to close DB cleanly on shutdown: %s", exc)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    # H4: Initialize database schema BEFORE starting the FastAPI server (launched in post_init).
    db.init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    register_handlers(app)

    logger.info("Starting bot (button-only mode, shared asyncio loop)...")
    app.run_polling(drop_pending_updates=True)


__all__ = [
    "Application", "HTML_PARSE_MODE", "LMSScraper", "MAX_CHECK_INTERVAL", "MIN_CHECK_INTERVAL",
    "MonitorEngine", "OWNER_TELEGRAM_ID", "ReminderEngine", "STATE_ADD_COURSE", "STATE_LOGIN_PASS",
    "STATE_LOGIN_USER", "TELEGRAM_BOT_TOKEN", "_BACKGROUND_TASKS", "_back_kb", "_build_course_sections",
    "_dispatch_callback", "_ensure_user_menu_button", "_fetch_show_course", "_format_interval",
    "_is_harmless_callback_error", "_is_owner", "_main_kb", "_make_answer_once", "_on_telegram_forbidden",
    "_reject_unauthorized", "_render_announcements_view", "_render_attendance_view", "_render_course_detail",
    "_render_courses", "_render_courses_view", "_render_deadlines_view", "_render_grades_view", "_render_help",
    "_render_main_menu", "_render_settings", "_render_stats_view", "_run_blocking", "_send", "_send_app_pairing",
    "_send_sections", "_show_main", "_track_background_task", "add_course_got_id", "add_course_start",
    "cancel_conv", "cmd_app", "cmd_courses", "cmd_help", "cmd_logout", "cmd_pair", "cmd_start", "cmd_unlink",
    "create_pairing_code", "db", "error_handler", "fallback_callback", "get_engine", "get_reminder_engine",
    "get_webapp_url", "handle_callback", "handle_confirm_logout", "handle_logout_prompt", "handle_text",
    "init_webapp_engines", "logger", "login_got_pass", "login_got_user", "login_start", "main", "owner_guard",
    "post_init", "post_shutdown", "register_handlers", "set_engine", "set_reminder_engine",
    "start_webapp_server_async", "status_cmd", "stop_webapp_server_async", "validate_check_interval",
]

if __name__ == "__main__":
    main()
