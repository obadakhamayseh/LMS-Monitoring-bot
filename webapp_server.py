import os
import re
import ssl
import json
import time
import hmac
import socket
import hashlib
import logging
import secrets
import threading
import http.cookies
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional

import database as db
from lms_scraper import LMSScraper
from config import TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID

logger = logging.getLogger(__name__)

# Pre-auth body cap (C5) and Telegram initData freshness window (H1)
MAX_BODY_BYTES = 64 * 1024
INIT_DATA_MAX_AGE = 300
INIT_DATA_MAX_FUTURE_SKEW = 60
INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Cookie & CSRF settings (H16)
SESSION_COOKIE_NAME = "mutah_session"
CSRF_COOKIE_NAME = "mutah_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_COOKIE_MAX_AGE = 30 * 86400  # 30 days (matches DB app_sessions TTL)

# Validation & hardening constants (H4, H16)
CHECK_INTERVAL_MIN = 5
CHECK_INTERVAL_MAX = 1440
REQUEST_TIMEOUT_SECONDS = 15

# Strict CSP compatible with webapp.html (H4)
WEBAPP_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://telegram.org; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
HSTS_VALUE = "max-age=31536000; includeSubDomains"
ALLOWED_CORS_METHODS = "GET, POST, OPTIONS"
ALLOWED_CORS_HEADERS = "Content-Type, Authorization, X-Telegram-Init-Data, X-CSRF-Token"

# References to engines passed from bot.py
_ENGINE = None
_REMINDER_ENGINE = None


def init_webapp_engines(engine=None, reminder_engine=None):
    global _ENGINE, _REMINDER_ENGINE
    _ENGINE = engine
    _REMINDER_ENGINE = reminder_engine


def _validate_telegram_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """Validate Telegram WebApp initData HMAC + auth_date freshness and return telegram_id."""
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None

        auth_date_raw = parsed.get("auth_date", "")
        if not auth_date_raw or not auth_date_raw.isdigit():
            return None
        auth_date = int(auth_date_raw)
        if auth_date <= 0:
            return None

        age = time.time() - auth_date
        if age > INIT_DATA_MAX_AGE or age < -INIT_DATA_MAX_FUTURE_SKEW:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calculated_hash, received_hash):
            user_json = parsed.get("user", "{}")
            user_data = json.loads(user_json)
            if isinstance(user_data, dict):
                return user_data.get("id")
    except Exception as e:
        logger.debug("initData validation failed: %s", e)
    return None


def _json_serial(obj):
    """Serialize datetime and non-standard objects to string/isoformat."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


# ── CORS origin helpers (H4) ────────────────────────────────────────────


def _extract_origin(raw: str) -> Optional[str]:
    """Extract normalized scheme://host[:port] from a URL or Origin header."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s == "*":
        return None
    try:
        parts = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return f"{scheme}://{parts.netloc.lower()}"


