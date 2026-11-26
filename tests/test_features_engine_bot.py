"""Prove-It tests for Features Phase (F1, F5, F6, F7-Hook, F8):
MoodleApiClient hybrid integration, Daily Digest, Absence Risk alerts,
Web Push secondary hook, Observability /status, stale-monitor watchdog, and bot settings toggles.
"""

from __future__ import annotations

import os
from cryptography.fernet import Fernet

# Configure isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import asyncio
from datetime import datetime, timedelta
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram.ext import CommandHandler, ConversationHandler

import bot
import config
import database as db
from handlers import callbacks as h_callbacks
from handlers import register_handlers
from handlers import views as h_views
from lms_scraper import FetchResult, LMSHttpClient, LMSWafError
from monitor import MonitorEngine
from reminder import ReminderEngine
from scraper.moodle_api import ApiCycleMetrics, MoodleApiClient, MoodleApiError
from services.digest_service import build_daily_digest, is_digest_due
from services.observability import JsonLogFormatter, get_observability

JORDAN_TZ = ZoneInfo("Asia/Amman")


class _FakeResp:
    def __init__(
        self,
        status_code: int = 200,
        json_data=None,
        text: str = "",
        url: str = "https://mulms.mutah.edu.jo/",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.url = url
        self.headers = headers or {"Content-Type": "application/json"}
        self.history = []
        self.closed = False

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def close(self):
        self.closed = True


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_features_engine_bot.db")
    monkeypatch.setattr(db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DIRECT_URL", "")
    db.init_db()
    get_observability().reset()
    yield db_file
    get_observability().reset()


def _make_fake_bot(send_ok: bool = True):
    tg_bot = MagicMock()
    if send_ok:
        tg_bot.send_message = AsyncMock(return_value=True)
    else:
        tg_bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    tg_bot.send_document = AsyncMock(return_value=True)
    return tg_bot


# ── F1: MoodleApiClient + MonitorEngine hybrid integration ──────────────────


def test_f1_moodle_api_client_mobile_token_ajax_and_normalized_fetch():
    session = MagicMock()
    calls: list[tuple[str, dict]] = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "/login/token.php" in url:
            return _FakeResp(200, {"token": "tok_secret_123"}, url=url)
        if "/lib/ajax/service.php" in url:
            return _FakeResp(
                200,
                [{"error": False, "data": {"events": [{"id": 9, "name": "AJAX Quiz", "modulename": "quiz", "cmid": 91, "course": {"id": 101, "fullname": "CS101"}, "timesort": 1796000000}]}}],
                url=url,
            )
        if "/webservice/rest/server.php" in url:
            fn = (kwargs.get("data") or {}).get("wsfunction")
            if fn == "core_calendar_get_action_events_by_timesort":
                return _FakeResp(
                    200,
                    {
                        "events": [
                            {
                                "id": 11,
                                "name": "Quiz 1",
                                "modulename": "quiz",
                                "cmid": 501,
                                "course": {"id": 101, "fullname": "CS101"},
                                "timesort": 1796000000,
                                "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=501",
                            }
                        ]
                    },
                    url=url,
                )
            if fn == "mod_assign_get_assignments":
                assert kwargs["data"].get("courseids[0]") == 101
                return _FakeResp(
                    200,
                    {
                        "courses": [
                            {
                                "id": 101,
                                "assignments": [
                                    {
                                        "id": 7,
                                        "cmid": 201,
                                        "name": "HW 1",
                                        "duedate": 1796050000,
                                        "submissionstatus": "new",
                                    }
                                ],
                            }
                        ]
                    },
                    url=url,
                )
            if fn == "gradereport_user_get_grade_items":
                return _FakeResp(
                    200,
                    {
                        "usergrades": [
                            {
                                "courseid": 101,
                                "gradeitems": [
                                    {
                                        "id": 55,
                                        "cmid": 501,
                                        "itemtype": "mod",
                                        "itemmodule": "quiz",
                                        "itemname": "Quiz 1",
                                        "gradeformatted": "19.0",
                                        "grademax": 20,
                                        "percentageformatted": "95.0 %",
                                        "feedback": "<p>Great job</p>",
                                    }
                                ],
                            }
                        ]
                    },
                    url=url,
                )
        return _FakeResp(404, {}, url=url)

    session.post = MagicMock(side_effect=_fake_post)
    http = LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0), session=session)
    client = MoodleApiClient(http)

    # 1. Mobile token acquisition
    tok = client.obtain_mobile_token("20240001", "pw")
    assert tok == "tok_secret_123"
    assert client.token == "tok_secret_123"
    assert any("/login/token.php?service=moodle_mobile_app" in u for u, _ in calls)

    # 2. Calendar events via mobile WS
    events = client.fetch_calendar_events(1795900000, 1797000000)
    assert len(events) == 1
    assert events[0]["id"] == "quiz_501"
    assert events[0]["type"] == "quiz"
    assert events[0]["course_id"] == 101
    assert events[0]["due_date"] != ""

    # 3. Assignments via mobile WS
    assigns = client.fetch_assignments([101])
    assert len(assigns) == 1
    assert assigns[0]["id"] == "assign_201"
    assert assigns[0]["type"] == "assign"
    assert assigns[0]["course_id"] == 101
    assert assigns[0]["is_submitted"] is False

    # 4. Grades via mobile WS
    grades = client.fetch_grades(101, 42)
    assert len(grades) == 1
    assert grades[0]["item_id"] == "mod_501"
    assert grades[0]["grade_val"] == "19.0"
    assert grades[0]["feedback"] == "Great job"
    assert grades[0]["is_quiz"] is True

    # 5. Secondary AJAX JSON API
    ajax_data = client.call_ajax_service("sk123", "core_calendar_get_action_events_by_timesort", {"limitnum": 5})
    assert ajax_data["events"][0]["name"] == "AJAX Quiz"
    assert any("/lib/ajax/service.php?sesskey=sk123&info=core_calendar_get_action_events_by_timesort" in u for u, _ in calls)

    # 6. Host allowlist enforced via LMSHttpClient (disallowed base_url blocked)
    bad_client = MoodleApiClient(http, base_url="http://evil.example.com")
    with pytest.raises(ValueError):
        bad_client.call_ajax_service("sk123", "core_test", {})


