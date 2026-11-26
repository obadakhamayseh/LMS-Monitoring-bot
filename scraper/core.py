"""Core LMSScraper class composed from scraper.client, dates, parsers, attendance, and quizzes."""

from __future__ import annotations

import logging
import re
import sys
import threading
from typing import Any
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from config import LMS_BASE_URL, LMS_LOGIN_URL
from scraper.attendance import (
    build_attendance_col_map,
    extract_attendance_links,
    parse_attendance_page_soup,
    parse_attendance_status_category,
)
from scraper.client import (
    MAX_HTML_INSPECT_BYTES,
    MAX_INTERSTITIAL_HTML_BYTES,
    FetchResult,
    LMSAuthError,
    LMSHttpClient,
    LMSWafError,
    _assert_allowed_url,
    _close_response,
    clean_url,
    is_waf_challenge,
    is_waf_challenge_text,
    is_waf_response,
    parse_content_length,
    read_bounded_html,
    response_content_type,
    sanitize_filename,
)
from scraper.dates import (
    extract_course_page_activity_dates,
    looks_like_date,
    normalize_due_date_year,
)
from scraper.parsers import (
    _PENDING_COMPLETION_CLASS_RE,
    _SUCCESS_COMPLETION_CLASS_RE,
    _VIEW_ONLY_COMPLETION_RE,
    clean_link_text,
    extract_course_name,
    extract_course_page_completions,
    extract_courses_from_soup,
    extract_forum_links,
    extract_moodle_cfg,
    is_completion_region_complete,
    is_login_html,
    is_login_soup,
    parse_activities_index_soup,
    parse_announcement_details_soup,
    parse_assignment_status_soup,
    parse_forum_announcements_soup,
    parse_grades_soup,
    parse_resources_from_course_soup,
)
from scraper.quizzes import parse_quiz_grade_text, parse_quiz_status_soup

logger = logging.getLogger(__name__)


def _parse_html(html_text: str, parser: str = "lxml") -> BeautifulSoup:
    """Build a BeautifulSoup tree while honoring monkeypatches on `lms_scraper.BeautifulSoup`."""
    facade = sys.modules.get("lms_scraper")
    bs_cls = getattr(facade, "BeautifulSoup", BeautifulSoup) if facade is not None else BeautifulSoup
    return bs_cls(html_text, parser)


