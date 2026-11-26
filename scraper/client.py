"""HTTP client, URL allowlist, WAF detection, and filename sanitization for Mutah LMS."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from config import LMS_BASE_URL

logger = logging.getLogger(__name__)

MAX_HTML_INSPECT_BYTES = 5 * 1024 * 1024
MAX_INTERSTITIAL_HTML_BYTES = 2 * 1024 * 1024

_ARABIC_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[<>:"|?*]+')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def sanitize_filename(filename: str, default: str = "document") -> str:
    """Strip path traversal, path separators, control chars, and invalid filename chars."""
    if not filename:
        return default
    s = _CONTROL_CHARS_RE.sub("", str(filename))
    s = s.replace("\\", "/")
    parts = [p for p in s.split("/") if p]
    s = parts[-1] if parts else ""
    while ".." in s:
        s = s.replace("..", "")
    s = _UNSAFE_FILENAME_CHARS_RE.sub("", s)
    s = s.strip(" .")
    if not s:
        return default
    if len(s) > 120:
        if "." in s:
            stem, ext = s.rsplit(".", 1)
            ext = ext[:16]
            s = f"{stem[:120 - len(ext) - 1]}.{ext}" if ext else stem[:120]
        else:
            s = s[:120]
    return s


def _assert_allowed_url(url: str) -> str:
    """Enforce HTTPS-only requests to the configured LMS host (blocks SSRF & http://)."""
    if not url or not isinstance(url, str):
        raise ValueError(f"Blocked invalid LMS URL: {url!r}")
    parts = urlsplit(url.strip())
    allowed_netloc = urlsplit(LMS_BASE_URL).netloc.lower()
    if parts.scheme.lower() != "https" or parts.netloc.lower() != allowed_netloc:
        raise ValueError(f"Blocked disallowed URL (scheme/host): {url}")
    return url


def _close_response(resp: Any) -> None:
    """Safely close an HTTP response object."""
    if resp is None:
        return
    close_fn = getattr(resp, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception as exc:
            logger.debug("Response close error: %s", exc)


def clean_url(url: str) -> str:
    """Strip volatile NetScaler WAF query parameters (__ncforminfo) for stable stored/display URLs."""
    if not url:
        return ""
    u = url.strip()
    if not u.startswith("http"):
        u = urljoin(LMS_BASE_URL + "/", u)
    try:
        parts = urlsplit(u)
        query = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.casefold() != "__ncforminfo"
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
    except Exception:
        return re.sub(r"([?&])__ncforminfo=[^&#]*&?", r"\1", u).rstrip("?&")


def response_content_type(resp: Any) -> str:
    """Return lowercased Content-Type header from an HTTP response."""
    headers = getattr(resp, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return ""
    raw = headers.get("Content-Type") or headers.get("content-type") or ""
    return raw.lower() if isinstance(raw, str) else ""


def parse_content_length(resp: Any) -> int | None:
    """Parse integer Content-Length header safely, returning None when absent or invalid."""
    headers = getattr(resp, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Content-Length") or headers.get("content-length")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def is_waf_challenge_text(text: str) -> bool:
    """Detect Citrix NetScaler / Barracuda WAF CAPTCHA validation HTML."""
    if not text:
        return False
    text_low = text[:2500].lower()
    return (
        "/captcha.gif" in text_low
        or "/captcha_resp" in text_low
        or "user validation required" in text_low
        or ("validation request" in text_low and "captcha" in text_low)
    )


def is_waf_challenge(resp: Any) -> bool:
    """Detect Citrix NetScaler / Barracuda WAF CAPTCHA validation pages."""
    if resp is None:
        return False
    return is_waf_challenge_text(resp.text or "")


def is_waf_response(resp: Any) -> bool:
    """Detect WAF CAPTCHA challenge on any non-binary HTTP response (even without HTML Content-Type)."""
    if resp is None:
        return False
    ctype = response_content_type(resp)
    binary_markers = (
        "application/pdf",
        "application/octet-stream",
        "image/",
        "audio/",
        "video/",
        "application/zip",
        "application/vnd.",
    )
    if ctype and any(b in ctype for b in binary_markers):
        return False
    return is_waf_challenge(resp)


def read_bounded_html(resp: Any, max_bytes: int = MAX_INTERSTITIAL_HTML_BYTES) -> str | None:
    """Read an HTML response body up to max_bytes via streaming (avoids OOM)."""
    cl = parse_content_length(resp)
    if cl is not None and cl > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if chunk:
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


@dataclass
class FetchResult:
    """Structured result for LMSScraper.fetch_course_content (avoids mutating self)."""

    content: dict[str, list[dict[str, Any]]]
    ok: bool
    session_expired: bool = False
    waf_blocked: bool = False
    error: str | None = None


class LMSAuthError(RuntimeError):
    """Raised when an authenticated LMS page still requires a login."""


class LMSWafError(RuntimeError):
    """Raised when the university WAF/NetScaler requests human CAPTCHA validation."""


class LMSHttpClient:
    """Thread-safe HTTP client for LMS requests with pacing, WAF cooldown, and host allowlist."""

    def __init__(
        self,
        min_interval: float = 0.5,
        jitter: tuple[float, float] = (0.05, 0.25),
        waf_cooldown: float = 900.0,
        headers: dict[str, str] | None = None,
        session: requests.Session | None = None,
    ):
        self._lock = threading.RLock()
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update(headers if headers is not None else HEADERS)
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = jitter
        self.waf_cooldown = max(0.0, float(waf_cooldown))
        self.last_request_at: float = 0.0
        self._waf_cooldown_until: float = 0.0

    @property
    def waf_cooldown_active(self) -> bool:
        with self._lock:
            return time.monotonic() < self._waf_cooldown_until

    def trigger_waf_cooldown(self, seconds: float | None = None) -> None:
        with self._lock:
            dur = self.waf_cooldown if seconds is None else max(0.0, float(seconds))
            self._waf_cooldown_until = time.monotonic() + dur

    def clear_waf_cooldown(self) -> None:
        with self._lock:
            self._waf_cooldown_until = 0.0

    def request(
        self,
        method: str,
        url: str,
        *,
        allow_waf_cooldown: bool = False,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            if not allow_waf_cooldown and time.monotonic() < self._waf_cooldown_until:
                raise LMSWafError(
                    "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ تم إيقاف الفحص مؤقتاً لتجنب الحظر"
                )
            _assert_allowed_url(url)

            if self.min_interval > 0 and self.last_request_at > 0:
                elapsed = time.monotonic() - self.last_request_at
                wait = self.min_interval - elapsed
                if wait > 0:
                    j_low, j_high = self.jitter
                    extra = random.uniform(j_low, j_high) if j_high > 0 else 0.0
                    time.sleep(wait + extra)

            method_upper = method.upper()
            if method_upper == "GET":
                resp = self.session.get(url, **kwargs)
            elif method_upper == "POST":
                resp = self.session.post(url, **kwargs)
            else:
                resp = self.session.request(method_upper, url, **kwargs)

            self.last_request_at = time.monotonic()

            try:
                for hist in getattr(resp, "history", None) or []:
                    hist_url = getattr(hist, "url", None)
                    if isinstance(hist_url, str) and hist_url:
                        _assert_allowed_url(hist_url)
                final_url = getattr(resp, "url", None)
                if isinstance(final_url, str) and final_url:
                    _assert_allowed_url(final_url)
            except ValueError:
                _close_response(resp)
                raise

            return resp

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def clear_cookies(self) -> None:
        with self._lock:
            try:
                self.session.cookies.clear()
            except Exception as exc:
                logger.debug("Cookie clear error: %s", exc)

    def close(self) -> None:
        with self._lock:
            try:
                self.session.close()
            except Exception as exc:
                logger.debug("Session close error: %s", exc)