def _is_strict_origin_header(raw: str) -> Optional[str]:
    """Validate an Origin header strictly (no path/query/fragment) and normalize it."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s == "*":
        return None
    try:
        parts = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return f"{scheme}://{parts.netloc.lower()}"


def _is_local_origin(origin: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(origin).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def _get_configured_origins() -> set[str]:
    origins: set[str] = set()
    for env_key in ("WEBAPP_URL", "RENDER_EXTERNAL_URL"):
        val = os.getenv(env_key, "").strip()
        if val:
            norm = _extract_origin(val)
            if norm:
                origins.add(norm)
    return origins


def _is_origin_allowed(origin: str) -> bool:
    norm = _is_strict_origin_header(origin)
    if not norm:
        return False
    configured = _get_configured_origins()
    if norm in configured:
        return True
    # Allow explicit localhost dev origins only when WEBAPP_URL is unset or itself local
    if not configured or any(_is_local_origin(o) for o in configured):
        return _is_local_origin(norm)
    return False


# ── Phone-app device pairing (H2 + C9: DB-backed codes + per-IP rate limit) ──
_PAIR_TTL = 600
_PAIR_MAX_FAILS = 8
_PAIR_WINDOW_SECONDS = 300
_pair_lock = threading.Lock()
_pair_ip_fails: dict[str, list[float]] = {}  # client_ip -> list of recent fail timestamps


def reset_pair_rate_limits() -> None:
    """Clear per-IP pairing failure counters (used by tests)."""
    with _pair_lock:
        _pair_ip_fails.clear()


def create_pairing_code(telegram_id: int) -> str:
    """Create a single-use 8-digit pairing code persisted in the DB."""
    code = "".join(secrets.choice("0123456789") for _ in range(8))
    expires_at = int(time.time()) + _PAIR_TTL
    db.cleanup_pair_codes()
    db.save_pair_code(code, int(telegram_id), expires_at)
    return code


def _is_pair_rate_limited(client_ip: str, now: Optional[float] = None) -> tuple[bool, int]:
    ts = time.time() if now is None else now
    ip_key = (client_ip or "unknown")[:64]
    with _pair_lock:
        fails = _pair_ip_fails.get(ip_key)
        if not fails:
            return False, 0
        recent = [t for t in fails if ts - t < _PAIR_WINDOW_SECONDS]
        if recent:
            _pair_ip_fails[ip_key] = recent
        else:
            _pair_ip_fails.pop(ip_key, None)
            return False, 0
        if len(recent) >= _PAIR_MAX_FAILS:
            retry_after = max(1, int(_PAIR_WINDOW_SECONDS - (ts - recent[0])))
            return True, retry_after
        return False, 0


def _record_pair_failure(client_ip: str, now: Optional[float] = None) -> None:
    ts = time.time() if now is None else now
    ip_key = (client_ip or "unknown")[:64]
    with _pair_lock:
        if len(_pair_ip_fails) > 1000:
            stale_ips = [
                k for k, vals in _pair_ip_fails.items()
                if not any(ts - t < _PAIR_WINDOW_SECONDS for t in vals)
            ]
            for k in stale_ips:
                _pair_ip_fails.pop(k, None)
        recent = [t for t in _pair_ip_fails.get(ip_key, []) if ts - t < _PAIR_WINDOW_SECONDS]
        recent.append(ts)
        _pair_ip_fails[ip_key] = recent


def _clear_pair_failures(client_ip: str) -> None:
    ip_key = (client_ip or "unknown")[:64]
    with _pair_lock:
        _pair_ip_fails.pop(ip_key, None)


def _consume_pairing_code(code: str, client_ip: str = "") -> tuple[Optional[int], bool, int]:
    """Validate and consume a DB-backed pairing code with per-IP rate limiting.

    Returns (telegram_id_or_None, rate_limited, retry_after_seconds).
    Never wipes valid pairing codes for other IPs.
    """
    limited, retry_after = _is_pair_rate_limited(client_ip)
    if limited:
        return None, True, retry_after

    normalized = re.sub(
        r"\D", "", str(code or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    )
    if normalized:
        tid = db.consume_pair_code(normalized)
        if tid is not None:
            _clear_pair_failures(client_ip)
            return int(tid), False, 0

    _record_pair_failure(client_ip)
    return None, False, 0


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


def _parse_bool_flag(val: Any, default: int = 1) -> int:
    if val is None:
        return default
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (int, float)):
        return 1 if val != 0 else 0
    s = str(val).strip().lower()
    if s in ("0", "false", "no", "off", ""):
        return 0
    return 1


def _validate_check_interval(raw_interval: Any) -> Optional[int]:
    """Strictly validate integer check_interval within [CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX]."""
    if isinstance(raw_interval, bool) or raw_interval is None:
        return None
    if isinstance(raw_interval, float):
        if not raw_interval.is_integer():
            return None
        val = int(raw_interval)
    elif isinstance(raw_interval, int):
        val = raw_interval
    elif isinstance(raw_interval, str):
        s = raw_interval.strip()
        if not s or not re.fullmatch(r"[+-]?\d+", s):
            return None
        val = int(s)
    else:
        return None
    if val < CHECK_INTERVAL_MIN or val > CHECK_INTERVAL_MAX:
        return None
    return val


class WebAppHTTPServer(ThreadingHTTPServer):
    """Hardened ThreadingHTTPServer with daemon threads and address reuse."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


