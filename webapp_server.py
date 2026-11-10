import os
import json
import time
import logging
import urllib.parse
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import database as db
from config import TELEGRAM_BOT_TOKEN

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

        # API: user data
        if path == "/api/user-data":
            tid_str = query.get("tid", "")
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

            # Fallback if only 1 user exists in DB
            if not tid:
                with db.DBContext() as ctx:
                    first_u = ctx.fetchone("SELECT telegram_id FROM users ORDER BY created_at DESC LIMIT 1")
                    if first_u:
                        tid = first_u["telegram_id"]

            if not tid:
                self._send_json({"success": False, "error": "المستخدم غير مسجل أو تعذر التحقق"}, 401)
                return

            user = db.get_user(tid)
            if not user:
                with db.DBContext() as ctx:
                    fallback_u = ctx.fetchone("SELECT telegram_id FROM users LIMIT 1")
                    if fallback_u:
                        tid = fallback_u["telegram_id"]
                        user = db.get_user(tid)

            if not user:
                self._send_json({"success": False, "error": "لم يتم العثور على حساب مرتبط بهذا المستخدم"}, 404)
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
                "last_check": user.get("last_check") or "لم يتم بعد",
            }

            courses = db.get_user_courses(tid)
            detailed_courses = []
            for c in courses:
                cid = c["course_id"]
                items = db.get_course_items(tid, cid)
                assigns = [i for i in items if i.get("content_type") == "assign"]
                quizzes = [i for i in items if i.get("content_type") == "quiz"]
                resources = [i for i in items if i.get("content_type") == "resource"]

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

            deadlines_raw = db.get_upcoming_deadlines(tid, limit=25)
            now_ts = int(time.time())
            deadlines = []
            for r in deadlines_raw:
                diff = r["due_ts"] - now_ts
                days = diff // 86400
                hours = (diff % 86400) // 3600
                time_remaining = f"{days} يوم و {hours} ساعة" if days > 0 else f"{hours} ساعة"
                if diff <= 3600:
                    time_remaining = "ساعة أو أقل!"

                deadlines.append({
                    "id": r["id"],
                    "content_id": r["content_id"],
                    "content_name": r.get("content_name", "واجب"),
                    "content_url": r.get("content_url", ""),
                    "course_id": r["course_id"],
                    "course_name": r.get("course_name", "مادة"),
                    "due_str": r.get("due_str", ""),
                    "due_ts": r["due_ts"],
                    "time_remaining": time_remaining,
                    "is_submitted": bool(r.get("is_submitted", 0)),
                })

            stats = db.get_stats(tid)

            # Calculate submission progress
            all_deadlines = db.get_upcoming_deadlines(tid, limit=100)
            total_deadlines = len(all_deadlines)
            submitted_deadlines = len([d for d in all_deadlines if d.get("is_submitted")])
            progress_pct = int((submitted_deadlines / total_deadlines) * 100) if total_deadlines > 0 else 100

            self._send_json({
                "success": True,
                "user": user_safe,
                "courses": detailed_courses,
                "deadlines": deadlines,
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

        tid = payload.get("tid")
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

        if not tid:
            with db.DBContext() as ctx:
                f_u = ctx.fetchone("SELECT telegram_id FROM users LIMIT 1")
                if f_u:
                    tid = f_u["telegram_id"]

        if not tid:
            self._send_json({"success": False, "error": "المستخدم غير معتمد"}, 401)
            return

        user = db.get_user(tid)
        if not user:
            with db.DBContext() as ctx:
                f_u = ctx.fetchone("SELECT telegram_id FROM users LIMIT 1")
                if f_u:
                    tid = f_u["telegram_id"]
                    user = db.get_user(tid)

        if not user:
            self._send_json({"success": False, "error": "المستخدم غير موجود"}, 404)
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
            db.mark_assignment_submitted(tid, content_id, is_sub)
            self._send_json({"success": True, "content_id": content_id, "is_submitted": bool(is_sub)})
            return

        # 4. Update user settings
        if path == "/api/update-settings":
            if "check_interval" in payload:
                db.update_check_interval(tid, int(payload["check_interval"]))
            if any(k in payload for k in ("notify_assign", "notify_quiz", "notify_resource")):
                db.update_notify_settings(
                    tid,
                    assign=payload.get("notify_assign"),
                    quiz=payload.get("notify_quiz"),
                    resource=payload.get("notify_resource")
                )
            self._send_json({"success": True, "message": "تم حفظ الإعدادات"})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # Quiet standard HTTP logs to avoid cluttering bot.log
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), WebAppRequestHandler)
    logger.info(f"WebApp server running on port {port}")
    server.serve_forever()

