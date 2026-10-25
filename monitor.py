import logging
import asyncio
import time
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

MD = ParseMode.MARKDOWN
from telegram.error import TelegramError
from lms_scraper import LMSScraper
import database as db

logger = logging.getLogger(__name__)

ASSIGN_MSG = """
📚 *{event}!*

📖 المادة: *{course}*
📝 عنوان الواجب: *{name}*
⏰ الموعد النهائي: `{due}`
🔗 [افتح الواجب هنا]({url})

_تم اكتشافه بواسطة بوت مراقب LMS_
""".strip()

QUIZ_MSG = """
📝 *{event}!*

📖 المادة: *{course}*
❓ عنوان الكويز: *{name}*
⏰ متاح حتى: `{due}`
🔗 [افتح الكويز هنا]({url})

_تم اكتشافه بواسطة بوت مراقب LMS_
""".strip()

RESOURCE_MSG = """
📎 *{event}!*

📖 المادة: *{course}*
📁 الملف/المورد: *{name}*
🔗 [افتح المورد هنا]({url})

_تم اكتشافه بواسطة بوت مراقب LMS_
""".strip()

def _esc(text: str) -> str:
    if not text:
        return ""
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text

class MonitorEngine:

    KEEPALIVE_SECONDS = 120

    def __init__(self, bot: Bot, reminder_engine=None):
        self.bot = bot
        self.reminder_engine = reminder_engine
        self._running = False
        self._task = None
        self._sessions = {}
        self._user_locks = {}
        self._check_slots = asyncio.Semaphore(5)
        self._auth_required = set()
        self._auth_alerted = set()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Monitor engine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for _, _, scraper in self._sessions.values():
            scraper.close()
        self._sessions.clear()
        logger.info("Monitor engine stopped")

    async def _loop(self):
        while self._running:
            try:
                users = db.get_all_monitoring_users()
                active_ids = {int(user["telegram_id"]) for user in users}
                for tid in set(self._sessions) - active_ids:
                    lock = self._user_locks.setdefault(tid, asyncio.Lock())
                    async with lock:
                        self._discard_session(tid)
                        self._auth_required.discard(tid)
                        self._auth_alerted.discard(tid)
                await asyncio.gather(*(self._safe_check_user(user) for user in users))
            except Exception as e:
                logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)

    async def _safe_check_user(self, user: dict):
        async with self._check_slots:
            try:
                await self._check_user(user)
            except Exception as e:
                logger.error(f"Error checking user {user['telegram_id']}: {e}")

    async def _check_user(self, user: dict):
        tid = int(user["telegram_id"])
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            courses = db.get_user_courses(tid, monitored_only=True)
            if not courses:
                self._discard_session(tid)
                self._auth_required.discard(tid)
                self._auth_alerted.discard(tid)
                db.update_last_check(tid)
                return

            if tid in self._auth_required:
                await self._notify_auth_stopped(tid)
                return

            scraper = self._get_session(user)
            loop = asyncio.get_running_loop()
            if not scraper.logged_in:
                ok = await loop.run_in_executor(None, scraper.login)
                if not ok:
                    logger.warning(f"Login failed for {tid}: {scraper.error_message}")
                    self._auth_required.add(tid)
                    await self._notify_auth_stopped(
                        tid,
                        "تعذر تسجيل الدخول تلقائياً إلى نظام الجامعة. أعد تسجيل الدخول من الزر أدناه؛ موادك المراقبة ستبقى محفوظة.",
                    )
                    return
                self._auth_alerted.discard(tid)

            due = True
            last_check = user.get("last_check")
            if last_check:
                from datetime import datetime
                try:
                    last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                    elapsed = (datetime.now() - last_dt).total_seconds()
                    due = elapsed >= max(1, int(user.get("check_interval", 5))) * 60
                except (TypeError, ValueError):
                    due = True

            if not due:
                idle_for = time.monotonic() - scraper.last_request_at
                if idle_for >= self.KEEPALIVE_SECONDS:
                    ok = await loop.run_in_executor(None, scraper.keep_session_alive)
                    if not ok:
                        logger.warning(f"LMS session keepalive failed for {tid}")
                    if scraper.session_expired:
                        self._auth_required.add(tid)
                        await self._notify_auth_stopped(tid)
                return

            logger.info(f"Checking user {tid}")
            count, complete = await self._scan_user(user, courses, scraper)
            if scraper.session_expired:
                self._auth_required.add(tid)
                await self._notify_auth_stopped(tid)
                return
            if complete:
                db.update_last_check(tid)
            else:
                logger.warning(f"Scan incomplete for {tid}; it will be retried soon")

    def _get_session(self, user: dict) -> LMSScraper:
        tid = int(user["telegram_id"])
        username = user["lms_username"]
        password = user["lms_password"]
        current = self._sessions.get(tid)
        if current and current[0] == username and current[1] == password:
            return current[2]
        if current:
            current[2].close()
        scraper = LMSScraper(username, password)
        self._sessions[tid] = (username, password, scraper)
        return scraper

    def _discard_session(self, tid: int):
        current = self._sessions.pop(int(tid), None)
        if current:
            current[2].close()

    async def resume_user(self, telegram_id: int, scraper: LMSScraper = None):
        """Resume monitoring with the fresh LMS session created by the login flow."""
        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            self._discard_session(tid)
            self._auth_required.discard(tid)
            self._auth_alerted.discard(tid)
            if scraper:
                self._sessions[tid] = (scraper.username, scraper.password, scraper)

    async def _notify_auth_stopped(self, telegram_id: int, message: str = None):
        tid = int(telegram_id)
        if tid in self._auth_alerted:
            return True
        text = message or (
            "⚠️ انتهت جلسة نظام الجامعة وتوقفت مراقبة المواد مؤقتاً.\n\n"
            "أعد تسجيل الدخول من الزر أدناه. ستبقى المواد المراقبة وإعداداتها محفوظة، "
            "وسيتابع البوت الفحص بعد نجاح الدخول."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 إعادة تسجيل الدخول", callback_data="login_start")
        ]])
        sent = await self._send(tid, text, keyboard=keyboard)
        if sent:
            self._auth_alerted.add(tid)
        return sent

    async def _scan_user(self, user: dict, courses: list, scraper: LMSScraper):
        tid = int(user["telegram_id"])
        sent_count = 0
        complete = True
        for course in courses:
            if scraper.session_expired:
                complete = False
                break
            count, course_complete = await self._check_course(tid, user, scraper, course)
            sent_count += count
            complete = complete and course_complete
        return sent_count, complete

    async def _check_course(self, tid, user, scraper, course):
        loop = asyncio.get_running_loop()
        cid  = course["course_id"]
        cname = course["course_name"]

        content = await loop.run_in_executor(
            None, scraper.get_course_content, cid
        )

        if not scraper.last_fetch_ok:
            logger.warning(f"Incomplete LMS content response for user {tid}, course {cid}")
            return 0, False

        assignments = content.get("assignments", [])
        if self.reminder_engine:
            self.reminder_engine.register_assignments(tid, cid, assignments)

        sent_count = 0
        complete = True

        for item in assignments:
            sent, ok = await self._notify_item(
                tid, cid, cname, item, "assign", user.get("notify_assign", 1)
            )
            sent_count += int(sent)
            complete = complete and ok

        for item in content.get("quizzes", []):
            sent, ok = await self._notify_item(
                tid, cid, cname, item, "quiz", user.get("notify_quiz", 1)
            )
            sent_count += int(sent)
            complete = complete and ok

        for item in content.get("resources", []):
            sent, ok = await self._notify_item(
                tid, cid, cname, item, "resource", user.get("notify_resource", 1)
            )
            sent_count += int(sent)
            complete = complete and ok

        return sent_count, complete

    async def _notify_item(self, tid, cid, cname, item, content_type, enabled):
        content_id = str(item["id"])
        name = item.get("name", "")
        url = item.get("url", "")
        due = item.get("due_date", "")
        existing = db.get_seen_content(tid, content_id, content_type)
        is_new = existing is None
        is_update = bool(existing and (
            (existing.get("content_name") or "") != name or
            (existing.get("content_url") or "") != url or
            (existing.get("due_date") or "") != due
        ))

        if not is_new and not is_update:
            return False, True

        if not enabled:
            if is_new:
                db.mark_seen(tid, cid, content_id, content_type, name, url, due)
            else:
                db.update_seen_content(tid, content_id, content_type, name, url, due)
            return False, True

        template = {
            "assign": ASSIGN_MSG,
            "quiz": QUIZ_MSG,
            "resource": RESOURCE_MSG,
        }[content_type]
        event = {
            "assign": "واجب جديد تم نشره" if is_new else "تم تحديث الواجب",
            "quiz": "كويز جديد تم نشره" if is_new else "تم تحديث الكويز",
            "resource": "محتوى جديد تم نشره" if is_new else "تم تحديث المحتوى",
        }[content_type]
        text = template.format(
            event=event,
            course=_esc(cname),
            name=_esc(name),
            due=_esc(due or "غير محدد"),
            url=url,
        )

        keyboard = None
        if content_type == "assign" and url:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 افتح الواجب", url=url)
            ]])

        # Store before sending; remove it on failure so the next scan retries.
        if is_new:
            db.mark_seen(tid, cid, content_id, content_type, name, url, due)

        sent = await self._send(tid, text, keyboard=keyboard)
        if not sent:
            if is_new:
                db.delete_seen(tid, content_id, content_type)
            return False, False

        if is_update:
            db.update_seen_content(tid, content_id, content_type, name, url, due)
        return True, True

    async def _send(self, chat_id: int, text: str,
                    keyboard: InlineKeyboardMarkup = None):
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=MD,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            await asyncio.sleep(0.3)
            return True
        except TelegramError as e:
            logger.warning(f"Markdown notification failed for {chat_id}; retrying plain: {e}")
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
                return True
            except Exception as retry_error:
                logger.error(f"Telegram notification retry failed for {chat_id}: {retry_error}")
                return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    async def force_check(self, telegram_id: int):
        user = db.get_user(telegram_id)
        if not user:
            return 0, "المستخدم غير مسجّل"

        courses = db.get_user_courses(telegram_id, monitored_only=True)
        if not courses:
            return 0, "لا توجد مواد مراقَبة"

        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            if tid in self._auth_required:
                return 0, "انتهت جلسة LMS؛ أعد تسجيل الدخول من البوت لاستئناف المراقبة"
            scraper = self._get_session(user)
            loop = asyncio.get_running_loop()
            if not scraper.logged_in:
                ok = await loop.run_in_executor(None, scraper.login)
                if not ok:
                    return 0, "فشل تسجيل الدخول إلى LMS"
            self._auth_alerted.discard(tid)
            count, complete = await self._scan_user(user, courses, scraper)
            if scraper.session_expired:
                self._auth_required.add(tid)
                await self._notify_auth_stopped(tid)
                return count, "انتهت جلسة LMS؛ أعد تسجيل الدخول لاستئناف المراقبة"
            if complete:
                db.update_last_check(tid)
                return count, "ok"
            return count, "تعذر إكمال الفحص؛ ستتم إعادة المحاولة"
