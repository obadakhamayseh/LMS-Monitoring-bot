import requests
import logging
import random
import re
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any, Tuple
from config import LMS_BASE_URL, LMS_LOGIN_URL

logger = logging.getLogger(__name__)

MAX_HTML_INSPECT_BYTES = 5 * 1024 * 1024
MAX_INTERSTITIAL_HTML_BYTES = 2 * 1024 * 1024

_ARABIC_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[<>:"|?*]+')

_NEGATIVE_SUBMISSION_RE = re.compile(
    r"not\s+submitted|no\s+submissions?\s+have\s+been\s+made(?:\s+yet)?|"
    r"no\s+submissions?|no\s+attempts?(?:\s+have\s+been\s+made)?|"
    r"not\s+(?:yet\s+)?graded|not\s+completed|not\s+finished|never\s+submitted|"
    r"\bdraft\b|in\s+progress|"
    r"لم\s+تسلم|لم\s+يتم\s+التسليم|لم\s+يتم\s+تقديم|غير\s+مسلم[ةه]?|"
    r"لا\s+توجد\s+محاول(?:ة|ات)|لا\s+يوجد\s+تسليم|لم\s+تقم\s+بأي\s+محاولة|"
    r"مسود[ةه]|لم\s+يتم\s+التقييم|غير\s+مقيم[ةه]?|لم\s+يقيم|"
    r"قيد\s+التنفيذ|قيد\s+التقدم|غير\s+مكتمل[ةه]?",
    re.I,
)
_POSITIVE_SUBMISSION_RE = re.compile(
    r"submitted\s+for\s+grading|assignment\s+was\s+submitted|"
    r"\bsubmitted\b|\bgraded\b|\bfinished\b|\bcompleted\b|"
    r"تم\s+تسليم(?:ها|ه)?\s+للتقييم|تم\s+تسليم\s+الواجب|تم\s+التسليم|"
    r"سلم\s+قبل|مسلم(?:ة|ه)?|تم\s+التقييم|مقيم(?:ة|ه)?|"
    r"منتهي(?:ة|ه)?|مكتمل(?:ة|ه)?|تم\s+الحل",
    re.I,
)


def parse_submission_state(text: str) -> Optional[bool]:
    """Parse English/Arabic Moodle submission or grading text (negative patterns first)."""
    if not text:
        return None
    cleaned = _ARABIC_DIACRITICS_RE.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned in ("-", "—", "–"):
        return None
    if _NEGATIVE_SUBMISSION_RE.search(cleaned):
        return False
    if _POSITIVE_SUBMISSION_RE.search(cleaned):
        return True
    return None


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
        except Exception as e:
            logger.debug(f"Response close error: {e}")


@dataclass
class FetchResult:
    """Structured result for LMSScraper.fetch_course_content (avoids mutating self)."""
    content: Dict[str, List[Dict[str, Any]]]
    ok: bool
    session_expired: bool = False
    waf_blocked: bool = False
    error: Optional[str] = None


class LMSAuthError(RuntimeError):
    """Raised when an authenticated LMS page still requires a login."""


class LMSWafError(RuntimeError):
    """Raised when the university WAF/NetScaler requests human CAPTCHA validation."""


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class LMSHttpClient:
    """Thread-safe HTTP client for LMS requests with pacing, WAF cooldown, and host allowlist."""

    def __init__(
        self,
        min_interval: float = 0.5,
        jitter: Tuple[float, float] = (0.05, 0.25),
        waf_cooldown: float = 900.0,
        headers: Optional[Dict[str, str]] = None,
        session: Optional[requests.Session] = None,
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

    def trigger_waf_cooldown(self, seconds: Optional[float] = None) -> None:
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
        **kwargs,
    ):
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

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def clear_cookies(self) -> None:
        with self._lock:
            try:
                self.session.cookies.clear()
            except Exception as e:
                logger.debug(f"Cookie clear error: {e}")

    def close(self) -> None:
        with self._lock:
            try:
                self.session.close()
            except Exception as e:
                logger.debug(f"Session close error: {e}")


class LMSScraper:

    def __init__(self, username: str, password: str, http_client: Optional[LMSHttpClient] = None):
        self.username = username
        self.password = password
        self.http = http_client or LMSHttpClient()
        self.session = self.http.session
        self._state_lock = threading.RLock()
        self._tls = threading.local()
        self.logged_in = False
        self.user_id: Optional[str] = None
        self.full_name: Optional[str] = None
        self.sesskey: Optional[str] = None
        self.moodle_theme: Optional[str] = None
        self.error_message: Optional[str] = None
        self.last_fetch_ok = True
        self.session_expired = False
        self.waf_blocked = False
        self.credentials_invalid = False

    @property
    def last_request_at(self) -> float:
        return self.http.last_request_at

    @last_request_at.setter
    def last_request_at(self, value: float) -> None:
        self.http.last_request_at = value

    def _suppress_legacy_state(self) -> bool:
        return bool(getattr(self._tls, "suppress_state_mutation", False))

    @staticmethod
    def _clean_url(url: str) -> str:
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

    @staticmethod
    def _is_waf_challenge_text(text: str) -> bool:
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

    @staticmethod
    def _is_waf_challenge(resp) -> bool:
        """Detect Citrix NetScaler / Barracuda WAF CAPTCHA validation pages."""
        if resp is None:
            return False
        return LMSScraper._is_waf_challenge_text(resp.text or "")

    @staticmethod
    def _extract_moodle_cfg(html_text: str) -> Dict[str, Any]:
        """Extract Moodle's embedded M.cfg JSON object (Moodle 4.x/5.x)."""
        if not html_text:
            return {}
        m = re.search(r"M\.cfg\s*=\s*(\{.*?\});", html_text, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(1))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _is_login_html(html_text: str) -> bool:
        if not html_text:
            return False
        soup = BeautifulSoup(html_text, "lxml")
        body = soup.find("body")
        if body and "notloggedin" in body.get("class", []):
            return True
        login_form = soup.find("form", id="login") or soup.find(
            "form", action=re.compile(r"/login/index\.php", re.I)
        )
        return bool(login_form and login_form.find("input", {"name": "username"}))

    def _is_login_response(self, resp) -> bool:
        if "/login/index.php" in (getattr(resp, "url", "") or "").lower():
            return True
        return self._is_login_html(resp.text)

    @staticmethod
    def _response_content_type(resp) -> str:
        headers = getattr(resp, "headers", None)
        if headers is None or not hasattr(headers, "get"):
            return ""
        raw = headers.get("Content-Type") or headers.get("content-type") or ""
        return raw.lower() if isinstance(raw, str) else ""

    @staticmethod
    def _parse_content_length(resp) -> Optional[int]:
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

    def authenticated_get(self, url: str, timeout: int = 30, stream: bool = False, **kwargs):
        """Fetch a protected LMS resource, enforcing host allowlist, streaming safety, WAF & auth checks."""
        suppress = self._suppress_legacy_state()
        if (not suppress and self.waf_blocked) or self.http.waf_cooldown_active:
            if not suppress:
                with self._state_lock:
                    self.last_fetch_ok = False
            raise LMSWafError("جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ تم إيقاف الفحص مؤقتاً لتجنب الحظر")

        _assert_allowed_url(url)
        resp = self.http.get(url, timeout=timeout, stream=stream, **kwargs)

        final_url = getattr(resp, "url", None)
        if isinstance(final_url, str) and final_url:
            try:
                _assert_allowed_url(final_url)
            except ValueError:
                _close_response(resp)
                raise

        if "/login/index.php" in (final_url or "").lower():
            _close_response(resp)
            logger.info("LMS session expired (login redirect); waiting for re-authentication")
            if not suppress:
                with self._state_lock:
                    self.logged_in = False
                    self.session_expired = True
                    self.last_fetch_ok = False
            raise LMSAuthError("انتهت جلسة LMS؛ يرجى تسجيل الدخول مجدداً من البوت")

        # Never read resp.text when stream=True or for non-HTML responses (prevents OOM/DoS)
        if stream:
            return resp

        ctype = self._response_content_type(resp)
        if ctype and ("text/html" not in ctype and "application/xhtml" not in ctype):
            return resp

        cl_bytes = self._parse_content_length(resp)
        if cl_bytes is not None and cl_bytes > MAX_HTML_INSPECT_BYTES:
            _close_response(resp)
            raise ValueError(f"HTML response exceeds inspection limit ({cl_bytes} bytes)")

        if self._is_waf_challenge(resp):
            _close_response(resp)
            logger.warning("University WAF CAPTCHA challenge encountered; backing off immediately")
            self.http.trigger_waf_cooldown()
            if not suppress:
                with self._state_lock:
                    self.waf_blocked = True
                    self.last_fetch_ok = False
            raise LMSWafError("جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA)؛ تم التوقف مؤقتاً")

        if not self._is_login_response(resp):
            return resp

        _close_response(resp)
        logger.info("LMS session expired; waiting for the user to sign in again")
        if not suppress:
            with self._state_lock:
                self.logged_in = False
                self.session_expired = True
                self.last_fetch_ok = False
        raise LMSAuthError("انتهت جلسة LMS؛ يرجى تسجيل الدخول مجدداً من البوت")

    def keep_session_alive(self) -> bool:
        """Make a lightweight authenticated request to keep an active monitor session warm."""
        resp = None
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=15)
            resp.raise_for_status()
            return self.logged_in
        except Exception as e:
            with self._state_lock:
                self.last_fetch_ok = False
            logger.warning(f"LMS keepalive failed: {e}")
            return False
        finally:
            _close_response(resp)

    def logout(self):
        """Terminate the LMS server session and clear local cookies."""
        resp = None
        try:
            if self.logged_in and self.sesskey:
                resp = self.http.get(
                    f"{LMS_BASE_URL}/login/logout.php?sesskey={self.sesskey}",
                    timeout=10,
                    allow_redirects=True,
                    allow_waf_cooldown=True,
                )
        except Exception as e:
            logger.debug(f"LMS logout request error: {e}")
        finally:
            _close_response(resp)
            with self._state_lock:
                self.logged_in = False
                self.sesskey = None
            self.http.clear_cookies()
            self.http.close()

    def close(self):
        self.http.close()

    def login(self) -> bool:
        with self._state_lock:
            self.logged_in = False
            self.session_expired = False
            self.waf_blocked = False
            self.credentials_invalid = False
            self.last_fetch_ok = True
            self.error_message = None
        self.http.clear_waf_cooldown()
        try:
            logger.info(f"محاولة تسجيل الدخول: {self.username}")

            resp = self.http.get(LMS_LOGIN_URL, timeout=30, allow_waf_cooldown=True)
            try:
                if resp.status_code != 200:
                    self.error_message = f"موقع الجامعة غير متاح (رمز {resp.status_code})"
                    logger.error(self.error_message)
                    return False

                if self._is_waf_challenge(resp):
                    self.http.trigger_waf_cooldown()
                    self.waf_blocked = True
                    self.error_message = "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) مؤقتاً؛ حاول مجدداً بعد عدة دقائق"
                    logger.warning(f"WAF CAPTCHA challenge on login GET for {self.username}")
                    return False

                login_get_text = resp.text
            finally:
                _close_response(resp)

            soup = BeautifulSoup(login_get_text, "lxml")

            # If session cookies already keep us logged in
            if self._check_logged_in(soup, login_get_text):
                self.logged_in = True
                self._extract_user_info(soup, login_get_text)
                logger.info(f"✅ جلسة نشطة بالفعل - {self.full_name}")
                return True

            # Preserve ALL hidden/default inputs (logintoken, anchor, NetScaler __ncforminfo, etc.)
            login_form = soup.find("form", id="login") or soup.find(
                "form", action=re.compile(r"/login/index\.php", re.I)
            )
            post_url = LMS_LOGIN_URL
            payload: Dict[str, str] = {}
            if login_form:
                action = (login_form.get("action") or "").strip()
                if action:
                    post_url = action if action.startswith("http") else urljoin(LMS_BASE_URL + "/", action)
                for inp in login_form.find_all("input"):
                    name = inp.get("name")
                    if not name:
                        continue
                    itype = (inp.get("type") or "text").lower()
                    if itype in ("hidden", "text", "password"):
                        payload[name] = inp.get("value", "")
                    elif itype == "checkbox" and name == "rememberusername":
                        payload[name] = "1"
            else:
                token_input = soup.find("input", {"name": "logintoken"})
                payload["logintoken"] = token_input.get("value", "") if token_input else ""
                payload["anchor"] = ""

            payload["username"] = self.username
            payload["password"] = self.password

            # Validate form action URL through LMSHttpClient host allowlist
            post_resp = self.http.post(
                post_url,
                data=payload,
                headers={"Referer": LMS_LOGIN_URL, "Origin": LMS_BASE_URL},
                timeout=30,
                allow_redirects=True,
                allow_waf_cooldown=True,
            )
            try:
                post_status = post_resp.status_code
                post_url_final = getattr(post_resp, "url", post_url)
                if post_status != 200:
                    self.error_message = f"فشل الاستجابة بعد إرسال البيانات (رمز {post_status})"
                    logger.error(self.error_message)
                    return False

                if self._is_waf_challenge(post_resp):
                    self.http.trigger_waf_cooldown()
                    self.waf_blocked = True
                    self.error_message = "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) بعد محاولة الدخول؛ حاول بعد عدة دقائق"
                    logger.warning(f"WAF CAPTCHA challenge on login POST for {self.username}")
                    return False

                post_text = post_resp.text
            finally:
                _close_response(post_resp)

            soup = BeautifulSoup(post_text, "lxml")

            if self._check_logged_in(soup, post_text):
                self.logged_in = True
                self._extract_user_info(soup, post_text)
                logger.info(f"✅ تسجيل الدخول ناجح - {self.full_name} (uid={self.user_id}, theme={self.moodle_theme})")
                return True

            my_resp = None
            try:
                my_resp = self.http.get(f"{LMS_BASE_URL}/my/", timeout=15, allow_waf_cooldown=True)
                if my_resp.status_code == 200 and not self._is_waf_challenge(my_resp) and "login" not in (my_resp.url or ""):
                    my_text = my_resp.text
                    my_soup = BeautifulSoup(my_text, "lxml")
                    if self._check_logged_in(my_soup, my_text):
                        self.logged_in = True
                        self._extract_user_info(my_soup, my_text)
                        logger.info(f"✅ تسجيل الدخول ناجح عبر /my/ - {self.full_name}")
                        return True
            except Exception as my_err:
                logger.debug(f"Login fallback /my/ check failed: {my_err}")
            finally:
                _close_response(my_resp)

            err = soup.find(class_=["loginerrors", "alert-danger", "error"]) or soup.find(id="loginerrormessage")
            if err:
                msg = err.get_text(" ", strip=True)
                msg_low = msg.lower()
                if any(k in msg_low for k in (
                    "invalid login", "incorrect", "غير صحيحة", "غير صحيح",
                    "خطأ في اسم المستخدم", "wrong password",
                )):
                    self.credentials_invalid = True
            else:
                title = soup.title.get_text(strip=True) if soup.title else ""
                msg = f"بيانات غير صحيحة أو تم حظر الجلسة (الصفحة: {title})"
            self.error_message = msg
            logger.error(f"❌ فشل تسجيل الدخول: {msg} | URL: {post_url_final}")
            return False

        except requests.exceptions.Timeout:
            self.error_message = "انتهت مهلة الاتصال بموقع الجامعة (Timeout)"
            logger.error(self.error_message)
            return False
        except requests.RequestException as e:
            self.error_message = f"خطأ في الاتصال بالشبكة: {e}"
            logger.error(self.error_message)
            return False
        except Exception as e:
            self.error_message = f"خطأ غير متوقع: {e}"
            logger.error(self.error_message)
            return False

    def _check_logged_in(self, soup: BeautifulSoup, html_text: str = "") -> bool:
        body = soup.find("body")
        if body:
            classes = body.get("class", [])
            if "notloggedin" in classes:
                return False
            uid = body.get("data-userid", "0")
            if uid and str(uid) != "0":
                return True

        # Moodle 5.x (classic/boost): userId is exposed in M.cfg
        cfg = self._extract_moodle_cfg(html_text or str(soup))
        if cfg:
            cfg_uid = cfg.get("userId")
            if cfg_uid and str(cfg_uid).isdigit() and int(cfg_uid) > 0:
                return True

        if soup.find("a", href=re.compile(r"logout", re.I)):
            return True
        if soup.find("form", action=re.compile(r"logout", re.I)):
            return True
        if soup.find(class_=["userinitials", "userbutton", "usermenu"]):
            return True
        if soup.find(attrs={"data-region": "user-menu"}):
            return True
        return False

    def _extract_sesskey(self, html_or_soup) -> Optional[str]:
        raw_text = html_or_soup if isinstance(html_or_soup, str) else ""
        if isinstance(html_or_soup, BeautifulSoup):
            inp = html_or_soup.find("input", {"name": "sesskey"})
            if inp and inp.get("value"):
                self.sesskey = inp["value"]
                return self.sesskey
            logout_a = html_or_soup.find("a", href=re.compile(r"sesskey=([A-Za-z0-9]+)"))
            if logout_a:
                m = re.search(r"sesskey=([A-Za-z0-9]+)", logout_a.get("href", ""))
                if m:
                    self.sesskey = m.group(1)
                    return self.sesskey
            raw_text = str(html_or_soup)

        cfg = self._extract_moodle_cfg(raw_text)
        if cfg:
            if cfg.get("sesskey"):
                self.sesskey = str(cfg["sesskey"])
            if cfg.get("userId") and str(cfg["userId"]).isdigit() and int(cfg["userId"]) > 0:
                self.user_id = str(cfg["userId"])
            if cfg.get("theme"):
                self.moodle_theme = str(cfg["theme"])
            if self.sesskey:
                return self.sesskey

        m = re.search(r'"sesskey"\s*:\s*"([A-Za-z0-9]+)"', raw_text)
        if m:
            self.sesskey = m.group(1)
            return self.sesskey
        return self.sesskey

    def _extract_user_info(self, soup: BeautifulSoup, html_text: str = ""):
        self._extract_sesskey(html_text or soup)
        body = soup.find("body")
        if body and not self.user_id:
            uid = body.get("data-userid")
            if uid and str(uid) != "0":
                self.user_id = str(uid)

        excluded_labels = {
            "user menu", "language selector", "قائمة المستخدم",
            "الملف الشخصي", "profile", "dashboard", "لوحة التحكم",
            "الصفحة الرئيسية", "home",
        }
        raw_name = None

        # 1. Explicit usertext or logininfo profile link
        for selector in [
            {"class": "usertext"},
            {"class": "username"},
            {"data-region": "user-name"},
        ]:
            el = soup.find(attrs=selector)
            if el:
                val = el.get_text(strip=True)
                if len(val) > 3 and val.lower() not in excluded_labels:
                    raw_name = val
                    break

        if not raw_name:
            login_info = soup.find(class_="logininfo")
            if login_info:
                prof_a = login_info.find("a", href=re.compile(r"/user/profile\.php"))
                if prof_a:
                    val = prof_a.get_text(strip=True)
                    if len(val) > 3 and val.lower() not in excluded_labels:
                        raw_name = val

        # 2. User button aria-label/title (excluding generic menu titles and short 2-char initials)
        if not raw_name:
            user_btn = soup.find(class_="userbutton") or soup.find(class_="usermenu")
            if user_btn:
                for attr_el in user_btn.find_all(attrs={"aria-label": True}):
                    val = attr_el.get("aria-label", "").strip()
                    if len(val) > 4 and val.lower() not in excluded_labels:
                        raw_name = val
                        break
                if not raw_name:
                    for attr_el in user_btn.find_all(attrs={"title": True}):
                        val = attr_el.get("title", "").strip()
                        if len(val) > 4 and val.lower() not in excluded_labels:
                            raw_name = val
                            break

        # 3. Fetch /user/profile.php for authoritative full name
        if not raw_name or len(raw_name.strip()) <= 3:
            resp = None
            try:
                resp = self.http.get(f"{LMS_BASE_URL}/user/profile.php", timeout=15)
                if resp.status_code == 200 and not self._is_waf_challenge(resp):
                    prof_soup = BeautifulSoup(resp.text, "lxml")
                    self._extract_sesskey(prof_soup)
                    if not self.user_id:
                        prof_body = prof_soup.find("body")
                        if prof_body:
                            self.user_id = prof_body.get("data-userid")
                    header_el = prof_soup.find(class_="page-header-headings") or prof_soup.find("h1")
                    if header_el:
                        val = header_el.get_text(strip=True)
                        if len(val) > 3 and val.lower() not in excluded_labels:
                            raw_name = val
            except Exception as e:
                logger.debug(f"فشل جلب الملف الشخصي للاسم: {e}")
            finally:
                _close_response(resp)

        if raw_name:
            cleaned = re.sub(r'\s*\d{5,}\s*$', '', raw_name).strip()
            cleaned = re.sub(r'^\s*\d{5,}\s*', '', cleaned).strip()
            if len(cleaned) > 2:
                self.full_name = cleaned
            else:
                self.full_name = raw_name.strip()

    def get_courses(self) -> List[Dict[str, Any]]:
        if not self.logged_in:
            logger.warning("لم يتم تسجيل الدخول")
            return []

        courses = []
        seen_ids: set = set()

        # 1. Try /my/courses.php and extract sesskey
        resp = None
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/courses.php", timeout=30)
            soup = BeautifulSoup(resp.text, "lxml")
            self._extract_sesskey(soup)
            self._extract_courses_from_soup(soup, courses, seen_ids)
        except Exception as e:
            logger.error(f"خطأ في صفحة My Courses: {e}")
        finally:
            _close_response(resp)

        # 2. Moodle 4.x AJAX WebService (loads courses rendered dynamically by block_myoverview)
        if self.sesskey:
            for classification in ("all", "inprogress", "All"):
                ajax_resp = None
                try:
                    ajax_url = (
                        f"{LMS_BASE_URL}/lib/ajax/service.php"
                        f"?sesskey={self.sesskey}&info=core_course_get_enrolled_courses_by_timeline_classification"
                    )
                    payload = [{
                        "index": 0,
                        "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                        "args": {
                            "offset": 0,
                            "limit": 0,
                            "classification": classification,
                            "sort": "fullname",
                            "customfieldname": "",
                            "customfieldvalue": "",
                        },
                    }]
                    ajax_resp = self.http.post(ajax_url, json=payload, timeout=20)
                    ctype = self._response_content_type(ajax_resp)
                    if ("text/html" in ctype or "application/xhtml" in ctype) and self._is_waf_challenge(ajax_resp):
                        self.http.trigger_waf_cooldown()
                        with self._state_lock:
                            self.waf_blocked = True
                        logger.warning("WAF CAPTCHA challenge on AJAX course fetch")
                        break
                    if ajax_resp.status_code == 200:
                        data = ajax_resp.json()
                        if isinstance(data, list) and data and not data[0].get("error"):
                            course_list = data[0].get("data", {}).get("courses", [])
                            for c_item in course_list:
                                cid = int(c_item.get("id", 0))
                                if cid <= 1 or cid in seen_ids:
                                    continue
                                cname = (c_item.get("fullname") or c_item.get("shortname") or f"مادة {cid}").strip()
                                view_url = c_item.get("viewurl") or f"{LMS_BASE_URL}/course/view.php?id={cid}"
                                seen_ids.add(cid)
                                courses.append({
                                    "id": cid,
                                    "name": cname,
                                    "url": view_url,
                                })
                            if courses:
                                break
                except Exception as e:
                    logger.debug(f"AJAX course fetch ({classification}) failed: {e}")
                finally:
                    _close_response(ajax_resp)

        # 3. Fallback to /my/ Dashboard
        if not courses:
            resp = None
            try:
                resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=30)
                soup = BeautifulSoup(resp.text, "lxml")
                self._extract_sesskey(soup)
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except Exception as e:
                logger.error(f"خطأ في Dashboard: {e}")
            finally:
                _close_response(resp)

        # 4. Fallback to User Profile page (supports both /course/view.php?id= and /user/view.php?course=)
        if not courses:
            resp = None
            try:
                prof_url = (
                    f"{LMS_BASE_URL}/user/profile.php?id={self.user_id}&showallcourses=1"
                    if self.user_id else f"{LMS_BASE_URL}/user/profile.php?showallcourses=1"
                )
                resp = self.authenticated_get(prof_url, timeout=30)
                soup = BeautifulSoup(resp.text, "lxml")
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except Exception as e:
                logger.error(f"خطأ في صفحة User Profile Courses: {e}")
            finally:
                _close_response(resp)

        logger.info(f"✅ تم جلب {len(courses)} مادة")
        return courses

    def _extract_courses_from_soup(self, soup: BeautifulSoup,
                                   courses: list, seen_ids: set):
        # Match both standard course view links and profile course links
        for a in soup.find_all("a", href=re.compile(r"(?:/course/view\.php\?id=\d+|/user/view\.php\?[^\"']*course=\d+)")):
            href = a.get("href", "")
            match = re.search(r"(?:course/view\.php\?id=|course=)(\d+)", href)
            if not match:
                continue
            cid = int(match.group(1))
            if cid <= 1 or cid in seen_ids:
                continue

            name = self._extract_course_name(a)
            if not name or name in ("الصفحة الرئيسية", "Home", "الموقع الرئيسي", "المقررات", "عرض المزيد", "View more"):
                continue

            seen_ids.add(cid)
            course_url = f"{LMS_BASE_URL}/course/view.php?id={cid}"

            courses.append({
                "id": cid,
                "name": name or f"مادة {cid}",
                "url": course_url,
            })

    @staticmethod
    def _clean_link_text(el) -> str:
        """Extract activity/course link name without Moodle hidden accessibility labels."""
        if el is None:
            return ""
        inst = el.find(class_="instancename")
        target = inst if inst else el
        parts = []
        for child in target.descendants:
            if getattr(child, "name", None) and "accesshide" in (child.get("class") or []):
                continue
            if isinstance(child, str):
                parent_classes = getattr(child.parent, "get", lambda k, d=None: [])("class", []) or []
                if "accesshide" not in parent_classes:
                    parts.append(child)
        text = re.sub(r"\s+", " ", "".join(parts)).strip()
        if not text:
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        return text

    def _extract_course_name(self, link_el) -> str:
        parent = link_el.parent
        for _ in range(4):
            if parent is None:
                break
            title = (
                parent.find(class_=re.compile(r"course-title|coursename|fullname")) or
                parent.find("h3") or parent.find("h4")
            )
            if title:
                txt = self._clean_link_text(title)
                if len(txt) > 3:
                    return txt
            parent = parent.parent

        name = self._clean_link_text(link_el)
        return name if len(name) > 2 else ""

    def fetch_course_content(
        self, course_id: int, course_soup: Optional[BeautifulSoup] = None
    ) -> FetchResult:
        """Fetch course content and return structured state without mutating self."""
        result: Dict[str, List[Dict[str, Any]]] = {
            "assignments": [],
            "quizzes": [],
            "resources": [],
            "grades": [],
            "announcements": [],
            "attendance": [],
        }

        prev_suppress = getattr(self._tls, "suppress_state_mutation", False)
        self._tls.suppress_state_mutation = True
        try:
            assign_status: Dict[str, Any] = {"ok": True, "error": None}
            result["assignments"] = self._get_activities_from_index(
                course_id, "assign", raise_auth_errors=True, status_out=assign_status
            )
            assign_ok = bool(assign_status["ok"])

            quiz_status: Dict[str, Any] = {"ok": True, "error": None}
            result["quizzes"] = self._get_activities_from_index(
                course_id, "quiz", raise_auth_errors=True, status_out=quiz_status
            )
            quiz_ok = bool(quiz_status["ok"])

            res_status: Dict[str, Any] = {"ok": True, "error": None}
            resources, page_soup = self._get_resources_from_course_page(
                course_id,
                soup=course_soup,
                return_soup=True,
                raise_auth_errors=True,
                status_out=res_status,
            )
            res_ok = bool(res_status["ok"])
            result["resources"] = resources

            # Supplement assignments & quizzes + full-year activity dates from the main course page
            completed_cmids = set()
            page_due_dates: Dict[str, str] = {}
            if page_soup is not None:
                completed_cmids = self._extract_course_page_completions(page_soup)
                page_due_dates = self._extract_course_page_activity_dates(page_soup)

                seen_assigns = {a["id"] for a in result["assignments"]}
                for a in page_soup.find_all("a", href=re.compile(r"/mod/assign/view\.php\?id=\d+")):
                    href = self._clean_url(a.get("href", ""))
                    id_m = re.search(r"id=(\d+)", href)
                    if not id_m:
                        continue
                    cmid = id_m.group(1)
                    act_id = f"assign_{cmid}"
                    if act_id in seen_assigns:
                        continue
                    seen_assigns.add(act_id)
                    name = self._clean_link_text(a)
                    result["assignments"].append({
                        "id": act_id,
                        "name": name or f"assign #{cmid}",
                        "url": href,
                        "type": "assign",
                        "course_id": course_id,
                        "due_date": page_due_dates.get(cmid, ""),
                        "is_submitted": cmid in completed_cmids,
                        "submission_text": "منجز في المقرر" if cmid in completed_cmids else "",
                    })

                seen_quizzes = {q["id"] for q in result["quizzes"]}
                for a in page_soup.find_all("a", href=re.compile(r"/mod/quiz/view\.php\?id=\d+")):
                    href = self._clean_url(a.get("href", ""))
                    id_m = re.search(r"id=(\d+)", href)
                    if not id_m:
                        continue
                    cmid = id_m.group(1)
                    act_id = f"quiz_{cmid}"
                    if act_id in seen_quizzes:
                        continue
                    seen_quizzes.add(act_id)
                    name = self._clean_link_text(a)
                    result["quizzes"].append({
                        "id": act_id,
                        "name": name or f"quiz #{cmid}",
                        "url": href,
                        "type": "quiz",
                        "course_id": course_id,
                        "due_date": page_due_dates.get(cmid, ""),
                        "is_submitted": cmid in completed_cmids,
                        "submission_text": "منجز في المقرر" if cmid in completed_cmids else "",
                    })

            # Fetch course grades, announcements, and attendance
            result["grades"] = self.get_course_grades(course_id, raise_auth_errors=True)
            result["announcements"] = self.get_course_announcements(
                course_id, course_soup=page_soup, raise_auth_errors=True
            )
            result["attendance"] = self.get_course_attendance(
                course_id, course_soup=page_soup, raise_auth_errors=True
            )

            # Cross-reference completion from course page badges & gradebook, and prefer full-year dates from course page
            graded_cmids = {
                g["item_id"].replace("mod_", "")
                for g in result["grades"]
                if g.get("item_id", "").startswith("mod_") and g.get("grade_val")
            }

            for a in result["assignments"]:
                cmid = str(a["id"]).replace("assign_", "")
                if page_due_dates.get(cmid):
                    a["due_date"] = page_due_dates[cmid]
                elif a.get("due_date"):
                    a["due_date"] = self._normalize_due_date_year(a["due_date"])
                if cmid in completed_cmids or cmid in graded_cmids:
                    a["is_submitted"] = True

            for q in result["quizzes"]:
                cmid = str(q["id"]).replace("quiz_", "")
                if page_due_dates.get(cmid):
                    q["due_date"] = page_due_dates[cmid]
                elif q.get("due_date"):
                    q["due_date"] = self._normalize_due_date_year(q["due_date"])
                if cmid in completed_cmids or cmid in graded_cmids:
                    q["is_submitted"] = True

            # Deep-check individual assignment & quiz pages when submission, due_date, or quiz grade is not yet known
            for a in result["assignments"]:
                if (not a.get("is_submitted") or not a.get("due_date")) and a.get("url"):
                    st = self.get_assignment_status(a["url"], raise_auth_errors=True)
                    if st.get("is_submitted"):
                        a["is_submitted"] = True
                        a["submission_text"] = st.get("status_text") or "تم التسليم"
                    if not a.get("due_date") and st.get("due_date"):
                        a["due_date"] = self._normalize_due_date_year(st["due_date"])

            for q in result["quizzes"]:
                cmid = str(q["id"]).replace("quiz_", "")
                needs_grade_check = bool(q.get("is_submitted")) and cmid not in graded_cmids and not q.get("grade_val")
                if (not q.get("is_submitted") or not q.get("due_date") or needs_grade_check) and q.get("url"):
                    qst = self.get_quiz_status(q["url"], raise_auth_errors=True)
                    if qst.get("is_submitted"):
                        q["is_submitted"] = True
                        q["submission_text"] = qst.get("status_text") or "تم الحل"
                    if not q.get("due_date") and qst.get("due_date"):
                        q["due_date"] = self._normalize_due_date_year(qst["due_date"])
                    if qst.get("grade_val"):
                        q["grade_val"] = qst["grade_val"]
                        if qst.get("grade_max"):
                            q["grade_max"] = qst["grade_max"]
                        if qst.get("percentage"):
                            q["percentage"] = qst["percentage"]
                        if qst.get("feedback"):
                            q["feedback"] = qst["feedback"]

            # Merge quiz grades discovered from quiz/index.php or quiz/view.php into result["grades"]
            quiz_cmids = {str(q["id"]).replace("quiz_", "") for q in result["quizzes"]}
            grades_by_id = {g["item_id"]: g for g in result["grades"]}
            for g in result["grades"]:
                g_cmid = g.get("item_id", "").replace("mod_", "")
                if g_cmid in quiz_cmids or "/mod/quiz/" in (g.get("url") or ""):
                    g["is_quiz"] = True

            for q in result["quizzes"]:
                cmid = str(q["id"]).replace("quiz_", "")
                q_grade = (q.get("grade_val") or "").strip()
                if not q_grade:
                    continue
                mod_key = f"mod_{cmid}"
                existing_g = grades_by_id.get(mod_key)
                if existing_g:
                    existing_g["is_quiz"] = True
                    if not (existing_g.get("grade_val") or "").strip():
                        existing_g["grade_val"] = q_grade
                    if not (existing_g.get("grade_max") or "").strip() and q.get("grade_max"):
                        existing_g["grade_max"] = q["grade_max"]
                    if not (existing_g.get("percentage") or "").strip() and q.get("percentage"):
                        existing_g["percentage"] = q["percentage"]
                    if not (existing_g.get("feedback") or "").strip() and q.get("feedback"):
                        existing_g["feedback"] = q["feedback"]
                else:
                    new_g = {
                        "item_id": mod_key,
                        "item_name": q.get("name") or f"Quiz #{cmid}",
                        "grade_val": q_grade,
                        "grade_max": q.get("grade_max") or "",
                        "percentage": q.get("percentage") or "",
                        "feedback": q.get("feedback") or "",
                        "url": q.get("url") or f"{LMS_BASE_URL}/mod/quiz/view.php?id={cmid}",
                        "course_id": course_id,
                        "is_quiz": True,
                    }
                    result["grades"].append(new_g)
                    grades_by_id[mod_key] = new_g

            ok = (assign_ok and quiz_ok and res_ok) or (res_ok and page_soup is not None)
            err = None
            if not ok:
                err = res_status.get("error") or assign_status.get("error") or quiz_status.get("error") or "Incomplete LMS fetch"
            return FetchResult(
                content=result,
                ok=ok,
                session_expired=False,
                waf_blocked=False,
                error=err,
            )

        except LMSWafError as e:
            return FetchResult(
                content=result,
                ok=False,
                session_expired=False,
                waf_blocked=True,
                error=str(e),
            )
        except LMSAuthError as e:
            return FetchResult(
                content=result,
                ok=False,
                session_expired=True,
                waf_blocked=False,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Unexpected error fetching course {course_id}: {e}")
            return FetchResult(
                content=result,
                ok=False,
                session_expired=False,
                waf_blocked=False,
                error=str(e),
            )
        finally:
            self._tls.suppress_state_mutation = prev_suppress

    def get_course_content(self, course_id: int, course_soup: Optional[BeautifulSoup] = None) -> Dict[str, List[Dict]]:
        """Deprecated backward-compatible wrapper around fetch_course_content().

        Mirrors result state into legacy attributes (last_fetch_ok/session_expired/waf_blocked)
        under a lock for callers not yet migrated to FetchResult.
        """
        res = self.fetch_course_content(course_id, course_soup=course_soup)
        with self._state_lock:
            self.last_fetch_ok = res.ok
            self.session_expired = res.session_expired
            self.waf_blocked = res.waf_blocked
            if res.session_expired:
                self.logged_in = False
        return res.content

    @staticmethod
    def _normalize_due_date_year(due_str: str) -> str:
        """Ensure Moodle 5.x yearless overview dates (e.g. 'Saturday, 28 November, 11:59 PM') include a 4-digit year."""
        if not due_str:
            return ""
        s = re.sub(r"\s+", " ", str(due_str)).strip()
        if not s or s in ("-", "—"):
            return ""
        # Pure unix timestamp (e.g. from Moodle 5 data-mdl-overview-value)
        if re.fullmatch(r"\d{9,11}", s):
            try:
                ts = int(s)
                if ts > 1_000_000_000:
                    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=3)))
                    hour12 = dt.strftime("%I").lstrip("0") or "12"
                    return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')}, {hour12}:{dt.strftime('%M %p')}"
            except Exception:
                pass
        # Already has a 4-digit year
        if re.search(r"\b20\d{2}\b", s):
            return s
        # Insert inferred year after month name (e.g. 'Saturday, 28 November, 11:59 PM' -> 'Saturday, 28 November 2026, 11:59 PM')
        month_names = (
            r"January|February|March|April|May|June|July|August|September|October|November|December|"
            r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
            r"يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر|"
            r"كانون الثاني|شباط|آذار|اذار|نيسان|أيار|ايار|حزيران|تموز|آب|اب|أيلول|ايلول|تشرين الأول|تشرين الاول|تشرين الثاني|كانون الأول|كانون الاول"
        )
        m = re.search(rf"(\b\d{{1,2}}\s+(?:{month_names})\b)", s, re.I)
        if m:
            now = datetime.now(timezone(timedelta(hours=3)))
            year = now.year
            # Handle Dec <-> Jan academic year boundary cleanly
            month_low = m.group(1).lower()
            if now.month >= 10 and any(k in month_low for k in ("jan", "feb", "يناير", "فبراير", "كانون الثاني", "شباط")):
                year += 1
            elif now.month <= 2 and any(k in month_low for k in ("nov", "dec", "نوفمبر", "ديسمبر", "تشرين الثاني", "كانون الأول", "كانون الاول")):
                year -= 1
            s = s[:m.end(1)] + f" {year}" + s[m.end(1):]
        return s

    @staticmethod
    def _extract_course_page_activity_dates(soup: BeautifulSoup) -> Dict[str, str]:
        """Extract full-year Due/Closes dates per cmid directly from Moodle 4.x/5.x course page."""
        dates: Dict[str, str] = {}
        if soup is None:
            return dates
        try:
            for act in soup.find_all(["li", "div"], class_=re.compile(r"\bactivity\b|\bactivity-item\b")):
                cmid = act.get("data-id") or ""
                if not cmid:
                    m = re.search(r"module-(\d+)", act.get("id") or "")
                    if m:
                        cmid = m.group(1)
                if not cmid:
                    continue
                act_dates = act.find(attrs={"data-region": "activity-dates"}) or act.find(class_=re.compile(r"activity-dates"))
                if not act_dates:
                    continue
                dates_txt = act_dates.get_text(" ", strip=True)
                m_due = re.search(
                    r"(?:Due|Closes|Closed|تاريخ الاستحقاق|موعد التسليم|تاريخ الإغلاق|يغلق|أغلق)\s*:\s*(.+?)(?=\s*(?:Opened|Opens|Due|Closes|Closed|تاريخ الفتح|يفتح|Done|To do|$))",
                    dates_txt,
                    re.I,
                )
                if m_due:
                    val = m_due.group(1).strip(" |·-")
                    if val and len(val) < 90:
                        dates[str(cmid)] = val
        except Exception as e:
            logger.debug(f"Course activity-dates parse error: {e}")
        return dates

    _VIEW_ONLY_COMPLETION_RE = re.compile(
        r"^(?:done|تم)?\s*:?\s*(?:view(?:\s+the\s+activity)?|عرض(?:\s+النشاط)?|معاينة|مشاهدة)$",
        re.I,
    )
    _PENDING_COMPLETION_CLASS_RE = re.compile(
        r"alert-secondary|badge-secondary|bg-secondary|text-bg-secondary|"
        r"btn-subtle-body|alert-warning|alert-danger|badge-danger|text-danger|incomplete",
        re.I,
    )
    _SUCCESS_COMPLETION_CLASS_RE = re.compile(
        r"btn-success|bg-success|badge-success|alert-success|text-bg-success",
        re.I,
    )

    @classmethod
    def _is_completion_region_complete(cls, comp_region) -> bool:
        """Return True only when a Moodle completion region is fully completed.

        Rejects any region containing 'To do', mark-done toggles, secondary/pending states,
        or only a green 'Done: View' partial completion badge.
        """
        if comp_region is None:
            return False

        # 1. Explicit undone manual toggle in Moodle 4.x/5.x
        if comp_region.find(attrs={"data-toggletype": re.compile(r"mark-done", re.I)}):
            return False

        # 2. Any pending / secondary / incomplete badge or button remaining in the region
        if comp_region.find(class_=cls._PENDING_COMPLETION_CLASS_RE):
            return False

        raw_txt = _ARABIC_DIACRITICS_RE.sub("", comp_region.get_text(" ", strip=True)).lower()
        if any(
            neg in raw_txt
            for neg in (
                "to do", "mark as done", "not done", "not completed", "incomplete", "failed",
                "للقيام به", "المطلوب", "وضع علامة", "غير مكتمل", "لم يكتمل", "غير منجز", "مطلوب",
            )
        ):
            return False

        # 3. Manual completion toggled to Done (data-toggletype="manual:undo")
        if comp_region.find(attrs={"data-toggletype": re.compile(r"undo", re.I)}):
            return True

        # 4. Success badges/buttons: require at least one non-View-only badge
        success_els = comp_region.find_all(class_=cls._SUCCESS_COMPLETION_CLASS_RE)
        if success_els:
            for el in success_els:
                el_txt = _ARABIC_DIACRITICS_RE.sub("", el.get_text(" ", strip=True))
                el_txt = re.sub(r"\s+", " ", el_txt).strip()
                if not el_txt:
                    continue
                if cls._VIEW_ONLY_COMPLETION_RE.match(el_txt):
                    continue
                return True
            # Every green badge was View-only -> NOT submitted/complete
            return False

        # 5. Text-only fallback (excluding View-only text)
        if cls._VIEW_ONLY_COMPLETION_RE.match(raw_txt):
            return False
        if any(k in raw_txt for k in ("done:", "done", "تم:", "مكتمل", "منجز")):
            return True
        return False

    @staticmethod
    def _extract_course_page_completions(soup: BeautifulSoup) -> set:
        """Detect completed activity module IDs (cmids) from Moodle 3.x/4.x/5.x course page."""
        completed = set()
        if soup is None:
            return completed
        try:
            for act in soup.find_all(["li", "div"], class_=re.compile(r"\bactivity\b|\bactivity-item\b")):
                cmid = act.get("data-id") or ""
                if not cmid:
                    act_id_attr = act.get("id") or ""
                    m = re.search(r"module-(\d+)", act_id_attr)
                    if m:
                        cmid = m.group(1)
                if not cmid:
                    link = act.find("a", href=re.compile(r"/mod/\w+/view\.php\?id=(\d+)"))
                    if link:
                        m = re.search(r"id=(\d+)", link.get("href", ""))
                        if m:
                            cmid = m.group(1)
                if not cmid:
                    continue

                # 1. Moodle 4.x/5.x completion badge/button (Bootstrap 4 & 5)
                comp_region = act.find(attrs={"data-region": "completion-info"}) or act.find(class_=re.compile(r"activity-completion"))
                if comp_region:
                    if LMSScraper._is_completion_region_complete(comp_region):
                        completed.add(str(cmid))
                    continue

                # 2. Moodle 3.x completion icon (completion-auto-y / completion-manual-y / completion-auto-pass)
                for img in act.find_all("img"):
                    src = (img.get("src") or "").lower()
                    title = (img.get("title") or img.get("alt") or "").lower()
                    if any(k in src for k in ["completion-auto-y", "completion-manual-y", "completion-auto-pass"]):
                        completed.add(str(cmid))
                        break
                    if any(k in title for k in ["completed:", "مكتمل:", "تم إنجاز"]):
                        if "not completed" not in title and "غير مكتمل" not in title:
                            completed.add(str(cmid))
                            break
        except Exception as e:
            logger.debug(f"Course completion badge parse error: {e}")
        return completed

    @staticmethod
    def _looks_like_date(text: str) -> bool:
        if not text or len(text) < 6 or len(text) > 90:
            return False
        t = text.lower()
        months = [
            "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
            "يناير", "فبراير", "مارس", "أبريل", "ابريل", "مايو", "يونيو", "يوليو", "أغسطس", "اغسطس",
            "سبتمبر", "أكتوبر", "اكتوبر", "نوفمبر", "ديسمبر",
            "كانون", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين",
        ]
        if any(m in t for m in months) and any(ch.isdigit() for ch in t):
            return True
        if re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", t):
            return True
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", t):
            return True
        return False

    def _get_activities_from_index(
        self,
        course_id: int,
        mod_type: str,
        *,
        raise_auth_errors: bool = False,
        status_out: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        url = f"{LMS_BASE_URL}/mod/{mod_type}/index.php?id={course_id}"
        activities = []
        resp = None

        try:
            resp = self.authenticated_get(url, timeout=30)
            if resp.status_code == 404:
                return activities
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # Support both Moodle 5.x (course-overview-table) and Moodle 3.x/4.x (generaltable)
            table = soup.find("table", class_=re.compile(r"course-overview-table|generaltable"))
            if table:
                all_rows = table.find_all("tr")
                due_col_idx = None
                sub_col_idx = None
                grade_col_idx = None
                name_col_idx = None

                if all_rows:
                    header_cells = all_rows[0].find_all(["th", "td"])
                    for idx, hc in enumerate(header_cells):
                        col_key = (hc.get("data-mdl-overview-column") or "").lower()
                        htxt = hc.get_text(strip=True).lower()
                        if col_key == "name" or any(k in htxt for k in ["name", "الاسم", "النشاط"]):
                            name_col_idx = idx
                        elif col_key in ("duedate", "allowsubmissionsfromdate", "closedate", "timeclose") or any(
                            k in htxt for k in ["due", "استحقاق", "موعد", "تاريخ", "close", "إغلاق", "انتهاء"]
                        ):
                            due_col_idx = idx
                        elif col_key in ("submissionstatus", "status", "attempts") or any(
                            k in htxt for k in ["status", "submission", "تسليم", "محاولة"]
                        ):
                            sub_col_idx = idx
                        elif col_key == "grade" or any(k in htxt for k in ["grade", "درجة", "علامة", "تقييم"]):
                            grade_col_idx = idx

                rows = all_rows[1:]
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue

                    link = (
                        row.find("a", class_=re.compile(r"activityname")) or
                        row.find("a", href=re.compile(rf"/mod/{mod_type}/view\.php\?id=\d+"))
                    )
                    if not link:
                        continue

                    href = self._clean_url(link.get("href", ""))
                    if f"/mod/{mod_type}/" not in href:
                        continue

                    id_match = re.search(r"id=(\d+)", href)
                    activity_id = row.get("data-mdl-overview-cmid") or (id_match.group(1) if id_match else href)

                    # In Moodle 5.x overview tables, data-mdl-overview-value on the name cell holds the exact clean name
                    name_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": "name"})
                    name = self._clean_link_text(link)
                    if not name and name_cell and name_cell.get("data-mdl-overview-value"):
                        name = str(name_cell.get("data-mdl-overview-value")).strip()

                    due_date = ""
                    is_submitted = False
                    submission_text = ""
                    sub_decided = False

                    # 1. Moodle 5.x structured data-mdl-overview-item cells
                    due_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": re.compile(r"duedate|close", re.I)})
                    if due_cell:
                        ov_val = (due_cell.get("data-mdl-overview-value") or "").strip()
                        d_txt = due_cell.get_text(" ", strip=True)
                        if ov_val and re.fullmatch(r"\d{9,11}", ov_val) and int(ov_val) > 1_000_000_000:
                            due_date = self._normalize_due_date_year(ov_val)
                        elif d_txt and d_txt not in ("-", "—"):
                            due_date = self._normalize_due_date_year(d_txt)
                    elif due_col_idx is not None and due_col_idx < len(cells):
                        d_txt = cells[due_col_idx].get_text(" ", strip=True)
                        if d_txt and d_txt not in ("-", "—"):
                            due_date = self._normalize_due_date_year(d_txt)

                    sub_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": re.compile(r"submission|status", re.I)})
                    if not sub_cell and sub_col_idx is not None and sub_col_idx < len(cells):
                        sub_cell = cells[sub_col_idx]
                    if sub_cell:
                        s_txt = sub_cell.get_text(" ", strip=True)
                        if s_txt and s_txt not in ("-", "—"):
                            submission_text = s_txt
                            parsed_sub = parse_submission_state(s_txt)
                            if parsed_sub is not None:
                                is_submitted = parsed_sub
                                sub_decided = True

                    grade_val = ""
                    grade_max = ""
                    percentage = ""
                    grade_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": "grade"})
                    if not grade_cell and grade_col_idx is not None and grade_col_idx < len(cells):
                        grade_cell = cells[grade_col_idx]
                    if grade_cell:
                        g_txt = grade_cell.get_text(" ", strip=True)
                        parsed_g = self._parse_quiz_grade_text(g_txt)
                        if parsed_g.get("grade_val"):
                            grade_val = parsed_g["grade_val"]
                            grade_max = parsed_g.get("grade_max", "")
                            percentage = parsed_g.get("percentage", "")
                            is_submitted = True
                            sub_decided = True
                            if not submission_text:
                                submission_text = f"تم التقييم ({g_txt})"

                    # 2. Fallback cell scan (strictly excluding the name/section cell & grade cell)
                    for idx, cell in enumerate(cells):
                        if cell.get("data-mdl-overview-item") in ("name", "grade"):
                            continue
                        if name_col_idx is not None and idx == name_col_idx:
                            continue
                        if link in cell.descendants or cell == link.parent:
                            continue
                        if grade_col_idx is not None and idx == grade_col_idx:
                            continue
                        if sub_cell is not None and cell is sub_cell:
                            continue

                        cell_txt = cell.get_text(" ", strip=True)
                        if not cell_txt or cell_txt in ("-", "—"):
                            continue

                        parsed_cell = parse_submission_state(cell_txt)
                        if parsed_cell is not None and not sub_decided:
                            is_submitted = parsed_cell
                            submission_text = cell_txt
                            sub_decided = True
                        elif not due_date and self._looks_like_date(cell_txt):
                            due_date = self._normalize_due_date_year(cell_txt)

                    activities.append({
                        "id": f"{mod_type}_{activity_id}",
                        "name": name or f"{mod_type} #{activity_id}",
                        "url": href,
                        "type": mod_type,
                        "course_id": course_id,
                        "due_date": due_date,
                        "is_submitted": is_submitted,
                        "submission_text": submission_text,
                        "grade_val": grade_val,
                        "grade_max": grade_max,
                        "percentage": percentage,
                    })

            if not activities:
                seen_ids = set()
                for a in soup.find_all("a", href=re.compile(
                    rf"/mod/{mod_type}/view\.php\?id=\d+"
                )):
                    href = self._clean_url(a.get("href", ""))
                    id_match = re.search(r"id=(\d+)", href)
                    activity_id = id_match.group(1) if id_match else href
                    full_id = f"{mod_type}_{activity_id}"
                    if full_id in seen_ids:
                        continue
                    seen_ids.add(full_id)
                    name = self._clean_link_text(a)

                    activities.append({
                        "id": full_id,
                        "name": name or f"{mod_type} #{activity_id}",
                        "url": href,
                        "type": mod_type,
                        "course_id": course_id,
                        "due_date": "",
                        "is_submitted": False,
                        "submission_text": "",
                    })

            logger.debug(f"المادة {course_id} | {mod_type}: {len(activities)} نشاط")

        except (LMSAuthError, LMSWafError) as e:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(e)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            if raise_auth_errors:
                raise
        except Exception as e:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(e)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            logger.error(f"خطأ في جلب {mod_type} للمادة {course_id}: {e}")
        finally:
            _close_response(resp)

        return activities

    def _get_resources_from_course_page(
        self,
        course_id: int,
        soup: Optional[BeautifulSoup] = None,
        return_soup: bool = False,
        *,
        raise_auth_errors: bool = False,
        status_out: Optional[Dict[str, Any]] = None,
    ):
        resources = []
        url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
        resp = None

        try:
            if soup is None:
                resp = self.authenticated_get(url, timeout=30)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")

            resource_patterns = [
                r"/mod/resource/view\.php\?id=\d+",
                r"/mod/folder/view\.php\?id=\d+",
                r"/mod/url/view\.php\?id=\d+",
                r"/mod/page/view\.php\?id=\d+",
                r"/mod/forum/view\.php\?id=\d+",
                r"/mod/scorm/view\.php\?id=\d+",
                r"/mod/book/view\.php\?id=\d+",
            ]
            combined = "|".join(resource_patterns)

            seen = set()
            for a in soup.find_all("a", href=re.compile(combined)):
                raw_href = a.get("href", "")
                if not raw_href.startswith("http"):
                    raw_href = urljoin(LMS_BASE_URL + "/", raw_href)
                clean_href = self._clean_url(raw_href)

                id_match = re.search(r"id=(\d+)", clean_href)
                if not id_match:
                    continue

                type_match = re.search(r"/mod/(\w+)/", clean_href)
                mod_type = type_match.group(1) if type_match else "resource"

                activity_id = f"{mod_type}_{id_match.group(1)}"
                if activity_id in seen:
                    continue
                seen.add(activity_id)

                name = self._clean_link_text(a)

                resources.append({
                    "id": activity_id,
                    "name": name or f"مورد #{id_match.group(1)}",
                    "url": clean_href,
                    "raw_url": raw_href,
                    "type": mod_type,
                    "course_id": course_id,
                    "due_date": "",
                })

            logger.debug(f"المادة {course_id} | resources: {len(resources)} مورد")

        except (LMSAuthError, LMSWafError) as e:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(e)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            if raise_auth_errors:
                raise
        except Exception as e:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(e)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            logger.error(f"خطأ في جلب موارد المادة {course_id}: {e}")
        finally:
            _close_response(resp)

        if return_soup:
            return resources, soup
        return resources

    def get_course_info(self, course_id: int):
        course_name = ""
        content = {"assignments": [], "quizzes": [], "resources": []}
        resp = None

        try:
            url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
            resp = self.authenticated_get(url, timeout=30)

            if resp.status_code == 404 or "login" in (getattr(resp, "url", "") or ""):
                return None, content

            soup = BeautifulSoup(resp.text, "lxml")
            _close_response(resp)
            resp = None

            title_el = (
                soup.find("h1") or
                soup.find(class_=re.compile(r"page-header-headings")) or
                soup.find("title")
            )
            if title_el:
                raw_title = title_el.get_text(" ", strip=True)
                course_name = raw_title.split("|")[0].strip()
                course_name = re.sub(r"^(?:Course|المقرر|المادة)\s*:\s*", "", course_name, flags=re.I).strip()

            if not course_name:
                crumb = soup.find(class_=re.compile(r"breadcrumb"))
                if crumb:
                    items = crumb.find_all("li")
                    if items:
                        course_name = items[-1].get_text(strip=True)

            if not course_name:
                return None, content

            content = self.get_course_content(course_id, course_soup=soup)

        except (LMSAuthError, LMSWafError):
            with self._state_lock:
                self.last_fetch_ok = False
            return None, content
        except Exception as e:
            with self._state_lock:
                self.last_fetch_ok = False
            logger.error(f"get_course_info error for {course_id}: {e}")
            return None, content
        finally:
            _close_response(resp)

        return course_name, content

    def get_assignment_due_date(self, assignment_url: str) -> str:
        resp = None
        try:
            resp = self.authenticated_get(assignment_url, timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")

            for row in soup.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    if any(k in label for k in ["due", "يُسلَّم", "نهائي", "deadline"]):
                        return cells[1].get_text(strip=True)

        except Exception as e:
            logger.debug(f"Error fetching assignment due date at {assignment_url}: {e}")
        finally:
            _close_response(resp)
        return "غير محدد"

    def get_assignment_status(self, url: str, *, raise_auth_errors: bool = False) -> Dict[str, Any]:
        """Fetch detailed submission status for an individual assignment."""
        result = {
            "is_submitted": False,
            "status_text": "غير معروف",
            "due_date": "",
            "grading_status": "",
            "time_remaining": "",
        }
        resp = None
        try:
            resp = self.authenticated_get(url, timeout=20)
            if resp.status_code != 200:
                return result
            soup = BeautifulSoup(resp.text, "lxml")

            for tr in soup.find_all("tr"):
                th = tr.find(["th", "td"])
                cells = tr.find_all("td")
                if not th or not cells:
                    continue
                header = th.get_text(strip=True).lower()
                val = cells[-1].get_text(strip=True)

                if any(k in header for k in ["submission status", "حالة التسليم", "حالة المحاولة"]):
                    result["status_text"] = val
                    st = parse_submission_state(val)
                    if st is not None:
                        result["is_submitted"] = st

                elif any(k in header for k in ["due date", "تاريخ الاستحقاق", "موعد التسليم", "يُسلَّم"]):
                    result["due_date"] = val

                elif any(k in header for k in ["grading status", "حالة التقييم"]):
                    result["grading_status"] = val
                    # Only promote to submitted when graded; 'Not graded' must NOT overwrite a submitted assignment
                    if parse_submission_state(val) is True:
                        result["is_submitted"] = True

                elif any(k in header for k in ["file submissions", "تسليمات الملفات", "online text", "نص عبر الإنترنت"]):
                    if val and val not in ("-", "—"):
                        result["is_submitted"] = True

                elif any(k in header for k in ["time remaining", "الوقت المتبقي"]):
                    result["time_remaining"] = val
                    if parse_submission_state(val) is True:
                        result["is_submitted"] = True

            # Moodle 4.x/5.x activity dates header (Opened / Due)
            if not result["due_date"]:
                act_dates = soup.find(attrs={"data-region": "activity-dates"}) or soup.find(class_=re.compile(r"activity-dates"))
                if act_dates:
                    dates_txt = act_dates.get_text(" ", strip=True)
                    m_due = re.search(
                        r"(?:Due|Closes|Closed|تاريخ الاستحقاق|موعد التسليم|يغلق|أغلق)\s*:\s*(.+?)(?=\s*(?:Opened|Opens|Due|Closes|Closed|تاريخ الفتح|يفتح|$))",
                        dates_txt,
                        re.I,
                    )
                    if m_due:
                        result["due_date"] = m_due.group(1).strip()

            # Check Moodle completion region safely (ignores View-only / partial completion badges)
            if not result["is_submitted"]:
                comp_region = soup.find(attrs={"data-region": "completion-info"}) or soup.find(class_=re.compile(r"activity-completion"))
                if comp_region and self._is_completion_region_complete(comp_region):
                    result["is_submitted"] = True

            # Explicit submission/grading status CSS classes (exclude generic completion badge-success)
            if not result["is_submitted"]:
                badge = soup.find(class_=re.compile(r"submissionstatussubmitted|earlysubmission|gradingstatusgraded|status-submitted"))
                if badge:
                    btxt = badge.get_text(" ", strip=True)
                    if parse_submission_state(btxt) is True:
                        result["is_submitted"] = True

        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as e:
            logger.warning(f"Error checking assignment status at {url}: {e}")
        finally:
            _close_response(resp)
        return result

    @staticmethod
    def _parse_quiz_grade_text(raw_text: str) -> Dict[str, str]:
        """Parse Moodle quiz grade text such as '15.00 out of 15.00 ( 100 %)', '14.5 / 15', or '15.00'."""
        out = {"grade_val": "", "grade_max": "", "percentage": ""}
        if not raw_text:
            return out
        s = re.sub(r"\s+", " ", str(raw_text)).strip()
        s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        low = s.lower()
        if not s or s in ("-", "—", "empty", "فارغ") or any(
            neg in low for neg in (
                "not yet graded", "لم يتم التقييم", "لم يُقيَّم", "review not permitted",
                "غير مسموح", "no grade", "بدون علامة",
            )
        ):
            return out

        # Percentage in parentheses or trailing: e.g. "( 100 %)" or "93.33%"
        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
        if m_pct:
            out["percentage"] = f"{m_pct.group(1)}%"

        # Pattern 1: "15.00 out of 15.00" / "15.00 من 15.00" / "15.00 من أصل 15.00"
        m_out = re.search(r"(\d+(?:\.\d+)?)\s*(?:out\s+of|من\s+أصل|من)\s*(\d+(?:\.\d+)?)", s, re.I)
        if m_out:
            out["grade_val"] = m_out.group(1)
            out["grade_max"] = f"0–{m_out.group(2)}"
            return out

        # Pattern 2: "15.00 / 15.00" or "14/15"
        m_slash = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
        if m_slash:
            out["grade_val"] = m_slash.group(1)
            out["grade_max"] = f"0–{m_slash.group(2)}"
            return out

        # Pattern 3: Pure number (e.g. "15.00" from quiz/index.php Grade column)
        m_num = re.fullmatch(r"\d+(?:\.\d+)?", s)
        if m_num:
            out["grade_val"] = s
            return out

        return out

    def get_quiz_status(self, url: str, *, raise_auth_errors: bool = False) -> Dict[str, Any]:
        """Fetch completion/attempt status, released grade, and close date for an individual quiz."""
        result = {
            "is_submitted": False,
            "status_text": "",
            "due_date": "",
            "grade_val": "",
            "grade_max": "",
            "percentage": "",
            "feedback": "",
        }
        resp = None
        try:
            resp = self.authenticated_get(url, timeout=20)
            if resp.status_code != 200:
                return result
            soup = BeautifulSoup(resp.text, "lxml")

            # 1. Check all tables on the quiz page (handles quizreviewsummary cards & quizattemptsummary tables)
            # Note: Moodle shows BOTH 'Marks' (raw points, e.g. 5/5) AND 'Grade' (scaled grade, e.g. 10/10).
            # Always prefer 'Grade' over 'Marks'.
            has_final_grade_row = False
            for tr in soup.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if not cells:
                    continue
                if len(cells) >= 2:
                    hdr = cells[0].lower()
                    val_raw = cells[-1]
                    val = val_raw.lower()
                    if any(k in hdr for k in ["status", "state", "الحالة", "حالة"]):
                        if parse_submission_state(val_raw) is True:
                            result["is_submitted"] = True
                            result["status_text"] = "تم الحل (Finished)"
                    elif any(k in hdr for k in ["completed", "اكتمل", "تاريخ الإكتمال"]) and len(val) > 4 and val not in ("-", "—"):
                        if parse_submission_state(val_raw) is not False:
                            result["is_submitted"] = True
                            result["status_text"] = result["status_text"] or "تم الحل (Finished)"
                    elif any(k in hdr for k in ["grade", "الدرجة", "العلامة", "النتيجة"]):
                        pg = self._parse_quiz_grade_text(val_raw)
                        if pg.get("grade_val"):
                            result["grade_val"] = pg["grade_val"]
                            result["grade_max"] = pg.get("grade_max", "")
                            result["percentage"] = pg.get("percentage", "")
                            result["is_submitted"] = True
                            result["status_text"] = result["status_text"] or "تم التقييم"
                            has_final_grade_row = True
                    elif any(k in hdr for k in ["mark", "marks", "نقاط"]) and not has_final_grade_row and not result["grade_val"]:
                        pg = self._parse_quiz_grade_text(val_raw)
                        if pg.get("grade_val"):
                            result["grade_val"] = pg["grade_val"]
                            result["grade_max"] = pg.get("grade_max", "")
                            result["percentage"] = pg.get("percentage", "")
                            result["is_submitted"] = True
                            result["status_text"] = result["status_text"] or "تم التقييم"
                row_txt = " ".join(cells)
                row_txt_low = row_txt.lower()
                if any(k in row_txt_low for k in ["status", "الحالة"]) and parse_submission_state(row_txt) is True:
                    result["is_submitted"] = True
                    result["status_text"] = result["status_text"] or "تم الحل (Finished)"

            # 2. Check final grade heading or feedback (e.g. "Your final grade for this quiz is 15.00/15.00.")
            grade_blocks = []
            fb_el = soup.find(id="feedback")
            if fb_el:
                grade_blocks.append(fb_el)
            for el in soup.find_all(["div", "h2", "h3", "p"], class_=re.compile(r"quizgradefeedback|quizattemptcounts|grade")):
                grade_blocks.append(el)
            for el in soup.find_all(["h2", "h3", "div"]):
                txt_l = el.get_text(" ", strip=True).lower()
                if len(txt_l) < 160 and any(k in txt_l for k in [
                    "your final grade for this quiz is", "final grade", "highest grade",
                    "علامتك النهائية", "الدرجة النهائية", "أعلى درجة",
                ]):
                    grade_blocks.append(el)

            for gb in grade_blocks:
                gtxt = gb.get_text(" ", strip=True)
                gtxt_l = gtxt.lower()
                if any(k in gtxt_l for k in ["final grade", "علامتك النهائية", "الدرجة النهائية", "أعلى درجة", "highest grade"]):
                    result["is_submitted"] = True
                    result["status_text"] = result["status_text"] or "تم التقييم"
                    if not result["grade_val"]:
                        pg = self._parse_quiz_grade_text(gtxt)
                        if pg.get("grade_val"):
                            result["grade_val"] = pg["grade_val"]
                            result["grade_max"] = pg.get("grade_max", "")
                            result["percentage"] = pg.get("percentage", "")
                if gb.get("id") == "feedback" or "quizgradefeedback" in " ".join(gb.get("class") or []):
                    if not result["feedback"] and len(gtxt) > 2:
                        result["feedback"] = gtxt[:300]

            # 3. Check Moodle 4.x/5.x completion badge on the quiz page safely (ignores View-only / partial)
            if not result["is_submitted"]:
                comp = soup.find(attrs={"data-region": "completion-info"}) or soup.find(class_=re.compile(r"activity-completion"))
                if comp and self._is_completion_region_complete(comp):
                    result["is_submitted"] = True
                    result["status_text"] = "مكتمل"

            # 4. Extract quiz close / due date from activity-dates or quizinfo
            act_dates = soup.find(attrs={"data-region": "activity-dates"}) or soup.find(class_=re.compile(r"activity-dates|quizinfo"))
            if act_dates:
                dates_txt = act_dates.get_text(" ", strip=True)
                m_close = re.search(
                    r"(?:Closes|Closed|Due|تاريخ الإغلاق|يغلق في|أغلق في|يغلق|أغلق|انتهاء)\s*:\s*(.+?)(?=\s*(?:Opened|Opens|Closes|Closed|Due|Attempts|Time limit|$))",
                    dates_txt,
                    re.I,
                )
                if m_close:
                    result["due_date"] = m_close.group(1).strip()
                else:
                    for el in act_dates.find_all(["div", "p", "span"]):
                        txt = el.get_text(" ", strip=True)
                        if any(k in txt.lower() for k in ["close", "closes", "closed", "يغلق", "إغلاق", "انتهاء", "due"]):
                            cleaned = re.sub(
                                r"^(?:this quiz (?:will close|closed|closes) (?:at|on)|closes|closed|due|هذا الاختبار (?:يغلق|أغلق) في|تاريخ الإغلاق|يغلق)\s*:?\s*",
                                "", txt, flags=re.I
                            ).strip()
                            if cleaned and len(cleaned) < 90:
                                result["due_date"] = cleaned
                                break

        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as e:
            logger.debug(f"Error checking quiz status at {url}: {e}")
        finally:
            _close_response(resp)
        return result

    @staticmethod
    def _read_bounded_html(resp, max_bytes: int = MAX_INTERSTITIAL_HTML_BYTES) -> Optional[str]:
        """Read an HTML response body up to max_bytes via streaming (avoids OOM)."""
        cl = LMSScraper._parse_content_length(resp)
        if cl is not None and cl > max_bytes:
            return None
        chunks: List[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def download_resource(
        self,
        url: str,
        max_mb: int = 40,
        raw_url: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Optional[tuple]:
        """Download a resource file from Moodle safely with streaming size cap, host allowlist, and filename sanitization.

        Returns:
            Tuple[str, bytes] -> (sanitized_filename, file_content_bytes) if successful, None otherwise.
        """
        resp = None
        try:
            candidates = []
            if raw_url and raw_url != url:
                candidates.append(raw_url)
            candidates.append(url)
            req_headers = {"Referer": referer} if referer else {"Referer": f"{LMS_BASE_URL}/my/"}

            for cand in candidates:
                r = self.authenticated_get(
                    cand, timeout=30, stream=True, allow_redirects=True, headers=req_headers
                )
                if r.status_code == 200:
                    resp = r
                    break
                logger.debug(f"Resource candidate returned {r.status_code} for {cand}")
                _close_response(r)

            if resp is None:
                logger.warning(f"Resource download failed for {url}")
                return None

            content_type = self._response_content_type(resp)
            content_disp = resp.headers.get("Content-Disposition", "") if hasattr(resp, "headers") else ""

            # If Moodle returned an HTML interstitial page instead of a direct file, read bounded HTML & follow embedded file link
            if "text/html" in content_type and not content_disp:
                html_text = self._read_bounded_html(resp, max_bytes=MAX_INTERSTITIAL_HTML_BYTES)
                _close_response(resp)
                resp = None
                if not html_text:
                    return None
                if self._is_waf_challenge_text(html_text):
                    self.http.trigger_waf_cooldown()
                    logger.warning("WAF CAPTCHA challenge on resource interstitial page")
                    return None
                if self._is_login_html(html_text):
                    logger.info("Login page returned on resource interstitial page")
                    return None

                soup = BeautifulSoup(html_text, "lxml")
                file_link = (
                    soup.find("a", href=re.compile(r"/pluginfile\.php/|/mod_resource/content/")) or
                    soup.find("object", data=re.compile(r"/pluginfile\.php/|/mod_resource/content/")) or
                    soup.find("iframe", src=re.compile(r"/pluginfile\.php/|/mod_resource/content/"))
                )
                if not file_link:
                    return None

                target_href = file_link.get("href") or file_link.get("data") or file_link.get("src")
                if not target_href:
                    return None

                if not target_href.startswith("http"):
                    target_href = urljoin(LMS_BASE_URL + "/", target_href)

                resp = self.authenticated_get(target_href, timeout=30, stream=True, allow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(f"Embedded resource returned HTTP {resp.status_code} for {target_href}")
                    return None
                content_type = self._response_content_type(resp)
                content_disp = resp.headers.get("Content-Disposition", "") if hasattr(resp, "headers") else ""
                if "text/html" in content_type and not content_disp:
                    logger.warning(f"Embedded resource returned HTML instead of binary for {target_href}")
                    return None

            filename = ""
            if content_disp and "filename" in content_disp.lower():
                fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', content_disp, re.I)
                if fn_match:
                    filename = unquote(fn_match.group(1).strip('"\''))

            if not filename:
                clean_path = (getattr(resp, "url", "") or "").split("?")[0].rstrip("/")
                candidate = unquote(clean_path.split("/")[-1])
                if "." in candidate and len(candidate) < 120:
                    filename = candidate
                else:
                    filename = "document"

            filename = sanitize_filename(filename, default="document")

            max_bytes = max(1, int(max_mb)) * 1024 * 1024
            cl_int = self._parse_content_length(resp)
            if cl_int is not None and cl_int > max_bytes:
                logger.info(f"Resource {filename} exceeds size limit: {cl_int} bytes")
                return None

            chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    total += len(chunk)
                    if total > max_bytes:
                        logger.info(f"Resource {filename} exceeded {max_mb}MB while downloading")
                        return None
                    chunks.append(chunk)

            file_bytes = b"".join(chunks)

            if "." not in filename:
                if "pdf" in content_type:
                    filename += ".pdf"
                elif "presentation" in content_type or "powerpoint" in content_type:
                    filename += ".pptx"
                elif "word" in content_type or "document" in content_type:
                    filename += ".docx"
                elif "zip" in content_type:
                    filename += ".zip"
                elif "image/jpeg" in content_type:
                    filename += ".jpg"
                elif "image/png" in content_type:
                    filename += ".png"

            filename = sanitize_filename(filename, default="document")
            return filename, file_bytes
        except Exception as e:
            logger.warning(f"Error downloading resource {url}: {e}")
            return None
        finally:
            _close_response(resp)

    def get_course_grades(self, course_id: int, *, raise_auth_errors: bool = False) -> List[Dict[str, Any]]:
        """Scrape Moodle User Grade Report (/grade/report/user/index.php?id=course_id)."""
        grades = []
        url = f"{LMS_BASE_URL}/grade/report/user/index.php?id={course_id}"
        resp = None
        try:
            resp = self.authenticated_get(url, timeout=25)
            if resp.status_code != 200:
                return grades
            soup = BeautifulSoup(resp.text, "lxml")

            table = (
                soup.find("table", class_=re.compile(r"user-grade|generaltable")) or
                soup.find("table")
            )
            if not table:
                return grades

            all_rows = table.find_all("tr")
            name_col, grade_col, range_col, pct_col, fb_col = None, None, None, None, None
            if all_rows:
                for idx, hc in enumerate(all_rows[0].find_all(["th", "td"])):
                    htxt = hc.get_text(strip=True).lower()
                    if any(k in htxt for k in ["grade item", "البند", "عنصر التقييم", "التقييم", "النشاط"]):
                        name_col = idx
                    elif any(k in htxt for k in ["grade", "الدرجة", "العلامة"]) and "item" not in htxt:
                        grade_col = idx
                    elif any(k in htxt for k in ["range", "المدى", "النطاق"]):
                        range_col = idx
                    elif any(k in htxt for k in ["percentage", "النسبة", "%"]):
                        pct_col = idx
                    elif any(k in htxt for k in ["feedback", "ملاحظات", "التغذية", "إفادة"]):
                        fb_col = idx

            seen_items = set()
            for row in all_rows[1:]:
                name_cell = (
                    row.find(["th", "td"], class_=re.compile(r"column-itemname")) or
                    row.find(["th", "td"], attrs={"headers": re.compile(r"itemname")})
                )
                grade_cell = (
                    row.find("td", class_=re.compile(r"column-grade")) or
                    row.find("td", attrs={"headers": re.compile(r"\bgrade\b")})
                )
                range_cell = (
                    row.find("td", class_=re.compile(r"column-range")) or
                    row.find("td", attrs={"headers": re.compile(r"\brange\b")})
                )
                pct_cell = (
                    row.find("td", class_=re.compile(r"column-percentage")) or
                    row.find("td", attrs={"headers": re.compile(r"percentage")})
                )
                fb_cell = (
                    row.find("td", class_=re.compile(r"column-feedback")) or
                    row.find("td", attrs={"headers": re.compile(r"feedback")})
                )

                cells = row.find_all(["th", "td"])
                if not name_cell and name_col is not None and name_col < len(cells):
                    name_cell = cells[name_col]
                if not grade_cell and grade_col is not None and grade_col < len(cells):
                    grade_cell = cells[grade_col]
                if not range_cell and range_col is not None and range_col < len(cells):
                    range_cell = cells[range_col]
                if not pct_cell and pct_col is not None and pct_col < len(cells):
                    pct_cell = cells[pct_col]
                if not fb_cell and fb_col is not None and fb_col < len(cells):
                    fb_cell = cells[fb_col]

                if not name_cell or not grade_cell:
                    continue

                link = name_cell.find("a", href=True)
                item_url = ""
                item_id = ""
                if link:
                    href = self._clean_url(link.get("href", ""))
                    item_url = href
                    m = re.search(r"id=(\d+)", href)
                    if m:
                        item_id = f"mod_{m.group(1)}"

                # Extract clean item name without Moodle category prefix labels
                title_el = name_cell.find(class_="gradeitemheader") or link or name_cell
                raw_name = self._clean_link_text(title_el)
                raw_name = re.sub(
                    r"^(?:البند|عنصر التقييم|الفئة|العلامة المحسوبة|Calculated grade|Category|Grade item)\s*:?\s*",
                    "", raw_name, flags=re.I
                ).strip()
                if not raw_name or len(raw_name) < 2:
                    continue

                # Skip Attendance items in Grades (tracked in dedicated Attendance section)
                if "/mod/attendance/" in item_url or raw_name.strip().lower() in (
                    "attendance", "الحضور والغياب", "الحضور", "سجل الحضور"
                ):
                    continue

                for menu_el in grade_cell.find_all(class_=re.compile(r"action-menu|dropdown")):
                    menu_el.decompose()
                grade_val = grade_cell.get_text(" ", strip=True)
                grade_val = re.sub(
                    r"\b(?:Actions|Grade analysis|الإجراءات|تحليل الدرجة)\b",
                    "",
                    grade_val,
                    flags=re.I,
                )
                grade_val = re.sub(r"\s+", " ", grade_val).strip()
                if grade_val in ("-", "—", "Empty", "فارغ") or not any(ch.isalnum() for ch in grade_val):
                    grade_val = ""

                grade_max = range_cell.get_text(" ", strip=True) if range_cell else ""
                percentage = pct_cell.get_text(" ", strip=True) if pct_cell else ""
                if percentage in ("-", "—"):
                    percentage = ""
                feedback = fb_cell.get_text(" ", strip=True) if fb_cell else ""
                if feedback in ("-", "—"):
                    feedback = ""

                # Skip empty category headers that have no link and no grade
                if not item_url and not grade_val:
                    continue

                if not item_id:
                    slug = re.sub(r"\W+", "_", raw_name.lower()).strip("_")[:40]
                    item_id = f"g_{course_id}_{slug}"

                if item_id in seen_items:
                    continue
                seen_items.add(item_id)

                grades.append({
                    "item_id": item_id,
                    "item_name": raw_name,
                    "grade_val": grade_val,
                    "grade_max": grade_max,
                    "percentage": percentage,
                    "feedback": feedback,
                    "url": item_url or url,
                    "course_id": course_id,
                })

            logger.debug(f"المادة {course_id} | grades: {len(grades)} عنصر تقييم")
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as e:
            logger.warning(f"خطأ في جلب العلامات للمادة {course_id}: {e}")
        finally:
            _close_response(resp)
        return grades

    def get_course_announcements(
        self,
        course_id: int,
        course_soup: Optional[BeautifulSoup] = None,
        *,
        raise_auth_errors: bool = False,
    ) -> List[Dict[str, Any]]:
        """Scrape forum discussions / announcements for a course."""
        announcements = []
        try:
            forum_links = []
            seen_forums = set()

            if course_soup is not None:
                for a in course_soup.find_all("a", href=re.compile(r"/mod/forum/view\.php\?id=\d+")):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = urljoin(LMS_BASE_URL + "/", href)
                    m = re.search(r"id=(\d+)", href)
                    if not m or m.group(1) in seen_forums:
                        continue
                    seen_forums.add(m.group(1))
                    fname = self._clean_link_text(a) or "منتدى الإعلانات"
                    forum_links.append((href, fname))

            if not forum_links:
                idx_url = f"{LMS_BASE_URL}/mod/forum/index.php?id={course_id}"
                resp = None
                try:
                    resp = self.authenticated_get(idx_url, timeout=20)
                    if resp.status_code == 200:
                        idx_soup = BeautifulSoup(resp.text, "lxml")
                        for a in idx_soup.find_all("a", href=re.compile(r"/mod/forum/view\.php\?id=\d+")):
                            href = a.get("href", "")
                            if not href.startswith("http"):
                                href = urljoin(LMS_BASE_URL + "/", href)
                            m = re.search(r"id=(\d+)", href)
                            if not m or m.group(1) in seen_forums:
                                continue
                            seen_forums.add(m.group(1))
                            fname = self._clean_link_text(a) or "منتدى الإعلانات"
                            forum_links.append((href, fname))
                finally:
                    _close_response(resp)

            seen_discuss = set()
            for f_url, f_name in forum_links[:4]:
                f_resp = None
                try:
                    f_resp = self.authenticated_get(f_url, timeout=20)
                    if f_resp.status_code != 200:
                        continue
                    f_soup = BeautifulSoup(f_resp.text, "lxml")

                    # Check discussion table rows first
                    for tr in f_soup.find_all("tr", class_=re.compile(r"discussion")):
                        d_link = tr.find("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+"))
                        if not d_link:
                            continue
                        d_href = self._clean_url(d_link.get("href", ""))
                        dm = re.search(r"d=(\d+)", d_href)
                        if not dm:
                            continue
                        discuss_id = f"discuss_{dm.group(1)}"
                        if discuss_id in seen_discuss:
                            continue
                        seen_discuss.add(discuss_id)

                        title = self._clean_link_text(d_link)
                        author_el = tr.find(class_=re.compile(r"author"))
                        author = ""
                        posted_date = ""
                        if author_el:
                            a_text = author_el.get_text(" ", strip=True)
                            author = a_text
                        time_el = tr.find("time")
                        if time_el:
                            posted_date = time_el.get_text(strip=True)

                        announcements.append({
                            "discuss_id": discuss_id,
                            "forum_name": f_name,
                            "title": title or f"إعلان #{dm.group(1)}",
                            "author": author,
                            "posted_date": posted_date,
                            "url": d_href,
                            "course_id": course_id,
                        })

                    # Fallback for any discuss.php links outside tr.discussion
                    for d_link in f_soup.find_all("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+")):
                        raw_d = d_link.get("href", "")
                        if "#" in raw_d:
                            continue
                        d_href = self._clean_url(raw_d)
                        dm = re.search(r"d=(\d+)", d_href)
                        if not dm:
                            continue
                        discuss_id = f"discuss_{dm.group(1)}"
                        if discuss_id in seen_discuss:
                            continue
                        title = self._clean_link_text(d_link)
                        if not title or len(title) < 2:
                            continue
                        seen_discuss.add(discuss_id)
                        announcements.append({
                            "discuss_id": discuss_id,
                            "forum_name": f_name,
                            "title": title,
                            "author": "",
                            "posted_date": "",
                            "url": d_href,
                            "course_id": course_id,
                        })
                except (LMSAuthError, LMSWafError):
                    if raise_auth_errors:
                        raise
                    break
                except Exception as fe:
                    logger.debug(f"Forum fetch error ({f_url}): {fe}")
                finally:
                    _close_response(f_resp)

            logger.debug(f"المادة {course_id} | announcements: {len(announcements)} إعلان")
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as e:
            logger.warning(f"خطأ في جلب الإعلانات للمادة {course_id}: {e}")
        return announcements

    def get_announcement_details(self, discuss_url: str) -> Dict[str, str]:
        """Fetch the body text, author, and date of a specific forum discussion post."""
        info = {"body": "", "author": "", "date": ""}
        resp = None
        try:
            resp = self.authenticated_get(discuss_url, timeout=20)
            if resp.status_code != 200:
                return info
            soup = BeautifulSoup(resp.text, "lxml")

            post_body = (
                soup.find(class_=re.compile(r"post-content-container|posting|fullpost")) or
                soup.find(attrs={"data-region-content": "forum-post-core"})
            )
            if post_body:
                text = post_body.get_text("\n", strip=True)
                info["body"] = re.sub(r"\n{3,}", "\n\n", text).strip()[:900]

            author_el = soup.find("a", href=re.compile(r"/user/view\.php\?"))
            if author_el:
                info["author"] = author_el.get_text(strip=True)

            time_el = soup.find("time")
            if time_el:
                info["date"] = time_el.get_text(strip=True)
        except Exception as e:
            logger.debug(f"Error fetching announcement details {discuss_url}: {e}")
        finally:
            _close_response(resp)
        return info

    def get_course_attendance(
        self,
        course_id: int,
        course_soup: Optional[BeautifulSoup] = None,
        *,
        raise_auth_errors: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Discover and scrape Moodle Attendance (/mod/attendance/view.php?id=...)
        for a given course, returning session records and summary statistics.
        """
        attendance_records: List[Dict[str, Any]] = []
        try:
            soup = course_soup
            if soup is None:
                resp = None
                try:
                    resp = self.authenticated_get(
                        f"{LMS_BASE_URL}/course/view.php", params={"id": course_id}, timeout=20
                    )
                    if resp.status_code != 200 or "/login/" in (getattr(resp, "url", "") or ""):
                        return attendance_records
                    soup = BeautifulSoup(resp.text, "lxml")
                finally:
                    _close_response(resp)

            att_links: List[Tuple[str, str, str]] = []
            seen_cmids = set()
            for a in soup.find_all("a", href=re.compile(r"/mod/attendance/view\.php\?id=\d+")):
                clean_href = self._clean_url(a.get("href", ""))
                m = re.search(r"id=(\d+)", clean_href)
                if not m:
                    continue
                cmid = m.group(1)
                if cmid in seen_cmids:
                    continue
                seen_cmids.add(cmid)
                title = self._clean_link_text(a) or "Attendance"
                att_links.append((cmid, title, clean_href))

            for cmid, title, base_url in att_links:
                r = None
                try:
                    # view=5 forces Moodle Attendance to display all sessions in the semester
                    full_url = f"{LMS_BASE_URL}/mod/attendance/view.php?id={cmid}&view=5"
                    r = self.authenticated_get(full_url, timeout=20)
                    if r.status_code != 200 or "/login/" in (getattr(r, "url", "") or ""):
                        continue
                    att_soup = BeautifulSoup(r.text, "lxml")
                    tables = att_soup.find_all("table")

                    sessions: List[Dict[str, str]] = []
                    present_count = 0
                    absent_count = 0
                    late_count = 0
                    excused_count = 0

                    taken_sessions = 0
                    total_sessions = 0
                    points_str = ""
                    percentage_str = ""

                    for tbl in tables:
                        headers = [th.get_text(" ", strip=True).lower() for th in tbl.find_all("th")]
                        # Session list table has Date & Status columns
                        if any("date" in h or "التاريخ" in h for h in headers) and any(
                            "status" in h or "الحالة" in h or "الوضع" in h for h in headers
                        ):
                            for tr in tbl.find_all("tr"):
                                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                                if len(tds) < 3:
                                    continue
                                s_date = re.sub(r"\s+", " ", tds[0]).strip()
                                s_desc = re.sub(r"\s+", " ", tds[1]).strip() if len(tds) > 1 else ""
                                s_status = re.sub(r"\s+", " ", tds[2]).strip() if len(tds) > 2 else ""
                                s_points = re.sub(r"\s+", " ", tds[3]).strip() if len(tds) > 3 else ""
                                s_remarks = re.sub(r"\s+", " ", tds[4]).strip() if len(tds) > 4 else ""

                                # Skip unrecorded future sessions (where status is '?' or empty)
                                if not s_status or s_status in ("?", "-", "—") or s_status.startswith("?"):
                                    continue
                                # Skip "Noiske" or empty table message rows
                                if "no " in s_date.lower() and len(tds) < 4:
                                    continue

                                st_low = s_status.lower()
                                if any(k in st_low for k in ("present", "حاضر", "موجود")):
                                    present_count += 1
                                elif any(k in st_low for k in ("absent", "غائب", "غياب")):
                                    absent_count += 1
                                elif any(k in st_low for k in ("late", "متأخر", "تأخير")):
                                    late_count += 1
                                elif any(k in st_low for k in ("excused", "معذور", "عذر")):
                                    excused_count += 1

                                sessions.append({
                                    "date": s_date,
                                    "description": s_desc,
                                    "status": s_status,
                                    "points": s_points,
                                    "remarks": s_remarks,
                                })
                        else:
                            # Summary key-value table
                            for tr in tbl.find_all("tr"):
                                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                                if len(cells) < 2:
                                    continue
                                k = cells[0].lower()
                                v = cells[1].strip()
                                if "percentage over taken" in k or "النسبة المئوية" in k or "نسبة الحضور" in k:
                                    percentage_str = v
                                elif "points over taken" in k or "النقاط على" in k:
                                    points_str = v
                                elif "taken sessions" in k or "الجلسات المأخوذة" in k or "المحاضرات المنعقدة" in k:
                                    m_num = re.search(r"(\d+)", v)
                                    if m_num:
                                        taken_sessions = int(m_num.group(1))
                                elif "total number of sessions" in k or "مجموع الجلسات" in k or "إجمالي الجلسات" in k:
                                    m_num = re.search(r"(\d+)", v)
                                    if m_num:
                                        total_sessions = int(m_num.group(1))

                    if taken_sessions == 0 and sessions:
                        taken_sessions = len(sessions)
                    if total_sessions < taken_sessions:
                        total_sessions = taken_sessions

                    attendance_records.append({
                        "att_id": f"att_{cmid}",
                        "title": title,
                        "taken_sessions": taken_sessions,
                        "total_sessions": total_sessions,
                        "present_count": present_count,
                        "absent_count": absent_count,
                        "late_count": late_count,
                        "excused_count": excused_count,
                        "points_str": points_str,
                        "percentage_str": percentage_str,
                        "sessions": sessions,
                        "url": base_url,
                        "course_id": course_id,
                    })
                except (LMSAuthError, LMSWafError):
                    if raise_auth_errors:
                        raise
                    break
                except Exception as ae:
                    logger.debug(f"Attendance fetch error ({base_url}): {ae}")
                finally:
                    _close_response(r)

            logger.debug(f"المادة {course_id} | attendance: {len(attendance_records)} سجل")
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as e:
            logger.warning(f"خطأ في جلب الحضور والغياب للمادة {course_id}: {e}")
        return attendance_records