def test_f1_circuit_breaker_on_403_invalidtoken_and_waf():
    session = MagicMock()
    http = LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0), session=session)

    # Case A: HTTP 403 trips circuit breaker
    session.post = MagicMock(return_value=_FakeResp(403, {}, url="https://mulms.mutah.edu.jo/login/token.php"))
    c403 = MoodleApiClient(http, cooldown_seconds=3600)
    assert c403.obtain_mobile_token("u", "p") is None
    assert c403.is_disabled is True
    # Subsequent calls do NOT hit HTTP while circuit breaker is open
    call_count_before = session.post.call_count
    assert c403.obtain_mobile_token("u", "p") is None
    assert session.post.call_count == call_count_before

    # Case B: invalidtoken clears token & trips circuit breaker
    session.post = MagicMock(
        return_value=_FakeResp(
            200,
            {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"},
            url="https://mulms.mutah.edu.jo/webservice/rest/server.php",
        )
    )
    c_tok = MoodleApiClient(http, token="stale_tok", cooldown_seconds=3600)
    with pytest.raises(MoodleApiError) as exc_info:
        c_tok.call_mobile_ws("mod_assign_get_assignments", {"courseids": [101]})
    assert exc_info.value.errorcode == "invalidtoken"
    assert c_tok.token is None
    assert c_tok.is_disabled is True

    # Case C: WAF challenge triggers LMSWafError + LMSHttpClient WAF cooldown
    session.post = MagicMock(
        return_value=_FakeResp(
            200,
            text="<html>User Validation Required /captcha.gif</html>",
            headers={"Content-Type": "text/html"},
            url="https://mulms.mutah.edu.jo/login/token.php",
        )
    )
    c_waf = MoodleApiClient(http)
    with pytest.raises(LMSWafError):
        c_waf.obtain_mobile_token("u", "p")
    assert http.waf_cooldown_active is True


def test_f1_monitor_disabled_by_default_and_hybrid_success_and_fallback(isolated_db, monkeypatch):
    assert config.ENABLE_MOODLE_API is False

    db.save_user(1, "20240001", "pw", "42", "Student")
    db.set_monitoring(1, True)
    db.add_course(1, 101, "Algorithms", "https://mulms.mutah.edu.jo/course/view.php?id=101")
    db.mark_course_seeded(1, 101, True)

    # 1. Default OFF: MoodleApiClient must NEVER be invoked
    tg_bot = _make_fake_bot()
    engine = MonitorEngine(tg_bot)
    user = db.get_user(1)
    assert int(user.get("use_moodle_api", 0)) == 0
    assert engine._should_use_moodle_api(user) is False

    scrape_calls = []

    class FakeScraper:
        def __init__(self, http_client):
            self.http = http_client
            self.logged_in = True
            self.waf_blocked = False
            self.session_expired = False
            self.credentials_invalid = False
            self.error_message = None
            self.user_id = "42"
            self.sesskey = "sk1"
            self.last_request_at = 0.0

        def fetch_course_content(self, cid):
            scrape_calls.append(cid)
            return FetchResult(
                content={"assignments": [], "quizzes": [], "resources": [], "grades": [], "announcements": [], "attendance": []},
                ok=True,
            )

        def close(self):
            pass

    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("API must not be called when disabled"))
    http = LMSHttpClient(min_interval=0.0, jitter=(0.0, 0.0), session=session)
    scraper = FakeScraper(http)
    courses = db.get_user_courses(1, True)

    count, complete = asyncio.run(engine._scan_user(user, courses, scraper))
    assert complete is True
    assert scrape_calls == [101]

    # 2. Opt-in API enabled: API succeeds -> zero HTML fetch_course_content calls + metrics recorded
    db.update_settings(1, use_moodle_api=1)
    user_api = db.get_user(1)
    assert engine._should_use_moodle_api(user_api) is True

    future_ts = int(time.time()) + 86400 * 3

    def _api_ok_post(url, **kwargs):
        if "/login/token.php" in url:
            return _FakeResp(200, {"token": "tok_ok"}, url=url)
        fn = (kwargs.get("data") or {}).get("wsfunction")
        if fn == "core_calendar_get_action_events_by_timesort":
            return _FakeResp(200, {"events": []}, url=url)
        if fn == "mod_assign_get_assignments":
            return _FakeResp(
                200,
                {"courses": [{"id": 101, "assignments": [{"id": 1, "cmid": 301, "name": "API HW", "duedate": future_ts}]}]},
                url=url,
            )
        if fn == "gradereport_user_get_grade_items":
            return _FakeResp(200, {"usergrades": [{"courseid": 101, "gradeitems": []}]}, url=url)
        return _FakeResp(404, {}, url=url)

    session.post = MagicMock(side_effect=_api_ok_post)
    scrape_calls.clear()
    engine._sessions[1] = ("20240001", "pw", scraper)
    monkeypatch.setattr(engine, "_respect_request_cooldown", AsyncMock())

    asyncio.run(engine._check_user(user_api))
    assert scrape_calls == [], "HTML fetch_course_content should be skipped when API succeeds"
    metrics = engine._last_cycle_metrics[1]
    assert isinstance(metrics, ApiCycleMetrics)
    assert metrics.used_api is True
    assert metrics.api_requests == 4
    assert metrics.scrape_requests == 0
    assert metrics.estimated_requests_saved > 0

    snap = get_observability().snapshot()
    assert snap["api_requests_total"] == 4
    assert snap["requests_saved_total"] == metrics.estimated_requests_saved

    # 3. API failure (enablewebservices / 403) -> automatic fallback to HTML scraper
    engine._api_clients.clear()
    engine._course_cache.clear()
    session.post = MagicMock(
        return_value=_FakeResp(
            200,
            {"error": "Web services are not enabled", "errorcode": "enablewebservices"},
            url="https://mulms.mutah.edu.jo/login/token.php",
        )
    )
    scraper.sesskey = None  # force full fallback
    scrape_calls.clear()

    count_fb, complete_fb = asyncio.run(engine._scan_user(user_api, courses, scraper))
    assert complete_fb is True
    assert scrape_calls == [101], "MonitorEngine must fall back to HTML scrape when API fails"
    fb_metrics = engine._last_cycle_metrics[1]
    assert fb_metrics.used_api is False
    assert fb_metrics.scrape_requests == 1
    assert "enablewebservices" in fb_metrics.fallback_reason

    # 4. WAF during API call -> sets _last_scan_waf without falling back to HTML scraper
    engine._api_clients.clear()
    engine._course_cache.clear()
    http.clear_waf_cooldown()
    session.post = MagicMock(
        return_value=_FakeResp(
            200,
            text="<html>User Validation Required /captcha.gif</html>",
            headers={"Content-Type": "text/html"},
            url="https://mulms.mutah.edu.jo/login/token.php",
        )
    )
    scrape_calls.clear()
    _, complete_waf = asyncio.run(engine._scan_user(user_api, courses, scraper))
    assert complete_waf is False
    assert engine._last_scan_waf[1] is True
    assert scrape_calls == [], "Must NOT scrape HTML after WAF challenge on API"


