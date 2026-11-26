"""Course management, interactive sync/refresh, and monitoring control handlers."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from handlers.common import (
    STATE_ADD_COURSE,
    _back_kb,
    _format_interval,
    _is_owner,
    _make_answer_once,
    _on_telegram_forbidden,
    _reject_unauthorized,
    _run_blocking,
    _send,
    get_engine,
    get_reminder_engine,
    resolve_hook,
)
from handlers.views import (
    _build_course_sections,
    _render_announcements_view,
    _render_attendance_view,
    _render_course_detail,
    _render_courses,
    _render_grades_view,
)
from telegram_utils import escape_html, safe_edit_message_text_html, send_html_message

logger = logging.getLogger("bot.handlers.courses")

_DEFAULT_SEND = _send
_DEFAULT_RENDER_COURSES = _render_courses
_DEFAULT_RENDER_GRADES = _render_grades_view
_DEFAULT_RENDER_ANNOUNCEMENTS = _render_announcements_view
_DEFAULT_RENDER_ATTENDANCE = _render_attendance_view


def _seed_course_snapshot_sync(
    tid: int,
    course_id: int,
    content: dict,
    *,
    course_name: str = "",
    course_url: str = "",
    reminder_engine: Any = None,
    add_course_record: bool = False,
) -> dict:
    """Persist a scraped course snapshot via services.sync_service.seed_course_snapshot."""
    from services import sync_service

    if add_course_record and course_name:
        db.add_course(tid, course_id, course_name, course_url or "")

    return sync_service.seed_course_snapshot(
        db,
        tid,
        course_id,
        content,
        course_name=course_name,
        mark_seeded=True,
        reminder_engine=reminder_engine,
    )


async def cmd_courses(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    render_courses_fn = resolve_hook("_render_courses", _render_courses, _DEFAULT_RENDER_COURSES)
    await render_courses_fn(update.message, update.effective_user.id, edit=False)
    return ConversationHandler.END


async def add_course_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    q = update.callback_query
    tid = update.effective_user.id
    _answer_once = _make_answer_once(q)

    try:
        if not await _run_blocking(db.get_user, tid):
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return ConversationHandler.END

        await _answer_once()
        ctx.user_data["add_course_msg_id"] = q.message.message_id

        send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
        await send_fn(
            q.message,
            "➕ <b>إضافة مادة للمراقبة</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>كيف أجد رقم المادة (Course ID)?</b>\n"
            "افتح المادة في LMS وانظر للرابط:\n"
            "<code>course/view.php?id=</code><b>XXXX</b>\n\n"
            "الرقم بعد <code>id=</code> هو ما تحتاجه.\n\n"
            "أرسل <b>رقم المادة (Course ID)</b> الآن:",
            [[InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_conv")]],
            edit=True,
        )
        return STATE_ADD_COURSE
    finally:
        if not _answer_once.is_answered():
            try:
                await _answer_once()
            except Exception as exc:
                logger.warning("Failed to answer add_course_start callback query: %s", exc)


async def add_course_got_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    raw = update.message.text.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

    try:
        await update.message.delete()
    except Exception as exc:
        logger.debug("Could not delete course-id message: %s", exc)

    tg_bot = update.get_bot()
    mid = ctx.user_data.get("add_course_msg_id")

    url_match = re.search(r"(?:id=|course=)(\d+)", raw)
    cid_str = url_match.group(1) if url_match else raw

    try:
        course_id = int(cid_str)
    except ValueError:
        try:
            await safe_edit_message_text_html(
                tg_bot,
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
        except Exception as exc:
            logger.debug("Could not edit invalid course-id message: %s", exc)
        return STATE_ADD_COURSE

    try:
        await safe_edit_message_text_html(
            tg_bot,
            chat_id=update.effective_chat.id,
            message_id=mid,
            text=f"⏳ <b>جارٍ جلب معلومات المادة <code>{course_id}</code>...</b>",
            on_forbidden=_on_telegram_forbidden,
        )
    except Exception as exc:
        logger.debug("Could not edit loading course message: %s", exc)

    user = await _run_blocking(db.get_user, tid)
    if not user:
        ctx.user_data.clear()
        return ConversationHandler.END

    engine = get_engine(ctx)
    if engine is None:
        try:
            await safe_edit_message_text_html(
                tg_bot,
                chat_id=update.effective_chat.id,
                message_id=mid,
                text="❌ <b>محرك المراقبة غير متاح حالياً.</b> حاول لاحقاً.",
                reply_markup=InlineKeyboardMarkup(_back_kb("main_menu")),
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception as exc:
            logger.debug("Could not edit missing-engine message: %s", exc)
        ctx.user_data.clear()
        return ConversationHandler.END

    try:
        result = await engine.run_with_user_scraper(tid, lambda sc: sc.get_course_info(course_id))
    except Exception as exc:
        logger.warning("Failed to fetch course %s via MonitorEngine: %s", course_id, exc)
        result = None

    if result is None:
        try:
            await safe_edit_message_text_html(
                tg_bot,
                chat_id=update.effective_chat.id,
                message_id=mid,
                text="❌ <b>فشل تسجيل الدخول إلى LMS.</b> حاول لاحقاً.",
                reply_markup=InlineKeyboardMarkup(_back_kb("main_menu")),
                on_forbidden=_on_telegram_forbidden,
            )
        except Exception as exc:
            logger.debug("Could not edit failed-login course message: %s", exc)
        ctx.user_data.clear()
        return ConversationHandler.END

    course_name, content = result
    if not course_name:
        try:
            await safe_edit_message_text_html(
                tg_bot,
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
        except Exception as exc:
            logger.debug("Could not edit course-not-found message: %s", exc)
        return STATE_ADD_COURSE

    course_url = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    reminder_eng = get_reminder_engine(ctx)
    await _run_blocking(
        _seed_course_snapshot_sync,
        tid,
        course_id,
        content,
        course_name=course_name,
        course_url=course_url,
        reminder_engine=reminder_eng,
        add_course_record=True,
    )

    sections = _build_course_sections(
        course_name,
        course_id,
        content.get("assignments", []),
        content.get("quizzes", []),
        content.get("resources", []),
        grades=content.get("grades", []),
        announcements=content.get("announcements", []),
        attendance=content.get("attendance", []),
        new_add=True,
    )
    kb = [
        [
            InlineKeyboardButton("➕ إضافة مادة أخرى", callback_data="add_course_start"),
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
        ],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ]

    try:
        first_kb = InlineKeyboardMarkup(kb) if len(sections) == 1 else None
        await safe_edit_message_text_html(
            tg_bot,
            chat_id=update.effective_chat.id,
            message_id=mid,
            text=sections[0],
            reply_markup=first_kb,
            disable_web_page_preview=True,
            on_forbidden=_on_telegram_forbidden,
        )

        for sec in sections[1:-1]:
            await send_html_message(
                tg_bot,
                update.effective_chat.id,
                sec,
                disable_web_page_preview=True,
                on_forbidden=_on_telegram_forbidden,
            )

        if len(sections) > 1:
            await send_html_message(
                tg_bot,
                update.effective_chat.id,
                sections[-1],
                reply_markup=InlineKeyboardMarkup(kb),
                disable_web_page_preview=True,
                on_forbidden=_on_telegram_forbidden,
            )
    except Exception as exc:
        logger.error("Error showing course summary: %s", exc)

    ctx.user_data.clear()
    return ConversationHandler.END


async def _fetch_show_course(
    msg_obj: Any,
    tid: int,
    user: dict,
    course_id: int,
    edit: bool = False,
    ctx: Optional[ContextTypes.DEFAULT_TYPE] = None,
) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, f"⏳ <b>جارٍ تحميل محتوى المادة <code>{course_id}</code>...</b>", edit=edit)

    engine = get_engine(ctx)
    if engine is None:
        await send_fn(msg_obj, "❌ <b>محرك المراقبة غير متاح حالياً.</b>", _back_kb("my_courses"), edit=True)
        return

    try:
        result = await engine.run_with_user_scraper(tid, lambda sc: sc.get_course_info(course_id))
    except Exception as exc:
        logger.warning("Failed to fetch course %s via MonitorEngine: %s", course_id, exc)
        result = None

    if result is None:
        await send_fn(msg_obj, "❌ <b>فشل تسجيل الدخول.</b>", _back_kb("my_courses"), edit=True)
        return

    course_name, content = result
    if not course_name:
        await send_fn(
            msg_obj,
            f"❌ <b>المادة <code>{course_id}</code> غير موجودة.</b>",
            _back_kb("my_courses"),
            edit=True,
        )
        return

    await _run_blocking(
        _seed_course_snapshot_sync,
        tid,
        course_id,
        content,
        course_name=course_name,
        reminder_engine=get_reminder_engine(ctx),
        add_course_record=False,
    )

    course = await _run_blocking(db.get_course, tid, course_id)
    is_mon = bool(course["is_monitored"]) if course else True
    await _render_course_detail(msg_obj, course_name, course_id, content, is_monitored=is_mon, edit_first=True)


async def handle_refresh_grades(
    msg_obj: Any, tid: int, courses: list[dict], ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, "⏳ <b>جارٍ فحص وتحديث سجل العلامات من LMS...</b>", edit=True)

    engine = get_engine(ctx)
    if engine is not None:
        def _sync_grades(sc: Any) -> bool:
            for c in courses:
                cid = c["course_id"]
                for g in sc.get_course_grades(cid):
                    db.upsert_grade(
                        tid,
                        cid,
                        g["item_id"],
                        g.get("item_name", ""),
                        g.get("grade_val", ""),
                        g.get("grade_max", ""),
                        g.get("percentage", ""),
                        g.get("feedback", ""),
                        g.get("item_url") or g.get("url", ""),
                    )
            return True

        try:
            await engine.run_with_user_scraper(tid, _sync_grades)
        except Exception as exc:
            logger.warning("Failed to refresh grades via MonitorEngine for %s: %s", tid, exc)

    render_fn = resolve_hook("_render_grades_view", _render_grades_view, _DEFAULT_RENDER_GRADES)
    await render_fn(msg_obj, tid, edit=True)


async def handle_refresh_announcements(
    msg_obj: Any, tid: int, courses: list[dict], ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, "⏳ <b>جارٍ فحص إعلانات الدكاترة ومنتديات المقررات...</b>", edit=True)

    engine = get_engine(ctx)
    if engine is not None:
        def _sync_anns(sc: Any) -> bool:
            for c in courses:
                cid = c["course_id"]
                for ann in sc.get_course_announcements(cid)[:8]:
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
                        tid,
                        cid,
                        ann["discuss_id"],
                        ann.get("forum_name", "منتدى المقرر"),
                        ann.get("title", ""),
                        author,
                        pdate,
                        body,
                        ann.get("url", ""),
                    )
            return True

        try:
            await engine.run_with_user_scraper(tid, _sync_anns)
        except Exception as exc:
            logger.warning("Failed to refresh announcements via MonitorEngine for %s: %s", tid, exc)

    render_fn = resolve_hook(
        "_render_announcements_view", _render_announcements_view, _DEFAULT_RENDER_ANNOUNCEMENTS
    )
    await render_fn(msg_obj, tid, edit=True)


async def handle_refresh_attendance(
    msg_obj: Any, tid: int, courses: list[dict], ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, "⏳ <b>جارٍ فحص وتحديث سجلات الحضور والغياب من LMS...</b>", edit=True)

    engine = get_engine(ctx)
    if engine is not None:
        def _sync_att(sc: Any) -> bool:
            for c in courses:
                cid = c["course_id"]
                for att in sc.get_course_attendance(cid):
                    db.upsert_attendance(
                        tid,
                        cid,
                        att["att_id"],
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

        try:
            await engine.run_with_user_scraper(tid, _sync_att)
        except Exception as exc:
            logger.warning("Failed to refresh attendance via MonitorEngine for %s: %s", tid, exc)

    render_fn = resolve_hook("_render_attendance_view", _render_attendance_view, _DEFAULT_RENDER_ATTENDANCE)
    await render_fn(msg_obj, tid, edit=True)


async def handle_start_monitor(
    msg_obj: Any,
    tid: int,
    user: Optional[dict],
    _answer_once: Callable[..., Awaitable[None]],
) -> None:
    if not user:
        await _answer_once("سجّل دخولك أولاً!", show_alert=True)
        return
    courses = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
    if not courses:
        await _answer_once("لا توجد مواد مراقَبة!\nأضف مادة أولاً.", show_alert=True)
        return
    await _run_blocking(db.set_monitoring, tid, True)
    iv_txt = _format_interval(user.get("check_interval", 10))
    clist = "\n".join(f"  • <b>{escape_html(c['course_name'][:40])}</b>" for c in courses)
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
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
        edit=True,
    )


async def handle_stop_monitor(
    msg_obj: Any,
    tid: int,
    user: Optional[dict],
    _answer_once: Callable[..., Awaitable[None]],
) -> None:
    if not user:
        await _answer_once("سجّل دخولك أولاً!", show_alert=True)
        return
    await _run_blocking(db.set_monitoring, tid, False)
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
        "🔴 <b>تم إيقاف المراقبة المؤقتة.</b>\n\nلن تصلك إشعارات حتى تقوم بإعادة تشغيل المراقبة.",
        [
            [InlineKeyboardButton("🟢 تشغيل المراقبة", callback_data="start_monitor")],
            [
                InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ],
        ],
        edit=True,
    )


async def handle_check_now(
    msg_obj: Any,
    tid: int,
    user: Optional[dict],
    ctx: ContextTypes.DEFAULT_TYPE,
    _answer_once: Callable[..., Awaitable[None]],
) -> None:
    if not user:
        await _answer_once("سجّل دخولك أولاً!", show_alert=True)
        return
    courses = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
    if not courses:
        await _answer_once("لا توجد مواد مراقَبة!", show_alert=True)
        return
    await _answer_once()
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, "🔍 <b>جارٍ الفحص الفوري في LMS...</b>", edit=True)
    engine = get_engine(ctx)
    if engine:
        count, status = await engine.force_check(tid)
        result = f"🔔 تم إرسال <b>{count}</b> إشعار جديد!" if count > 0 else "😴 لا يوجد محتوى جديد حالياً."
        if status != "ok":
            result += f"\n\n⚠️ {escape_html(status)}"
        await send_fn(
            msg_obj,
            f"{'✅' if status == 'ok' else '⚠️'} "
            f"<b>{'اكتمل الفحص' if status == 'ok' else 'الفحص غير مكتمل'}</b>\n\n{result}",
            [
                [InlineKeyboardButton("⚡ فحص مجدداً", callback_data="check_now")],
                [
                    InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ],
            ],
            edit=True,
        )


async def handle_delete_course_prompt(msg_obj: Any, tid: int, course_id: int) -> None:
    course = await _run_blocking(db.get_course, tid, course_id)
    if course:
        send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
        await send_fn(
            msg_obj,
            f"🗑 <b>تأكيد حذف المادة</b>\n\n"
            f"هل تريد حذف المادة:\n<b>{escape_html(course['course_name'])}</b>؟\n\n"
            "سيتم إيقاف مراقبتها وحذف سجل محتوياتها من البوت.",
            [
                [
                    InlineKeyboardButton("🗑 نعم، احذف المادة", callback_data=f"confirmDel_{course_id}"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="my_courses"),
                ]
            ],
            edit=True,
        )


async def handle_clear_all_courses_prompt(msg_obj: Any) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
        "🗑 <b>تأكيد حذف جميع المواد</b>\n\n"
        "هل تريد حذف جميع المواد المسجلة حالياً من البوت لإدخال المواد التي تريد مراقبتها فقط؟\n"
        "<i>(سيبقى حسابك مسجلاً ولن تحتاج لإعادة تسجيل الدخول)</i>",
        [
            [
                InlineKeyboardButton("🗑 نعم، احذف جميع المواد", callback_data="confirm_clear_all_courses"),
                InlineKeyboardButton("❌ إلغاء", callback_data="my_courses"),
            ]
        ],
        edit=True,
    )
