"""Unified course snapshot seeding and LMS course list synchronization service."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

from scraper.client import FetchResult, LMSAuthError, LMSWafError

logger = logging.getLogger(__name__)


def _normalize_course_id(course_id: Union[int, str]) -> Union[int, str]:
    """Convert numeric course ID strings to int while preserving non-numeric IDs."""
    if isinstance(course_id, int):
        return course_id
    raw = str(course_id).strip()
    return int(raw) if raw.isdigit() else raw


def seed_course_snapshot(
    db: Any,
    telegram_id: int,
    course_id: Union[int, str],
    content: Optional[Dict[str, Any]],
    *,
    course_name: str = "",
    mark_seeded: bool = True,
    reminder_engine: Any = None,
    include_seen: bool = True,
) -> Dict[str, Any]:
    """Persist a full course content snapshot into the database in one canonical place.

    Seeds resources, assignments, quizzes, grades, attendance, and announcements,
    registers deadline reminders (when `reminder_engine` is provided), updates
    submitted flags, and marks `user_courses.is_seeded = 1` when `mark_seeded=True`.
    """
    tid = int(telegram_id)
    cid = _normalize_course_id(course_id)
    payload = content if isinstance(content, dict) else {}

    assigns: List[Dict[str, Any]] = list(payload.get("assignments") or [])
    quizzes: List[Dict[str, Any]] = list(payload.get("quizzes") or [])
    resources: List[Dict[str, Any]] = list(payload.get("resources") or [])
    grades: List[Dict[str, Any]] = list(payload.get("grades") or [])
    anns: List[Dict[str, Any]] = list(payload.get("announcements") or [])
    att_list: List[Dict[str, Any]] = list(payload.get("attendance") or [])

    if include_seen:
        for item in assigns:
            db.mark_seen(
                tid,
                cid,
                item["id"],
                "assign",
                item.get("name", ""),
                item.get("url", ""),
                item.get("due_date", ""),
                is_submitted=1 if item.get("is_submitted") else 0,
            )
        for item in quizzes:
            db.mark_seen(
                tid,
                cid,
                item["id"],
                "quiz",
                item.get("name", ""),
                item.get("url", ""),
                item.get("due_date", ""),
                is_submitted=1 if item.get("is_submitted") else 0,
            )
        for item in resources:
            db.mark_seen(
                tid,
                cid,
                item["id"],
                "resource",
                item.get("name", ""),
                item.get("url", ""),
            )

    grades_saved = 0
    for g in grades:
        try:
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
            grades_saved += 1
        except Exception as exc:
            logger.warning("Error persisting grade for course %s (user %s): %s", cid, tid, exc)

    anns_saved = 0
    for ann in anns:
        try:
            db.upsert_announcement(
                tid,
                cid,
                ann["discuss_id"],
                ann.get("forum_name", "منتدى المقرر"),
                ann.get("title", ""),
                ann.get("author", ""),
                ann.get("posted_date", ""),
                ann.get("body_text") or ann.get("body", ""),
                ann.get("url", ""),
            )
            anns_saved += 1
        except Exception as exc:
            logger.warning("Error persisting announcement for course %s (user %s): %s", cid, tid, exc)

    att_saved = 0
    for att in att_list:
        try:
            sessions_json = att.get("sessions_json")
            if not isinstance(sessions_json, str):
                sessions_json = json.dumps(att.get("sessions", []), ensure_ascii=False)
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
                sessions_json=sessions_json,
                att_url=att.get("att_url") or att.get("url", ""),
            )
            att_saved += 1
        except Exception as exc:
            logger.warning("Error persisting attendance for course %s (user %s): %s", cid, tid, exc)

    if reminder_engine is not None and (assigns or quizzes):
        try:
            reminder_engine.register_assignments(tid, cid, assigns + quizzes)
        except Exception as exc:
            logger.warning("Error registering reminders for course %s (user %s): %s", cid, tid, exc)

    if include_seen:
        for task_item in assigns + quizzes:
            if task_item.get("is_submitted"):
                try:
                    db.mark_assignment_submitted(tid, task_item["id"], 1)
                except Exception as exc:
                    logger.debug("Error marking task %s submitted: %s", task_item.get("id"), exc)

    seeded_marked = False
    if mark_seeded:
        mark_fn = getattr(db, "mark_course_seeded", None)
        if callable(mark_fn):
            try:
                mark_fn(tid, cid)
                seeded_marked = True
            except Exception as exc:
                logger.debug("mark_course_seeded skipped for (%s, %s): %s", tid, cid, exc)

    return {
        "course_id": cid,
        "course_name": course_name,
        "assignments": len(assigns),
        "quizzes": len(quizzes),
        "resources": len(resources),
        "grades": grades_saved,
        "announcements": anns_saved,
        "attendance": att_saved,
        "seeded": seeded_marked,
    }


def sync_user_courses_list(
    db: Any,
    scraper: Any,
    telegram_id: int,
    *,
    seed_new: bool = False,
    reminder_engine: Any = None,
) -> Dict[str, Any]:
    """Fetch a user's enrolled courses from LMS, upsert into DB, and optionally seed new courses."""
    tid = int(telegram_id)
    waf_blocked = False
    session_expired = False
    error_msg: Optional[str] = None
    courses_list: List[Dict[str, Any]] = []

    try:
        try:
            courses_list = scraper.get_courses(raise_auth_errors=True) or []
        except TypeError:
            courses_list = scraper.get_courses() or []
    except LMSWafError as exc:
        waf_blocked = True
        error_msg = str(exc)
    except LMSAuthError as exc:
        session_expired = True
        error_msg = str(exc)

    if not waf_blocked and getattr(scraper, "waf_blocked", False):
        waf_blocked = True
        error_msg = error_msg or "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق"
    if not session_expired and getattr(scraper, "session_expired", False):
        session_expired = True
        error_msg = error_msg or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً"

    if waf_blocked or session_expired:
        return {
            "ok": False,
            "courses": [],
            "total_count": 0,
            "synced_count": 0,
            "newly_added": [],
            "already_present": [],
            "seeded_count": 0,
            "waf_blocked": waf_blocked,
            "session_expired": session_expired,
            "error": error_msg,
        }

    existing_rows = db.get_user_courses(tid) or []
    existing_ids = {int(c["course_id"]) for c in existing_rows}

    newly_added: List[str] = []
    already_present: List[str] = []
    courses_to_seed: List[Dict[str, Any]] = []

    for course in courses_list:
        cid = int(course["id"])
        cname = str(course.get("name") or f"مادة {cid}").strip()
        curl = str(course.get("url") or "").strip()

        is_existing = cid in existing_ids
        db.add_course(tid, cid, cname, curl)

        if is_existing:
            already_present.append(cname)
            if seed_new:
                is_seeded_fn = getattr(db, "is_course_seeded", None)
                already_seeded = bool(is_seeded_fn(tid, cid)) if callable(is_seeded_fn) else False
                if not already_seeded:
                    items_fn = getattr(db, "get_course_items", None)
                    has_items = bool(items_fn(tid, cid)) if callable(items_fn) else False
                    if not has_items:
                        courses_to_seed.append({"id": cid, "name": cname, "url": curl})
        else:
            newly_added.append(cname)
            if seed_new:
                courses_to_seed.append({"id": cid, "name": cname, "url": curl})

    seeded_count = 0
    if seed_new:
        for course in courses_to_seed:
            cid = course["id"]
            cname = course["name"]
            try:
                fetch_fn = getattr(scraper, "fetch_course_content", None)
                if callable(fetch_fn):
                    res = fetch_fn(cid)
                else:
                    res = scraper.get_course_content(cid)
            except LMSWafError as exc:
                waf_blocked = True
                error_msg = str(exc)
                break
            except LMSAuthError as exc:
                session_expired = True
                error_msg = str(exc)
                break
            except Exception as exc:
                logger.warning("Error seeding course %s for user %s: %s", cid, tid, exc)
                continue

            if isinstance(res, FetchResult):
                if res.waf_blocked:
                    waf_blocked = True
                    error_msg = res.error or "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق"
                    break
                if res.session_expired:
                    session_expired = True
                    error_msg = res.error or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى تسجيل الدخول مجدداً"
                    break
                if not res.ok:
                    logger.warning("Skipping seed for course %s (ok=False): %s", cid, res.error)
                    continue
                content = res.content
            else:
                if getattr(scraper, "waf_blocked", False):
                    waf_blocked = True
                    error_msg = "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق"
                    break
                if getattr(scraper, "session_expired", False):
                    session_expired = True
                    error_msg = "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى تسجيل الدخول مجدداً"
                    break
                if not getattr(scraper, "last_fetch_ok", True):
                    logger.warning("Skipping seed for course %s (last_fetch_ok=False)", cid)
                    continue
                content = res if isinstance(res, dict) else {}

            seed_course_snapshot(
                db,
                tid,
                cid,
                content,
                course_name=cname,
                mark_seeded=True,
                reminder_engine=reminder_engine,
            )
            seeded_count += 1

    return {
        "ok": not waf_blocked and not session_expired,
        "courses": courses_list,
        "total_count": len(courses_list),
        "synced_count": len(courses_list),
        "newly_added": newly_added,
        "already_present": already_present,
        "seeded_count": seeded_count,
        "waf_blocked": waf_blocked,
        "session_expired": session_expired,
        "error": error_msg,
    }