class LMSScraper:
    """High-level Moodle 4.x/5.x scraper composed from modular client & parser helpers."""

    _VIEW_ONLY_COMPLETION_RE = _VIEW_ONLY_COMPLETION_RE
    _PENDING_COMPLETION_CLASS_RE = _PENDING_COMPLETION_CLASS_RE
    _SUCCESS_COMPLETION_CLASS_RE = _SUCCESS_COMPLETION_CLASS_RE

    def __init__(self, username: str, password: str, http_client: LMSHttpClient | None = None):
        self.username = username
        self.password = password
        self.http = http_client or LMSHttpClient()
        self.session = self.http.session
        self._state_lock = threading.RLock()
        self._tls = threading.local()
        self.logged_in = False
        self.user_id: str | None = None
        self.full_name: str | None = None
        self.sesskey: str | None = None
        self.moodle_theme: str | None = None
        self.error_message: str | None = None
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

    # ── Static/class helpers delegating to modular helpers (backward-compatible) ──

    @staticmethod
    def _clean_url(url: str) -> str:
        return clean_url(url)

    @staticmethod
    def _is_waf_challenge_text(text: str) -> bool:
        return is_waf_challenge_text(text)

    @staticmethod
    def _is_waf_challenge(resp: Any) -> bool:
        return is_waf_challenge(resp)

    @classmethod
    def _is_waf_response(cls, resp: Any) -> bool:
        return is_waf_response(resp)

    @staticmethod
    def _extract_moodle_cfg(html_text: str) -> dict[str, Any]:
        return extract_moodle_cfg(html_text)

    @staticmethod
    def _is_login_soup(soup: BeautifulSoup | None) -> bool:
        return is_login_soup(soup)

    @classmethod
    def _is_login_html(cls, html_text: str) -> bool:
        return is_login_html(html_text, soup_factory=_parse_html)

    @staticmethod
    def _soup_from_response(resp: Any) -> BeautifulSoup:
        """Parse an HTTP response's HTML with BeautifulSoup at most once and cache on the response."""
        cached = getattr(resp, "_parsed_soup", None)
        if cached is not None:
            return cached
        soup = _parse_html(resp.text, "lxml")
        try:
            setattr(resp, "_parsed_soup", soup)
        except Exception:
            pass
        return soup

    def _is_login_response(self, resp: Any) -> bool:
        if "/login/index.php" in (getattr(resp, "url", "") or "").lower():
            return True
        if not (getattr(resp, "text", None) or ""):
            return False
        return self._is_login_soup(self._soup_from_response(resp))

    @staticmethod
    def _response_content_type(resp: Any) -> str:
        return response_content_type(resp)

    @staticmethod
    def _parse_content_length(resp: Any) -> int | None:
        return parse_content_length(resp)

    @staticmethod
    def _clean_link_text(el: Any) -> str:
        return clean_link_text(el)

    def _extract_course_name(self, link_el: Any) -> str:
        return extract_course_name(link_el)

    @staticmethod
    def _normalize_due_date_year(due_str: str) -> str:
        return normalize_due_date_year(due_str)

    @staticmethod
    def _extract_course_page_activity_dates(soup: BeautifulSoup | None) -> dict[str, str]:
        return extract_course_page_activity_dates(soup)

    @classmethod
    def _is_completion_region_complete(cls, comp_region: Any) -> bool:
        return is_completion_region_complete(comp_region)

    @staticmethod
    def _extract_course_page_completions(soup: BeautifulSoup | None) -> set[str]:
        return extract_course_page_completions(soup)

    @staticmethod
    def _looks_like_date(text: str) -> bool:
        return looks_like_date(text)

    @staticmethod
    def _parse_quiz_grade_text(raw_text: str) -> dict[str, str]:
        return parse_quiz_grade_text(raw_text)

    @staticmethod
    def _read_bounded_html(resp: Any, max_bytes: int = MAX_INTERSTITIAL_HTML_BYTES) -> str | None:
        return read_bounded_html(resp, max_bytes=max_bytes)

    @staticmethod
    def _parse_attendance_status_category(status_text: str) -> str | None:
        return parse_attendance_status_category(status_text)

    @staticmethod
    def _build_attendance_col_map(tbl: Any) -> tuple[Any | None, dict[str, int]]:
        return build_attendance_col_map(tbl)

    # ── Authenticated HTTP & session lifecycle ──

    def authenticated_get(self, url: str, timeout: int = 30, stream: bool = False, **kwargs: Any) -> Any:
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

        if self._is_waf_response(resp):
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

    def authenticated_post(self, url: str, timeout: int = 30, **kwargs: Any) -> Any:
        """Send an authenticated POST (e.g. Moodle AJAX) through LMSHttpClient with WAF & auth checks."""
        suppress = self._suppress_legacy_state()
        if (not suppress and self.waf_blocked) or self.http.waf_cooldown_active:
            if not suppress:
                with self._state_lock:
                    self.last_fetch_ok = False
            raise LMSWafError("جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ تم إيقاف الفحص مؤقتاً لتجنب الحظر")

        _assert_allowed_url(url)
        resp = self.http.post(url, timeout=timeout, **kwargs)

        final_url = getattr(resp, "url", None)
        if isinstance(final_url, str) and final_url:
            try:
                _assert_allowed_url(final_url)
            except ValueError:
                _close_response(resp)
                raise

        if "/login/index.php" in (final_url or "").lower():
            _close_response(resp)
            logger.info("LMS session expired on POST (login redirect); waiting for re-authentication")
            if not suppress:
                with self._state_lock:
                    self.logged_in = False
                    self.session_expired = True
                    self.last_fetch_ok = False
            raise LMSAuthError("انتهت جلسة LMS؛ يرجى تسجيل الدخول مجدداً من البوت")

        if self._is_waf_response(resp):
            _close_response(resp)
            logger.warning("University WAF CAPTCHA challenge encountered on POST/AJAX; backing off immediately")
            self.http.trigger_waf_cooldown()
            if not suppress:
                with self._state_lock:
                    self.waf_blocked = True
                    self.last_fetch_ok = False
            raise LMSWafError("جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA)؛ تم التوقف مؤقتاً")

        ctype = self._response_content_type(resp)
        if ("text/html" in ctype or "application/xhtml" in ctype) and self._is_login_response(resp):
            _close_response(resp)
            logger.info("LMS session expired on POST; waiting for re-authentication")
            if not suppress:
                with self._state_lock:
                    self.logged_in = False
                    self.session_expired = True
                    self.last_fetch_ok = False
            raise LMSAuthError("انتهت جلسة LMS؛ يرجى تسجيل الدخول مجدداً من البوت")

        return resp

    def keep_session_alive(self) -> bool:
        """Make a lightweight authenticated request to keep an active monitor session warm."""
        resp = None
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=15)
            resp.raise_for_status()
            return self.logged_in
        except Exception as exc:
            with self._state_lock:
                self.last_fetch_ok = False
            logger.warning("LMS keepalive failed: %s", exc)
            return False
        finally:
            _close_response(resp)

    def logout(self) -> None:
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
        except Exception as exc:
            logger.debug("LMS logout request error: %s", exc)
        finally:
            _close_response(resp)
            with self._state_lock:
                self.logged_in = False
                self.sesskey = None
            self.http.clear_cookies()
            self.http.close()

    def close(self) -> None:
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
            logger.info("محاولة تسجيل الدخول: %s", self.username)

            resp = self.http.get(LMS_LOGIN_URL, timeout=30, allow_waf_cooldown=True)
            try:
                if resp.status_code != 200:
                    self.error_message = f"موقع الجامعة غير متاح (رمز {resp.status_code})"
                    logger.error(self.error_message)
                    return False

                if self._is_waf_challenge(resp):
                    self.http.trigger_waf_cooldown()
                    self.waf_blocked = True
                    self.error_message = (
                        "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) مؤقتاً؛ حاول مجدداً بعد عدة دقائق"
                    )
                    logger.warning("WAF CAPTCHA challenge on login GET for %s", self.username)
                    return False

                login_get_text = resp.text
            finally:
                _close_response(resp)

            soup = _parse_html(login_get_text, "lxml")

            # If session cookies already keep us logged in
            if self._check_logged_in(soup, login_get_text):
                self.logged_in = True
                self._extract_user_info(soup, login_get_text)
                logger.info("✅ جلسة نشطة بالفعل - %s", self.full_name)
                return True

            # Preserve ALL hidden/default inputs (logintoken, anchor, NetScaler __ncforminfo, etc.)
            login_form = soup.find("form", id="login") or soup.find(
                "form", action=re.compile(r"/login/index\.php", re.I)
            )
            post_url = LMS_LOGIN_URL
            payload: dict[str, str] = {}
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
                    self.error_message = (
                        "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) بعد محاولة الدخول؛ حاول بعد عدة دقائق"
                    )
                    logger.warning("WAF CAPTCHA challenge on login POST for %s", self.username)
                    return False

                post_text = post_resp.text
            finally:
                _close_response(post_resp)

            soup = _parse_html(post_text, "lxml")

            if self._check_logged_in(soup, post_text):
                self.logged_in = True
                self._extract_user_info(soup, post_text)
                logger.info(
                    "✅ تسجيل الدخول ناجح - %s (uid=%s, theme=%s)",
                    self.full_name,
                    self.user_id,
                    self.moodle_theme,
                )
                return True

            my_resp = None
            try:
                my_resp = self.http.get(f"{LMS_BASE_URL}/my/", timeout=15, allow_waf_cooldown=True)
                if (
                    my_resp.status_code == 200
                    and not self._is_waf_challenge(my_resp)
                    and "login" not in (my_resp.url or "")
                ):
                    my_text = my_resp.text
                    my_soup = _parse_html(my_text, "lxml")
                    if self._check_logged_in(my_soup, my_text):
                        self.logged_in = True
                        self._extract_user_info(my_soup, my_text)
                        logger.info("✅ تسجيل الدخول ناجح عبر /my/ - %s", self.full_name)
                        return True
            except Exception as my_err:
                logger.debug("Login fallback /my/ check failed: %s", my_err)
            finally:
                _close_response(my_resp)

            err = soup.find(class_=["loginerrors", "alert-danger", "error"]) or soup.find(id="loginerrormessage")
            if err:
                msg = err.get_text(" ", strip=True)
                msg_low = msg.lower()
                if any(
                    k in msg_low
                    for k in (
                        "invalid login",
                        "incorrect",
                        "غير صحيحة",
                        "غير صحيح",
                        "خطأ في اسم المستخدم",
                        "wrong password",
                    )
                ):
                    self.credentials_invalid = True
            else:
                title = soup.title.get_text(strip=True) if soup.title else ""
                msg = f"بيانات غير صحيحة أو تم حظر الجلسة (الصفحة: {title})"
            self.error_message = msg
            logger.error("❌ فشل تسجيل الدخول: %s | URL: %s", msg, post_url_final)
            return False

        except requests.exceptions.Timeout:
            self.error_message = "انتهت مهلة الاتصال بموقع الجامعة (Timeout)"
            logger.error(self.error_message)
            return False
        except requests.RequestException as exc:
            self.error_message = f"خطأ في الاتصال بالشبكة: {exc}"
            logger.error(self.error_message)
            return False
        except Exception as exc:
            self.error_message = f"خطأ غير متوقع: {exc}"
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

    def _extract_sesskey(self, html_or_soup: Any) -> str | None:
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

    def _extract_user_info(self, soup: BeautifulSoup, html_text: str = "") -> None:
        self._extract_sesskey(html_text or soup)
        body = soup.find("body")
        if body and not self.user_id:
            uid = body.get("data-userid")
            if uid and str(uid) != "0":
                self.user_id = str(uid)

        excluded_labels = {
            "user menu",
            "language selector",
            "قائمة المستخدم",
            "الملف الشخصي",
            "profile",
            "dashboard",
            "لوحة التحكم",
            "الصفحة الرئيسية",
            "home",
        }
        raw_name = None

        for selector in (
            {"class": "usertext"},
            {"class": "username"},
            {"data-region": "user-name"},
        ):
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

        if not raw_name or len(raw_name.strip()) <= 3:
            resp = None
            try:
                resp = self.http.get(f"{LMS_BASE_URL}/user/profile.php", timeout=15)
                if resp.status_code == 200 and not self._is_waf_challenge(resp):
                    prof_soup = _parse_html(resp.text, "lxml")
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
            except Exception as exc:
                logger.debug("فشل جلب الملف الشخصي للاسم: %s", exc)
            finally:
                _close_response(resp)

        if raw_name:
            cleaned = re.sub(r"\s*\d{5,}\s*$", "", raw_name).strip()
            cleaned = re.sub(r"^\s*\d{5,}\s*", "", cleaned).strip()
            self.full_name = cleaned if len(cleaned) > 2 else raw_name.strip()

    # ── Courses & content fetching ──

    def _extract_courses_from_soup(
        self, soup: BeautifulSoup, courses: list[dict[str, Any]], seen_ids: set[int]
    ) -> None:
        extract_courses_from_soup(soup, courses, seen_ids)

    def get_courses(self, *, raise_auth_errors: bool = False) -> list[dict[str, Any]]:
        if not self.logged_in:
            logger.warning("لم يتم تسجيل الدخول")
            return []

        courses: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        # 1. Try /my/courses.php and extract sesskey
        resp = None
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/courses.php", timeout=30)
            soup = self._soup_from_response(resp)
            self._extract_sesskey(soup)
            self._extract_courses_from_soup(soup, courses, seen_ids)
        except (LMSWafError, LMSAuthError):
            if raise_auth_errors:
                raise
            return courses
        except Exception as exc:
            logger.error("خطأ في صفحة My Courses: %s", exc)
        finally:
            _close_response(resp)

        # 2. Moodle 4.x/5.x AJAX WebService (routed through authenticated_post with WAF & auth checks)
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
                    ajax_resp = self.authenticated_post(ajax_url, json=payload, timeout=20)
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
                                courses.append({"id": cid, "name": cname, "url": view_url})
                            if courses:
                                break
                except (LMSWafError, LMSAuthError):
                    if raise_auth_errors:
                        raise
                    return courses
                except Exception as exc:
                    logger.debug("AJAX course fetch (%s) failed: %s", classification, exc)
                finally:
                    _close_response(ajax_resp)

        # 3. Fallback to /my/ Dashboard
        if not courses:
            resp = None
            try:
                resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=30)
                soup = self._soup_from_response(resp)
                self._extract_sesskey(soup)
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except (LMSWafError, LMSAuthError):
                if raise_auth_errors:
                    raise
                return courses
            except Exception as exc:
                logger.error("خطأ في Dashboard: %s", exc)
            finally:
                _close_response(resp)

        # 4. Fallback to User Profile page
        if not courses:
            resp = None
            try:
                prof_url = (
                    f"{LMS_BASE_URL}/user/profile.php?id={self.user_id}&showallcourses=1"
                    if self.user_id
                    else f"{LMS_BASE_URL}/user/profile.php?showallcourses=1"
                )
                resp = self.authenticated_get(prof_url, timeout=30)
                soup = self._soup_from_response(resp)
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except (LMSWafError, LMSAuthError):
                if raise_auth_errors:
                    raise
                return courses
            except Exception as exc:
                logger.error("خطأ في صفحة User Profile Courses: %s", exc)
            finally:
                _close_response(resp)

        logger.info("✅ تم جلب %d مادة", len(courses))
        return courses

    def fetch_course_content(
        self, course_id: int, course_soup: BeautifulSoup | None = None
    ) -> FetchResult:
        """Fetch course content and return structured state without mutating self."""
        result: dict[str, list[dict[str, Any]]] = {
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
            assign_status: dict[str, Any] = {"ok": True, "error": None}
            result["assignments"] = self._get_activities_from_index(
                course_id, "assign", raise_auth_errors=True, status_out=assign_status
            )
            assign_ok = bool(assign_status["ok"])

            quiz_status: dict[str, Any] = {"ok": True, "error": None}
            result["quizzes"] = self._get_activities_from_index(
                course_id, "quiz", raise_auth_errors=True, status_out=quiz_status
            )
            quiz_ok = bool(quiz_status["ok"])

            res_status: dict[str, Any] = {"ok": True, "error": None}
            resources, page_soup = self._get_resources_from_course_page(
                course_id,
                soup=course_soup,
                return_soup=True,
                raise_auth_errors=True,
                status_out=res_status,
            )
            res_ok = bool(res_status["ok"])
            result["resources"] = resources

            completed_cmids: set[str] = set()
            page_due_dates: dict[str, str] = {}
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

            result["grades"] = self.get_course_grades(course_id, raise_auth_errors=True)
            result["announcements"] = self.get_course_announcements(
                course_id, course_soup=page_soup, raise_auth_errors=True
            )
            result["attendance"] = self.get_course_attendance(
                course_id, course_soup=page_soup, raise_auth_errors=True
            )

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
                needs_grade_check = (
                    bool(q.get("is_submitted")) and cmid not in graded_cmids and not q.get("grade_val")
                )
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
                err = (
                    res_status.get("error")
                    or assign_status.get("error")
                    or quiz_status.get("error")
                    or "Incomplete LMS fetch"
                )
            return FetchResult(content=result, ok=ok, session_expired=False, waf_blocked=False, error=err)

        except LMSWafError as exc:
            return FetchResult(content=result, ok=False, session_expired=False, waf_blocked=True, error=str(exc))
        except LMSAuthError as exc:
            return FetchResult(content=result, ok=False, session_expired=True, waf_blocked=False, error=str(exc))
        except Exception as exc:
            logger.error("Unexpected error fetching course %s: %s", course_id, exc)
            return FetchResult(content=result, ok=False, session_expired=False, waf_blocked=False, error=str(exc))
        finally:
            self._tls.suppress_state_mutation = prev_suppress

    def get_course_content(
        self, course_id: int, course_soup: BeautifulSoup | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Deprecated backward-compatible wrapper around fetch_course_content()."""
        res = self.fetch_course_content(course_id, course_soup=course_soup)
        with self._state_lock:
            self.last_fetch_ok = res.ok
            self.session_expired = res.session_expired
            self.waf_blocked = res.waf_blocked
            if res.session_expired:
                self.logged_in = False
        return res.content

    def _get_activities_from_index(
        self,
        course_id: int,
        mod_type: str,
        *,
        raise_auth_errors: bool = False,
        status_out: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{LMS_BASE_URL}/mod/{mod_type}/index.php?id={course_id}"
        activities: list[dict[str, Any]] = []
        resp = None

        try:
            resp = self.authenticated_get(url, timeout=30)
            if resp.status_code == 404:
                return activities
            resp.raise_for_status()
            soup = self._soup_from_response(resp)
            activities = parse_activities_index_soup(
                soup, course_id, mod_type, parse_quiz_grade_fn=self._parse_quiz_grade_text
            )
            logger.debug("المادة %s | %s: %d نشاط", course_id, mod_type, len(activities))

        except (LMSAuthError, LMSWafError) as exc:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(exc)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            if raise_auth_errors:
                raise
        except Exception as exc:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(exc)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            logger.error("خطأ في جلب %s للمادة %s: %s", mod_type, course_id, exc)
        finally:
            _close_response(resp)

        return activities

    def _get_resources_from_course_page(
        self,
        course_id: int,
        soup: BeautifulSoup | None = None,
        return_soup: bool = False,
        *,
        raise_auth_errors: bool = False,
        status_out: dict[str, Any] | None = None,
    ) -> Any:
        resources: list[dict[str, Any]] = []
        url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
        resp = None

        try:
            if soup is None:
                resp = self.authenticated_get(url, timeout=30)
                resp.raise_for_status()
                soup = self._soup_from_response(resp)

            resources = parse_resources_from_course_soup(soup, course_id)
            logger.debug("المادة %s | resources: %d مورد", course_id, len(resources))

        except (LMSAuthError, LMSWafError) as exc:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(exc)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            if raise_auth_errors:
                raise
        except Exception as exc:
            if status_out is not None:
                status_out["ok"] = False
                status_out["error"] = str(exc)
            if not self._suppress_legacy_state():
                with self._state_lock:
                    self.last_fetch_ok = False
            logger.error("خطأ في جلب موارد المادة %s: %s", course_id, exc)
        finally:
            _close_response(resp)

        if return_soup:
            return resources, soup
        return resources

    def get_course_info(self, course_id: int) -> tuple[str | None, dict[str, Any]]:
        course_name = ""
        content: dict[str, Any] = {"assignments": [], "quizzes": [], "resources": []}
        resp = None

        try:
            url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
            resp = self.authenticated_get(url, timeout=30)

            if resp.status_code == 404 or "login" in (getattr(resp, "url", "") or ""):
                return None, content

            soup = self._soup_from_response(resp)
            _close_response(resp)
            resp = None

            title_el = (
                soup.find("h1")
                or soup.find(class_=re.compile(r"page-header-headings"))
                or soup.find("title")
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
        except Exception as exc:
            with self._state_lock:
                self.last_fetch_ok = False
            logger.error("get_course_info error for %s: %s", course_id, exc)
            return None, content
        finally:
            _close_response(resp)

        return course_name, content

    def get_assignment_due_date(self, assignment_url: str) -> str:
        resp = None
        try:
            resp = self.authenticated_get(assignment_url, timeout=20)
            soup = self._soup_from_response(resp)

            for row in soup.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    if any(k in label for k in ["due", "يُسلَّم", "نهائي", "deadline"]):
                        return cells[1].get_text(strip=True)

        except Exception as exc:
            logger.debug("Error fetching assignment due date at %s: %s", assignment_url, exc)
        finally:
            _close_response(resp)
        return "غير محدد"

    def get_assignment_status(self, url: str, *, raise_auth_errors: bool = False) -> dict[str, Any]:
        """Fetch detailed submission status for an individual assignment."""
        result: dict[str, Any] = {
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
            soup = self._soup_from_response(resp)
            result = parse_assignment_status_soup(soup)
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as exc:
            logger.warning("Error checking assignment status at %s: %s", url, exc)
        finally:
            _close_response(resp)
        return result

    def get_quiz_status(self, url: str, *, raise_auth_errors: bool = False) -> dict[str, Any]:
        """Fetch completion/attempt status, released grade, and close date for an individual quiz."""
        result: dict[str, Any] = {
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
            soup = self._soup_from_response(resp)
            result = parse_quiz_status_soup(soup)
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as exc:
            logger.debug("Error checking quiz status at %s: %s", url, exc)
        finally:
            _close_response(resp)
        return result

    def download_resource(
        self,
        url: str,
        max_mb: int = 40,
        raw_url: str | None = None,
        referer: str | None = None,
    ) -> tuple[str, bytes] | None:
        """Download a resource file from Moodle safely with streaming size cap, host allowlist, and filename sanitization."""
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
                logger.debug("Resource candidate returned %s for %s", r.status_code, cand)
                _close_response(r)

            if resp is None:
                logger.warning("Resource download failed for %s", url)
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
                soup = _parse_html(html_text, "lxml")
                if self._is_login_soup(soup):
                    logger.info("Login page returned on resource interstitial page")
                    return None
                file_link = (
                    soup.find("a", href=re.compile(r"/pluginfile\.php/|/mod_resource/content/"))
                    or soup.find("object", data=re.compile(r"/pluginfile\.php/|/mod_resource/content/"))
                    or soup.find("iframe", src=re.compile(r"/pluginfile\.php/|/mod_resource/content/"))
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
                    logger.warning("Embedded resource returned HTTP %s for %s", resp.status_code, target_href)
                    return None
                content_type = self._response_content_type(resp)
                content_disp = resp.headers.get("Content-Disposition", "") if hasattr(resp, "headers") else ""
                if "text/html" in content_type and not content_disp:
                    logger.warning("Embedded resource returned HTML instead of binary for %s", target_href)
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
                logger.info("Resource %s exceeds size limit: %d bytes", filename, cl_int)
                return None

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    total += len(chunk)
                    if total > max_bytes:
                        logger.info("Resource %s exceeded %sMB while downloading", filename, max_mb)
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
        except Exception as exc:
            logger.warning("Error downloading resource %s: %s", url, exc)
            return None
        finally:
            _close_response(resp)

    def get_course_grades(self, course_id: int, *, raise_auth_errors: bool = False) -> list[dict[str, Any]]:
        """Scrape Moodle User Grade Report (/grade/report/user/index.php?id=course_id)."""
        grades: list[dict[str, Any]] = []
        url = f"{LMS_BASE_URL}/grade/report/user/index.php?id={course_id}"
        resp = None
        try:
            resp = self.authenticated_get(url, timeout=25)
            if resp.status_code != 200:
                return grades
            soup = self._soup_from_response(resp)
            grades = parse_grades_soup(soup, course_id, fallback_url=url)
            logger.debug("المادة %s | grades: %d عنصر تقييم", course_id, len(grades))
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as exc:
            logger.warning("خطأ في جلب العلامات للمادة %s: %s", course_id, exc)
        finally:
            _close_response(resp)
        return grades

    def get_course_announcements(
        self,
        course_id: int,
        course_soup: BeautifulSoup | None = None,
        *,
        raise_auth_errors: bool = False,
    ) -> list[dict[str, Any]]:
        """Scrape forum discussions / announcements for a course."""
        announcements: list[dict[str, Any]] = []
        try:
            seen_forums: set[str] = set()
            forum_links: list[tuple[str, str]] = []

            if course_soup is not None:
                forum_links = extract_forum_links(course_soup, seen_forums=seen_forums)

            if not forum_links:
                idx_url = f"{LMS_BASE_URL}/mod/forum/index.php?id={course_id}"
                resp = None
                try:
                    resp = self.authenticated_get(idx_url, timeout=20)
                    if resp.status_code == 200:
                        idx_soup = self._soup_from_response(resp)
                        forum_links = extract_forum_links(idx_soup, seen_forums=seen_forums)
                finally:
                    _close_response(resp)

            seen_discuss: set[str] = set()
            for f_url, f_name in forum_links[:4]:
                f_resp = None
                try:
                    f_resp = self.authenticated_get(f_url, timeout=20)
                    if f_resp.status_code != 200:
                        continue
                    f_soup = self._soup_from_response(f_resp)
                    announcements.extend(
                        parse_forum_announcements_soup(f_soup, course_id, f_name, seen_discuss=seen_discuss)
                    )
                except (LMSAuthError, LMSWafError):
                    if raise_auth_errors:
                        raise
                    break
                except Exception as fe:
                    logger.debug("Forum fetch error (%s): %s", f_url, fe)
                finally:
                    _close_response(f_resp)

            logger.debug("المادة %s | announcements: %d إعلان", course_id, len(announcements))
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as exc:
            logger.warning("خطأ في جلب الإعلانات للمادة %s: %s", course_id, exc)
        return announcements

    def get_announcement_details(self, discuss_url: str) -> dict[str, str]:
        """Fetch the body text, author, and date of a specific forum discussion post."""
        info = {"body": "", "author": "", "date": ""}
        resp = None
        try:
            resp = self.authenticated_get(discuss_url, timeout=20)
            if resp.status_code != 200:
                return info
            soup = self._soup_from_response(resp)
            info = parse_announcement_details_soup(soup)
        except Exception as exc:
            logger.debug("Error fetching announcement details %s: %s", discuss_url, exc)
        finally:
            _close_response(resp)
        return info

    def get_course_attendance(
        self,
        course_id: int,
        course_soup: BeautifulSoup | None = None,
        *,
        raise_auth_errors: bool = False,
    ) -> list[dict[str, Any]]:
        """Discover and scrape Moodle Attendance (/mod/attendance/view.php?id=...) for a course."""
        attendance_records: list[dict[str, Any]] = []
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
                    soup = self._soup_from_response(resp)
                finally:
                    _close_response(resp)

            att_links = extract_attendance_links(soup, clean_link_text_fn=self._clean_link_text)

            for cmid, title, base_url in att_links:
                r = None
                try:
                    full_url = f"{LMS_BASE_URL}/mod/attendance/view.php?id={cmid}&view=5"
                    r = self.authenticated_get(full_url, timeout=20)
                    if r.status_code != 200 or "/login/" in (getattr(r, "url", "") or ""):
                        continue
                    att_soup = self._soup_from_response(r)
                    attendance_records.append(
                        parse_attendance_page_soup(att_soup, course_id, cmid, title, base_url)
                    )
                except (LMSAuthError, LMSWafError):
                    if raise_auth_errors:
                        raise
                    break
                except Exception as ae:
                    logger.debug("Attendance fetch error (%s): %s", base_url, ae)
                finally:
                    _close_response(r)

            logger.debug("المادة %s | attendance: %d سجل", course_id, len(attendance_records))
        except (LMSAuthError, LMSWafError):
            if raise_auth_errors:
                raise
        except Exception as exc:
            logger.warning("خطأ في جلب الحضور والغياب للمادة %s: %s", course_id, exc)
        return attendance_records
