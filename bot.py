import os
import re
import json
import threading
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import asyncio
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, ConversationHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

import database as db
from lms_scraper import LMSScraper
from monitor import MonitorEngine
from reminder import ReminderEngine
from config import TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID
from webapp_server import run_web_server, init_webapp_engines, create_pairing_code
from telegram_utils import (
    escape_html,
    html_link,
    safe_edit_message_text_html,
    safe_edit_or_reply_html,
    send_html_message,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

HTML_PARSE_MODE = ParseMode.HTML
MIN_CHECK_INTERVAL = 5
MAX_CHECK_INTERVAL = 1440

_BACKGROUND_TASKS: set[asyncio.Task] = set()

(
    STATE_LOGIN_USER,
    STATE_LOGIN_PASS,
    STATE_ADD_COURSE,
) = range(3)


def _track_background_task(task: asyncio.Task) -> asyncio.Task:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_blocking(func, *args, **kwargs):
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


def validate_check_interval(raw) -> Optional[int]:
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
            elif hours == 2:
                return "ساعتين"
            elif 3 <= hours <= 10:
                return f"{hours} ساعات"
            else:
                return f"{hours} ساعة"
        else:
            return f"{hours} س و {rem_min} د"
    else:
        if minutes == 1:
            return "دقيقة واحدة"
        elif minutes == 2:
            return "دقيقتين"
        elif 3 <= minutes <= 10:
            return f"{minutes} دقائق"
        else:
            return f"{minutes} دقيقة"


async def _send(obj, text, keyboard=None, edit=False):
    kb = InlineKeyboardMarkup(keyboard) if keyboard else None
    return await safe_edit_or_reply_html(
        obj,
        text,
        reply_markup=kb,
        edit=edit,
        disable_web_page_preview=True,
        on_forbidden=_on_telegram_forbidden,
    )


async def _send_sections(obj, sections: list, keyboard=None, edit_first=False):
    if not sections:
        return
    if len(sections) == 1:
        await _send(obj, sections[0], keyboard=keyboard, edit=edit_first)
        return

    await _send(obj, sections[0], keyboard=None, edit=edit_first)
    for sec in sections[1:-1]:
        await _send(obj, sec, keyboard=None, edit=False)
    await _send(obj, sections[-1], keyboard=keyboard, edit=False)

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

def _main_kb(user=None):
    if not user:
        return [
            [InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")],
        ]
    is_mon = user.get("is_monitoring", 0) if isinstance(user, dict) else False
    mon_btn = (
        InlineKeyboardButton("🔴 إيقاف المراقبة", callback_data="stop_monitor")
        if is_mon else
        InlineKeyboardButton("🟢 تشغيل المراقبة", callback_data="start_monitor")
    )

    webapp_url = get_webapp_url()
    rows = []
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
            InlineKeyboardButton("📅 جدول الواجبات",  callback_data="deadlines"),
            InlineKeyboardButton("📢 إعلانات الدكاترة", callback_data="announcements_menu"),
        ],
        [
            mon_btn,
            InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
        ],
        [
            InlineKeyboardButton("📊 الإحصائيات",     callback_data="stats"),
            InlineKeyboardButton("⚙️ إعدادات الإشعارات", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("📲 ربط تطبيق الهاتف", callback_data="app_pair"),
            InlineKeyboardButton("🚪 تسجيل الخروج من الجلسة", callback_data="logout_prompt"),
        ],
    ])
    return rows

def _back_kb(cb="main_menu"):
    if cb == "main_menu":
        return [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
    elif cb == "my_courses":
        return [
            [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ]
        ]
    return [[InlineKeyboardButton("🔙 رجوع", callback_data=cb)]]

async def _ensure_user_menu_button(bot, tid: int):
    webapp_url = get_webapp_url()
    if not webapp_url or not tid:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=tid,
            menu_button=MenuButtonWebApp(
                text="📱 لوحة التحكم",
                web_app=WebAppInfo(url=f"{webapp_url}?tid={tid}")
            )
        )
    except Exception:
        pass

async def _show_main(msg_obj, tid, edit=False):
    user = await _run_blocking(db.get_user, tid)
    try:
        bot = msg_obj.get_bot()
        _track_background_task(asyncio.create_task(_ensure_user_menu_button(bot, tid)))
    except Exception:
        pass
    if user:
        courses = await _run_blocking(db.get_user_courses, tid)
        mon     = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
        status  = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
        uname   = escape_html(user.get("full_name") or user["lms_username"])
        std_id  = escape_html(user.get("lms_username") or "")
        text = (
            f"🎓 <b>بوت مراقب LMS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الطالب: <b>{uname}</b>\n"
            f"🆔 الرقم الجامعي: <code>{std_id}</code>\n"
            f"📡 حالة المراقبة: {status}\n"
            f"📚 المواد المضافة: <b>{len(courses)}</b> (المراقَبة: <b>{len(mon)}</b>)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            "اختر من القائمة أدناه:"
        )
    else:
        text = (
            "🎓 <b>بوت مراقب نظام LMS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "يُرسل إشعارات فورية عند نزول:\n"
            "📚 واجبات جديدة ومواعيدها\n"
            "📝 كويزات وامتحانات جديدة\n"
            "📎 سلايدات وملفات ومحاضرات\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ابدأ بتسجيل الدخول:"
        )
    await _send(msg_obj, text, _main_kb(user), edit=edit)

def _is_harmless_callback_error(exc: BaseException) -> bool:
    """Return True for benign Telegram BadRequest errors on expired/invalid callback queries."""
    msg = str(exc).lower()
    return (
        "query is too old" in msg
        or "query id is invalid" in msg
        or "response timeout expired" in msg
    )


def _make_answer_once(q) -> Callable[..., Awaitable[None]]:
    """Return an async helper that answers a CallbackQuery at most once."""
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
        except BadRequest as e:
            if _is_harmless_callback_error(e):
                logger.debug("Ignored harmless callback query answer error: %s", e)
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

async def _reject_unauthorized(update: Update):
    if update.callback_query:
        try:
            await update.callback_query.answer("🔒 هذا البوت خاص بصاحبه فقط.", show_alert=True)
        except Exception as e:
            logger.debug("Failed to answer unauthorized callback query: %s", e)
    elif update.message:
        try:
            await update.message.reply_text("🔒 هذا البوت خاص بصاحبه فقط وغير متاح للاستخدام العام.")
        except Exception as e:
            logger.debug("Failed to reply to unauthorized message: %s", e)
    return ConversationHandler.END

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    ctx.user_data.clear()
    await _show_main(update.message, update.effective_user.id)
    return ConversationHandler.END

async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    q = update.callback_query
    await q.answer()
    ctx.user_data["login_msg_id"] = q.message.message_id

    await _send(q.message,
        "🔑 <b>تسجيل الدخول</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل <b>رقم الطالب</b> (Student ID):",
        [[InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")]],
        edit=True
    )
    return STATE_LOGIN_USER

async def login_got_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_user = update.message.text.strip()
    ctx.user_data["lms_user"] = raw_user.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    try:
        await update.message.delete()
    except Exception:
        pass

    bot = update.get_bot()
    try:
        await safe_edit_message_text_html(
            bot,
            chat_id=update.effective_chat.id,
            message_id=ctx.user_data.get("login_msg_id"),
            text=(
                "🔑 <b>تسجيل الدخول</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ رقم الطالب: <code>{escape_html(ctx.user_data['lms_user'])}</code>\n\n"
                "الآن أرسل <b>كلمة المرور</b>:"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
            ]]),
            on_forbidden=_on_telegram_forbidden,
        )
    except Exception:
        pass
    return STATE_LOGIN_PASS

