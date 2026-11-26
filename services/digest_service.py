"""Daily morning digest builder and schedule helper (F5)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config
from reminder import _parse_due_date
from services.absence_risk import evaluate_user_absence_risks
from telegram_utils import escape_html, html_link

logger = logging.getLogger(__name__)
JORDAN_TZ = ZoneInfo("Asia/Amman")

DIGEST_WINDOW_SECONDS = 48 * 3600
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _normalize_amman_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(JORDAN_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=JORDAN_TZ)
    return now.astimezone(JORDAN_TZ)


def _normalize_digest_time(raw: Any) -> str:
    default_time = str(getattr(config, "DEFAULT_DIGEST_TIME", "08:00") or "08:00").strip()
    candidate = str(raw or "").strip() or default_time
    m = _HHMM_RE.match(candidate)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    m_def = _HHMM_RE.match(default_time)
    if m_def:
        hh, mm = int(m_def.group(1)), int(m_def.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    return "08:00"


def is_digest_due(user: dict[str, Any], now: Optional[datetime] = None) -> tuple[bool, str]:
    """Return ``(is_due, today_amman_iso)`` for a user based on Asia/Amman time."""
    now_amman = _normalize_amman_now(now)
    today_iso = now_amman.strftime("%Y-%m-%d")

    if not getattr(config, "ENABLE_DAILY_DIGEST", True):
        return False, today_iso
    if not user or not user.get("digest_enabled", 1):
        return False, today_iso

    last_sent = str(user.get("last_digest_date") or "").strip()
    if last_sent == today_iso:
        return False, today_iso

    target_hhmm = _normalize_digest_time(user.get("digest_time"))
    current_hhmm = now_amman.strftime("%H:%M")
    return current_hhmm >= target_hhmm, today_iso


def _parse_due_timestamp(raw_due: Any) -> Optional[int]:
    if raw_due is None or isinstance(raw_due, bool):
        return None
    if isinstance(raw_due, (int, float)):
        ts = int(raw_due)
        return ts if ts > 0 else None
    if isinstance(raw_due, datetime):
        dt = raw_due.replace(tzinfo=JORDAN_TZ) if raw_due.tzinfo is None else raw_due
        return int(dt.timestamp())

    text = str(raw_due).strip()
    if not text:
        return None

    parsed = _parse_due_date(text)
    if parsed is not None:
        return int(parsed)

    iso_candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    if "T" in iso_candidate or "t" in iso_candidate:
        try:
            dt = datetime.fromisoformat(iso_candidate)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JORDAN_TZ)
        return int(dt.timestamp())
    return None


def _fetch_user_reminders(db: Any, telegram_id: int) -> list[dict[str, Any]]:
    db_ctx_cls = getattr(db, "DBContext", None)
    if callable(db_ctx_cls):
        try:
            with db_ctx_cls() as ctx:
                return ctx.fetchall(
                    "SELECT * FROM reminders WHERE telegram_id=?",
                    (telegram_id,),
                )
        except Exception as exc:
            logger.debug("Fallback to get_upcoming_deadlines in digest_service: %s", exc)

    get_upcoming = getattr(db, "get_upcoming_deadlines", None)
    if callable(get_upcoming):
        return list(get_upcoming(telegram_id, limit=200) or [])
    return []


def _format_remaining(seconds: int) -> str:
    hrs = max(1, int(round(seconds / 3600.0)))
    if hrs == 1:
        return "خلال ساعة"
    if hrs == 2:
        return "بعد ساعتين"
    if 3 <= hrs <= 10:
        return f"بعد {hrs} ساعات"
    return f"بعد {hrs} ساعة"


def build_daily_digest(
    db: Any,
    telegram_id: int,
    now: Optional[datetime] = None,
    *,
    include_empty: bool = False,
) -> Optional[str]:
    """Build an HTML daily morning digest for ``telegram_id``.

    Includes:
    1. All pending assignments, quizzes, and personal reminders due within the next 48 hours
       (formatted in ``Asia/Amman``).
    2. All courses with ``warning``, ``critical``, or ``deprived`` absence risk
       (via ``services.absence_risk.evaluate_user_absence_risks``).

    Returns ``None`` when there are zero upcoming 48h tasks AND zero at-risk courses
    (unless ``include_empty=True``, in which case a short calm-day HTML summary is returned).
    """
    now_amman = _normalize_amman_now(now)
    now_ts = int(now_amman.timestamp())
    cutoff_ts = now_ts + DIGEST_WINDOW_SECONDS

    courses = list(getattr(db, "get_user_courses", lambda _tid: [])(telegram_id) or [])
    course_map: dict[int, str] = {}
    for c in courses:
        try:
            course_map[int(c["course_id"])] = str(c.get("course_name") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue

    reminder_rows = _fetch_user_reminders(db, telegram_id)
    all_seen_by_course = getattr(db, "get_all_seen_content", lambda _tid: {})(telegram_id) or {}
    user_meta = getattr(db, "get_user_meta", lambda _tid: {})(telegram_id) or {}

    submitted_ids: set[str] = set()

    def _mark_submitted(cid_val: Any) -> None:
        cid_s = str(cid_val or "").strip()
        if not cid_s:
            return
        raw = cid_s.replace("assign_", "").replace("quiz_", "")
        submitted_ids.add(cid_s)
        if raw:
            submitted_ids.add(raw)
            submitted_ids.add(f"assign_{raw}")
            submitted_ids.add(f"quiz_{raw}")

    for rem in reminder_rows:
        if rem.get("is_submitted"):
            _mark_submitted(rem.get("content_id"))
    for rows in all_seen_by_course.values():
        for item in rows:
            if item.get("is_submitted"):
                _mark_submitted(item.get("content_id"))

    upcoming_tasks: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add_lms_task(
        *,
        content_id: Any,
        content_type: str,
        title: str,
        course_id: Any,
        course_name: str,
        due_ts: int,
        url: str,
    ) -> None:
        cid_str = str(content_id or "").strip()
        if not cid_str or due_ts <= now_ts or due_ts > cutoff_ts:
            return
        if cid_str in submitted_ids:
            return
        raw_num = cid_str.replace("assign_", "").replace("quiz_", "")
        dedup = {cid_str}
        if raw_num:
            dedup.add(f"{content_type}:{raw_num}")
            dedup.add(f"raw:{raw_num}")
        if any(k in seen_keys for k in dedup):
            return
        seen_keys.update(dedup)

        resolved_course = course_name.strip()
        if not resolved_course and course_id is not None:
            try:
                resolved_course = course_map.get(int(course_id), "")
            except (TypeError, ValueError):
                resolved_course = ""

        kind = "quiz" if content_type == "quiz" or cid_str.startswith("quiz_") else "assign"
        upcoming_tasks.append({
            "kind": kind,
            "title": (title or "مهمة دراسية").strip(),
            "course_name": resolved_course,
            "due_ts": due_ts,
            "url": str(url or "").strip(),
        })

    # 1a. Pending reminders table rows due within 48h
    for rem in reminder_rows:
        if rem.get("is_submitted"):
            continue
        due_ts = _parse_due_timestamp(rem.get("due_ts"))
        if due_ts is None:
            due_ts = _parse_due_timestamp(rem.get("due_str"))
        if due_ts is None or due_ts <= now_ts or due_ts > cutoff_ts:
            continue
        cid_str = str(rem.get("content_id") or "").strip()
        ctype = "quiz" if cid_str.startswith("quiz_") else "assign"
        _add_lms_task(
            content_id=cid_str,
            content_type=ctype,
            title=str(rem.get("content_name") or "واجب"),
            course_id=rem.get("course_id"),
            course_name=str(rem.get("course_name") or ""),
            due_ts=due_ts,
            url=str(rem.get("content_url") or ""),
        )

    # 1b. Pending seen_content assignments/quizzes due within 48h
    for rows in all_seen_by_course.values():
        for item in rows:
            if item.get("is_submitted"):
                continue
            ctype = str(item.get("content_type") or "").strip().lower()
            if ctype not in ("assign", "quiz"):
                continue
            due_ts = _parse_due_timestamp(item.get("due_date"))
            if due_ts is None or due_ts <= now_ts or due_ts > cutoff_ts:
                continue
            _add_lms_task(
                content_id=item.get("content_id"),
                content_type=ctype,
                title=str(item.get("content_name") or "مهمة دراسية"),
                course_id=item.get("course_id"),
                course_name="",
                due_ts=due_ts,
                url=str(item.get("content_url") or ""),
            )

    # 1c. Pending personal reminders due within 48h
    personal_list = user_meta.get("personal_reminders") if isinstance(user_meta, dict) else None
    if isinstance(personal_list, list):
        for idx, entry in enumerate(personal_list):
            if not isinstance(entry, dict):
                continue
            if entry.get("done") or entry.get("is_submitted") or entry.get("completed"):
                continue
            raw_when = (
                entry.get("due_ts")
                or entry.get("when")
                or entry.get("due_date")
                or entry.get("due")
                or entry.get("date")
            )
            due_ts = _parse_due_timestamp(raw_when)
            if due_ts is None or due_ts <= now_ts or due_ts > cutoff_ts:
                continue
            p_key = f"personal:{entry.get('id') or idx}:{due_ts}"
            if p_key in seen_keys:
                continue
            seen_keys.add(p_key)
            upcoming_tasks.append({
                "kind": "personal",
                "title": str(entry.get("title") or "تذكير شخصي").strip() or "تذكير شخصي",
                "course_name": str(entry.get("course_name") or "").strip(),
                "due_ts": due_ts,
                "url": str(entry.get("url") or "").strip(),
            })

    upcoming_tasks.sort(key=lambda t: (t["due_ts"], t["title"]))

    # 2. At-risk courses (warning / critical / deprived)
    at_risk_courses: list[dict[str, Any]] = []
    if getattr(config, "ENABLE_ABSENCE_RISK", True):
        try:
            all_risks = evaluate_user_absence_risks(db, telegram_id)
            at_risk_courses = [
                r for r in all_risks if r.get("at_risk") and r.get("risk_level") in ("warning", "critical", "deprived")
            ]
        except Exception as exc:
            logger.warning("Failed evaluating absence risks for daily digest (user %s): %s", telegram_id, exc)

    if not upcoming_tasks and not at_risk_courses:
        if include_empty:
            date_label = escape_html(now_amman.strftime("%Y-%m-%d"))
            return (
                f"☀️ <b>الملخص الصباحي اليومي ({date_label})</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "😌 يومك هادئ — لا توجد مهام خلال 48 ساعة القادمة ولا مواد معرضة لخطر الغياب."
            )
        return None

    date_label = escape_html(now_amman.strftime("%Y-%m-%d"))
    lines: list[str] = [
        f"☀️ <b>الملخص الصباحي اليومي ({date_label} — توقيت عمّان)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if upcoming_tasks:
        lines.append(f"📅 <b>المهام القادمة خلال 48 ساعة ({len(upcoming_tasks)}):</b>")
        for task in upcoming_tasks:
            kind = task["kind"]
            icon = "📝" if kind == "quiz" else ("📌" if kind == "personal" else "📚")
            kind_lbl = "كويز" if kind == "quiz" else ("تذكير شخصي" if kind == "personal" else "واجب")
            due_dt = datetime.fromtimestamp(task["due_ts"], tz=timezone.utc).astimezone(JORDAN_TZ)
            due_amman = due_dt.strftime("%Y-%m-%d %H:%M")
            rem_txt = _format_remaining(task["due_ts"] - now_ts)
            title_html = html_link(task["title"], task["url"]) if task["url"] else escape_html(task["title"])
            course_part = f" — <b>{escape_html(task['course_name'])}</b>" if task["course_name"] else ""
            lines.append(
                f"  {icon} {title_html}{course_part} <i>({kind_lbl})</i>\n"
                f"      ⏰ <code>{escape_html(due_amman)}</code> <i>({escape_html(rem_txt)})</i>"
            )
        lines.append("")

    if at_risk_courses:
        lines.append(f"⚠️ <b>تنبيهات خطر الغياب ({len(at_risk_courses)}):</b>")
        for risk in at_risk_courses:
            level = risk.get("risk_level", "warning")
            badge = "🚨 حرمان" if level == "deprived" else ("🔴 حرج" if level == "critical" else "🟡 تحذير")
            cname = escape_html(risk.get("course_name") or f"مادة {risk.get('course_id', '')}")
            absents = int(risk.get("absent_count", 0))
            max_allowed = int(risk.get("max_allowed", 0))
            remaining = int(risk.get("remaining", 0))
            lines.append(
                f"  • {badge} <b>{cname}</b>: غيابات <code>{absents}/{max_allowed}</code> "
                f"(المتبقي: <code>{remaining}</code>)"
            )
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>تمنياتنا لك بيوم دراسي موفق!</i>")
    return "\n".join(lines).strip()


__all__ = [
    "DIGEST_WINDOW_SECONDS",
    "build_daily_digest",
    "is_digest_due",
]
