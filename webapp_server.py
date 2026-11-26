"""FastAPI + Uvicorn web server for Mutah LMS Monitor Mini App and PWA."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

import database as db
from config import OWNER_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from lms_scraper import LMSScraper
from services.sync_service import seed_course_snapshot

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

# Validation & hardening constants (H4, H16, F2, M-VALID)
CHECK_INTERVAL_MIN = 5
CHECK_INTERVAL_MAX = 1440
COURSE_ID_MIN = 1
COURSE_ID_MAX = 999_999_999
REQUEST_TIMEOUT_SECONDS = 15
_ARABIC_DIGITS_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# General per-IP API rate limit (F2)
API_RATE_LIMIT_MAX = 120
API_RATE_LIMIT_WINDOW = 60

# Strict CSP for extracted /static/* JS & CSS (H4, H9, F2)
# Inline <script> blocks are eliminated; style-src keeps 'unsafe-inline' for dynamic progress bar widths.
WEBAPP_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
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

# Managed Uvicorn server state for shared asyncio event loop
_UVICORN_SERVER: uvicorn.Server | None = None
_UVICORN_TASK: asyncio.Task[Any] | None = None

_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = str(_BASE_DIR)
_STATIC_ASSETS_DIR = (_BASE_DIR / "static").resolve()
_ICONS_DIR = (_BASE_DIR / "icons").resolve()

_ROOT_STATIC_FILES: dict[str, tuple[str, str, bool]] = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8", True),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8", True),
    "/logo.png": ("logo.png", "image/png", False),
    "/logo.jpg": ("logo.jpg", "image/jpeg", False),
}

_STATIC_MIME_TYPES: dict[str, str] = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}


def init_webapp_engines(engine=None, reminder_engine=None) -> None:
    """Attach MonitorEngine and ReminderEngine references from bot.py."""
    global _ENGINE, _REMINDER_ENGINE
    _ENGINE = engine
    _REMINDER_ENGINE = reminder_engine


def set_monitor_engine(engine) -> None:
    """Attach or update MonitorEngine reference."""
    global _ENGINE
    _ENGINE = engine


def set_reminder_engine(reminder_engine) -> None:
    """Attach or update ReminderEngine reference."""
    global _REMINDER_ENGINE
    _REMINDER_ENGINE = reminder_engine


def _validate_telegram_init_data(init_data: str, bot_token: str) -> int | None:
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
    except Exception as exc:
        logger.debug("initData validation failed: %s", exc)
    return None


def _json_serial(obj: Any) -> str:
    """Serialize datetime and non-standard objects to string/isoformat."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _normalize_utc_iso(value: Any) -> str:
    """Normalize legacy naive SQLite/PG timestamps or datetime objects to explicit UTC ISO-8601 (M-UTC)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_response(
    data: dict[str, Any],
    status_code: int = 200,
    *,
    headers: dict[str, str] | None = None,
    set_cookies: list[str] | None = None,
) -> Response:
    body = json.dumps(data, default=_json_serial, ensure_ascii=False).encode("utf-8")
    resp = Response(content=body, status_code=status_code, media_type="application/json; charset=utf-8")
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    if set_cookies:
        for cookie_str in set_cookies:
            resp.headers.append("Set-Cookie", cookie_str)
    return resp


# ── CORS origin helpers (H4) ────────────────────────────────────────────


def _extract_origin(raw: str) -> str | None:
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


def _is_strict_origin_header(raw: str) -> str | None:
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


# ── Rate limiting: /api/pair (H2+C9) and general /api/* (F2) ────────────

_PAIR_TTL = 600
_PAIR_MAX_FAILS = 8
_PAIR_WINDOW_SECONDS = 300
_pair_lock = threading.Lock()
_pair_ip_fails: dict[str, list[float]] = {}

_api_rate_lock = threading.Lock()
_api_ip_hits: dict[str, list[float]] = {}


def reset_pair_rate_limits() -> None:
    """Clear per-IP pairing failure counters (used by tests)."""
    with _pair_lock:
        _pair_ip_fails.clear()


def reset_rate_limits() -> None:
    """Clear both general per-IP API rate limits and pairing failure counters (used by tests)."""
    reset_pair_rate_limits()
    with _api_rate_lock:
        _api_ip_hits.clear()


def _check_general_api_rate_limit(client_ip: str, now: float | None = None) -> tuple[bool, int]:
    """Sliding-window per-IP rate limiter for /api/* routes."""
    limit = API_RATE_LIMIT_MAX
    window = API_RATE_LIMIT_WINDOW
    if limit <= 0:
        return False, 0
    ts = time.time() if now is None else now
    ip_key = (client_ip or "unknown")[:64]
    with _api_rate_lock:
        if len(_api_ip_hits) > 2000:
            stale = [k for k, vals in _api_ip_hits.items() if not any(ts - t < window for t in vals)]
            for k in stale:
                _api_ip_hits.pop(k, None)
        recent = [t for t in _api_ip_hits.get(ip_key, []) if ts - t < window]
        if len(recent) >= limit:
            _api_ip_hits[ip_key] = recent
            retry_after = max(1, int(window - (ts - recent[0])))
            return True, retry_after
        recent.append(ts)
        _api_ip_hits[ip_key] = recent
        return False, 0


def create_pairing_code(telegram_id: int) -> str:
    """Create a single-use 8-digit pairing code persisted in the DB."""
    code = "".join(secrets.choice("0123456789") for _ in range(8))
    expires_at = int(time.time()) + _PAIR_TTL
    db.cleanup_pair_codes()
    db.save_pair_code(code, int(telegram_id), expires_at)
    return code


def _is_pair_rate_limited(client_ip: str, now: float | None = None) -> tuple[bool, int]:
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


def _record_pair_failure(client_ip: str, now: float | None = None) -> None:
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


def _consume_pairing_code(code: Any, client_ip: str = "") -> tuple[int | None, bool, int]:
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


# ── Validation helpers & Pydantic v2 models (C10, H16, F2) ──────────────


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


def _validate_check_interval(raw_interval: Any) -> int | None:
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


class _BaseApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PairRequest(_BaseApiModel):
    code: str | int | None = None
    device: str | None = None


class LoginRequest(_BaseApiModel):
    username: str | int
    # C10: Never strip or translate password; StrictStr prevents coercion of non-strings
    password: StrictStr = Field(min_length=1)

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, v: Any) -> str:
        if isinstance(v, bool) or v is None or not isinstance(v, (str, int)):
            raise ValueError("يرجى إدخال الرقم الجامعي وكلمة المرور")
        norm = str(v).strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        if not norm:
            raise ValueError("يرجى إدخال الرقم الجامعي وكلمة المرور")
        return norm


def _validate_course_id(raw: Any, *, allow_lms_url: bool = False) -> int:
    """Strictly validate positive numeric Moodle course ID in [1, 999999999] (M-VALID)."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError("course_id غير صالح")
    if isinstance(raw, int):
        if COURSE_ID_MIN <= raw <= COURSE_ID_MAX:
            return raw
        raise ValueError("course_id غير صالح")
    if not isinstance(raw, str):
        raise ValueError("course_id غير صالح")

    s = raw.strip().translate(_ARABIC_DIGITS_TRANS)
    if not s:
        raise ValueError("course_id مطلوب")

    if allow_lms_url and (s.startswith("http://") or s.startswith("https://")):
        try:
            parts = urllib.parse.urlsplit(s)
        except ValueError as exc:
            raise ValueError("يرجى إدخال رقم مادة صحيح أو رابط المقرر") from exc
        if (
            parts.scheme.lower() in ("http", "https")
            and parts.netloc
            and ".." not in parts.path
            and not any(ch in s for ch in (" ", "\t", "\r", "\n", ";"))
        ):
            qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True, strict_parsing=False)
            candidates = qs.get("id") or qs.get("course") or []
            if len(candidates) == 1 and re.fullmatch(r"[0-9]+", candidates[0]):
                val = int(candidates[0])
                if COURSE_ID_MIN <= val <= COURSE_ID_MAX:
                    return val
        raise ValueError("يرجى إدخال رقم مادة صحيح أو رابط المقرر")

    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError("course_id غير صالح")
    val = int(s)
    if val < COURSE_ID_MIN or val > COURSE_ID_MAX:
        raise ValueError("course_id غير صالح")
    return val


class AddCourseRequest(_BaseApiModel):
    course_id: int

    @field_validator("course_id", mode="before")
    @classmethod
    def _validate_course_input(cls, v: Any) -> int:
        return _validate_course_id(v, allow_lms_url=True)


class ToggleCourseRequest(_BaseApiModel):
    course_id: int

    @field_validator("course_id", mode="before")
    @classmethod
    def _validate_course_input(cls, v: Any) -> int:
        return _validate_course_id(v)


class DeleteCourseRequest(_BaseApiModel):
    clear_all: bool = False
    course_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_delete_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if out.get("clear_all"):
            out["clear_all"] = True
            out["course_id"] = None
            return out
        out["course_id"] = _validate_course_id(out.get("course_id"))
        return out


class UpdateSettingsRequest(_BaseApiModel):
    check_interval: int | None = Field(default=None, ge=CHECK_INTERVAL_MIN, le=CHECK_INTERVAL_MAX)
    is_monitoring: bool | None = None
    notify_assign: bool | None = None
    notify_quiz: bool | None = None
    notify_resource: bool | None = None
    notify_grade: bool | None = None
    notify_announcement: bool | None = None
    notify_attendance: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_strict_interval(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "check_interval" in out:
            validated = _validate_check_interval(out["check_interval"])
            if validated is None:
                raise ValueError(
                    f"فترة الفحص يجب أن تكون عدداً صحيحاً بين {CHECK_INTERVAL_MIN} و {CHECK_INTERVAL_MAX} دقيقة"
                )
            out["check_interval"] = validated
        return out


class UserMetaRequest(_BaseApiModel):
    meta: dict[str, Any]


class MarkSubmittedRequest(_BaseApiModel):
    item_key: str | None = None
    content_id: str | None = None
    course_id: int | None = None
    title: str | None = None
    content_name: str | None = None
    url: str | None = None
    content_url: str | None = None
    item_type: str | None = None
    content_type: str | None = None
    is_submitted: bool | int | str = 1

    @field_validator("course_id", mode="before")
    @classmethod
    def _validate_optional_course_id(cls, v: Any) -> int | None:
        if v is None:
            return None
        return _validate_course_id(v)

    @model_validator(mode="after")
    def _require_item_key(self) -> MarkSubmittedRequest:
        key = str(self.item_key or self.content_id or "").strip()
        if not key:
            raise ValueError("content_id مطلوب")
        return self


class EmptyActionRequest(_BaseApiModel):
    tid: int | str | None = None


# ── Request/Cookie/Auth helpers ─────────────────────────────────────────


def _scope_headers_dict(scope: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers") or ():
        k = raw_k.decode("latin-1").lower()
        v = raw_v.decode("latin-1")
        if k in out:
            out[k] = f"{out[k]}, {v}"
        else:
            out[k] = v
    return out


def _client_ip_from_scope(scope: dict[str, Any], headers: dict[str, str]) -> str:
    xff = (headers.get("x-forwarded-for") or "").strip()
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    client = scope.get("client")
    if client and len(client) >= 1 and client[0]:
        return str(client[0])[:64]
    return "unknown"


def _client_ip_from_request(request: Request) -> str:
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return str(request.client.host)[:64]
    return "unknown"


def _is_https_from_scope(scope: dict[str, Any], headers: dict[str, str]) -> bool:
    xfp = (headers.get("x-forwarded-proto") or "").strip()
    if xfp:
        first = xfp.split(",")[0].strip().lower()
        if first == "https":
            return True
    return str(scope.get("scheme") or "").lower() == "https"


def _is_https_request(request: Request) -> bool:
    xfp = (request.headers.get("X-Forwarded-Proto") or "").strip()
    if xfp:
        first = xfp.split(",")[0].strip().lower()
        if first == "https":
            return True
    return request.url.scheme.lower() == "https"


def _should_set_secure_cookie(request: Request) -> bool:
    if _is_https_request(request):
        return True
    for env_key in ("WEBAPP_URL", "RENDER_EXTERNAL_URL"):
        if os.getenv(env_key, "").strip().lower().startswith("https://"):
            return True
    return False


def _format_cookie(name: str, value: str, *, max_age: int, http_only: bool, secure: bool) -> str:
    parts = [f"{name}={value}", "Path=/", "SameSite=Strict", f"Max-Age={max_age}"]
    if max_age == 0:
        parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _clear_session_cookies(request: Request) -> list[str]:
    secure = _should_set_secure_cookie(request)
    return [
        _format_cookie(SESSION_COOKIE_NAME, "", max_age=0, http_only=True, secure=secure),
        _format_cookie(CSRF_COOKIE_NAME, "", max_age=0, http_only=False, secure=secure),
    ]


def _parse_cookies(raw: str) -> dict[str, str]:
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


class AuthContext:
    """Authenticated request identity and mechanism."""

    __slots__ = ("method", "session_token", "tid")

    def __init__(self, tid: int, method: str, session_token: str = "") -> None:
        self.tid = tid
        self.method = method
        self.session_token = session_token


class _ApiError(Exception):
    """Structured API error returning consistent JSON payload."""

    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = headers


async def _authenticate_request(request: Request) -> AuthContext | None:
    """Authenticate owner via HttpOnly session cookie or Telegram initData header."""
    cookies = _parse_cookies(request.headers.get("Cookie") or "")
    session_tok = (cookies.get(SESSION_COOKIE_NAME) or "").strip()
    if session_tok:
        tid = await asyncio.to_thread(db.verify_app_token, session_tok)
        if tid and int(tid) == int(OWNER_TELEGRAM_ID):
            return AuthContext(int(tid), "cookie", session_tok)

    # H1: Accept Telegram initData exclusively via header (never query string or JSON body)
    init_data = (request.headers.get(INIT_DATA_HEADER) or "").strip()
    if init_data:
        tid = _validate_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN)
        if tid and int(tid) == int(OWNER_TELEGRAM_ID):
            return AuthContext(int(tid), "telegram", "")

    return None


async def _require_auth(request: Request) -> AuthContext:
    auth = await _authenticate_request(request)
    if auth is None:
        raise _ApiError(
            401,
            {"success": False, "auth_required": True, "error": "يلزم ربط هذا الجهاز بالتطبيق"},
        )
    return auth


async def _require_auth_and_csrf(request: Request) -> AuthContext:
    auth = await _require_auth(request)
    if auth.method == "cookie":
        cookies = _parse_cookies(request.headers.get("Cookie") or "")
        csrf_cookie = (cookies.get(CSRF_COOKIE_NAME) or "").strip()
        csrf_header = (request.headers.get(CSRF_HEADER_NAME) or "").strip()
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise _ApiError(
                403,
                {"success": False, "csrf_error": True, "error": "رمز الحماية CSRF مفقود أو غير صالح"},
            )
    return auth


AuthDep = Annotated[AuthContext, Depends(_require_auth)]
CsrfAuthDep = Annotated[AuthContext, Depends(_require_auth_and_csrf)]


# ── Pure ASGI Middleware Stack ──────────────────────────────────────────


def _build_raw_headers(
    headers_list: list[tuple[bytes, bytes]],
    updates: list[tuple[bytes, bytes]],
    *,
    remove_keys: set[bytes] | None = None,
) -> list[tuple[bytes, bytes]]:
    remove_set = remove_keys or set()
    update_keys = {k.lower() for k, _ in updates}
    filtered = [
        (k, v) for k, v in headers_list
        if k.lower() not in remove_set and k.lower() not in update_keys
    ]
    filtered.extend(updates)
    return filtered


class AccessErrorLoggingMiddleware:
    """Outermost ASGI middleware: structured 4xx (WARNING) & 5xx (ERROR) logging without secrets (M-VALID)."""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").upper().replace("\r", "").replace("\n", "")
        # ASGI path excludes query_string so ?code= or query tokens never enter logs
        raw_path = str(scope.get("path") or "/")
        safe_path = raw_path.split("?", 1)[0].replace("\r", "").replace("\n", "")
        headers = _scope_headers_dict(scope)
        client_ip = _client_ip_from_scope(scope, headers).replace("\r", "").replace("\n", "")
        logged_response = False

        async def send_wrapper(message) -> None:
            nonlocal logged_response
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 0))
                if status_code >= 500:
                    logged_response = True
                    logger.error(
                        "HTTP %s %s -> %d (ip=%s)",
                        method,
                        safe_path,
                        status_code,
                        client_ip,
                    )
                elif status_code >= 400:
                    logged_response = True
                    logger.warning(
                        "HTTP %s %s -> %d (ip=%s)",
                        method,
                        safe_path,
                        status_code,
                        client_ip,
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not logged_response:
                logger.error(
                    "HTTP %s %s -> 500 (ip=%s)",
                    method,
                    safe_path,
                    client_ip,
                )
            raise


class SecurityAndCorsMiddleware:
    """Outermost ASGI middleware: strict CORS allowlist + security headers on every response."""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = (scope.get("path") or "").rstrip("/") or "/"
        method = (scope.get("method") or "GET").upper()
        req_headers = _scope_headers_dict(scope)
        is_https = _is_https_from_scope(scope, req_headers)
        origin = (req_headers.get("origin") or "").strip()

        is_api = path == "/health" or path.startswith("/api/") or path == "/api"
        is_webapp_html = path in ("/webapp", "/app")
        is_docs = path in ("/docs", "/redoc", "/openapi.json")

        # Handle CORS preflight OPTIONS directly
        if method == "OPTIONS":
            resp_headers: list[tuple[bytes, bytes]] = [
                (b"x-content-type-options", b"nosniff"),
                (b"referrer-policy", b"no-referrer"),
                (b"content-security-policy", API_CSP.encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"vary", b"Origin"),
                (b"content-length", b"0"),
            ]
            if is_https:
                resp_headers.append((b"strict-transport-security", HSTS_VALUE.encode("ascii")))

            if origin and not _is_origin_allowed(origin):
                await send({"type": "http.response.start", "status": 403, "headers": resp_headers})
                await send({"type": "http.response.body", "body": b""})
                return

            if origin and _is_origin_allowed(origin):
                resp_headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                resp_headers.append((b"access-control-allow-credentials", b"true"))
            resp_headers.append((b"access-control-allow-methods", ALLOWED_CORS_METHODS.encode("ascii")))
            resp_headers.append((b"access-control-allow-headers", ALLOWED_CORS_HEADERS.encode("ascii")))
            await send({"type": "http.response.start", "status": 200, "headers": resp_headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers") or ())
                extra: list[tuple[bytes, bytes]] = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                if is_https:
                    extra.append((b"strict-transport-security", HSTS_VALUE.encode("ascii")))
                if is_webapp_html:
                    extra.append((b"content-security-policy", WEBAPP_CSP.encode("ascii")))
                elif is_api and not is_docs:
                    extra.append((b"content-security-policy", API_CSP.encode("ascii")))
                    extra.append((b"cache-control", b"no-store"))

                if is_api:
                    extra.append((b"vary", b"Origin"))
                    if origin:
                        if _is_origin_allowed(origin):
                            extra.append((b"access-control-allow-origin", origin.encode("latin-1")))
                            extra.append((b"access-control-allow-credentials", b"true"))
                            extra.append((b"access-control-allow-methods", ALLOWED_CORS_METHODS.encode("ascii")))
                            extra.append((b"access-control-allow-headers", ALLOWED_CORS_HEADERS.encode("ascii")))
                    else:
                        extra.append((b"access-control-allow-methods", ALLOWED_CORS_METHODS.encode("ascii")))
                        extra.append((b"access-control-allow-headers", ALLOWED_CORS_HEADERS.encode("ascii")))

                message = {
                    **message,
                    "headers": _build_raw_headers(raw_headers, extra),
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyGuardMiddleware:
    """Pre-auth Content-Length + streaming body size cap (C5) and JSON object pre-check."""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def _send_json_err(self, send, status: int, error: str, *, close: bool = False) -> None:
        body = json.dumps({"success": False, "error": error}, ensure_ascii=False).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if close:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return

        headers = _scope_headers_dict(scope)
        raw_len = headers.get("content-length")
        if raw_len is not None and raw_len.strip() != "":
            try:
                content_len = int(raw_len.strip())
            except ValueError:
                logger.warning("Rejected %s with invalid Content-Length: %r", method, raw_len)
                await self._send_json_err(send, 400, "Invalid Content-Length")
                return

            if content_len < 0:
                logger.warning("Rejected %s with negative Content-Length: %d", method, content_len)
                await self._send_json_err(send, 400, "Invalid Content-Length")
                return

            if content_len > MAX_BODY_BYTES:
                logger.warning("Rejected %s exceeding MAX_BODY_BYTES (%d > %d)", method, content_len, MAX_BODY_BYTES)
                await self._send_json_err(send, 413, "Payload Too Large", close=True)
                return

        # Stream-read body chunk by chunk to enforce MAX_BODY_BYTES even on chunked transfers
        chunks: list[bytes] = []
        total_bytes = 0
        more_body = True
        while more_body:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk:
                total_bytes += len(chunk)
                if total_bytes > MAX_BODY_BYTES:
                    logger.warning(
                        "Rejected streamed %s exceeding MAX_BODY_BYTES (> %d)",
                        method,
                        MAX_BODY_BYTES,
                    )
                    await self._send_json_err(send, 413, "Payload Too Large", close=True)
                    return
                chunks.append(chunk)
            more_body = bool(message.get("more_body", False))

        raw_body = b"".join(chunks)
        path = (scope.get("path") or "").rstrip("/") or "/"

        # Pre-auth UTF-8 & JSON object validation for POST /api/* (C5)
        if method == "POST" and path.startswith("/api/"):
            if raw_body:
                try:
                    decoded = raw_body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    logger.debug("Rejected POST with invalid UTF-8 body: %s", exc)
                    await self._send_json_err(send, 400, "Invalid UTF-8 request body")
                    return

                if decoded.strip():
                    try:
                        parsed = json.loads(decoded)
                    except (json.JSONDecodeError, ValueError) as exc:
                        logger.debug("Rejected POST with invalid JSON body: %s", exc)
                        await self._send_json_err(send, 400, "Invalid JSON body")
                        return
                    if not isinstance(parsed, dict):
                        logger.debug("Rejected POST with non-object JSON payload: %s", type(parsed).__name__)
                        await self._send_json_err(send, 400, "JSON object expected")
                        return
                else:
                    raw_body = b"{}"
            else:
                raw_body = b"{}"

            # Ensure Content-Type is JSON when body defaulted to {}
            if "content-type" not in headers:
                new_headers = list(scope.get("headers") or ())
                new_headers.append((b"content-type", b"application/json"))
                scope = {**scope, "headers": new_headers}

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


class RateLimitMiddleware:
    """General per-IP rate limiting middleware for /api/* routes (F2)."""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = (scope.get("path") or "").rstrip("/") or "/"
        method = (scope.get("method") or "GET").upper()
        if method != "OPTIONS" and path.startswith("/api/"):
            headers = _scope_headers_dict(scope)
            client_ip = _client_ip_from_scope(scope, headers)
            limited, retry_after = _check_general_api_rate_limit(client_ip)
            if limited:
                body = json.dumps(
                    {
                        "success": False,
                        "rate_limited": True,
                        "error": "تم تجاوز حد الطلبات المسموح؛ يرجى الانتظار قليلاً.",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                resp_headers = [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ]
                await send({"type": "http.response.start", "status": 429, "headers": resp_headers})
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)


# ── Business Logic Helpers (executed via asyncio.to_thread) ─────────────


def _persist_course_content(
    tid: int,
    cid: int,
    content: dict[str, Any],
    *,
    course_name: str = "",
    mark_seeded: bool = True,
) -> dict[str, Any]:
    """Persist scraped course snapshot via canonical services.sync_service.seed_course_snapshot."""
    return seed_course_snapshot(
        db,
        tid,
        cid,
        content,
        course_name=course_name,
        mark_seeded=mark_seeded,
        reminder_engine=_REMINDER_ENGINE,
    )


def _build_user_data_payload(tid: int) -> dict[str, Any]:
    """Assemble full /api/user-data payload without N+1 queries (H5, H15)."""
    user = db.get_user(tid)
    if not user:
        return {
            "success": False,
            "not_registered": True,
            "tid": tid,
            "error": "لم يتم العثور على حساب مرتبط؛ سجل دخولك أدناه",
        }

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
        "last_check": _normalize_utc_iso(user.get("last_check")) or "لم يتم بعد",
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

    # H15: Compute stats in memory from all_seen_by_course
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

    return {
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
        },
    }


def _do_lms_login(tid: int, username: str, password: str) -> tuple[int, dict[str, Any]]:
    scraper = LMSScraper(username, password)
    try:
        if not scraper.login():
            return 401, {"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}
        db.save_user(tid, username, password, scraper.user_id, scraper.full_name)
        if _ENGINE is not None:
            try:
                _ENGINE.invalidate_user_session(tid, logout=False)
            except Exception as eng_err:
                logger.warning("MonitorEngine session reset failed on login: %s", eng_err)
        return 200, {"success": True}
    finally:
        scraper.close()


def _do_logout(tid: int, session_token: str) -> None:
    if _ENGINE is not None:
        try:
            _ENGINE.invalidate_user_session(tid, logout=True)
        except Exception as eng_err:
            logger.warning("MonitorEngine session invalidation failed on logout: %s", eng_err)
    if session_token:
        db.revoke_app_token(session_token)
    db.delete_user(tid)


def _do_sync_courses(tid: int) -> tuple[int, dict[str, Any]]:
    user = db.get_user(tid)
    if not user:
        return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    try:
        if not scraper.login():
            return 400, {"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}
        courses_list = scraper.get_courses()
        if getattr(scraper, "waf_blocked", False):
            return 429, {
                "success": False,
                "waf_blocked": True,
                "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
            }
        if getattr(scraper, "session_expired", False):
            return 400, {
                "success": False,
                "session_expired": True,
                "error": "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
            }

        added = 0
        for c in courses_list:
            db.add_course(tid, c["id"], c["name"], c["url"])
            added += 1

        # C2: Scan courses sequentially to avoid sharing one LMSScraper across threads
        for c in courses_list:
            cid = c["id"]
            try:
                result = scraper.fetch_course_content(cid)
            except Exception as exc:
                logger.warning("Error syncing course %s: %s", cid, exc)
                continue

            if result.waf_blocked or getattr(scraper, "waf_blocked", False):
                return 429, {
                    "success": False,
                    "waf_blocked": True,
                    "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
                }
            if result.session_expired or getattr(scraper, "session_expired", False):
                return 400, {
                    "success": False,
                    "session_expired": True,
                    "error": result.error
                    or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
                }
            if not result.ok:
                logger.warning("Sync course %s returned ok=False: %s", cid, result.error)
                continue
            if result.content:
                _persist_course_content(tid, cid, result.content, course_name=c.get("name", ""))

        db.update_last_check(tid)
        db.append_sync_log(tid, "استيراد المقررات", f"تم جلب ومزامنة {added} مقررات من LMS")
        return 200, {"success": True, "synced_count": added}
    finally:
        scraper.close()


def _do_add_course(tid: int, cid: int) -> tuple[int, dict[str, Any]]:
    user = db.get_user(tid)
    if not user:
        return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    try:
        if not scraper.login():
            return 400, {"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}
        cname, content = scraper.get_course_info(cid)
        if not cname:
            return 404, {"success": False, "error": f"لم يتم العثور على مادة بالرقم {cid}"}
        curl = f"https://mulms.mutah.edu.jo/course/view.php?id={cid}"
        db.add_course(tid, cid, cname, curl)
        _persist_course_content(tid, cid, content, course_name=cname)
        db.update_last_check(tid)
        db.append_sync_log(tid, "إضافة مقرر", f"تم ربط ومزامنة مقرر: {cname}")
        return 200, {"success": True, "course_name": cname, "course_id": cid}
    finally:
        scraper.close()


def _do_force_check(tid: int) -> tuple[int, dict[str, Any]]:
    user = db.get_user(tid)
    if not user:
        return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}

    courses = db.get_user_courses(tid, monitored_only=True)
    if not courses:
        return 400, {"success": False, "error": "لا توجد مقررات مفعلة للمراقبة"}

    scraper = LMSScraper(user["lms_username"], user["lms_password"])
    try:
        if not scraper.login():
            return 400, {"success": False, "error": scraper.error_message or "فشل تسجيل الدخول إلى LMS"}

        # H15: Load all seen content once in memory instead of calling db.is_seen per item
        all_seen_by_course = db.get_all_seen_content(tid) or {}
        seen_keys: set[tuple[str, str]] = {
            (str(it.get("content_id", "")), str(it.get("content_type", "")))
            for rows in all_seen_by_course.values()
            for it in rows
        }

        new_items = 0
        for c in courses:
            cid = c["course_id"]
            try:
                result = scraper.fetch_course_content(cid)
            except Exception as exc:
                logger.warning("Error fetching course %s in force-check: %s", cid, exc)
                continue

            if result.waf_blocked or getattr(scraper, "waf_blocked", False):
                return 429, {
                    "success": False,
                    "waf_blocked": True,
                    "error": "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول مجدداً بعد عدة دقائق",
                }
            if result.session_expired or getattr(scraper, "session_expired", False):
                return 400, {
                    "success": False,
                    "session_expired": True,
                    "error": result.error
                    or "انتهت جلسة نظام التعليم الإلكتروني (LMS)؛ يرجى إعادة المحاولة أو تسجيل الدخول مجدداً",
                }
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
            _persist_course_content(tid, cid, content, course_name=c.get("course_name", ""))

        db.update_last_check(tid)
        detail_msg = (
            f"تم فحص {len(courses)} مقررات — رُصد {new_items} عنصر جديد"
            if new_items > 0
            else f"تم فحص {len(courses)} مقررات — كافة البيانات محدّثة"
        )
        db.append_sync_log(tid, "اكتمل الفحص الفوري", detail_msg)
        return 200, {"success": True, "new_items": new_items}
    finally:
        scraper.close()


# ── Static file helpers with strict path-traversal defense ──────────────


def _serve_safe_file(root: Path, rel_path: str, *, default_ctype: str | None = None, no_cache: bool = False):
    if not rel_path or "\x00" in rel_path:
        return Response(status_code=404)
    # Reject backslashes and explicit traversal segments before resolving
    norm_rel = rel_path.replace("\\", "/")
    parts = [p for p in norm_rel.split("/") if p != ""]
    if not parts or any(p in (".", "..") for p in parts):
        return Response(status_code=404)

    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / Path(*parts)).resolve()
    except OSError:
        return Response(status_code=404)

    if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
        return Response(status_code=404)

    ctype = default_ctype or _STATIC_MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    body = candidate.read_bytes()
    cache_val = "no-cache" if no_cache else "public, max-age=86400"
    return Response(
        content=body,
        status_code=200,
        media_type=ctype,
        headers={"Cache-Control": cache_val},
    )


# ── FastAPI Application Factory ─────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application with middleware, docs, and routes."""
    application = FastAPI(
        title="Mutah LMS Monitor API",
        version="2.0.0",
        description="FastAPI backend for Mutah University LMS Monitor Mini App and PWA.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Exception handlers returning consistent JSON envelopes
    @application.exception_handler(_ApiError)
    async def _handle_api_error(_request: Request, exc: _ApiError) -> Response:
        return _json_response(exc.payload, status_code=exc.status_code, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> Response:
        errors = exc.errors()
        first_msg = "بيانات الطلب غير صالحة"
        if errors:
            raw_msg = str(errors[0].get("msg") or "")
            if raw_msg.startswith("Value error, "):
                first_msg = raw_msg[len("Value error, "):]
            elif raw_msg:
                first_msg = raw_msg
        return _json_response(
            {"success": False, "error": first_msg, "detail": errors},
            status_code=400,
        )

    # Middleware registration (add_middleware wraps LIFO: last added is outermost)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(BodyGuardMiddleware)
    application.add_middleware(SecurityAndCorsMiddleware)
    application.add_middleware(AccessErrorLoggingMiddleware)

    # ── Health & Root Routes ──

    async def _health_check_impl(is_head: bool) -> Response:
        try:
            db_ok = bool(await asyncio.to_thread(db.health_check))
        except Exception as exc:
            logger.warning("Health check failed: %s", exc)
            db_ok = False
        status = 200 if db_ok else 503
        if is_head:
            return Response(status_code=status, media_type="application/json; charset=utf-8")
        payload = {"status": "ok", "db": "ok"} if db_ok else {"status": "error", "db": "error"}
        return _json_response(payload, status_code=status)

    @application.get("/health", tags=["system"])
    async def health_check() -> Response:
        return await _health_check_impl(is_head=False)

    @application.head("/health", include_in_schema=False)
    async def health_check_head() -> Response:
        return await _health_check_impl(is_head=True)

    @application.get("/", tags=["system"])
    async def root_ping() -> Response:
        return Response(
            content=b"OK",
            status_code=200,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @application.head("/", include_in_schema=False)
    async def root_ping_head() -> Response:
        return Response(status_code=200, media_type="text/plain; charset=utf-8")

    # ── WebApp HTML & PWA Static Assets ──

    @application.get("/webapp", include_in_schema=False)
    @application.get("/app", include_in_schema=False)
    async def serve_webapp() -> Response:
        html_file = _BASE_DIR / "webapp.html"
        if html_file.is_file():
            content = html_file.read_text(encoding="utf-8")
            return Response(
                content=content.encode("utf-8"),
                status_code=200,
                media_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-cache"},
            )
        return Response(
            content=b"<h1>Mini App template not found</h1>",
            status_code=404,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @application.get("/manifest.webmanifest", include_in_schema=False)
    @application.get("/sw.js", include_in_schema=False)
    @application.get("/logo.png", include_in_schema=False)
    @application.get("/logo.jpg", include_in_schema=False)
    async def serve_root_asset(request: Request) -> Response:
        entry = _ROOT_STATIC_FILES.get(request.url.path)
        if not entry:
            return Response(status_code=404)
        rel, ctype, no_cache = entry
        return _serve_safe_file(_BASE_DIR, rel, default_ctype=ctype, no_cache=no_cache)

    @application.get("/icons/{icon_name:path}", include_in_schema=False)
    async def serve_icon(icon_name: str) -> Response:
        return _serve_safe_file(_ICONS_DIR, icon_name)

    @application.get("/static/{file_path:path}", include_in_schema=False)
    async def serve_static_asset(file_path: str) -> Response:
        return _serve_safe_file(_STATIC_ASSETS_DIR, file_path)

    # ── API: User Data (GET) ──

    @application.get("/api/user-data", tags=["user"])
    async def api_user_data(auth: AuthDep) -> Response:
        data = await asyncio.to_thread(_build_user_data_payload, auth.tid)
        return _json_response(data, status_code=200)

    # ── API: Pairing (Public, per-IP rate-limited) ──

    @application.post("/api/pair", tags=["auth"])
    async def api_pair(request: Request, body: PairRequest) -> Response:
        client_ip = _client_ip_from_request(request)
        owner_tid, rate_limited, retry_after = await asyncio.to_thread(
            _consume_pairing_code, body.code, client_ip
        )
        if rate_limited:
            return _json_response(
                {
                    "success": False,
                    "rate_limited": True,
                    "error": "تم تجاوز عدد المحاولات المسموحة من هذا الجهاز؛ يرجى المحاولة لاحقاً.",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if not owner_tid or int(owner_tid) != int(OWNER_TELEGRAM_ID):
            return _json_response(
                {
                    "success": False,
                    "error": "الرمز غير صحيح أو منتهي الصلاحية. أرسل /app للبوت للحصول على رمز جديد.",
                },
                status_code=400,
            )

        device = str(body.device or request.headers.get("User-Agent", ""))[:120]
        token = await asyncio.to_thread(db.create_app_session, owner_tid, device)
        csrf_token = secrets.token_urlsafe(32)
        secure = _should_set_secure_cookie(request)
        cookies = [
            _format_cookie(
                SESSION_COOKIE_NAME, token, max_age=SESSION_COOKIE_MAX_AGE, http_only=True, secure=secure
            ),
            _format_cookie(
                CSRF_COOKIE_NAME, csrf_token, max_age=SESSION_COOKIE_MAX_AGE, http_only=False, secure=secure
            ),
        ]
        return _json_response({"success": True}, status_code=200, set_cookies=cookies)

    # ── API: Device Unlink / App Logout ──

    @application.post("/api/app-logout", tags=["auth"])
    @application.post("/api/unlink-device", tags=["auth"])
    async def api_unlink_device(
        request: Request,
        _body: EmptyActionRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        if auth.session_token:
            await asyncio.to_thread(db.revoke_app_token, auth.session_token)
        return _json_response({"success": True}, status_code=200, set_cookies=_clear_session_cookies(request))

    # ── API: LMS Login & Account Logout ──

    @application.post("/api/login", tags=["auth"])
    async def api_login(
        body: LoginRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        status_code, payload = await asyncio.to_thread(_do_lms_login, auth.tid, body.username, body.password)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/logout", tags=["auth"])
    async def api_logout(
        request: Request,
        _body: EmptyActionRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        await asyncio.to_thread(_do_logout, auth.tid, auth.session_token)
        return _json_response({"success": True}, status_code=200, set_cookies=_clear_session_cookies(request))

    # ── API: Monitoring & Courses ──

    @application.post("/api/toggle-monitor", tags=["monitoring"])
    async def api_toggle_monitor(
        _body: EmptyActionRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            user = db.get_user(auth.tid)
            if not user:
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            new_status = not bool(user.get("is_monitoring", 0))
            db.set_monitoring(auth.tid, new_status)
            return 200, {"success": True, "is_monitoring": new_status}

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/toggle-course", tags=["courses"])
    async def api_toggle_course(
        body: ToggleCourseRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            if not db.get_user(auth.tid):
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            db.toggle_course(auth.tid, body.course_id)
            course = db.get_course(auth.tid, body.course_id)
            is_mon = bool(course.get("is_monitored", 1)) if course else False
            return 200, {"success": True, "course_id": body.course_id, "is_monitored": is_mon}

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/mark-submitted", tags=["tasks"])
    async def api_mark_submitted(
        body: MarkSubmittedRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            if not db.get_user(auth.tid):
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            item_key = str(body.item_key or body.content_id or "").strip()
            is_sub = _parse_bool_flag(body.is_submitted, default=1)
            course_id_val: int | None = body.course_id
            title = str(body.title or body.content_name or "")
            url = str(body.url or body.content_url or "")
            item_type = str(
                body.item_type
                or body.content_type
                or ("quiz" if item_key.startswith("quiz_") else "assign")
            )
            db.mark_item_submitted(
                auth.tid,
                course_id_val,
                item_key,
                title=title,
                url=url,
                item_type=item_type,
                is_submitted=is_sub,
            )
            return 200, {
                "success": True,
                "content_id": item_key,
                "item_key": item_key,
                "is_submitted": bool(is_sub),
            }

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/update-settings", tags=["settings"])
    async def api_update_settings(
        body: UpdateSettingsRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            if not db.get_user(auth.tid):
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            if body.check_interval is not None:
                db.update_check_interval(auth.tid, body.check_interval)
            if body.is_monitoring is not None:
                db.set_monitoring(auth.tid, bool(body.is_monitoring))
            notify_fields = (
                body.notify_assign,
                body.notify_quiz,
                body.notify_resource,
                body.notify_grade,
                body.notify_announcement,
                body.notify_attendance,
            )
            if any(v is not None for v in notify_fields):
                db.update_notify_settings(
                    auth.tid,
                    assign=body.notify_assign,
                    quiz=body.notify_quiz,
                    resource=body.notify_resource,
                    grade=body.notify_grade,
                    announcement=body.notify_announcement,
                    attendance=body.notify_attendance,
                )
            return 200, {"success": True, "message": "تم حفظ الإعدادات"}

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/user-meta", tags=["user"])
    async def api_user_meta(
        body: UserMetaRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            if not db.get_user(auth.tid):
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            updated_meta = db.merge_user_meta(auth.tid, body.meta)
            return 200, {"success": True, "user_meta": updated_meta}

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/sync-courses", tags=["courses"])
    async def api_sync_courses(
        _body: EmptyActionRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        status_code, payload = await asyncio.to_thread(_do_sync_courses, auth.tid)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/delete-course", tags=["courses"])
    async def api_delete_course(
        body: DeleteCourseRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        def _run() -> tuple[int, dict[str, Any]]:
            if not db.get_user(auth.tid):
                return 404, {"success": False, "not_registered": True, "error": "المستخدم غير مسجل"}
            if body.clear_all:
                db.clear_all_user_courses(auth.tid)
                return 200, {"success": True, "cleared_all": True}
            assert body.course_id is not None
            db.remove_course(auth.tid, body.course_id)
            return 200, {"success": True, "course_id": body.course_id}

        status_code, payload = await asyncio.to_thread(_run)
        return _json_response(payload, status_code=status_code)

    @application.post("/api/add-course", tags=["courses"])
    async def api_add_course(
        body: AddCourseRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        status_code, payload = await asyncio.to_thread(_do_add_course, auth.tid, int(body.course_id))
        return _json_response(payload, status_code=status_code)

    @application.post("/api/force-check", tags=["monitoring"])
    async def api_force_check(
        _body: EmptyActionRequest,
        auth: CsrfAuthDep,
    ) -> Response:
        status_code, payload = await asyncio.to_thread(_do_force_check, auth.tid)
        return _json_response(payload, status_code=status_code)

    return application


app = create_app()


# ── Shared Asyncio Event Loop Uvicorn Lifecycle (F2) ────────────────────


class _NoSignalUvicornServer(uvicorn.Server):
    """Uvicorn Server subclass that leaves OS signal handling to python-telegram-bot."""

    def install_signal_handlers(self) -> None:
        return None


def build_uvicorn_config(
    host: str = "0.0.0.0",
    port: int | None = None,
    *,
    app_instance: FastAPI | None = None,
) -> uvicorn.Config:
    """Build hardened Uvicorn configuration with request keep-alive timeouts."""
    resolved_port = int(os.environ.get("PORT", "8080")) if port is None else int(port)
    return uvicorn.Config(
        app=app_instance or app,
        host=host,
        port=resolved_port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=REQUEST_TIMEOUT_SECONDS,
        limit_concurrency=100,
    )


async def start_webapp_server_async(
    host: str = "0.0.0.0",
    port: int | None = None,
    *,
    app_instance: FastAPI | None = None,
) -> asyncio.Task[Any]:
    """Start FastAPI/Uvicorn on the current asyncio event loop and return the server task."""
    global _UVICORN_SERVER, _UVICORN_TASK

    if _UVICORN_TASK is not None and not _UVICORN_TASK.done():
        await stop_webapp_server_async()

    config = build_uvicorn_config(host=host, port=port, app_instance=app_instance)
    server = _NoSignalUvicornServer(config=config)
    task = asyncio.create_task(server.serve(), name="webapp-uvicorn-server")
    _UVICORN_SERVER = server
    _UVICORN_TASK = task

    # Wait briefly until Uvicorn binds its listening socket (or fails fast on error)
    deadline = time.monotonic() + 5.0
    while not server.started and not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)

    if task.done():
        exc = task.exception()
        _UVICORN_SERVER = None
        _UVICORN_TASK = None
        if exc is not None:
            raise exc

    logger.info("FastAPI/Uvicorn WebApp server listening on %s:%s", config.host, config.port)
    return task


async def stop_webapp_server_async() -> None:
    """Gracefully stop the managed Uvicorn server running on the shared asyncio loop."""
    global _UVICORN_SERVER, _UVICORN_TASK
    server = _UVICORN_SERVER
    task = _UVICORN_TASK
    _UVICORN_SERVER = None
    _UVICORN_TASK = None

    if server is not None:
        server.should_exit = True
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass


def start_webapp_server(port: int | None = None, host: str = "0.0.0.0") -> None:
    """Run FastAPI/Uvicorn synchronously (for standalone execution)."""
    config = build_uvicorn_config(host=host, port=port)
    server = uvicorn.Server(config=config)
    logger.info("FastAPI/Uvicorn WebApp server running on %s:%s", config.host, config.port)
    server.run()


def run_web_server() -> None:
    """Backward-compatible standalone entrypoint reading PORT env var."""
    port = int(os.environ.get("PORT", "10000"))
    start_webapp_server(port=port)
