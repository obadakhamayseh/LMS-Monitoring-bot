import asyncio
import functools
import logging
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Bot

import database as db
from telegram_utils import escape_html, html_link, send_html_message

logger = logging.getLogger(__name__)
JORDAN_TZ = ZoneInfo("Asia/Amman")

# Month names (English full/short, Arabic Gregorian, Arabic Levantine incl. compound names)
MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "إبريل": 4, "ابريل": 4,
    "مايو": 5, "يونيو": 6, "يوليو": 7, "أغسطس": 8, "اغسطس": 8,
    "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
    "كانون الثاني": 1, "كانون ثاني": 1,
    "شباط": 2,
    "آذار": 3, "أذار": 3, "اذار": 3,
    "نيسان": 4,
    "أيار": 5, "ايار": 5,
    "حزيران": 6,
    "تموز": 7,
    "آب": 8, "أب": 8, "اب": 8,
    "أيلول": 9, "إيلول": 9, "ايلول": 9,
    "تشرين الأول": 10, "تشرين الاول": 10, "تشرين أول": 10, "تشرين اول": 10,
    "تشرين الثاني": 11, "تشرين ثاني": 11,
    "كانون الأول": 12, "كانون الاول": 12, "كانون أول": 12, "كانون اول": 12,
}

# Build month alternation longest-first so compound names (e.g. كانون الثاني) match before shorter tokens.
_MONTH_PATTERN = "|".join(re.escape(k) for k in sorted(MONTH_MAP, key=len, reverse=True))
_AMPM_PATTERN = r"صباحاً|صباحا|مساءً|مساءا|مساء|a\.?m\.?|p\.?m\.?|ص|م"
_DUE_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+({_MONTH_PATTERN})\.?(?!\w)"
    rf"(?:\s+(\d{{4}}))?"
    rf"(?:[,،\s]+(\d{{1,2}}):(\d{{2}})(?:\s*({_AMPM_PATTERN})(?!\w))?)?",
    re.IGNORECASE,
)

_DIGIT_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _now_jordan() -> datetime:
    """Return the current time in Asia/Amman (isolated for deterministic testing)."""
    return datetime.now(JORDAN_TZ)


REMIND_MSG = {
    "24h": "🔴",
    "3h":  "🟠",
    "1h":  "💥",
}

REMIND_LABEL = {
    "24h": "باقي <b>24 ساعة</b>",
    "3h":  "باقي <b>3 ساعات</b>",
    "1h":  "باقي <b>ساعة واحدة فقط!</b>",
}


