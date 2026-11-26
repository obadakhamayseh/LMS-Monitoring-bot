import os
import re
import json
import time
import logging
import urllib.parse
import hmac
import hashlib
import secrets
import threading
import concurrent.futures
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

# ── Phone-app device pairing ────────────────────────────────────────────
# The owner asks the Telegram bot for a one-time code (/app); the phone app
# exchanges it for a long-lived device token. Codes live in memory only.
_PAIR_TTL = 600
_PAIR_MAX_FAILS = 8
_pair_lock = threading.Lock()
_pair_codes = {}          # code -> (telegram_id, expires_ts)
_pair_fail_times = []     # timestamps of recent failed attempts (global)

def create_pairing_code(telegram_id: int) -> str:
    code = "".join(secrets.choice("0123456789") for _ in range(8))
    now = time.time()
    with _pair_lock:
        for c in [c for c, (_, exp) in _pair_codes.items() if exp < now]:
            _pair_codes.pop(c, None)
        _pair_codes[code] = (int(telegram_id), now + _PAIR_TTL)
    return code

def _consume_pairing_code(code: str):
    """Return telegram_id for a valid code (single use), else None. Throttled globally."""
    now = time.time()
    code = re.sub(r"\D", "", str(code or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    with _pair_lock:
        _pair_fail_times[:] = [t for t in _pair_fail_times if now - t < 300]
        if len(_pair_fail_times) >= _PAIR_MAX_FAILS:
            _pair_codes.clear()  # too many wrong guesses: invalidate everything
            return None
        entry = _pair_codes.pop(code, None) if code else None
        if entry and entry[1] >= now:
            return entry[0]
        _pair_fail_times.append(now)
        return None

_STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/logo.jpg": ("logo.jpg", "image/jpeg"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}

class WebAppRequestHandler(BaseHTTPRequestHandler):

    def _authenticate(self, init_data: str = "") -> Optional[int]:
        """Return the owner's telegram_id if the request carries a valid device
        token or a Telegram-signed initData; otherwise None."""
        auth = self.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            tid = db.verify_app_token(auth[7:].strip())
            if tid and int(tid) == int(OWNER_TELEGRAM_ID):
                return int(tid)
        if init_data:
            tid = _validate_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN)
            if tid and int(tid) == int(OWNER_TELEGRAM_ID):
                return int(tid)
        return None

    def _send_static(self, rel_path: str, ctype: str, no_cache: bool = False):
        full = os.path.join(_STATIC_DIR, rel_path)
        if not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache" if no_cache else "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

        # PWA assets (manifest, service worker, icons)
        if path in _STATIC_FILES:
            rel, ctype = _STATIC_FILES[path]
            self._send_static(rel, ctype, no_cache=(path in ("/sw.js", "/manifest.webmanifest")))
            return

        # API: user data (Personal Bot — locked to OWNER_TELEGRAM_ID)
        if path == "/api/user-data":
            tid = self._authenticate(query.get("initData", ""))
            if not tid:
                self._send_json({"success": False, "auth_required": True,
                                 "error": "يلزم ربط هذا الجهاز بالتطبيق"}, 401)
                return

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
                "notify_attendance": bool(user.get("notify_attendance", 1)),
                "last_check": user.get("last_check") or "لم يتم بعد",
            }

            courses = db.get_user_courses(tid)
            course_map = {c["course_id"]: c["course_name"] for c in courses}
            detailed_courses = []
            all_assigns_and_quizzes = []

            # Build unified submission map from both reminders and seen_content tables
            with db.DBContext() as ctx:
                rem_rows = ctx.fetchall("SELECT content_id, is_submitted FROM reminders WHERE telegram_id=?", (tid,))
                seen_rows = ctx.fetchall("SELECT content_id, is_submitted FROM seen_content WHERE telegram_id=? AND (content_type in ('assign', 'quiz'))", (tid,))

            sub_map = {}
            for r in rem_rows:
                if r.get("is_submitted"):
                    cid_s = str(r["content_id"])
                    raw = cid_s.replace("assign_", "").replace("quiz_", "")
                    sub_map[cid_s] = True
                    sub_map[raw] = True
                    sub_map[f"assign_{raw}"] = True
                    sub_map[f"quiz_{raw}"] = True
            for s in seen_rows:
                if s.get("is_submitted"):
                    cid_s = str(s["content_id"])
                    raw = cid_s.replace("assign_", "").replace("quiz_", "")
                    sub_map[cid_s] = True
                    raw = cid_s.replace("assign_", "").replace("quiz_", "")
                    sub_map[raw] = True
                    sub_map[f"assign_{raw}"] = True
                    sub_map[f"quiz_{raw}"] = True

            for c in courses:
                cid = c["course_id"]
                items = db.get_course_items(tid, cid)
                assigns = [i for i in items if i.get("content_type") == "assign"]
                quizzes = [i for i in items if i.get("content_type") == "quiz"]
                resources = [i for i in items if i.get("content_type") == "resource"]

                for aq in assigns + quizzes:
                    all_assigns_and_quizzes.append((c["course_name"], aq))

                done_tasks = sum(1 for i in assigns + quizzes if bool(i.get("is_submitted")) or sub_map.get(str(i.get("content_id")), False))
                detailed_courses.append({
                    "id": cid,
                    "name": c["course_name"],
                    "url": c.get("course_url", f"https://mulms.mutah.edu.jo/course/view.php?id={cid}"),
                    "is_monitored": bool(c.get("is_monitored", 1)),
                    "added_at": c.get("added_at", ""),
                    "stats": {
                        "assignments": len(assigns),
                        "quizzes": len(quizzes),
                        "resources": len(resources),
                        "tasks_total": len(assigns) + len(quizzes),
                        "tasks_done": done_tasks,
                    },
                    "items": items,
                })

            all_files = []
            for c_obj in detailed_courses:
                cname = c_obj["name"]
                cid = c_obj["id"]
                for it in c_obj.get("items", []):
                    if it.get("content_type") != "resource":
                        continue
                    fname = it.get("content_name") or "مورد دراسي"
                    furl = it.get("content_url") or ""
                    low = (fname + " " + furl).lower()
                    if ".pdf" in low or "pdf" in low:
                        fkind = "PDF"
                    elif any(x in low for x in (".doc", ".docx", "word")):
                        fkind = "DOCX"
                    elif any(x in low for x in (".ppt", ".pptx", "slide", "شرائح", "عرض")):
                        fkind = "PPTX"
                    elif any(x in low for x in (".xls", ".xlsx", "csv")):
                        fkind = "XLSX"
                    elif any(x in low for x in (".zip", ".rar")):
                        fkind = "ZIP"
                    elif any(x in low for x in ("video", "youtube", "mp4", "تسجيل", "فيديو")):
                        fkind = "فيديو"
                    elif "/mod/folder/" in low:
                        fkind = "مجلد"
                    elif "/mod/url/" in low:
                        fkind = "رابط"
                    elif "/mod/page/" in low:
                        fkind = "صفحة"
                    else:
                        fkind = "ملف / مورد"
                    all_files.append({
                        "id": str(it.get("content_id") or it.get("id") or ""),
                        "name": fname,
                        "course_id": cid,
                        "course": cname,
                        "kind": fkind,
                        "url": furl,
                        "date": str(it.get("first_seen") or ""),
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

                c_id_str = str(r["content_id"])
                seen_deadline_ids.add(c_id_str)
                is_sub = bool(r.get("is_submitted", 0)) or sub_map.get(c_id_str, False)
                deadlines.append({
                    "id": r["id"],
                    "content_id": c_id_str,
                    "content_type": "quiz" if c_id_str.startswith("quiz_") else "assign",
                    "content_name": r.get("content_name", "واجب"),
                    "content_url": r.get("content_url", ""),
                    "course_id": r["course_id"],
                    "course_name": r.get("course_name") or course_map.get(r["course_id"], "مادة"),
                    "due_str": r.get("due_str", ""),
                    "due_ts": r["due_ts"],
                    "time_remaining": time_remaining,
                    "is_submitted": is_sub,
                    "is_upcoming": True,
                })

            for cname, aq in all_assigns_and_quizzes:
                cid_key = str(aq.get("content_id", ""))
                if not cid_key or cid_key in seen_deadline_ids:
                    continue
                seen_deadline_ids.add(cid_key)
                is_done = bool(aq.get("is_submitted", 0)) or sub_map.get(cid_key, False)
                deadlines.append({
                    "id": aq.get("id", 0),
                    "content_id": cid_key,
                    "content_type": aq.get("content_type", "assign"),
                    "content_name": aq.get("content_name", "واجب"),
                    "content_url": aq.get("content_url", ""),
                    "course_id": aq.get("course_id", 0),
                    "course_name": cname,
                    "due_str": aq.get("due_date") or "",
                    "first_seen": aq.get("first_seen") or "",
                    "due_ts": 0,
                    "time_remaining": "",
                    "is_submitted": is_done,
                    "is_upcoming": False,
                })

            stats = db.get_stats(tid)
            raw_grades = db.get_user_grades(tid)
            grades = [
                g for g in raw_grades
                if "/mod/attendance/" not in (g.get("url") or "")
                and (g.get("item_name") or "").strip().lower() not in ("attendance", "الحضور والغياب", "الحضور")
            ]
            announcements = db.get_user_announcements(tid, limit=30)
            raw_attendance = db.get_user_attendance(tid)
            attendance = []
            for att_row in raw_attendance:
                sessions_list = []
                if att_row.get("sessions_json"):
                    try:
                        sessions_list = json.loads(att_row["sessions_json"])
                    except Exception:
                        sessions_list = []
                att_copy = dict(att_row)
                att_copy["sessions"] = sessions_list
                attendance.append(att_copy)

            total_deadlines = len(deadlines)
            submitted_deadlines = len([d for d in deadlines if d.get("is_submitted")])
            progress_pct = int((submitted_deadlines / total_deadlines) * 100) if total_deadlines > 0 else 100
            user_meta = db.get_user_meta(tid)

            self._send_json({
                "success": True,
                "user": user_safe,
                "courses": detailed_courses,
                "deadlines": deadlines,
                "grades": grades,
                "announcements": announcements,
                "attendance": attendance,
                "files": all_files,
                "user_meta": user_meta,
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

        # Device pairing (public endpoint, protected by one-time code + throttling)
        if path == "/api/pair":
            owner_tid = _consume_pairing_code(payload.get("code"))
            if not owner_tid or int(owner_tid) != int(OWNER_TELEGRAM_ID):
                self._send_json({"success": False, "error": "الرمز غير صحيح أو منتهي الصلاحية. أرسل /app للبوت للحصول على رمز جديد."}, 400)
                return
            device = str(payload.get("device") or self.headers.get("User-Agent", ""))[:120]
            token = db.create_app_session(owner_tid, device)
            self._send_json({"success": True, "token": token})
            return

        tid = self._authenticate(payload.get("initData", ""))
        if not tid:
            self._send_json({"success": False, "auth_required": True,
                             "error": "يلزم ربط هذا الجهاز بالتطبيق"}, 401)
            return

        # Unlink only this device (does not touch the LMS account or Telegram alerts)
        if path == "/api/app-logout":
            auth = self.headers.get("Authorization", "") or ""
            if auth.startswith("Bearer "):
                db.revoke_app_token(auth[7:].strip())
            self._send_json({"success": True})
            return

        def _persist_course_content(cid, content):
            assigns = content.get("assignments", [])
            quizzes = content.get("quizzes", [])
            for item in assigns:
                db.mark_seen(tid, cid, item["id"], "assign", item["name"], item["url"],
                             item.get("due_date", ""), is_submitted=1 if item.get("is_submitted") else 0)
            for item in quizzes:
                db.mark_seen(tid, cid, item["id"], "quiz", item["name"], item["url"],
                             item.get("due_date", ""), is_submitted=1 if item.get("is_submitted") else 0)
            for item in content.get("resources", []):
                db.mark_seen(tid, cid, item["id"], "resource", item["name"], item["url"])
            try:
                for g in content.get("grades", []):
                    db.upsert_grade(tid, cid, g["item_id"], g.get("item_name", ""),
                                    g.get("grade_val", ""), g.get("grade_max", ""),
                                    g.get("percentage", ""), g.get("feedback", ""), g.get("url", ""))
            except Exception as ge:
                logger.warning(f"Error persisting grades for course {cid}: {ge}")
            try:
                for ann in content.get("announcements", []):
                    db.upsert_announcement(tid, cid, ann["discuss_id"], ann.get("forum_name", ""),
                                           ann.get("title", ""), ann.get("author", ""),
                                           ann.get("posted_date", ""), "", ann.get("url", ""))
            except Exception as ae:
                logger.warning(f"Error persisting announcements for course {cid}: {ae}")
            try:
                for att in content.get("attendance", []):
                    db.upsert_attendance(
                        tid, cid, att["att_id"],
                        taken_sessions=att.get("taken_sessions", 0),
                        total_sessions=att.get("total_sessions", 0),
                        present_count=att.get("present_count", 0),
                        absent_count=att.get("absent_count", 0),
                        late_count=att.get("late_count", 0),
                        excused_count=att.get("excused_count", 0),
                        points_str=att.get("points_str", ""),
                        percentage_str=att.get("percentage_str", ""),
                        sessions_json=json.dumps(att.get("sessions", []), ensure_ascii=False),
                        url=att.get("url", ""),
                    )
            except Exception as atte:
                logger.warning(f"Error persisting attendance for course {cid}: {atte}")
            if _REMINDER_ENGINE and (assigns or quizzes):
                try:
                    _REMINDER_ENGINE.register_assignments(tid, cid, assigns + quizzes)
                except Exception as re:
                    logger.warning(f"Error registering reminders for course {cid}: {re}")
            for task_item in assigns + quizzes:
                if task_item.get("is_submitted"):
                    db.mark_assignment_submitted(tid, task_item["id"], 1)

        # 0. Direct Login from Mini App (No auto-import of courses — user adds only desired courses)
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
                if _ENGINE:
                    try:
                        _ENGINE._auth_required.discard(tid)
                        _ENGINE._auth_alerted.discard(tid)
                        _ENGINE._waf_backoff_until.pop(tid, None)
                        _ENGINE._transient_backoff_until.pop(tid, None)
                    except Exception:
                        pass
                self._send_json({"success": True})
            finally:
                scraper.close()
            return

        # 0.b Logout from Session
        if path == "/api/logout":
            if _ENGINE:
                try:
                    _ENGINE._discard_session(tid, logout=True)
                    _ENGINE._auth_required.discard(tid)
                    _ENGINE._auth_alerted.discard(tid)
                except Exception:
                    pass
            db.delete_user(tid)
            self._send_json({"success": True})
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
            if "is_monitoring" in payload:
                db.set_monitoring(tid, bool(payload["is_monitoring"]))
            if any(k in payload for k in ("notify_assign", "notify_quiz", "notify_resource", "notify_grade", "notify_announcement", "notify_attendance")):
                db.update_notify_settings(
                    tid,
                    assign=payload.get("notify_assign"),
                    quiz=payload.get("notify_quiz"),
                    resource=payload.get("notify_resource"),
                    grade=payload.get("notify_grade"),
                    announcement=payload.get("notify_announcement"),
                    attendance=payload.get("notify_attendance"),
                )
            self._send_json({"success": True, "message": "تم حفظ الإعدادات"})
            return

        # 4.b Save personal user metadata (notes, personal reminders, read announcements, absence limit, course schedule)
        if path == "/api/user-meta":
            meta_patch = payload.get("meta")
            if not isinstance(meta_patch, dict):
                self._send_json({"success": False, "error": "بيانات غير صالحة"}, 400)
                return
            updated_meta = db.update_user_meta(tid, meta_patch)
            self._send_json({"success": True, "user_meta": updated_meta})
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
                    db.add_course(tid, c["id"], c["name"], c["url"])
                    added += 1

                def _fetch_sync_course(c):
                    cid = c["id"]
                    try:
                        return cid, scraper.get_course_content(cid)
                    except Exception as e:
                        logger.warning(f"Error syncing course {cid}: {e}")
                        return cid, {}

                if courses_list:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(courses_list))) as pool:
                        sync_results = list(pool.map(_fetch_sync_course, courses_list))
                    for cid, content in sync_results:
                        if content:
                            _persist_course_content(cid, content)

                if scraper.waf_blocked:
                    self._send_json({"success": False, "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق"}, 429)
                    return

                db.update_last_check(tid)
                db.append_sync_log(tid, "استيراد المقررات", f"تم جلب ومزامنة {added} مقررات من LMS")
                self._send_json({"success": True, "synced_count": added})
            finally:
                scraper.close()
            return

        # 5. Delete specific course or clear all courses
        if path == "/api/delete-course":
            if payload.get("clear_all"):
                db.clear_all_user_courses(tid)
                self._send_json({"success": True, "cleared_all": True})
                return
            course_id = payload.get("course_id")
            if not course_id:
                self._send_json({"success": False, "error": "course_id مطلوب"}, 400)
                return
            db.remove_course(tid, int(course_id))
            self._send_json({"success": True, "course_id": int(course_id)})
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
                db.update_last_check(tid)
                db.append_sync_log(tid, "إضافة مقرر", f"تم ربط ومزامنة مقرر: {cname}")
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

                def _fetch_single_course(c):
                    cid = c["course_id"]
                    try:
                        c_content = scraper.get_course_content(cid)
                        return cid, c_content
                    except Exception as e:
                        logger.warning(f"Error fetching course {cid} in force-check: {e}")
                        return cid, {}

                max_w = min(2, len(courses))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
                    course_results = list(pool.map(_fetch_single_course, courses))

                if scraper.waf_blocked:
                    self._send_json({"success": False, "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق"}, 429)
                    return

                for cid, content in course_results:
                    if not content:
                        continue
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
                detail_msg = f"تم فحص {len(courses)} مقررات — رُصد {new_items} عنصر جديد" if new_items > 0 else f"تم فحص {len(courses)} مقررات — كافة البيانات محدّثة"
                db.append_sync_log(tid, "اكتمل الفحص الفوري", detail_msg)
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