# ── F5: Daily Digest + ReminderEngine scheduling ────────────────────────────


def test_f5_daily_digest_48h_window_absence_risk_and_empty_behavior(isolated_db):
    db.save_user(1, "20240001", "pw", "42", "Student")
    db.add_course(1, 101, "Data Structures", "")
    db.add_course(1, 102, "Calculus", "")

    now_amman = datetime(2026, 11, 27, 8, 5, tzinfo=JORDAN_TZ)
    now_ts = int(now_amman.timestamp())

    # Within 48h (12h away) -> INCLUDED
    db.upsert_reminder(1, 101, "assign_1", "HW <1>", "https://mulms.mutah.edu.jo/a/1", now_ts + 12 * 3600, "2026-11-27 20:05", is_submitted=0)
    # Within 48h quiz (30h away) -> INCLUDED
    db.upsert_reminder(1, 101, "quiz_2", "Quiz 2", "https://mulms.mutah.edu.jo/q/2", now_ts + 30 * 3600, "2026-11-28 14:05", is_submitted=0)
    # Beyond 48h (72h away) -> EXCLUDED
    db.upsert_reminder(1, 101, "assign_3", "Far Future HW", "https://mulms.mutah.edu.jo/a/3", now_ts + 72 * 3600, "2026-11-30 08:05", is_submitted=0)
    # Submitted within 48h -> EXCLUDED
    db.upsert_reminder(1, 101, "assign_4", "Already Done HW", "https://mulms.mutah.edu.jo/a/4", now_ts + 6 * 3600, "2026-11-27 14:05", is_submitted=1)

    # Personal reminder within 48h -> INCLUDED
    db.merge_user_meta(
        1,
        {
            "personal_reminders": [
                {"id": "p1", "title": "Review Notes", "course_name": "Data Structures", "due_ts": now_ts + 20 * 3600},
                {"id": "p2", "title": "Next Week Task", "due_ts": now_ts + 100 * 3600},
            ]
        },
    )

    # Absence risk: course 101 has 6/48 absences (max_allowed=7 -> remaining=1 -> critical) -> INCLUDED
    db.upsert_attendance(1, 101, "att_1", taken_sessions=20, total_sessions=48, present_count=14, absent_count=6)
    # Course 102 is safe (0 absences) -> EXCLUDED
    db.upsert_attendance(1, 102, "att_2", taken_sessions=20, total_sessions=48, present_count=20, absent_count=0)

    digest = build_daily_digest(db, 1, now=now_amman)
    assert digest is not None
    assert "HW &lt;1&gt;" in digest  # HTML escaped
    assert "Quiz 2" in digest
    assert "Review Notes" in digest
    assert "Far Future HW" not in digest
    assert "Already Done HW" not in digest
    assert "Next Week Task" not in digest
    assert "Data Structures" in digest
    assert "تنبيهات خطر الغياب" in digest
    assert "Calculus" not in digest

    # Empty case: user with zero 48h tasks and zero at-risk courses -> None (or calm summary when include_empty=True)
    db.save_user(2, "20240002", "pw", "43", "Calm Student")
    assert build_daily_digest(db, 2, now=now_amman) is None
    calm = build_daily_digest(db, 2, now=now_amman, include_empty=True)
    assert calm is not None and "يومك هادئ" in calm


