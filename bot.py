import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import asyncio
import time
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardRemove, ForceReply
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, ConversationHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

import database as db
from lms_scraper import LMSScraper
from monitor import MonitorEngine
from reminder import ReminderEngine
from config import TELEGRAM_BOT_TOKEN

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

MD = ParseMode.MARKDOWN

(
    STATE_LOGIN_USER,
    STATE_LOGIN_PASS,
    STATE_ADD_COURSE,
) = range(3)

def _esc(text: str) -> str:
    if not text:
        return ""
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text

def _format_interval(minutes: int) -> str:
    minutes = int(minutes or 600)
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
    kw = dict(text=text, parse_mode=MD,
               reply_markup=kb, disable_web_page_preview=True)
    try:
        return await obj.edit_text(**kw) if edit else await obj.reply_text(**kw)
    except TelegramError as e:
        logger.warning(f"Markdown send failed: {e}. Retrying plain.")
        kw["parse_mode"] = None
        try:
            return await obj.edit_text(**kw) if edit else await obj.reply_text(**kw)
        except Exception as e2:
            logger.error(f"Plain send also failed: {e2}")

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
    return [
        [
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("➕ إضافة مادة",     callback_data="add_course_start"),
        ],
        [
            mon_btn,
            InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
        ],
        [
            InlineKeyboardButton("📅 جدول المواعيد",  callback_data="deadlines"),
            InlineKeyboardButton("📊 الإحصائيات",     callback_data="stats"),
        ],
        [
            InlineKeyboardButton("⚙️ إعدادات الإشعارات", callback_data="settings"),
            InlineKeyboardButton("🚪 تسجيل الخروج",     callback_data="logout_prompt"),
        ],
    ]

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

async def _show_main(msg_obj, tid, edit=False):
    user = db.get_user(tid)
    if user:
        courses = db.get_user_courses(tid)
        mon     = db.get_user_courses(tid, monitored_only=True)
        status  = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
        uname   = _esc(user.get("full_name") or user["lms_username"])
        std_id  = _esc(user.get("lms_username") or "")
        text = (
            f"🎓 *بوت مراقب LMS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الطالب: *{uname}*\n"
            f"🆔 الرقم الجامعي: `{std_id}`\n"
            f"📡 حالة المراقبة: {status}\n"
            f"📚 المواد المضافة: *{len(courses)}* (المراقَبة: *{len(mon)}*)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            "اختر من القائمة أدناه:"
        )
    else:
        text = (
            "🎓 *بوت مراقب نظام LMS*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "يُرسل إشعارات فورية عند نزول:\n"
            "📚 واجبات جديدة ومواعيدها\n"
            "📝 كويزات وامتحانات جديدة\n"
            "📎 سلايدات وملفات ومحاضرات\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ابدأ بتسجيل الدخول:"
        )
    await _send(msg_obj, text, _main_kb(user), edit=edit)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_main(update.message, update.effective_user.id)

async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["login_msg_id"] = q.message.message_id

    await _send(q.message,
        "🔑 *تسجيل الدخول*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل *رقم الطالب* (Student ID):",
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
        await bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ctx.user_data.get("login_msg_id"),
            text=(
                "🔑 *تسجيل الدخول*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ رقم الطالب: `{_esc(ctx.user_data['lms_user'])}`\n\n"
                "الآن أرسل *كلمة المرور*:"
            ),
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
            ]])
        )
    except Exception:
        pass
    return STATE_LOGIN_PASS

