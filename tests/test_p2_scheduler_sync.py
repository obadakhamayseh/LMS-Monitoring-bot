"""P2 tests: APScheduler (MonitorEngine & ReminderEngine), services.sync_service, run_with_user_scraper, scraper/ facade."""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

# Configure safe isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as real_db
import lms_scraper
import monitor
import reminder
import scraper as scraper_pkg
from monitor import MonitorEngine
from reminder import ReminderEngine
from scraper.client import FetchResult, LMSAuthError, LMSWafError
from services.sync_service import seed_course_snapshot, sync_user_courses_list


def _init_temp_db(tmp_path: Path, monkeypatch, filename: str = "p2_sync.db") -> None:
    db_file = str(tmp_path / filename)
    monkeypatch.setattr(real_db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(real_db, "DATABASE_URL", "")
    monkeypatch.setattr(real_db, "DIRECT_URL", "")
    real_db.init_db()


# ===========================================================================
# 1. APScheduler replacing manual while True / asyncio.sleep loops
# ===========================================================================

def test_p2_no_manual_polling_loops_in_monitor_and_reminder():
    mon_src = inspect.getsource(monitor)
    rem_src = inspect.getsource(reminder)
    assert "while self._running" not in mon_src
    assert "await asyncio.sleep(60)" not in mon_src
    assert "while self._running" not in rem_src
    assert "await asyncio.sleep(60)" not in rem_src


def test_p2_monitor_apscheduler_config_and_clean_shutdown():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng.POLL_INTERVAL_SECONDS = 3600

        tick_ran = asyncio.Event()

        async def fake_run_tick():
            tick_ran.set()

        eng._run_tick = fake_run_tick
        await eng.start()

        assert isinstance(eng._scheduler, AsyncIOScheduler)
        assert eng._scheduler.running is True
        assert str(eng._scheduler.timezone) == "Asia/Amman"

        job = eng._scheduler.get_job(MonitorEngine.JOB_ID)
        assert job is not None
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 30

        # Initial immediate tick fires on the shared loop
        await asyncio.wait_for(tick_ran.wait(), timeout=2.0)

        # Track an in-flight task and verify stop() shuts down scheduler & awaits it
        order = []

        async def slow_bg():
            await asyncio.sleep(0.05)
            order.append("in_flight_done")

        eng._track_task(slow_bg())
        sched_ref = eng._scheduler
        await eng.stop()
        assert eng._running is False
        assert eng._scheduler is None
        assert sched_ref.running is False
        assert order == ["in_flight_done"]

    asyncio.run(_run())


def test_p2_monitor_tick_timeout_and_backoff():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng._running = True
        eng.TICK_TIMEOUT_SECONDS = 0.05
        eng.TICK_BACKOFF_BASE = 5.0

        calls = 0

        async def hanging_tick():
            nonlocal calls
            calls += 1
            await asyncio.sleep(1.0)

        eng._run_tick = hanging_tick
        t0 = time.monotonic()
        await eng._scheduled_tick()
        assert calls == 1
        assert eng._tick_error_count == 1
        assert eng._tick_backoff_until > t0 + 4.0

        # Subsequent tick during backoff must skip without invoking _run_tick
        await eng._scheduled_tick()
        assert calls == 1

    asyncio.run(_run())


def test_p2_reminder_apscheduler_config_timeout_and_shutdown():
    async def _run():
        bot = MagicMock()
        eng = ReminderEngine(bot=bot)
        eng.POLL_INTERVAL_SECONDS = 3600

        tick_ran = asyncio.Event()

        async def fake_check():
            tick_ran.set()

        eng._check_reminders = fake_check
        await eng.start()

        assert isinstance(eng._scheduler, AsyncIOScheduler)
        assert eng._scheduler.running is True
        assert eng._scheduler.timezone == ZoneInfo("Asia/Amman")

        job = eng._scheduler.get_job(ReminderEngine.JOB_ID)
        assert job is not None
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 30

        await asyncio.wait_for(tick_ran.wait(), timeout=2.0)

        # Verify timeout handling and backoff
        eng.TICK_TIMEOUT_SECONDS = 0.05
        eng.TICK_BACKOFF_BASE = 2.0

        async def slow_check():
            await asyncio.sleep(1.0)

        eng._check_reminders = slow_check
        await eng._scheduled_tick()
        assert eng._tick_error_count == 1
        assert eng._tick_backoff_until > time.monotonic()

        sched_ref = eng._scheduler
        await eng.stop()
        assert eng._running is False
        assert eng._scheduler is None
        assert sched_ref.running is False

    asyncio.run(_run())


# ===========================================================================
# 2. services.sync_service (seed_course_snapshot & sync_user_courses_list)
# ===========================================================================

def test_p2_seed_course_snapshot_persists_all_entities_and_marks_seeded(tmp_path, monkeypatch):
    _init_temp_db(tmp_path, monkeypatch)
    tid = 888101
    cid = 71177
    real_db.save_user(tid, "stu", "secret")
    real_db.add_course(tid, cid, "Simulation", "https://mulms.mutah.edu.jo/course/view.php?id=71177")
    assert real_db.is_course_seeded(tid, cid) is False

    reminder_eng = ReminderEngine(bot=MagicMock())
    content = {
        "assignments": [{
            "id": "assign_10",
            "name": "HW 1",
            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=10",
            "due_date": "15 January 2099, 11:59 PM",
            "is_submitted": False,
        }, {
            "id": "assign_11",
            "name": "HW Done",
            "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=11",
            "due_date": "16 January 2099, 11:59 PM",
            "is_submitted": True,
        }],
        "quizzes": [{
            "id": "quiz_20",
            "name": "Quiz 1",
            "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=20",
            "due_date": "17 January 2099, 11:59 PM",
            "is_submitted": False,
        }],
        "resources": [{
            "id": "resource_30",
            "name": "Chapter 1 PDF",
            "url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=30",
        }],
        "grades": [{
            "item_id": "mod_20",
            "item_name": "Quiz 1",
            "grade_val": "9.5",
            "grade_max": "0–10",
            "percentage": "95%",
            "feedback": "Great",
            "url": "https://mulms.mutah.edu.jo/mod/quiz/view.php?id=20",
        }],
        "announcements": [{
            "discuss_id": "discuss_40",
            "forum_name": "Announcements",
            "title": "Welcome",
            "author": "Dr. Ahmad",
            "posted_date": "2026-11-26",
            "body_text": "Welcome to the course",
            "url": "https://mulms.mutah.edu.jo/mod/forum/discuss.php?d=40",
        }],
        "attendance": [{
            "att_id": "att_50",
            "taken_sessions": 2,
            "total_sessions": 2,
            "present_count": 2,
            "absent_count": 0,
            "late_count": 0,
            "excused_count": 0,
            "points_str": "4/4",
            "percentage_str": "100%",
            "sessions": [{"date": "Sun 15 Nov", "status": "P"}],
            "url": "https://mulms.mutah.edu.jo/mod/attendance/view.php?id=50",
        }],
    }

    summary = seed_course_snapshot(
        real_db,
        tid,
        str(cid),
        content,
        course_name="Simulation",
        mark_seeded=True,
        reminder_engine=reminder_eng,
    )
    assert summary == {
        "course_id": cid,
        "course_name": "Simulation",
        "assignments": 2,
        "quizzes": 1,
        "resources": 1,
        "grades": 1,
        "announcements": 1,
        "attendance": 1,
        "seeded": True,
    }
    assert real_db.is_course_seeded(tid, cid) is True
    assert len(real_db.get_course_items(tid, cid)) == 4
    assert real_db.get_seen_content(tid, "assign_11", "assign")["is_submitted"] == 1
    assert real_db.get_grade(tid, cid, "mod_20")["grade_val"] == "9.5"
    assert real_db.get_announcement(tid, "discuss_40")["title"] == "Welcome"
    assert real_db.get_attendance(tid, cid, "att_50")["present_count"] == 2
    upcoming = real_db.get_upcoming_deadlines(tid, limit=10)
    assert {u["content_id"] for u in upcoming} == {"assign_10", "assign_11", "quiz_20"}


def test_p2_sync_user_courses_list_seeds_new_and_handles_waf(tmp_path, monkeypatch):
    _init_temp_db(tmp_path, monkeypatch, filename="p2_sync_courses.db")
    tid = 888102
    real_db.save_user(tid, "stu", "secret")
    real_db.add_course(tid, 101, "Existing Course", "https://mulms.mutah.edu.jo/course/view.php?id=101")
    real_db.mark_course_seeded(tid, 101, True)

    fake_scraper = MagicMock()
    fake_scraper.waf_blocked = False
    fake_scraper.session_expired = False
    fake_scraper.get_courses = MagicMock(return_value=[
        {"id": 101, "name": "Existing Course", "url": "https://mulms.mutah.edu.jo/course/view.php?id=101"},
        {"id": 102, "name": "New Course", "url": "https://mulms.mutah.edu.jo/course/view.php?id=102"},
    ])
    fake_scraper.fetch_course_content = MagicMock(return_value=FetchResult(
        content={
            "assignments": [],
            "quizzes": [],
            "resources": [{"id": "resource_1", "name": "Syllabus", "url": "https://mulms.mutah.edu.jo/mod/resource/view.php?id=1"}],
            "grades": [],
            "announcements": [],
            "attendance": [],
        },
        ok=True,
    ))

    res = sync_user_courses_list(real_db, fake_scraper, tid, seed_new=True)
    assert res["ok"] is True
    assert res["synced_count"] == 2
    assert res["newly_added"] == ["New Course"]
    assert res["already_present"] == ["Existing Course"]
    assert res["seeded_count"] == 1
    fake_scraper.fetch_course_content.assert_called_once_with(102)
    assert real_db.is_course_seeded(tid, 102) is True

    # WAF stops cleanly without crashing
    waf_scraper = MagicMock()
    waf_scraper.waf_blocked = False
    waf_scraper.session_expired = False
    waf_scraper.get_courses = MagicMock(side_effect=LMSWafError("WAF CAPTCHA"))
    waf_res = sync_user_courses_list(real_db, waf_scraper, tid, seed_new=True)
    assert waf_res["ok"] is False
    assert waf_res["waf_blocked"] is True


# ===========================================================================
# 3. MonitorEngine.run_with_user_scraper helper
# ===========================================================================

def test_p2_run_with_user_scraper_reuses_session_serializes_and_retries_auth():
    async def _run():
        bot = MagicMock()
        eng = MonitorEngine(bot=bot)
        eng.MIN_REQUEST_COOLDOWN_SECONDS = 0.0
        loop_tid = threading.get_ident()

        s1 = MagicMock(username="stu", password="pw", logged_in=False, waf_blocked=False, credentials_invalid=False, last_request_at=0.0)
        s1.login = MagicMock(side_effect=lambda: (setattr(s1, "logged_in", True) or True))

        s2 = MagicMock(username="stu", password="pw", logged_in=False, waf_blocked=False, credentials_invalid=False, last_request_at=0.0)
        s2.login = MagicMock(side_effect=lambda: (setattr(s2, "logged_in", True) or True))

        scrapers = iter([s1, s2])
        user = {"telegram_id": 55, "lms_username": "stu", "lms_password": "pw"}

        worker_threads = []

        def sample_task(sc, x, *, mul=2):
            worker_threads.append(threading.get_ident())
            return (sc, x * mul)

        with patch("monitor.db.get_user", return_value=user), \
             patch("monitor.LMSScraper", side_effect=lambda u, p: next(scrapers)):
            # First call logs in s1 and runs in executor thread
            sc_used_1, val1 = await eng.run_with_user_scraper(55, sample_task, 7, mul=3)
            assert sc_used_1 is s1
            assert val1 == 21
            assert s1.login.call_count == 1
            assert worker_threads[0] != loop_tid

            # Second call reuses already-logged-in cached s1 without re-logging in
            sc_used_2, val2 = await eng.run_with_user_scraper(55, sample_task, 5, mul=2)
            assert sc_used_2 is s1
            assert val2 == 10
            assert s1.login.call_count == 1

            # If func raises LMSAuthError once, helper auto-relogins fresh s2 and retries once
            attempts = {"n": 0}

            def expiring_task(sc):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise LMSAuthError("expired")
                return ("ok", sc)

            status, sc_used_3 = await eng.run_with_user_scraper(55, expiring_task)
            assert status == "ok"
            assert sc_used_3 is s2
            assert s2.login.call_count == 1

    asyncio.run(_run())


# ===========================================================================
# 4. Decomposed scraper/ package & thin lms_scraper.py facade
# ===========================================================================

def test_p2_scraper_package_and_thin_facade():
    facade_lines = Path(lms_scraper.__file__).read_text(encoding="utf-8").splitlines()
    assert len(facade_lines) < 120, f"lms_scraper.py should be a thin facade, got {len(facade_lines)} lines"

    for sym in (
        "LMSScraper",
        "LMSHttpClient",
        "FetchResult",
        "LMSAuthError",
        "LMSWafError",
        "HEADERS",
        "sanitize_filename",
        "parse_submission_state",
        "parse_quiz_grade_text",
        "parse_attendance_status_category",
        "build_attendance_col_map",
        "normalize_due_date_year",
    ):
        assert hasattr(lms_scraper, sym), f"Facade missing {sym}"
        assert hasattr(scraper_pkg, sym), f"scraper package missing {sym}"
        assert getattr(lms_scraper, sym) is getattr(scraper_pkg, sym)