def test_f5_reminder_engine_digest_scheduling_dedup_and_disable_toggle(isolated_db, monkeypatch):
    db.save_user(1, "20240001", "pw", "42", "Student")
    db.add_course(1, 101, "Algorithms", "")
    db.update_settings(1, digest_enabled=1, digest_time="08:00")

    now_before = datetime(2026, 11, 27, 7, 55, tzinfo=JORDAN_TZ)
    now_due = datetime(2026, 11, 27, 8, 5, tzinfo=JORDAN_TZ)
    db.upsert_reminder(1, 101, "assign_10", "Due Soon", "https://mulms.mutah.edu.jo/a/10", int(now_due.timestamp()) + 10 * 3600, "2026-11-27 18:05")

    # Before 08:00 -> not due
    assert is_digest_due(db.get_user(1), now=now_before)[0] is False

    # Failed Telegram send -> MUST NOT mark_digest_sent
    failing_bot = _make_fake_bot(send_ok=False)
    rem_fail = ReminderEngine(failing_bot)
    assert asyncio.run(rem_fail._check_daily_digests(now=now_due)) == 0
    assert (db.get_user(1).get("last_digest_date") or "") == ""

    # Successful Telegram send -> marks sent & deduplicates same day
    ok_bot = _make_fake_bot(send_ok=True)
    rem_ok = ReminderEngine(ok_bot)
    assert asyncio.run(rem_ok._check_daily_digests(now=now_due)) == 1
    assert db.get_user(1)["last_digest_date"] == "2026-11-27"
    assert ok_bot.send_message.await_count == 1

    # Second call same day -> deduplicated (0 sent)
    assert asyncio.run(rem_ok._check_daily_digests(now=now_due + timedelta(hours=2))) == 0
    assert ok_bot.send_message.await_count == 1

    # Next day with digest_enabled=0 -> skipped
    next_day = now_due + timedelta(days=1)
    db.upsert_reminder(1, 101, "assign_11", "Tomorrow HW", "https://mulms.mutah.edu.jo/a/11", int(next_day.timestamp()) + 10 * 3600, "2026-11-28 18:05")
    db.update_settings(1, digest_enabled=0)
    assert asyncio.run(rem_ok._check_daily_digests(now=next_day)) == 0

    # Scheduler registers DIGEST_JOB_ID on start()
    async def _check_sched():
        await rem_ok.start()
        try:
            job = rem_ok._scheduler.get_job(ReminderEngine.DIGEST_JOB_ID)
            assert job is not None
        finally:
            await rem_ok.stop()

    asyncio.run(_check_sched())