async def login_got_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_pass = update.message.text.strip()
    password = raw_pass.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    username = ctx.user_data.get("lms_user", "")
    tid      = update.effective_user.id

    if not username:
        bot = update.get_bot()
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text=(
                    "⚠️ *انتهت الجلسة المؤقتة!*\n\n"
                    "يرجى إعادة المحاولة من جديد بالضغط على الزر أدناه وإدخال رقم الطالب ثم كلمة المرور."
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 البدء من جديد", callback_data="login_start"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ]])
            )
        except Exception:
            pass
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    bot = update.get_bot()
    try:
        await bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ctx.user_data.get("login_msg_id"),
            text="⏳ *جارٍ تسجيل الدخول إلى LMS...*",
            parse_mode=MD,
        )
    except Exception:
        pass

    scraper = LMSScraper(username, password)
    loop    = asyncio.get_event_loop()
    ok      = await loop.run_in_executor(None, scraper.login)

    if ok:
        db.save_user(tid, username, password, scraper.user_id, scraper.full_name)
        full = _esc(scraper.full_name or username)

        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text=(
                    f"✅ *تم تسجيل الدخول بنجاح!*\n\n"
                    f"👤 الطالب: *{full}*\n"
                    f"🆔 الرقم الجامعي: `{_esc(username)}`\n\n"
                    "ابدأ الآن بإضافة المواد التي تريد مراقبتها:"
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("➕ إضافة مادة", callback_data="add_course_start"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ]
                ])
            )
        except Exception:
            pass
    else:
        err_detail = scraper.error_message or "تحقق من رقم الطالب وكلمة المرور وحاول مجدداً."
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text=(
                    "❌ *فشل تسجيل الدخول!*\n\n"
                    f"📌 *السبب:* {_esc(err_detail)}\n\n"
                    "يرجى التأكد من البيانات والمحاولة مجدداً."
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 إعادة المحاولة", callback_data="login_start"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ]])
            )
        except Exception:
            pass

    ctx.user_data.clear()
    return ConversationHandler.END

