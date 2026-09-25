import re
import time
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
import database as db

logger = logging.getLogger(__name__)
MD = ParseMode.MARKDOWN

REMIND_MSG = {
    "24h": "🔴",
    "3h":  "🟠",
    "1h":  "💥",
}

REMIND_LABEL = {
    "24h": "باقي *24 ساعة*",
    "3h":  "باقي *3 ساعات*",
    "1h":  "باقي *ساعة واحدة فقط!*",
}

def _esc(t):
    if not t: return ""
    for ch in ["_", "*", "`", "["]:
        t = t.replace(ch, f"\\{ch}")
    return t

def _parse_due_date(due_str: str):
    if not due_str:
        return None

    s = due_str.strip()

    formats = [
        "%A, %d %B %Y, %I:%M %p",
        "%A, %d %B %Y, %H:%M",
        "%d %B %Y, %I:%M %p",
        "%d %B %Y, %H:%M",
        "%d %b %Y, %I:%M %p",
        "%d %b %Y, %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            ts = int(dt.timestamp()) - (3 * 3600)
            return ts
        except ValueError:
            continue

    match = re.search(
        r"(\d{1,2})\s+(\w+)\s+(\d{4})[,،]?\s*(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?",
        s, re.IGNORECASE
    )
    if match:
        try:
            day, month_s, year, hour, minute = (
                match.group(1), match.group(2), match.group(3),
                match.group(4), match.group(5)
            )
            am_pm = match.group(6)
            month_map = {
                "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
                "january":1,"february":2,"march":3,"april":4,
                "june":6,"july":7,"august":8,"september":9,
                "october":10,"november":11,"december":12,
                "يناير":1,"فبراير":2,"مارس":3,"أبريل":4,"ابريل":4,
                "مايو":5,"يونيو":6,"يوليو":7,"أغسطس":8,"اغسطس":8,
                "سبتمبر":9,"أكتوبر":10,"اكتوبر":10,"نوفمبر":11,"ديسمبر":12,
            }
            m = month_map.get(month_s.lower())
            if not m:
                return None
            h = int(hour)
            if am_pm and am_pm.lower() == "pm" and h != 12:
                h += 12
            elif am_pm and am_pm.lower() == "am" and h == 12:
                h = 0
            dt = datetime(int(year), m, int(day), h, int(minute))
            return int(dt.timestamp()) - (3 * 3600)
        except Exception:
            pass

    logger.debug(f"Could not parse due date: {due_str!r}")
    return None

class ReminderEngine:

    def __init__(self, bot: Bot):
        self.bot  = bot
        self._running = False
        self._task    = None

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
        logger.info("Reminder engine stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._check_reminders()
            except Exception as e:
                logger.error(f"Reminder loop error: {e}")
            await asyncio.sleep(60)

    async def _check_reminders(self):
        now_ts  = int(time.time())
        pending = db.get_pending_reminders(now_ts)

        for rem in pending:
            tid      = rem["telegram_id"]
            rid      = rem["id"]
            due_ts   = rem["due_ts"]
            diff     = due_ts - now_ts

            if not rem["reminded_24h"] and diff <= 86400 and diff > 10800:
                slot  = "24h"
                field = "reminded_24h"
            elif not rem["reminded_3h"] and diff <= 10800 and diff > 3600:
                slot  = "3h"
                field = "reminded_3h"
            elif not rem["reminded_1h"] and diff <= 3600:
                slot  = "1h"
                field = "reminded_1h"
            else:
                continue

            await self._send_reminder(tid, rem, slot)
            db.mark_reminded(rid, field)

    async def _send_reminder(self, tid: int, rem: dict, slot: str):
        emoji = REMIND_MSG[slot]
        label = REMIND_LABEL[slot]
        name  = _esc(rem.get("content_name", "واجب"))
        url   = rem.get("content_url", "")
        due   = _esc(rem.get("due_str", ""))

        course = db.get_course(tid, rem["course_id"])
        cname  = _esc(course["course_name"]) if course else "مادة"

        text = (
            f"{emoji} *تذكير بموعد تسليم واجب!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ {label}\n\n"
            f"📖 المادة: *{cname}*\n"
            f"📝 الواجب: [{name}]({url})\n"
            f"⏰ الموعد: `{due}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_لا تنسَ التسليم قبل فوات الأوان!_"
        )
        try:
            await self.bot.send_message(
                chat_id=tid, text=text,
                parse_mode=MD, disable_web_page_preview=True
            )
            logger.info(f"Reminder sent ({slot}) to {tid} for '{rem.get('content_name')}'")
        except TelegramError as e:
            logger.error(f"Reminder send error: {e}")

    def register_assignments(self, telegram_id: int, course_id: int,
                              assignments: list):
        for a in assignments:
            due_str = a.get("due_date", "")
            if not due_str:
                continue
            due_ts = _parse_due_date(due_str)
            if not due_ts:
                continue
            if due_ts <= int(time.time()):
                continue
            db.upsert_reminder(
                telegram_id  = telegram_id,
                course_id    = course_id,
                content_id   = a["id"],
                content_name = a["name"],
                content_url  = a["url"],
                due_ts       = due_ts,
                due_str      = due_str,
            )
            logger.debug(f"Reminder registered: {a['name']} @ {due_str}")