# ── F6: Absence Risk Alerts in MonitorEngine ────────────────────────────────


def test_f6_absence_risk_alert_triggers_dedups_escalates_and_respects_disable(isolated_db, monkeypatch):
    db.save_user(1, "20240001", "pw", "42", "Student")
    db.set_monitoring(1, True)
    db.add_course(1, 101, "Operating Systems", "")
    db.mark_course_seeded(1, 101, True)
    db.update_settings(1, notify_attendance=0, notify_absence_risk=1, max_absence_pct=15.0)

    def _make_content(absent_count: int):
        return {
            "assignments": [],
            "quizzes": [],
            "resources": [],
            "grades": [],
            "announcements": [],
            "attendance": [
                {
                    "att_id": "att_101",
                    "taken_sessions": 20,
                    "total_sessions": 48,
                    "present_count": 20 - absent_count,
                    "absent_count": absent_count,
                    "late_count": 0,
                    "excused_count": 0,
                    "points_str": "",
                    "percentage_str": "",
                    "sessions": [],
                    "url": "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=101",
                }
            ],
        }

    # 1. Warning level (5/48 -> max_allowed=7, remaining=2 -> warning)
    # First simulate failed Telegram send -> last_risk_level MUST remain empty
    fail_bot = _make_fake_bot(send_ok=False)
    eng_fail = MonitorEngine(fail_bot)
    course = db.get_course(1, 101)
    user = db.get_user(1)
    _, ok_fail, _, _ = asyncio.run(
        eng_fail._check_course_result(1, user, MagicMock(), course, bypass_cache=True, prefetched_content=_make_content(5))
    )
    assert ok_fail is False
    assert (db.get_course(1, 101).get("last_risk_level") or "") == ""

    # 2. Successful send -> alerts warning & persists last_risk_level='warning'
    ok_bot = _make_fake_bot(send_ok=True)
    eng = MonitorEngine(ok_bot)
    _, ok_warn, _, _ = asyncio.run(
        eng._check_course_result(1, user, MagicMock(), db.get_course(1, 101), bypass_cache=True, prefetched_content=_make_content(5))
    )
    assert ok_warn is True
    assert ok_bot.send_message.await_count == 1
    sent_text = ok_bot.send_message.await_args.kwargs["text"]
    assert "بقي لك 2 غيابات" in sent_text
    assert "Operating Systems" in sent_text
    assert db.get_course(1, 101)["last_risk_level"] == "warning"

    # 3. Same level again -> deduplicated (no duplicate message)
    asyncio.run(
        eng._check_course_result(1, user, MagicMock(), db.get_course(1, 101), bypass_cache=True, prefetched_content=_make_content(5))
    )
    assert ok_bot.send_message.await_count == 1

    # 4. Escalation to critical (6/48 -> remaining=1 -> critical) -> sends new alert!
    asyncio.run(
        eng._check_course_result(1, user, MagicMock(), db.get_course(1, 101), bypass_cache=True, prefetched_content=_make_content(6))
    )
    assert ok_bot.send_message.await_count == 2
    assert db.get_course(1, 101)["last_risk_level"] == "critical"

    # 5. Disabled toggle (`notify_absence_risk=0`) -> skips even on escalation to deprived
    db.update_settings(1, notify_absence_risk=0)
    user_off = db.get_user(1)
    asyncio.run(
        eng._check_course_result(1, user_off, MagicMock(), db.get_course(1, 101), bypass_cache=True, prefetched_content=_make_content(7))
    )
    assert ok_bot.send_message.await_count == 2


