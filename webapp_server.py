import os
import re
import json
import time
import logging
import urllib.parse
import hmac
import hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional

import database as db
from lms_scraper import LMSScraper
from config import TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID

logger = logging.getLogger(__name__)

# References to engines passed from bot.py
_ENGINE = None
_REMINDER_ENGINE = None

def init_webapp_engines(engine=None, reminder_engine=None):
    global _ENGINE, _REMINDER_ENGINE
    _ENGINE = engine
    _REMINDER_ENGINE = reminder_engine

def _validate_telegram_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """Validate Telegram WebApp initData HMAC and return telegram_id if valid."""
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed:
            return None
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calculated_hash, received_hash):
            user_json = parsed.get("user", "{}")
            user_data = json.loads(user_json)
            return user_data.get("id")
    except Exception as e:
        logger.debug(f"initData validation failed: {e}")
    return None

def _json_serial(obj):
    """Serialize datetime and non-standard objects to string/isoformat."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)

class WebAppRequestHandler(BaseHTTPRequestHandler):

    def _send_json(self, data: dict, status_code: int = 200):
        body = json.dumps(data, default=_json_serial, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_content: str, status_code: int = 200):
        body = html_content.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        query = dict(urllib.parse.parse_qsl(parsed_url.query))

        # Health check
        if path in ("", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Mini App UI
        if path in ("/webapp", "/app"):
            html_file = os.path.join(os.path.dirname(__file__), "webapp.html")
            if os.path.exists(html_file):
                with open(html_file, "r", encoding="utf-8") as f:
                    content = f.read()
                self._send_html(content)
            else:
                self._send_html("<h1>Mini App template not found</h1>", 404)
            return

        # API: user data (Personal Bot — locked to OWNER_TELEGRAM_ID)
        if path == "/api/user-data":
            tid_str = str(query.get("tid", "")).strip()
            init_data = query.get("initData", "")

            tid = None
            if tid_str.isdigit():
                tid = int(tid_str)

            if not tid and init_data:
                tid = _validate_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN)
                if not tid:
                    try:
                        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
                        if "user" not in parsed:
                            parsed = dict(urllib.parse.parse_qsl(urllib.parse.unquote(init_data), keep_blank_values=True))
                        if "user" in parsed:
                            user_data = json.loads(parsed["user"])
                            tid = user_data.get("id")
                    except Exception:
                        pass

            if tid and int(tid) != int(OWNER_TELEGRAM_ID):
                self._send_json({"success": False, "error": "🔒 هذا النظام خاص بصاحبه فقط"}, 403)
                return

            tid = int(OWNER_TELEGRAM_ID)

            user = db.get_user(tid)
            if not user:
                self._send_json({
                    "success": False,
                    "not_registered": True,
                    "tid": tid,
                    "error": "لم يتم العثور على حساب مرتبط؛ سجل دخولك أدناه"
                }, 200)
                return

            # Sanitize user info (omit password)
            user_safe = {
                "telegram_id": user["telegram_id"],
                "lms_username": user["lms_username"],
                "full_name": user.get("full_name") or user["lms_username"],
                "is_monitoring": bool(user.get("is_monitoring", 0)),
                "check_interval": user.get("check_interval", 10),
                "notify_assign": bool(user.get("notify_assign", 1)),
                "notify_quiz": bool(user.get("notify_quiz", 1)),
                "notify_resource": bool(user.get("notify_resource", 1)),
                "notify_grade": bool(user.get("notify_grade", 1)),
                "notify_announcement": bool(user.get("notify_announcement", 1)),
                "last_check": user.get("last_check") or "لم يتم بعد",
            }

            courses = db.get_user_courses(tid)
            course_map = {c["course_id"]: c["course_name"] for c in courses}
            detailed_courses = []
            all_assigns_and_quizzes = []

            for c in courses:
                cid = c["course_id"]
                items = db.get_course_items(tid, cid)
                assigns = [i for i in items if i.get("content_type") == "assign"]
                quizzes = [i for i in items if i.get("content_type") == "quiz"]
                resources = [i for i in items if i.get("content_type") == "resource"]

                for aq in assigns + quizzes:
                    all_assigns_and_quizzes.append((c["course_name"], aq))

                detailed_courses.append({
                    "id": cid,
                    "name": c["course_name"],
                    "url": c.get("course_url", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}"),
                    "is_monitored": bool(c.get("is_monitored", 1)),
                    "stats": {
                        "assignments": len(assigns),
                        "quizzes": len(quizzes),
                        "resources": len(resources),
                    },
                    "items": items,
                })

            deadlines_raw = db.get_upcoming_deadlines(tid, limit=50)
            now_ts = int(time.time())
            deadlines = []
            seen_deadline_ids = set()

            for r in deadlines_raw:
                diff = r["due_ts"] - now_ts
                days = diff // 86400
                hours = (diff % 86400) // 3600
                time_remaining = f"{days} يوم و {hours} ساعة" if days > 0 else f"{hours} ساعة"
                if diff <= 3600:
                    time_remaining = "ساعة أو أقل!"

                seen_deadline_ids.add(str(r["content_id"]))
                deadlines.append({
                    "id": r["id"],
                    "content_id": r["content_id"],
                    "content_type": "assign",
                    "content_name": r.get("content_name", "واجب"),
                    "content_url": r.get("content_url", ""),
                    "course_id": r["course_id"],
                    "course_name": r.get("course_name") or course_map.get(r["course_id"], "مادة"),
                    "due_str": r.get("due_str", ""),
                    "due_ts": r["due_ts"],
                    "time_remaining": time_remaining,
                    "is_submitted": bool(r.get("is_submitted", 0)),
                    "is_upcoming": True,
                })

            # Also include all other assignments and quizzes from seen_content so the tasks tab is always populated
            with db.DBContext() as ctx:
                rem_rows = ctx.fetchall("SELECT content_id, is_submitted FROM reminders WHERE telegram_id=?", (tid,))
                sub_map = {str(r["content_id"]): bool(r.get("is_submitted", 0)) for r in rem_rows}

            for cname, aq in all_assigns_and_quizzes:
                cid_key = str(aq.get("content_id", ""))
                if not cid_key or cid_key in seen_deadline_ids:
                    continue
                seen_deadline_ids.add(cid_key)
                deadlines.append({
                    "id": aq.get("id", 0),
                    "content_id": cid_key,
                    "content_type": aq.get("content_type", "assign"),
                    "content_name": aq.get("content_name", "واجب"),
                    "content_url": aq.get("content_url", ""),
                    "course_id": aq.get("course_id", 0),
                    "course_name": cname,
                    "due_str": aq.get("due_date") or "متاح في المقرر",
                    "due_ts": 0,
                    "time_remaining": "",
                    "is_submitted": sub_map.get(cid_key, False),
                    "is_upcoming": False,
                })

            stats = db.get_stats(tid)
            grades = db.get_user_grades(tid)
            announcements = db.get_user_announcements(tid, limit=30)

            total_deadlines = len(deadlines)
            submitted_deadlines = len([d for d in deadlines if d.get("is_submitted")])
            progress_pct = int((submitted_deadlines / total_deadlines) * 100) if total_deadlines > 0 else 100

            self._send_json({
                "success": True,
                "user": user_safe,
                "courses": detailed_courses,
                "deadlines": deadlines,
                "grades": grades,
                "announcements": announcements,
                "stats": stats,
                "progress": {
                    "total": total_deadlines,
                    "submitted": submitted_deadlines,
                    "percentage": progress_pct,
                }
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(post_body)
        except Exception:
            payload = {}

        tid_raw = str(payload.get("tid") or "").strip()
        tid = int(tid_raw) if tid_raw.isdigit() else None
        if not tid and payload.get("initData"):
            tid = _validate_telegram_init_data(payload["initData"], TELEGRAM_BOT_TOKEN)
            if not tid:
                try:
                    p = dict(urllib.parse.parse_qsl(payload["initData"], keep_blank_values=True))
                    if "user" not in p:
                        p = dict(urllib.parse.parse_qsl(urllib.parse.unquote(payload["initData"]), keep_blank_values=True))
                    if "user" in p:
                        tid = json.loads(p["user"]).get("id")
                except Exception:
                    pass

        if tid and int(tid) != int(OWNER_TELEGRAM_ID):
            self._send_json({"success": False, "error": "🔒 هذا النظام خاص بصاحبه فقط"}, 403)
            return

        tid = int(OWNER_TELEGRAM_ID)

        def _persist_course_content(cid, content):
            for item in content.get("assignments", []):
                db.mark_seen(tid, cid, item["id"], "assign", item["name"], item["url"], item.get("due_date", ""))
            for item in content.get("quizzes", []):
                db.mark_seen(tid, cid, item["id"], "quiz", item["name"], item["url"], item.get("due_date", ""))
            for item in content.get("resources", []):
                db.mark_seen(tid, cid, item["id"], "resource", item["name"], item["url"])
            for g in content.get("grades", []):
                db.upsert_grade(tid, cid, g["item_id"], g.get("item_name", ""),
                                g.get("grade_val", ""), g.get("grade_max", ""),
                                g.get("percentage", ""), g.get("feedback", ""), g.get("url", ""))
            for ann in content.get("announcements", []):
                db.upsert_announcement(tid, cid, ann["discuss_id"], ann.get("forum_name", ""),
                                       ann.get("title", ""), ann.get("author", ""),
                                       ann.get("posted_date", ""), "", ann.get("url", ""))
            if _REMINDER_ENGINE and content.get("assignments"):
                _REMINDER_ENGINE.register_assignments(tid, cid, content["assignments"])
            for a in content.get("assignments", []):
                if a.get("is_submitted"):
                    db.mark_assignment_submitted(tid, a["id"], 1)

        # 0. Direct Login from Mini App
        if path == "/api/login":
            raw_user = str(payload.get("username") or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
            raw_pass = str(payload.get("password") or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
            if not raw_user or not raw_pass:
                self._send_json({"success": False, "error": "يرجى إدخال الرقم الجامعي وكلمة المرور"}, 400)
                return

            scraper = LMSScraper(raw_user, raw_pass)
            try:
                if not scraper.login():
                    self._send_json({"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}, 401)
                    return

                db.save_user(tid, raw_user, raw_pass, scraper.user_id, scraper.full_name)
                # Auto-fetch courses on login
                courses_list = scraper.get_courses()
                for c in courses_list:
                    cid = c["id"]
                    db.add_course(tid, cid, c["name"], c["url"])
                    content = scraper.get_course_content(cid)
                    _persist_course_content(cid, content)
                if courses_list:
                    db.set_monitoring(tid, True)
                    db.update_last_check(tid)
                self._send_json({"success": True, "courses_synced": len(courses_list)})
            finally:
                scraper.close()
            return

        user = db.get_user(tid)
        if not user:
            self._send_json({"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}, 404)
            return

        # 1. Toggle overall monitoring
        if path == "/api/toggle-monitor":
            new_status = not bool(user.get("is_monitoring", 0))
            db.set_monitoring(tid, new_status)
            self._send_json({"success": True, "is_monitoring": new_status})
            return

        # 2. Toggle specific course
        if path == "/api/toggle-course":
            course_id = payload.get("course_id")
            if not course_id:
                self._send_json({"success": False, "error": "course_id مطلوب"}, 400)
                return
            db.toggle_course(tid, int(course_id))
            course = db.get_course(tid, int(course_id))
            is_mon = bool(course.get("is_monitored", 1)) if course else False
            self._send_json({"success": True, "course_id": course_id, "is_monitored": is_mon})
            return

        # 3. Mark assignment as submitted
        if path == "/api/mark-submitted":
            content_id = payload.get("content_id")
            is_sub = payload.get("is_submitted", 1)
            if not content_id:
                self._send_json({"success": False, "error": "content_id مطلوب"}, 400)
                return
            # Ensure reminder row exists or update it
            with db.DBContext() as ctx:
                existing_rem = ctx.fetchone("SELECT id FROM reminders WHERE telegram_id=? AND content_id=?", (tid, str(content_id)))
                if existing_rem:
                    db.mark_assignment_submitted(tid, content_id, is_sub)
                else:
                    sc = ctx.fetchone("SELECT * FROM seen_content WHERE telegram_id=? AND content_id=?", (tid, str(content_id)))
                    if sc:
                        db.upsert_reminder(
                            tid, sc["course_id"], str(content_id),
                            sc.get("content_name", ""), sc.get("content_url", ""),
                            None, sc.get("due_date", ""), is_submitted=1 if is_sub else 0
                        )
            self._send_json({"success": True, "content_id": content_id, "is_submitted": bool(is_sub)})
            return

        # 4. Update user settings
        if path == "/api/update-settings":
            if "check_interval" in payload:
                db.update_check_interval(tid, int(payload["check_interval"]))
            if any(k in payload for k in ("notify_assign", "notify_quiz", "notify_resource", "notify_grade", "notify_announcement")):
                db.update_notify_settings(
                    tid,
                    assign=payload.get("notify_assign"),
                    quiz=payload.get("notify_quiz"),
                    resource=payload.get("notify_resource"),
                    grade=payload.get("notify_grade"),
                    announcement=payload.get("notify_announcement"),
                )
            self._send_json({"success": True, "message": "تم حفظ الإعدادات"})
            return

        # 5. Sync courses from LMS directly via Mini App
        if path == "/api/sync-courses":
            scraper = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not scraper.login():
                    self._send_json({"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}, 400)
                    return
                courses_list = scraper.get_courses()
                added = 0
                for c in courses_list:
                    cid = c["id"]
                    db.add_course(tid, cid, c["name"], c["url"])
                    added += 1
                    content = scraper.get_course_content(cid)
                    _persist_course_content(cid, content)
                db.update_last_check(tid)
                self._send_json({"success": True, "synced_count": added})
            finally:
                scraper.close()
            return

        # 6. Add course by ID or URL directly via Mini App
        if path == "/api/add-course":
            raw_input = str(payload.get("course_id") or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
            m = re.search(r"(?:id=|course=)(\d+)", raw_input)
            cid_str = m.group(1) if m else raw_input
            if not cid_str.isdigit():
                self._send_json({"success": False, "error": "يرجى إدخال رقم مادة صحيح أو رابط المقرر"}, 400)
                return
            cid = int(cid_str)
            scraper = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not scraper.login():
                    self._send_json({"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}, 400)
                    return
                cname, content = scraper.get_course_info(cid)
                if not cname:
                    self._send_json({"success": False, "error": f"لم يتم العثور على مادة بالرقم {cid}"}, 404)
                    return
                curl = f"https://mulms.mutah.edu.jo/course/view.php?id={cid}"
                db.add_course(tid, cid, cname, curl)
                _persist_course_content(cid, content)
                self._send_json({"success": True, "course_name": cname, "course_id": cid})
            finally:
                scraper.close()
            return

        # 7. Real Force Check across monitored courses
        if path == "/api/force-check":
            courses = db.get_user_courses(tid, monitored_only=True)
            if not courses:
                self._send_json({"success": False, "error": "لا توجد مقررات مفعلة للمراقبة"}, 400)
                return
            scraper = LMSScraper(user["lms_username"], user["lms_password"])
            try:
                if not scraper.login():
                    self._send_json({"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}, 400)
                    return
                new_items = 0
                for c in courses:
                    cid = c["course_id"]
                    content = scraper.get_course_content(cid)
                    for item in content.get("assignments", []):
                        if not db.is_seen(tid, item["id"], "assign"):
                            new_items += 1
                    for item in content.get("quizzes", []):
                        if not db.is_seen(tid, item["id"], "quiz"):
                            new_items += 1
                    for item in content.get("resources", []):
                        if not db.is_seen(tid, item["id"], "resource"):
                            new_items += 1
                    _persist_course_content(cid, content)
                db.update_last_check(tid)
                self._send_json({"success": True, "new_items": new_items})
            finally:
                scraper.close()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # Quiet standard HTTP logs to avoid cluttering bot.log
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebAppRequestHandler)
    logger.info(f"WebApp server running on port {port}")
    server.serve_forever()