async def add_course_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    tid = update.effective_user.id
    await q.answer()

    if not db.get_user(tid):
        await q.answer("سجّل دخولك أولاً!", show_alert=True)
        return ConversationHandler.END

    ctx.user_data["add_course_msg_id"] = q.message.message_id

    await _send(q.message,
        "➕ *إضافة مادة للمراقبة*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 *كيف أجد رقم المادة (Course ID)?*\n"
        "افتح المادة في LMS وانظر للرابط:\n"
        "`course/view.php?id=`*XXXX*\n\n"
        "الرقم بعد `id=` هو ما تحتاجه.\n\n"
        "أرسل *رقم المادة (Course ID)* الآن:",
        [[InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")]],
        edit=True
    )
    return STATE_ADD_COURSE

async def add_course_got_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    raw = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    bot = update.get_bot()
    mid = ctx.user_data.get("add_course_msg_id")

    try:
        course_id = int(raw)
    except ValueError:
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=(
                    "❌ *الرقم غير صحيح!*\n\n"
                    "يجب أن يكون رقماً مثل: `12345`\n\n"
                    "أرسل رقم المادة مجدداً:"
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
                ]])
            )
        except Exception:
            pass
        return STATE_ADD_COURSE

    try:
        await bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=mid,
            text=f"⏳ *جارٍ جلب معلومات المادة `{course_id}`...*",
            parse_mode=MD,
        )
    except Exception:
        pass

    user     = db.get_user(tid)
    scraper  = LMSScraper(user["lms_username"], user["lms_password"])
    loop     = asyncio.get_event_loop()
    ok       = await loop.run_in_executor(None, scraper.login)

    if not ok:
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=mid,
                text="❌ *فشل تسجيل الدخول.* حاول لاحقاً.",
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup(_back_kb("main_menu"))
            )
        except Exception:
            pass
        ctx.user_data.clear()
        return ConversationHandler.END

    course_name, content = await loop.run_in_executor(
        None, scraper.get_course_info, course_id
    )

    if not course_name:
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=mid,
                text=(
                    f"❌ *لم يتم العثور على مادة بالـ ID: `{course_id}`*\n\n"
                    "تأكد من الرقم وأنك مسجّل في هذه المادة.\n\n"
                    "أرسل رقم مادة آخر:"
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")
                ]])
            )
        except Exception:
            pass
        return STATE_ADD_COURSE

    course_url = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    db.add_course(tid, course_id, course_name, course_url)

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])

    for item in assigns:
        db.mark_seen(tid, course_id, item["id"], "assign",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in quizzes:
        db.mark_seen(tid, course_id, item["id"], "quiz",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in resources:
        db.mark_seen(tid, course_id, item["id"], "resource",
                     item["name"], item["url"])

    reminder_eng: ReminderEngine = ctx.bot_data.get("reminder_engine")
    if reminder_eng:
        reminder_eng.register_assignments(tid, course_id, assigns)

    sections = _build_course_sections(course_name, course_id, assigns, quizzes, resources, new_add=True)
    kb = [
        [
            InlineKeyboardButton("➕ إضافة مادة أخرى", callback_data="add_course_start"),
            InlineKeyboardButton("📚 موادي المسجلة",   callback_data="my_courses"),
        ],
        [InlineKeyboardButton("🏠 القائمة الرئيسية",   callback_data="main_menu")],
    ]

    try:
        first_kb = InlineKeyboardMarkup(kb) if len(sections) == 1 else None
        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=sections[0],
                parse_mode=MD,
                disable_web_page_preview=True,
                reply_markup=first_kb,
            )
        except TelegramError:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=sections[0],
                parse_mode=None,
                disable_web_page_preview=True,
                reply_markup=first_kb,
            )

        for sec in sections[1:-1]:
            try:
                await bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=sec,
                    parse_mode=MD,
                    disable_web_page_preview=True,
                )
            except TelegramError:
                await bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=sec,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )

        if len(sections) > 1:
            try:
                await bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=sections[-1],
                    parse_mode=MD,
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            except TelegramError:
                await bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=sections[-1],
                    parse_mode=None,
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(kb),
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
                            new_add=False):
    MAX = 3500

    url   = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    cname = _esc(course_name)

    header = (
        f"{'✅ *تمت إضافة المادة بنجاح!*' if new_add else '📖 *تفاصيل المادة*'}\n\n"
        f"🎓 [{cname}]({url})\n"
        f"🆔 ID: `{course_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    def _section_lines(title, icon, items, key_name="name", key_url="url",
                       key_due="due_date", show_due=True):
        if not items:
            return [f"{icon} *{title}:* لا يوجد حالياً\n"]
        lines = [f"{icon} *{title} ({len(items)}):*"]
        for item in items:
            name = _esc(str(item.get(key_name, ""))[:80])
            link = item.get(key_url, "")
            due  = ""
            if show_due and item.get(key_due):
                due = f"\n      ⏰ `{item[key_due]}`"
            lines.append(f"  • [{name}]({link}){due}")
        lines.append("")
        return lines

    all_lines = []
    all_lines.extend(_section_lines("الواجبات", "📚", assigns, show_due=True))
    all_lines.extend(_section_lines("الكويزات", "📝", quizzes, show_due=True))
    all_lines.extend(_section_lines("الملفات والموارد", "📎", resources, show_due=False))

    footer = "━━━━━━━━━━━━━━━━━━━━━━\n🔔 سيتم إشعارك فور نزول أي محتوى جديد."

    messages = []
    current = header

    for line in all_lines:
        if len(current) + len(line) + 1 > MAX:
            messages.append(current.strip())
            current = f"📖 *{cname}* _(تابع)_\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + line + "\n"
        else:
            current += line + "\n"

    if len(current) + len(footer) + 2 <= MAX:
        current += "\n" + footer
        messages.append(current.strip())
    else:
        messages.append(current.strip())
        messages.append(f"📖 *{cname}*\n\n" + footer)

    return messages

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    data = q.data
    user = db.get_user(tid)

    if data == "main_menu":
        await _show_main(q.message, tid, edit=True)

    elif data == "my_courses":
        await _render_courses(q.message, tid, edit=True)

    elif data == "start_monitor":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid, monitored_only=True)
        if not courses:
            await q.answer("لا توجد مواد مراقَبة!\nأضف مادة أولاً.", show_alert=True); return
        db.set_monitoring(tid, True)
        iv_txt = _format_interval(user.get("check_interval", 600))
        clist = "\n".join(f"  • *{_esc(c['course_name'][:40])}*" for c in courses)
        await _send(q.message,
            f"🟢 *تم تفعيل المراقبة بنجاح!*\n\n"
            f"⏱️ الفحص الدوري: كل *{iv_txt}*\n\n"
            f"📚 *المواد المراقَبة ({len(courses)}):*\n{clist}\n\n"
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
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        db.set_monitoring(tid, False)
        await _send(q.message,
            "🔴 *تم إيقاف المراقبة المؤقتة.*\n\nلن تصلك إشعارات حتى تقوم بإعادة تشغيل المراقبة.",
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
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        upcoming = db.get_upcoming_deadlines(tid, limit=15)
        if not upcoming:
            await _send(q.message,
                "📅 *جدول المواعيد*\n\n"
                "😌 لا توجد واجبات قادمة مسجّلة بمواعيد حالياً.\n\n"
                "_سيتم تحديث الجدول تلقائياً عند اكتشاف واجبات جديدة._",
                [
                    [
                        InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                    ]
                ],
                edit=True
            )
            return

        lines = ["📅 *جدول المواعيد القادمة:*\n"]
        now_ts = int(time.time())
        for r in upcoming:
            diff = r["due_ts"] - now_ts
            days  = diff // 86400
            hours = (diff % 86400) // 3600

            if diff <= 3600:
                urgency = "💥"
                time_str = "ساعة أو أقل!"
            elif diff <= 10800:
                urgency = "🟠"
                time_str = f"{hours} ساعة"
            elif diff <= 86400:
                urgency = "🔴"
                time_str = f"{hours} ساعة"
            else:
                urgency = "🟡" if days <= 3 else "🟢"
                time_str = f"{days} يوم {'و' + str(hours) + ' ساعة' if hours else ''}"

            cname = _esc(r.get("course_name", "مادة"))
            aname = _esc(r.get("content_name", "واجب"))
            url   = r.get("content_url", "")
            due   = _esc(r.get("due_str", ""))

            lines.append(
                f"{urgency} [{aname}]({url})\n"
                f"    📖 {cname}\n"
                f"    ⏰ `{due}`\n"
                f"    ⏳ متبقي: *{time_str}*"
            )

        await _send(q.message,
            "\n\n".join(lines),
            [
                [InlineKeyboardButton("🔄 تحديث المواعيد", callback_data="deadlines")],
                [
                    InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True
        )

    elif data == "check_now":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid, monitored_only=True)
        if not courses:
            await q.answer("لا توجد مواد مراقَبة!", show_alert=True); return
        await _send(q.message, "🔍 *جارٍ الفحص الفوري في LMS...*", edit=True)
        engine: MonitorEngine = ctx.bot_data.get("engine")
        if engine:
            count, status = await engine.force_check(tid)
            result = (f"🔔 تم إرسال *{count}* إشعار جديد!" if count > 0
                      else "😴 لا يوجد محتوى جديد حالياً.")
            await _send(q.message,
                f"✅ *اكتمل الفحص!*\n\n{result}",
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
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid)
        mon     = db.get_user_courses(tid, monitored_only=True)
        stats   = db.get_stats(tid)
        bt      = stats.get("by_type", {})
        last    = _esc(user.get("last_check") or "لم يتم بعد")
        mon_st  = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
        uname   = _esc(user.get("full_name") or user["lms_username"])
        std_id  = _esc(user.get("lms_username") or "")

        iv_txt  = _format_interval(user.get("check_interval", 600))

        await _send(q.message,
            f"📊 *الإحصائيات والحالة*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الطالب: *{uname}*\n"
            f"🆔 الرقم الجامعي: `{std_id}`\n"
            f"📡 المراقبة: {mon_st}\n"
            f"⏱️ فترة الفحص: كل *{iv_txt}*\n"
            f"⏰ آخر فحص: `{last}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📚 المواد المضافة: *{len(courses)}*\n"
            f"🔍 المواد المراقَبة: *{len(mon)}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔔 *المحتوى المرصود:*\n"
            f"  📚 واجبات: *{bt.get('assign',0)}*\n"
            f"  📝 كويزات: *{bt.get('quiz',0)}*\n"
            f"  📎 موارد: *{bt.get('resource',0)}*\n"
            f"  ┗ المجموع: *{stats['total']}*",
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

    elif data == "settings":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await _render_settings(q.message, user, edit=True)

    elif data == "sett_assign":
        if user:
            db.update_notify_settings(tid, assign=not user.get("notify_assign", 1))
            await _render_settings(q.message, db.get_user(tid), edit=True)

    elif data == "sett_quiz":
        if user:
            db.update_notify_settings(tid, quiz=not user.get("notify_quiz", 1))
            await _render_settings(q.message, db.get_user(tid), edit=True)

    elif data == "sett_resource":
        if user:
            db.update_notify_settings(tid, resource=not user.get("notify_resource", 1))
            await _render_settings(q.message, db.get_user(tid), edit=True)

    elif data.startswith("iv_"):
        minutes = int(data.split("_")[1])
        if user:
            db.update_check_interval(tid, minutes)
            iv_txt = _format_interval(minutes)
            await q.answer(f"✅ تم ضبط فترة الفحص: كل {iv_txt}", show_alert=True)
            await _render_settings(q.message, db.get_user(tid), edit=True)

    elif data == "logout_prompt":
        await _send(q.message,
            "⚠️ *تأكيد تسجيل الخروج*\n\n"
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
        db.delete_user(tid)
        await _send(q.message,
            "✅ *تم تسجيل الخروج بنجاح.*\n\nاضغط الزر أدناه للبدء من جديد:",
            [[InlineKeyboardButton("🔑 تسجيل الدخول إلى LMS", callback_data="login_start")]],
            edit=True
        )

    elif data.startswith("view_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

    elif data.startswith("toggle_"):
        course_id = int(data.split("_", 1)[1])
        db.toggle_course(tid, course_id)
        await q.answer("تم تغيير حالة المراقبة للمادة")
        await _render_courses(q.message, tid, edit=True)

    elif data.startswith("cToggle_"):
        course_id = int(data.split("_", 1)[1])
        db.toggle_course(tid, course_id)
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await q.answer("تم تغيير حالة المراقبة للمادة")
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

    elif data.startswith("del_"):
        course_id = int(data.split("_", 1)[1])
        course    = db.get_course(tid, course_id)
        if course:
            await _send(q.message,
                f"🗑 *تأكيد حذف المادة*\n\n"
                f"هل تريد حذف المادة:\n*{_esc(course['course_name'])}*؟\n\n"
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
        db.remove_course(tid, course_id)
        await q.answer("🗑 تم حذف المادة بنجاح", show_alert=True)
        await _render_courses(q.message, tid, edit=True)

    elif data.startswith("refresh_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await _fetch_show_course(q.message, tid, user, course_id, edit=True)

async def _render_courses(msg_obj, tid, edit=False):
    courses = db.get_user_courses(tid)
    if not courses:
        await _send(msg_obj,
            "📭 *لا توجد مواد مضافة للمراقبة بعد!*\n\n"
            "اضغط *إضافة مادة جديدة* وأدخل رقم المادة (Course ID) من LMS.",
            [
                [InlineKeyboardButton("➕ إضافة مادة جديدة", callback_data="add_course_start")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية",  callback_data="main_menu")],
            ],
            edit=edit
        )
        return

    lines = [
        "📚 *موادي المسجلة في المراقبة*\n",
        "💡 _اضغط على اسم المادة لعرض محتوياتها بالكامل، أو تحكّم بها من الأزرار أدناها:_\n"
    ]
    keyboard = []
    for c in courses:
        mon = "🟢" if c["is_monitored"] else "🔴"
        st_text = "المراقبة نشطة" if c["is_monitored"] else "المراقبة متوقفة"
        name = _esc(c["course_name"])
        cid = c["course_id"]
        lines.append(f"{mon} *{name}*\n   🆔 `{cid}` • _{st_text}_")

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
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])

    await _send(msg_obj, "\n".join(lines), keyboard, edit=edit)

async def _fetch_show_course(msg_obj, tid, user, course_id, edit=False):
    await _send(msg_obj, f"⏳ *جارٍ تحميل محتوى المادة `{course_id}`...*", edit=edit)

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    loop    = asyncio.get_event_loop()
    ok      = await loop.run_in_executor(None, scraper.login)

    if not ok:
        await _send(msg_obj, "❌ *فشل تسجيل الدخول.*", _back_kb("my_courses"), edit=True)
        return

    course_name, content = await loop.run_in_executor(
        None, scraper.get_course_info, course_id
    )

    if not course_name:
        await _send(msg_obj, f"❌ *المادة `{course_id}` غير موجودة.*",
                    _back_kb("my_courses"), edit=True)
        return

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])

    course = db.get_course(tid, course_id)
    is_mon = course["is_monitored"] if course else True
    toggle_txt = "⏸ إيقاف المراقبة" if is_mon else "▶️ تفعيل المراقبة"

    sections = _build_course_sections(course_name, course_id, assigns, quizzes, resources)
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

async def _render_settings(msg_obj, user, edit=False):
    a  = "🔔 مفعل" if user.get("notify_assign",   1) else "🔕 معطل"
    q  = "🔔 مفعل" if user.get("notify_quiz",     1) else "🔕 معطل"
    r  = "🔔 مفعل" if user.get("notify_resource", 1) else "🔕 معطل"
    iv = user.get("check_interval", 600)
    iv_txt = _format_interval(iv)

    keyboard = [
        [
            InlineKeyboardButton(f"الواجبات: {a}", callback_data="sett_assign"),
        ],
        [
            InlineKeyboardButton(f"الكويزات: {q}", callback_data="sett_quiz"),
        ],
        [
            InlineKeyboardButton(f"الملفات والموارد: {r}", callback_data="sett_resource"),
        ],
        [
            InlineKeyboardButton(f"{'✔ ' if iv==30   else ''}30 د",  callback_data="iv_30"),
            InlineKeyboardButton(f"{'✔ ' if iv==60   else ''}1 س",   callback_data="iv_60"),
            InlineKeyboardButton(f"{'✔ ' if iv==360  else ''}6 س",   callback_data="iv_360"),
            InlineKeyboardButton(f"{'✔ ' if iv==600  else ''}10 س",  callback_data="iv_600"),
            InlineKeyboardButton(f"{'✔ ' if iv==1440 else ''}24 س",  callback_data="iv_1440"),
        ],
        [
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]

    await _send(msg_obj,
        f"⚙️ *إعدادات المراقبة والإشعارات*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *تنبيهات المحتوى:*\n"
        f"  • الواجبات: {a}\n"
        f"  • الكويزات: {q}\n"
        f"  • الملفات والموارد: {r}\n\n"
        f"⏱️ *فترة الفحص الدوري:* كل `{iv_txt}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_اضغط على أي زر لتفعيل/تعطيل الإشعار أو تحديد فترة الفحص:_",
        keyboard,
        edit=edit
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send(update.message,
        "💡 *استخدم الأزرار للتنقل!*\n\n"
        "اضغط /start لفتح القائمة الرئيسية.",
        [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
    )

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 القائمة الرئيسية"),
    ])
    engine = MonitorEngine(app.bot)
    app.bot_data["engine"] = engine
    await engine.start()
    rem_engine = ReminderEngine(app.bot)
    app.bot_data["reminder_engine"] = rem_engine
    await rem_engine.start()
    logger.info("Bot ready — button-only mode!")

async def post_shutdown(app: Application):
    engine = app.bot_data.get("engine")
    if engine:
        await engine.stop()
    rem_engine = app.bot_data.get("reminder_engine")
    if rem_engine:
        await rem_engine.stop()

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/test-lms"):
            import json
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            u = query.get("u", ["test_student"])[0]
            p = query.get("p", ["test_password"])[0]
            scraper = LMSScraper(u, p)
            ok = scraper.login()
            data = {
                "ok": ok,
                "error": scraper.error_message,
                "name": scraper.full_name,
                "cookies": list(scraper.session.cookies.keys()),
            }
            body = json.dumps(data, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        pass

def run_keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    keep_alive_thread = threading.Thread(target=run_keep_alive, daemon=True)
    keep_alive_thread.start()

    db.init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern="^login_start$")],
        states={
            STATE_LOGIN_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_user)
            ],
            STATE_LOGIN_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_pass)
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )

    add_course_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_course_start, pattern="^add_course_start$")],
        states={
            STATE_ADD_COURSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_got_id)
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(login_conv)
    app.add_handler(add_course_conv)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting bot (button-only mode)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
