import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import asyncio
import time
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp
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
from config import TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID
from webapp_server import run_web_server, init_webapp_engines

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
    text = str(text)
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text

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
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("📥 استيراد موادي تلقائياً", callback_data="sync_courses"),
        ],
        [
            InlineKeyboardButton("🎯 كشف العلامات", callback_data="grades_menu"),
            InlineKeyboardButton("📢 إعلانات الدكاترة", callback_data="announcements_menu"),
        ],
        [
            mon_btn,
            InlineKeyboardButton("⚡ فحص فوري الآن", callback_data="check_now"),
        ],
        [
            InlineKeyboardButton("📅 جدول المواعيد",  callback_data="deadlines"),
            InlineKeyboardButton("➕ إضافة برقم ID", callback_data="add_course_start"),
        ],
        [
            InlineKeyboardButton("📊 الإحصائيات",     callback_data="stats"),
            InlineKeyboardButton("⚙️ إعدادات الإشعارات", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🚪 تسجيل الخروج",     callback_data="logout_prompt"),
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
    user = db.get_user(tid)
    try:
        bot = msg_obj.get_bot()
        asyncio.create_task(_ensure_user_menu_button(bot, tid))
    except Exception:
        pass
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

def _is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and int(u.id) == int(OWNER_TELEGRAM_ID))

async def _reject_unauthorized(update: Update):
    if update.callback_query:
        try:
            await update.callback_query.answer("🔒 هذا البوت خاص بصاحبه فقط.", show_alert=True)
        except Exception:
            pass
    elif update.message:
        try:
            await update.message.reply_text("🔒 هذا البوت خاص بصاحبه فقط وغير متاح للاستخدام العام.")
        except Exception:
            pass
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
    loop    = asyncio.get_running_loop()
    ok      = await loop.run_in_executor(None, scraper.login)

    if ok:
        db.save_user(tid, username, password, scraper.user_id, scraper.full_name)
        asyncio.create_task(_ensure_user_menu_button(bot, tid))
        engine: MonitorEngine = ctx.bot_data.get("engine")
        if engine:
            await engine.resume_user(tid, scraper)
        else:
            scraper.close()
        full = _esc(scraper.full_name or username)
        monitored_courses = db.get_user_courses(tid, monitored_only=True)
        registered_courses = db.get_user_courses(tid)
        if monitored_courses and db.get_user(tid).get("is_monitoring"):
            next_step = "موادك المراقبة محفوظة، وسيستأنف البوت متابعتها قريباً."
            actions = [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ]
        elif registered_courses:
            next_step = "موادك المسجلة محفوظة. يمكنك مراجعتها أو تشغيل المراقبة من القائمة الرئيسية."
            actions = [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ]
        else:
            next_step = "اضغط على الزر أدناه لاستيراد جميع موادك المسجلة تلقائياً بنقرة واحدة:"
            actions = [
                InlineKeyboardButton("📥 استيراد موادي تلقائياً", callback_data="sync_courses"),
                InlineKeyboardButton("➕ إضافة برقم ID", callback_data="add_course_start"),
            ]

        try:
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=ctx.user_data.get("login_msg_id"),
                text=(
                    f"✅ *تم تسجيل الدخول بنجاح!*\n\n"
                    f"👤 الطالب: *{full}*\n"
                    f"🆔 الرقم الجامعي: `{_esc(username)}`\n\n"
                    f"{next_step}"
                ),
                parse_mode=MD,
                reply_markup=InlineKeyboardMarkup([
                    actions
                ])
            )
        except Exception:
            pass
    else:
        err_detail = scraper.error_message or "تحقق من رقم الطالب وكلمة المرور وحاول مجدداً."
        scraper.close()
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
    if not _is_owner(update):
        return await _reject_unauthorized(update)
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
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=mid,
                text=(
                    "❌ *الرقم غير صحيح!*\n\n"
                    "أرسل رقم المادة مثل `12345` أو الصق رابط المادة مباشرة من LMS:\n"
                    "`https://mulms.mutah.edu.jo/course/view.php?id=12345`\n\n"
                    "أرسل رقم المادة أو الرابط مجدداً:"
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

    user = db.get_user(tid)
    if not user:
        ctx.user_data.clear()
        return ConversationHandler.END

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    loop    = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, scraper.login)

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
    finally:
        scraper.close()

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
    grades    = content.get("grades",     [])
    anns      = content.get("announcements", [])

    for item in assigns:
        db.mark_seen(tid, course_id, item["id"], "assign",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in quizzes:
        db.mark_seen(tid, course_id, item["id"], "quiz",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in resources:
        db.mark_seen(tid, course_id, item["id"], "resource",
                     item["name"], item["url"])
    for g in grades:
        db.upsert_grade(tid, course_id, g["item_id"], g.get("item_name", ""),
                        g.get("grade_val", ""), g.get("grade_max", ""),
                        g.get("percentage", ""), g.get("feedback", ""), g.get("url", ""))
    for ann in anns:
        db.upsert_announcement(tid, course_id, ann["discuss_id"], ann.get("forum_name", ""),
                               ann.get("title", ""), ann.get("author", ""),
                               ann.get("posted_date", ""), "", ann.get("url", ""))

    reminder_eng: ReminderEngine = ctx.bot_data.get("reminder_engine")
    if reminder_eng:
        reminder_eng.register_assignments(tid, course_id, assigns)

    for a in assigns:
        if a.get("is_submitted"):
            db.mark_assignment_submitted(tid, a["id"], 1)

    sections = _build_course_sections(
        course_name, course_id, assigns, quizzes, resources,
        grades=grades, announcements=anns, new_add=True
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
                            grades=None, announcements=None, new_add=False):
    MAX = 3500
    grades = grades or []
    announcements = announcements or []

    url   = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    cname = _esc(course_name)

    header = (
        f"{'✅ *تمت إضافة المادة بنجاح!*' if new_add else '📖 *تفاصيل المادة*'}\n\n"
        f"🎓 [{cname}]({url})\n"
        f"🆔 ID: `{course_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    def _section_lines(title, icon, items, key_name="name", key_url="url",
                       key_due="due_date", show_due=True, is_assign=False):
        if not items:
            return [f"{icon} *{title}:* لا يوجد حالياً\n"]
        lines = [f"{icon} *{title} ({len(items)}):*"]
        for item in items:
            name = _esc(str(item.get(key_name, ""))[:80])
            link = item.get(key_url, "")
            due  = ""
            sub_st = ""
            if show_due and item.get(key_due):
                due = f"\n      ⏰ `{item[key_due]}`"
            if is_assign:
                if item.get("is_submitted"):
                    sub_st = "\n      ✅ _تم التسليم للتقييم_"
                else:
                    sub_st = "\n      ⏳ _لم يتم التسليم بعد_"
            lines.append(f"  • [{name}]({link}){due}{sub_st}")
        lines.append("")
        return lines

    all_lines = []
    all_lines.extend(_section_lines("الواجبات", "📚", assigns, show_due=True, is_assign=True))
    all_lines.extend(_section_lines("الكويزات", "📝", quizzes, show_due=True, is_assign=False))
    all_lines.extend(_section_lines("الملفات والموارد", "📎", resources, show_due=False, is_assign=False))

    graded_items = [g for g in grades if g.get("grade_val")]
    if graded_items:
        all_lines.append(f"🎯 *العلامات المرصودة ({len(graded_items)}):*")
        for g in graded_items:
            gname = _esc(str(g.get("item_name", ""))[:70])
            gval = _esc(g.get("grade_val", ""))
            gmax = f" / {_esc(g['grade_max'])}" if g.get("grade_max") else ""
            all_lines.append(f"  • *{gname}*: `{gval}{gmax}`")
        all_lines.append("")

    if announcements:
        all_lines.append(f"📢 *إعلانات المقرر ({len(announcements)}):*")
        for ann in announcements[:5]:
            atitle = _esc(str(ann.get("title", ""))[:75])
            aurl = ann.get("url", "")
            all_lines.append(f"  • [{atitle}]({aurl})")
        all_lines.append("")

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
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    data = q.data
    user = db.get_user(tid)

    if data == "main_menu":
        await _show_main(q.message, tid, edit=True)

    elif data == "webapp_info":
        url = get_webapp_url()
        if url:
            await _send(q.message,
                f"📱 *لوحة التحكم الذكية (Telegram Mini App)*\n\n"
                f"اضغط على الرابط أدناه لفتح لوحة التحكم مباشرة:\n{url}",
                _back_kb("main_menu"),
                edit=True
            )
        else:
            await _send(q.message,
                "📱 *لوحة التحكم الذكية (Telegram Mini App)*\n\n"
                "لتفعيل لوحة التحكم التفاعلية على هاتفك:\n"
                "• عند رفع البوت على Render، تُفعّل تلقائياً.\n"
                "• أو يمكنك ضبط رابط الدومين في ملف `.env`:\n"
                "`WEBAPP_URL=https://your-domain.com/webapp`",
                _back_kb("main_menu"),
                edit=True
            )

    elif data == "my_courses":
        await _render_courses(q.message, tid, edit=True)

    elif data == "sync_courses":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True)
            return

        await _send(
            q.message,
            "⏳ *جارٍ الاتصال بـ LMS واستيراد موادك المسجلة...*\n\nيرجى الانتظار بضع لحظات.",
            edit=True
        )

        reminder_eng: ReminderEngine = ctx.bot_data.get("reminder_engine")
        loop = asyncio.get_running_loop()

        def _fetch_all_courses():
            scraper = LMSScraper(user["lms_username"], user["lms_password"])
            if not scraper.login():
                err_msg = scraper.error_message or "فشل تسجيل الدخول إلى LMS"
                scraper.close()
                return None, err_msg
            courses_list = scraper.get_courses()
            return (courses_list, scraper), None

        result, err = await loop.run_in_executor(None, _fetch_all_courses)
        if err or result is None:
            await _send(
                q.message,
                f"❌ *تعذر استيراد المواد تلقائياً!*\n\n"
                f"📌 *السبب:* {_esc(err or 'حدث خطأ أثناء الاتصال بالجامعة')}\n\n"
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
                    "⚠️ *لم يتم العثور على أي مواد مسجلة تلقائياً في صفحتك!*\n\n"
                    "قد لا تكون المواد مفعلة للفصل الحالي بعد في LMS، أو معروضة بنمط مختلف.\n\n"
                    "يمكنك إضافة المادة برقم الـ ID يدوياً بكل سهولة.",
                    [
                        [InlineKeyboardButton("➕ إضافة مادة برقم ID", callback_data="add_course_start")],
                        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
                    ],
                    edit=True
                )
                return

            existing_courses = {c["course_id"]: c for c in db.get_user_courses(tid)}
            newly_added = []
            already_present = []

            def _seed_course(cid):
                try:
                    return scraper.get_course_content(cid)
                except Exception:
                    return {}

            for c in courses_list:
                cid = c["id"]
                cname = c["name"]
                curl = c["url"]
                is_existing = cid in existing_courses
                if is_existing:
                    already_present.append(cname)
                    # Only re-seed if course has 0 seen items
                    if db.get_course_items(tid, cid):
                        continue
                else:
                    db.add_course(tid, cid, cname, curl)
                    newly_added.append(cname)

                content = await loop.run_in_executor(None, _seed_course, cid)
                assigns = content.get("assignments", [])
                quizzes = content.get("quizzes", [])
                resources = content.get("resources", [])
                grades = content.get("grades", [])
                anns = content.get("announcements", [])

                for item in assigns:
                    db.mark_seen(tid, cid, item["id"], "assign",
                                 item["name"], item["url"], item.get("due_date", ""))
                for item in quizzes:
                    db.mark_seen(tid, cid, item["id"], "quiz",
                                 item["name"], item["url"], item.get("due_date", ""))
                for item in resources:
                    db.mark_seen(tid, cid, item["id"], "resource",
                                 item["name"], item["url"])
                for g in grades:
                    db.upsert_grade(tid, cid, g["item_id"], g.get("item_name", ""),
                                    g.get("grade_val", ""), g.get("grade_max", ""),
                                    g.get("percentage", ""), g.get("feedback", ""), g.get("url", ""))
                for ann in anns:
                    db.upsert_announcement(tid, cid, ann["discuss_id"], ann.get("forum_name", ""),
                                           ann.get("title", ""), ann.get("author", ""),
                                           ann.get("posted_date", ""), "", ann.get("url", ""))

                if reminder_eng and assigns:
                    reminder_eng.register_assignments(tid, cid, assigns)

                for a in assigns:
                    if a.get("is_submitted"):
                        db.mark_assignment_submitted(tid, a["id"], 1)
        finally:
            scraper.close()

        lines = [
            "✅ *اكتملت مزامنة المواد بنجاح!*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 إجمالي المواد في LMS: *{len(courses_list)}*",
            f"✨ مواد جديدة أُضيفت: *{len(newly_added)}*",
        ]

        if newly_added:
            lines.append("\n📚 *المواد التي تمت إضافتها:*")
            for name in newly_added[:8]:
                lines.append(f"  • {_esc(name)}")
            if len(newly_added) > 8:
                lines.append(f"  • ...و {len(newly_added) - 8} مواد إضافية")

        if already_present and not newly_added:
            lines.append("\nℹ️ *جميع موادك المسجلة كانت مضافة بالفعل مسبقاً!*")

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
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid, monitored_only=True)
        if not courses:
            await q.answer("لا توجد مواد مراقَبة!\nأضف مادة أولاً.", show_alert=True); return
        db.set_monitoring(tid, True)
        iv_txt = _format_interval(user.get("check_interval", 10))
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
            is_sub = bool(r.get("is_submitted"))

            if is_sub:
                urgency = "✅"
                sub_badge = "✅ *تم التسليم بنجاح*"
                time_str = f"مسلّم ({days} يوم متبقي)" if days > 0 else "مسلّم"
            else:
                sub_badge = "⏳ *لم يتم التسليم بعد*"
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
                f"    📌 الحالة: {sub_badge}\n"
                f"    ⏰ `{due}`\n"
                f"    ⏳ الوقت المتبقي: *{time_str}*"
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
            if status != "ok":
                result += f"\n\n⚠️ {status}"
            await _send(q.message,
                f"{'✅' if status == 'ok' else '⚠️'} *{'اكتمل الفحص' if status == 'ok' else 'الفحص غير مكتمل'}*\n\n{result}",
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

        iv_txt  = _format_interval(user.get("check_interval", 10))

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

    elif data == "grades_menu":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await _render_grades_view(q.message, tid, edit=True)

    elif data == "refresh_grades":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid)
        if not courses:
            await q.answer("أضف موادك أولاً!", show_alert=True); return
        await _send(q.message, "⏳ *جارٍ فحص وتحديث سجل العلامات من LMS...*", edit=True)
        loop = asyncio.get_running_loop()

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
                            g.get("percentage", ""), g.get("feedback", ""), g.get("url", "")
                        )
                return True
            finally:
                sc.close()

        await loop.run_in_executor(None, _fetch_all_grades)
        await _render_grades_view(q.message, tid, edit=True)

    elif data == "announcements_menu":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        await _render_announcements_view(q.message, tid, edit=True)

    elif data == "refresh_announcements":
        if not user:
            await q.answer("سجّل دخولك أولاً!", show_alert=True); return
        courses = db.get_user_courses(tid)
        if not courses:
            await q.answer("أضف موادك أولاً!", show_alert=True); return
        await _send(q.message, "⏳ *جارٍ فحص إعلانات الدكاترة ومنتديات المقررات...*", edit=True)
        loop = asyncio.get_running_loop()

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

        await loop.run_in_executor(None, _fetch_all_anns)
        await _render_announcements_view(q.message, tid, edit=True)

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

    elif data == "sett_grade":
        if user:
            db.update_notify_settings(tid, grade=not user.get("notify_grade", 1))
            await _render_settings(q.message, db.get_user(tid), edit=True)

    elif data == "sett_announcement":
        if user:
            db.update_notify_settings(tid, announcement=not user.get("notify_announcement", 1))
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
        engine: MonitorEngine = ctx.bot_data.get("engine")
        if engine:
            await engine.stop_user(tid)
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
            "يمكنك استيراد جميع موادك تلقائياً بنقرة واحدة من LMS، أو إضافة مادة برقم الـ ID.",
            [
                [InlineKeyboardButton("📥 استيراد موادي تلقائياً", callback_data="sync_courses")],
                [InlineKeyboardButton("➕ إضافة مادة برقم ID", callback_data="add_course_start")],
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
        InlineKeyboardButton("📥 مزامنة المواد من LMS", callback_data="sync_courses"),
        InlineKeyboardButton("➕ إضافة برقم ID", callback_data="add_course_start"),
    ])
    keyboard.append([
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])

    await _send(msg_obj, "\n".join(lines), keyboard, edit=edit)

