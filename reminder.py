import asyncio
import functools
import logging
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

import config
import database as db
from services.web_push import send_web_push_to_user
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
    POLL_INTERVAL_SECONDS = 60
    DIGEST_POLL_INTERVAL_SECONDS = 60
    TICK_TIMEOUT_SECONDS = 120.0
    MISFIRE_GRACE_TIME = 30
    TICK_BACKOFF_BASE = 30.0
    TICK_BACKOFF_MAX = 300.0
    JOB_ID = "reminder_poll_tick"
    DIGEST_JOB_ID = "reminder_daily_digest_tick"

    def __init__(self, bot: Bot):
        self.bot = bot
        self._running = False
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._active_tasks: set[asyncio.Task] = set()
        self._tick_error_count: int = 0
        self._tick_backoff_until: float = 0.0

    def _track_task(self, coro) -> asyncio.Task:
        """Create and track an in-flight asyncio task so stop() awaits it cleanly."""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _run_db(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def _on_forbidden(self, chat_id: int) -> None:
        fn = getattr(db, "set_user_notifications_enabled", None)
        if callable(fn):
            try:
                await self._run_db(fn, int(chat_id), False)
            except Exception as exc:
                logger.warning("Failed to disable notifications after Forbidden for %s: %s", chat_id, exc)

    async def start(self):
        if self._running:
            return
        loop = asyncio.get_running_loop()
        self._running = True
        self._tick_error_count = 0
        self._tick_backoff_until = 0.0

        scheduler = AsyncIOScheduler(
            timezone=JORDAN_TZ,
            event_loop=loop,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": self.MISFIRE_GRACE_TIME,
            },
        )
        scheduler.add_job(
            self._scheduled_tick,
            trigger="interval",
            seconds=self.POLL_INTERVAL_SECONDS,
            id=self.JOB_ID,
            name="ReminderEngine periodic check",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.MISFIRE_GRACE_TIME,
            replace_existing=True,
            next_run_time=datetime.now(JORDAN_TZ),
        )
        scheduler.add_job(
            self._scheduled_digest_tick,
            trigger="interval",
            seconds=self.DIGEST_POLL_INTERVAL_SECONDS,
            id=self.DIGEST_JOB_ID,
            name="ReminderEngine daily digest check",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.MISFIRE_GRACE_TIME,
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info("Reminder engine started (APScheduler AsyncIOScheduler, tz=Asia/Amman)")

    async def stop(self):
        self._running = False
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.debug("Reminder scheduler shutdown error: %s", exc)
            # Yield once so AsyncIOScheduler's call_soon_threadsafe shutdown callback finishes
            await asyncio.sleep(0)

        in_flight = [t for t in list(self._active_tasks) if not t.done()]
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        logger.info("Reminder engine stopped")

    async def _scheduled_tick(self):
        if not self._running:
            return
        now_mono = time.monotonic()
        if now_mono < self._tick_backoff_until:
            logger.debug("Reminder tick skipped due to backoff (%.1fs remaining)", self._tick_backoff_until - now_mono)
            return

        tick_task = self._track_task(self._check_reminders())
        try:
            await asyncio.wait_for(asyncio.shield(tick_task), timeout=self.TICK_TIMEOUT_SECONDS)
            self._tick_error_count = 0
            self._tick_backoff_until = 0.0
        except asyncio.TimeoutError:
            tick_task.cancel()
            try:
                await tick_task
            except (asyncio.CancelledError, Exception):
                pass
            self._record_tick_failure("Reminder tick timed out after %.1fs" % self.TICK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            tick_task.cancel()
            raise
        except Exception as exc:
            self._record_tick_failure(f"Reminder tick error: {exc}")

    def _record_tick_failure(self, message: str) -> None:
        self._tick_error_count += 1
        delay = min(
            self.TICK_BACKOFF_BASE * (2 ** (self._tick_error_count - 1)),
            self.TICK_BACKOFF_MAX,
        )
        self._tick_backoff_until = time.monotonic() + delay
        logger.error("%s (consecutive=%d; backing off %.1fs)", message, self._tick_error_count, delay)

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

    async def _fire_web_push(
        self,
        user: Optional[dict],
        tid: int,
        title: str,
        body: str,
        url: str = "/webapp",
    ) -> None:
        """Secondary Web Push channel (F7): non-blocking and strictly isolated from Telegram."""
        if not getattr(config, "ENABLE_WEB_PUSH", False):
            return
        if user is not None and not user.get("enable_web_push", 1):
            return
        try:
            await asyncio.to_thread(send_web_push_to_user, db, tid, title, body, url)
        except Exception as exc:
            logger.warning("Web Push secondary hook failed for user %s: %s", tid, exc)

    async def _scheduled_digest_tick(self) -> None:
        if not self._running:
            return
        task = self._track_task(self._check_daily_digests())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.TICK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            logger.error("Daily digest tick timed out after %.1fs", self.TICK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:
            logger.error("Daily digest tick error: %s", exc)

    async def _check_daily_digests(self, now: Optional[datetime] = None) -> int:
        """Evaluate and deliver due morning digests (Asia/Amman), marking sent ONLY AFTER delivery."""
        if not getattr(config, "ENABLE_DAILY_DIGEST", True):
            return 0
        from services.digest_service import build_daily_digest, is_digest_due

        fetch_users = getattr(db, "get_digest_users", None)
        if callable(fetch_users):
            users = await self._run_db(fetch_users)
        else:
            users = await self._run_db(db.get_all_monitoring_users)

        sent_count = 0
        for user in users or []:
            due, today_amman_iso = is_digest_due(user, now=now)
            if not due:
                continue
            tid = int(user["telegram_id"])
            digest_html = await self._run_db(build_daily_digest, db, tid, now)
            if not digest_html:
                continue
            ok = await send_html_message(
                self.bot,
                tid,
                digest_html,
                disable_web_page_preview=True,
                on_forbidden=self._on_forbidden,
            )
            if ok:
                mark_fn = getattr(db, "mark_digest_sent", None)
                if callable(mark_fn):
                    await self._run_db(mark_fn, tid, today_amman_iso)
                await self._fire_web_push(
                    user,
                    tid,
                    "☀️ الملخص الصباحي اليومي",
                    "لديك مهام قادمة خلال 48 ساعة أو تنبيهات حضور.",
                    "/webapp",
                )
                sent_count += 1
                logger.info("Daily digest sent to %s for %s", tid, today_amman_iso)
        return sent_count

    async def _send_reminder(self, tid: int, rem: dict, slot: str) -> bool:
        emoji = REMIND_MSG[slot]
        label = REMIND_LABEL[slot]
        raw_name = rem.get("content_name", "واجب")
        url = rem.get("content_url", "")
        due = escape_html(rem.get("due_str", ""))

        course = await self._run_db(db.get_course, tid, rem["course_id"])
        raw_cname = course["course_name"] if course else "مادة"
        cname = escape_html(raw_cname)
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
            logger.info("Reminder sent (%s) to %s for '%s'", slot, tid, raw_name)
            await self._fire_web_push(
                None,
                tid,
                f"⏰ تذكير ({slot}): {raw_name}",
                f"{raw_cname} — الموعد: {rem.get('due_str', '')}",
                "/webapp",
            )
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
            logger.debug("Reminder registered: %s @ %s (submitted: %s)", a["name"], due_str, is_sub)
