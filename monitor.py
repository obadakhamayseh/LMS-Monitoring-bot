import io
import json
import logging
import asyncio
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

MD = ParseMode.MARKDOWN
from telegram.error import TelegramError
from lms_scraper import LMSScraper
import database as db

logger = logging.getLogger(__name__)

def _esc(text: str) -> str:
    if not text:
        return ""
    for ch in ["_", "*", "`", "["]:
        text = str(text).replace(ch, f"\\{ch}")
    return text

def _canonical_content_url(url: str) -> str:
    """Normalize volatile Moodle link parameters before checking for updates."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() != "__ncforminfo"
        )
        return urlunsplit((
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path,
            urlencode(query, doseq=True),
            "",
        ))
    except ValueError:
        return url.strip()

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
        self._course_cache = {}  # course_id -> (timestamp, content)
        self.COURSE_CACHE_TTL = 300  # 5 minutes shared cache TTL

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

    def _discard_session(self, tid: int, logout: bool = False):
        current = self._sessions.pop(int(tid), None)
        if current:
            if logout:
                try:
                    current[2].logout()
                except Exception:
                    current[2].close()
            else:
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

    async def stop_user(self, telegram_id: int):
        """Clean up active scraper session and cache when a user logs out."""
        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            self._discard_session(tid, logout=True)
            self._auth_required.discard(tid)
            self._auth_alerted.discard(tid)
            for key in [k for k in self._course_cache if isinstance(k, tuple) and k[0] == tid]:
                self._course_cache.pop(key, None)

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

    async def _scan_user(self, user: dict, courses: list, scraper: LMSScraper, bypass_cache: bool = False):
        tid = int(user["telegram_id"])
        sent_count = 0
        complete = True
        events = []

        sem = asyncio.Semaphore(4)

        async def _check_course_safe(course):
            async with sem:
                if scraper.session_expired:
                    return [], False
                try:
                    return await self._check_course(
                        tid, user, scraper, course, bypass_cache=bypass_cache
                    )
                except Exception as e:
                    logger.error(f"Error checking course {course.get('course_id')} for {tid}: {e}")
                    return [], False

        course_results = await asyncio.gather(*(_check_course_safe(c) for c in courses))
        for course_events, course_complete in course_results:
            events.extend(course_events)
            complete = complete and course_complete

        loop = asyncio.get_running_loop()
        for chunk_events, message in self._build_change_chunks(events):
            for event in chunk_events:
                if event["is_new"]:
                    self._mark_new_seen(event)
            sent = await self._send(tid, message, plain=True)
            if sent:
                for event in chunk_events:
                    self._save_change(event)
                sent_count += len(chunk_events)

                # Direct document dispatch for newly added resources
                if user.get("notify_resource", 1):
                    new_resources = [e for e in chunk_events if e["is_new"] and e["content_type"] == "resource"]
                    for res_ev in new_resources[:3]:
                        try:
                            file_info = await loop.run_in_executor(
                                None, scraper.download_resource, res_ev["url"]
                            )
                            if file_info:
                                filename, file_bytes = file_info
                                try:
                                    await self.bot.send_document(
                                        chat_id=tid,
                                        document=io.BytesIO(file_bytes),
                                        filename=filename,
                                        caption=(
                                            f"📎 *ملف جديد:* {_esc(res_ev['name'])}\n"
                                            f"📖 *المادة:* {_esc(res_ev['course_name'])}"
                                        ),
                                        parse_mode=MD,
                                    )
                                except TelegramError:
                                    await self.bot.send_document(
                                        chat_id=tid,
                                        document=io.BytesIO(file_bytes),
                                        filename=filename,
                                        caption=(
                                            f"📎 ملف جديد: {res_ev['name']}\n"
                                            f"📖 المادة: {res_ev['course_name']}"
                                        ),
                                        parse_mode=None,
                                    )
                                logger.info(f"Direct file delivered: {filename} to {tid}")
                                await asyncio.sleep(0.5)
                        except Exception as dl_err:
                            logger.warning(f"Direct file send error for {tid}: {dl_err}")
            else:
                for event in chunk_events:
                    if event["is_new"]:
                        db.delete_seen(
                            event["telegram_id"], event["content_id"],
                            event["content_type"],
                        )
                complete = False
        return sent_count, complete

    async def _check_course(self, tid, user, scraper, course, bypass_cache: bool = False):
        loop = asyncio.get_running_loop()
        cid  = course["course_id"]
        cname = course["course_name"]

        now = time.monotonic()
        content = None
        cache_key = (tid, cid)

        if not bypass_cache and cache_key in self._course_cache:
            cache_ts, cached_content = self._course_cache[cache_key]
            if now - cache_ts < self.COURSE_CACHE_TTL:
                content = cached_content
                logger.debug(f"Using course cache for course {cid} (user {tid})")

        if content is None:
            content = await loop.run_in_executor(
                None, scraper.get_course_content, cid
            )
            if not scraper.last_fetch_ok:
                logger.warning(f"Incomplete LMS content response for user {tid}, course {cid}")
                return [], False
            self._course_cache[cache_key] = (now, content)

        assignments = content.get("assignments", [])
        quizzes = content.get("quizzes", [])
        if self.reminder_engine:
            self.reminder_engine.register_assignments(tid, cid, assignments + quizzes)

        # Auto-detect solved/completed assignments & quizzes from LMS and update DB
        for task_item in assignments + quizzes:
            if task_item.get("is_submitted"):
                t_type = task_item.get("type", "assign")
                existing_seen = db.get_seen_content(tid, task_item["id"], t_type)
                was_previously_unsubmitted = existing_seen is not None and not bool(existing_seen.get("is_submitted", 0))
                db.mark_assignment_submitted(tid, task_item["id"], 1)
                if was_previously_unsubmitted:
                    t_label = "الواجب" if t_type == "assign" else "الاختبار/المهمة"
                    done_msg = (
                        f"✅ *تم رصد إنجاز {t_label} تلقائياً في LMS!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📖 المادة: *{_esc(cname)}*\n"
                        f"📝 المهمة: *{_esc(task_item.get('name', ''))}*\n"
                        f"🔕 _تم تحديث حالتها إلى (منجز) وإيقاف التذكيرات الزمنية الخاصة بها تلقائياً._"
                    )
                    await self._send(tid, done_msg)

        events = []

        for item in assignments:
            event = self._collect_change(
                tid, cid, cname, item, "assign", user.get("notify_assign", 1)
            )
            if event:
                events.append(event)

        for item in content.get("quizzes", []):
            event = self._collect_change(
                tid, cid, cname, item, "quiz", user.get("notify_quiz", 1)
            )
            if event:
                events.append(event)

        for item in content.get("resources", []):
            event = self._collect_change(
                tid, cid, cname, item, "resource", user.get("notify_resource", 1)
            )
            if event:
                events.append(event)

        # 4. Gradebook & Marks Radar
        try:
            grades_list = content.get("grades", [])
            existing_course_grades = db.get_user_grades(tid, cid)
            is_first_grade_seed = len(existing_course_grades) == 0

            for g in grades_list:
                item_id = g["item_id"]
                item_name = g.get("item_name", "تقييم")
                grade_val = (g.get("grade_val") or "").strip()
                grade_max = (g.get("grade_max") or "").strip()
                pct = (g.get("percentage") or "").strip()
                fb = (g.get("feedback") or "").strip()
                g_url = g.get("url", "")

                old_g = db.get_grade(tid, cid, item_id)
                old_val = (old_g.get("grade_val") or "").strip() if old_g else ""

                db.upsert_grade(tid, cid, item_id, item_name, grade_val, grade_max, pct, fb, g_url)

                # Notify if a real grade value was newly posted or changed (and not initial course seed)
                if not is_first_grade_seed and grade_val and grade_val != old_val:
                    if user.get("notify_grade", 1):
                        g_lines = [
                            "🎯 *تم رصد علامة جديدة في LMS!*",
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            f"📖 المادة: *{_esc(cname)}*",
                            f"📝 التقييم: *{_esc(item_name)}*",
                            f"🏆 العلامة: `{_esc(grade_val)}`" + (f" _(المدى: {_esc(grade_max)})_" if grade_max else ""),
                        ]
                        if pct:
                            g_lines.append(f"📊 النسبة: `{_esc(pct)}`")
                        if fb:
                            g_lines.append(f"💬 ملاحظات الدكتور: _{_esc(fb[:300])}_")
                        if g_url:
                            g_lines.append(f"🔗 [عرض سجل العلامات]({g_url})")
                        await self._send(tid, "\n".join(g_lines))
        except Exception as ge:
            logger.warning(f"Error checking grades for course {cid} (user {tid}): {ge}")

        # 5. Course Announcements & Forum Radar
        try:
            announcements_list = content.get("announcements", [])
            existing_course_anns = db.get_user_announcements(tid, cid, limit=5)
            is_first_ann_seed = len(existing_course_anns) == 0

            for ann in announcements_list:
                discuss_id = ann["discuss_id"]
                existing_ann = db.get_announcement(tid, discuss_id)
                if existing_ann is not None:
                    continue

                body_text = ""
                author = ann.get("author", "")
                posted_date = ann.get("posted_date", "")

                if not is_first_ann_seed and ann.get("url"):
                    details = await loop.run_in_executor(None, scraper.get_announcement_details, ann["url"])
                    body_text = details.get("body", "")
                    author = author or details.get("author", "")
                    posted_date = posted_date or details.get("date", "")

                db.upsert_announcement(
                    tid, cid, discuss_id,
                    ann.get("forum_name", "منتدى المقرر"),
                    ann.get("title", "إعلان جديد"),
                    author, posted_date, body_text, ann.get("url", "")
                )

                if not is_first_ann_seed and user.get("notify_announcement", 1):
                    a_lines = [
                        "📢 *إعلان جديد من مدرس المساق!*",
                        "━━━━━━━━━━━━━━━━━━━━━━",
                        f"📖 المادة: *{_esc(cname)}*",
                        f"📌 الموضوع: *{_esc(ann.get('title', 'إعلان'))}*",
                    ]
                    if author:
                        a_lines.append(f"👨‍🏫 الناشر: {_esc(author)}")
                    if posted_date:
                        a_lines.append(f"🕒 التاريخ: `{_esc(posted_date)}`")
                    if body_text:
                        a_lines.append(f"\n📝 *نص الإعلان:*\n{_esc(body_text[:600])}")
                    if ann.get("url"):
                        a_lines.append(f"\n🔗 [فتح الإعلان في LMS]({ann['url']})")
                    await self._send(tid, "\n".join(a_lines))
        except Exception as ane:
            logger.warning(f"Error checking announcements for course {cid} (user {tid}): {ane}")

        # 6. Attendance & Absence Radar
        try:
            attendance_list = content.get("attendance", [])
            for att in attendance_list:
                att_id = att["att_id"]
                old_att = db.get_attendance(tid, cid, att_id)
                is_first_att_seed = old_att is None

                old_sessions_map = {}
                if old_att and old_att.get("sessions_json"):
                    try:
                        for s in json.loads(old_att["sessions_json"]):
                            if s.get("date"):
                                old_sessions_map[s["date"]] = s.get("status", "")
                    except Exception:
                        pass

                new_sessions = att.get("sessions", [])
                db.upsert_attendance(
                    tid,
                    cid,
                    att_id,
                    taken_sessions=att.get("taken_sessions", 0),
                    total_sessions=att.get("total_sessions", 0),
                    present_count=att.get("present_count", 0),
                    absent_count=att.get("absent_count", 0),
                    late_count=att.get("late_count", 0),
                    excused_count=att.get("excused_count", 0),
                    points_str=att.get("points_str", ""),
                    percentage_str=att.get("percentage_str", ""),
                    sessions_json=json.dumps(new_sessions, ensure_ascii=False),
                    url=att.get("url", ""),
                )

                if not is_first_att_seed and user.get("notify_attendance", 1):
                    changed_sessions = []
                    for s in new_sessions:
                        s_date = s.get("date", "")
                        s_status = s.get("status", "")
                        if not s_date or not s_status:
                            continue
                        prev_st = old_sessions_map.get(s_date)
                        if prev_st != s_status:
                            changed_sessions.append((s, prev_st))

                    for s, prev_st in changed_sessions:
                        st_low = s.get("status", "").lower()
                        if any(k in st_low for k in ("present", "حاضر", "موجود")):
                            st_badge = "✅ حاضر (Present)"
                        elif any(k in st_low for k in ("absent", "غائب", "غياب")):
                            st_badge = "❌ غائب (Absent)"
                        elif any(k in st_low for k in ("late", "متأخر", "تأخير")):
                            st_badge = "⏳ متأخر (Late)"
                        elif any(k in st_low for k in ("excused", "معذور", "عذر")):
                            st_badge = "📝 معذور (Excused)"
                        else:
                            st_badge = f"📌 {s.get('status', '')}"

                        att_lines = [
                            "📋 *تحديث سجل الحضور والغياب في LMS!*",
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            f"📖 المادة: *{_esc(cname)}*",
                            f"📅 تاريخ المحاضرة: `{_esc(s.get('date', ''))}`",
                            f"📌 الحالة المسجلة: *{st_badge}*",
                        ]
                        if prev_st:
                            att_lines.append(f"🔄 _(تغيرت من: {_esc(prev_st)})_")
                        if s.get("points"):
                            att_lines.append(f"🎯 النقاط: `{_esc(s['points'])}`")
                        if s.get("remarks"):
                            att_lines.append(f"💬 ملاحظات: _{_esc(s['remarks'])}_")

                        att_lines.append(
                            f"\n📊 *ملخص حضورك في المادة:*\n"
                            f"✅ حاضر: `{att.get('present_count', 0)}`  |  "
                            f"❌ غائب: `{att.get('absent_count', 0)}`  |  "
                            f"📈 النسبة: `{_esc(att.get('percentage_str') or '—')}`"
                        )
                        if att.get("url"):
                            att_lines.append(f"🔗 [فتح سجل الحضور في LMS]({att['url']})")
                        await self._send(tid, "\n".join(att_lines))
        except Exception as atte:
            logger.warning(f"Error checking attendance for course {cid} (user {tid}): {atte}")

        return events, True

    def _collect_change(self, tid, cid, cname, item, content_type, enabled):
        content_id = str(item["id"])
        name = item.get("name", "")
        url = item.get("url", "")
        due = item.get("due_date", "")
        existing = db.get_seen_content(tid, content_id, content_type)
        is_new = existing is None
        changes = []
        if existing:
            old_name = existing.get("content_name") or ""
            old_url = existing.get("content_url") or ""
            old_due = existing.get("due_date") or ""
            if old_name != name:
                changes.append(
                    f"تغيّر الاسم: «{self._one_line(old_name)}» ← «{self._one_line(name)}»"
                )
            if old_due != due:
                changes.append(
                    f"تغيّر موعد التسليم: «{self._one_line(old_due)}» ← "
                    f"«{self._one_line(due)}»"
                )
            if _canonical_content_url(old_url) != _canonical_content_url(url):
                changes.append("تم تغيير رابط المحتوى")

        if not is_new and not changes:
            # Refresh the stored link or submission status if changed
            old_sub = bool(existing.get("is_submitted", 0))
            new_sub = bool(item.get("is_submitted", False))
            if (existing.get("content_url") or "") != url or (new_sub and not old_sub):
                db.update_seen_content(tid, content_id, content_type, name, url, due, is_submitted=1 if (old_sub or new_sub) else 0)
            return None

        is_sub_val = 1 if item.get("is_submitted") else 0
        if not enabled:
            if is_new:
                db.mark_seen(tid, cid, content_id, content_type, name, url, due, is_submitted=is_sub_val)
            else:
                db.update_seen_content(tid, content_id, content_type, name, url, due, is_submitted=is_sub_val)
            return None

        return {
            "telegram_id": tid,
            "course_id": cid,
            "course_name": cname,
            "content_id": content_id,
            "content_type": content_type,
            "name": name,
            "url": url,
            "due": due,
            "is_submitted": is_sub_val,
            "is_new": is_new,
            "changes": changes,
        }

    @staticmethod
    def _one_line(value):
        return " ".join(str(value or "غير محدد").split())

    def _build_change_chunks(self, events, max_chars=3400):
        if not events:
            return []

        labels = {"assign": "واجب", "quiz": "كويز", "resource": "مورد"}
        grouped = {}
        for event in events:
            grouped.setdefault(
                (event["course_id"], event["course_name"]), []
            ).append(event)

        chunks = []
        current_events = []
        current_lines = []
        current_course = None

        for (course_id, course_name), course_events in grouped.items():
            for event in course_events:
                course_key = (course_id, course_name)
                item_label = labels.get(event["content_type"], "محتوى")
                status = "🆕 جديد" if event["is_new"] else "✏️ تحديث"
                block = []
                if current_course != course_key:
                    block.append(f"📚 المادة: {self._one_line(course_name)}")
                block.append(
                    f"{status} {item_label}: {self._one_line(event['name'])}"
                )
                if event["is_new"]:
                    block.append(f"   التغيير: إضافة {item_label} جديد للمادة")
                else:
                    block.extend(f"   • {change}" for change in event["changes"])
                if event["is_new"] and event["due"]:
                    block.append(f"   • موعد التسليم: {self._one_line(event['due'])}")
                if event["url"]:
                    block.append(f"   🔗 {event['url']}")

                proposed = "\n".join(current_lines + block)
                if current_events and len(proposed) > max_chars:
                    chunks.append((current_events, "\n".join(current_lines)))
                    current_events = []
                    current_lines = []
                    current_course = None
                    if not block or not block[0].startswith("📚 المادة:"):
                        block.insert(0, f"📚 المادة: {self._one_line(course_name)}")

                current_lines.extend(block)
                current_events.append(event)
                current_course = course_key

        if current_events:
            chunks.append((current_events, "\n".join(current_lines)))

        total = len(events)
        return [
            (
                chunk_events,
                f"🔔 ملخص تغييرات المواد ({index}/{len(chunks)})\n"
                f"عدد التغييرات: {total}\n\n{body}",
            )
            for index, (chunk_events, body) in enumerate(chunks, start=1)
        ]

    @staticmethod
    def _mark_new_seen(event):
        db.mark_seen(
            event["telegram_id"], event["course_id"], event["content_id"],
            event["content_type"], event["name"], event["url"], event["due"],
            is_submitted=event.get("is_submitted", 0),
        )

    @staticmethod
    def _save_change(event):
        if not event["is_new"]:
            db.update_seen_content(
                event["telegram_id"], event["content_id"], event["content_type"],
                event["name"], event["url"], event["due"],
                is_submitted=event.get("is_submitted", 0),
            )

    async def _send(self, chat_id: int, text: str,
                    keyboard: InlineKeyboardMarkup = None, plain: bool = False):
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=None if plain else MD,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            await asyncio.sleep(0.3)
            return True
        except TelegramError as e:
            logger.warning(f"Notification send failed for {chat_id}; retrying plain: {e}")
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
            # Invalidate course cache for this user's courses to ensure fresh data
            for c in courses:
                self._course_cache.pop((tid, c["course_id"]), None)
            count, complete = await self._scan_user(user, courses, scraper, bypass_cache=True)
            if scraper.session_expired:
                self._auth_required.add(tid)
                await self._notify_auth_stopped(tid)
                return count, "انتهت جلسة LMS؛ أعد تسجيل الدخول لاستئناف المراقبة"
            if complete:
                db.update_last_check(tid)
                return count, "ok"
            return count, "تعذر إكمال الفحص؛ ستتم إعادة المحاولة"
