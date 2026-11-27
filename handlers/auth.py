"""Authentication, session lifecycle, pairing, and fallback text handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from handlers.common import (
    STATE_LOGIN_PASS,
    STATE_LOGIN_USER,
    _back_kb,
    _ensure_user_menu_button,
    _is_owner,
    _on_telegram_forbidden,
    _reject_unauthorized,
    _run_blocking,
    _send,
    _track_background_task,
    get_engine,
    get_webapp_url,
    resolve_hook,
)
from handlers.views import _render_main_menu, _show_main
from lms_scraper import LMSScraper
from telegram_utils import escape_html, html_link, safe_edit_message_text_html
from webapp_server import create_pairing_code

logger = logging.getLogger("bot.handlers.auth")

_DEFAULT_SEND = _send
_DEFAULT_SHOW_MAIN = _show_main
_DEFAULT_LMS_SCRAPER = LMSScraper
_DEFAULT_ENSURE_MENU = _ensure_user_menu_button
_DEFAULT_GET_WEBAPP_URL = get_webapp_url


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    ctx.user_data.clear()
    show_main_fn = resolve_hook("_show_main", _show_main, _DEFAULT_SHOW_MAIN)
    await show_main_fn(update.message, update.effective_user.id)
    return ConversationHandler.END


async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    q = update.callback_query
    await q.answer()
    ctx.user_data["login_msg_id"] = q.message.message_id

    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        q.message,
        "🔑 <b>تسجيل الدخول</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل <b>رقم الطالب</b> (Student ID):",
        [[InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")]],
        edit=True,
    )
    return STATE_LOGIN_USER


async def login_got_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw_user = update.message.text.strip()
    ctx.user_data["lms_user"] = raw_user.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    try:
        await update.message.delete()
    except Exception as exc:
        logger.debug("Could not delete username message: %s", exc)

    tg_bot = update.get_bot()
    try:
        await safe_edit_message_text_html(
            tg_bot,
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
    except Exception as exc:
        logger.debug("Could not edit login prompt after username: %s", exc)
    return STATE_LOGIN_PASS


async def login_got_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
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
        # C10: Read password verbatim (never strip/translate/modify) and delete the message FIRST.
        if msg is not None and msg.text is not None:
            password = msg.text
        await _delete_password_message()

        username = ctx.user_data.get("lms_user", "")
        tid = update.effective_user.id

        if not username:
            tg_bot = update.get_bot()
            try:
                await safe_edit_message_text_html(
                    tg_bot,
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
            except Exception as exc:
                logger.debug("Could not edit expired-session login message: %s", exc)
            return ConversationHandler.END

        tg_bot = update.get_bot()
        try:
            await safe_edit_message_text_html(
                tg_bot,
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text="⏳ <b>جارٍ تسجيل الدخول إلى LMS...</b>",
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception as exc:
            logger.debug("Could not edit in-progress login message: %s", exc)

        # Fresh LMSScraper is intentionally used ONLY here to verify new credentials before saving.
        scraper_cls = resolve_hook("LMSScraper", LMSScraper, _DEFAULT_LMS_SCRAPER)
        scraper = scraper_cls(username, password)
        ok = await _run_blocking(scraper.login)

        if ok:
            await _run_blocking(db.save_user, tid, username, password, scraper.user_id, scraper.full_name)
            if hasattr(scraper, "export_session_state") and hasattr(db, "save_lms_session"):
                try:
                    state = scraper.export_session_state()
                    if isinstance(state, dict) and state.get("cookies"):
                        await _run_blocking(db.save_lms_session, tid, state)
                except Exception as sess_err:
                    logger.debug("Could not persist LMS session on login for %s: %s", tid, sess_err)
            ensure_fn = resolve_hook("_ensure_user_menu_button", _ensure_user_menu_button, _DEFAULT_ENSURE_MENU)
            _track_background_task(asyncio.create_task(ensure_fn(tg_bot, tid)))
            engine = get_engine(ctx)
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
                    tg_bot,
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
            except Exception as exc:
                logger.debug("Could not edit login-success message: %s", exc)
        else:
            err_detail = scraper.error_message or "تحقق من رقم الطالب وكلمة المرور وحاول مجدداً."
            await _run_blocking(scraper.close)
            try:
                await safe_edit_message_text_html(
                    tg_bot,
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
            except Exception as exc:
                logger.debug("Could not edit login-failure message: %s", exc)

        ctx.user_data.clear()
        return ConversationHandler.END
    finally:
        await _delete_password_message()


async def cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("تم الإلغاء")
    ctx.user_data.clear()
    show_main_fn = resolve_hook("_show_main", _render_main_menu, _DEFAULT_SHOW_MAIN)
    await show_main_fn(q.message, update.effective_user.id, edit=True)
    return ConversationHandler.END


async def handle_logout_prompt(msg_obj: Any, edit: bool = True) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
        "⚠️ <b>تأكيد تسجيل الخروج</b>\n\n"
        "عند تسجيل الخروج سيتم حذف:\n"
        "• بيانات اعتماد حسابك من البوت\n"
        "• قائمة المواد المراقبة\n"
        "• سجل المحتويات المرصودة والتنبيهات\n\n"
        "هل أنت متأكد من رغبتك بالخروج؟",
        [
            [
                InlineKeyboardButton("⚠️ نعم، تسجيل الخروج", callback_data="confirm_logout"),
                InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"),
            ]
        ],
        edit=edit,
    )


async def handle_confirm_logout(msg_obj: Any, tid: int, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    engine = get_engine(ctx)
    if engine:
        if hasattr(engine, "invalidate_user_session_async"):
            await engine.invalidate_user_session_async(tid)
        else:
            await engine.stop_user(tid)
    await _run_blocking(db.delete_user, tid)
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
        "✅ <b>تم تسجيل الخروج بنجاح.</b>\n\nاضغط الزر أدناه للبدء من جديد:",
        [[InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")]],
        edit=edit,
    )


async def cmd_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    ctx.user_data.clear()
    tid = update.effective_user.id
    user = await _run_blocking(db.get_user, tid)
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    if not user:
        await send_fn(
            update.message,
            "ℹ️ <b>أنت غير مسجل الدخول حالياً.</b>\n\nاضغط على الزر أدناه لتسجيل الدخول:",
            [[InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")]],
        )
        return ConversationHandler.END
    await send_fn(
        update.message,
        "⚠️ <b>تأكيد تسجيل الخروج من الجلسة</b>\n\n"
        "عند تسجيل الخروج سيتم إنهاء جلستك في LMS وحذف بيانات الحساب والمواد من البوت.\n"
        "هل أنت متأكد من رغبتك بتسجيل الخروج؟",
        [
            [
                InlineKeyboardButton("⚠️ نعم، تسجيل الخروج", callback_data="confirm_logout"),
                InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"),
            ]
        ],
    )
    return ConversationHandler.END


async def _send_app_pairing(msg_obj: Any, tid: int, edit: bool = False) -> None:
    """Give the owner a one-time code (and link) to pair the phone app."""
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    url = resolve_hook("get_webapp_url", get_webapp_url, _DEFAULT_GET_WEBAPP_URL)()
    if not url:
        await send_fn(
            msg_obj,
            "📲 <b>تطبيق الهاتف</b>\n\n"
            "رابط الخادم غير مضبوط. على Render يُضبط تلقائياً، أو أضف <code>WEBAPP_URL</code> في الإعدادات.",
            _back_kb("main_menu"),
            edit=edit,
        )
        return
    code = await _run_blocking(create_pairing_code, tid)
    link = f"{url}?code={code}"
    await send_fn(
        msg_obj,
        "📲 <b>ربط تطبيق الهاتف</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"رمز الربط (صالح ١٠ دقائق، يُستخدم مرة واحدة):\n<code>{escape_html(code)}</code>\n\n"
        "<b>الطريقة ١ — الأسرع:</b> افتح الرابط التالي في <b>Chrome</b> (أندرويد) أو <b>Safari</b> (آيفون) "
        "وسيتم الربط تلقائياً، ثم ثبّته من قائمة المتصفح ← «إضافة إلى الشاشة الرئيسية»:\n"
        f"{html_link(link, link)}\n\n"
        f"<b>الطريقة ٢:</b> افتح <code>{escape_html(url)}</code> وأدخل الرمز يدوياً.\n\n"
        "💡 إن فتح تيليجرام الرابط داخل متصفحه الداخلي فاختر «فتح في المتصفح».\n"
        "🔔 التنبيهات ستبقى تصلك هنا في تيليجرام كما هي.",
        _back_kb("main_menu"),
        edit=edit,
    )


async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    await _send_app_pairing(update.message, update.effective_user.id)
    return ConversationHandler.END


cmd_pair = cmd_app


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    n = await _run_blocking(db.revoke_all_app_sessions, update.effective_user.id)
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        update.message,
        f"🔌 تم فصل جميع أجهزة التطبيق ({n}). لإعادة الربط أرسل /app.",
        _back_kb("main_menu"),
    )
    return ConversationHandler.END


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        update.message,
        "💡 <b>استخدم الأزرار للتنقل!</b>\n\nاضغط /start لفتح القائمة الرئيسية.",
        [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]],
    )