# ── F7: Web Push Secondary Channel Hook ─────────────────────────────────────


def test_f7_web_push_hook_isolated_from_telegram_in_monitor_and_reminder(isolated_db, monkeypatch):
    db.save_user(1, "20240001", "pw", "42", "Student")
    db.set_monitoring(1, True)
    db.add_course(1, 101, "Networks", "")
    db.mark_course_seeded(1, 101, True)
    db.update_settings(1, enable_web_push=1)

    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", True)

    push_calls = []

    def _boom_push(db_mod, tid, title, body, url="/webapp"):
        push_calls.append((tid, title, body, url))
        raise RuntimeError("VAPID push network explosion")

    import monitor as mon_mod
    import reminder as rem_mod

    monkeypatch.setattr(mon_mod, "send_web_push_to_user", _boom_push)
    monkeypatch.setattr(rem_mod, "send_web_push_to_user", _boom_push)

    # 1. MonitorEngine grade alert succeeds & persists DB even when Web Push raises
    tg_bot = _make_fake_bot(send_ok=True)
    eng = MonitorEngine(tg_bot)
    content = {
        "assignments": [],
        "quizzes": [],
        "resources": [],
        "grades": [
            {
                "item_id": "mod_900",
                "item_name": "Midterm",
                "grade_val": "27",
                "grade_max": "30",
                "percentage": "90%",
                "feedback": "",
                "url": "https://mulms.mutah.edu.jo/grade/1",
            }
        ],
        "announcements": [],
        "attendance": [],
    }
    _, ok, _, _ = asyncio.run(
        eng._check_course_result(1, db.get_user(1), MagicMock(), db.get_course(1, 101), bypass_cache=True, prefetched_content=content)
    )
    assert ok is True
    assert tg_bot.send_message.await_count == 1
    assert db.get_grade(1, 101, "mod_900")["grade_val"] == "27"
    assert len(push_calls) == 1

    # 2. ReminderEngine reminder send succeeds even when Web Push raises
    rem_eng = ReminderEngine(tg_bot)
    sent_rem = asyncio.run(
        rem_eng._send_reminder(
            1,
            {"course_id": 101, "content_name": "Lab 1", "content_url": "", "due_str": "2026-11-28 12:00"},
            "24h",
        )
    )
    assert sent_rem is True
    assert len(push_calls) == 2

    # 3. Disabled globally -> Web Push never called
    monkeypatch.setattr(config, "ENABLE_WEB_PUSH", False)
    asyncio.run(
        rem_eng._send_reminder(
            1,
            {"course_id": 101, "content_name": "Lab 2", "content_url": "", "due_str": "2026-11-28 12:00"},
            "3h",
        )
    )
    assert len(push_calls) == 2


