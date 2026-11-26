"""Shared state, utilities, keyboards, and error handling for bot handlers."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from functools import partial
from typing import Any, Awaitable, Callable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from config import OWNER_TELEGRAM_ID
import database as db
from telegram_utils import safe_edit_or_reply_html

logger = logging.getLogger("bot.handlers.common")

HTML_PARSE_MODE = ParseMode.HTML
MIN_CHECK_INTERVAL = 5
MAX_CHECK_INTERVAL = 1440

(
    STATE_LOGIN_USER,
    STATE_LOGIN_PASS,
    STATE_ADD_COURSE,
) = range(3)

_BACKGROUND_TASKS: set[asyncio.Task] = set()
_ENGINE: Any = None
_REMINDER_ENGINE: Any = None


def set_engine(engine: Any) -> None:
    """Store the shared MonitorEngine reference for bot handlers."""
    global _ENGINE
    _ENGINE = engine


def get_engine(ctx: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Any:
    """Return the active MonitorEngine from context bot_data or module state."""
    if ctx is not None:
        bot_data = getattr(ctx, "bot_data", None)
        if isinstance(bot_data, dict) and bot_data.get("engine") is not None:
            return bot_data["engine"]
    return _ENGINE


def set_reminder_engine(reminder_engine: Any) -> None:
    """Store the shared ReminderEngine reference for bot handlers."""
    global _REMINDER_ENGINE
    _REMINDER_ENGINE = reminder_engine


def get_reminder_engine(ctx: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Any:
    """Return the active ReminderEngine from context bot_data or module state."""
    if ctx is not None:
        bot_data = getattr(ctx, "bot_data", None)
        if isinstance(bot_data, dict) and bot_data.get("reminder_engine") is not None:
            return bot_data["reminder_engine"]
    return _REMINDER_ENGINE


def resolve_hook(name: str, local_value: Any, default_value: Any) -> Any:
    """Resolve a hook allowing monkeypatches on either a handler module or the `bot` facade."""
    if local_value is not default_value:
        return local_value
    bot_mod = sys.modules.get("bot")
    if bot_mod is not None:
        bot_val = getattr(bot_mod, name, default_value)
        if bot_val is not default_value:
            return bot_val
    return default_value


def _track_background_task(task: asyncio.Task) -> asyncio.Task:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_blocking(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking DB/scraper/IO work in the default thread executor."""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))
    return await loop.run_in_executor(None, func, *args)


async def _on_telegram_forbidden(chat_id: int) -> None:
    """Disable notifications/monitoring when a user blocks the bot (H12)."""
    if not chat_id:
        return
    if hasattr(db, "set_user_notifications_enabled"):
        await _run_blocking(db.set_user_notifications_enabled, chat_id, False)
    else:
        await _run_blocking(db.set_monitoring, chat_id, False)


def validate_check_interval(raw: Any) -> Optional[int]:
    """Validate check_interval within unified bounds [5, 1440] minutes (H16)."""
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if MIN_CHECK_INTERVAL <= val <= MAX_CHECK_INTERVAL:
        return val
    return None


def _format_interval(minutes: int) -> str:
    minutes = int(minutes or 10)
    if minutes >= 60:
        hours = minutes // 60
        rem_min = minutes % 60
        if rem_min == 0:
            if hours == 1:
                return "ساعة واحدة"
            if hours == 2:
                return "ساعتين"
            if 3 <= hours <= 10:
                return f"{hours} ساعات"
            return f"{hours} ساعة"
        return f"{hours} س و {rem_min} د"
    if minutes == 1:
        return "دقيقة واحدة"
    if minutes == 2:
        return "دقيقتين"
    if 3 <= minutes <= 10:
        return f"{minutes} دقائق"
    return f"{minutes} دقيقة"


async def _send(obj: Any, text: str, keyboard: Any = None, edit: bool = False):
    kb = InlineKeyboardMarkup(keyboard) if keyboard else None
    return await safe_edit_or_reply_html(
        obj,
        text,
        reply_markup=kb,
        edit=edit,
        disable_web_page_preview=True,
        on_forbidden=_on_telegram_forbidden,
    )


_DEFAULT_SEND = _send


async def _send_sections(obj: Any, sections: list, keyboard: Any = None, edit_first: bool = False) -> None:
    if not sections:
        return
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    if len(sections) == 1:
        await send_fn(obj, sections[0], keyboard=keyboard, edit=edit_first)
        return

    await send_fn(obj, sections[0], keyboard=None, edit=edit_first)
    for sec in sections[1:-1]:
        await send_fn(obj, sec, keyboard=None, edit=False)
    await send_fn(obj, sections[-1], keyboard=keyboard, edit=False)


