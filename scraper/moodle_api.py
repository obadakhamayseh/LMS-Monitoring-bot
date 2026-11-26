"""Hybrid Moodle Mobile REST & AJAX JSON API client (F1).

All HTTP traffic is routed strictly through `LMSHttpClient` so the HTTPS host
allowlist, RLock thread-safety, pacing/jitter, and NetScaler WAF detection are
always preserved.

Enabled ONLY when `config.ENABLE_MOODLE_API` or `user.use_moodle_api` is true
(OFF by default per DECISIONS.md D2). Includes a per-client circuit breaker
(`disable_for`) to avoid hammering disabled web services.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from config import LMS_BASE_URL
from scraper.client import (
    LMSAuthError,
    LMSHttpClient,
    LMSWafError,
    _assert_allowed_url,
    _close_response,
    clean_url,
    is_waf_response,
)

logger = logging.getLogger(__name__)
JORDAN_TZ = ZoneInfo("Asia/Amman")

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Moodle error codes that indicate web services or tokens are unavailable/disabled
_CIRCUIT_BREAKER_ERROR_CODES = frozenset({
    "enablewebservices",
    "servicenotavailable",
    "webservice_disabled",
    "invalidtoken",
    "accessexception",
    "nopermissions",
    "webservice_access_exception",
    "wsdisabled",
})

# HTTP status codes that indicate the endpoint is disabled/forbidden/missing
_CIRCUIT_BREAKER_HTTP_STATUSES = frozenset({403, 404, 405, 501})


class MoodleApiError(RuntimeError):
    """Raised when a Moodle Mobile WS or AJAX JSON API call fails."""

    def __init__(
        self,
        message: str,
        *,
        errorcode: str = "",
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.errorcode = errorcode
        self.status_code = status_code


@dataclass
class ApiCycleMetrics:
    """Metrics recorded for a single monitoring cycle (API-first vs HTML fallback)."""

    used_api: bool
    api_requests: int
    scrape_requests: int
    estimated_requests_saved: int
    fallback_reason: str = ""


def _strip_html(raw: Any) -> str:
    if not raw:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", str(raw))
    return " ".join(cleaned.split()).strip()


def _format_amman_ts(epoch_ts: Any) -> str:
    try:
        ts = int(epoch_ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JORDAN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def _flatten_moodle_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Flatten nested lists/dicts into Moodle REST form-encoded keys (e.g. courseids[0]=101)."""
    if not params:
        return {}
    flat: dict[str, Any] = {}

    def _walk(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            for idx, item in enumerate(value):
                _walk(f"{prefix}[{idx}]", item)
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(f"{prefix}[{k}]", v)
        elif isinstance(value, bool):
            flat[prefix] = 1 if value else 0
        else:
            flat[prefix] = value

    for key, val in params.items():
        _walk(str(key), val)
    return flat


class MoodleApiClient:
    """Moodle Mobile REST + AJAX JSON API client routed strictly through `LMSHttpClient`."""

    DEFAULT_COOLDOWN_SECONDS = 3600.0

    def __init__(
        self,
        http_client: LMSHttpClient,
        base_url: str = LMS_BASE_URL,
        *,
        token: Optional[str] = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.http = http_client
        self.base_url = (base_url or LMS_BASE_URL).rstrip("/")
        self.token: Optional[str] = token
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._lock = threading.RLock()
        self._disabled_until: float = 0.0
        self._disable_reason: str = ""
        self._request_count: int = 0

    @property
    def is_disabled(self) -> bool:
        with self._lock:
            return time.monotonic() < self._disabled_until

    @property
    def disable_reason(self) -> str:
        with self._lock:
            if time.monotonic() >= self._disabled_until:
                return ""
            return self._disable_reason

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def reset_request_count(self) -> int:
        """Reset and return the previous request counter value."""
        with self._lock:
            prev = self._request_count
            self._request_count = 0
            return prev

    def disable_for(self, seconds: float = 3600.0, reason: str = "") -> None:
        """Trip the API circuit breaker for `seconds` so disabled endpoints are not spammed."""
        dur = max(0.0, float(seconds))
        with self._lock:
            self._disabled_until = time.monotonic() + dur
            if reason:
                self._disable_reason = reason
        logger.warning(
            "MoodleApiClient disabled for %.0fs (reason=%s)",
            dur,
            reason or "unspecified",
        )

    def reset_circuit_breaker(self) -> None:
        with self._lock:
            self._disabled_until = 0.0
            self._disable_reason = ""

    def _build_url(self, path: str) -> str:
        rel = path.lstrip("/")
        url = f"{self.base_url}/{rel}"
        return _assert_allowed_url(url)

    def _inc_requests(self) -> None:
        with self._lock:
            self._request_count += 1

    def _handle_waf_or_status(self, resp: Any, *, endpoint_label: str) -> None:
        if is_waf_response(resp):
            self.http.trigger_waf_cooldown()
            raise LMSWafError(
                "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) أثناء استدعاء Moodle API"
            )
        status = getattr(resp, "status_code", 200)
        if status in _CIRCUIT_BREAKER_HTTP_STATUSES:
            reason = f"{endpoint_label}_http_{status}"
            self.disable_for(self.cooldown_seconds, reason=reason)
            raise MoodleApiError(
                f"Moodle API endpoint returned HTTP {status}",
                errorcode=f"http_{status}",
                status_code=status,
            )
        if status >= 400:
            raise MoodleApiError(
                f"Moodle API HTTP error {status}",
                errorcode=f"http_{status}",
                status_code=status,
            )

    def obtain_mobile_token(
        self,
        username: str,
        password: str,
        *,
        service: str = "moodle_mobile_app",
        timeout: int = 20,
    ) -> Optional[str]:
        """Acquire a Moodle Mobile WS token via `POST /login/token.php?service=moodle_mobile_app`."""
        if self.is_disabled:
            return None
        if not username or not password:
            return None

        url = self._build_url(f"login/token.php?service={quote_plus(service)}")
        resp = None
        try:
            self._inc_requests()
            resp = self.http.post(
                url,
                data={
                    "username": username,
                    "password": password,
                    "service": service,
                },
                timeout=timeout,
            )
            if is_waf_response(resp):
                self.http.trigger_waf_cooldown()
                raise LMSWafError(
                    "جدار حماية موقع الجامعة طلب تحققاً أمنياً (CAPTCHA) أثناء طلب رمز Moodle API"
                )
            status = getattr(resp, "status_code", 200)
            if status in _CIRCUIT_BREAKER_HTTP_STATUSES:
                self.disable_for(self.cooldown_seconds, reason=f"token_http_{status}")
                return None
            if status != 200:
                logger.warning("Moodle token endpoint returned HTTP %s", status)
                return None

            try:
                payload = resp.json()
            except Exception:
                self.disable_for(self.cooldown_seconds, reason="token_non_json")
                return None

            if not isinstance(payload, dict):
                self.disable_for(self.cooldown_seconds, reason="token_invalid_payload")
                return None

            tok = (payload.get("token") or "").strip()
            if tok:
                with self._lock:
                    self.token = tok
                return tok

            errorcode = str(payload.get("errorcode") or payload.get("exception") or "").strip().lower()
            err_msg = str(payload.get("error") or payload.get("message") or errorcode or "token_denied")
            if errorcode in _CIRCUIT_BREAKER_ERROR_CODES or "web services" in err_msg.lower():
                self.disable_for(self.cooldown_seconds, reason=errorcode or "enablewebservices")
            else:
                logger.warning("Moodle mobile token unavailable (errorcode=%s)", errorcode or "unknown")
            return None
        except LMSWafError:
            raise
        except Exception as exc:
            logger.warning("Failed to obtain Moodle mobile token: %s", exc)
            return None
        finally:
            _close_response(resp)

    def call_mobile_ws(
        self,
        wsfunction: str,
        params: Optional[dict[str, Any]] = None,
        *,
        token: Optional[str] = None,
        timeout: int = 20,
    ) -> Any:
        """Invoke a Moodle REST webservice function via `/webservice/rest/server.php?moodlewsrestformat=json`."""
        if self.is_disabled:
            raise MoodleApiError(
                f"Moodle API disabled ({self.disable_reason or 'cooldown'})",
                errorcode="circuit_open",
            )
        active_token = (token or self.token or "").strip()
        if not active_token:
            raise MoodleApiError("Missing Moodle mobile token", errorcode="missing_token")

        url = self._build_url("webservice/rest/server.php?moodlewsrestformat=json")
        form_data: dict[str, Any] = {
            "wstoken": active_token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        form_data.update(_flatten_moodle_params(params))

        resp = None
        try:
            self._inc_requests()
            resp = self.http.post(url, data=form_data, timeout=timeout)
            self._handle_waf_or_status(resp, endpoint_label=wsfunction)

            try:
                data = resp.json()
            except Exception as exc:
                self.disable_for(self.cooldown_seconds, reason="ws_non_json")
                raise MoodleApiError("Invalid JSON response from Moodle WS", errorcode="non_json") from exc

            if isinstance(data, dict) and (data.get("exception") or data.get("errorcode")):
                errorcode = str(data.get("errorcode") or data.get("exception") or "").strip().lower()
                msg = str(data.get("message") or data.get("error") or errorcode or "ws_error")
                if errorcode == "invalidtoken":
                    with self._lock:
                        self.token = None
                if errorcode in _CIRCUIT_BREAKER_ERROR_CODES:
                    self.disable_for(self.cooldown_seconds, reason=errorcode)
                raise MoodleApiError(msg, errorcode=errorcode)

            return data
        finally:
            _close_response(resp)

    def call_ajax_service(
        self,
        sesskey: str,
        methodname: str,
        args: Optional[dict[str, Any]] = None,
        *,
        timeout: int = 20,
    ) -> Any:
        """Invoke Moodle session-based AJAX JSON API via `POST /lib/ajax/service.php?sesskey=...&info=...`."""
        if self.is_disabled:
            raise MoodleApiError(
                f"Moodle API disabled ({self.disable_reason or 'cooldown'})",
                errorcode="circuit_open",
            )
        sk = (sesskey or "").strip()
        if not sk:
            raise MoodleApiError("Missing Moodle sesskey", errorcode="missing_sesskey")

        url = self._build_url(
            f"lib/ajax/service.php?sesskey={quote_plus(sk)}&info={quote_plus(methodname)}"
        )
        payload = [
            {
                "index": 0,
                "methodname": methodname,
                "args": args or {},
            }
        ]
        resp = None
        try:
            self._inc_requests()
            resp = self.http.post(url, json=payload, timeout=timeout)
            final_url = (getattr(resp, "url", "") or "").lower()
            if "/login/index.php" in final_url:
                raise LMSAuthError("انتهت جلسة LMS أثناء استدعاء AJAX API")
            self._handle_waf_or_status(resp, endpoint_label=f"ajax_{methodname}")

            try:
                body = resp.json()
            except Exception as exc:
                raise MoodleApiError("Invalid JSON response from Moodle AJAX", errorcode="non_json") from exc

            if isinstance(body, list) and body:
                first = body[0]
                if isinstance(first, dict) and first.get("error"):
                    exc_info = first.get("exception") if isinstance(first.get("exception"), dict) else {}
                    errorcode = str(exc_info.get("errorcode") or first.get("errorcode") or "ajax_error").lower()
                    msg = str(exc_info.get("message") or first.get("message") or errorcode)
                    if errorcode in ("servicerequireslogin", "invalidsesskey", "requireloginerror"):
                        raise LMSAuthError(msg)
                    if errorcode in _CIRCUIT_BREAKER_ERROR_CODES:
                        self.disable_for(self.cooldown_seconds, reason=errorcode)
                    raise MoodleApiError(msg, errorcode=errorcode)
                return first.get("data") if isinstance(first, dict) else first

            if isinstance(body, dict) and (body.get("error") or body.get("errorcode")):
                errorcode = str(body.get("errorcode") or "ajax_error").lower()
                msg = str(body.get("error") or body.get("message") or errorcode)
                if errorcode in ("servicerequireslogin", "invalidsesskey", "requireloginerror"):
                    raise LMSAuthError(msg)
                if errorcode in _CIRCUIT_BREAKER_ERROR_CODES:
                    self.disable_for(self.cooldown_seconds, reason=errorcode)
                raise MoodleApiError(msg, errorcode=errorcode)

            return body
        finally:
            _close_response(resp)

    def fetch_calendar_events(
        self,
        timesortfrom: int,
        timesortto: int,
        *,
        limitnum: int = 50,
        sesskey: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch upcoming action events (`core_calendar_get_action_events_by_timesort`).

        Uses Mobile WS when a token is available, or falls back to session AJAX when `sesskey` is provided.
        """
        params = {
            "timesortfrom": int(timesortfrom),
            "timesortto": int(timesortto),
            "limitnum": max(1, int(limitnum)),
        }
        if self.token:
            raw = self.call_mobile_ws("core_calendar_get_action_events_by_timesort", params)
        elif sesskey:
            raw = self.call_ajax_service(
                sesskey, "core_calendar_get_action_events_by_timesort", params
            )
        else:
            raise MoodleApiError(
                "Neither mobile token nor sesskey available for calendar events",
                errorcode="missing_auth",
            )

        events_raw = raw.get("events", []) if isinstance(raw, dict) else []
        normalized: list[dict[str, Any]] = []
        for ev in events_raw:
            if not isinstance(ev, dict):
                continue
            mod = str(ev.get("modulename") or "").strip().lower()
            instance = ev.get("instance")
            cmid = ev.get("coursemoduleid") or ev.get("cmid") or instance or ev.get("id")
            course_obj = ev.get("course") if isinstance(ev.get("course"), dict) else {}
            cid_raw = course_obj.get("id") or ev.get("courseid") or 0
            try:
                cid = int(cid_raw)
            except (TypeError, ValueError):
                cid = 0
            course_name = str(course_obj.get("fullname") or course_obj.get("shortname") or "").strip()

            ev_url = str(ev.get("url") or "").strip()
            if not ev_url and isinstance(ev.get("action"), dict):
                ev_url = str(ev["action"].get("url") or "").strip()
            if not ev_url and mod and cmid:
                ev_url = f"{self.base_url}/mod/{mod}/view.php?id={cmid}"
            ev_url = clean_url(ev_url) if ev_url else ""

            ts = ev.get("timesort") or ev.get("timestart") or 0
            try:
                due_ts = int(ts) if ts else 0
            except (TypeError, ValueError):
                due_ts = 0

            if mod == "quiz":
                item_type = "quiz"
                item_id = f"quiz_{cmid}" if cmid else f"quiz_{ev.get('id', '')}"
            elif mod == "assign":
                item_type = "assign"
                item_id = f"assign_{cmid}" if cmid else f"assign_{ev.get('id', '')}"
            else:
                item_type = mod or "event"
                item_id = f"{item_type}_{cmid}" if cmid else f"event_{ev.get('id', '')}"

            action = ev.get("action") if isinstance(ev.get("action"), dict) else {}
            is_actionable = action.get("actionable", True)
            normalized.append({
                "id": item_id,
                "event_id": ev.get("id"),
                "name": str(ev.get("name") or "").strip() or f"{item_type} #{cmid}",
                "url": ev_url,
                "type": item_type,
                "modulename": mod,
                "course_id": cid,
                "course_name": course_name,
                "due_date": _format_amman_ts(due_ts),
                "due_ts": due_ts or None,
                "is_submitted": not bool(is_actionable) and bool(action),
                "submission_text": "",
            })
        return normalized

    def fetch_assignments(
        self,
        course_ids: list[int],
        *,
        sesskey: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch assignments (`mod_assign_get_assignments`) normalized to LMSScraper shape."""
        clean_cids = [int(c) for c in course_ids if int(c) > 0]
        if not clean_cids:
            return []

        params = {"courseids": clean_cids}
        if self.token:
            raw = self.call_mobile_ws("mod_assign_get_assignments", params)
        elif sesskey:
            raw = self.call_ajax_service(sesskey, "mod_assign_get_assignments", params)
        else:
            raise MoodleApiError("Missing Moodle mobile token for assignments", errorcode="missing_token")

        courses_raw = raw.get("courses", []) if isinstance(raw, dict) else []
        normalized: list[dict[str, Any]] = []
        for course_entry in courses_raw:
            if not isinstance(course_entry, dict):
                continue
            try:
                cid = int(course_entry.get("id") or 0)
            except (TypeError, ValueError):
                cid = 0
            for item in course_entry.get("assignments") or []:
                if not isinstance(item, dict):
                    continue
                cmid = item.get("cmid") or item.get("id")
                if not cmid:
                    continue
                due_ts_raw = item.get("duedate") or 0
                try:
                    due_ts = int(due_ts_raw)
                except (TypeError, ValueError):
                    due_ts = 0
                url = clean_url(f"{self.base_url}/mod/assign/view.php?id={cmid}")
                sub_state = str(item.get("submissionstatus") or "").strip().lower()
                is_sub = bool(item.get("is_submitted")) or sub_state in ("submitted", "graded")
                normalized.append({
                    "id": f"assign_{cmid}",
                    "name": str(item.get("name") or f"assign #{cmid}").strip(),
                    "url": url,
                    "type": "assign",
                    "course_id": cid,
                    "due_date": _format_amman_ts(due_ts),
                    "due_ts": due_ts or None,
                    "is_submitted": is_sub,
                    "submission_text": "تم التسليم" if is_sub else "",
                })
        return normalized

    def fetch_grades(
        self,
        course_id: int,
        user_id: int | str,
        *,
        sesskey: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch course grade items (`gradereport_user_get_grade_items`) normalized to LMSScraper shape."""
        cid = int(course_id)
        try:
            uid = int(str(user_id).strip())
        except (TypeError, ValueError) as exc:
            raise MoodleApiError("Invalid Moodle user_id for grades", errorcode="invalid_userid") from exc

        params = {"courseid": cid, "userid": uid}
        if self.token:
            raw = self.call_mobile_ws("gradereport_user_get_grade_items", params)
        elif sesskey:
            raw = self.call_ajax_service(sesskey, "gradereport_user_get_grade_items", params)
        else:
            raise MoodleApiError("Missing Moodle mobile token for grades", errorcode="missing_token")

        usergrades = raw.get("usergrades", []) if isinstance(raw, dict) else []
        normalized: list[dict[str, Any]] = []
        for ug in usergrades:
            if not isinstance(ug, dict):
                continue
            for item in ug.get("gradeitems") or []:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("itemtype") or "").strip().lower()
                item_module = str(item.get("itemmodule") or "").strip().lower()
                if item_type == "category":
                    continue
                cmid = item.get("cmid")
                gid = item.get("id")
                item_id = f"mod_{cmid}" if cmid else f"grade_{gid}"
                item_name = str(item.get("itemname") or ("المجموع الكلي" if item_type == "course" else f"Item #{gid}")).strip()

                grade_formatted = _strip_html(item.get("gradeformatted"))
                if grade_formatted in ("-", "—", ""):
                    raw_g = item.get("graderaw")
                    grade_val = "" if raw_g is None else str(raw_g)
                else:
                    grade_val = grade_formatted

                grade_max_raw = item.get("grademax")
                grade_max = "" if grade_max_raw is None else str(grade_max_raw)
                pct = _strip_html(item.get("percentageformatted"))
                if pct in ("-", "—"):
                    pct = ""
                feedback = _strip_html(item.get("feedback"))

                if cmid and item_module:
                    item_url = clean_url(f"{self.base_url}/mod/{item_module}/view.php?id={cmid}")
                else:
                    item_url = clean_url(f"{self.base_url}/grade/report/user/index.php?id={cid}")

                normalized.append({
                    "item_id": item_id,
                    "item_name": item_name,
                    "grade_val": grade_val,
                    "grade_max": grade_max,
                    "percentage": pct,
                    "feedback": feedback,
                    "url": item_url,
                    "course_id": cid,
                    "is_quiz": item_module == "quiz",
                })
        return normalized


__all__ = [
    "ApiCycleMetrics",
    "MoodleApiClient",
    "MoodleApiError",
]