# ── F8: Observability /status, stale-monitor watchdog, JSON logs & settings ──


def test_f8_status_cmd_output_and_registration(isolated_db, monkeypatch):
    obs = get_observability()
    obs.set_monitor_running(True)
    obs.record_check_success({"api_requests": 5, "scrape_requests": 2, "requests_saved": 15}, now_ts=1795700000.0)
    obs.record_check_error("Sample timeout")

    # Verify /status registered in register_handlers
    registered = []
    fake_app = MagicMock()
    fake_app.add_handler = MagicMock(side_effect=lambda h, *a, **kw: registered.append(h))
    fake_app.add_error_handler = MagicMock()
    register_handlers(fake_app)
    cmd_names = set()
    for h in registered:
        if isinstance(h, CommandHandler):
            cmd_names.update(h.commands)
    assert "status" in cmd_names

    # Invoke status_cmd as owner
    captured = {}

    async def _fake_send(obj, text, kb=None, edit=False):
        captured["text"] = text

    monkeypatch.setattr(h_views, "_send", _fake_send)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=MagicMock(), callback_query=None)
    res = asyncio.run(h_views.status_cmd(upd, SimpleNamespace(bot_data={}, user_data={})))
    assert res == ConversationHandler.END
    txt = captured["text"]
    assert "Amman" in txt and "UTC" in txt
    assert "يعمل" in txt
    assert "2" in txt  # total checks (1 success + 1 error)
    assert "50.0%" in txt  # error rate
    assert "15" in txt  # saved requests
    assert "Sample timeout" in txt

    # Non-owner blocked
    stranger = SimpleNamespace(
        effective_user=SimpleNamespace(id=99999),
        message=SimpleNamespace(reply_text=AsyncMock()),
        callback_query=None,
    )
    asyncio.run(h_views.status_cmd(stranger, SimpleNamespace(bot_data={}, user_data={})))
    assert stranger.message.reply_text.await_count == 1