def get_webapp_url() -> str:
    url = os.getenv("WEBAPP_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return ""
    if not url.startswith("http"):
        url = f"https://{url}"
    clean = url.rstrip("/")
    if not clean.endswith("/webapp") and not clean.endswith("/app"):
        clean = f"{clean}/webapp"
    return clean


_DEFAULT_GET_WEBAPP_URL = get_webapp_url


def _main_kb(user: Any = None) -> list[list[InlineKeyboardButton]]:
    if not user:
        return [
            [InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")],
        ]
    is_mon = user.get("is_monitoring", 0) if isinstance(user, dict) else False
    mon_btn = (
        InlineKeyboardButton("🔴 إيقاف المراقبة", callback_data="stop_monitor")
        if is_mon
        else InlineKeyboardButton("🟢 تشغيل المراقبة", callback_data="start_monitor")
    )

    webapp_url = resolve_hook("get_webapp_url", get_webapp_url, _DEFAULT_GET_WEBAPP_URL)()
    rows: list[list[InlineKeyboardButton]] = []
    if webapp_url:
        tid_param = f"?tid={user['telegram_id']}" if (isinstance(user, dict) and user.get("telegram_id")) else ""
        target_url = f"{webapp_url}{tid_param}"
        rows.append([
            InlineKeyboardButton("📱 فتح لوحة التحكم الأكاديمية (Mini App)", web_app=WebAppInfo(url=target_url))
        ])
    else:
        rows.append([
            InlineKeyboardButton("📱 لوحة التحكم التفاعلية (Mini App)", callback_data="webapp_info")
        ])

    rows.extend([
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="add_course_start"),
        ],
        [
            InlineKeyboardButton("🎯 كشف العلامات", callback_data="grades_menu"),
            InlineKeyboardButton("📋 الحضور والغياب", callback_data="attendance_menu"),
        ],
        [
            InlineKeyboardButton("📅 جدول الواجبات", callback_data="deadlines"),
            InlineKeyboardButton("📢 إعلانات الدكاترة", callback_data="announcements_menu"),
        ],
        [
            mon_btn,
            InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
        ],
        [
            InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"),
            InlineKeyboardButton("⚙️ إعدادات الإشعارات", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("📲 ربط تطبيق الهاتف", callback_data="app_pair"),
            InlineKeyboardButton("🚪 تسجيل الخروج من الجلسة", callback_data="logout_prompt"),
        ],
    ])
    return rows


def _back_kb(cb: str = "main_menu") -> list[list[InlineKeyboardButton]]:
    if cb == "main_menu":
        return [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
    if cb == "my_courses":
        return [
            [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ]
        ]
    return [[InlineKeyboardButton("🔙 رجوع", callback_data=cb)]]


async def _ensure_user_menu_button(bot_obj: Any, tid: int) -> None:
    webapp_url = resolve_hook("get_webapp_url", get_webapp_url, _DEFAULT_GET_WEBAPP_URL)()
    if not webapp_url or not tid:
        return
    try:
        await bot_obj.set_chat_menu_button(
            chat_id=tid,
            menu_button=MenuButtonWebApp(
                text="📱 لوحة التحكم",
                web_app=WebAppInfo(url=f"{webapp_url}?tid={tid}"),
            ),
        )
    except Exception as exc:
        logger.debug("Could not set user chat menu button for %s: %s", tid, exc)


def _is_harmless_callback_error(exc: BaseException) -> bool:
    """Return True for benign Telegram BadRequest errors on expired/invalid callback queries."""
    msg = str(exc).lower()
    return (
        "query is too old" in msg
        or "query id is invalid" in msg
        or "response timeout expired" in msg
    )


def _make_answer_once(q: Any) -> Callable[..., Awaitable[None]]:
    """Return an async helper that answers a CallbackQuery at most once (C8)."""
    answered = False

    async def _answer_once(text: Optional[str] = None, *, show_alert: bool = False) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        try:
            if text is None and not show_alert:
                await q.answer()
            else:
                await q.answer(text, show_alert=show_alert)
        except BadRequest as exc:
            if _is_harmless_callback_error(exc):
                logger.debug("Ignored harmless callback query answer error: %s", exc)
                return
            raise

    _answer_once.is_answered = lambda: answered  # type: ignore[attr-defined]
    return _answer_once


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global Telegram error handler: ignore harmless expired-query errors, log all others."""
    err = ctx.error
    if isinstance(err, BadRequest) and _is_harmless_callback_error(err):
        logger.debug("Ignored harmless Telegram BadRequest: %s", err)
        return
    exc_info = (type(err), err, err.__traceback__) if isinstance(err, BaseException) else True
    logger.error("Unhandled exception while handling update %r: %s", update, err, exc_info=exc_info)


def _is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and int(u.id) == int(OWNER_TELEGRAM_ID))


async def _reject_unauthorized(update: Update) -> int:
    if update.callback_query:
        try:
            await update.callback_query.answer("🔒 هذا البوت خاص بصاحبه فقط.", show_alert=True)
        except Exception as exc:
            logger.debug("Failed to answer unauthorized callback query: %s", exc)
    elif update.message:
        try:
            await update.message.reply_text("🔒 هذا البوت خاص بصاحبه فقط وغير متاح للاستخدام العام.")
        except Exception as exc:
            logger.debug("Failed to reply to unauthorized message: %s", exc)
    return ConversationHandler.END