class WebAppRequestHandler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_SECONDS

    def setup(self):
        super().setup()
        if self.connection is not None and self.timeout is not None:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _client_ip(self) -> str:
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first[:64]
        if self.client_address:
            return str(self.client_address[0])[:64]
        return "unknown"

    def _is_https_transport(self) -> bool:
        xfp = (self.headers.get("X-Forwarded-Proto") or "").strip()
        if xfp:
            first = xfp.split(",")[0].strip().lower()
            if first == "https":
                return True
        return isinstance(self.connection, ssl.SSLSocket)

    def _should_set_secure_cookie(self) -> bool:
        if self._is_https_transport():
            return True
        for env_key in ("WEBAPP_URL", "RENDER_EXTERNAL_URL"):
            if os.getenv(env_key, "").strip().lower().startswith("https://"):
                return True
        return False

    def _format_cookie(self, name: str, value: str, *, max_age: int, http_only: bool) -> str:
        parts = [f"{name}={value}", "Path=/", "SameSite=Strict", f"Max-Age={max_age}"]
        if max_age == 0:
            parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
        if http_only:
            parts.append("HttpOnly")
        if self._should_set_secure_cookie():
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_session_cookies(self) -> list[str]:
        return [
            self._format_cookie(SESSION_COOKIE_NAME, "", max_age=0, http_only=True),
            self._format_cookie(CSRF_COOKIE_NAME, "", max_age=0, http_only=False),
        ]

    def _parse_cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "") or ""
        if not raw:
            return {}
        out: dict[str, str] = {}
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
            for k, morsel in jar.items():
                out[k] = morsel.value
            if out:
                return out
        except Exception:
            pass
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
        return out

    def _send_security_headers(self, *, is_api: bool = False, is_html: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if is_html:
            self.send_header("Content-Security-Policy", WEBAPP_CSP)
        elif is_api:
            self.send_header("Content-Security-Policy", API_CSP)
        if self._is_https_transport():
            self.send_header("Strict-Transport-Security", HSTS_VALUE)
        if is_api:
            self.send_header("Cache-Control", "no-store")

    def _send_cors_headers(self) -> None:
        self.send_header("Vary", "Origin")
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if _is_origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Allow-Methods", ALLOWED_CORS_METHODS)
                self.send_header("Access-Control-Allow-Headers", ALLOWED_CORS_HEADERS)
        else:
            self.send_header("Access-Control-Allow-Methods", ALLOWED_CORS_METHODS)
            self.send_header("Access-Control-Allow-Headers", ALLOWED_CORS_HEADERS)

    def _authenticate(self) -> Optional[int]:
        """Return owner's telegram_id via HttpOnly session cookie or Telegram initData header."""
        self._auth_method = ""
        self._session_token = ""

        cookies = self._parse_cookies()
        session_tok = (cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if session_tok:
            tid = db.verify_app_token(session_tok)
            if tid and int(tid) == int(OWNER_TELEGRAM_ID):
                self._auth_method = "cookie"
                self._session_token = session_tok
                return int(tid)

        # Accept Telegram initData exclusively via header (never query string or JSON body)
        init_data = (self.headers.get(INIT_DATA_HEADER, "") or "").strip()
        if init_data:
            tid = _validate_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN)
            if tid and int(tid) == int(OWNER_TELEGRAM_ID):
                self._auth_method = "telegram"
                return int(tid)

        return None

    def _verify_csrf(self) -> bool:
        """Require matching X-CSRF-Token header for cookie-authenticated state-changing requests."""
        if getattr(self, "_auth_method", "") != "cookie":
            return True
        cookies = self._parse_cookies()
        csrf_cookie = (cookies.get(CSRF_COOKIE_NAME) or "").strip()
        csrf_header = (self.headers.get(CSRF_HEADER_NAME) or "").strip()
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            self._send_json(
                {"success": False, "csrf_error": True, "error": "رمز الحماية CSRF مفقود أو غير صالح"},
                403,
            )
            return False
        return True

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        """Validate Content-Length and parse a JSON object body before authentication."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len.strip() == "":
            content_len = 0
        else:
            try:
                content_len = int(raw_len.strip())
            except ValueError:
                logger.warning("Rejected POST with invalid Content-Length: %r", raw_len)
                self._send_json({"success": False, "error": "Invalid Content-Length"}, 400)
                return None

        if content_len < 0:
            logger.warning("Rejected POST with negative Content-Length: %d", content_len)
            self._send_json({"success": False, "error": "Invalid Content-Length"}, 400)
            return None

        if content_len > MAX_BODY_BYTES:
            logger.warning("Rejected POST exceeding MAX_BODY_BYTES (%d > %d)", content_len, MAX_BODY_BYTES)
            self.close_connection = True
            self._send_json({"success": False, "error": "Payload Too Large"}, 413)
            return None

        if content_len == 0:
            return {}

        try:
            raw_bytes = self.rfile.read(content_len)
        except socket.timeout:
            logger.warning("Timed out reading request body from %s", self._client_ip())
            self.close_connection = True
            self._send_json({"success": False, "error": "Request Timeout"}, 408)
            return None

        try:
            post_body = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            logger.debug("Rejected POST with invalid UTF-8 body: %s", e)
            self._send_json({"success": False, "error": "Invalid UTF-8 request body"}, 400)
            return None

        if not post_body.strip():
            return {}

        try:
            payload = json.loads(post_body)
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("Rejected POST with invalid JSON body: %s", e)
            self._send_json({"success": False, "error": "Invalid JSON body"}, 400)
            return None

        if not isinstance(payload, dict):
            logger.debug("Rejected POST with non-object JSON payload: %s", type(payload).__name__)
            self._send_json({"success": False, "error": "JSON object expected"}, 400)
            return None

        return payload

    def _send_static(self, rel_path: str, ctype: str, no_cache: bool = False):
        full = os.path.join(_STATIC_DIR, rel_path)
        if not os.path.isfile(full):
            self.send_response(404)
            self._send_security_headers()
            self.end_headers()
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache" if no_cache else "public, max-age=86400")
        self._send_security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        data: dict,
        status_code: int = 200,
        *,
        extra_headers: Optional[dict[str, str]] = None,
        set_cookies: Optional[list[str]] = None,
    ):
        body = json.dumps(data, default=_json_serial, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_security_headers(is_api=True)
        self._send_cors_headers()
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        if set_cookies:
            for cookie_str in set_cookies:
                self.send_header("Set-Cookie", cookie_str)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_content: str, status_code: int = 200):
        body = html_content.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._send_security_headers(is_html=True)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not _is_origin_allowed(origin):
            self.send_response(403)
            self._send_security_headers(is_api=True)
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self._send_security_headers(is_api=True)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        if path == "/health":
            try:
                ok = bool(db.health_check())
            except Exception:
                ok = False
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_security_headers(is_api=True)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._send_security_headers()
        self.end_headers()

    def do_PUT(self):
        tid = self._authenticate()
        if not tid:
            self._send_json({"success": False, "auth_required": True, "error": "يلزم ربط هذا الجهاز بالتطبيق"}, 401)
            return
        if not self._verify_csrf():
            return
        self._send_json({"success": False, "error": "Method Not Allowed"}, 405)

    def do_DELETE(self):
        tid = self._authenticate()
        if not tid:
            self._send_json({"success": False, "auth_required": True, "error": "يلزم ربط هذا الجهاز بالتطبيق"}, 401)
            return
        if not self._verify_csrf():
            return
        self._send_json({"success": False, "error": "Method Not Allowed"}, 405)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        # Health check (C9: Render healthCheckPath + UptimeRobot)
        if path == "/health":
            try:
                db_ok = bool(db.health_check())
            except Exception as e:
                logger.warning("Health check failed: %s", e)
                db_ok = False
            if db_ok:
                self._send_json({"status": "ok", "db": "ok"}, 200)
            else:
                self._send_json({"status": "error", "db": "error"}, 503)
            return

        if path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.send_header("Content-Length", "2")
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
            tid = self._authenticate()
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

            # H15: Fetch all seen_content in ONE query (eliminates per-course N+1)
            all_seen_by_course = db.get_all_seen_content(tid) or {}

            # Fetch reminders submission state once
            with db.DBContext() as ctx:
                rem_rows = ctx.fetchall(
                    "SELECT content_id, is_submitted FROM reminders WHERE telegram_id=?",
                    (tid,),
                )

            sub_map: dict[str, bool] = {}

            def _record_submitted(cid_val: Any) -> None:
                cid_s = str(cid_val)
                raw = cid_s.replace("assign_", "").replace("quiz_", "")
                sub_map[cid_s] = True
                sub_map[raw] = True
                sub_map[f"assign_{raw}"] = True
                sub_map[f"quiz_{raw}"] = True

            for r in rem_rows:
                if r.get("is_submitted"):
                    _record_submitted(r["content_id"])
            for rows in all_seen_by_course.values():
                for s in rows:
                    if s.get("content_type") in ("assign", "quiz") and s.get("is_submitted"):
                        _record_submitted(s["content_id"])

            for c in courses:
                cid = c["course_id"]
                items = all_seen_by_course.get(cid)
                if items is None:
                    items = all_seen_by_course.get(str(cid), [])
                assigns = [i for i in items if i.get("content_type") == "assign"]
                quizzes = [i for i in items if i.get("content_type") == "quiz"]
                resources = [i for i in items if i.get("content_type") == "resource"]

                for aq in assigns + quizzes:
                    all_assigns_and_quizzes.append((c["course_name"], aq))

                done_tasks = sum(
                    1
                    for i in assigns + quizzes
                    if bool(i.get("is_submitted")) or sub_map.get(str(i.get("content_id")), False)
                )
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

            # H15: Compute stats in memory from all_seen_by_course (avoid extra queries)
            by_type: dict[str, int] = {}
            total_seen = 0
            for rows in all_seen_by_course.values():
                for it in rows:
                    total_seen += 1
                    ct = it.get("content_type") or "other"
                    by_type[ct] = by_type.get(ct, 0) + 1
            stats = {"total": total_seen, "by_type": by_type}

            # H5: Unify grade URL field to `item_url` and filter attendance via `item_url`
            raw_grades = db.get_user_grades(tid)
            grades = []
            for g in raw_grades:
                g_copy = dict(g)
                item_url = g_copy.get("item_url") or g_copy.get("url") or ""
                g_copy["item_url"] = item_url
                if "/mod/attendance/" in item_url:
                    continue
                if (g_copy.get("item_name") or "").strip().lower() in ("attendance", "الحضور والغياب", "الحضور"):
                    continue
                grades.append(g_copy)

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

            # Parse user_meta from already-fetched user row (avoid duplicate query)
            user_meta: dict[str, Any] = {}
            raw_meta = user.get("user_meta")
            if isinstance(raw_meta, dict):
                user_meta = raw_meta
            elif isinstance(raw_meta, str) and raw_meta.strip():
                try:
                    parsed_meta = json.loads(raw_meta)
                    if isinstance(parsed_meta, dict):
                        user_meta = parsed_meta
                except Exception:
                    user_meta = {}

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
        self._send_security_headers()
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        payload = self._read_json_body()
        if payload is None:
            return

        # Device pairing (public endpoint, protected by DB one-time code + per-IP rate limit)
        if path == "/api/pair":
            client_ip = self._client_ip()
            owner_tid, rate_limited, retry_after = _consume_pairing_code(payload.get("code"), client_ip)
            if rate_limited:
                self._send_json(
                    {
                        "success": False,
                        "rate_limited": True,
                        "error": "تم تجاوز عدد المحاولات المسموحة من هذا الجهاز؛ يرجى المحاولة لاحقاً.",
                    },
                    429,
                    extra_headers={"Retry-After": str(retry_after)},
                )
                return
            if not owner_tid or int(owner_tid) != int(OWNER_TELEGRAM_ID):
                self._send_json(
                    {
                        "success": False,
                        "error": "الرمز غير صحيح أو منتهي الصلاحية. أرسل /app للبوت للحصول على رمز جديد.",
                    },
                    400,
                )
                return
            device = str(payload.get("device") or self.headers.get("User-Agent", ""))[:120]
            token = db.create_app_session(owner_tid, device)
            csrf_token = secrets.token_urlsafe(32)
            cookies = [
                self._format_cookie(
                    SESSION_COOKIE_NAME, token, max_age=SESSION_COOKIE_MAX_AGE, http_only=True
                ),
                self._format_cookie(
                    CSRF_COOKIE_NAME, csrf_token, max_age=SESSION_COOKIE_MAX_AGE, http_only=False
                ),
            ]
            self._send_json({"success": True}, 200, set_cookies=cookies)
            return

        tid = self._authenticate()
        if not tid:
            self._send_json({"success": False, "auth_required": True,
                             "error": "يلزم ربط هذا الجهاز بالتطبيق"}, 401)
            return

        # H16: CSRF check for all cookie-authenticated state-changing POST requests
        if not self._verify_csrf():
            return

        # Unlink only this device (does not touch the LMS account or Telegram alerts)
        if path in ("/api/app-logout", "/api/unlink-device"):
            if getattr(self, "_session_token", ""):
                db.revoke_app_token(self._session_token)
            self._send_json({"success": True}, 200, set_cookies=self._clear_session_cookies())
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
                    db.upsert_grade(
                        tid, cid, g["item_id"], g.get("item_name", ""),
                        g.get("grade_val", ""), g.get("grade_max", ""),
                        g.get("percentage", ""), g.get("feedback", ""),
                        g.get("item_url") or g.get("url", ""),
                    )
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
                        att_url=att.get("att_url") or att.get("url", ""),
                    )
            except Exception as atte:
                logger.warning(f"Error persisting attendance for course {cid}: {atte}")
            if _REMINDER_ENGINE and (assigns or quizzes):
                try:
                    _REMINDER_ENGINE.register_assignments(tid, cid, assigns + quizzes)
                except Exception as rem_err:
                    logger.warning(f"Error registering reminders for course {cid}: {rem_err}")
            for task_item in assigns + quizzes:
                if task_item.get("is_submitted"):
                    db.mark_assignment_submitted(tid, task_item["id"], 1)

        # 0. Direct Login from Mini App (No auto-import of courses — user adds only desired courses)
        if path == "/api/login":
            raw_user = str(payload.get("username") or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
            # Never strip or translate password bytes (C10)
            raw_pass = payload.get("password")
            if not raw_user or not isinstance(raw_pass, str) or raw_pass == "":
                self._send_json({"success": False, "error": "يرجى إدخال الرقم الجامعي وكلمة المرور"}, 400)
                return

            scraper = LMSScraper(raw_user, raw_pass)
            try:
                if not scraper.login():
                    self._send_json({"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}, 401)
                    return

                db.save_user(tid, raw_user, raw_pass, scraper.user_id, scraper.full_name)
                if _ENGINE is not None:
                    try:
                        # H4: Use public thread-safe MonitorEngine API (never poke private attributes)
                        _ENGINE.invalidate_user_session(tid, logout=False)
                    except Exception as eng_err:
                        logger.warning("MonitorEngine session reset failed on login: %s", eng_err)
                self._send_json({"success": True})
            finally:
                scraper.close()
            return

        # 0.b Logout from Session
        if path == "/api/logout":
            if _ENGINE is not None:
                try:
                    # H4: Public thread-safe MonitorEngine API
                    _ENGINE.invalidate_user_session(tid, logout=True)
                except Exception as eng_err:
                    logger.warning("MonitorEngine session invalidation failed on logout: %s", eng_err)
            if getattr(self, "_session_token", ""):
                db.revoke_app_token(self._session_token)
            db.delete_user(tid)
            self._send_json({"success": True}, 200, set_cookies=self._clear_session_cookies())
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
            try:
                cid_int = int(course_id)
            except (TypeError, ValueError):
                self._send_json({"success": False, "error": "course_id غير صالح"}, 400)
                return
            db.toggle_course(tid, cid_int)
            course = db.get_course(tid, cid_int)
            is_mon = bool(course.get("is_monitored", 1)) if course else False
            self._send_json({"success": True, "course_id": cid_int, "is_monitored": is_mon})
            return

        # 3. Mark assignment/quiz as submitted or unsubmitted (H5)
        if path == "/api/mark-submitted":
            item_key = str(payload.get("item_key") or payload.get("content_id") or "").strip()
            if not item_key:
                self._send_json({"success": False, "error": "content_id مطلوب"}, 400)
                return
            is_sub = _parse_bool_flag(payload.get("is_submitted", 1), default=1)
            raw_cid = payload.get("course_id")
            course_id_val: Optional[int] = None
            if raw_cid is not None and str(raw_cid).strip() != "":
                try:
                    course_id_val = int(raw_cid)
                except (TypeError, ValueError):
                    course_id_val = None
            title = str(payload.get("title") or payload.get("content_name") or "")
            url = str(payload.get("url") or payload.get("content_url") or "")
            item_type = str(
                payload.get("item_type")
                or payload.get("content_type")
                or ("quiz" if item_key.startswith("quiz_") else "assign")
            )
            db.mark_item_submitted(
                tid,
                course_id_val,
                item_key,
                title=title,
                url=url,
                item_type=item_type,
                is_submitted=is_sub,
            )
            self._send_json({
                "success": True,
                "content_id": item_key,
                "item_key": item_key,
                "is_submitted": bool(is_sub),
            })
            return

        # 4. Update user settings (H16: strict check_interval bounds 5..1440)
        if path == "/api/update-settings":
            validated_interval: Optional[int] = None
            if "check_interval" in payload:
                validated_interval = _validate_check_interval(payload["check_interval"])
                if validated_interval is None:
                    self._send_json(
                        {
                            "success": False,
                            "error": f"فترة الفحص يجب أن تكون عدداً صحيحاً بين {CHECK_INTERVAL_MIN} و {CHECK_INTERVAL_MAX} دقيقة",
                        },
                        400,
                    )
                    return

            if validated_interval is not None:
                db.update_check_interval(tid, validated_interval)
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

        # 4.b Save personal user metadata atomically via merge_user_meta (H5)
        if path == "/api/user-meta":
            meta_patch = payload.get("meta")
            if not isinstance(meta_patch, dict):
                self._send_json({"success": False, "error": "بيانات غير صالحة"}, 400)
                return
            updated_meta = db.merge_user_meta(tid, meta_patch)
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
                if getattr(scraper, "waf_blocked", False):
                    self._send_json({
                        "success": False,
                        "waf_blocked": True,
                        "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
                    }, 429)
                    return
                if getattr(scraper, "session_expired", False):
                    self._send_json({
                        "success": False,
                        "session_expired": True,
                        "error": "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
                    }, 400)
                    return

                added = 0
                for c in courses_list:
                    db.add_course(tid, c["id"], c["name"], c["url"])
                    added += 1

                # Scan courses sequentially to avoid sharing one LMSScraper across threads (C2)
                for c in courses_list:
                    cid = c["id"]
                    try:
                        result = scraper.fetch_course_content(cid)
                    except Exception as e:
                        logger.warning("Error syncing course %s: %s", cid, e)
                        continue

                    if result.waf_blocked or getattr(scraper, "waf_blocked", False):
                        self._send_json({
                            "success": False,
                            "waf_blocked": True,
                            "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
                        }, 429)
                        return

                    if result.session_expired or getattr(scraper, "session_expired", False):
                        self._send_json({
                            "success": False,
                            "session_expired": True,
                            "error": result.error or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
                        }, 400)
                        return

                    if not result.ok:
                        logger.warning("Sync course %s returned ok=False: %s", cid, result.error)
                        continue

                    if result.content:
                        _persist_course_content(cid, result.content)

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
            try:
                cid_int = int(course_id)
            except (TypeError, ValueError):
                self._send_json({"success": False, "error": "course_id غير صالح"}, 400)
                return
            db.remove_course(tid, cid_int)
            self._send_json({"success": True, "course_id": cid_int})
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

        # 7. Real Force Check across monitored courses (H15: single get_all_seen_content query)
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

                # H15: Load all seen content once in memory instead of calling db.is_seen per item
                all_seen_by_course = db.get_all_seen_content(tid) or {}
                seen_keys: set[tuple[str, str]] = {
                    (str(it.get("content_id", "")), str(it.get("content_type", "")))
                    for rows in all_seen_by_course.values()
                    for it in rows
                }

                new_items = 0
                # Scan courses sequentially to avoid sharing one LMSScraper across threads (C2)
                for c in courses:
                    cid = c["course_id"]
                    try:
                        result = scraper.fetch_course_content(cid)
                    except Exception as e:
                        logger.warning("Error fetching course %s in force-check: %s", cid, e)
                        continue

                    if result.waf_blocked or getattr(scraper, "waf_blocked", False):
                        self._send_json({
                            "success": False,
                            "waf_blocked": True,
                            "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
                        }, 429)
                        return

                    if result.session_expired or getattr(scraper, "session_expired", False):
                        self._send_json({
                            "success": False,
                            "session_expired": True,
                            "error": result.error or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
                        }, 400)
                        return

                    if not result.ok:
                        logger.warning("Force-check course %s returned ok=False: %s", cid, result.error)
                        continue

                    content = result.content
                    if not content:
                        continue
                    for list_key, ctype in (("assignments", "assign"), ("quizzes", "quiz"), ("resources", "resource")):
                        for item in content.get(list_key, []):
                            key = (str(item["id"]), ctype)
                            if key not in seen_keys:
                                seen_keys.add(key)
                                new_items += 1
                    _persist_course_content(cid, content)

                db.update_last_check(tid)
                detail_msg = (
                    f"تم فحص {len(courses)} مقررات — رُصد {new_items} عنصر جديد"
                    if new_items > 0
                    else f"تم فحص {len(courses)} مقررات — كافة البيانات محدّثة"
                )
                db.append_sync_log(tid, "اكتمل الفحص الفوري", detail_msg)
                self._send_json({"success": True, "new_items": new_items})
            finally:
                scraper.close()
            return

        self.send_response(404)
        self._send_security_headers(is_api=True)
        self.end_headers()

    def log_message(self, format, *args):
        # Quiet standard HTTP logs to avoid cluttering bot.log
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = WebAppHTTPServer(("0.0.0.0", port), WebAppRequestHandler)
    logger.info(f"WebApp server running on port {port}")
    server.serve_forever()
