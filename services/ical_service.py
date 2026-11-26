"""RFC 5545 Live iCalendar (.ics) feed generator for Mutah LMS Monitor (F4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from typing import Any, Optional
import urllib.parse
from zoneinfo import ZoneInfo

from reminder import _parse_due_date

logger = logging.getLogger(__name__)

JORDAN_TZ = ZoneInfo("Asia/Amman")
ICAL_PRODID = "-//Mutah LMS Monitor//EN"
ICAL_CALNAME = "Mutah LMS Monitor"
ICAL_TIMEZONE = "Asia/Amman"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_UID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def escape_ics_text(value: Any) -> str:
    """Escape RFC 5545 TEXT values (backslash, semicolon, comma, newline)."""
    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def safe_ics_url(raw: Any) -> str:
    """Return a sanitized http(s) URL safe for an iCal URL property, else empty string."""
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate or _CONTROL_CHARS_RE.search(candidate) or any(ch.isspace() for ch in candidate):
        return ""
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return ""
    return candidate


def format_ics_utc(dt: datetime) -> str:
    """Format a datetime as an RFC 5545 UTC timestamp (`YYYYMMDDTHHMMSSZ`)."""
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=timezone.utc)
    else:
        utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y%m%dT%H%M%SZ")


def _normalize_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_due_timestamp(raw_due: Any) -> Optional[int]:
    """Parse a due date strictly into a Unix epoch timestamp; return None if invalid."""
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

    # Primary parser: Moodle English/Arabic & standard date formats via reminder._parse_due_date
    parsed = _parse_due_date(text)
    if parsed is not None:
        return int(parsed)

    # Secondary strict ISO-8601 fallback (e.g. HTML datetime-local '2026-12-01T14:30' or '...Z')
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


def _canonical_content_key(content_id: Any, content_type: str = "") -> str:
    raw = str(content_id or "").strip()
    if not raw:
        return ""
    if raw.startswith(("assign_", "quiz_")):
        return raw
    if content_type in ("assign", "quiz"):
        return f"{content_type}_{raw}"
    return raw


def _make_safe_uid(prefix: str, telegram_id: int, identifier: str) -> str:
    clean = _UID_UNSAFE_RE.sub("_", str(identifier or "")).strip("_")
    if not clean:
        clean = hashlib.sha256(f"{prefix}:{telegram_id}:{identifier}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{int(telegram_id)}-{clean}@mutah-lms-monitor"


def _fetch_user_reminders(db: Any, telegram_id: int) -> list[dict[str, Any]]:
    """Fetch all reminder rows for a user without depending on wall-clock time."""
    db_ctx_cls = getattr(db, "DBContext", None)
    if callable(db_ctx_cls):
        try:
            with db_ctx_cls() as ctx:
                return ctx.fetchall(
                    "SELECT * FROM reminders WHERE telegram_id=?",
                    (telegram_id,),
                )
        except Exception as exc:
            logger.debug("Fallback to get_upcoming_deadlines in ical_service: %s", exc)

    get_upcoming = getattr(db, "get_upcoming_deadlines", None)
    if callable(get_upcoming):
        return list(get_upcoming(telegram_id, limit=200) or [])
    return []


def _fetch_user_meta(db: Any, telegram_id: int) -> dict[str, Any]:
    get_meta = getattr(db, "get_user_meta", None)
    if callable(get_meta):
        meta = get_meta(telegram_id)
        if isinstance(meta, dict):
            return meta
    return {}


def build_ical_feed(db: Any, telegram_id: int, now: Optional[datetime] = None) -> str:
    """Build an RFC 5545 VCALENDAR feed of upcoming non-completed due items for a user.

    Combines upcoming items from:
    1. `reminders` table
    2. `seen_content` table (items with parseable due dates via `reminder._parse_due_date`)
    3. `user_meta.personal_reminders`

    Items that are completed/submitted or lack a valid due date are strictly skipped.
    """
    now_utc = _normalize_now(now)
    now_ts = int(now_utc.timestamp())
    dtstamp = format_ics_utc(now_utc)

    courses = list(getattr(db, "get_user_courses", lambda _tid: [])(telegram_id) or [])
    course_map: dict[int, str] = {}
    for c in courses:
        try:
            course_map[int(c["course_id"])] = str(c.get("course_name") or "")
        except (KeyError, TypeError, ValueError):
            continue

    all_seen_by_course = getattr(db, "get_all_seen_content", lambda _tid: {})(telegram_id) or {}
    reminder_rows = _fetch_user_reminders(db, telegram_id)
    user_meta = _fetch_user_meta(db, telegram_id)

    # Collect completed/submitted content IDs across both reminders and seen_content
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

    events: list[tuple[int, str, dict[str, str]]] = []
    seen_event_keys: set[str] = set()

    def _add_lms_event(
        *,
        content_id: Any,
        content_type: str,
        title: str,
        course_id: Any,
        course_name: str,
        due_ts: int,
        due_str: str,
        url: str,
    ) -> None:
        cid_str = str(content_id or "").strip()
        if not cid_str or due_ts <= now_ts:
            return
        if cid_str in submitted_ids:
            return
        canonical_key = _canonical_content_key(cid_str, content_type)
        raw_num = cid_str.replace("assign_", "").replace("quiz_", "")
        dedup_keys = {cid_str, canonical_key}
        if raw_num:
            dedup_keys.add(f"{content_type or 'item'}:{raw_num}")
            dedup_keys.add(f"raw:{raw_num}")
        if any(k in seen_event_keys for k in dedup_keys):
            return
        seen_event_keys.update(dedup_keys)

        resolved_course = course_name.strip()
        if not resolved_course and course_id is not None:
            try:
                resolved_course = course_map.get(int(course_id), "")
            except (TypeError, ValueError):
                resolved_course = ""

        clean_title = (title or "مهمة دراسية").strip()
        summary = f"{clean_title} ({resolved_course})" if resolved_course else clean_title
        safe_url = safe_ics_url(url)

        kind_label = "اختبار قصير" if content_type == "quiz" or cid_str.startswith("quiz_") else "واجب"
        desc_parts = [p for p in (resolved_course, kind_label, f"الموعد: {due_str}" if due_str else "", safe_url) if p]
        description = "\n".join(desc_parts)

        start_dt = datetime.fromtimestamp(due_ts, tz=timezone.utc)
        end_dt = start_dt + timedelta(hours=1)
        uid = _make_safe_uid("lms", telegram_id, canonical_key)

        events.append((
            due_ts,
            uid,
            {
                "uid": uid,
                "dtstamp": dtstamp,
                "dtstart": format_ics_utc(start_dt),
                "dtend": format_ics_utc(end_dt),
                "summary": escape_ics_text(summary),
                "description": escape_ics_text(description),
                "url": safe_url,
            },
        ))

    # 1. Upcoming reminders
    for rem in reminder_rows:
        if rem.get("is_submitted"):
            continue
        due_ts = _parse_due_timestamp(rem.get("due_ts"))
        if due_ts is None:
            due_ts = _parse_due_timestamp(rem.get("due_str"))
        if due_ts is None or due_ts <= now_ts:
            continue
        cid_str = str(rem.get("content_id") or "").strip()
        ctype = "quiz" if cid_str.startswith("quiz_") else "assign"
        _add_lms_event(
            content_id=cid_str,
            content_type=ctype,
            title=str(rem.get("content_name") or "واجب"),
            course_id=rem.get("course_id"),
            course_name=str(rem.get("course_name") or ""),
            due_ts=due_ts,
            due_str=str(rem.get("due_str") or ""),
            url=str(rem.get("content_url") or ""),
        )

    # 2. Upcoming seen_content items with valid parseable due dates
    for rows in all_seen_by_course.values():
        for item in rows:
            if item.get("is_submitted"):
                continue
            due_str = str(item.get("due_date") or "").strip()
            if not due_str:
                continue
            due_ts = _parse_due_timestamp(due_str)
            if due_ts is None or due_ts <= now_ts:
                continue
            ctype = str(item.get("content_type") or "assign").strip().lower()
            _add_lms_event(
                content_id=item.get("content_id"),
                content_type=ctype,
                title=str(item.get("content_name") or "مهمة دراسية"),
                course_id=item.get("course_id"),
                course_name="",
                due_ts=due_ts,
                due_str=due_str,
                url=str(item.get("content_url") or ""),
            )

    # 3. Upcoming personal reminders from user_meta.personal_reminders
    personal_list = user_meta.get("personal_reminders")
    if isinstance(personal_list, list):
        for entry in personal_list:
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
            if due_ts is None or due_ts <= now_ts:
                continue

            title = str(entry.get("title") or "تذكير شخصي").strip() or "تذكير شخصي"
            course_name = str(entry.get("course_name") or "").strip()
            task_id = str(entry.get("task_id") or "").strip()
            when_label = str(entry.get("when") or entry.get("due_date") or "").strip()
            safe_url = safe_ics_url(entry.get("url") or "")

            summary = f"{title} ({course_name})" if course_name else title
            desc_parts = [p for p in ("تذكير شخصي", course_name, when_label, safe_url) if p]
            description = "\n".join(desc_parts)

            stable_seed = f"{entry.get('id') or ''}|{task_id}|{title}|{course_name}|{due_ts}"
            digest = hashlib.sha256(stable_seed.encode("utf-8")).hexdigest()[:16]
            uid = _make_safe_uid("personal", telegram_id, digest)
            if uid in seen_event_keys:
                continue
            seen_event_keys.add(uid)

            start_dt = datetime.fromtimestamp(due_ts, tz=timezone.utc)
            end_dt = start_dt + timedelta(hours=1)
            events.append((
                due_ts,
                uid,
                {
                    "uid": uid,
                    "dtstamp": dtstamp,
                    "dtstart": format_ics_utc(start_dt),
                    "dtend": format_ics_utc(end_dt),
                    "summary": escape_ics_text(summary),
                    "description": escape_ics_text(description),
                    "url": safe_url,
                },
            ))

    # Sort deterministically by due timestamp then UID
    events.sort(key=lambda item: (item[0], item[1]))

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{ICAL_PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{ICAL_CALNAME}",
        f"X-WR-TIMEZONE:{ICAL_TIMEZONE}",
    ]

    for _, _, ev in events:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{ev['dtstamp']}",
            f"DTSTART:{ev['dtstart']}",
            f"DTEND:{ev['dtend']}",
            f"SUMMARY:{ev['summary']}",
            f"DESCRIPTION:{ev['description']}",
        ])
        if ev["url"]:
            lines.append(f"URL:{ev['url']}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