async def login_got_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    password: str = ""
    msg_deleted = False

    async def _delete_password_message() -> None:
        nonlocal msg_deleted
        if msg_deleted or msg is None:
            return
        msg_deleted = True
        try:
            await msg.delete()
        except Exception as del_err:
            logger.warning(
                "Failed to delete password message for user %s: %s",
                getattr(update.effective_user, "id", None),
                del_err,
            )
            try:
                await msg.reply_text(
                    "⚠️ تعذّر حذف رسالة كلمة المرور تلقائياً. "
                    "يُرجى حذف رسالتك يدوياً فوراً حفاظاً على أمان حسابك."
                )
            except Exception as warn_err:
                logger.error("Failed to send manual password-deletion warning: %s", warn_err)

    try:
        # Read password verbatim (never strip/translate/modify) and delete the message FIRST.
        if msg is not None and msg.text is not None:
            password = msg.text
        await _delete_password_message()

        username = ctx.user_data.get("lms_user", "")
        tid      = update.effective_user.id

        if not username:
            bot = update.get_bot()
            try:
                await safe_edit_message_text_html(
                    bot,
                    chat_id=update.effective_chat.id,
                    message_id=ctx.user_data.get("login_msg_id"),
                    text=(
                        "⚠️ <b>انتهت الجلسة المؤقتة!</b>\n\n"
                        "يرجى إعادة المحاولة من جديد بالضغط على الزر أدناه وإدخال رقم الطالب ثم كلمة المرور."
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 البدء من جديد", callback_data="login_start"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ]]),
                    on_forbidden=_on_telegram_forbidden,
                )
            except Exception as e:
                logger.debug("Could not edit expired-session login message: %s", e)
            return ConversationHandler.END

        bot = update.get_bot()
        try:
            await safe_edit_message_text_html(
                bot,
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text="⏳ <b>جارٍ تسجيل الدخول إلى LMS...</b>",
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception as e:
            logger.debug("Could not edit in-progress login message: %s", e)

        scraper = LMSScraper(username, password)
        ok      = await _run_blocking(scraper.login)

        if ok:
            await _run_blocking(db.save_user, tid, username, password, scraper.user_id, scraper.full_name)
            _track_background_task(asyncio.create_task(_ensure_user_menu_button(bot, tid)))
            engine: MonitorEngine = ctx.bot_data.get("engine")
            if engine:
                await engine.resume_user(tid, scraper)
            else:
                await _run_blocking(scraper.close)
            full = escape_html(scraper.full_name or username)
            monitored_courses = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
            registered_courses = await _run_blocking(db.get_user_courses, tid)
            saved_user = await _run_blocking(db.get_user, tid) or {}
            if monitored_courses and saved_user.get("is_monitoring"):
                next_step = "موادك المراقبة محفوظة، وسيستأنف البوت متابعتها قريباً."
                actions = [
                    InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ]
            elif registered_courses:
                next_step = "موادك المضافة محفوظة. يمكنك مراجعتها أو إضافة مواد جديدة من القائمة أدناه."
                actions = [
                    InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                    InlineKeyboardButton("➕ إضافة مادة", callback_data="add_course_start"),
                ]
            else:
                next_step = "اضغط على «➕ إضافة مادة للمراقبة» لإدخال رقم أو رابط المواد التي ترغب بمراقبتها:"
                actions = [
                    InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="add_course_start"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ]

            try:
                await safe_edit_message_text_html(
                    bot,
                    chat_id=update.effective_chat.id,
                    message_id=ctx.user_data.get("login_msg_id"),
                    text=(
                        f"✅ <b>تم تسجيل الدخول بنجاح!</b>\n\n"
                        f"👤 الطالب: <b>{full}</b>\n"
                        f"🆔 الرقم الجامعي: <code>{escape_html(username)}</code>\n\n"
                        f"{next_step}"
                    ),
                    reply_markup=InlineKeyboardMarkup([actions]),
                    on_forbidden=_on_telegram_forbidden,
                )
            except Exception as e:
                logger.debug("Could not edit login-success message: %s", e)
        else:
            err_detail = scraper.error_message or "تحقق من رقم الطالب وكلمة المرور وحاول مجدداً."
            await _run_blocking(scraper.close)
            try:
                await safe_edit_message_text_html(
                    bot,
                    chat_id=update.effective_chat.id,
                    message_id=ctx.user_data.get("login_msg_id"),
                    text=(
                        "❌ <b>فشل تسجيل الدخول!</b>\n\n"
                        f"📌 <b>السبب:</b> {escape_html(err_detail)}\n\n"
                        "يرجى التأكد من البيانات والمحاولة مجدداً."
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 إعادة المحاولة", callback_data="login_start"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ]]),
                    on_forbidden=_on_telegram_forbidden,
                )
            except Exception as e:
                logger.debug("Could not edit login-failure message: %s", e)

        ctx.user_data.clear()
        return ConversationHandler.END
    finally:
        await _delete_password_message()

async def add_course_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    q   = update.callback_query
    tid = update.effective_user.id
    _answer_once = _make_answer_once(q)

    try:
        if not await _run_blocking(db.get_user, tid):
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return ConversationHandler.END

        await _answer_once()
        ctx.user_data["add_course_msg_id"] = q.message.message_id

        await _send(q.message,
            "➕ <b>إضافة مادة للمراقبة</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>كيف أجد رقم المادة (Course ID)?</b>\n"
            "افتح المادة في LMS وانظر للرابط:\n"
            "<code>course/view.php?id=</code><b>XXXX</b>\n\n"
            "الرقم بعد <code>id=</code> هو ما تحتاجه.\n\n"
            "أرسل <b>رقم المادة (Course ID)</b> الآن:",
            [[InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")]],
            edit=True
        )
        return STATE_ADD_COURSE
    finally:
        if not _answer_once.is_answered():
            try:
                await _answer_once()
            except Exception as e:
                logger.warning("Failed to answer add_course_start callback query: %s", e)

def _persist_course_snapshot_sync(
    tid: int,
    course_id: int,
    course_name: Optional[str],
    course_url: Optional[str],
    content: dict,
    reminder_eng: Optional[ReminderEngine] = None,
    add_course_record: bool = False,
) -> None:
    """Persist scraped course snapshot (items, grades, announcements, attendance, reminders) synchronously."""
    if add_course_record and course_name:
        db.add_course(tid, course_id, course_name, course_url or "")

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])
    grades    = content.get("grades",     [])
    anns      = content.get("announcements", [])
    att_list  = content.get("attendance", [])

    for item in assigns:
        db.mark_seen(tid, course_id, item["id"], "assign",
                     item["name"], item["url"], item.get("due_date", ""),
                     is_submitted=1 if item.get("is_submitted") else 0)
    for item in quizzes:
        db.mark_seen(tid, course_id, item["id"], "quiz",
                     item["name"], item["url"], item.get("due_date", ""),
                     is_submitted=1 if item.get("is_submitted") else 0)
    for item in resources:
        db.mark_seen(tid, course_id, item["id"], "resource",
                     item["name"], item["url"])
    for g in grades:
        db.upsert_grade(tid, course_id, g["item_id"], g.get("item_name", ""),
                        g.get("grade_val", ""), g.get("grade_max", ""),
                        g.get("percentage", ""), g.get("feedback", ""),
                        g.get("item_url") or g.get("url", ""))
    for ann in anns:
        db.upsert_announcement(tid, course_id, ann["discuss_id"], ann.get("forum_name", ""),
                               ann.get("title", ""), ann.get("author", ""),
                               ann.get("posted_date", ""), "", ann.get("url", ""))
    for att in att_list:
        db.upsert_attendance(
            tid, course_id, att["att_id"],
            taken_sessions=att.get("taken_sessions", 0),
            total_sessions=att.get("total_sessions", 0),
            present_count=att.get("present_count", 0),
            absent_count=att.get("absent_count", 0),
            late_count=att.get("late_count", 0),
            excused_count=att.get("excused_count", 0),
            points_str=att.get("points_str", ""),
            percentage_str=att.get("percentage_str", ""),
            sessions_json=json.dumps(att.get("sessions", []), ensure_ascii=False),
            att_url=att.get("att_url") or att.get("url", ""),
        )

    if reminder_eng:
        reminder_eng.register_assignments(tid, course_id, assigns + quizzes)

    for task_item in assigns + quizzes:
        if task_item.get("is_submitted"):
            db.mark_assignment_submitted(tid, task_item["id"], 1)

    if hasattr(db, "mark_course_seeded"):
        try:
            db.mark_course_seeded(tid, course_id)
        except Exception:
            pass


async def add_course_got_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    raw = update.message.text.strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))

    try:
        await update.message.delete()
    except Exception:
        pass

    bot = update.get_bot()
    mid = ctx.user_data.get("add_course_msg_id")

    # Support either plain Course ID or full Moodle course URL
    url_match = re.search(r"(?:id=|course=)(\d+)", raw)
    cid_str = url_match.group(1) if url_match else raw

    try:
        course_id = int(cid_str)
    except ValueError:
        try:
            await safe_edit_message_text_html(
                bot,
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=(
                    "❌ <b>الرقم غير صحيح!</b>\n\n"
                    "أرسل رقم المادة مثل <code>12345</code> أو الصق رابط المادة مباشرة من LMS:\n"
                    "<code>https://mulms.mutah.edu.jo/course/view.php?id=12345</code>\n\n"
                    "أرسل رقم المادة أو الرابط مجدداً:"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
                ]]),
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception:
            pass
        return STATE_ADD_COURSE

    try:
        await safe_edit_message_text_html(
            bot,
            chat_id=update.effective_chat.id,
            message_id=mid,
            text=f"⏳ <b>جارٍ جلب معلومات المادة <code>{course_id}</code>...</b>",
            on_forbidden=_on_telegram_forbidden,
        )
    except Exception:
        pass

    user = await _run_blocking(db.get_user, tid)
    if not user:
        ctx.user_data.clear()
        return ConversationHandler.END

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    try:
        ok = await _run_blocking(scraper.login)

        if not ok:
            try:
                await safe_edit_message_text_html(
                    bot,
                    chat_id=update.effective_chat.id,
                    message_id=mid,
                    text="❌ <b>فشل تسجيل الدخول.</b> حاول لاحقاً.",
                    reply_markup=InlineKeyboardMarkup(_back_kb("main_menu")),
                    on_forbidden=_on_telegram_forbidden,
                )
            except Exception:
                pass
            ctx.user_data.clear()
            return ConversationHandler.END

        course_name, content = await _run_blocking(scraper.get_course_info, course_id)
    finally:
        await _run_blocking(scraper.close)

    if not course_name:
        try:
            await safe_edit_message_text_html(
                bot,
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=(
                    f"❌ <b>لم يتم العثور على مادة بالـ ID: <code>{course_id}</code></b>\n\n"
                    "تأكد من الرقم وأنك مسجّل في هذه المادة.\n\n"
                    "أرسل رقم مادة آخر:"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
                ]]),
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception:
            pass
        return STATE_ADD_COURSE

    course_url = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    reminder_eng: ReminderEngine = ctx.bot_data.get("reminder_engine")
    await _run_blocking(
        _persist_course_snapshot_sync,
        tid,
        course_id,
        course_name,
        course_url,
        content,
        reminder_eng,
        True,
    )

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])
    grades    = content.get("grades",     [])
    anns      = content.get("announcements", [])
    att_list  = content.get("attendance", [])

    sections = _build_course_sections(
        course_name, course_id, assigns, quizzes, resources,
        grades=grades, announcements=anns, attendance=att_list, new_add=True
    )
    kb = [
        [
            InlineKeyboardButton("➕ إضافة مادة أخرى", callback_data="add_course_start"),
            InlineKeyboardButton("📚 موادي المسجلة",   callback_data="my_courses"),
        ],
        [InlineKeyboardButton("🏠 القائمة الرئيسية",   callback_data="main_menu")],
    ]

    try:
        first_kb = InlineKeyboardMarkup(kb) if len(sections) == 1 else None
        await safe_edit_message_text_html(
            bot,
            chat_id=update.effective_chat.id,
            message_id=mid,
            text=sections[0],
            reply_markup=first_kb,
            disable_web_page_preview=True,
            on_forbidden=_on_telegram_forbidden,
        )

        for sec in sections[1:-1]:
            await send_html_message(
                bot,
                update.effective_chat.id,
                sec,
                disable_web_page_preview=True,
                on_forbidden=_on_telegram_forbidden,
            )

        if len(sections) > 1:
            await send_html_message(
                bot,
                update.effective_chat.id,
                sections[-1],
                reply_markup=InlineKeyboardMarkup(kb),
                disable_web_page_preview=True,
                on_forbidden=_on_telegram_forbidden,
            )
    except Exception as e:
        logger.error(f"Error showing course summary: {e}")

    ctx.user_data.clear()
    return ConversationHandler.END

