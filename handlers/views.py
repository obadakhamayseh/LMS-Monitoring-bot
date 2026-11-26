"""Telegram HTML view renderers for menus, courses, grades, attendance, deadlines, settings, and help."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from handlers.common import (
    _back_kb,
    _ensure_user_menu_button,
    _format_interval,
    _is_owner,
    _main_kb,
    _reject_unauthorized,
    _run_blocking,
    _send,
    _send_sections,
    _track_background_task,
    get_webapp_url,
    resolve_hook,
)
from services.observability import get_observability
from telegram_utils import escape_html, html_link

logger = logging.getLogger("bot.handlers.views")
JORDAN_TZ = ZoneInfo("Asia/Amman")

_DEFAULT_SEND = _send
_DEFAULT_SEND_SECTIONS = _send_sections
_DEFAULT_ENSURE_MENU = _ensure_user_menu_button
_DEFAULT_GET_WEBAPP_URL = get_webapp_url


async def _render_main_menu(msg_obj: Any, tid: int, edit: bool = False) -> None:
    user = await _run_blocking(db.get_user, tid)
    try:
        tg_bot = msg_obj.get_bot()
        ensure_fn = resolve_hook("_ensure_user_menu_button", _ensure_user_menu_button, _DEFAULT_ENSURE_MENU)
        _track_background_task(asyncio.create_task(ensure_fn(tg_bot, tid)))
    except Exception as exc:
        logger.debug("Skipping chat menu button refresh for %s: %s", tid, exc)

    if user:
        courses = await _run_blocking(db.get_user_courses, tid)
        mon = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
        status = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
        uname = escape_html(user.get("full_name") or user["lms_username"])
        std_id = escape_html(user.get("lms_username") or "")
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

    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(msg_obj, text, _main_kb(user), edit=edit)


# Backward-compatible alias used by callbacks and existing tests
_show_main = _render_main_menu


async def _render_webapp_info(msg_obj: Any, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    url = resolve_hook("get_webapp_url", get_webapp_url, _DEFAULT_GET_WEBAPP_URL)()
    if url:
        await send_fn(
            msg_obj,
            f"📱 <b>لوحة التحكم الذكية (Telegram Mini App)</b>\n\n"
            f"اضغط على الرابط أدناه لفتح لوحة التحكم مباشرة:\n{html_link(url, url)}",
            _back_kb("main_menu"),
            edit=edit,
        )
    else:
        await send_fn(
            msg_obj,
            "📱 <b>لوحة التحكم الذكية (Telegram Mini App)</b>\n\n"
            "لتفعيل لوحة التحكم التفاعلية على هاتفك:\n"
            "• عند رفع البوت على Render، تُفعّل تلقائياً.\n"
            "• أو يمكنك ضبط رابط الدومين في ملف <code>.env</code>:\n"
            "<code>WEBAPP_URL=https://your-domain.com/webapp</code>",
            _back_kb("main_menu"),
            edit=edit,
        )


def _build_course_sections(
    course_name: str,
    course_id: int,
    assigns: list,
    quizzes: list,
    resources: list,
    grades: list | None = None,
    announcements: list | None = None,
    attendance: list | None = None,
    new_add: bool = False,
) -> list[str]:
    max_len = 3500
    grades = grades or []
    announcements = announcements or []
    attendance = attendance or []

    url = f"https://mulms.mutah.edu.jo/course/view.php?id={course_id}"
    cname = escape_html(course_name)

    header = (
        f"{'✅ <b>تمت إضافة المادة بنجاح!</b>' if new_add else '📖 <b>تفاصيل المادة</b>'}\n\n"
        f"🎓 {html_link(course_name, url)}\n"
        f"🆔 ID: <code>{course_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    def _section_lines(
        title: str,
        icon: str,
        items: list,
        key_name: str = "name",
        key_url: str = "url",
        key_due: str = "due_date",
        show_due: bool = True,
        is_assign: bool = False,
    ) -> list[str]:
        if not items:
            return [f"{icon} <b>{escape_html(title)}:</b> لا يوجد حالياً\n"]
        lines = [f"{icon} <b>{escape_html(title)} ({len(items)}):</b>"]
        for item in items:
            raw_name = str(item.get(key_name, ""))[:80]
            link = item.get(key_url, "")
            due = ""
            sub_st = ""
            if show_due and item.get(key_due):
                due = f"\n      ⏰ <code>{escape_html(item[key_due])}</code>"
            if is_assign:
                is_quiz_type = item.get("type") == "quiz" or str(item.get("id", "")).startswith("quiz_")
                if item.get("is_submitted"):
                    sub_st = (
                        "\n      ✅ <i>منجز (تم الحل في LMS)</i>"
                        if is_quiz_type
                        else "\n      ✅ <i>منجز (تم التسليم في LMS)</i>"
                    )
                else:
                    sub_st = (
                        "\n      ⏳ <i>لم يتم الحل بعد</i>"
                        if is_quiz_type
                        else "\n      ⏳ <i>لم يتم التسليم بعد</i>"
                    )
            lines.append(f"  • {html_link(raw_name, link)}{due}{sub_st}")
        lines.append("")
        return lines

    all_lines: list[str] = []
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

    messages: list[str] = []
    current = header
    for line in all_lines:
        if len(current) + len(line) + 1 > max_len:
            messages.append(current.strip())
            current = f"📖 <b>{cname}</b> <i>(تابع)</i>\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + line + "\n"
        else:
            current += line + "\n"

    if len(current) + len(footer) + 2 <= max_len:
        current += "\n" + footer
        messages.append(current.strip())
    else:
        messages.append(current.strip())
        messages.append(f"📖 <b>{cname}</b>\n\n" + footer)

    return messages


async def _render_courses_view(msg_obj: Any, tid: int, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    courses = await _run_blocking(db.get_user_courses, tid)
    if not courses:
        await send_fn(
            msg_obj,
            "📭 <b>لا توجد مواد مضافة للمراقبة بعد!</b>\n\n"
            "اضغط على الزر أدناه لإضافة المواد التي تريد مراقبتها فقط (عبر رقم الـ ID أو لصق رابط المادة من LMS):",
            [
                [InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="add_course_start")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
            ],
            edit=edit,
        )
        return

    lines = [
        "📚 <b>موادي المضافة للمراقبة</b>\n",
        "💡 <i>اضغط على اسم المادة لعرض محتوياتها بالكامل، أو تحكّم بها من الأزرار أدناها:</i>\n",
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

    await send_fn(msg_obj, "\n".join(lines), keyboard, edit=edit)


# Backward-compatible alias
_render_courses = _render_courses_view


async def _render_course_detail(
    msg_obj: Any,
    course_name: str,
    course_id: int,
    content: dict,
    is_monitored: bool = True,
    *,
    edit_first: bool = True,
) -> None:
    assigns = content.get("assignments", [])
    quizzes = content.get("quizzes", [])
    resources = content.get("resources", [])
    grades = content.get("grades", [])
    anns = content.get("announcements", [])
    att_list = content.get("attendance", [])

    toggle_txt = "⏸ إيقاف المراقبة" if is_monitored else "▶️ تفعيل المراقبة"
    sections = _build_course_sections(
        course_name,
        course_id,
        assigns,
        quizzes,
        resources,
        grades=grades,
        announcements=anns,
        attendance=att_list,
    )
    keyboard = [
        [
            InlineKeyboardButton("🔄 تحديث المحتوى", callback_data=f"refresh_{course_id}"),
            InlineKeyboardButton(toggle_txt, callback_data=f"cToggle_{course_id}"),
        ],
        [
            InlineKeyboardButton("🗑 حذف المادة", callback_data=f"del_{course_id}"),
        ],
        [
            InlineKeyboardButton("📚 موادي المسجلة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    send_sections_fn = resolve_hook("_send_sections", _send_sections, _DEFAULT_SEND_SECTIONS)
    await send_sections_fn(msg_obj, sections, keyboard=keyboard, edit_first=edit_first)


async def _render_deadlines_view(msg_obj: Any, tid: int, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)

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
        await send_fn(
            msg_obj,
            "📅 <b>جدول الواجبات والكويزات حسب المادة</b>\n\n"
            "😌 لا توجد واجبات أو كويزات مسجّلة حالياً.\n\n"
            "<i>سيتم تحديث الجدول تلقائياً عند فحص المواد.</i>",
            [
                [
                    InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                    InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
                ]
            ],
            edit=edit,
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

    await send_fn(
        msg_obj,
        "\n".join(lines),
        [
            [InlineKeyboardButton("⚡ فحص وتحديث حالة الإنجاز من LMS", callback_data="check_now")],
            [
                InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ],
        ],
        edit=edit,
    )


async def _render_stats_view(msg_obj: Any, tid: int, user: dict, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    courses = await _run_blocking(db.get_user_courses, tid)
    mon = await _run_blocking(db.get_user_courses, tid, monitored_only=True)
    stats = await _run_blocking(db.get_stats, tid)
    bt = stats.get("by_type", {})
    last = escape_html(user.get("last_check") or "لم يتم بعد")
    mon_st = "🟢 نشطة" if user["is_monitoring"] else "🔴 متوقفة"
    uname = escape_html(user.get("full_name") or user["lms_username"])
    std_id = escape_html(user.get("lms_username") or "")
    iv_txt = _format_interval(user.get("check_interval", 10))

    await send_fn(
        msg_obj,
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
        f"  📚 واجبات: <b>{bt.get('assign', 0)}</b>\n"
        f"  📝 كويزات: <b>{bt.get('quiz', 0)}</b>\n"
        f"  📎 موارد: <b>{bt.get('resource', 0)}</b>\n"
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
        edit=edit,
    )


async def _render_grades_view(msg_obj: Any, tid: int, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
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
        await send_fn(
            msg_obj,
            "🎯 <b>رادار العلامات والتقييمات</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد علامات مرصودة في قاعدة البيانات حالياً.\n\n"
            "اضغط على الزر أدناه لسحب أحدث العلامات من نظام الجامعة:",
            kb,
            edit=edit,
        )
        return

    grouped: dict[str, list[dict]] = {}
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

    await send_fn(msg_obj, "\n".join(lines), kb, edit=edit)


async def _render_announcements_view(msg_obj: Any, tid: int, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
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
        await send_fn(
            msg_obj,
            "📢 <b>رادار إعلانات الدكاترة ومنتديات المقررات</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "لا توجد إعلانات مرصودة في منتديات موادك حتى الآن.\n\n"
            "اضغط على الزر أدناه لفحص منتديات الأخبار في LMS:",
            kb,
            edit=edit,
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

    await send_fn(msg_obj, "\n".join(lines), kb, edit=edit)


async def _render_attendance_view(msg_obj: Any, tid: int, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    att_rows = await _run_blocking(db.get_user_attendance, tid)
    kb = [
        [InlineKeyboardButton("🔄 تحديث الحضور والغياب من LMS الآن", callback_data="refresh_attendance")],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    if not att_rows:
        await send_fn(
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

    await send_fn(msg_obj, "\n".join(lines), kb, edit=edit)


async def _render_settings(msg_obj: Any, user: dict, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    a = "🔔 مفعل" if user.get("notify_assign", 1) else "🔕 معطل"
    q = "🔔 مفعل" if user.get("notify_quiz", 1) else "🔕 معطل"
    r = "🔔 مفعل" if user.get("notify_resource", 1) else "🔕 معطل"
    g = "🔔 مفعل" if user.get("notify_grade", 1) else "🔕 معطل"
    ann = "🔔 مفعل" if user.get("notify_announcement", 1) else "🔕 معطل"
    att = "🔔 مفعل" if user.get("notify_attendance", 1) else "🔕 معطل"
    dig = "🔔 مفعل" if user.get("digest_enabled", 1) else "🔕 معطل"
    risk = "🔔 مفعل" if user.get("notify_absence_risk", 1) else "🔕 معطل"
    api = "⚡ مفعل" if user.get("use_moodle_api", 0) else "🔒 معطل"
    iv = user.get("check_interval", 10)
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
            InlineKeyboardButton(f"☀️ الملخص الصباحي: {dig}", callback_data="sett_digest"),
            InlineKeyboardButton(f"⚠️ خطر الغياب: {risk}", callback_data="sett_absence_risk"),
        ],
        [
            InlineKeyboardButton(f"⚡ Moodle API الهجين: {api}", callback_data="sett_moodle_api"),
        ],
        [
            InlineKeyboardButton(f"{'✔ ' if iv == 5 else ''}5 د", callback_data="iv_5"),
            InlineKeyboardButton(f"{'✔ ' if iv == 10 else ''}10 د", callback_data="iv_10"),
            InlineKeyboardButton(f"{'✔ ' if iv == 30 else ''}30 د", callback_data="iv_30"),
            InlineKeyboardButton(f"{'✔ ' if iv == 60 else ''}1 س", callback_data="iv_60"),
        ],
        [
            InlineKeyboardButton(f"{'✔ ' if iv == 360 else ''}6 س", callback_data="iv_360"),
            InlineKeyboardButton(f"{'✔ ' if iv == 1440 else ''}24 س", callback_data="iv_1440"),
        ],
        [
            InlineKeyboardButton("📚 موادي المراقَبة", callback_data="my_courses"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
        [
            InlineKeyboardButton("🚪 تسجيل الخروج من الحساب", callback_data="logout_prompt"),
        ],
    ]

    await send_fn(
        msg_obj,
        f"⚙️ <b>إعدادات المراقبة والإشعارات</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 <b>تنبيهات المحتوى:</b>\n"
        f"  • الواجبات: {a}\n"
        f"  • الكويزات: {q}\n"
        f"  • الملفات والموارد: {r}\n"
        f"  • رادار العلامات: {g}\n"
        f"  • رادار الحضور والغياب: {att}\n"
        f"  • إعلانات الدكاترة: {ann}\n"
        f"  • الملخص الصباحي (48س): {dig}\n"
        f"  • تنبيهات خطر الغياب: {risk}\n"
        f"  • Moodle API الهجين: {api}\n\n"
        f"⏱️ <b>فترة الفحص الدوري:</b> كل <code>{escape_html(iv_txt)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>اضغط على أي زر لتفعيل/تعطيل الإشعار أو تحديد فترة الفحص:</i>",
        keyboard,
        edit=edit,
    )


async def _render_help(msg_obj: Any, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    await send_fn(
        msg_obj,
        "ℹ️ <b>دليل استخدام بوت مراقب LMS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• /start — فتح القائمة الرئيسية\n"
        "• /courses — عرض المواد المراقَبة\n"
        "• /status — عرض حالة النظام والمراقبة\n"
        "• /pair أو /app — ربط تطبيق الهاتف (PWA)\n"
        "• /unlink — فصل جميع الأجهزة المرتبطة\n"
        "• /logout — تسجيل الخروج وحذف الجلسة\n"
        "• /help — عرض هذه المساعدة\n\n"
        "💡 يمكنك التنقل بالكامل عبر الأزرار التفاعلية في القائمة الرئيسية.",
        _back_kb("main_menu"),
        edit=edit,
    )


def _format_uptime(seconds: float) -> str:
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def _format_last_check_amman_utc(iso_utc: Any) -> str:
    if not iso_utc:
        return "لم يتم بعد"
    raw = str(iso_utc).strip()
    try:
        dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        else:
            dt_utc = dt_utc.astimezone(timezone.utc)
        dt_amman = dt_utc.astimezone(JORDAN_TZ)
        utc_str = dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        amman_str = dt_amman.strftime("%Y-%m-%d %H:%M:%S")
        return f"{amman_str} (Amman) | {utc_str} (UTC)"
    except ValueError:
        return raw


async def _render_status_view(msg_obj: Any, edit: bool = False) -> None:
    send_fn = resolve_hook("_send", _send, _DEFAULT_SEND)
    snap = get_observability().snapshot()
    mon_state = "🟢 يعمل (Running)" if snap.get("monitor_running") else "🔴 متوقف (Stopped)"
    waf_state = "🔴 محظور مؤقتاً (WAF Active)" if snap.get("waf_blocked") else "🟢 طبيعي (OK)"
    last_ok = escape_html(_format_last_check_amman_utc(snap.get("last_successful_check_at")))
    total_checks = int(snap.get("total_checks", 0))
    err_count = int(snap.get("error_count", 0))
    err_rate_pct = float(snap.get("error_rate", 0.0)) * 100.0
    api_reqs = int(snap.get("api_requests_total", 0))
    scrape_reqs = int(snap.get("scrape_requests_total", 0))
    saved_reqs = int(snap.get("requests_saved_total", 0))
    uptime_txt = escape_html(_format_uptime(float(snap.get("uptime_seconds", 0.0))))

    lines = [
        "🩺 <b>حالة النظام والمراقبة (/status)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📡 حالة المراقب: <b>{mon_state}</b>",
        f"🛡️ حالة WAF: <b>{waf_state}</b>",
        f"⏰ آخر فحص ناجح:\n   <code>{last_ok}</code>",
        f"⏱️ مدة التشغيل (Uptime): <code>{uptime_txt}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 إجمالي الفحوصات: <b>{total_checks}</b>",
        f"❌ الأخطاء: <b>{err_count}</b> (نسبة الخطأ: <code>{err_rate_pct:.1f}%</code>)",
        f"⚡ طلبات Moodle API: <b>{api_reqs}</b> | طلبات Scraping: <b>{scrape_reqs}</b>",
        f"📉 الطلبات الموفّرة (API-First): <b>{saved_reqs}</b>",
    ]
    if snap.get("last_error"):
        lines.append(f"⚠️ آخر خطأ: <code>{escape_html(str(snap['last_error'])[:160])}</code>")

    await send_fn(msg_obj, "\n".join(lines), _back_kb("main_menu"), edit=edit)


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    await _render_status_view(update.message, edit=False)
    return ConversationHandler.END


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return await _reject_unauthorized(update)
    await _render_help(update.message, edit=False)
    return ConversationHandler.END
