import re
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
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
    t = str(t)
    for ch in ["_", "*", "`", "["]:
        t = t.replace(ch, f"\\{ch}")
    return t

def _parse_due_date(due_str: str):
    if not due_str:
        return None

    s = due_str.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

    def _timestamp(dt):
        # Moodle displays course dates in Jordan local time; do not depend on
        # the host machine's timezone (Render commonly runs in UTC).
        return int(dt.replace(tzinfo=timezone(timedelta(hours=3))).timestamp())

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

    month_map = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,
        "june":6,"july":7,"august":8,"september":9,
        "october":10,"november":11,"december":12,
        "يناير":1,"فبراير":2,"مارس":3,"أبريل":4,"ابريل":4,
        "مايو":5,"يونيو":6,"يوليو":7,"أغسطس":8,"اغسطس":8,
        "سبتمبر":9,"أكتوبر":10,"اكتوبر":10,"نوفمبر":11,"ديسمبر":12,
        "كانون الثاني":1,"شباط":2,"آذار":3,"اذار":3,"نيسان":4,
        "أيار":5,"ايار":5,"حزيران":6,"تموز":7,"آب":8,"اب":8,
        "أيلول":9,"ايلول":9,"تشرين الأول":10,"تشرين الاول":10,
        "تشرين الثاني":11,"كانون الأول":12,"كانون الاول":12,
    }

    match = re.search(
        r"(\d{1,2})\s+([^\d,،:]+?)\s+(\d{4})(?:[,،\s]+(\d{1,2}):(\d{2})\s*(AM|PM|am|pm|ص|م|صباحاً|صباحا|مساءً|مساء)?)?",
        s, re.IGNORECASE
    )
    if match:
        try:
            day = int(match.group(1))
            month_s = re.sub(r"\s+", " ", match.group(2).strip()).lower()
            year = int(match.group(3))
            hour = int(match.group(4)) if match.group(4) else 23
            minute = int(match.group(5)) if match.group(5) else 59
            am_pm = (match.group(6) or "").strip().lower()

            m = month_map.get(month_s)
            if not m:
                return None
            h = hour
            is_pm = am_pm in ("pm", "م", "مساءً", "مساء")
            is_am = am_pm in ("am", "ص", "صباحاً", "صباحا")
            if is_pm and h != 12:
                h += 12
            elif is_am and h == 12:
                h = 0
            dt = datetime(year, m, day, h, minute)
            return _timestamp(dt)
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

            if await self._send_reminder(tid, rem, slot):
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
            return True
        except TelegramError as e:
            logger.error(f"Reminder send error: {e}")
            try:
                await self.bot.send_message(
                    chat_id=tid, text=text,
                    parse_mode=None, disable_web_page_preview=True
                )
                return True
            except Exception as retry_error:
                logger.error(f"Reminder retry failed for {tid}: {retry_error}")
                return False
        except Exception as e:
            logger.error(f"Reminder send error: {e}")
            return False

    def register_assignments(self, telegram_id: int, course_id: int,
                              assignments: list):
        for a in assignments:
            due_str = a.get("due_date", "")
            if not due_str:
                db.delete_reminder(telegram_id, a["id"])
                continue
            due_ts = _parse_due_date(due_str)
            if not due_ts:
                db.delete_reminder(telegram_id, a["id"])
                continue
            if due_ts <= int(time.time()):
                db.delete_reminder(telegram_id, a["id"])
                continue
            is_sub = 1 if a.get("is_submitted") else 0
            db.upsert_reminder(
                telegram_id  = telegram_id,
                course_id    = course_id,
                content_id   = a["id"],
                content_name = a["name"],
                content_url  = a["url"],
                due_ts       = due_ts,
                due_str      = due_str,
                is_submitted = is_sub,
            )
            logger.debug(f"Reminder registered: {a['name']} @ {due_str} (submitted: {is_sub})")