async def cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("تم الإلغاء")
    ctx.user_data.clear()
    await _show_main(q.message, update.effective_user.id, edit=True)
    return ConversationHandler.END

def _build_course_sections(course_name, course_id, assigns, quizzes, resources,
                            grades=None, announcements=None, attendance=None, new_add=False):
    MAX = 3500
    grades = grades or []
    announcements = announcements or []
    attendance = attendance or []

    url   = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    cname = escape_html(course_name)

    header = (
        f"{'✅ <b>تمت إضافة المادة بنجاح!</b>' if new_add else '📖 <b>تفاصيل المادة</b>'}\n\n"
        f"🎓 {html_link(course_name, url)}\n"
        f"🆔 ID: <code>{course_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    def _section_lines(title, icon, items, key_name="name", key_url="url",
                       key_due="due_date", show_due=True, is_assign=False):
        if not items:
            return [f"{icon} <b>{escape_html(title)}:</b> لا يوجد حالياً\n"]
        lines = [f"{icon} <b>{escape_html(title)} ({len(items)}):</b>"]
        for item in items:
            raw_name = str(item.get(key_name, ""))[:80]
            link = item.get(key_url, "")
            due  = ""
            sub_st = ""
            if show_due and item.get(key_due):
                due = f"\n      ⏰ <code>{escape_html(item[key_due])}</code>"
            if is_assign:
                is_quiz_type = item.get("type") == "quiz" or str(item.get("id", "")).startswith("quiz_")
                if item.get("is_submitted"):
                    sub_st = "\n      ✅ <i>منجز (تم الحل في LMS)</i>" if is_quiz_type else "\n      ✅ <i>منجز (تم التسليم في LMS)</i>"
                else:
                    sub_st = "\n      ⏳ <i>لم يتم الحل بعد</i>" if is_quiz_type else "\n      ⏳ <i>لم يتم التسليم بعد</i>"
            lines.append(f"  • {html_link(raw_name, link)}{due}{sub_st}")
        lines.append("")
        return lines

    all_lines = []
    all_lines.extend(_section_lines("الواجبات", "📚", assigns, show_due=True, is_assign=True))
    all_lines.extend(_section_lines("الكويزات", "📝", quizzes, show_due=True, is_assign=True))
    all_lines.extend(_section_lines("الملفات والموارد", "📎", resources, show_due=False, is_assign=False))

    graded_items = [g for g in grades if g.get("grade_val")]
    if graded_items:
        all_lines.append(f"🎯 <b>العلامات المرصودة ({len(graded_items)}):</b>")
        for g in graded_items:
            gname = escape_html(str(g.get("item_name", ""))[:70])
            gval = escape_html(g.get("grade_val", ""))
            gmax = f" / {escape_html(g['grade_max'])}" if g.get("grade_max") else ""
            all_lines.append(f"  • <b>{gname}</b>: <code>{gval}{gmax}</code>")
        all_lines.append("")

    if announcements:
        all_lines.append(f"📢 <b>إعلانات المقرر ({len(announcements)}):</b>")
        for ann in announcements[:5]:
            raw_atitle = str(ann.get("title", ""))[:75]
            aurl = ann.get("url", "")
            all_lines.append(f"  • {html_link(raw_atitle, aurl)}")
        all_lines.append("")

    if attendance:
        all_lines.append("📋 <b>سجل الحضور والغياب (Attendance):</b>")
        for att in attendance:
            taken = att.get("taken_sessions", 0)
            pres = att.get("present_count", 0)
            absn = att.get("absent_count", 0)
            pct = escape_html(att.get("percentage_str") or "—")
            aurl = att.get("att_url") or att.get("url", "")
            all_lines.append(
                f"  • {html_link('سجل المحاضرات', aurl)}: المحاضرات المسجلة <code>{taken}</code> "
                f"(✅ حاضر: <code>{pres}</code> | ❌ غائب: <code>{absn}</code> | النسبة: <code>{pct}</code>)"
            )
        all_lines.append("")

    footer = "━━━━━━━━━━━━━━━━━━━━━━\n🔔 سيتم إشعارك فور نزول أي محتوى جديد."

    messages = []
    current = header

    for line in all_lines:
        if len(current) + len(line) + 1 > MAX:
            messages.append(current.strip())
            current = f"📖 <b>{cname}</b> <i>(تابع)</i>\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + line + "\n"
        else:
            current += line + "\n"

    if len(current) + len(footer) + 2 <= MAX:
        current += "\n" + footer
        messages.append(current.strip())
    else:
        messages.append(current.strip())
        messages.append(f"📖 <b>{cname}</b>\n\n" + footer)

    return messages

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    q    = update.callback_query
    tid  = update.effective_user.id
    data = q.data
    user = await _run_blocking(db.get_user, tid)
    _answer_once = _make_answer_once(q)
    try:
        await _dispatch_callback(update, ctx, q, tid, data, user, _answer_once)
    finally:
        if not _answer_once.is_answered():
            try:
                await _answer_once()
            except Exception as e:
                logger.warning("Failed to answer callback query in finally: %s", e)