def _parse_due_date(due_str: str) -> Optional[int]:
    if not due_str:
        return None

    s = re.sub(r"\s+", " ", due_str.strip().translate(_DIGIT_TRANS))

    def _timestamp(dt: datetime) -> int:
        # Moodle displays course dates in Jordan local time; bind explicitly to Asia/Amman.
        return int(dt.replace(tzinfo=JORDAN_TZ).timestamp())

    formats = [
        "%A, %d %B %Y, %I:%M %p",
        "%A, %d %B %Y, %H:%M",
        "%d %B %Y, %I:%M %p",
        "%d %B %Y, %H:%M",
        "%d %b %Y, %I:%M %p",
        "%d %b %Y, %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                dt = dt.replace(hour=23, minute=59)
            return _timestamp(dt)
        except ValueError:
            continue

    match = _DUE_RE.search(s)
    if match:
        try:
            day = int(match.group(1))
            month_s = re.sub(r"\s+", " ", match.group(2).strip()).lower()
            m = MONTH_MAP.get(month_s)
            if not m:
                return None
            if match.group(3):
                year = int(match.group(3))
            else:
                # Moodle 5.x overview tables may omit the year (e.g. 'Saturday, 28 November, 11:59 p.m.')
                now_jo = _now_jordan()
                year = now_jo.year
                if now_jo.month >= 10 and m <= 2:
                    year += 1
                elif now_jo.month <= 2 and m >= 11:
                    year -= 1
            hour = int(match.group(4)) if match.group(4) else 23
            minute = int(match.group(5)) if match.group(5) else 59
            am_pm = (match.group(6) or "").replace(".", "").strip().lower()

            h = hour
            is_pm = am_pm in ("pm", "م", "مساءً", "مساء", "مساءا")
            is_am = am_pm in ("am", "ص", "صباحاً", "صباحا")
            if is_pm and 1 <= h < 12:
                h += 12
            elif is_am and h == 12:
                h = 0
            dt = datetime(year, m, day, h, minute)
            return _timestamp(dt)
        except ValueError as exc:
            logger.debug("Invalid calendar date in %r: %s", due_str, exc)

    logger.debug("Could not parse due date: %r", due_str)
    return None


class ReminderEngine:

    def __init__(self, bot: Bot):
        self.bot = bot
        self._running = False
        self._task = None

    async def _run_db(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def _on_forbidden(self, chat_id: int) -> None:
        fn = getattr(db, "set_user_notifications_enabled", None)
        if callable(fn):
            try:
                await self._run_db(fn, int(chat_id), False)
            except Exception as exc:
                logger.warning(f"Failed to disable notifications after Forbidden for {chat_id}: {exc}")

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Reminder engine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Reminder engine stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._check_reminders()
            except Exception as e:
                logger.error(f"Reminder loop error: {e}")
            await asyncio.sleep(60)

    async def _check_reminders(self):
        now_ts = int(time.time())
        pending = await self._run_db(db.get_pending_reminders, now_ts)

        for rem in pending:
            tid = rem["telegram_id"]
            rid = rem["id"]
            due_ts = rem["due_ts"]
            diff = due_ts - now_ts

            if not rem["reminded_24h"] and diff <= 86400 and diff > 10800:
                slot = "24h"
                field = "reminded_24h"
            elif not rem["reminded_3h"] and diff <= 10800 and diff > 3600:
                slot = "3h"
                field = "reminded_3h"
            elif not rem["reminded_1h"] and diff <= 3600:
                slot = "1h"
                field = "reminded_1h"
            else:
                continue

            # H6: Mark reminded ONLY AFTER HTML send succeeds
            if await self._send_reminder(tid, rem, slot):
                await self._run_db(db.mark_reminded, rid, field)

    async def _send_reminder(self, tid: int, rem: dict, slot: str) -> bool:
        emoji = REMIND_MSG[slot]
        label = REMIND_LABEL[slot]
        raw_name = rem.get("content_name", "واجب")
        url = rem.get("content_url", "")
        due = escape_html(rem.get("due_str", ""))

        course = await self._run_db(db.get_course, tid, rem["course_id"])
        cname = escape_html(course["course_name"]) if course else "مادة"
        task_link = html_link(raw_name, url)

        text = (
            f"{emoji} <b>تذكير بموعد تسليم واجب!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ {label}\n\n"
            f"📖 المادة: <b>{cname}</b>\n"
            f"📝 الواجب: {task_link}\n"
            f"⏰ الموعد: <code>{due}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>لا تنسَ التسليم قبل فوات الأوان!</i>"
        )
        ok = await send_html_message(
            self.bot,
            tid,
            text,
            disable_web_page_preview=True,
            on_forbidden=self._on_forbidden,
        )
        if ok:
            logger.info(f"Reminder sent ({slot}) to {tid} for '{raw_name}'")
        return ok

    def register_assignments(self, telegram_id: int, course_id: int, assignments: list):
        for a in assignments:
            due_str = (a.get("due_date") or "").strip()
            # H6: Never delete an existing reminder when due_str is empty or transiently unparseable
            if not due_str:
                continue
            due_ts = _parse_due_date(due_str)
            if not due_ts:
                continue
            if due_ts <= int(time.time()):
                db.delete_reminder(telegram_id, a["id"])
                continue
            is_sub = 1 if a.get("is_submitted") else 0
            db.upsert_reminder(
                telegram_id=telegram_id,
                course_id=course_id,
                content_id=a["id"],
                content_name=a["name"],
                content_url=a["url"],
                due_ts=due_ts,
                due_str=due_str,
                is_submitted=is_sub,
            )
            logger.debug(f"Reminder registered: {a['name']} @ {due_str} (submitted: {is_sub})")