async def _fetch_show_course(msg_obj, tid, user, course_id, edit=False):
    await _send(msg_obj, f"⏳ *جارٍ تحميل محتوى المادة `{course_id}`...*", edit=edit)

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    loop    = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, scraper.login)

        if not ok:
            await _send(msg_obj, "❌ *فشل تسجيل الدخول.*", _back_kb("my_courses"), edit=True)
            return

        course_name, content = await loop.run_in_executor(
            None, scraper.get_course_info, course_id
        )
    finally:
        scraper.close()

    if not course_name:
        await _send(msg_obj, f"❌ *المادة `{course_id}` غير موجودة.*",
                    _back_kb("my_courses"), edit=True)
        return

    assigns   = content.get("assignments", [])
    quizzes   = content.get("quizzes",    [])
    resources = content.get("resources",  [])
    grades    = content.get("grades",     [])
    anns      = content.get("announcements", [])

    for item in assigns:
        db.mark_seen(tid, course_id, item["id"], "assign",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in quizzes:
        db.mark_seen(tid, course_id, item["id"], "quiz",
                     item["name"], item["url"], item.get("due_date", ""))
    for item in resources:
        db.mark_seen(tid, course_id, item["id"], "resource",
                     item["name"], item["url"])
    for g in grades:
        db.upsert_grade(tid, course_id, g["item_id"], g.get("item_name", ""),
                        g.get("grade_val", ""), g.get("grade_max", ""),
                        g.get("percentage", ""), g.get("feedback", ""), g.get("url", ""))
    for ann in anns:
        db.upsert_announcement(tid, course_id, ann["discuss_id"], ann.get("forum_name", ""),
                               ann.get("title", ""), ann.get("author", ""),
                               ann.get("posted_date", ""), "", ann.get("url", ""))

    for a in assigns:
        if a.get("is_submitted"):
            db.mark_assignment_submitted(tid, a["id"], 1)

    course = db.get_course(tid, course_id)
    is_mon = course["is_monitored"] if course else True
    toggle_txt = "⏸ إيقاف المراقبة" if is_mon else "▶️ تفعيل المراقبة"

    sections = _build_course_sections(
        course_name, course_id, assigns, quizzes, resources,
        grades=grades, announcements=anns
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
    all_grades = db.get_user_grades(tid)
    kb = [
        [InlineKeyboardButton("🔄 تحديث العلامات من LMS الآن", callback_data="refresh_grades")],
        [
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    if not all_grades:
        await _send(
            msg_obj,
            "🎯 *رادار العلامات والتقييمات*\n"
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

    lines = ["🎯 *كشف العلامات والتقييمات الأكاديمية*\n━━━━━━━━━━━━━━━━━━━━━━"]
    for cname, items in grouped.items():
        lines.append(f"\n📖 *{_esc(cname)}:*")
        has_any = False
        for g in items:
            iname = _esc(str(g.get("item_name", "تقييم"))[:65])
            gval = _esc(g.get("grade_val") or "لم تُرصد بعد")
            gmax = f" / `{_esc(g['grade_max'])}`" if g.get("grade_max") else ""
            pct = f" ({_esc(g['percentage'])})" if g.get("percentage") else ""
            fb = f"\n      💬 _{_esc(g['feedback'][:120])}_" if g.get("feedback") else ""
            icon = "✅" if g.get("grade_val") else "⏳"
            lines.append(f"  {icon} *{iname}*: `{gval}`{gmax}{pct}{fb}")
            has_any = True
        if not has_any:
            lines.append("  • لا توجد عناصر تقييم بعد")

    text = "\n".join(lines)[:3900]
    await _send(msg_obj, text, kb, edit=edit)

async def _render_announcements_view(msg_obj, tid, edit=False):
    anns = db.get_user_announcements(tid, limit=15)
    kb = [
        [InlineKeyboardButton("🔄 فحص الإعلانات الجديدة من LMS", callback_data="refresh_announcements")],
        [
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    if not anns:
        await _send(
            msg_obj,
            "📢 *رادار إعلانات الدكاترة ومنتديات المقررات*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد إعلانات مرصودة في منتديات موادك حتى الآن.\n\n"
            "اضغط على الزر أدناه لفحص منتديات الأخبار في LMS:",
            kb,
            edit=edit
        )
        return

    lines = ["📢 *أحدث إعلانات الدكاترة والمقررات*\n━━━━━━━━━━━━━━━━━━━━━━"]
    for a in anns[:10]:
        cname = _esc(a.get("course_name", "مادة"))
        title = _esc(a.get("title", "إعلان"))
        author = _esc(a.get("author", ""))
        pdate = _esc(a.get("posted_date", ""))
        body = _esc((a.get("body_text") or "")[:240])
        url = a.get("url", "")
        meta = " • ".join(x for x in [author, pdate] if x)
        block = f"\n📌 [{title}]({url})\n   📖 *{cname}*"
        if meta:
            block += f"\n   👨‍🏫 `{meta}`"
        if body:
            block += f"\n   📝 _{body}_"
        lines.append(block)

    text = "\n".join(lines)[:3900]
    await _send(msg_obj, text, kb, edit=edit)

async def _render_settings(msg_obj, user, edit=False):
    a   = "🔔 مفعل" if user.get("notify_assign",       1) else "🔕 معطل"
    q   = "🔔 مفعل" if user.get("notify_quiz",         1) else "🔕 معطل"
    r   = "🔔 مفعل" if user.get("notify_resource",     1) else "🔕 معطل"
    g   = "🔔 مفعل" if user.get("notify_grade",        1) else "🔕 معطل"
    ann = "🔔 مفعل" if user.get("notify_announcement", 1) else "🔕 معطل"
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
        f"  • الملفات والموارد: {r}\n"
        f"  • رادار العلامات: {g}\n"
        f"  • إعلانات الدكاترة: {ann}\n\n"
        f"⏱️ *فترة الفحص الدوري:* كل `{iv_txt}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_اضغط على أي زر لتفعيل/تعطيل الإشعار أو تحديد فترة الفحص:_",
        keyboard,
        edit=edit
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    await _send(update.message,
        "💡 *استخدم الأزرار للتنقل!*\n\n"
        "اضغط /start لفتح القائمة الرئيسية.",
        [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
    )

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 القائمة الرئيسية"),
    ])
    rem_engine = ReminderEngine(app.bot)
    app.bot_data["reminder_engine"] = rem_engine
    engine = MonitorEngine(app.bot, rem_engine)
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
    engine = app.bot_data.get("engine")
    if engine:
        await engine.stop()
    rem_engine = app.bot_data.get("reminder_engine")
    if rem_engine:
        await rem_engine.stop()

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    db.init_db()

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting bot (button-only mode)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