async def _dispatch_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    q,
    tid: int,
    data: Optional[str],
    user,
    _answer_once: Callable[..., Awaitable[None]],
):

    if data == "main_menu":
        await _show_main(q.message, tid, edit=True)

    elif data == "app_pair":
        await _send_app_pairing(q.message, tid, edit=True)

    elif data == "webapp_info":
        url = get_webapp_url()
        if url:
            await _send(q.message,
                f"📱 <b>لوحة التحكم الذكية (Telegram Mini App)</b>\n\n"
                f"اضغط على الرابط أدناه لفتح لوحة التحكم مباشرة:\n{html_link(url, url)}",
                _back_kb("main_menu"),
                edit=True
            )
        else:
            await _send(q.message,
                "📱 <b>لوحة التحكم الذكية (Telegram Mini App)</b>\n\n"
                "لتفعيل لوحة التحكم التفاعلية على هاتفك:\n"
                "• عند رفع البوت على Render، تُفعّل تلقائياً.\n"
                "• أو يمكنك ضبط رابط الدومين في ملف <code>.env</code>:\n"
                "<code>WEBAPP_URL=https://your-domain.com/webapp</code>",
                _back_kb("main_menu"),
                edit=True
            )

    elif data == "my_courses":
        await _render_courses(q.message, tid, edit=True)

    elif data == "sync_courses":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _answer_once()

        await _send(
            q.message,
            "⏳ <b>جارٍ الاتصال بـ LMS واستيراد موادك المسجلة...</b>\n\nيرجى الانتظار بضع لحظات.",
            edit=True
        )

        reminder_eng: ReminderEngine = ctx.bot_data.get("reminder_engine")

        def _fetch_all_courses():
            scraper = LMSScraper(user["lms_username"], user["lms_password"])
            if not scraper.login():
                err_msg = scraper.error_message or "فشل تسجيل الدخول إلى LMS"
                scraper.close()
                return None, err_msg
            courses_list = scraper.get_courses()
            return (courses_list, scraper), None

        result, err = await _run_blocking(_fetch_all_courses)
        if err or result is None:
            await _send(
                q.message,
                f"❌ <b>تعذر استيراد المواد تلقائياً!</b>\n\n"
                f"📌 <b>السبب:</b> {escape_html(err or 'حدث خطأ أثناء الاتصال بالجامعة')}\n\n"
                "يمكنك إضافة المواد يدوياً باستخدام رقم المادة (Course ID).",
                [
                    [InlineKeyboardButton("➕ إضافة مادة برقم ID", callback_data="add_course_start")],
                    [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
                ],
                edit=True
            )
            return

        courses_list, scraper = result

        try:
            if not courses_list:
                await _send(
                    q.message,
                    "⚠️ <b>لم يتم العثور على أي مواد مسجلة تلقائياً في صفحتك!</b>\n\n"
                    "قد لا تكون المواد مفعلة للفصل الحالي بعد في LMS، أو معروضة بنمط مختلف.\n\n"
                    "يمكنك إضافة المادة برقم الـ ID يدوياً بكل سهولة.",
                    [
                        [InlineKeyboardButton("➕ إضافة مادة برقم ID", callback_data="add_course_start")],
                        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
                    ],
                    edit=True
                )
                return

            def _sync_and_seed_all():
                existing_courses = {c["course_id"]: c for c in db.get_user_courses(tid)}
                newly_added_local = []
                already_present_local = []

                for c in courses_list:
                    cid = c["id"]
                    cname = c["name"]
                    curl = c["url"]
                    is_existing = cid in existing_courses
                    if is_existing:
                        already_present_local.append(cname)
                        if db.get_course_items(tid, cid):
                            continue
                    else:
                        db.add_course(tid, cid, cname, curl)
                        newly_added_local.append(cname)

                    try:
                        content = scraper.get_course_content(cid)
                    except Exception:
                        content = {}

                    _persist_course_snapshot_sync(
                        tid,
                        cid,
                        cname,
                        curl,
                        content,
                        reminder_eng,
                        add_course_record=False,
                    )
                return newly_added_local, already_present_local

            newly_added, already_present = await _run_blocking(_sync_and_seed_all)
        finally:
            await _run_blocking(scraper.close)

        lines = [
            "✅ <b>اكتملت مزامنة المواد بنجاح!</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 إجمالي المواد في LMS: <b>{len(courses_list)}</b>",
            f"✨ مواد جديدة أُضيفت: <b>{len(newly_added)}</b>",
        ]

        if newly_added:
            lines.append("\n📚 <b>المواد التي تمت إضافتها:</b>")
            for name in newly_added[:8]:
                lines.append(f"  • {escape_html(name)}")
            if len(newly_added) > 8:
                lines.append(f"  • ...و {len(newly_added) - 8} مواد إضافية")

        if already_present and not newly_added:
            lines.append("\nℹ️ <b>جميع موادك المسجلة كانت مضافة بالفعل مسبقاً!</b>")

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("يمكنك تصفح المحتويات أو تشغيل المراقبة لتصلك الإشعارات أولاً بأول:")

        next_action_btn = (
            InlineKeyboardButton("🟢 تشغيل المراقبة", callback_data="start_monitor")
            if not user.get("is_monitoring")
            else InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now")
        )

        buttons = [
            [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                next_action_btn,
            ],
            [
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ]
        ]

        await _send(q.message, "\n".join(lines), buttons, edit=True)

    elif data == "start_monitor":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
        if not courses:
            await _answer_once("لا توجد مواد مراقَبة!\nأضف مادة أولاً.", show_alert=True); return
        await _run_blocking(db.set_monitoring, tid, True)
        iv_txt = _format_interval(user.get("check_interval", 10))
        clist = "\n".join(f"  • <b>{escape_html(c['course_name'][:40])}</b>" for c in courses)
        await _send(q.message,
            f"🟢 <b>تم تفعيل المراقبة بنجاح!</b>\n\n"
            f"⏱️ الفحص الدوري: كل <b>{escape_html(iv_txt)}</b>\n\n"
            f"📚 <b>المواد المراقَبة ({len(courses)}):</b>\n{clist}\n\n"
            "🔔 ستصلك إشعارات فورية عند أي تحديث!",
            [
                [
                    InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
                    InlineKeyboardButton("🔴 إيقاف المراقبة", callback_data="stop_monitor"),
                ],
                [
                    InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True
        )

    elif data == "stop_monitor":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _run_blocking(db.set_monitoring, tid, False)
        await _send(q.message,
            "🔴 <b>تم إيقاف المراقبة المؤقتة.</b>\n\nلن تصلك إشعارات حتى تقوم بإعادة تشغيل المراقبة.",
            [
                [InlineKeyboardButton("🟢 تشغيل المراقبة", callback_data="start_monitor")],
                [
                    InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True
        )

    elif data == "deadlines":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return

        def _load_deadlines():
            c_list = db.get_user_courses(tid)
            up = db.get_upcoming_deadlines(tid, limit=50)
            d_map = {str(r["content_id"]): r for r in up}
            grouped = []
            tot = 0
            all_seen = db.get_all_seen_content(tid) if hasattr(db, "get_all_seen_content") else None
            for c in c_list:
                if all_seen is not None:
                    c_items = all_seen.get(c["course_id"]) or all_seen.get(str(c["course_id"])) or []
                else:
                    c_items = db.get_course_items(tid, c["course_id"])
                tasks = [it for it in c_items if it.get("content_type") in ("assign", "quiz")]
                if tasks:
                    grouped.append((c["course_name"], tasks))
                    tot += len(tasks)
            return grouped, tot, d_map

        grouped_tasks, total_tasks, due_map = await _run_blocking(_load_deadlines)

        if not grouped_tasks:
            await _send(q.message,
                "📅 <b>جدول الواجبات والكويزات حسب المادة</b>\n\n"
                "😌 لا توجد واجبات أو كويزات مسجّلة حالياً.\n\n"
                "<i>سيتم تحديث الجدول تلقائياً عند فحص المواد.</i>",
                [
                    [
                        InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ]
                ],
                edit=True
            )
            return

        lines = [f"📅 <b>الواجبات والكويزات مصنفة حسب المادة ({total_tasks} مهمة):</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for cname_raw, tasks in grouped_tasks:
            done_cnt = sum(1 for t in tasks if t.get("is_submitted"))
            pend_cnt = len(tasks) - done_cnt
            lines.append(
                f"\n📖 <b>{escape_html(cname_raw)}</b>\n"
                f"   📊 الإنجاز: <code>✅ {done_cnt} منجز</code> • <code>⏳ {pend_cnt} قيد الحل</code>"
            )
            for t in tasks:
                cid_str = str(t.get("content_id"))
                is_sub = bool(t.get("is_submitted"))
                icon = "✅" if is_sub else "⏳"
                st_txt = "منجز في LMS" if is_sub else "قيد الحل"
                t_label = "كويز" if t.get("content_type") == "quiz" else "واجب"
                raw_tname = t.get("content_name", "مهمة")
                turl = t.get("content_url", "")
                due_str = t.get("due_date") or (due_map[cid_str].get("due_str", "") if cid_str in due_map else "")
                due_part = f"\n      ⏰ <code>{escape_html(due_str)}</code>" if due_str else ""
                lines.append(
                    f"   {icon} {html_link(raw_tname, turl)} <i>({t_label} — {st_txt})</i>{due_part}"
                )

        await _send(q.message,
            "\n".join(lines),
            [
                [InlineKeyboardButton("⚡ فحص وتحديث حالة الإنجاز من LMS", callback_data="check_now")],
                [
                    InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True
        )

    elif data == "check_now":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
        if not courses:
            await _answer_once("لا توجد مواد مراقَبة!", show_alert=True); return
        await _answer_once()
        await _send(q.message, "🔍 <b>جارٍ الفحص الفوري في LMS...</b>", edit=True)
        engine: MonitorEngine = ctx.bot_data.get("engine")
        if engine:
            count, status = await engine.force_check(tid)
            result = (f"🔔 تم إرسال <b>{count}</b> إشعار جديد!" if count > 0
                      else "😴 لا يوجد محتوى جديد حالياً.")
            if status != "ok":
                result += f"\n\n⚠️ {escape_html(status)}"
            await _send(q.message,
                f"{'✅' if status == 'ok' else '⚠️'} <b>{'اكتمل الفحص' if status == 'ok' else 'الفحص غير مكتمل'}</b>\n\n{result}",
                [
                    [InlineKeyboardButton("⚡ فحص مجدداً", callback_data="check_now")],
                    [
                        InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ],
                ],
                edit=True
            )

    elif data == "stats":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid)
        mon     = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
        stats   = await _run_blocking(db.get_stats, tid)
        bt      = stats.get("by_type", {})
        last    = escape_html(user.get("last_check") or "لم يتم بعد")
        mon_st  = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
        uname   = escape_html(user.get("full_name") or user["lms_username"])
        std_id  = escape_html(user.get("lms_username") or "")

        iv_txt  = _format_interval(user.get("check_interval", 10))

        await _send(q.message,
            f"📊 <b>الإحصائيات والحالة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الطالب: <b>{uname}</b>\n"
            f"🆔 الرقم الجامعي: <code>{std_id}</code>\n"
            f"📡 المراقبة: {mon_st}\n"
            f"⏱️ فترة الفحص: كل <b>{escape_html(iv_txt)}</b>\n"
            f"⏰ آخر فحص: <code>{last}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📚 المواد المضافة: <b>{len(courses)}</b>\n"
            f"🔍 المواد المراقَبة: <b>{len(mon)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔔 <b>المحتوى المرصود:</b>\n"
            f"  📚 واجبات: <b>{bt.get('assign',0)}</b>\n"
            f"  📝 كويزات: <b>{bt.get('quiz',0)}</b>\n"
            f"  📎 موارد: <b>{bt.get('resource',0)}</b>\n"
            f"  ┗ المجموع: <b>{stats['total']}</b>",
            [
                [
                    InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
                    InlineKeyboardButton("📅 جدول المواعيد", callback_data="deadlines"),
                ],
                [
                    InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True
        )

    elif data == "grades_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _render_grades_view(q.message, tid, edit=True)

    elif data == "refresh_grades":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True); return
        await _answer_once()
        await _send(q.message, "⏳ <b>جارٍ فحص وتحديث سجل العلامات من LMS...</b>", edit=True)

        def _fetch_all_grades():
            sc = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not sc.login():
                    return False
                for c in courses:
                    cid = c["course_id"]
                    g_list = sc.get_course_grades(cid)
                    for g in g_list:
                        db.upsert_grade(
                            tid, cid, g["item_id"], g.get("item_name", ""),
                            g.get("grade_val", ""), g.get("grade_max", ""),
                            g.get("percentage", ""), g.get("feedback", ""),
                            g.get("item_url") or g.get("url", "")
                        )
                return True
            finally:
                sc.close()

        await _run_blocking(_fetch_all_grades)
        await _render_grades_view(q.message, tid, edit=True)

    elif data == "announcements_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _render_announcements_view(q.message, tid, edit=True)

    elif data == "refresh_announcements":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True); return
        await _answer_once()
        await _send(q.message, "⏳ <b>جارٍ فحص إعلانات الدكاترة ومنتديات المقررات...</b>", edit=True)

        def _fetch_all_anns():
            sc = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not sc.login():
                    return False
                for c in courses:
                    cid = c["course_id"]
                    a_list = sc.get_course_announcements(cid)
                    for ann in a_list[:8]:
                        existing = db.get_announcement(tid, ann["discuss_id"])
                        body = existing.get("body_text", "") if existing else ""
                        author = ann.get("author", "") or (existing.get("author", "") if existing else "")
                        pdate = ann.get("posted_date", "") or (existing.get("posted_date", "") if existing else "")
                        if not body and ann.get("url"):
                            det = sc.get_announcement_details(ann["url"])
                            body = det.get("body", "")
                            author = author or det.get("author", "")
                            pdate = pdate or det.get("date", "")
                        db.upsert_announcement(
                            tid, cid, ann["discuss_id"], ann.get("forum_name", "منتدى المقرر"),
                            ann.get("title", ""), author, pdate, body, ann.get("url", "")
                        )
                return True
            finally:
                sc.close()

        await _run_blocking(_fetch_all_anns)
        await _render_announcements_view(q.message, tid, edit=True)

    elif data == "attendance_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _render_attendance_view(q.message, tid, edit=True)

    elif data == "refresh_attendance":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True); return
        await _answer_once()
        await _send(q.message, "⏳ <b>جارٍ فحص وتحديث سجلات الحضور والغياب من LMS...</b>", edit=True)

        def _fetch_all_att():
            sc = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not sc.login():
                    return False
                for c in courses:
                    cid = c["course_id"]
                    att_list = sc.get_course_attendance(cid)
                    for att in att_list:
                        db.upsert_attendance(
                            tid, cid, att["att_id"],
                            taken_sessions=att.get("taken_sessions", 0),
                            total_sessions=att.get("total_sessions", 0),
                            present_count=att.get("present_count", 0),
                            absent_count=att.get("absent_count", 0),
                            late_count=att.get("late_count", 0),
                            excused_count=att.get("excused_count", 0),
                            points_str=att.get("points_str", ""),
                            percentage_str=att.get("percentage_str", ""),
                            sessions_json=json.dumps(att.get("sessions", []), ensure_ascii=False),
                            att_url=att.get("att_url") or att.get("url", ""),
                        )
                return True
            finally:
                sc.close()

        await _run_blocking(_fetch_all_att)
        await _render_attendance_view(q.message, tid, edit=True)

    elif data == "settings":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _render_settings(q.message, user, edit=True)

    elif data == "sett_assign":
        if user:
            await _run_blocking(db.update_notify_settings, tid, assign=not user.get("notify_assign", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "sett_quiz":
        if user:
            await _run_blocking(db.update_notify_settings, tid, quiz=not user.get("notify_quiz", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "sett_resource":
        if user:
            await _run_blocking(db.update_notify_settings, tid, resource=not user.get("notify_resource", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "sett_grade":
        if user:
            await _run_blocking(db.update_notify_settings, tid, grade=not user.get("notify_grade", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "sett_announcement":
        if user:
            await _run_blocking(db.update_notify_settings, tid, announcement=not user.get("notify_announcement", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "sett_attendance":
        if user:
            await _run_blocking(db.update_notify_settings, tid, attendance=not user.get("notify_attendance", 1))
            await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data.startswith("iv_"):
        minutes = validate_check_interval(data.split("_", 1)[1])
        if minutes is None:
            await _answer_once(
                f"⚠️ فترة الفحص يجب أن تكون بين {MIN_CHECK_INTERVAL} و {MAX_CHECK_INTERVAL} دقيقة.",
                show_alert=True,
            )
            return
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _run_blocking(db.update_check_interval, tid, minutes)
        iv_txt = _format_interval(minutes)
        await _answer_once(f"✅ تم ضبط فترة الفحص: كل {iv_txt}", show_alert=True)
        await _render_settings(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "logout_prompt":
        await _send(q.message,
            "⚠️ <b>تأكيد تسجيل الخروج</b>\n\n"
            "عند تسجيل الخروج سيتم حذف:\n"
            "• بيانات اعتماد حسابك من البوت\n"
            "• قائمة المواد المراقبة\n"
            "• سجل المحتويات المرصودة والتنبيهات\n\n"
            "هل أنت متأكد من رغبتك بالخروج؟",
            [
                [
                    InlineKeyboardButton("⚠️ نعم، تسجيل الخروج", callback_data="confirm_logout"),
                    InlineKeyboardButton("❌ إلغاء",            callback_data="main_menu"),
                ]
            ],
            edit=True
        )

    elif data == "confirm_logout":
        engine: MonitorEngine = ctx.bot_data.get("engine")
        if engine:
            if hasattr(engine, "invalidate_user_session_async"):
                await engine.invalidate_user_session_async(tid)
            else:
                await engine.stop_user(tid)
        await _run_blocking(db.delete_user, tid)
        await _send(q.message,
            "✅ <b>تم تسجيل الخروج بنجاح.</b>\n\nاضغط الزر أدناه للبدء من جديد:",
            [[InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")]],
            edit=True
        )

    elif data.startswith("view_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _answer_once()
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

    elif data.startswith("toggle_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.toggle_course, tid, course_id)
        await _answer_once("تم تغيير حالة المراقبة للمادة")
        await _render_courses(q.message, tid, edit=True)

    elif data.startswith("cToggle_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.toggle_course, tid, course_id)
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _answer_once("تم تغيير حالة المراقبة للمادة")
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

    elif data.startswith("del_"):
        course_id = int(data.split("_", 1)[1])
        course    = await _run_blocking(db.get_course, tid, course_id)
        if course:
            await _send(q.message,
                f"🗑 <b>تأكيد حذف المادة</b>\n\n"
                f"هل تريد حذف المادة:\n<b>{escape_html(course['course_name'])}</b>؟\n\n"
                "سيتم إيقاف مراقبتها وحذف سجل محتوياتها من البوت.",
                [
                    [
                        InlineKeyboardButton("🗑 نعم، احذف المادة", callback_data=f"confirmDel_{course_id}"),
                        InlineKeyboardButton("❌ إلغاء",          callback_data="my_courses"),
                    ]
                ],
                edit=True
            )

    elif data.startswith("confirmDel_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.remove_course, tid, course_id)
        await _answer_once("🗑 تم حذف المادة بنجاح", show_alert=True)
        await _render_courses(q.message, tid, edit=True)

    elif data == "clear_all_courses_prompt":
        await _send(q.message,
            "🗑 <b>تأكيد حذف جميع المواد</b>\n\n"
            "هل تريد حذف جميع المواد المسجلة حالياً من البوت لإدخال المواد التي تريد مراقبتها فقط؟\n"
            "<i>(سيبقى حسابك مسجلاً ولن تحتاج لإعادة تسجيل الدخول)</i>",
            [
                [
                    InlineKeyboardButton("🗑 نعم، احذف جميع المواد", callback_data="confirm_clear_all_courses"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="my_courses"),
                ]
            ],
            edit=True
        )

    elif data == "confirm_clear_all_courses":
        await _run_blocking(db.clear_all_user_courses, tid)
        await _answer_once("🗑 تم حذف جميع المواد بنجاح", show_alert=True)
        await _render_courses(q.message, tid, edit=True)

    elif data.startswith("refresh_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True); return
        await _answer_once()
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

async def _render_courses(msg_obj, tid, edit=False):
    courses = await _run_blocking(db.get_user_courses, tid)
    if not courses:
        await _send(msg_obj,
            "📭 <b>لا توجد مواد مضافة للمراقبة بعد!</b>\n\n"
            "اضغط على الزر أدناه لإضافة المواد التي تريد مراقبتها فقط (عبر رقم الـ ID أو لصق رابط المادة من LMS):",
            [
                [InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="add_course_start")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية",  callback_data="main_menu")],
            ],
            edit=edit
        )
        return

    lines = [
        "📚 <b>موادي المضافة للمراقبة</b>\n",
        "💡 <i>اضغط على اسم المادة لعرض محتوياتها بالكامل، أو تحكّم بها من الأزرار أدناها:</i>\n"
    ]
    keyboard = []
    for c in courses:
        mon = "🟢" if c["is_monitored"] else "🔴"
        st_text = "المراقبة نشطة" if c["is_monitored"] else "المراقبة متوقفة"
        name = escape_html(c["course_name"])
        cid = c["course_id"]
        lines.append(f"{mon} <b>{name}</b>\n   🆔 <code>{cid}</code> • <i>{st_text}</i>")

        short_name = c["course_name"][:25]
        toggle_label = "⏸ إيقاف" if c["is_monitored"] else "▶️ تفعيل"
        keyboard.append([
            InlineKeyboardButton(f"📖 {mon} {short_name} ({cid})", callback_data=f"view_{cid}")
        ])
        keyboard.append([
            InlineKeyboardButton(f"{toggle_label} المراقبة", callback_data=f"toggle_{cid}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"del_{cid}"),
        ])

    keyboard.append([
        InlineKeyboardButton("➕ إضافة مادة جديدة", callback_data="add_course_start"),
        InlineKeyboardButton("🗑 حذف جميع المواد", callback_data="clear_all_courses_prompt"),
    ])
    keyboard.append([
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])

    await _send(msg_obj, "\n".join(lines), keyboard, edit=edit)

async def _fetch_show_course(msg_obj, tid, user, course_id, edit=False):
    await _send(msg_obj, f"⏳ <b>جارٍ تحميل محتوى المادة <code>{course_id}</code>...</b>", edit=edit)

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    try:
        ok = await _run_blocking(scraper.login)

        if not ok:
            await _send(msg_obj, "❌ <b>فشل تسجيل الدخول.</b>", _back_kb("my_courses"), edit=True)
            return

        course_name, content = await _run_blocking(scraper.get_course_info, course_id)
    finally:
        await _run_blocking(scraper.close)

    if not course_name:
        await _send(msg_obj, f"❌ <b>المادة <code>{course_id}</code> غير موجودة.</b>",
                    _back_kb("my_courses"), edit=True)
        return

    await _run_blocking(
        _persist_course_snapshot_sync,
        tid,
        course_id,
        course_name,
        None,
        content,
        None,
        False,
    )

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])
    grades    = content.get("grades",     [])
    anns      = content.get("announcements", [])
    att_list  = content.get("attendance", [])

    course = await _run_blocking(db.get_course, tid, course_id)
    is_mon = course["is_monitored"] if course else True
    toggle_txt = "⏸ إيقاف المراقبة" if is_mon else "▶️ تفعيل المراقبة"

    sections = _build_course_sections(
        course_name, course_id, assigns, quizzes, resources,
        grades=grades, announcements=anns, attendance=att_list
    )
    keyboard = [
        [
            InlineKeyboardButton("🔄 تحديث المحتوى", callback_data=f"refresh_{course_id}"),
            InlineKeyboardButton(toggle_txt,           callback_data=f"cToggle_{course_id}"),
        ],
        [
            InlineKeyboardButton("🗑 حذف المادة",      callback_data=f"del_{course_id}"),
        ],
        [
            InlineKeyboardButton("📚 موادي المسجلة",   callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    await _send_sections(msg_obj, sections, keyboard=keyboard, edit_first=True)

async def _render_grades_view(msg_obj, tid, edit=False):
    all_grades = await _run_blocking(db.get_user_grades, tid)
    kb = [
        [InlineKeyboardButton("🔄 تحديث العلامات من LMS الآن", callback_data="refresh_grades")],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
        [
            InlineKeyboardButton("🚪 تسجيل الخروج من الحساب", callback_data="logout_prompt"),
        ],
    ]
    if not all_grades:
        await _send(
            msg_obj,
            "🎯 <b>رادار العلامات والتقييمات</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد علامات مرصودة في قاعدة البيانات حالياً.\n\n"
            "اضغط على الزر أدناه لسحب أحدث العلامات من نظام الجامعة:",
            kb,
            edit=edit
        )
        return

    grouped = {}
    for g in all_grades:
        grouped.setdefault(g.get("course_name", "مادة"), []).append(g)

    lines = ["🎯 <b>كشف العلامات والتقييمات الأكاديمية</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for cname, items in grouped.items():
        lines.append(f"\n📖 <b>{escape_html(cname)}:</b>")
        has_any = False
        for g in items:
            raw_iname = str(g.get("item_name", "تقييم"))[:65]
            iurl = g.get("item_url") or g.get("url", "")
            ilabel = html_link(raw_iname, iurl)
            gval = escape_html(g.get("grade_val") or "لم تُرصد بعد")
            gmax = f" / <code>{escape_html(g['grade_max'])}</code>" if g.get("grade_max") else ""
            pct = f" ({escape_html(g['percentage'])})" if g.get("percentage") else ""
            fb = f"\n      💬 <i>{escape_html(g['feedback'][:120])}</i>" if g.get("feedback") else ""
            icon = "✅" if g.get("grade_val") else "⏳"
            lines.append(f"  {icon} <b>{ilabel}</b>: <code>{gval}</code>{gmax}{pct}{fb}")
            has_any = True
        if not has_any:
            lines.append("  • لا توجد عناصر تقييم بعد")

    text = "\n".join(lines)
    await _send(msg_obj, text, kb, edit=edit)

async def _render_announcements_view(msg_obj, tid, edit=False):
    anns = await _run_blocking(db.get_user_announcements, tid, limit=15)
    kb = [
        [InlineKeyboardButton("🔄 فحص الإعلانات الجديدة من LMS", callback_data="refresh_announcements")],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
        [
            InlineKeyboardButton("🚪 تسجيل الخروج من الحساب", callback_data="logout_prompt"),
        ],
    ]
    if not anns:
        await _send(
            msg_obj,
            "📢 <b>رادار إعلانات الدكاترة ومنتديات المقررات</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد إعلانات مرصودة في منتديات موادك حتى الآن.\n\n"
            "اضغط على الزر أدناه لفحص منتديات الأخبار في LMS:",
            kb,
            edit=edit
        )
        return

    lines = ["📢 <b>أحدث إعلانات الدكاترة والمقررات</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for a in anns[:10]:
        cname = escape_html(a.get("course_name", "مادة"))
        raw_title = a.get("title", "إعلان")
        author = escape_html(a.get("author", ""))
        pdate = escape_html(a.get("posted_date", ""))
        body = escape_html((a.get("body_text") or "")[:240])
        url = a.get("url", "")
        meta = " • ".join(x for x in [author, pdate] if x)
        block = f"\n📌 {html_link(raw_title, url)}\n   📖 <b>{cname}</b>"
        if meta:
            block += f"\n   👨‍🏫 <code>{meta}</code>"
        if body:
            block += f"\n   📝 <i>{body}</i>"
        lines.append(block)

    text = "\n".join(lines)
    await _send(msg_obj, text, kb, edit=edit)

async def _render_attendance_view(msg_obj, tid, edit=False):
    att_rows = await _run_blocking(db.get_user_attendance, tid)
    kb = [
        [InlineKeyboardButton("🔄 تحديث الحضور والغياب من LMS الآن", callback_data="refresh_attendance")],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    if not att_rows:
        await _send(
            msg_obj,
            "📋 <b>قسم الحضور والغياب (Attendance)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد سجلات حضور وغياب مرصودة في موادك حالياً.\n\n"
            "اضغط على الزر أدناه لفحص موادك وسحب سجلات الحضور من LMS:",
            kb,
            edit=edit,
        )
        return

    lines = ["📋 <b>سجل الحضور والغياب في المواد (Attendance)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for row in att_rows:
        raw_cname = row.get("course_name", "مادة")
        taken = row.get("taken_sessions", 0)
        total = row.get("total_sessions", 0)
        pres = row.get("present_count", 0)
        absn = row.get("absent_count", 0)
        late = row.get("late_count", 0)
        excu = row.get("excused_count", 0)
        pts = escape_html(row.get("points_str") or "—")
        pct = escape_html(row.get("percentage_str") or "—")
        att_url = row.get("att_url") or row.get("url", "")

        sessions = []
        if row.get("sessions_json"):
            try:
                sessions = json.loads(row["sessions_json"])
            except Exception:
                sessions = []

        lines.append(
            f"\n📖 <b>{html_link(raw_cname, att_url)}</b>\n"
            f"   📊 المحاضرات المسجلة: <code>{taken}</code> من <code>{total}</code>\n"
            f"   ✅ حاضر: <code>{pres}</code>  |  ❌ غائب: <code>{absn}</code>"
            + (f"  |  ⏳ متأخر: <code>{late}</code>" if late else "")
            + (f"  |  📝 معذور: <code>{excu}</code>" if excu else "")
            + f"\n   📈 النسبة: <code>{pct}</code>  |  🎯 النقاط: <code>{pts}</code>"
        )
        if sessions:
            lines.append("   🗓 <b>سجل المحاضرات المسجلة:</b>")
            for s in sessions[-8:]:
                s_date = escape_html(s.get("date", ""))
                s_st = s.get("status", "")
                st_low = s_st.lower()
                if any(k in st_low for k in ("present", "حاضر", "موجود")):
                    s_icon = "✅"
                    s_lbl = "حاضر"
                elif any(k in st_low for k in ("absent", "غائب", "غياب")):
                    s_icon = "❌"
                    s_lbl = "غائب"
                elif any(k in st_low for k in ("late", "متأخر")):
                    s_icon = "⏳"
                    s_lbl = "متأخر"
                else:
                    s_icon = "📌"
                    s_lbl = escape_html(s_st)
                lines.append(f"      {s_icon} <code>{s_date}</code> — <b>{s_lbl}</b>")
        else:
            lines.append("   ℹ️ <i>لم يتم رصد محاضرات في هذا السجل حتى الآن</i>")

    text = "\n".join(lines)
    await _send(msg_obj, text, kb, edit=edit)

async def _render_settings(msg_obj, user, edit=False):
    a   = "🔔 مفعل" if user.get("notify_assign",       1) else "🔕 معطل"
    q   = "🔔 مفعل" if user.get("notify_quiz",         1) else "🔕 معطل"
    r   = "🔔 مفعل" if user.get("notify_resource",     1) else "🔕 معطل"
    g   = "🔔 مفعل" if user.get("notify_grade",        1) else "🔕 معطل"
    ann = "🔔 مفعل" if user.get("notify_announcement", 1) else "🔕 معطل"
    att = "🔔 مفعل" if user.get("notify_attendance",   1) else "🔕 معطل"
    iv  = user.get("check_interval", 10)
    iv_txt = _format_interval(iv)

    keyboard = [
        [
            InlineKeyboardButton(f"الواجبات: {a}", callback_data="sett_assign"),
            InlineKeyboardButton(f"الكويزات: {q}", callback_data="sett_quiz"),
        ],
        [
            InlineKeyboardButton(f"الملفات والموارد: {r}", callback_data="sett_resource"),
        ],
        [
            InlineKeyboardButton(f"🎯 رادار العلامات: {g}", callback_data="sett_grade"),
            InlineKeyboardButton(f"📋 الحضور والغياب: {att}", callback_data="sett_attendance"),
        ],
        [
            InlineKeyboardButton(f"📢 إعلانات الدكاترة: {ann}", callback_data="sett_announcement"),
        ],
        [
            InlineKeyboardButton(f"{'✔ ' if iv==5    else ''}5 د",  callback_data="iv_5"),
            InlineKeyboardButton(f"{'✔ ' if iv==10   else ''}10 د", callback_data="iv_10"),
            InlineKeyboardButton(f"{'✔ ' if iv==30   else ''}30 د", callback_data="iv_30"),
            InlineKeyboardButton(f"{'✔ ' if iv==60   else ''}1 س",  callback_data="iv_60"),
        ],
        [
            InlineKeyboardButton(f"{'✔ ' if iv==360  else ''}6 س",  callback_data="iv_360"),
            InlineKeyboardButton(f"{'✔ ' if iv==1440 else ''}24 س", callback_data="iv_1440"),
        ],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
        [
            InlineKeyboardButton("🚪 تسجيل الخروج من الحساب", callback_data="logout_prompt"),
        ],
    ]

    await _send(msg_obj,
        f"⚙️ <b>إعدادات المراقبة والإشعارات</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 <b>تنبيهات المحتوى:</b>\n"
        f"  • الواجبات: {a}\n"
        f"  • الكويزات: {q}\n"
        f"  • الملفات والموارد: {r}\n"
        f"  • رادار العلامات: {g}\n"
        f"  • رادار الحضور والغياب: {att}\n"
        f"  • إعلانات الدكاترة: {ann}\n\n"
        f"⏱️ <b>فترة الفحص الدوري:</b> كل <code>{escape_html(iv_txt)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>اضغط على أي زر لتفعيل/تعطيل الإشعار أو تحديد فترة الفحص:</i>",
        keyboard,
        edit=edit
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    await _send(update.message,
        "💡 <b>استخدم الأزرار للتنقل!</b>\n\n"
        "اضغط /start لفتح القائمة الرئيسية.",
        [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
    )

async def cmd_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    ctx.user_data.clear()
    tid = update.effective_user.id
    user = await _run_blocking(db.get_user, tid)
    if not user:
        await _send(
            update.message,
            "ℹ️ <b>أنت غير مسجل الدخول حالياً.</b>\n\nاضغط على الزر أدناه لتسجيل الدخول:",
            [[InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")]]
        )
        return ConversationHandler.END
    await _send(
        update.message,
        "⚠️ <b>تأكيد تسجيل الخروج من الجلسة</b>\n\n"
        "عند تسجيل الخروج سيتم إنهاء جلستك في LMS وحذف بيانات الحساب والمواد من البوت.\n"
        "هل أنت متأكد من رغبتك بتسجيل الخروج؟",
        [
            [
                InlineKeyboardButton("⚠️ نعم، تسجيل الخروج", callback_data="confirm_logout"),
                InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"),
            ]
        ]
    )
    return ConversationHandler.END

async def _send_app_pairing(msg_obj, tid, edit=False):
    """Give the owner a one-time code (and link) to pair the phone app."""
    url = get_webapp_url()
    if not url:
        await _send(msg_obj,
            "📲 <b>تطبيق الهاتف</b>\n\n"
            "رابط الخادم غير مضبوط. على Render يُضبط تلقائياً، أو أضف <code>WEBAPP_URL</code> في الإعدادات.",
            _back_kb("main_menu"), edit=edit)
        return
    code = await _run_blocking(create_pairing_code, tid)
    link = f"{url}?code={code}"
    await _send(msg_obj,
        "📲 <b>ربط تطبيق الهاتف</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"رمز الربط (صالح ١٠ دقائق، يُستخدم مرة واحدة):\n<code>{escape_html(code)}</code>\n\n"
        "<b>الطريقة ١ — الأسرع:</b> افتح الرابط التالي في <b>Chrome</b> (أندرويد) أو <b>Safari</b> (آيفون) "
        "وسيتم الربط تلقائياً، ثم ثبّته من قائمة المتصفح ← «إضافة إلى الشاشة الرئيسية»:\n"
        f"{html_link(link, link)}\n\n"
        f"<b>الطريقة ٢:</b> افتح <code>{escape_html(url)}</code> وأدخل الرمز يدوياً.\n\n"
        "💡 إن فتح تيليجرام الرابط داخل متصفحه الداخلي فاختر «فتح في المتصفح».\n"
        "🔔 التنبيهات ستبقى تصلك هنا في تيليجرام كما هي.",
        _back_kb("main_menu"), edit=edit)

async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    await _send_app_pairing(update.message, update.effective_user.id)
    return ConversationHandler.END

async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    n = await _run_blocking(db.revoke_all_app_sessions, update.effective_user.id)
    await _send(update.message,
        f"🔌 تم فصل جميع أجهزة التطبيق ({n}). لإعادة الربط أرسل /app.",
        _back_kb("main_menu"))
    return ConversationHandler.END

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 القائمة الرئيسية"),
        BotCommand("app", "📲 ربط تطبيق الهاتف"),
        BotCommand("unlink", "🔌 فصل أجهزة التطبيق"),
        BotCommand("logout", "🚪 تسجيل الخروج من الحساب"),
    ])
    rem_engine = ReminderEngine(app.bot)
    app.bot_data["reminder_engine"] = rem_engine
    engine = MonitorEngine(app.bot, rem_engine)
    if hasattr(engine, "attach_loop"):
        engine.attach_loop(asyncio.get_running_loop())
    app.bot_data["engine"] = engine
    await engine.start()
    await rem_engine.start()

    init_webapp_engines(engine, rem_engine)

    webapp_url = get_webapp_url()
    if webapp_url:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📱 لوحة التحكم", web_app=WebAppInfo(url=webapp_url))
            )
            logger.info(f"Telegram MenuButtonWebApp configured: {webapp_url}")
        except Exception as e:
            logger.warning(f"Could not configure chat menu button: {e}")

    logger.info("Bot ready — button-only mode!")

async def post_shutdown(app: Application):
    # Wait for in-flight background tasks spawned by bot handlers before stopping engines/DB.
    pending_tasks = [t for t in list(_BACKGROUND_TASKS) if not t.done()]
    if pending_tasks:
        _, still_pending = await asyncio.wait(pending_tasks, timeout=5.0)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    try:
        engine = app.bot_data.get("engine")
        if engine:
            await engine.stop()
    finally:
        try:
            rem_engine = app.bot_data.get("reminder_engine")
            if rem_engine:
                await rem_engine.stop()
        finally:
            if hasattr(db, "close"):
                try:
                    await _run_blocking(db.close)
                except Exception as exc:
                    logger.warning("Failed to close DB cleanly on shutdown: %s", exc)

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    # H4: Initialize database schema BEFORE starting the HTTP server thread.
    db.init_db()

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    async def fallback_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data.clear()
        await handle_callback(update, ctx)
        return ConversationHandler.END

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(login_start, pattern="^login_start$"),
            CallbackQueryHandler(add_course_start, pattern="^add_course_start$"),
        ],
        states={
            STATE_LOGIN_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_user)
            ],
            STATE_LOGIN_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_pass)
            ],
            STATE_ADD_COURSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_got_id)
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(login_start, pattern="^login_start$"),
            CallbackQueryHandler(add_course_start, pattern="^add_course_start$"),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            CallbackQueryHandler(fallback_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Starting bot (button-only mode)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
