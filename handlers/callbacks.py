"""Central Telegram callback query router with single-answer guarantee (C8)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from handlers.auth import _send_app_pairing, handle_confirm_logout, handle_logout_prompt
from handlers.common import (
    MAX_CHECK_INTERVAL,
    MIN_CHECK_INTERVAL,
    _format_interval,
    _is_owner,
    _make_answer_once,
    _reject_unauthorized,
    _run_blocking,
    resolve_hook,
    validate_check_interval,
)
from handlers.courses import (
    _fetch_show_course,
    handle_check_now,
    handle_clear_all_courses_prompt,
    handle_delete_course_prompt,
    handle_refresh_announcements,
    handle_refresh_attendance,
    handle_refresh_grades,
    handle_start_monitor,
    handle_stop_monitor,
)
from handlers.views import (
    _render_announcements_view,
    _render_attendance_view,
    _render_courses,
    _render_deadlines_view,
    _render_grades_view,
    _render_help,
    _render_settings,
    _render_stats_view,
    _render_webapp_info,
    _show_main,
)

logger = logging.getLogger("bot.handlers.callbacks")

_DEFAULT_SHOW_MAIN = _show_main
_DEFAULT_RENDER_COURSES = _render_courses
_DEFAULT_RENDER_SETTINGS = _render_settings
_DEFAULT_FETCH_SHOW_COURSE = _fetch_show_course
_DEFAULT_RENDER_GRADES = _render_grades_view
_DEFAULT_RENDER_ANNOUNCEMENTS = _render_announcements_view
_DEFAULT_RENDER_ATTENDANCE = _render_attendance_view

_NOTIFY_SETTING_MAP: dict[str, tuple[str, str]] = {
    "sett_assign": ("assign", "notify_assign"),
    "sett_quiz": ("quiz", "notify_quiz"),
    "sett_resource": ("resource", "notify_resource"),
    "sett_grade": ("grade", "notify_grade"),
    "sett_announcement": ("announcement", "notify_announcement"),
    "sett_attendance": ("attendance", "notify_attendance"),
}


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await _reject_unauthorized(update)
        return
    q = update.callback_query
    tid = update.effective_user.id
    data = q.data
    user = await _run_blocking(db.get_user, tid)
    _answer_once = _make_answer_once(q)
    try:
        await _dispatch_callback(update, ctx, q, tid, data, user, _answer_once)
    finally:
        if not _answer_once.is_answered():
            try:
                await _answer_once()
            except Exception as exc:
                logger.warning("Failed to answer callback query in finally: %s", exc)


async def _dispatch_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    q: Any,
    tid: int,
    data: Optional[str],
    user: Optional[dict],
    _answer_once: Callable[..., Awaitable[None]],
) -> None:
    if not data:
        return

    show_main_fn = resolve_hook("_show_main", _show_main, _DEFAULT_SHOW_MAIN)
    render_courses_fn = resolve_hook("_render_courses", _render_courses, _DEFAULT_RENDER_COURSES)
    render_settings_fn = resolve_hook("_render_settings", _render_settings, _DEFAULT_RENDER_SETTINGS)
    fetch_course_fn = resolve_hook("_fetch_show_course", _fetch_show_course, _DEFAULT_FETCH_SHOW_COURSE)

    if data == "main_menu":
        await show_main_fn(q.message, tid, edit=True)

    elif data == "help":
        await _render_help(q.message, edit=True)

    elif data == "app_pair":
        await _send_app_pairing(q.message, tid, edit=True)

    elif data == "webapp_info":
        await _render_webapp_info(q.message, edit=True)

    elif data == "my_courses":
        await render_courses_fn(q.message, tid, edit=True)

    elif data == "start_monitor":
        await handle_start_monitor(q.message, tid, user, _answer_once)

    elif data == "stop_monitor":
        await handle_stop_monitor(q.message, tid, user, _answer_once)

    elif data == "deadlines":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _render_deadlines_view(q.message, tid, edit=True)

    elif data == "check_now":
        await handle_check_now(q.message, tid, user, ctx, _answer_once)

    elif data == "stats":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _render_stats_view(q.message, tid, user, edit=True)

    elif data == "grades_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        render_grades_fn = resolve_hook("_render_grades_view", _render_grades_view, _DEFAULT_RENDER_GRADES)
        await render_grades_fn(q.message, tid, edit=True)

    elif data == "refresh_grades":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True)
            return
        await _answer_once()
        await handle_refresh_grades(q.message, tid, courses, ctx)

    elif data == "announcements_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        render_anns_fn = resolve_hook(
            "_render_announcements_view", _render_announcements_view, _DEFAULT_RENDER_ANNOUNCEMENTS
        )
        await render_anns_fn(q.message, tid, edit=True)

    elif data == "refresh_announcements":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True)
            return
        await _answer_once()
        await handle_refresh_announcements(q.message, tid, courses, ctx)

    elif data == "attendance_menu":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        render_att_fn = resolve_hook("_render_attendance_view", _render_attendance_view, _DEFAULT_RENDER_ATTENDANCE)
        await render_att_fn(q.message, tid, edit=True)

    elif data == "refresh_attendance":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        courses = await _run_blocking(db.get_user_courses, tid)
        if not courses:
            await _answer_once("أضف موادك أولاً!", show_alert=True)
            return
        await _answer_once()
        await handle_refresh_attendance(q.message, tid, courses, ctx)

    elif data == "settings":
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await render_settings_fn(q.message, user, edit=True)

    elif data in _NOTIFY_SETTING_MAP:
        if user:
            kw_key, col_key = _NOTIFY_SETTING_MAP[data]
            await _run_blocking(db.update_notify_settings, tid, **{kw_key: not user.get(col_key, 1)})
            await render_settings_fn(q.message, await _run_blocking(db.get_user, tid), edit=True)

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
        await render_settings_fn(q.message, await _run_blocking(db.get_user, tid), edit=True)

    elif data == "logout_prompt":
        await handle_logout_prompt(q.message, edit=True)

    elif data == "confirm_logout":
        await handle_confirm_logout(q.message, tid, ctx, edit=True)

    elif data.startswith("view_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _answer_once()
        await fetch_course_fn(q.message, tid, user, course_id, edit=True, ctx=ctx)

    elif data.startswith("toggle_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.toggle_course, tid, course_id)
        await _answer_once("تم تغيير حالة المراقبة للمادة")
        await render_courses_fn(q.message, tid, edit=True)

    elif data.startswith("cToggle_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.toggle_course, tid, course_id)
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _answer_once("تم تغيير حالة المراقبة للمادة")
        await fetch_course_fn(q.message, tid, user, course_id, edit=True, ctx=ctx)

    elif data.startswith("del_"):
        course_id = int(data.split("_", 1)[1])
        await handle_delete_course_prompt(q.message, tid, course_id)

    elif data.startswith("confirmDel_"):
        course_id = int(data.split("_", 1)[1])
        await _run_blocking(db.remove_course, tid, course_id)
        await _answer_once("🗑 تم حذف المادة بنجاح", show_alert=True)
        await render_courses_fn(q.message, tid, edit=True)

    elif data == "clear_all_courses_prompt":
        await handle_clear_all_courses_prompt(q.message)

    elif data == "confirm_clear_all_courses":
        await _run_blocking(db.clear_all_user_courses, tid)
        await _answer_once("🗑 تم حذف جميع المواد بنجاح", show_alert=True)
        await render_courses_fn(q.message, tid, edit=True)

    elif data.startswith("refresh_"):
        course_id = int(data.split("_", 1)[1])
        if not user:
            await _answer_once("سجّل دخولك أولاً!", show_alert=True)
            return
        await _answer_once()
        await fetch_course_fn(q.message, tid, user, course_id, edit=True, ctx=ctx)


async def fallback_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await handle_callback(update, ctx)
    return ConversationHandler.END
