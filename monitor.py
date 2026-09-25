import logging
import asyncio
from telegram import Bot
from telegram.constants import ParseMode

MD = ParseMode.MARKDOWN
from telegram.error import TelegramError
from lms_scraper import LMSScraper
import database as db

logger = logging.getLogger(__name__)

ASSIGN_MSG = """
📚 *واجب جديد تم نشره!*

📖 المادة: *{course}*
📝 عنوان الواجب: *{name}*
⏰ الموعد النهائي: `{due}`
🔗 [افتح الواجب هنا]({url})

_تم اكتشافه بواسطة بوت مراقب LMS_
""".strip()

QUIZ_MSG = """
📝 *كويز جديد تم نشره!*

📖 المادة: *{course}*
❓ عنوان الكويز: *{name}*
⏰ متاح حتى: `{due}`
🔗 [افتح الكويز هنا]({url})

_تم اكتشافه بواسطة بوت مراقب LMS_
""".strip()

RESOURCE_MSG = """
📎 *محتوى جديد تم نشره!*

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

    def __init__(self, bot: Bot):
        self.bot = bot
        self._running = False
        self._task = None

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
        logger.info("Monitor engine stopped")

    async def _loop(self):
        while self._running:
            try:
                users = db.get_all_monitoring_users()
                for user in users:
                    try:
                        await self._check_user(user)
                    except Exception as e:
                        logger.error(f"Error checking user {user['telegram_id']}: {e}")
            except Exception as e:
                logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)

    async def _check_user(self, user: dict):
        import time
        tid  = user["telegram_id"]
        interval_sec = user.get("check_interval", 600) * 60

        last_check = user.get("last_check")
        if last_check:
            from datetime import datetime
            try:
                last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.now() - last_dt).total_seconds()
                if elapsed < interval_sec:
                    return
            except ValueError:
                pass

        logger.info(f"Checking user {tid}")
        courses = db.get_user_courses(tid, monitored_only=True)
        if not courses:
            return

        scraper = LMSScraper(user["lms_username"], user["lms_password"])
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, scraper.login)
        if not ok:
            logger.warning(f"Login failed for {tid}")
            return

        for course in courses:
            await self._check_course(tid, user, scraper, course)

        db.update_last_check(tid)

    async def _check_course(self, tid, user, scraper, course):
        loop = asyncio.get_event_loop()
        cid  = course["course_id"]
        cname = course["course_name"]

        content = await loop.run_in_executor(
            None, scraper.get_course_content, cid
        )

        if user.get("notify_assign", 1):
            for item in content.get("assignments", []):
                if not db.is_seen(tid, item["id"], "assign"):
                    db.mark_seen(tid, cid, item["id"], "assign",
                                 item["name"], item["url"], item.get("due_date",""))
                    await self._send(
                        tid,
                        ASSIGN_MSG.format(
                            course=_esc(cname),
                            name=_esc(item["name"]),
                            due=_esc(item.get("due_date","غير محدد")),
                            url=item["url"],
                        )
                    )

        if user.get("notify_quiz", 1):
            for item in content.get("quizzes", []):
                if not db.is_seen(tid, item["id"], "quiz"):
                    db.mark_seen(tid, cid, item["id"], "quiz",
                                 item["name"], item["url"], item.get("due_date",""))
                    await self._send(
                        tid,
                        QUIZ_MSG.format(
                            course=_esc(cname),
                            name=_esc(item["name"]),
                            due=_esc(item.get("due_date","غير محدد")),
                            url=item["url"],
                        )
                    )

        if user.get("notify_resource", 1):
            for item in content.get("resources", []):
                if not db.is_seen(tid, item["id"], "resource"):
                    db.mark_seen(tid, cid, item["id"], "resource",
                                 item["name"], item["url"])
                    await self._send(
                        tid,
                        RESOURCE_MSG.format(
                            course=_esc(cname),
                            name=_esc(item["name"]),
                            url=item["url"],
                        )
                    )

    async def _send(self, chat_id: int, text: str):
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=MD,
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.3)
        except TelegramError as e:
            logger.error(f"Telegram send error: {e}")

    async def force_check(self, telegram_id: int):
        user = db.get_user(telegram_id)
        if not user:
            return 0, "المستخدم غير مسجّل"

        courses = db.get_user_courses(telegram_id, monitored_only=True)
        if not courses:
            return 0, "لا توجد مواد مراقَبة"

        scraper = LMSScraper(user["lms_username"], user["lms_password"])
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, scraper.login)
        if not ok:
            return 0, "فشل تسجيل الدخول إلى LMS"

        new_count = 0
        for course in courses:
            cid  = course["course_id"]
            cname= course["course_name"]
            content = await loop.run_in_executor(None, scraper.get_course_content, cid)

            for item in content.get("assignments", []):
                if not db.is_seen(telegram_id, item["id"], "assign"):
                    db.mark_seen(telegram_id, cid, item["id"], "assign",
                                 item["name"], item["url"], item.get("due_date",""))
                    await self._send(telegram_id, ASSIGN_MSG.format(
                        course=_esc(cname), name=_esc(item["name"]),
                        due=_esc(item.get("due_date","غير محدد")), url=item["url"]
                    ))
                    new_count += 1

            for item in content.get("quizzes", []):
                if not db.is_seen(telegram_id, item["id"], "quiz"):
                    db.mark_seen(telegram_id, cid, item["id"], "quiz",
                                 item["name"], item["url"], item.get("due_date",""))
                    await self._send(telegram_id, QUIZ_MSG.format(
                        course=_esc(cname), name=_esc(item["name"]),
                        due=_esc(item.get("due_date","غير محدد")), url=item["url"]
                    ))
                    new_count += 1

            for item in content.get("resources", []):
                if not db.is_seen(telegram_id, item["id"], "resource"):
                    db.mark_seen(telegram_id, cid, item["id"], "resource",
                                 item["name"], item["url"])
                    await self._send(telegram_id, RESOURCE_MSG.format(
                        course=_esc(cname), name=_esc(item["name"]),
                        url=item["url"]
                    ))
                    new_count += 1

        db.update_last_check(telegram_id)
        return new_count, "ok"