def test_f8_stale_monitor_alert_and_deduplication(isolated_db, monkeypatch):
    obs = get_observability()
    base_ts = 1_700_000_000.0
    obs.reset(started_at=base_ts)
    obs.set_monitor_running(True)
    obs.record_check_success(now_ts=base_ts)

    monkeypatch.setattr(config, "ENABLE_STALE_ALERT", True)
    monkeypatch.setattr(config, "STALE_MONITOR_SECONDS", 3600)

    tg_bot = _make_fake_bot(send_ok=True)
    eng = MonitorEngine(tg_bot)

    # 1. No active monitoring users -> must NOT alert even after >3600s
    assert asyncio.run(eng._check_stale_monitor(now_ts=base_ts + 4000.0)) is False
    assert tg_bot.send_message.await_count == 0

    # 2. Add active monitoring user -> still fresh at +1800s
    db.save_user(1, "20240001", "pw", "42", "Student")
    db.set_monitoring(1, True)
    assert asyncio.run(eng._check_stale_monitor(now_ts=base_ts + 1800.0)) is False

    # 3. Past >3600s -> sends alert ONCE to OWNER_TELEGRAM_ID
    assert asyncio.run(eng._check_stale_monitor(now_ts=base_ts + 3605.0)) is True
    assert tg_bot.send_message.await_count == 1
    assert tg_bot.send_message.await_args.kwargs["chat_id"] == int(config.OWNER_TELEGRAM_ID)

    # 4. Deduplicated on subsequent watchdog ticks while still stale
    assert asyncio.run(eng._check_stale_monitor(now_ts=base_ts + 5000.0)) is False
    assert tg_bot.send_message.await_count == 1

    # 5. Recovery resets stale_alerted; going stale again re-alerts
    obs.record_check_success(now_ts=base_ts + 5100.0)
    assert asyncio.run(eng._check_stale_monitor(now_ts=base_ts + 9000.0)) is True
    assert tg_bot.send_message.await_count == 2


def test_f8_bot_json_log_formatter_with_rotating_file_handler():
    handler = getattr(bot, "_BOT_LOG_HANDLER", None)
    assert isinstance(handler, RotatingFileHandler)
    assert isinstance(handler.formatter, JsonLogFormatter)

    rec = logging.LogRecord(
        name="bot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Structured check %s",
        args=("ok",),
        exc_info=None,
    )
    formatted = handler.formatter.format(rec)
    assert "\n" not in formatted
    parsed = json.loads(formatted)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "bot.test"
    assert parsed["message"] == "Structured check ok"
    assert parsed["timestamp"].endswith("Z")


@pytest.mark.parametrize(
    "cb_data,col_name,expected_after",
    [
        ("sett_digest", "digest_enabled", 0),
        ("sett_absence_risk", "notify_absence_risk", 0),
        ("sett_moodle_api", "use_moodle_api", 1),
    ],
)
def test_f8_settings_feature_toggles_persist_and_answer_once(
    isolated_db, monkeypatch, cb_data, col_name, expected_after
):
    db.save_user(1, "20240001", "pw", "42", "Student")
    monkeypatch.setattr(h_callbacks, "_render_settings", AsyncMock())

    msg = MagicMock()
    q = MagicMock()
    q.data = cb_data
    q.message = msg
    q.answer = AsyncMock()

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=q,
        message=None,
    )
    ctx = SimpleNamespace(bot_data={}, user_data={})

    asyncio.run(h_callbacks.handle_callback(upd, ctx))

    assert q.answer.await_count == 1
    assert db.get_user(1)[col_name] == expected_after
